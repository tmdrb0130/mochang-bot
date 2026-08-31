"""문항 생성 + 후처리 (글자 수 절단, 마크다운 제거)."""
import re

from ..llm.client import LLMClient
from . import assemble

_MD_LINE_PREFIX = re.compile(r"^\s*(#{1,6}|\*|-)\s+")

_SENTENCE_ENDS = ("다.", "요.", ".", "!", "?")


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
        text = cut[:end] if end > limit * 0.5 else cut
    return text


async def generate_one(client: LLMClient, form: dict) -> dict:
    system, user, meta = assemble.build_prompts(form)
    res = await client.complete(system, user, model=form.get("model"))
    text = postprocess(res.text, meta["limit"])
    return {
        "question_id": meta["id"],
        "style": form["style"],
        "text": text,
        "length": len(text),
        "limit": meta["limit"],
        "model": res.model,
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
