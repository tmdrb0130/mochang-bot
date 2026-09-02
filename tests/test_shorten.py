"""작업 21 — 짧은 문항(Q1·Q10)이 한도를 넘으면 자르지 않고 '줄여 다시 쓰기'. 모델 호출 없음."""
import pytest

from backend.llm.client import LLMResult
from backend.pipeline import generate


class Client:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.systems = []

    async def complete(self, system, user, model=None):
        self.systems.append(system)
        return LLMResult(text=self.replies.pop(0) if self.replies else "", model="fake")


FORM = {"question_id": "q1", "track": "tech", "style": "logic", "idea": "냉장고 식재료 관리 앱",
        "team": "2명", "capability": "경험", "structure_offset": 0}


def test_postprocess_does_not_slice_short_answers_mid_word():
    """작업 15 때 넣은 어절 자름+'…' 은 긴 문항에만. 짧은 문항은 손대지 않고 그대로 돌려준다."""
    long_one = "가" * 130                                   # 문장 끝이 없는 130자
    assert generate.postprocess(long_one, 100) == long_one   # 자르지 않음
    assert "…" not in generate.postprocess(long_one, 100)

    two_sentences = "첫 문장입니다. " + "나" * 120
    assert generate.postprocess(two_sentences, 100) == two_sentences     # 짧은 문항은 postprocess 가 손대지 않는다

    long_q = "가" * 2200
    assert generate.postprocess(long_q, 2000).endswith("…")  # 긴 문항은 예전 동작 그대로


def test_cut_at_sentence_end_is_the_last_resort():
    assert generate._cut_at_sentence_end("짧다.", 100) == "짧다."
    assert generate._cut_at_sentence_end("한 문장입니다. " + "가" * 200, 100) == "한 문장입니다."
    keep = "가" * 130                                        # 자를 문장 끝이 없으면 원문 유지
    assert generate._cut_at_sentence_end(keep, 100) == keep


@pytest.mark.asyncio
async def test_over_limit_answer_is_regenerated_shorter():
    over = "고객에게 " + "가" * 110 + " 알려 줍니다."          # 100자 초과
    ok = "장 본 것을 잊고 버리던 1인 가구에게 임박한 식재료를 먼저 알려 줍니다."
    client = Client(over, ok)
    out = await generate.generate_one(client, dict(FORM))

    assert out["shortened"] == 1 and out["text"] == ok and out["length"] <= 100
    assert len(client.systems) == 2
    assert "줄여 쓰기 지시" in client.systems[1] and "85자 이내" in client.systems[1]   # 한도-15


@pytest.mark.asyncio
async def test_shorten_tries_twice_then_cuts_at_a_sentence_end():
    over1 = "가" * 130 + " 첫 시도입니다."
    over2 = "나" * 120 + " 두 번째입니다."
    client = Client(over1, over2, "쓰이지 않음")
    out = await generate.generate_one(client, dict(FORM))

    assert out["shortened"] == 2 and len(client.systems) == 3        # 생성 1 + 줄이기 2
    assert len(out["text"]) <= 100 or out["text"] == over2           # 문장 끝에서만 자른다
    assert "…" not in out["text"]


@pytest.mark.asyncio
async def test_shorten_keeps_the_shortest_candidate():
    """두 번째 시도가 더 길면 첫 번째 결과를 쓴다."""
    over = "가" * 130
    mid = "다" * 105                                        # 아직 초과지만 더 짧다
    worse = "라" * 140
    client = Client(over, mid, worse)
    out = await generate.generate_one(client, dict(FORM))
    assert out["text"] == mid and out["shortened"] == 2


@pytest.mark.asyncio
async def test_within_limit_answer_is_untouched():
    ok = "장 본 것을 잊고 버리던 1인 가구에게 임박한 식재료를 먼저 알려 줍니다."
    client = Client(ok)
    out = await generate.generate_one(client, dict(FORM))
    assert out["shortened"] == 0 and out["text"] == ok and len(client.systems) == 1


@pytest.mark.asyncio
async def test_long_questions_never_use_shorten():
    """2,000자 문항은 예전처럼 자른다 — 줄이기 호출이 붙으면 안 된다."""
    client = Client("가" * 2500)
    out = await generate.generate_one(client, {**FORM, "question_id": "q2"})
    assert out["shortened"] == 0 and len(client.systems) == 1
    assert len(out["text"]) <= 2000


@pytest.mark.asyncio
async def test_shorten_survives_model_error():
    over = "가" * 130

    class Boom(Client):
        async def complete(self, system, user, model=None):
            self.systems.append(system)
            if len(self.systems) > 1:
                raise RuntimeError("모델 오류")
            return LLMResult(text=over, model="fake")

    out = await generate.generate_one(Boom(), dict(FORM))
    assert out["text"] == over and out["shortened"] == generate.SHORTEN_TRIES   # 원문 유지, 잘림 없음
