"""작업 18 — 긴 문항이 목표의 70%도 못 채우면 서버가 이어쓰기를 1회 대신 돌린다. 모델 호출 없음(가짜 클라이언트)."""
import pytest

from backend.llm.client import LLMResult
from backend.pipeline import generate


class Client:
    """첫 호출은 생성, 두 번째는 이어쓰기. 각 호출의 system 프롬프트를 기록한다."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.systems = []

    async def complete(self, system, user, model=None):
        self.systems.append(system)
        return LLMResult(text=self.replies.pop(0) if self.replies else "", model="fake")


FORM = {"question_id": "q2", "track": "tech", "style": "logic", "idea": "냉장고 식재료 관리 앱",
        "team": "2명", "capability": "경험"}
OFF = {"auto_extend": {"enabled": False}}


def test_needs_extend_only_for_long_questions_below_the_ratio():
    s = {"enabled": True, "min_ratio": 0.7, "min_limit": 500}
    assert generate.needs_extend("가" * 698, 2000, s) is True          # 작업 16 검증에서 나온 실제 길이
    assert generate.needs_extend("가" * 1400, 2000, s) is False        # 정확히 70% 는 통과
    assert generate.needs_extend("가" * 50, 100, s) is False           # Q1·Q10 은 대상 아님
    assert generate.needs_extend("가" * 10, 2000, {**s, "enabled": False}) is False


def test_settings_come_from_config_and_can_be_overridden():
    assert generate.auto_extend_settings()["min_ratio"] == 0.7        # config.yaml 값
    over = generate.auto_extend_settings({"auto_extend": {"min_ratio": 0.9, "enabled": False}})
    assert over["min_ratio"] == 0.9 and over["enabled"] is False and over["min_limit"] == 500


@pytest.mark.asyncio
async def test_short_answer_is_extended_once():
    short, added = "가" * 600, "나" * 900
    client = Client(short, added)
    out = await generate.generate_one(client, dict(FORM))

    assert out["auto_extended"] is True and len(client.systems) == 2
    assert "이미 작성된 글" in client.systems[1] or "이어" in client.systems[1]   # 이어쓰기 프롬프트로 갔다
    assert out["text"].startswith(short) and len(out["text"]) > len(short)
    assert out["length"] == len(out["text"])


@pytest.mark.asyncio
async def test_long_enough_answer_is_left_alone():
    client = Client("가" * 1500)
    out = await generate.generate_one(client, dict(FORM))
    assert out["auto_extended"] is False and len(client.systems) == 1


@pytest.mark.asyncio
async def test_disabled_by_config():
    client = Client("가" * 300)
    out = await generate.generate_one(client, {**FORM, **OFF})
    assert out["auto_extended"] is False and len(client.systems) == 1


@pytest.mark.asyncio
async def test_extend_failure_keeps_the_original_text():
    """이어쓰기가 실패하거나 더 짧아지면 원래 글을 그대로 둔다 — 늘리려다 망가뜨리지 않는다."""
    short = "가" * 600
    client = Client(short, "")                       # 이어쓰기가 빈 응답
    out = await generate.generate_one(client, dict(FORM))
    assert out["auto_extended"] is False and out["text"] == short

    class Boom(Client):
        async def complete(self, system, user, model=None):
            self.systems.append(system)
            if len(self.systems) > 1:
                raise RuntimeError("모델 오류")
            return LLMResult(text=short, model="fake")

    out2 = await generate.generate_one(Boom(), dict(FORM))
    assert out2["auto_extended"] is False and out2["text"] == short


@pytest.mark.asyncio
async def test_short_question_never_triggers_extend():
    """Q1 은 100자 문항이라 짧아도 이어쓰기를 하지 않는다 (호출이 늘면 안 된다)."""
    client = Client("빈자리를 미리 알려 줍니다.")
    out = await generate.generate_one(client, {**FORM, "question_id": "q1", "structure_offset": 0})
    assert out["auto_extended"] is False and len(client.systems) == 1
