"""인테이크 — 멘토링(Q4-2) 슬롯·카드 보장, ready 계산, 카드 재생성. 모델 호출은 전부 가짜."""
import json

import pytest

from backend.llm.client import LLMResult
from backend.pipeline import intake as I


class FakeClient:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    async def complete(self, system, user, model=None):
        self.calls.append({"system": system, "user": user, "model": model})
        reply = self.replies.pop(0) if self.replies else ""
        return LLMResult(text=reply, model="fake")


FORM = {"track": "tech", "idea": "동네 반찬가게 남은 반찬을 저녁에 할인 꾸러미로 예약하는 앱", "team": "팀원 없음"}


def slot(sid, status="missing", known=None):
    return {"id": sid, "status": status, "known": known}


def card(sid, labels, ctype="single", qids=None):
    return {"slot": sid, "type": ctype, "question": f"{sid}?", "why": "w", "question_ids": qids or [],
            "options": [{"label": l, "hint": f"{l} 힌트"} for l in labels]}


def all_slots(**overrides):
    return [slot(s, *overrides.get(s, ("missing",))) for s in I.SLOT_ORDER]


# ── 멘토링 슬롯 ──

def test_mentoring_is_a_slot_and_optional_for_ready():
    assert "mentoring" in I.SLOT_ORDER and I.SLOT_LABELS["mentoring"]
    parsed = {"summary": "s", "slots": all_slots(customer=("known", "1인 가구"), problem=("known", "남은 반찬"),
                                                  revenue=("known", "수수료"), alternative=("weak", None), solution=("known", "앱"),
                                                  first_step=("known", "x"), evidence=("known", "y"), capability=("known", "z")),
              "cards": [card("mentoring", ["원가·가격 구조", "위생·인허가", "첫 고객 확보"], "multi", ["q4_2"])]}
    out = I._normalize(parsed)
    assert out["missing"] == ["mentoring"]
    assert out["ready"] is True, "mentoring 이 비어 있어도 핵심 정보가 충분하면 ready"
    assert [c["slot"] for c in out["cards"]] == ["mentoring"]
    assert out["cards"][0]["type"] == "multi" and out["cards"][0]["question_ids"] == ["q4_2"]


def test_mentoring_card_survives_max_cards_cut():
    """카드가 MAX_CARDS 보다 많아도 mentoring 카드는 잘리지 않는다 (우선순위상 뒤쪽이므로 보장 규칙이 필요)."""
    cards = [card(s, ["a", "b", "c"]) for s in I.CARD_PRIORITY]     # 9장
    out = I._normalize({"summary": "", "slots": all_slots(), "cards": cards})
    kept = [c["slot"] for c in out["cards"]]
    assert len(kept) == I.MAX_CARDS
    assert "mentoring" in kept
    assert kept[:I.MAX_CARDS - 1] == I.CARD_PRIORITY[:I.MAX_CARDS - 1]     # 앞 순위는 그대로


def test_no_card_for_known_mentoring():
    parsed = {"summary": "", "slots": all_slots(mentoring=("known", "원가 계산")),
              "cards": [card("mentoring", ["a", "b"]), card("problem", ["x", "y"])]}
    out = I._normalize(parsed)
    assert [c["slot"] for c in out["cards"]] == ["problem"]


def test_multi_cards_allow_more_options_than_single():
    parsed = {"summary": "", "slots": all_slots(),
              "cards": [card("mentoring", [f"m{i}" for i in range(10)], "multi"), card("problem", [f"p{i}" for i in range(10)])]}
    out = I._normalize(parsed)
    by = {c["slot"]: c for c in out["cards"]}
    assert len(by["mentoring"]["options"]) == I.MAX_OPTIONS["multi"]
    assert len(by["problem"]["options"]) == I.MAX_OPTIONS["single"]


def test_core_missing_is_still_not_ready():
    parsed = {"summary": "", "slots": all_slots(customer=("known", "x"), revenue=("known", "y")), "cards": []}
    assert I._normalize(parsed)["ready"] is False


# ── 카드 재생성 ──

def reply(cards):
    return json.dumps({"summary": "s", "slots": [], "cards": cards}, ensure_ascii=False)


@pytest.mark.asyncio
async def test_regenerate_returns_only_requested_slots_and_bans_seen_options():
    client = FakeClient([reply([
        card("problem", ["퇴근 후 저녁 준비 시간", "남은 반찬 처리 부담", "메뉴 고르기 피로"]),
        card("customer", ["자취생", "맞벌이"]),                      # 요청 안 한 슬롯 → 버림
    ])])
    seen = {"problem": ["남은 반찬 처리 부담", "저녁 장보기 귀찮음"]}
    out = await I.regenerate_cards(client, FORM, ["problem"], seen, note="너무 일반적이에요")
    assert [c["slot"] for c in out["cards"]] == ["problem"]
    labels = [o["label"] for o in out["cards"][0]["options"]]
    assert "남은 반찬 처리 부담" not in labels and len(labels) == 2
    assert "error" not in out and out["slots"] == ["problem"]

    system = client.calls[0]["system"]
    assert "[카드 다시 만들기" in system and "problem (해결할 불편)" in system
    assert "남은 반찬 처리 부담 / 저녁 장보기 귀찮음" in system      # 이미 보여준 보기가 금지 목록으로
    assert "너무 일반적이에요" in system
    assert "[슬롯 9개" in system                                       # intake.md 본문도 같이 들어감


@pytest.mark.asyncio
async def test_regenerate_keeps_picked_options_in_front_and_replaces_the_rest():
    """이미 고른 보기(keep)는 카드 맨 앞에 그대로 남고, 새 보기는 뒤에 붙는다. 모델이 keep 을 다시 내도 중복되지 않는다."""
    client = FakeClient([reply([card("problem", ["남은 반찬 처리 부담", "퇴근 후 저녁 준비 시간", "메뉴 고르기 피로"])])])
    keep = {"problem": [{"label": "남은 반찬 처리 부담", "hint": "원래 힌트"}]}
    seen = {"problem": ["남은 반찬 처리 부담", "저녁 장보기 귀찮음"]}
    out = await I.regenerate_cards(client, FORM, ["problem"], seen, keep=keep)
    opts = out["cards"][0]["options"]
    # source 는 웹 조사 근거 표기 — keep 으로 넘어온 보기엔 없으므로 빈 문자열
    assert opts[0] == {"label": "남은 반찬 처리 부담", "hint": "원래 힌트", "source": ""}  # 유지 보기가 맨 앞, 힌트도 원래 것
    assert [o["label"] for o in opts[1:]] == ["퇴근 후 저녁 준비 시간", "메뉴 고르기 피로"]
    system = client.calls[0]["system"]
    assert "이미 고른 보기" in system and "problem (해결할 불편): 남은 반찬 처리 부담" in system


@pytest.mark.asyncio
async def test_regenerate_keep_does_not_get_cut_by_option_limit():
    """유지 보기가 상한(single=4)을 차지해도 새 보기가 최소 2개는 들어간다."""
    client = FakeClient([reply([card("problem", ["새1", "새2", "새3"])])])
    keep = {"problem": [{"label": f"유지{i}", "hint": ""} for i in range(4)]}
    out = await I.regenerate_cards(client, FORM, ["problem"], keep=keep)
    labels = [o["label"] for o in out["cards"][0]["options"]]
    assert labels[:4] == ["유지0", "유지1", "유지2", "유지3"] and labels[4:] == ["새1", "새2"]


@pytest.mark.asyncio
async def test_regenerate_passes_other_answers_as_facts():
    client = FakeClient([reply([card("mentoring", ["a", "b"], "multi")])])
    form = dict(FORM, answers=[{"slot": "customer", "label": "대상 고객", "answer": ["1인 가구 직장인"], "unknown": False}])
    await I.regenerate_cards(client, form, ["mentoring"])
    assert "1인 가구 직장인" in client.calls[0]["user"]


@pytest.mark.asyncio
async def test_regenerate_all_options_seen_gives_error_not_crash():
    client = FakeClient([reply([card("problem", ["같은 보기", "또 같은 보기"])])])
    out = await I.regenerate_cards(client, FORM, ["problem"], {"problem": ["같은 보기", "또 같은 보기"]})
    assert out["cards"] == [] and "error" in out


@pytest.mark.asyncio
async def test_regenerate_garbage_output_gives_error_not_crash():
    client = FakeClient(["모델이 이상한 말"])
    out = await I.regenerate_cards(client, FORM, ["problem"])
    assert out["cards"] == [] and "error" in out and out["model"] == "fake"


@pytest.mark.asyncio
async def test_regenerate_rejects_unknown_slots():
    with pytest.raises(ValueError):
        await I.regenerate_cards(FakeClient([]), FORM, ["nope"])


# ── run_intake 기존 동작 ──

@pytest.mark.asyncio
async def test_run_intake_parse_failure_does_not_block():
    out = await I.run_intake(FakeClient(["???"]), FORM)
    assert out["ready"] is True and out["cards"] == [] and "error" in out
    assert [s["id"] for s in out["slots"]] == I.SLOT_ORDER
