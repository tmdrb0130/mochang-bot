"""문항 생성 + 후처리 (글자 수 절단, 마크다운 제거)."""
import re

from ..llm.client import LLMClient, load_config
from . import assemble

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

    if len(text) > limit:
        cut = text[:limit]
        end = max(
            (cut.rfind(p) + len(p)) for p in _SENTENCE_ENDS if cut.rfind(p) != -1
        ) if any(cut.rfind(p) != -1 for p in _SENTENCE_ENDS) else 0
        if end > limit * 0.5:
            text = cut[:end]
        else:
            # 문장 끝을 못 찾았으면 마지막 어절 경계에서 자른다 — 낱말 중간에서 끊기면 글이 망가진 티가 난다.
            space = cut.rfind(" ")
            text = (cut[:space].rstrip() if space > limit * 0.5 else cut).rstrip(" ,·") + "…"
    return text


async def generate_one(client: LLMClient, form: dict) -> dict:
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
    }


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
