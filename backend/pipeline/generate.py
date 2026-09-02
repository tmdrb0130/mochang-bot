"""문항 생성 + 후처리 (글자 수 절단, 마크다운 제거)."""
import re

from ..llm.client import LLMClient
from . import assemble

_MD_LINE_PREFIX = re.compile(r"^\s*(#{1,6}|\*|-)\s+")

_SENTENCE_ENDS = ("다.", "요.", ".", "!", "?")

# 이렇게 끝나면 "정의형 한 줄" 로 떨어진 것이다 — 짜임을 바꿔 한 번만 다시 생성한다 (작업 15 후속).
# 프롬프트로 금지해도 라마가 우회하므로(실호출 25건) 코드에서 걸러 낸다.
_DEFINITION_ENDING = re.compile("(제공(합니다|하는|하여)|앱입니다|서비스입니다|플랫폼입니다)[ .]*$")


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
    return {
        "question_id": meta["id"],
        "style": form["style"],
        "text": text,
        "length": len(text),
        "limit": meta["limit"],
        "model": res.model,
        "retried": retried,          # 정의형 맺음이라 다시 생성했는지 (진단용, 필드 추가)
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
