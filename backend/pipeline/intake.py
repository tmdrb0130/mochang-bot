"""아이디어 인테이크: 입력을 읽고 슬롯(고객·문제·대안·수익·첫 단계·근거·역량·멘토링)별로 확인/부족을 판정하고,
부족한 슬롯마다 '보기 고르기' 카드를 만든다. LLM 호출 1회, JSON.

- ready=True 면 핵심 정보(고객·문제·수익)가 충분하다는 뜻. 프론트는 카드가 있으면 보여주되 건너뛸 수 있게 한다.
- regenerate_cards(): 지원자가 "보기가 안 맞다"고 한 슬롯만 다시 생성 (LLM 1회). 이미 보여준 보기는 금지 목록으로 넣는다.
"""
import json

import yaml
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
# 항상 카드를 내는 슬롯 (known 이 아닌 한). MAX_CARDS 에 밀려도 자리를 보장하고, 모델이 빠뜨리면 기본 카드로 채운다.
# 작업 24: 실측(캠핑장)에서 evidence·capability·first_step 카드가 빠져 Q2·Q8·Q4-1 이 지어낸 근거·경력·일정으로 채워졌다.
ALWAYS_CARD_SLOTS = ["evidence", "capability", "first_step", "mentoring"]
# 영어가 든 보기("word of mouth", "prototype 개발")는 버린다 — 한국어 신청서 카드. 널리 쓰는 약어만 허용
_LATIN_WORD = re.compile(r"[A-Za-z]{2,}")
_ALLOWED_LATIN = {"ai", "mvp", "b2b", "b2c", "iot", "sns", "app", "it"}
# 화면이 자동으로 붙이는 보기 — 모델이 넣어도 버린다
_AUTO_OPTIONS = {"기타", "모르겠어요", "모름", "해당 없음", "없음"}

# 역량 축(Q4_2_QUALITY 1-1) — capability 입력에서 강점 축을 읽고, mentoring 보기에서 그 축을 뺀다
_AXIS_WORDS = {
    "tech": ("개발", "공학", "프로그래", "코딩", "소프트웨어", "엔지니어", "기술", "데이터", "제작", "설계"),
    "customer": ("리서치", "인터뷰", "고객 조사", "UX"),
    "biz": ("경영", "회계", "재무", "수익", "가격", "사업 기획", "MBA"),
    "marketing": ("마케팅", "영업", "판매", "홍보", "브랜드", "채널"),
    "legal": ("법", "행정", "세무", "인허가", "규제", "특허"),
    "ops": ("운영", "제조", "유통", "공급", "농사", "재배", "조리", "요식", "현장", "매장", "돌보", "간호", "교육"),
}
_MENTORING_AXIS_HINTS = {
    "tech": ("개발", "기술", "구현", "코딩", "설계", "제작"),
    "customer": ("고객 검증", "고객이 정말", "인터뷰", "문제 검증", "수요"),
    "biz": ("가격", "수익", "비즈니스 모델", "원가", "수익화", "사업 계획", "사업계획"),
    "marketing": ("마케팅", "홍보", "고객 확보", "채널", "첫 고객", "모을지", "시장 진입"),
    "legal": ("법", "인허가", "위생", "안전", "개인정보", "규제", "특허"),
    "ops": ("파트너", "제조", "유통", "협약", "공급", "운영"),
}


def strength_axes(capability: str) -> set[str]:
    text = str(capability or "")
    return {axis for axis, words in _AXIS_WORDS.items() if any(w in text for w in words)}


def option_axis(label: str) -> str | None:
    text = str(label or "")
    for axis, hints in _MENTORING_AXIS_HINTS.items():
        if any(h in text for h in hints):
            return axis
    return None
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
        # source: 웹 조사에서 나온 근거 표기("매체, 연도"). 근거 없이 만든 보기는 빈 문자열.
        {"label": str(o.get("label", "")).strip(), "hint": str(o.get("hint", "")).strip(),
         "source": str(o.get("source", "") or "").strip()}
        for o in c.get("options", []) if isinstance(o, dict) and str(o.get("label", "")).strip()
    ]
    # 무료 모델이 간헐적으로 한자·가나를 섞음 → 그런 보기는 버린다. 영어 낱말이 든 보기·자동 보기도 버린다(작업 24 ④)
    options = [o for o in options if not _FOREIGN_CJK.search(o["label"] + o["hint"] + o["source"])
               and not any(w.lower() not in _ALLOWED_LATIN for w in _LATIN_WORD.findall(o["label"]))
               and o["label"].strip() not in _AUTO_OPTIONS]
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


def fallback_cards() -> dict:
    """모델이 빠뜨린 필수 카드를 대신할 기본 카드 (prompts/intake_fallback.md frontmatter). 분야 중립 보기만."""
    text = assemble.read_prompt("intake_fallback.md")
    m = re.match(r"^---\s*\n(.*?)\n---", text, re.S)
    data = yaml.safe_load(m.group(1)) if m else {}
    return data if isinstance(data, dict) else {}


def _fallback_card(slot: str) -> dict | None:
    spec = fallback_cards().get(slot)
    if not isinstance(spec, dict):
        return None
    return _normalize_card({"slot": slot, **spec})


def _limit_cards(cards: list[dict]) -> list[dict]:
    """우선순위로 정렬하고 MAX_CARDS 로 자른다. ALWAYS_CARD_SLOTS 는 잘려도 자리를 보장."""
    cards = sorted(cards, key=lambda c: CARD_PRIORITY.index(c["slot"]))
    kept = cards[:MAX_CARDS]
    for slot in ALWAYS_CARD_SLOTS:
        must = next((c for c in cards if c["slot"] == slot), None)
        if must and must not in kept:
            # 필수 카드가 잘렸으면, 필수가 아닌 카드 중 우선순위가 가장 낮은 것을 빼고 넣는다
            droppable = [c for c in kept if c["slot"] not in ALWAYS_CARD_SLOTS]
            if droppable:
                kept.remove(droppable[-1])
            kept.append(must)
    return sorted(kept, key=lambda c: CARD_PRIORITY.index(c["slot"]))


def _enforce_card_rules(card: dict, capability: str = "") -> None:
    """모델이 지키지 못하는 카드 규칙을 코드로 (작업 24).

    - mentoring: 입력된 강점 축의 보기는 뺀다(개발자에게 '개발 방법'을 묻게 하지 않는다), 부족하면 기본 카드의 반대편 축 보기로 채운다
    - evidence·first_step: 기본 카드의 보기(확인 방법 3종+아직 안 함 / MVP 행동)를 빠진 만큼 채운다"""
    slot = card["slot"]
    spec = fallback_cards().get(slot)
    if not isinstance(spec, dict):
        return
    fallback_opts = [{"label": str(o.get("label", "")).strip(), "hint": str(o.get("hint", "")).strip(), "source": "",
                      "must": bool(o.get("must"))}
                     for o in spec.get("options", []) if isinstance(o, dict)]
    fallback_opts.sort(key=lambda o: not o["must"])      # must 보기를 먼저 채운다
    axes = strength_axes(capability)
    if slot == "mentoring":
        card["options"] = [o for o in card["options"] if option_axis(o["label"]) not in axes]
        fallback_opts = [o for o in fallback_opts if option_axis(o["label"]) not in axes]
    if slot in ("mentoring", "evidence", "first_step"):
        have = {o["label"] for o in card["options"]}
        limit = MAX_OPTIONS[card["type"]]
        for o in fallback_opts:
            if len(card["options"]) >= limit:
                break
            if o["label"] not in have and not any(_similar(o["label"], h) for h in have):
                card["options"].append({k: v for k, v in o.items() if k != "must"})


def _similar(a: str, b: str) -> bool:
    """짧은 보기 문구가 사실상 같은지 (앞 4글자 공백 제거 비교)."""
    ka, kb = re.sub(r"\s+", "", a)[:4], re.sub(r"\s+", "", b)[:4]
    return bool(ka) and ka == kb


def _normalize(parsed: dict, capability: str = "") -> dict:
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
    # 필수 슬롯이 known 이 아닌데 모델이 카드를 안 냈으면 기본 카드로 채운다 (작업 24 ②)
    for slot in ALWAYS_CARD_SLOTS:
        if status_of.get(slot) != "known" and not any(k["slot"] == slot for k in cards):
            card = _fallback_card(slot)
            if card:
                card["fallback"] = True
                cards.append(card)
    for card in cards:
        _enforce_card_rules(card, capability)
    cards = [c for c in cards if len(c["options"]) >= 2]
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
    system = assemble.process_block() + "\n\n" + assemble.read_prompt("intake.md")

    user = assemble.build_context(form)
    res = await client.complete(system, user, model=form.get("model"))
    try:
        parsed = _parse_json_object(res.text)
    except (ValueError, json.JSONDecodeError):
        # 파싱 실패 → 카드 없이 진행 가능하게 (생성을 막지 않는다)
        return {"summary": "", "slots": [{"id": s, "label": SLOT_LABELS[s], "status": "weak", "known": None} for s in SLOT_ORDER],
                "cards": [], "missing": [], "weak": SLOT_ORDER[:], "ready": True, "model": res.model,
                "error": "인테이크 결과를 해석하지 못해 카드를 생략했습니다."}
    out = _normalize(parsed, str(form.get("capability") or ""))
    out["model"] = res.model
    return out


def _clean_options(items) -> list[dict]:
    """[{label, hint, source}] 정리 — 빈 label 제거, 중복 제거."""
    out, seen = [], set()
    for o in items or []:
        if not isinstance(o, dict):
            continue
        label = str(o.get("label", "")).strip()
        if label and label not in seen:
            seen.add(label)
            out.append({"label": label, "hint": str(o.get("hint", "")).strip(),
                        "source": str(o.get("source", "") or "").strip()})
    return out


def _regenerate_prompt(slots: list[str], seen: dict[str, list[str]], keep: dict[str, list[dict]], note: str) -> str:
    """intake.md + intake_regenerate.md(다시 만들 슬롯 · 유지할 보기 · 금지 보기 · 지원자 메모)."""
    def lines(getter):
        out = []
        for slot in slots:
            labels = getter(slot)
            out.append(f"- {slot} ({SLOT_LABELS[slot]}): " + (" / ".join(labels) if labels else "(없음)"))
        return "\n".join(out)

    extra = assemble.render("intake_regenerate.md",
                            slots=", ".join(f"{s} ({SLOT_LABELS[s]})" for s in slots),
                            keep=lines(lambda s: [o["label"] for o in keep.get(s, [])]),
                            seen=lines(lambda s: [str(x).strip() for x in (seen.get(s) or []) if str(x).strip()]),
                            note=note.strip() or "(없음)")
    return assemble.process_block() + "\n\n" + assemble.read_prompt("intake.md") + "\n\n" + extra



async def regenerate_cards(client: LLMClient, form: dict, slots: list[str],
                           seen: dict[str, list[str]] | None = None, note: str = "",
                           keep: dict[str, list[dict]] | None = None) -> dict:
    """지정한 슬롯의 카드만 다시 만든다 (LLM 1회). → {cards, slots(요청한 것), model, error?}

    seen: {slot: [이미 보여준 보기 label...]} — 결과에서 같은 보기는 걸러낸다(모델이 무시해도 코드가 막음).
    keep: {slot: [{label, hint}...]} — 지원자가 이미 고른 보기. 결과 카드 맨 앞에 그대로 남기고, 새 보기는 그 뒤에 붙는다.
    form.answers 에 다른 카드의 답이 있으면 [지원자가 확인한 사실]로 들어가 그와 어울리는 보기를 낸다.
    """
    slots = [s for s in slots if s in SLOT_ORDER]
    if not slots:
        raise ValueError("다시 만들 슬롯이 없습니다. slots 에 " + ", ".join(SLOT_ORDER) + " 중 하나 이상을 넣어주세요.")
    seen = seen or {}
    keep = {s: _clean_options(v) for s, v in (keep or {}).items() if s in SLOT_ORDER}
    system = _regenerate_prompt(slots, seen, keep, note)
    user = assemble.build_context(form)
    res = await client.complete(system, user, model=form.get("model"))
    try:
        parsed = _parse_json_object(res.text)
    except (ValueError, json.JSONDecodeError):
        return {"cards": [], "slots": slots, "model": res.model, "error": "카드를 다시 만들지 못했습니다. 한 번 더 눌러보세요."}
    cards = []
    for c in parsed.get("cards", []):
        if not isinstance(c, dict) or c.get("slot") not in slots or any(k["slot"] == c["slot"] for k in cards):
            continue
        kept = keep.get(c["slot"], [])
        banned = {str(x).strip() for x in seen.get(c["slot"], []) or []} | {o["label"] for o in kept}
        # 새 보기(금지·유지 보기 제외)가 1개 이상 있어야 의미가 있다. 유지 보기 + 새 보기를 합쳐 카드로.
        fresh = [o for o in _clean_options(c.get("options")) if o["label"] not in banned and not _FOREIGN_CJK.search(o["label"] + o["hint"])]
        if not fresh:
            continue
        card = _normalize_card({**c, "options": kept + fresh})
        if card is None:
            continue
        # _normalize_card 의 상한(MAX_OPTIONS)에 유지 보기가 밀리지 않도록: 유지 보기는 전부 + 새 보기 최소 2개
        limit = max(MAX_OPTIONS[card["type"]], len(kept) + 2)
        card["options"] = (kept + fresh)[:limit]
        cards.append(card)
    cards.sort(key=lambda c: CARD_PRIORITY.index(c["slot"]))
    out = {"cards": cards, "slots": slots, "model": res.model}
    if not cards:
        out["error"] = "새 보기를 만들지 못했습니다(이전 보기와 겹침). 메모를 적어서 다시 시도해 보세요."
    return out
