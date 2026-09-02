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
    assert set(q2) == {"problem", "customer", "evidence", "solution", "differentiators", "key_stats"}
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


# ── 참고자료 분배 — 문항 역할(각도) 기반, 문항 간 중복 없이 (작업 33) ──

def _refs():
    mk = lambda i, angle, fact: {"fact": fact, "quote": f"q{i}", "url": f"https://a.co/{i}", "date": "2026-01-01",
                                 "publisher": "출처", "angle": angle}
    return [mk(0, "problem", "가구의 42%가 같은 어려움을 겪는다"),
            mk(1, "competitor", "○○ 서비스는 예약 알림만 제공한다"),
            mk(2, "pricing", "유사 서비스 월 이용료는 9천 원 수준"),
            mk(3, "trend", "이 분야 이용자가 3년 연속 늘었다"),
            mk(4, "", "지원자들이 검색으로 정보를 모으는 비율이 높다"),
            mk(5, "competitor", "△△ 플랫폼은 기사 매칭 기능을 운영한다")]


def test_each_question_gets_the_facts_of_its_own_angle():
    refs = _refs()
    q2, q3_1, q3_2 = (assemble.references_for(refs, q) for q in ("q2", "q3_1", "q3_2"))
    assert refs[0] in q2 and refs[1] in q3_1 and refs[5] in q3_1 and refs[2] in q3_2   # 각도 일치 우선
    urls = [r["url"] for part in (q2, q3_1, q3_2) for r in part]
    assert len(urls) == len(set(urls))                                                 # 같은 사실은 한 문항에만
    assert refs[3] in q2 + q3_1 + q3_2 and refs[4] in q2 + q3_1 + q3_2                 # trend·미분류도 어딘가엔 간다


def test_untagged_facts_are_classified_by_hints():
    from backend.rag import allocate as A
    assert A.angle_of({"fact": "유사 서비스 월 구독료는 9천 원", "angle": ""}) == "pricing"
    assert A.angle_of({"fact": "○○ 앱은 알림 기능을 제공한다", "use_for": ""}) == "competitor"
    assert A.angle_of({"fact": "1인 가구 비율 36.6%"}) == "problem"
    assert A.angle_of({"fact": "특별한 단서 없음"}) == "trend"
    assert A.angle_of({"fact": "x", "angle": "PRICING"}) == "pricing"                  # 추출 단계 태그가 우선


def test_allocation_is_stable_and_skips_non_receivers():
    refs = _refs()
    from backend.rag import allocate as A
    assert A.assign(refs) == A.assign(refs)                    # 문항마다 따로 불러도 같은 답
    assert A.for_question(refs, "q8") == [] and A.for_question(refs, "q1") == []
    assert assemble.references_for(refs, "q2", per=0) == refs  # per=0 이면 예전 동작(전부)
    assert assemble.references_for(refs, "") == refs           # 문항 밖(골자·인테이크)에서는 전부


def test_context_only_carries_this_questions_references():
    refs = _refs()
    ctx2 = assemble.build_context({**FORM, "question_id": "q2", "references": refs})
    ctx3 = assemble.build_context({**FORM, "question_id": "q3_1", "references": refs})
    assert "42%" in ctx2 and "○○ 서비스" not in ctx2
    assert "○○ 서비스" in ctx3 and "42%" not in ctx3


def test_plan_mentor_and_capability_questions_get_no_references():
    """WORKORDER_QUALITY 1-7 — Q4-1·Q4-2·Q8 에는 참고자료를 주지 않는다 (기사 요약이 계획을 밀어낸다)."""
    refs = [{"fact": f"사실 {i}", "quote": f"q{i}", "url": f"https://a.co/{i}",
             "date": "2026-01-01", "publisher": "출처"} for i in range(6)]
    for qid in ("q4_1", "q4_2", "q8"):
        assert assemble.references_for(refs, qid) == []
        ctx = assemble.build_context({**FORM, "question_id": qid, "references": refs})
        assert "[웹 참고자료" not in ctx


def test_long_question_without_facts_gets_an_explicit_notice():
    """근거 0건인 긴 문항은 '참고자료 없음' 을 명시해 지어냄을 막는다 (WORKORDER_QUALITY 2-4)."""
    ctx = assemble.build_context({**FORM, "question_id": "q2", "references": []})
    assert "웹 참고자료 없음" in ctx and "기관명" in ctx
    assert "웹 참고자료 없음" not in assemble.build_context({**FORM, "question_id": "q1"})     # 짧은 문항은 원래 안 준다
    assert "웹 참고자료 없음" not in assemble.build_context({**FORM, "question_id": "q8"})     # 미주입 문항도 표시 안 함


def test_outline_carries_the_voice_from_team_input():
    assert O.voice_for({"team": "팀원 없음"}) == "저" and O.voice_for({}) == "저"
    assert O.voice_for({"team": "개발 1명"}) == "저희"
    full = {**O.normalize(RAW), "voice": "저"}
    assert "1인칭: 처음부터 끝까지 '저'" in O.format_outline(full)
    assert O.slice_for(full, "q8")["voice"] == "저"           # 어느 문항이 받아도 같은 값


def test_context_shows_only_this_questions_references():
    refs = [{"fact": f"사실 {i}", "quote": f"q{i}", "url": f"https://a.co/{i}",
             "date": "2026-01-01", "publisher": "출처"} for i in range(6)]
    ctx2 = assemble.build_context({**FORM, "question_id": "q2", "references": refs})
    ctx8 = assemble.build_context({**FORM, "question_id": "q8", "references": refs})
    assert "사실 0" in ctx2 and "사실 4" not in ctx2
    assert "사실 0" not in ctx8


def test_differentiators_with_unsupported_competitor_names_are_dropped():
    """골자가 참고자료에 없는 경쟁사 이름을 지어내 본문이 단정하던 건 (Q3_1_QUALITY 4절)."""
    form = {**FORM, "references": [{"fact": "Notion 은 메모 정리 기능을 제공한다", "url": "u"}]}
    items = ["Notion 과 달리 자동 인식", "ChatGPT 와 달리 직접 답하게 한다", "기존 수작업 대비 사진 한 장으로 끝난다",
             "잡코리아 앱과 달리 시뮬레이션이 있다"]
    kept = O.verified_differentiators(items, form)
    assert "Notion 과 달리 자동 인식" in kept                   # 참고자료에 있는 이름
    assert "기존 수작업 대비 사진 한 장으로 끝난다" in kept      # 이름 없는 범주 비교
    assert not any("ChatGPT" in k or "잡코리아" in k for k in kept)

