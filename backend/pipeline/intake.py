"""아이디어 인테이크: 입력을 읽고 슬롯(고객·문제·대안·수익·첫 단계·근거·역량)별로 확인/부족을 판정하고,
부족한 슬롯마다 '보기 고르기' 카드를 만든다. LLM 호출 1회, JSON.

충분하면(ready=True) 프론트는 카드 단계를 건너뛰고 바로 생성으로 간다.
"""
import json
import re

_FOREIGN_CJK = re.compile(r"[一-鿿぀-ヿ]")  # 한자(CJK 통합) · 히라가나 · 가타카나

from ..llm.client import LLMClient
from . import assemble

SLOT_ORDER = ["customer", "problem", "alternative", "solution", "revenue", "first_step", "evidence", "capability"]
SLOT_LABELS = {
    "customer": "대상 고객", "problem": "해결할 불편", "alternative": "지금의 대안", "solution": "제품·서비스 형태",
    "revenue": "수익 방식", "first_step": "첫 6개월 행동", "evidence": "직접 확인한 근거", "capability": "관련 경험·역량",
}
# 이 슬롯들이 모두 known/weak 이상이면 카드 없이 바로 생성해도 된다고 본다
CORE_SLOTS = {"customer", "problem", "revenue"}
CARD_PRIORITY = ["problem", "customer", "revenue", "alternative", "evidence", "capability", "first_step", "solution"]
MAX_CARDS = 6
VALID_TYPES = {"single", "multi", "number"}


def _parse_json_object(raw: str) -> dict:
    cleaned = raw.replace("```json", "").replace("```", "")
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"JSON 객체를 찾지 못함: {raw[:200]!r}")
    return json.loads(cleaned[start:end + 1])


def _normalize(parsed: dict) -> dict:
    """모델 출력의 빈틈을 메우고 규칙(카드 수·타입·정렬)을 강제."""
    by_id = {s.get("id"): s for s in parsed.get("slots", []) if isinstance(s, dict)}
    slots = []
    for sid in SLOT_ORDER:
        s = by_id.get(sid, {})
        status = s.get("status") if s.get("status") in ("known", "weak", "missing") else "missing"
        slots.append({"id": sid, "label": SLOT_LABELS[sid], "status": status,
                      "known": (s.get("known") or None) if status != "missing" else None})

    status_of = {s["id"]: s["status"] for s in slots}
    cards = []
    for c in parsed.get("cards", []):
        if not isinstance(c, dict) or c.get("slot") not in SLOT_ORDER:
            continue
        if status_of.get(c["slot"]) == "known":
            continue  # 이미 확인된 슬롯엔 카드 안 냄
        options = [
            {"label": str(o.get("label", "")).strip(), "hint": str(o.get("hint", "")).strip()}
            for o in c.get("options", []) if isinstance(o, dict) and str(o.get("label", "")).strip()
        ]
        # 무료 모델이 간헐적으로 한자·가나를 섞음 → 그런 보기는 버린다
        options = [o for o in options if not _FOREIGN_CJK.search(o["label"] + o["hint"])][:4]
        if len(options) < 2:
            continue
        cards.append({
            "slot": c["slot"],
            "label": SLOT_LABELS[c["slot"]],
            "type": c.get("type") if c.get("type") in VALID_TYPES else "single",
            "question": str(c.get("question") or f"{SLOT_LABELS[c['slot']]}에 가장 가까운 것을 골라주세요.").strip(),
            "why": str(c.get("why") or "").strip(),
            "question_ids": [q for q in c.get("question_ids", []) if isinstance(q, str)],
            "options": options,
        })
    cards.sort(key=lambda c: CARD_PRIORITY.index(c["slot"]))
    cards = cards[:MAX_CARDS]

    missing = [s["id"] for s in slots if s["status"] == "missing"]
    weak = [s["id"] for s in slots if s["status"] == "weak"]
    core_missing = [s for s in missing if s in CORE_SLOTS]
    # 충분: 핵심 슬롯이 비지 않았고, 전체 missing 이 1개 이하
    ready = len(core_missing) == 0 and len(missing) <= 1
    return {
        "summary": str(parsed.get("summary") or "").strip(),
        "slots": slots,
        "cards": cards,
        "missing": missing,
        "weak": weak,
        "ready": ready,
    }


async def run_intake(client: LLMClient, form: dict) -> dict:
    system = assemble.read_prompt("intake.md")
    user = assemble.build_context(form)
    res = await client.complete(system, user, model=form.get("model"))
    try:
        parsed = _parse_json_object(res.text)
    except (ValueError, json.JSONDecodeError):
        # 파싱 실패 → 카드 없이 진행 가능하게 (생성을 막지 않는다)
        return {"summary": "", "slots": [{"id": s, "label": SLOT_LABELS[s], "status": "weak", "known": None} for s in SLOT_ORDER],
                "cards": [], "missing": [], "weak": SLOT_ORDER[:], "ready": True, "model": res.model,
                "error": "인테이크 결과를 해석하지 못해 카드를 생략했습니다."}
    out = _normalize(parsed)
    out["model"] = res.model
    return out
