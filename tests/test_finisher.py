"""마무리 작업자 (backend/finisher.py) — 모델·네트워크 호출 0. 생성 함수는 가짜로 바꿔 끼운다.

본다: ① 조용한 초안의 빈 문항만 만든다 (생성 안 시작한 초안·활동 중 초안은 건너뜀)
      ② 참고자료는 research 테이블 → 없으면 research_fn 순
      ③ 실패는 max_attempts 까지만 재시도 ④ 사업자면 q7_1 포함 ⑤ 클라이언트 설정: priority 200, 작은 큐
      ⑥ research 저장·조회 (storage.record 의 research/idea_research 경로)"""
import asyncio
from datetime import datetime, timedelta

import pytest

from backend import finisher as F
from backend import storage as S


def _store(tmp_path) -> S.Storage:
    st = S.Storage("sqlite:///" + (tmp_path / "t.sqlite").as_posix())
    st.init()
    return st


def _draft(st: S.Storage, did: str, done=("q1", "q2"), **extra) -> str:
    form = {"draft_id": did, "idea": f"아이디어 {did}", "track": "tech", "team": "팀원 없음", "capability": "개발",
            "answers": [{"slot": "customer", "label": "고객", "answer": ["대학생"], "unknown": False}], **extra}
    st.upsert_draft(form, owner="203.0.113.1")
    for q in done:
        st.add_generation(did, "generate", {**form, "question_id": q, "style": "logic"},
                          {"question_id": q, "style": "logic", "text": "이미 있음 " * 20, "model": "m"})
    return did


class FakeGen:
    def __init__(self, fail_for=()):
        self.calls = []
        self.fail_for = set(fail_for)

    async def __call__(self, client, form):
        self.calls.append(form)
        if form["question_id"] in self.fail_for:
            raise RuntimeError("모델 실패")
        return {"question_id": form["question_id"], "style": form["style"], "text": f"{form['question_id']} 자동 본문 " * 30, "model": "fake"}


def _fin(st, gen, research=None, **over) -> F.Finisher:
    settings = {**F.DEFAULTS, "enabled": True, "idle_seconds": 0, "max_attempts": 2, **over}
    return F.Finisher(st, client=None, settings=settings, generate_fn=gen, research_fn=research)


def test_missing_pairs_follows_question_order_and_business_flag():
    d = {"done": {("q1", "logic"), ("q3_2", "logic")}, "styles": ["logic"], "is_business": False}
    assert [q for q, _ in F.missing_pairs(d)] == ["q2", "q3_1", "q4_1", "q4_2", "q8", "q10"]
    d["is_business"] = True
    assert "q7_1" in [q for q, _ in F.missing_pairs(d)]
    assert F.missing_pairs({"done": {(q, "logic") for q in F.DEFAULT_QUESTIONS}, "styles": ["logic"]}) == []


def test_client_config_lowers_priority_and_uses_small_queue():
    config = {"llm": {"base_url": "http://x/v1", "api_key": "k", "model": "m", "extra": {"chat_template_kwargs": {"enable_thinking": False}, "priority": 100}},
              "models": [{"id": "m", "name": "M"}], "max_workers": 80, "retry": {"attempts": 3}}
    s = F.load_settings({"finisher": {"enabled": True, "priority": 200, "concurrency": 3}})
    c = F.finisher_client_config(config, s)
    assert c["llm"]["extra"] == {"chat_template_kwargs": {"enable_thinking": False}, "priority": 200}
    assert c["max_workers"] == 3 and c["fallback"] is False and c["max_jobs_per_client"] == 0
    assert config["llm"]["extra"]["priority"] == 100                     # 원본은 안 건드린다
    assert F.load_settings({})["enabled"] is False                        # 설정이 없으면 꺼짐


@pytest.mark.asyncio
async def test_tick_fills_only_missing_questions_of_quiet_started_drafts(tmp_path):
    st = _store(tmp_path)
    started = _draft(st, "draft-started-0001", done=("q1", "q2"))
    _draft(st, "draft-cards-only-01", done=())                           # 생성을 시작한 적 없음 → 대상 아님
    gen = FakeGen()
    fin = _fin(st, gen)
    made = await fin.tick()
    assert made == 6 and sorted(f["question_id"] for f in gen.calls) == ["q10", "q3_1", "q3_2", "q4_1", "q4_2", "q8"]
    assert all(f["draft_id"] == started and f["style"] == "logic" and f["answers"] for f in gen.calls)
    row = st.get_draft(started)
    autos = [g for g in row["generations"] if g["meta"] and g["meta"].get("auto")]
    assert len(autos) == 6 and {g["question_id"] for g in autos} == {"q3_1", "q3_2", "q4_1", "q4_2", "q8", "q10"}
    assert st.get_draft("draft-cards-only-01")["generations"] == []
    # 다 채웠으니 다음 주기엔 할 일이 없다
    assert await fin.tick() == 0 and len(gen.calls) == 6
    # updated_at 은 안 건드렸다 (record 대신 add_generation 직접)
    assert row["request_count"] == 1
    st.close()


@pytest.mark.asyncio
async def test_active_drafts_are_left_alone(tmp_path):
    st = _store(tmp_path)
    _draft(st, "draft-active-00001", done=("q1",))
    fin = _fin(st, FakeGen(), idle_seconds=3600)                         # 방금 요청이 있었다 → 아직 활동 중
    assert await fin.tick() == 0
    assert st.list_unfinished(idle_seconds=0) and not st.list_unfinished(idle_seconds=3600)
    old = datetime.now() - timedelta(days=10)
    assert st.list_unfinished(idle_seconds=0, lookback_days=3, now=old + timedelta(days=5)) == []   # 너무 오래된 것도 제외
    st.close()


@pytest.mark.asyncio
async def test_references_come_from_research_table_then_research_fn(tmp_path):
    st = _store(tmp_path)
    did = _draft(st, "draft-refs-000001", done=tuple(q for q in F.DEFAULT_QUESTIONS if q not in ("q3_2", "q8")))
    facts = [{"fact": "국내 전기차 등록 50만 대", "url": "https://kosis.kr/x", "quote": "50만"}]
    st.add_research(did, "q3_2", {"facts": facts, "queries": ["전기차 등록"], "pages": [{"url": "https://kosis.kr/x", "title": "통계"}], "backend": "ddgs"})
    assert st.get_research(did, "q3_2") == facts and st.get_research(did, "q8") is None
    asked = []

    async def research_fn(form, qid):
        asked.append(qid)
        return [{"fact": "새 조사", "url": "https://a"}]

    gen = FakeGen()
    fin = _fin(st, gen, research=research_fn)
    assert await fin.tick() == 2
    by_q = {f["question_id"]: f for f in gen.calls}
    assert by_q["q3_2"]["references"] == facts and asked == ["q8"] and by_q["q8"]["references"][0]["fact"] == "새 조사"
    metas = {g["question_id"]: g["meta"] for g in st.get_draft(did)["generations"] if g["meta"] and g["meta"].get("auto")}
    assert metas["q3_2"]["references_used"] == 1
    st.close()


@pytest.mark.asyncio
async def test_failures_retry_up_to_max_attempts(tmp_path):
    st = _store(tmp_path)
    _draft(st, "draft-fail-0000001", done=tuple(q for q in F.DEFAULT_QUESTIONS if q != "q10"))
    gen = FakeGen(fail_for={"q10"})
    fin = _fin(st, gen, max_attempts=2)
    assert await fin.tick() == 0 and await fin.tick() == 0 and await fin.tick() == 0
    assert len(gen.calls) == 2 and fin.stats["failed"] == 2 and fin.stats["skipped"] >= 1
    assert fin.in_progress == set()
    st.close()


@pytest.mark.asyncio
async def test_business_draft_gets_q7_1_and_uses_saved_style(tmp_path):
    st = _store(tmp_path)
    did = "draft-biz-00000001"
    form = {"draft_id": did, "idea": "기존 카페의 구독 서비스", "track": "tech", "is_business": True, "current_item": "카페"}
    st.upsert_draft(form)
    for q in F.DEFAULT_QUESTIONS:
        st.add_generation(did, "generate", {**form, "question_id": q, "style": "story"}, {"question_id": q, "style": "story", "text": "x" * 50})
    gen = FakeGen()
    assert await _fin(st, gen).tick() == 1
    assert gen.calls[0]["question_id"] == "q7_1" and gen.calls[0]["style"] == "story" and gen.calls[0]["is_business"] is True
    st.close()


def test_record_stores_research_rows(tmp_path):
    st = _store(tmp_path)
    form = {"draft_id": "draft-rec-00000001", "idea": "조사 저장", "track": "tech", "question_id": "q2"}
    asyncio.run(st.record("research", form, {"facts": [{"fact": "a", "url": "u"}], "queries": ["q"], "pages": [], "cached": True}))
    asyncio.run(st.record("research", form, {"facts": [], "error": "검색 실패"}))                    # 실패는 안 남김
    asyncio.run(st.record("idea_research", {**form, "question_id": None}, {"facts": [{"fact": "b", "url": "v"}], "queries": []}))
    assert st.get_research("draft-rec-00000001", "q2") == [{"fact": "a", "url": "u"}]
    assert st.get_research("draft-rec-00000001", S.IDEA_QUESTION) == [{"fact": "b", "url": "v"}]
    assert st.get_research("draft-rec-00000001", "q3_1") is None
    st.close()
