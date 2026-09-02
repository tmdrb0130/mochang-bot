"""작업 22 — 생성 후처리: 이물·지어낸 수치·상용구·1인칭·중복 통계. 모델 호출 없음(가짜 클라이언트)."""
import pytest

from backend.llm.client import LLMResult
from backend.pipeline import generate
from backend.pipeline import polish as P


class Client:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.systems = []

    async def complete(self, system, user, model=None):
        self.systems.append(system)
        return LLMResult(text=self.replies.pop(0) if self.replies else "", model="fake")


SOLO = {"question_id": "q2", "track": "tech", "style": "logic", "team": "팀원 없음",
        "idea": "1인 가구 냉장고 관리 앱", "capability": "경영학 전공, 카페 아르바이트 2년",
        "answers": [{"slot": "evidence", "label": "직접 확인한 근거", "answer": None, "unknown": True}]}


# ── ④ 1인칭 ──

def test_solo_applicant_gets_one_voice():
    text = "저희는 앱을 만듭니다. 저희가 확인한 문제입니다. 저희 팀은 작습니다."
    out, n = P.unify_person(text, SOLO)
    assert "저희" not in out and n == 3
    assert out.startswith("저는 앱을") and "제가 확인한" in out

    team = {**SOLO, "team": "개발 1명, 기획 1명"}
    assert P.unify_person(text, team) == (text, 0)          # 팀이 있으면 그대로


def test_solo_applicant_detection():
    assert P.solo_applicant({"team": "팀원 없음"}) and P.solo_applicant({}) and P.solo_applicant({"team": "0명"})
    assert not P.solo_applicant({"team": "개발 2명"})


# ── ⑤ 같은 통계 재인용 ──

def test_repeated_statistic_sentence_is_dropped():
    text = ("한경에 따르면 시장은 917억 원입니다. 그래서 저는 앱을 만듭니다. "
            "다시 말해 917억 원 시장입니다. 침투율은 10%입니다.")
    out, n = P.drop_repeated_stats(text)
    assert n == 1 and out.count("917억") == 1
    assert "침투율은 10%입니다." in out and "저는 앱을 만듭니다." in out   # 새 통계·일반 문장은 남는다


def test_sentence_with_a_new_statistic_survives():
    text = "시장은 917억 원입니다. 917억 원 중 350억 원이 수도권입니다."
    assert P.drop_repeated_stats(text)[1] == 0              # 새 수치(350억)가 있으면 남긴다


# ── ①②③ 문장 표시 ──

@pytest.mark.parametrize("sentence,reason", [
    ("生活 속 불편을 해결합니다.", "이물"),
    ("まず 첫 달에는 준비합니다.", "이물"),
    ("첫해 매출 1억을 목표로 합니다.", "상용구 수치"),
    ("건당 5천 원을 받습니다.", "상용구 수치"),
    ("시장 규모는 2,340억 원입니다.", "근거 없는 수치"),
    ("1인 가구 10명에게 인터뷰했습니다.", "하지 않은 조사"),
])
def test_flags(sentence, reason):
    blob = P.allowed_numbers(SOLO)
    assert reason in P.flag(sentence, blob, evidence_known=False)


def test_numbers_from_the_input_are_not_flagged():
    form = {**SOLO, "references": [{"fact": "시장 917억 원", "quote": "917억", "url": "u"}]}
    blob = P.allowed_numbers(form)
    assert P.flag("시장은 917억 원입니다.", blob, False) == []        # 참고자료에 있는 수치는 통과
    assert P.unsupported_numbers("917억 원과 350억 원", blob) == ["350억"]   # 정규식이 잡은 형태 그대로


def test_interview_is_fine_when_the_applicant_actually_did_it():
    form = {**SOLO, "answers": [{"slot": "evidence", "answer": "자취 커뮤니티 12명에게 물어봄"}]}
    blob = P.allowed_numbers(form)
    assert P.flag("12명에게 물어봤습니다.", blob, P.evidence_known(form)) == []
    assert P.evidence_known(form) is True and P.evidence_known(SOLO) is False


# ── 고쳐 쓰기 ──

@pytest.mark.asyncio
async def test_problem_sentences_are_rewritten_in_one_call():
    text = ("저희는 生活 속 불편을 해결합니다. 첫해 매출 1억을 목표로 합니다. 저는 앱을 만듭니다.")
    client = Client("1. 저는 생활 속 불편을 해결합니다.\n2. 초기 사용자를 모읍니다.")
    out, report = await P.polish_text(client, text, SOLO)

    assert len(client.systems) == 1 and "문장 고쳐 쓰기" in client.systems[0]
    assert "生活" not in out and "1억" not in out
    assert "저희" not in out and report["person"] == 1 and report["rewritten"] == 2


@pytest.mark.asyncio
async def test_sentence_is_dropped_when_the_rewrite_still_breaks_the_rule():
    text = "먼저 需求를 확인합니다. 저는 앱을 만듭니다."
    client = Client("1. 需求를 또 확인합니다.")               # 고쳐 오지 못함
    out, report = await P.polish_text(client, text, SOLO)
    assert "需求" not in out and out.strip() == "저는 앱을 만듭니다."
    assert report["dropped"] == 1 and report["rewritten"] == 0


@pytest.mark.asyncio
async def test_ambiguous_sentence_is_kept_when_the_rewrite_fails():
    """애매한 것(근거 없는 수치만 있는 문장)은 지우지 않는다 — 지우면 글이 빈다."""
    text = "시장 규모는 2,340억 원으로 추정됩니다. 저는 앱을 만듭니다."
    client = Client("")                                      # 모델이 아무 것도 못 줌
    out, report = await P.polish_text(client, text, SOLO)
    assert "2,340억" in out and report["left"] == 1


@pytest.mark.asyncio
async def test_clean_text_costs_no_extra_call():
    text = "저는 자취 3년 차 1인 가구를 위해 앱을 만듭니다. 아직 확인하지 못한 부분은 계획으로 적었습니다."
    client = Client()
    out, report = await P.polish_text(client, text, SOLO)
    assert out == text and client.systems == [] and report["rewritten"] == 0


@pytest.mark.asyncio
async def test_generate_one_runs_polish_and_reports_it():
    long_clean = "저는 앱을 만듭니다. " * 60
    client = Client(long_clean, "1. 고침")
    out = await generate.generate_one(client, {**SOLO, "auto_extend": {"enabled": False},
                                               "outline_settings": {"enabled": False},
                                               "polish_settings": {"enabled": True}})
    assert out["polished"]["person"] == 0 and "polished" in out


@pytest.mark.asyncio
async def test_polish_can_be_turned_off():
    text = "저희는 生活 속에서 첫해 매출 1억을 냅니다. " * 40
    client = Client(text)
    out = await generate.generate_one(client, {**SOLO, "polish_settings": {"enabled": False},
                                               "auto_extend": {"enabled": False},
                                               "outline_settings": {"enabled": False}})
    assert out["polished"] == {} and "저희" in out["text"]


# ── 작업 26 보강: 자기소개 첫 문장·추상어 ──

def test_q2_self_intro_first_sentence_is_dropped():
    text = ("저는 예비창업자로서 관련 분야 경험 2년을 가지고 있습니다. "
            "장을 본 뒤 재료를 쟁여 두고도 때를 놓치는 일이 반복됐습니다. "
            "혼자서는 무엇이 문제인지 판단하기 어려웠습니다.")
    out, n = P.drop_self_intro(text, "q2")
    assert n == 1 and out.startswith("장을 본 뒤")

    assert P.drop_self_intro(text, "q3_1") == (text, 0)      # 다른 문항은 손대지 않는다
    short = "저는 예비창업자입니다. 한 문장 더."
    assert P.drop_self_intro(short, "q2") == (short, 0)      # 남는 문장이 적으면 그대로


def test_abstract_words_are_flagged():
    blob = P.allowed_numbers(SOLO)
    assert "추상어" in P.flag("저는 고유한 가치를 만들고자 합니다.", blob, False)
    assert "추상어" in P.flag("작업을 효율적으로 바꿉니다.", blob, False)
    assert P.flag("사진으로 재고를 등록하면 임박한 재료를 알려 줍니다.", blob, False) == []


# ── 작업 22 ⑥: 아이디어 재진술 첫 문장·중복 문장 ──

def test_idea_restatement_first_sentence_is_dropped():
    form = {**SOLO, "question_id": "q4_1", "idea": "1인 가구 냉장고 관리 앱"}
    text = ("저는 1인 가구 냉장고 관리 앱을 만들고자 합니다. "
            "첫 단계에서는 최소 기능만 만들어 봅니다. "
            "그다음 이용자 열 명에게 써 보게 합니다.")
    out, n = P.drop_idea_restatement(text, form)
    assert n == 1 and out.startswith("첫 단계에서는")

    keep = {**form, "question_id": "q1"}
    assert P.drop_idea_restatement(text, keep) == (text, 0)     # Q1 은 그것이 답이다

    other = ("장을 본 뒤 재료를 쟁여 두고도 때를 놓칩니다. 두 번째 문장입니다. 세 번째 문장입니다.")
    assert P.drop_idea_restatement(other, form) == (other, 0)   # 재진술이 아니면 그대로


def test_duplicate_sentences_are_removed():
    """자동 이어쓰기가 앞 문단을 되풀이한 실측 건 — 모델 호출 없이 지운다."""
    a = "현재 이용자들은 직접 검색하거나 주변에 물어보는 방법을 쓰고 있습니다."
    b = "현재 이용자들은 직접 검색하거나 주변에 물어보는 방식을 쓰고 있습니다."
    c = "전문가에게 맡기면 비용과 시간이 많이 듭니다."
    out, n = P.dedupe_sentences(" ".join([a, c, b]))
    assert n == 1 and b not in out and c in out and out.startswith(a)


def test_similarity_matches_the_measurement_tool():
    assert P.similarity("같은 문장입니다.", "같은 문장입니다.") == 1.0
    assert P.similarity("완전히 다른 이야기", "숫자와 통계 이야기") < 0.3


def test_foreign_sentences_dropped_without_a_model_call():
    text = "첫 문장입니다. 두 번째는 需求를 확인합니다. 세 번째 문장입니다."
    out, n = P.drop_foreign_sentences(text)
    assert n == 1 and "需求" not in out and out.startswith("첫 문장") and "세 번째" in out


def test_short_questions_lose_a_leading_self_reference():
    assert P.strip_leading_self("저는 딸기를 모아 포장해 보내는 공동 센터를 만듭니다.", "q1") ==         ("딸기를 모아 포장해 보내는 공동 센터를 만듭니다.", 1)
    assert P.strip_leading_self("저는 그렇게 생각합니다.", "q2") == ("저는 그렇게 생각합니다.", 0)   # 긴 문항은 손대지 않음
    assert P.strip_leading_self("딸기를 모아 보냅니다.", "q10") == ("딸기를 모아 보냅니다.", 0)

