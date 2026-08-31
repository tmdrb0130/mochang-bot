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

    if len(text) > limit:
        cut = text[:limit]
        end = max(
            (cut.rfind(p) + len(p)) for p in _SENTENCE_ENDS if cut.rfind(p) != -1
        ) if any(cut.rfind(p) != -1 for p in _SENTENCE_ENDS) else 0
        text = cut[:end] if end > limit * 0.5 else cut
    return text


async def generate_one(client: LLMClient, form: dict) -> dict:
    system, user, meta = assemble.build_prompts(form)
    raw = await client.complete(system, user)
    text = postprocess(raw, meta["limit"])
    return {
        "question_id": meta["id"],
        "style": form["style"],
        "text": text,
        "length": len(text),
        "limit": meta["limit"],
    }


async def extend_one(client: LLMClient, form: dict, current: str) -> dict:
    """기존 글 뒤에 이어질 내용만 생성해 붙여서 반환."""
    system, user, meta = assemble.build_extend_prompts(form, current)
    if meta["room"] < 100:
        return {"question_id": meta["id"], "style": form["style"], "text": current,
                "added": "", "length": len(current), "limit": meta["limit"]}
    raw = await client.complete(system, user)
    added = postprocess(raw, meta["room"])
    text = (current.rstrip() + "\n\n" + added).strip()
    text = postprocess(text, meta["limit"])
    return {"question_id": meta["id"], "style": form["style"], "text": text,
            "added": added, "length": len(text), "limit": meta["limit"]}


def _parse_json_object(raw: str) -> dict:
    """모델이 코드펜스나 설명을 붙여도 첫 { ... } 만 뽑아 파싱."""
    import json
    cleaned = raw.replace("```json", "").replace("```", "")
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"JSON 객체를 찾지 못함: {raw[:200]!r}")
    return json.loads(cleaned[start:end + 1])


async def recommend_field(client: LLMClient, form: dict) -> dict:
    """Q6 사업 분야 추천. 선택지에 없는 답이면 field 를 비워서 UI가 직접 선택으로 넘기게 함."""
    system, user, options = assemble.build_recommend_prompts(form)
    raw = await client.complete(system, user)
    try:
        parsed = _parse_json_object(raw)
    except Exception:
        return {"field": "", "reason": "자동 추천에 실패했어요. 직접 선택해 주세요.", "raw": raw[:300]}
    field = str(parsed.get("field", "")).strip()
    if field not in options:
        match = next((o for o in options if o in field or field in o), "")
        field = match
    return {"field": field, "reason": str(parsed.get("reason", "")).strip(), "options": options}
