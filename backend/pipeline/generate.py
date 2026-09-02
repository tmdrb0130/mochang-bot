"""문항 생성 + 후처리 (글자 수 절단, 마크다운 제거)."""
import re

from ..llm.client import LLMClient, load_config
from . import assemble
from . import outline as outline_mod
from . import polish as polish_mod
from . import postprocess as refine_mod

_MD_LINE_PREFIX = re.compile(r"^\s*(#{1,6}|\*|-)\s+")

_SENTENCE_ENDS = ("다.", "요.", ".", "!", "?")

# 이렇게 끝나면 알맹이가 없는 한 줄이다 — 짜임을 바꿔 한 번만 다시 생성한다 (작업 15 후속 → 작업 25 로 조정).
# 작업 25: **명사 맺음('~하는 서비스')은 이제 허용**한다. 사용자가 싫어한 것은 정의형 자체가 아니라
# "…를 제공하는 앱입니다" 같은 빈 틀과 "자신감을 얻습니다" 같은 추상 가치어 맺음이었다.
# 프롬프트로 금지해도 라마가 우회하므로(실호출 25건) 코드에서 걸러 낸다.
_DEFINITION_ENDING = re.compile("(제공(합니다|하여|하는 서비스입니다|하는 앱입니다)|자신감을 (얻습니다|줍니다|드립니다)|효율화(합니다|됩니다)|불편을 해소(합니다|해 줍니다)|만족도를 높입니다|편리해집니다|편리하게 (합니다|해 줍니다))[ .]*$")


# ── 짧게 끝난 긴 문항 자동 이어쓰기 (작업 18) ──
# 라마가 2,000자 문항을 700~900자에서 끝내 버리는 일이 잦다(작업 16 검증에서 Q2 698자).
# 사용자가 '이어쓰기'를 손으로 누르면 되는 기능이지만, 목표의 70%도 못 채운 글은 사실상 미완성이라
# 서버가 한 번만 대신 눌러 준다. 모델 호출이 문항당 최대 1회 늘어나므로 config 로 끌 수 있다.
AUTO_EXTEND_DEFAULTS = {"enabled": True, "min_ratio": 0.7, "min_limit": 500}
EXTEND_TRIES = 2           # 이어쓰기 최대 횟수 (중복을 지우면 다시 짧아져 한 번으로는 하한을 못 맞춘다)

# ── 짧은 문항 한도 초과 → 압축 재생성 (작업 21) ──
# Q1·Q10 은 한 문장이라 한도에서 자르면 말이 끊긴 채 사용자에게 간다("…식재료" 처럼).
# 자르는 대신 "같은 뜻으로 줄여 다시" 를 최대 SHORTEN_TRIES 회 시킨다.
# ── 생성 후처리 (작업 22) ── 지어낸 근거·이물·1인칭·중복 통계를 코드가 잡는다.
POLISH_DEFAULTS = {"enabled": True}
_polish_cfg: dict | None = None


def polish_settings(form: dict | None = None) -> dict:
    global _polish_cfg
    if form and isinstance(form.get("polish_settings"), dict):
        return {**POLISH_DEFAULTS, **form["polish_settings"]}
    if _polish_cfg is None:
        try:
            _polish_cfg = dict((load_config().get("generate") or {}).get("polish") or {})
        except Exception:
            _polish_cfg = {}
    return {**POLISH_DEFAULTS, **_polish_cfg}


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

# ── 세션2 refine() 연결 (작업 22·27 마무리) ──
# postprocess.refine() 은 모델을 부르지 않는 순수 함수라 여기서 신호만 받아 행동한다:
#   needs_compress → shorten.md 로 1회 압축, 그래도 넘으면 문단 끝에서만 자름 (작업 27, 긴 문항)
#   needs_extend   → 이어쓰기 1회 후 refine 재적용(⑦ 중복 제거), 최대 REGENERATE_TRIES 회
#   *_flagged      → 짧은 문항은 지울 수 없으니 짜임을 바꿔 1회 재생성, 그래도 남으면 원문 유지
#   foreign        → polish 가 이미 1회 고쳐 쓰게 했으므로 남은 문장은 지운다 (글이 비면 둔다)
# 재생성(압축·이어쓰기·재생성) 합계는 문항당 REGENERATE_TRIES 회를 넘지 않는다.
REFINE_DEFAULTS = {"enabled": True}
REGENERATE_TRIES = 2
_refine_cfg: dict | None = None


def refine_settings(form: dict | None = None) -> dict:
    global _refine_cfg
    if form and isinstance(form.get("refine_settings"), dict):
        return {**REFINE_DEFAULTS, **form["refine_settings"]}
    if _refine_cfg is None:
        try:
            _refine_cfg = dict((load_config().get("generate") or {}).get("refine") or {})
        except Exception:
            _refine_cfg = {}
    return {**REFINE_DEFAULTS, **_refine_cfg}


def refine_ctx(form: dict, meta: dict, current: str | None = None) -> dict:
    """refine() 에 넘길 문맥 — 이 문항이 실제로 받은 참고자료 몫만 넣는다(지어냄 판정 기준이 되므로)."""
    qid = str(meta.get("id") or form.get("question_id") or "")
    outline = form.get("outline") if isinstance(form.get("outline"), dict) else None
    refs = form.get("references")
    if refs and not isinstance(refs, str):
        refs = assemble.references_for(refs, qid, outline=outline)
    floor = int(meta.get("min") or 0)
    ctx = {
        "question_id": qid,
        "limit": int(meta["limit"]),
        "target_range": (floor, int(meta["limit"])) if floor else None,
        "idea": form.get("idea") or "",
        "outline": outline,
        "answers": form.get("answers") or [],
        "references": refs or [],
        "team": form.get("team") or "",
        "capability": form.get("capability") or "",
        "current_item": form.get("current_item") or "",
    }
    if isinstance(form.get("other_texts"), dict):
        ctx["other_texts"] = form["other_texts"]       # 확정된 다른 문항 본문만 (프론트가 주면)
    if current:
        ctx["current"] = current
    return ctx


def _has_flags(report: dict) -> bool:
    return bool(report.get("foreign") or report.get("invented_flagged")
                or report.get("banned_flagged") or report.get("unit_economics_flagged"))


def _flag_count(report: dict) -> int:
    return sum(len(report.get(k) or []) for k in ("foreign", "invented_flagged", "banned_flagged", "unit_economics_flagged"))


def _cut_at_paragraph_end(text: str, limit: int) -> str:
    """긴 문항 마지막 수단(작업 27) — 한도 안에서 끝나는 마지막 문단까지만. 문단 경계가 너무 앞이면 문장 끝에서."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    boundary = cut.rfind("\n\n")
    if boundary > limit * 0.5:
        return cut[:boundary].strip()
    shorter = _cut_at_sentence_end(text, limit)
    return shorter if len(shorter) <= limit else postprocess(text, limit)


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


def postprocess(text: str, limit: int, cut: bool = True) -> str:
    """마크다운 제거 + 한도 절단. cut=False 면 자르지 않는다 (refine 연결 경로가 압축을 먼저 시도한다)."""
    lines = []
    for line in text.strip().splitlines():
        line = _MD_LINE_PREFIX.sub("", line)
        line = line.replace("**", "")
        lines.append(line)
    text = "\n".join(lines).strip()
    if limit <= 100:
        text = " ".join(part.strip() for part in text.splitlines() if part.strip())

    if not cut or (len(text) > limit and limit <= SHORT_LIMIT):
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

    refining = bool(refine_settings(form).get("enabled"))
    system, user, meta = assemble.build_prompts(form)
    res = await client.complete(system, user, model=form.get("model"))
    # refine 연결 경로에서는 긴 문항을 여기서 자르지 않는다 — 아래에서 압축을 먼저 시도한다 (작업 27).
    text = postprocess(res.text, meta["limit"], cut=not refining)

    retried = False
    if meta.get("structure_offset") is not None and _DEFINITION_ENDING.search(text):
        # 짜임을 다음 것으로 바꿔 딱 한 번 다시 생성. 그래도 정의형이면 원래 것을 쓴다(무한 재시도 금지).
        retry_form = {**form, "structure_offset": int(meta["structure_offset"]) + 1}
        system2, user2, _ = assemble.build_prompts(retry_form)
        res2 = await client.complete(system2, user2, model=form.get("model"))
        text2 = postprocess(res2.text, meta["limit"], cut=not refining)
        retried = True
        if not _DEFINITION_ENDING.search(text2):
            text, res = text2, res2
    shortened = 0
    if meta["limit"] <= SHORT_LIMIT and len(text) > meta["limit"]:
        text, shortened = await _shorten(client, form, system, user, text, meta)
    elif meta["limit"] <= SHORT_LIMIT and len(text) < int(meta.get("min") or 0):
        # 짧은 문항이 하한에 한참 못 미치면 한 번만 늘려 쓴다 (작업 25 — Q10 이 22자로 나온 건이 있었다)
        text = await _lengthen(client, form, system, user, text, meta)

    # 후처리를 먼저 하고(문장이 지워질 수 있다) 그다음에 분량을 채운다 — 순서가 반대면 채운 뒤 지워져 하한을 밑돈다.
    polished = {}
    if polish_settings(form).get("enabled") and text:
        text, polished = await polish_mod.polish_text(client, text, form)
        text = postprocess(text, meta["limit"], cut=not refining)

    # ── refine() (세션2, 모델 호출 0) + 신호에 따른 재생성 (최대 REGENERATE_TRIES 회) ──
    refined: dict = {}
    regenerated = 0
    if refining and text:
        ctx = refine_ctx(form, meta)
        text, refined = refine_mod.refine(text, ctx)
        if meta["limit"] <= SHORT_LIMIT and _has_flags(refined) and regenerated < REGENERATE_TRIES:
            # 짧은 문항은 한 문장이 곧 답이라 지울 수 없다 — 짜임을 바꿔 한 번 다시 쓰고 흠이 줄었을 때만 받는다.
            offset = meta.get("structure_offset")
            retry_form = {**form, "structure_offset": int(offset) + 1} if offset is not None else form
            system3, user3, _ = assemble.build_prompts(retry_form)
            regenerated += 1
            try:
                res3 = await client.complete(system3, user3, model=form.get("model"))
                text3 = postprocess(res3.text, meta["limit"])
                if len(text3) > meta["limit"]:
                    text3, _ = await _shorten(client, form, system3, user3, text3, meta)
                text3, report3 = refine_mod.refine(text3, ctx)
                if text3 and _flag_count(report3) < _flag_count(refined):
                    text, refined = text3, report3
            except Exception:
                pass
        if refined.get("foreign"):
            # polish 가 이미 1회 고쳐 쓰게 했다 — 그래도 남은 이물 문장은 지운다. 글이 통째로 비면 그대로 둔다.
            dropped_text, n = polish_mod.drop_foreign_sentences(text)
            if dropped_text.strip():
                text, refined["foreign_dropped"] = dropped_text, n
        if meta["limit"] <= SHORT_LIMIT and refined.get("needs_extend") and regenerated < REGENERATE_TRIES:
            # 짧은 문항 하한 미달 — 위의 _lengthen 1회로도 못 채웠으면 남은 예산으로 한 번 더 (실측: Q1 이 43·52자로 남았다)
            regenerated += 1
            longer = await _lengthen(client, form, system, user, text, meta)
            if longer != text:
                text, refined = refine_mod.refine(longer, ctx)
                refined["lengthened_again"] = True
        if refined.get("needs_compress") and regenerated < REGENERATE_TRIES:
            # 긴 문항 한도 초과 (작업 27): 자르기 전에 shorten.md 로 1회 압축, 그래도 넘으면 문단 끝에서만 자른다.
            regenerated += 1
            text, _ = await _shorten(client, form, system, user, text, meta, tries=1, cutter=_cut_at_paragraph_end)
            refined["compressed"] = True
            text, _ = refine_mod.refine(text, ctx)
        elif len(text) > meta["limit"]:
            text = _cut_at_paragraph_end(text, meta["limit"])

    auto_extended = False
    settings = auto_extend_settings(form)
    # 이어쓰기 → 중복 제거 → 아직 짧으면 한 번 더 (최대 2회). 되풀이한 문단을 지우고 나면 다시 하한을 밑돌기 때문이다.
    # 실패하거나 더 짧아지면 원래 글을 그대로 둔다 — 늘리려다 망가뜨리지 않는다.
    budget = REGENERATE_TRIES - regenerated if refining else EXTEND_TRIES
    for _ in range(max(budget, 0)):
        below_floor = bool(refined.get("needs_extend")) and settings.get("enabled") and meta["limit"] > SHORT_LIMIT
        if not (needs_extend(text, meta["limit"], settings) or below_floor):
            break
        try:
            out = await extend_one(client, form, text)
        except Exception:
            break
        regenerated += 1
        if not (out.get("added") or "").strip():
            break                                        # ⑦ 이어쓴 글이 전부 기존 본문의 되풀이였다
        merged, duplicates = polish_mod.dedupe_sentences(out.get("text") or "")
        merged, foreign = polish_mod.drop_foreign_sentences(merged)      # 이어쓴 글은 후처리를 안 거쳤다
        if len(merged) <= len(text):
            break
        polished["foreign_after_extend"] = polished.get("foreign_after_extend", 0) + foreign
        text, auto_extended = merged, True
        polished["duplicate"] = polished.get("duplicate", 0) + duplicates
        if refining:
            extra = out.get("refined") or {}
            refined["extend_overlap"] = refined.get("extend_overlap", 0) + int(extra.get("extend_overlap") or 0)
            refined["needs_extend"] = bool(meta.get("min")) and len(text) < int(meta["min"])
    if refining:
        refined["length"] = len(text)
        refined["regenerated"] = regenerated
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
        "polished": polished,             # 후처리로 무엇을 몇 건 고쳤는지 (진단용, 필드 추가)
        "refined": refined,               # refine() 보고서 + 재생성 횟수 (진단용, 필드 추가)
    }


async def _lengthen(client: LLMClient, form: dict, system: str, user: str, text: str, meta: dict) -> str:
    """짧은 문항이 하한에 못 미치면 한 번만 늘려 쓴다. 더 나빠지면 원문을 쓴다."""
    target, limit = int(meta.get("min") or 0), int(meta["limit"])
    instruction = assemble.render("lengthen.md", current=len(text), target=target, limit=limit)
    try:
        res = await client.complete(
            system + "\n\n" + instruction,
            user + "\n\n" + instruction + "\n" + text,
            model=form.get("model"))
    except Exception:
        return text
    candidate = postprocess(res.text, limit)
    # 하한을 채우지 못했어도 더 길어졌으면 받는다 — 실호출 6건 중 4건이 두 번 늘려도 49~65자에 머물렀다.
    # 43자보다 61자가 낫고, 한도만 넘지 않으면 된다.
    return candidate if len(text) < len(candidate) <= limit else text


async def _shorten(client: LLMClient, form: dict, system: str, user: str, text: str, meta: dict,
                   tries: int = SHORTEN_TRIES, cutter=None) -> tuple[str, int]:
    """한도를 넘긴 글을 같은 뜻으로 줄여 다시 쓰게 한다 (작업 21 짧은 문항 / 작업 27 긴 문항).

    자르지 않는 이유: Q1·Q10 은 한 문장이라 한도에서 자르면 말이 끊긴 채로 사용자에게 간다.
    최대 tries 회 시도하고, 그래도 넘치면 cutter(기본: 문장 끝에서만 자름)로 마무리한다(어절 자름·'…' 없음)."""
    limit = int(meta["limit"])
    target = max(limit - SHORTEN_MARGIN, int(limit * 0.6)) if limit <= SHORT_LIMIT else int(limit * 0.9)
    best = text
    for attempt in range(tries):
        instruction = assemble.render("shorten.md", limit=limit, current=len(best), target=target)
        try:
            res = await client.complete(
                system + "\n\n" + instruction,
                user + "\n\n" + instruction + "\n" + best,
                model=form.get("model"))
        except Exception:
            break
        candidate = postprocess(res.text, limit, cut=False)
        if not candidate:
            continue
        if len(candidate) < len(best):
            best = candidate
        if len(best) <= limit:
            return best, attempt + 1
    return (cutter or _cut_at_sentence_end)(best, limit), tries


def _cut_at_sentence_end(text: str, limit: int) -> str:
    """마지막 수단 — 한도 안에서 문장이 끝나는 지점까지만. 그런 지점이 없으면 원문을 그대로 둔다
    (낱말 중간에서 끊긴 글보다 조금 넘치는 글이 낫다. 프론트가 글자 수를 표시한다)."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    end = max((cut.rfind(p) + len(p)) for p in _SENTENCE_ENDS if cut.rfind(p) != -1) \
        if any(cut.rfind(p) != -1 for p in _SENTENCE_ENDS) else 0
    return cut[:end].strip() if end > 0 else text


async def extend_one(client: LLMClient, form: dict, current: str) -> dict:
    """기존 글 뒤에 이어질 내용만 생성해 붙여서 반환.

    refine 이 켜져 있으면 이어 쓴 부분에 refine(⑦ 기존 본문 되풀이 제거 등)을 적용하고,
    되풀이만 남았으면(extend_empty) 이번 이어쓰기는 버려 기존 글을 그대로 돌려준다."""
    system, user, meta = assemble.build_extend_prompts(form, current)
    if meta["room"] < 100:
        return {"question_id": meta["id"], "style": form["style"], "text": current,
                "added": "", "length": len(current), "limit": meta["limit"]}
    res = await client.complete(system, user, model=form.get("model"))
    added = postprocess(res.text, meta["room"])
    refined: dict = {}
    if refine_settings(form).get("enabled") and added:
        added, refined = refine_mod.refine(added, refine_ctx(form, meta, current=current))
        if refined.get("extend_empty") or not added.strip():
            added = ""
    text = (current.rstrip() + "\n\n" + added).strip() if added else current
    text = postprocess(text, meta["limit"])
    return {"question_id": meta["id"], "style": form["style"], "text": text,
            "added": added, "length": len(text), "limit": meta["limit"], "model": res.model,
            "refined": refined}                   # 이어 쓴 부분의 refine 보고서 (진단용, 필드 추가)
