"""작업 20 — 사업계획 골자를 아이디어당 1회 만들어 문항 9개가 공유한다. 모델 호출 없음(가짜 클라이언트)."""
import asyncio
import json

import pytest

from backend.llm.client import LLMResult
from backend.pipeline import assemble, generate
from backend.pipeline import outline as O
from backend.rag.research import DiskCache

pytestmark = pytest.mark.outline          # conftest 의 '골자 끄기' 기본값을 이 파일에서는 쓰지 않는다


RAW = {
    "customer": "  자취 3년차  1인 가구 ",
    "problem": "장 본 것을 잊고 유통기한을 넘긴다",
    "evidence": "검증 계획: 자취 커뮤니티 20명에게 설문",
    "solution": "사진 한 장으로 재고 등록, 임박 순 알림",
    "differentiators": ["메모 앱과 달리 자동 인식", "임박 재료 레시피 추천", "세 번째", "네 번째"],
    "revenue": "월 구독 (금액 미정)",
    "plan_6m": ["최소 기능만", "노코드로 제작", "30명 사용성 테스트"],
    "capability": "식품 유통 데이터 분석 3년",
    "gaps": ["수익모델 검증 방법"],
    "key_stats": ["1인 가구 36.6% (통계청 2025)", "폐기 130g (환경부)", "세 번째 수치"],
}

FORM = {"question_id": "q2", "track": "tech", "style": "logic", "idea": "냉장고 식재료 관리 앱",
        "team": "2명", "capability": "경험",
        "answers": [{"slot": "customer", "label": "대상 고객", "answer": "1인 가구"}]}


class Client:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.systems = []

    async def complete(self, system, user, model=None):
        self.systems.append(system)
        return LLMResult(text=self.replies.pop(0) if self.replies else "", model="fake")


def outline_reply(raw=None):
    return "```json\n" + json.dumps(raw or RAW, ensure_ascii=False) + "\n```"


# ── 형식 ──

def test_normalize_fixes_shape_and_caps_list_lengths():
    out = O.normalize(RAW)
    assert out["customer"] == "자취 3년차 1인 가구"                 # 공백 정리
    assert len(out["differentiators"]) == 3 and len(out["key_stats"]) == 2   # 수치는 2개까지
    assert set(out) == set(O.OUTLINE_KEYS)

    empty = O.normalize({})
    assert set(empty) == set(O.OUTLINE_KEYS) and not any(empty.values())      # 키는 항상 같은 모양
    assert O.normalize({"gaps": "한 개만 문자열로"})["gaps"] == ["한 개만 문자열로"]


def test_format_outline_is_readable_and_short_form_has_two_lines():
    full = O.format_outline(O.normalize(RAW))
    assert "핵심 고객:" in full and "6개월 계획:" in full and "인용할 수치:" in full
    short = O.format_outline(O.normalize(RAW), short=True)
    assert short.count("\n") == 1 and "핵심 고객" in short and "해결책" in short
    assert "수익" not in short and "6개월" not in short


def test_cache_key_follows_idea_and_answers():
    a = O.cache_key(FORM)
    assert a == O.cache_key(dict(FORM))
    assert a != O.cache_key({**FORM, "idea": "다른 아이디어"})
    assert a != O.cache_key({**FORM, "answers": [{"slot": "customer", "answer": "다른 답"}]})
    assert a == O.cache_key({**FORM, "question_id": "q8"})       # 문항이 달라도 같은 골자를 쓴다


# ── 생성·캐시 ──

@pytest.mark.asyncio
async def test_build_outline_parses_json_and_survives_garbage():
    got = await O.build_outline(Client(outline_reply()), dict(FORM))
    assert got["customer"] == "자취 3년차 1인 가구" and got["plan_6m"][2] == "30명 사용성 테스트"
    assert await O.build_outline(Client("모델이 이상한 말"), dict(FORM)) == {}


@pytest.mark.asyncio
async def test_key_stats_are_dropped_without_references():
    """참고자료가 없으면 인용할 수치도 없다 — 모델이 출처를 지어내는 일이 실호출에서 나왔다."""
    got = await O.build_outline(Client(outline_reply()), dict(FORM))
    assert got["key_stats"] == [] and got["customer"]        # 나머지는 그대로

    # 참고자료가 있어도 **그 안에 실제로 있는 숫자**만 남는다
    refs = [{"fact": "1인 가구 36.6%", "quote": "1인 가구 비중은 36.6%", "url": "https://kostat.go.kr/a"}]
    with_refs = await O.build_outline(Client(outline_reply({**RAW, "key_stats": [
        "1인 가구 36.6% (통계청 2025)", "식품 폐기 130g (환경부)", "출처만 있고 숫자 없음"]})),
        {**FORM, "references": refs})
    assert with_refs["key_stats"] == ["1인 가구 36.6% (통계청 2025)"]      # 130g·숫자없음 은 버려진다


def test_verified_stats_drops_numbers_not_in_the_references():
    refs = [{"fact": "폐기율 23%", "quote": "하루 폐기율은 평균 23%", "url": "https://a.go.kr/1"}]
    assert O.verified_stats(["폐기율 23% (서울시)"], refs) == ["폐기율 23% (서울시)"]
    assert O.verified_stats(["폐기율 70% (한국통계청)"], refs) == []       # 참고자료에 없는 숫자
    assert O.verified_stats(["수치 없는 문장"], refs) == []
    # 인용 표기의 연도(2025)는 참고자료에 없어도 통과 — 대조 대상은 '의미 있는 수치' 다
    assert O.verified_stats(["폐기율 23% (서울시 2025)"], refs) == ["폐기율 23% (서울시 2025)"]
    assert O.verified_stats(["폐기율 23%"], []) == []                      # 참고자료 자체가 없으면 전부 버림


@pytest.mark.asyncio
async def test_outline_is_made_once_and_reused_from_cache(tmp_path):
    cache = DiskCache(tmp_path)
    client = Client(outline_reply())
    first = await O.get_outline(client, dict(FORM), cache=cache)
    second = await O.get_outline(Client(), {**FORM, "question_id": "q8"}, cache=cache)
    assert first == second and len(client.systems) == 1          # 두 번째 문항은 모델 호출 0회


@pytest.mark.asyncio
async def test_concurrent_requests_build_the_outline_only_once(tmp_path):
    """프론트가 문항 3개를 동시에 제출해도 골자는 한 번만 만든다."""
    cache = DiskCache(tmp_path)
    client = Client(outline_reply(), outline_reply(), outline_reply())
    O._locks.clear()
    outs = await asyncio.gather(*(O.get_outline(client, {**FORM, "question_id": q}, cache=cache)
                                 for q in ("q2", "q3_1", "q8")))
    assert len(client.systems) == 1 and outs[0] == outs[1] == outs[2]


@pytest.mark.asyncio
async def test_failed_outline_is_not_cached(tmp_path):
    cache = DiskCache(tmp_path)
    assert await O.get_outline(Client("쓰레기"), dict(FORM), cache=cache) == {}
    good = await O.get_outline(Client(outline_reply()), dict(FORM), cache=cache)
    assert good["customer"]                                       # 실패를 캐시했다면 빈 값이 나왔을 것


# ── 문항 프롬프트에 실리는지 ──

def test_outline_section_goes_into_the_user_prompt():
    ctx = assemble.build_context({**FORM, "outline": O.normalize(RAW)})   # FORM 은 q2
    assert "[사업계획 골자" in ctx and "자취 3년차 1인 가구" in ctx
    assert "검증 계획" in ctx and "노코드로 제작" not in ctx    # q2 몫(문제 확인 방법)은 들어가고 계획은 빠진다

    short = assemble.build_context({**FORM, "question_id": "q1", "outline": O.normalize(RAW)})
    assert "자취 3년차 1인 가구" in short and "노코드로 제작" not in short      # 짧은 문항은 두 줄만


@pytest.mark.parametrize("qid,must", [("q2", "문제 확인 방법"), ("q3_1", "기존 대안과 다른 점"),
                                      ("q3_2", "수익 방식"), ("q4_1", "6개월 계획"),
                                      ("q4_2", "아직 모르는 것"), ("q8", "지원자 역량")])
def test_each_question_has_its_role_rule(qid, must):
    _, body = assemble.load_question(qid)
    rules = body.split("[골자 사용 규칙]")[1].split("[필수 요소]")[0]
    assert "펼쳐 씁니다" in rules and must in rules


@pytest.mark.asyncio
async def test_generate_one_makes_the_outline_then_writes(tmp_path, monkeypatch):
    monkeypatch.setattr(O, "DiskCache", lambda *a, **kw: DiskCache(tmp_path))
    client = Client(outline_reply(), "문항 본문입니다. " * 60)
    # 자동 이어쓰기(작업 18)는 여기서 관심 밖이라 끈다 — 켜 두면 짧은 본문 때문에 호출이 하나 더 붙는다
    # config.yaml 값에 좌우되지 않게 켜는 것도 명시한다 (실측 때 잠깐 꺼 두는 일이 있다)
    out = await generate.generate_one(client, {**FORM, "outline_settings": {"enabled": True},
                                              "auto_extend": {"enabled": False}})

    assert out["outline_used"] is True and len(client.systems) == 2
    assert "[사업계획 골자" not in client.systems[1]              # 골자는 user 프롬프트에 들어간다
    assert out["text"].startswith("문항 본문입니다.")


@pytest.mark.asyncio
async def test_outline_can_be_turned_off():
    client = Client("문항 본문입니다. " * 60)
    out = await generate.generate_one(client, {**FORM, "outline_settings": {"enabled": False},
                                              "auto_extend": {"enabled": False}})
    assert out["outline_used"] is False and len(client.systems) == 1


# ── 문항별 몫만 보여 주기 (전/후 실측 뒤 보강) ──

def test_slice_gives_each_question_only_its_part():
    full = O.normalize(RAW)
    q2 = O.slice_for(full, "q2")
    assert set(q2) == {"problem", "customer", "evidence", "solution", "key_stats"}
    assert "revenue" not in q2 and "plan_6m" not in q2          # 남의 몫은 아예 안 보인다

    assert set(O.slice_for(full, "q3_2")) == {"revenue", "customer"}
    assert set(O.slice_for(full, "q8")) == {"capability", "plan_6m"}
    assert set(O.slice_for(full, "q4_2")) == {"gaps", "capability"}
    assert set(O.slice_for(full, "모르는문항")) == set(full)       # 모르는 문항이면 전체


def test_each_stat_is_assigned_to_one_question():
    """같은 수치를 여러 문항이 인용하면 심사위원에게 반복으로 읽힌다 — 문항마다 하나씩."""
    full = O.normalize(RAW)
    assert O.slice_for(full, "q2")["key_stats"] == [full["key_stats"][0]]
    assert O.slice_for(full, "q3_1")["key_stats"] == [full["key_stats"][1]]
    assert "key_stats" not in O.slice_for(full, "q4_1")
    assert "key_stats" not in O.slice_for(full, "q8")


def test_context_only_carries_this_questions_slice():
    ctx = assemble.build_context({**FORM, "question_id": "q3_2", "outline": O.normalize(RAW)})
    assert "월 구독" in ctx                                       # 수익 방식은 이 문항 몫
    assert "노코드로 제작" not in ctx and "검증 계획" not in ctx    # 계획·검증은 다른 문항 몫


# ── 참고자료 분배 (전/후 실측에서 드러난 반복의 주범) ──

def test_each_question_cites_a_different_slice_of_the_references():
    refs = [{"fact": f"사실 {i}", "quote": f"q{i}", "url": f"https://a.co/{i}",
             "date": "2026-01-01", "publisher": "출처"} for i in range(6)]
    q2 = assemble.references_for(refs, "q2")
    q3_1 = assemble.references_for(refs, "q3_1")
    q3_2 = assemble.references_for(refs, "q3_2")

    assert [r["fact"] for r in q2] == ["사실 0", "사실 1"]
    assert [r["fact"] for r in q3_1] == ["사실 2", "사실 3"]
    assert [r["fact"] for r in q3_2] == ["사실 4", "사실 5"]
    assert not (set(r["url"] for r in q2) & set(r["url"] for r in q3_1))   # 겹치지 않는다


def test_reference_slicing_is_skipped_when_there_are_few_facts():
    refs = [{"fact": "하나", "url": "u"}, {"fact": "둘", "url": "v"}]
    assert assemble.references_for(refs, "q8") == refs        # 2건뿐이면 그대로 (나눌 것이 없다)
    assert assemble.references_for(refs, "q2", per=0) == refs  # per=0 이면 예전 동작


def test_context_shows_only_this_questions_references():
    refs = [{"fact": f"사실 {i}", "quote": f"q{i}", "url": f"https://a.co/{i}",
             "date": "2026-01-01", "publisher": "출처"} for i in range(6)]
    ctx2 = assemble.build_context({**FORM, "question_id": "q2", "references": refs})
    ctx8 = assemble.build_context({**FORM, "question_id": "q8", "references": refs})
    assert "사실 0" in ctx2 and "사실 4" not in ctx2
    assert "사실 0" not in ctx8
