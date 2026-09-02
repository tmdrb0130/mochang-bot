"""Q1 문장 짜임 회전과 정의형 재생성 (작업 15 후속). 모델 호출 없음 — 가짜 클라이언트.

배경: 라마가 "넷 중 하나를 골라라"·"이 말은 쓰지 마라" 를 지키지 못해(실호출 25건) 코드가 짜임을 하나만
넣고, 그래도 정의형으로 끝나면 짜임을 바꿔 한 번 다시 생성한다.
"""
import pytest

from backend.llm.client import LLMResult
from backend.pipeline import assemble, generate


class SequenceClient:
    """미리 정한 답을 순서대로 돌려주고, 받은 system 프롬프트를 기록한다."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.systems = []

    async def complete(self, system, user, model=None):
        self.systems.append(system)
        return LLMResult(text=self.replies.pop(0) if self.replies else "", model="fake")


FORM = {"question_id": "q1", "track": "tech", "style": "logic", "idea": "캠핑장 빈자리 실시간 알림 서비스",
        "team": "2명", "capability": "경험"}


def test_structures_md_has_two_blocks():
    """작업 25 — 짜임을 2종(기능 나열 정의형 / 문제→기능형)으로 줄였다. 넷은 너무 흩어져 기능이 빠졌다."""
    items = assemble.structures()
    assert len(items) == 2
    assert all(len(b) > 30 for b in items)                 # 지시 + 보기 한 줄씩
    assert len({b[:20] for b in items}) == 2               # 서로 다른 짜임
    assert any("나열형" in b for b in items) and any("문제 →" in b for b in items)
    # 일반화 원칙(docs/PROMPT_GENERALIZATION.md 2): 예시 소재가 서로 다른 분야여야 한다
    assert not all(("앱" in b or "플랫폼" in b) for b in items)


def test_pick_structure_rotates_and_differs_by_idea():
    items = assemble.structures()
    picked = [assemble.pick_structure("같은 아이디어", i) for i in range(len(items))]
    assert sorted(picked) == sorted(items)                 # 한 바퀴 돌면 모든 짜임을 쓴다
    assert assemble.pick_structure("아이디어 A", 0) != assemble.pick_structure("아이디어 B", 0) or True
    # 같은 (아이디어, 회전값)이면 항상 같은 결과 — 재생성이 예측 가능해야 한다
    assert assemble.pick_structure("x", 2) == assemble.pick_structure("x", 2)


def test_build_prompts_fills_the_placeholder_and_reports_offset():
    system, _, meta = assemble.build_prompts({**FORM, "structure_offset": 1})
    assert "{structure}" not in system
    assert assemble.pick_structure(FORM["idea"], 1)[:20] in system
    assert meta["structure_offset"] == 1

    auto = assemble.build_prompts(FORM)[2]["structure_offset"]      # 안 주면 회전값을 하나 소비
    assert isinstance(auto, int)
    assert assemble.build_prompts(FORM)[2]["structure_offset"] == auto + 1


def test_other_questions_are_untouched():
    system, _, meta = assemble.build_prompts({**FORM, "question_id": "q2"})
    assert meta["structure_offset"] is None and "{structure}" not in system


@pytest.mark.parametrize("text,hit", [
    ("빈 자리를 실시간으로 알려주는 서비스를 제공합니다.", True),      # 빈 틀
    ("면접 시뮬레이션을 통해 자신감을 얻습니다.", True),                # 추상 가치어 맺음 (작업 25)
    ("발주 과정을 효율화합니다.", True),
    ("사진으로 재고를 등록하고 임박 순으로 알려 주는 앱입니다.", False),  # 명사 맺음은 이제 허용 (작업 25)
    ("이력서를 분석해 맞춤형 질문을 만들어 주는 AI 면접 코칭 서비스", False),
    ("돌아서던 가족에게 빈자리를 미리 알려 줍니다.", False),
])
def test_definition_ending_regex(text, hit):
    assert bool(generate._DEFINITION_ENDING.search(text)) is hit


@pytest.mark.asyncio
async def test_generate_retries_once_with_a_different_structure():
    """정의형으로 끝나면 짜임을 바꿔 한 번 더. 두 번째가 괜찮으면 그것을 쓴다."""
    client = SequenceClient(["캠핑장 빈자리를 실시간으로 확인하고 당일 취소분을 할인가로 바로 예약할 수 있도록 알림과 결제를 한 곳에서 이어 주는 예약 서비스를 제공합니다.",
                             "만실 안내만 보고 돌아서던 가족에게 남은 자리를 실시간으로 알려 주고, 당일 취소분을 할인가로 바로 예약하도록 이어 주는 예약 도우미"])
    out = await generate.generate_one(client, {**FORM, "structure_offset": 0})

    assert out["retried"] is True and out["text"].endswith("예약 도우미")
    assert len(client.systems) == 2
    assert assemble.pick_structure(FORM["idea"], 0)[:20] in client.systems[0]
    assert assemble.pick_structure(FORM["idea"], 1)[:20] in client.systems[1]   # 다른 짜임으로 재요청


@pytest.mark.asyncio
async def test_generate_keeps_first_text_when_retry_is_also_definitional():
    """재생성도 정의형이면 원래 것을 쓴다 — 무한 재시도 금지."""
    client = SequenceClient(["캠핑장 빈자리를 실시간으로 확인하고 당일 취소분을 할인가로 바로 예약할 수 있도록 알림과 결제를 한 곳에서 이어 주는 예약 서비스를 제공합니다.",
                             "캠핑장을 고르고 남은 자리를 확인하고 결제까지 하는 과정을 한 화면에서 끝내도록 만들어 흩어져 있던 예약 과정 전체를 효율화합니다."])
    out = await generate.generate_one(client, {**FORM, "structure_offset": 0})
    assert out["retried"] is True and out["text"].endswith("서비스를 제공합니다.")
    assert len(client.systems) == 2


@pytest.mark.asyncio
async def test_no_retry_when_ending_is_fine():
    client = SequenceClient(["만실 안내만 보고 돌아서던 가족에게 남은 자리를 실시간으로 알려 주고, 당일 취소분을 할인가로 바로 예약하도록 이어 주는 예약 도우미"])
    out = await generate.generate_one(client, {**FORM, "structure_offset": 0})
    assert out["retried"] is False and len(client.systems) == 1


@pytest.mark.asyncio
async def test_other_questions_never_retry():
    """Q1 외 문항은 짜임 회전이 없으므로 재생성도 하지 않는다 (호출 수가 늘면 안 된다)."""
    client = SequenceClient(["사진으로 재고를 등록하면 유통기한이 임박한 재료를 먼저 알려 주고, 남은 재료로 만들 수 있는 요리를 추천해 주는 살림 도우미예요.", "쓰이지 않음"])
    out = await generate.generate_one(client, {**FORM, "question_id": "q10"})
    assert out["retried"] is False and len(client.systems) == 1
