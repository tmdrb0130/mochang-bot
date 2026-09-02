"""문항 생성 + 후처리 (글자 수 절단, 마크다운 제거)."""
import re

from ..llm.client import LLMClient, load_config
from . import assemble
from . import outline as outline_mod

_MD_LINE_PREFIX = re.compile(r"^\s*(#{1,6}|\*|-)\s+")

_SENTENCE_ENDS = ("다.", "요.", ".", "!", "?")

# 이렇게 끝나면 "정의형 한 줄" 로 떨어진 것이다 — 짜임을 바꿔 한 번만 다시 생성한다 (작업 15 후속).
# 프롬프트로 금지해도 라마가 우회하므로(실호출 25건) 코드에서 걸러 낸다.
_DEFINITION_ENDING = re.compile("(제공(합니다|하는|하여)|앱입니다|서비스입니다|플랫폼입니다)[ .]*$")


# ── 짧게 끝난 긴 문항 자동 이어쓰기 (작업 18) ──
# 라마가 2,000자 문항을 700~900자에서 끝내 버리는 일이 잦다(작업 16 검증에서 Q2 698자).
# 사용자가 '이어쓰기'를 손으로 누르면 되는 기능이지만, 목표의 70%도 못 채운 글은 사실상 미완성이라
# 서버가 한 번만 대신 눌러 준다. 모델 호출이 문항당 최대 1회 늘어나므로 config 로 끌 수 있다.
AUTO_EXTEND_DEFAULTS = {"enabled": True, "min_ratio": 0.7, "min_limit": 500}

# ── 짧은 문항 한도 초과 → 압축 재생성 (작업 21) ──
# Q1·Q10 은 한 문장이라 한도에서 자르면 말이 끊긴 채 사용자에게 간다("…식재료" 처럼).
# 자르는 대신 "같은 뜻으로 줄여 다시" 를 최대 SHORTEN_TRIES 회 시킨다.
# ── 사업계획 골자 공유 (작업 20) ──
OUTLINE_DEFAULTS = {"enabled": True, "cache_ttl_seconds": 7 * 24 * 3600}
_outline_cfg: dict | None = None


def outline_settings(form: dict | None = None) -> dict:
    """config.yaml generate.outline 설정. form 의 outline_settings 가 있으면 그것을 우선(테스트용)."""
    global _outline_cfg
    if form and isinstance(form.get("outline_settings"), dict):
        return {**OUTLINE_DEFAULTS, **form["outline_settings"]}
    if _outline_cfg is None:
        try:
            _outline_cfg = dict((load_config().get("generate") or {}).get("outline") or {})
        except Exception:
            _outline_cfg = {}
    return {**OUTLINE_DEFAULTS, **_outline_cfg}


SHORT_LIMIT = 100          # 이 한도 이하를 '짧은 문항' 으로 본다
SHORTEN_TRIES = 2
SHORTEN_MARGIN = 15        # 목표를 한도보다 이만큼 낮게 잡아 여유를 둔다
_auto_extend_cfg: dict | None = None


def auto_extend_settings(form: dict | None = None) -> dict:
    """config.yaml auto_extend 설정. form 에 auto_extend 가 있으면 그것을 우선한다(테스트·개별 요청용)."""
    global _auto_extend_cfg
    if form and isinstance(form.get("auto_extend"), dict):
        return {**AUTO_EXTEND_DEFAULTS, **form["auto_extend"]}
    if _auto_extend_cfg is None:
        try:
            _auto_extend_cfg = dict(load_config().get("auto_extend") or {})
        except Exception:
            _auto_extend_cfg = {}
    return {**AUTO_EXTEND_DEFAULTS, **_auto_extend_cfg}


def needs_extend(text: str, limit: int, settings: dict) -> bool:
    """긴 문항인데 목표(한도)의 min_ratio 에 못 미치면 이어쓰기 대상."""
    if not settings.get("enabled"):
        return False
    if int(limit) < int(settings.get("min_limit", 500)):
        return False                      # Q1·Q10 같은 100자 문항은 대상이 아니다
    return len(text) < int(limit) * float(settings.get("min_ratio", 0.7))


def postprocess(text: str, limit: int) -> str:
    lines = []
    for line in text.strip().splitlines():
        line = _MD_LINE_PREFIX.sub("", line)
        line = line.replace("**", "")
        lines.append(line)
    text = "\n".join(lines).strip()
    if limit <= 100:
        text = " ".join(part.strip() for part in text.splitlines() if part.strip())

    if len(text) > limit and limit <= SHORT_LIMIT:
        # 짧은 문항(Q1·Q10)은 여기서 자르지 않는다 — 한 문장뿐이라 자르면 말이 끊긴다.
        # generate_one 이 '줄여 쓰기'로 다시 생성하고, 마지막에야 문장 끝에서 자른다 (작업 21).
        return text

    if len(text) > limit:
        cut = text[:limit]
        end = max(
            (cut.rfind(p) + len(p)) for p in _SENTENCE_ENDS if cut.rfind(p) != -1
        ) if any(cut.rfind(p) != -1 for p in _SENTENCE_ENDS) else 0
        if end > limit * 0.5:
            text = cut[:end]                       # 문장 끝에서 자르는 것은 안전하다
        else:
            # 긴 문항: 문장 끝을 못 찾았으면 마지막 어절 경계에서 자르고 '…' 을 붙인다.
            # '…' 한 글자도 한도 안에 들어가야 한다.
            room = text[:limit - 1]
            space = room.rfind(" ")
            text = (room[:space].rstrip() if space > limit * 0.5 else room).rstrip(" ,·") + "…"
    return text


async def generate_one(client: LLMClient, form: dict) -> dict:
    # 사업계획 골자 — 아이디어당 1회 만들어 문항 9개가 공유한다. 캐시에 있으면 모델 호출 0회.
    outline_used = isinstance(form.get("outline"), dict) and any(form["outline"].values())
    settings = outline_settings(form)
    if settings.get("enabled") and not outline_used:
        got = await outline_mod.get_outline(client, form, ttl=int(settings.get("cache_ttl_seconds", 604800)))
        if got and any(got.values()):
            form = {**form, "outline": got}
            outline_used = True

    system, user, meta = assemble.build_prompts(form)
    res = await client.complete(system, user, model=form.get("model"))
    text = postprocess(res.text, meta["limit"])

    retried = False
    if meta.get("structure_offset") is not None and _DEFINITION_ENDING.search(text):
        # 짜임을 다음 것으로 바꿔 딱 한 번 다시 생성. 그래도 정의형이면 원래 것을 쓴다(무한 재시도 금지).
        retry_form = {**form, "structure_offset": int(meta["structure_offset"]) + 1}
        system2, user2, _ = assemble.build_prompts(retry_form)
        res2 = await client.complete(system2, user2, model=form.get("model"))
        text2 = postprocess(res2.text, meta["limit"])
        retried = True
        if not _DEFINITION_ENDING.search(text2):
            text, res = text2, res2
    shortened = 0
    if meta["limit"] <= SHORT_LIMIT and len(text) > meta["limit"]:
        text, shortened = await _shorten(client, form, system, user, text, meta)

    auto_extended = False
    settings = auto_extend_settings(form)
    if needs_extend(text, meta["limit"], settings):
        # 이어쓰기 1회. 실패하거나 더 짧아지면 원래 글을 그대로 둔다 — 늘리려다 망가뜨리지 않는다.
        try:
            out = await extend_one(client, form, text)
            if len(out.get("text") or "") > len(text):
                text = out["text"]
                auto_extended = True
        except Exception:
            pass
    return {
        "question_id": meta["id"],
        "style": form["style"],
        "text": text,
        "length": len(text),
        "limit": meta["limit"],
        "model": res.model,
        "retried": retried,          # 정의형 맺음이라 다시 생성했는지 (진단용, 필드 추가)
        "auto_extended": auto_extended,   # 짧아서 서버가 이어쓰기를 한 번 돌렸는지 (진단용, 필드 추가)
        "shortened": shortened,           # 한도 초과로 줄여 다시 쓴 횟수 (진단용, 필드 추가)
        "outline_used": outline_used,     # 공유 골자를 넣고 썼는지 (진단용, 필드 추가)
    }


async def _shorten(client: LLMClient, form: dict, system: str, user: str, text: str, meta: dict) -> tuple[str, int]:
    """짧은 문항이 한도를 넘겼을 때 같은 뜻으로 줄여 다시 쓰게 한다 (작업 21).

    자르지 않는 이유: Q1·Q10 은 한 문장이라 한도에서 자르면 말이 끊긴 채로 사용자에게 간다.
    최대 SHORTEN_TRIES 회 시도하고, 그래도 넘치면 **문장 끝에서만** 자른다(어절 자름·'…' 없음)."""
    limit = int(meta["limit"])
    target = max(limit - SHORTEN_MARGIN, int(limit * 0.6))
    best = text
    for attempt in range(SHORTEN_TRIES):
        instruction = assemble.render("shorten.md", limit=limit, current=len(best), target=target)
        try:
            res = await client.complete(
                system + "\n\n" + instruction,
                user + "\n\n" + instruction + "\n" + best,
                model=form.get("model"))
        except Exception:
            break
        candidate = postprocess(res.text, limit)
        if not candidate:
            continue
        if len(candidate) < len(best):
            best = candidate
        if len(best) <= limit:
            return best, attempt + 1
    return _cut_at_sentence_end(best, limit), SHORTEN_TRIES


def _cut_at_sentence_end(text: str, limit: int) -> str:
    """마지막 수단 — 한도 안에서 문장이 끝나는 지점까지만. 그런 지점이 없으면 원문을 그대로 둔다
    (낱말 중간에서 끊긴 글보다 조금 넘치는 글이 낫다. 프론트가 글자 수를 표시한다)."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    end = max((cut.rfind(p) + len(p)) for p in _SENTENCE_ENDS if cut.rfind(p) != -1)         if any(cut.rfind(p) != -1 for p in _SENTENCE_ENDS) else 0
    return cut[:end].strip() if end > 0 else text


async def extend_one(client: LLMClient, form: dict, current: str) -> dict:
    """기존 글 뒤에 이어질 내용만 생성해 붙여서 반환."""
    system, user, meta = assemble.build_extend_prompts(form, current)
    if meta["room"] < 100:
        return {"question_id": meta["id"], "style": form["style"], "text": current,
                "added": "", "length": len(current), "limit": meta["limit"]}
    res = await client.complete(system, user, model=form.get("model"))
    added = postprocess(res.text, meta["room"])
    text = (current.rstrip() + "\n\n" + added).strip()
    text = postprocess(text, meta["limit"])
    return {"question_id": meta["id"], "style": form["style"], "text": text,
            "added": added, "length": len(text), "limit": meta["limit"], "model": res.model}
