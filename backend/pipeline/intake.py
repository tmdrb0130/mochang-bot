"""아이디어 인테이크: 입력을 읽고 슬롯(고객·문제·대안·수익·첫 단계·근거·역량·멘토링)별로 확인/부족을 판정하고,
부족한 슬롯마다 '보기 고르기' 카드를 만든다. LLM 호출 1회, JSON.

- ready=True 면 핵심 정보(고객·문제·수익)가 충분하다는 뜻. 프론트는 카드가 있으면 보여주되 건너뛸 수 있게 한다.
- regenerate_cards(): 지원자가 "보기가 안 맞다"고 한 슬롯만 다시 생성 (LLM 1회). 이미 보여준 보기는 금지 목록으로 넣는다.
"""
import json
import re

_FOREIGN_CJK = re.compile(r"[一-鿿぀-ヿ]")  # 한자(CJK 통합) · 히라가나 · 가타카나

from ..llm.client import LLMClient
from . import assemble

SLOT_ORDER = ["customer", "problem", "alternative", "solution", "revenue", "first_step", "evidence", "capability", "mentoring"]
SLOT_LABELS = {
    "customer": "대상 고객", "problem": "해결할 불편", "alternative": "지금의 대안", "solution": "제품·서비스 형태",
    "revenue": "수익 방식", "first_step": "첫 6개월 행동", "evidence": "직접 확인한 근거", "capability": "관련 경험·역량",
    "mentoring": "책임멘토에게 받고 싶은 도움",
}
# 이 슬롯들이 모두 known/weak 이상이면 카드 없이 바로 생성해도 된다고 본다
CORE_SLOTS = {"customer", "problem", "revenue"}
# mentoring 은 Q4-2 전용 선택 항목 — 비어 있어도 '정보 부족'으로 세지 않는다 (ready 계산에서 제외)
OPTIONAL_SLOTS = {"mentoring"}
# 항상 카드를 내는 슬롯 (known 이 아닌 한). MAX_CARDS 에 밀려도 자리를 보장한다.
ALWAYS_CARD_SLOTS = ["mentoring"]
CARD_PRIORITY = ["problem", "customer", "revenue", "alternative", "evidence", "capability", "mentoring", "first_step", "solution"]
MAX_CARDS = 7
VALID_TYPES = {"single", "multi", "number"}
MAX_OPTIONS = {"single": 4, "number": 4, "multi": 8}   # multi(역량·멘토링 체크리스트)는 보기를 더 허용


def _parse_json_object(raw: str) -> dict:
    cleaned = raw.replace("```json", "").replace("```", "")
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"JSON 객체를 찾지 못함: {raw[:200]!r}")
    return json.loads(cleaned[start:end + 1])


def _normalize_card(c: dict, banned: set[str] | None = None) -> dict | None:
    """카드 한 장 정리. 보기가 2개 미만이면 None. banned 에 있는 보기(이미 보여준 것)는 버린다."""
    if not isinstance(c, dict) or c.get("slot") not in SLOT_ORDER:
        return None
    ctype = c.get("type") if c.get("type") in VALID_TYPES else "single"
    options = [
        {"label": str(o.get("label", "")).strip(), "hint": str(o.get("hint", "")).strip()}
        for o in c.get("options", []) if isinstance(o, dict) and str(o.get("label", "")).strip()
    ]
    # 무료 모델이 간헐적으로 한자·가나를 섞음 → 그런 보기는 버린다
    options = [o for o in options if not _FOREIGN_CJK.search(o["label"] + o["hint"])]
    if banned:
        options = [o for o in options if o["label"] not in banned]
    seen: set[str] = set()
    options = [o for o in options if not (o["label"] in seen or seen.add(o["label"]))][:MAX_OPTIONS[ctype]]
    if len(options) < 2:
        return None
    return {
        "slot": c["slot"],
        "label": SLOT_LABELS[c["slot"]],
        "type": ctype,
        "question": str(c.get("question") or f"{SLOT_LABELS[c['slot']]}에 가장 가까운 것을 골라주세요.").strip(),
        "why": str(c.get("why") or "").strip(),
        "question_ids": [q for q in c.get("question_ids", []) if isinstance(q, str)],
        "options": options,
    }


def _limit_cards(cards: list[dict]) -> list[dict]:
    """우선순위로 정렬하고 MAX_CARDS 로 자른다. ALWAYS_CARD_SLOTS 는 잘려도 자리를 보장."""
    cards = sorted(cards, key=lambda c: CARD_PRIORITY.index(c["slot"]))
    kept = cards[:MAX_CARDS]
    for slot in ALWAYS_CARD_SLOTS:
        must = next((c for c in cards if c["slot"] == slot), None)
        if must and must not in kept:
            kept = kept[:-1] + [must]
    return kept


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
        card = _normalize_card(c)
        if card is None or status_of.get(card["slot"]) == "known":
            continue  # 이미 확인된 슬롯엔 카드 안 냄
        if any(k["slot"] == card["slot"] for k in cards):
            continue  # 슬롯당 한 장
        cards.append(card)
    cards = _limit_cards(cards)

    missing = [s["id"] for s in slots if s["status"] == "missing"]
    weak = [s["id"] for s in slots if s["status"] == "weak"]
    core_missing = [s for s in missing if s in CORE_SLOTS]
    # 충분: 핵심 슬롯이 비지 않았고, (선택 슬롯을 뺀) missing 이 1개 이하
    ready = len(core_missing) == 0 and len([m for m in missing if m not in OPTIONAL_SLOTS]) <= 1
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


def _regenerate_prompt(slots: list[str], seen: dict[str, list[str]], note: str) -> str:
    """intake.md + intake_regenerate.md(다시 만들 슬롯 · 금지 보기 · 지원자 메모)."""
    seen_lines = []
    for slot in slots:
        labels = [str(x).strip() for x in (seen.get(slot) or []) if str(x).strip()]
        seen_lines.append(f"- {slot} ({SLOT_LABELS[slot]}): " + (" / ".join(labels) if labels else "(없음)"))
    extra = assemble.render("intake_regenerate.md",
                            slots=", ".join(f"{s} ({SLOT_LABELS[s]})" for s in slots),
                            seen="\n".join(seen_lines),
                            note=note.strip() or "(없음)")
    return assemble.read_prompt("intake.md") + "\n\n" + extra


async def regenerate_cards(client: LLMClient, form: dict, slots: list[str],
                           seen: dict[str, list[str]] | None = None, note: str = "") -> dict:
    """지정한 슬롯의 카드만 다시 만든다 (LLM 1회). → {cards, slots(요청한 것), model, error?}

    seen: {slot: [이미 보여준 보기 label...]} — 결과에서 같은 보기는 걸러낸다(모델이 무시해도 코드가 막음).
    form.answers 에 다른 카드의 답이 있으면 [지원자가 확인한 사실]로 들어가 그와 어울리는 보기를 낸다.
    """
    slots = [s for s in slots if s in SLOT_ORDER]
    if not slots:
        raise ValueError("다시 만들 슬롯이 없습니다. slots 에 " + ", ".join(SLOT_ORDER) + " 중 하나 이상을 넣어주세요.")
    seen = seen or {}
    system = _regenerate_prompt(slots, seen, note)
    user = assemble.build_context(form)
    res = await client.complete(system, user, model=form.get("model"))
    try:
        parsed = _parse_json_object(res.text)
    except (ValueError, json.JSONDecodeError):
        return {"cards": [], "slots": slots, "model": res.model, "error": "카드를 다시 만들지 못했습니다. 한 번 더 눌러보세요."}
    cards = []
    for c in parsed.get("cards", []):
        if not isinstance(c, dict):
            continue
        banned = {str(x).strip() for x in seen.get(c.get("slot"), []) or []}
        card = _normalize_card(c, banned=banned)
        if card is None or card["slot"] not in slots or any(k["slot"] == card["slot"] for k in cards):
            continue
        cards.append(card)
    cards.sort(key=lambda c: CARD_PRIORITY.index(c["slot"]))
    out = {"cards": cards, "slots": slots, "model": res.model}
    if not cards:
        out["error"] = "새 보기를 만들지 못했습니다(이전 보기와 겹침). 메모를 적어서 다시 시도해 보세요."
    return out
