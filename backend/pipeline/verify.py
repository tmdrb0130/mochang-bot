"""생성문 검증: 문항 md 의 [필수 요소] 를 체크리스트로 삼아 모델이 포함 여부를 JSON 으로 판정.

    result = await verify_text(client, form, "q2", text)
    result["missing"]  → 누락된 필수 요소 목록 (UI 에 "누락: 수익 주체" 표시)

모델 호출 1회. 판정의 evidence 는 글의 원문 문장이어야 하며, 원문에 없으면 present 를 false 로 뒤집는다(프로그램 검증).
"""
from __future__ import annotations

import json
import re

from ..llm.client import LLMClient
from . import assemble

_SECTION = re.compile(r"^\[(.+?)\]\s*$")


def parse_sections(body: str) -> dict[str, list[str]]:
    """문항 md 본문의 '[필수 요소]' 같은 섹션 → {섹션명: [불릿...]}. 괄호 안 부연 줄은 앞 불릿에 붙인다."""
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for raw in body.splitlines():
        line = raw.strip()
        if not line:
            continue
        m = _SECTION.match(line)
        if m:
            current = m.group(1).strip()
            sections.setdefault(current, [])
            continue
        if current is None:
            continue
        if line.startswith(("-", "•", "*")):
            sections[current].append(line.lstrip("-•* ").strip())
        elif sections[current]:
            sections[current][-1] += " " + line
        else:
            sections[current].append(line)
    return sections


def required_items(question_id: str) -> tuple[dict, list[str]]:
    meta, body = assemble.load_question(question_id)
    return meta, parse_sections(body).get("필수 요소", [])


def _parse_json_object(raw: str) -> dict:
    cleaned = raw.replace("```json", "").replace("```", "")
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"JSON 객체를 찾지 못함: {raw[:160]!r}")
    data = json.loads(cleaned[start:end + 1])
    if not isinstance(data, dict):
        raise ValueError("객체가 아님")
    return data


def _norm(s: str) -> str:
    return re.sub(r"[\s\"'“”‘’.,!?]", "", s or "")


def _check_evidence(items: list[dict], text: str, requirements: list[str]) -> list[dict]:
    """모델 판정을 프로그램으로 다시 조인다: evidence 가 글에 없으면 present=false. 요구 항목 순서·개수도 맞춘다."""
    norm_text = _norm(text)
    by_req = {}
    for it in items:
        if isinstance(it, dict) and it.get("requirement"):
            by_req[str(it["requirement"]).strip()] = it
    out = []
    for req in requirements:
        it = by_req.get(req) or next((v for k, v in by_req.items() if k[:12] == req[:12]), {})
        present = bool(it.get("present"))
        evidence = it.get("evidence")
        if present:
            ev = _norm(str(evidence or ""))
            if len(ev) < 6 or ev not in norm_text:
                present, evidence = False, None
        out.append({"requirement": req, "present": present, "evidence": evidence if present else None})
    return out


def local_format_issues(text: str, limit: int) -> list[str]:
    issues = []
    if len(text) > limit:
        issues.append(f"글자 수 초과 ({len(text)}/{limit})")
    if re.search(r"^\s*(#{1,6}|\*|-)\s+", text, re.M) or "**" in text:
        issues.append("마크다운 기호 사용")
    if re.search(r"[\U0001F300-\U0001FAFF☀-➿]", text):
        issues.append("이모지 사용")
    return issues


async def verify_text(client: LLMClient, form: dict, question_id: str, text: str) -> dict:
    meta, requirements = required_items(question_id)
    system = assemble.read_prompt("verify.md")
    user = "\n\n".join([
        f"[문항] {meta['label']}. {meta['title']} (제한 {meta['limit']}자)",
        "[필수 요소]\n" + "\n".join(f"- {r}" for r in requirements),
        "[지원자 입력·확인된 사실·웹 참고자료]\n" + assemble.build_context(form),
        f"[생성된 글]\n{text}",
    ])
    res = await client.complete(system, user, model=form.get("model"))
    try:
        parsed = _parse_json_object(res.text)
    except Exception:
        parsed = {}
    items = _check_evidence(parsed.get("items") or [], text, requirements)
    unsupported = [str(c) for c in (parsed.get("unsupported_claims") or []) if _norm(str(c)) and _norm(str(c)) in _norm(text)]
    issues = list(dict.fromkeys(local_format_issues(text, int(meta["limit"])) + [str(i) for i in (parsed.get("format_issues") or [])]))
    missing = [it["requirement"] for it in items if not it["present"]]
    return {
        "question_id": question_id,
        "items": items,
        "missing": missing,
        "unsupported_claims": unsupported,
        "format_issues": issues,
        "overall": str(parsed.get("overall") or "").strip(),
        "score": round(100 * (len(items) - len(missing)) / len(items)) if items else None,
        "model": res.model,
        "parse_ok": bool(parsed),
    }
