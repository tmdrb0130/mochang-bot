"""2026-09-04 보강 — 접근 통제·상한·신선도·가시화. 모델·네트워크 호출 0 (전부 가짜).

  ① storage: 초안 접근 열쇠(owner_token) — 발급·불일치 거부·옛 행 IP 규칙·형식 밖 draft_id 미저장·저장 오류 카운터
  ② main: 신뢰 프록시의 XFF 마지막 항목 · 초안 단위 동시 제한(같은 IP 여러 초안) · IP 천장 · 인테이크 시간당 상한 ·
          번역 초안당/전역 상한 · 동기 엔드포인트 404 · /health 의 storage·llm_reachable
  ③ jobs: ip·kind 상한
  ④ vectorstore: 신선도 조건 · Ollama 죽음 → 색인·조회 건너뜀 · embed_model 메타 · 오프라인 임베딩은 테스트 전용
  ⑤ allocate: q7_1 은 의도적으로 근거 미배분
  ⑥ scripts/db_snapshot: 스냅샷·보관 정리
  ⑦ full_stack: 운영 구성(polish+outline+refine ON)으로 문항 8개 전체를 가짜 모델로 한 바퀴
"""
from __future__ import annotations

import asyncio
import gzip
import re
import sqlite3
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest

from backend import storage as S
from backend.llm.client import LLMResult
from backend.llm.jobs import JobQueue, TooManyJobs


def _mk(tmp_path, name="t") -> S.Storage:
    st = S.Storage("sqlite:///" + (tmp_path / f"{name}.sqlite").as_posix())
    st.init()
    return st


# ────────────────────────── ① storage: 접근 열쇠 ──────────────────────────

def test_owner_token_issued_on_insert_and_required_for_update(tmp_path):
    st = _mk(tmp_path)
    form = {"draft_id": "draft-key-000001", "idea": "열쇠 테스트", "track": "tech"}
    info = st.upsert(form, owner="203.0.113.1")
    key = info["draft_key"]
    assert info["draft_id"] == "draft-key-000001" and S.OWNER_TOKEN_RE.match(key)
    # 열쇠가 맞으면 갱신 (다른 IP 여도 — 다른 기기 이어 쓰기)
    assert st.upsert({**form, "capability": "개발", "draft_key": key}, owner="198.51.100.9")["draft_key"] == key
    assert st.get_draft("draft-key-000001")["capability"] == "개발"
    # 열쇠가 틀리거나 없으면 갱신 거부 → None, 내용 그대로
    assert st.upsert({**form, "capability": "해킹", "draft_key": "wrong-000000000000000000"}, owner="203.0.113.1") is None
    assert st.upsert({**form, "capability": "해킹"}, owner="203.0.113.1") is None
    assert st.get_draft("draft-key-000001")["capability"] == "개발"
    # 거부된 요청의 생성문도 남지 않는다
    assert asyncio.run(st.record("generate", {**form, "question_id": "q1", "style": "logic"},
                                 {"question_id": "q1", "text": "몰래"}, "203.0.113.1")) is None
    assert st.get_draft("draft-key-000001")["generations"] == []
    # 응답에는 열쇠가 안 실린다 (main 이 접근 확인 뒤에만 draft_key 로 붙인다)
    assert "owner_token" not in st.get_draft("draft-key-000001")
    assert st.check_access("draft-key-000001", key, "1.2.3.4") is True
    assert st.check_access("draft-key-000001", None, "203.0.113.1") is False
    assert st.check_access("draft-key-000001", "wrong-000000000000000000", "203.0.113.1") is False
    assert st.check_access("no-such-000000001", key, "1.2.3.4") is False
    st.close()


def test_legacy_row_without_token_opens_for_same_ip_and_gets_upgraded(tmp_path):
    """열쇠 열이 생기기 전의 초안: 같은 IP 면 갱신되고 그때 열쇠가 채워진다. 다른 IP 는 거부."""
    st = _mk(tmp_path)
    form = {"draft_id": "draft-old-000001", "idea": "옛 초안", "track": "tech"}
    st.upsert(form, owner="203.0.113.5")
    with st.engine.begin() as conn:                                  # 옛 행처럼 열쇠를 지운다
        conn.execute(S.drafts.update().values(owner_token=None))
    assert st.check_access("draft-old-000001", None, "198.51.100.1") is False
    assert st.check_access("draft-old-000001", None, "203.0.113.5") is True
    assert st.upsert({**form, "capability": "x"}, owner="198.51.100.1") is None        # 다른 IP → 거부
    info = st.upsert({**form, "capability": "y"}, owner="203.0.113.5")                 # 같은 IP → 갱신 + 열쇠 발급
    assert info and S.OWNER_TOKEN_RE.match(info["draft_key"])
    assert st.owner_key("draft-old-000001") == info["draft_key"]
    # 열쇠가 생긴 뒤로는 열쇠가 기준이다 — 같은 IP 라도 열쇠 없이는 못 연다
    assert st.check_access("draft-old-000001", None, "203.0.113.5") is False
    assert st.check_access("draft-old-000001", info["draft_key"], "0.0.0.0") is True
    st.close()


def test_owner_key_fills_legacy_row_and_mirrors_backup(tmp_path):
    bak = S.Storage("sqlite:///" + (tmp_path / "bak.sqlite").as_posix())
    st = S.Storage("sqlite:///" + (tmp_path / "svc.sqlite").as_posix(), backup=bak)
    st.init()
    info = st.upsert({"draft_id": "draft-mirror-0001", "idea": "미러", "track": "tech"}, owner="1.1.1.1")
    with bak.engine.connect() as conn:
        assert conn.execute(S.select(S.drafts.c.owner_token)).scalar() == info["draft_key"]      # 백업에도 같은 열쇠
    for eng in (st.engine, bak.engine):
        with eng.begin() as conn:
            conn.execute(S.drafts.update().values(owner_token=None))
    key = st.owner_key("draft-mirror-0001")
    assert key and st.owner_key("draft-mirror-0001") == key                                       # 두 번 불러도 같은 값
    with bak.engine.connect() as conn:
        assert conn.execute(S.select(S.drafts.c.owner_token)).scalar() == key
    assert st.owner_key("no-such-0000001") is None
    st.close()


def test_internal_calls_without_owner_are_trusted(tmp_path):
    """마무리 작업자의 조사 저장처럼 요청이 아닌 내부 호출(owner=None)은 열쇠 없이 통과한다 — main 은 항상 IP 를 넘긴다."""
    st = _mk(tmp_path)
    form = {"draft_id": "draft-int-000001", "idea": "내부", "track": "tech", "question_id": "q2"}
    st.upsert(form, owner="203.0.113.1")
    asyncio.run(st.record("research", form, {"facts": [{"fact": "a"}], "queries": []}, None))
    assert st.get_research("draft-int-000001", "q2") == [{"fact": "a"}]
    st.close()


def test_invalid_draft_id_is_not_stored_at_all(tmp_path):
    st = _mk(tmp_path)
    assert st.upsert({"draft_id": "bad id!", "idea": "x", "track": "tech"}) is None
    assert st.upsert({"idea": "x", "track": "tech"}) is None
    assert st.list_drafts() == []
    st.close()


def test_record_counts_swallowed_errors(tmp_path, monkeypatch):
    st = _mk(tmp_path)
    logged = []
    from backend import timing
    monkeypatch.setattr(timing, "log", lambda event, **f: logged.append((event, f)))

    def boom(*a, **k):
        raise RuntimeError("database is locked")
    monkeypatch.setattr(st, "_record_sync", boom)
    assert asyncio.run(st.record("generate", {"idea": "x", "draft_id": "draft-err-0000001"}, {"text": "t"}, "1.1.1.1")) is None
    assert st.error_count == 1 and "locked" in st.last_error
    assert logged and logged[0][0] == "storage_error" and logged[0][1]["kind"] == "generate"
    st.close()


def test_schema_v4_db_gains_owner_token_column(tmp_path):
    path = tmp_path / "v4.sqlite"
    with sqlite3.connect(path) as c:
        c.execute("create table drafts (draft_id varchar(64) primary key, idea text not null default '', track varchar(16), is_business boolean, "
                  "current_item text, team text, capability text, answers text, owner varchar(64), model varchar(160), request_count integer, "
                  "is_test boolean not null default 0, share_token varchar(64), created_at datetime not null, updated_at datetime not null)")
        c.execute("insert into drafts (draft_id, idea, owner, created_at, updated_at) values ('old-000000000005', '옛', '9.9.9.9', '2026-09-01', '2026-09-01')")
    st = S.Storage("sqlite:///" + path.as_posix())
    st.init()
    assert st.check_access("old-000000000005", None, "9.9.9.9") is True and st.check_access("old-000000000005", None, "8.8.8.8") is False
    st.close()


# ────────────────────────── ③ jobs: ip·kind 상한 ──────────────────────────

class _Gate:
    def __init__(self):
        self.ev = asyncio.Event()

    def make(self, v="x"):
        async def run():
            await self.ev.wait()
            return v
        return run


@pytest.mark.asyncio
async def test_queue_limits_by_ip_and_kind_independently():
    q = JobQueue(max_workers=8, max_per_client=2)
    await q.start()
    g = _Gate()
    try:
        # 초안(owner)이 달라도 같은 IP 는 max_per_ip 까지만
        a = q.submit(g.make(), kind="generate", owner="d:one", ip="10.0.0.1", max_per_ip=3)
        b = q.submit(g.make(), kind="generate", owner="d:two", ip="10.0.0.1", max_per_ip=3)
        c = q.submit(g.make(), kind="generate", owner="d:three", ip="10.0.0.1", max_per_ip=3)
        with pytest.raises(TooManyJobs) as e:
            q.submit(g.make(), kind="generate", owner="d:four", ip="10.0.0.1", max_per_ip=3)
        assert e.value.scope == "ip" and "네트워크" in str(e.value)
        assert q.active_ip("10.0.0.1") == 3 and q.active_for("d:one") == 1
        # 다른 IP 는 통과, kind 전역 상한은 IP·초안과 무관하게 센다
        q.submit(g.make(), kind="translate", owner="d:t1", ip="10.0.0.2", max_per_owner=10, max_per_kind=2)
        q.submit(g.make(), kind="translate", owner="d:t2", ip="10.0.0.3", max_per_owner=10, max_per_kind=2)
        with pytest.raises(TooManyJobs) as e2:
            q.submit(g.make(), kind="translate", owner="d:t3", ip="10.0.0.4", max_per_owner=10, max_per_kind=2)
        assert e2.value.scope == "kind" and q.active_kind("translate") == 2
        # max_per_owner 가 큐 기본값(2)을 덮는다
        q.submit(g.make(), kind="translate", owner="d:t1", ip="10.0.0.2", max_per_owner=10, max_per_kind=0)
        assert q.active_for("d:t1") == 2
        g.ev.set()
        await asyncio.gather(a.future, b.future, c.future)
    finally:
        g.ev.set()
        await q.stop()


# ────────────────────────── ② main: IP 식별·상한·게이트 ──────────────────────────

def _req(peer, xff=None):
    headers = {"x-forwarded-for": xff} if xff else {}
    return SimpleNamespace(client=SimpleNamespace(host=peer), headers=headers)


def test_client_key_trusts_last_xff_only_behind_trusted_proxy():
    from backend import main as M
    # nginx(127.0.0.1)는 실제 접속 IP 를 XFF 맨 뒤에 덧붙인다 → 마지막 항목. 클라이언트가 앞에 붙인 가짜는 무시된다
    assert M._client_key(_req("127.0.0.1", "1.1.1.1, 2.2.2.2")) == "2.2.2.2"
    assert M._client_key(_req("127.0.0.1", "203.0.113.9")) == "203.0.113.9"
    assert M._client_key(_req("127.0.0.1")) == "127.0.0.1"
    # 프록시를 거치지 않은 요청은 헤더를 믿지 않는다
    assert M._client_key(_req("198.51.100.7", "1.1.1.1")) == "198.51.100.7"


def test_job_owner_is_draft_then_ip():
    from backend import main as M
    assert M._job_owner({"draft_id": "0b1c9f2e-9d1a-4f42-8c1e-1234567890ab"}, "1.1.1.1") == "d:0b1c9f2e-9d1a-4f42-8c1e-1234567890ab"
    assert M._job_owner({"draft_id": "bad id"}, "1.1.1.1") == "ip:1.1.1.1"
    assert M._job_owner({}, "1.1.1.1") == "ip:1.1.1.1"


def test_intake_rate_limit_per_ip_hour(monkeypatch):
    from backend import main as M
    monkeypatch.setattr(M, "MAX_INTAKES_PER_IP_HOUR", 2)
    M._intake_times.clear()
    M._check_intake_rate("10.0.0.9", now=1000.0)
    M._check_intake_rate("10.0.0.9", now=1001.0)
    with pytest.raises(TooManyJobs):
        M._check_intake_rate("10.0.0.9", now=1002.0)
    M._check_intake_rate("10.0.0.8", now=1002.0)                      # 다른 IP 는 무관
    M._check_intake_rate("10.0.0.9", now=1000.0 + 3601)               # 한 시간 지나면 다시 열린다
    monkeypatch.setattr(M, "MAX_INTAKES_PER_IP_HOUR", 0)
    for _ in range(5):
        M._check_intake_rate("10.0.0.9", now=2000.0)                  # 0 = 끔


@pytest.mark.asyncio
async def test_same_ip_many_drafts_pass_but_ip_ceiling_and_translate_limits_hold(monkeypatch):
    """강의실: 같은 IP 의 초안 둘이 각각 3건씩 → 전부 수락 (예전엔 IP 당 3건). IP 천장·번역 상한은 429."""
    import httpx
    from backend import main as M

    gate = asyncio.Event()

    async def slow_complete(system, user, model, extra=None):
        await gate.wait()
        return LLMResult(text="가짜 본문입니다.", model=model)

    monkeypatch.setattr(M.client, "_complete", slow_complete)
    monkeypatch.setattr(M.research_client, "_complete", slow_complete)
    monkeypatch.setattr(M, "MAX_JOBS_PER_IP", 7)
    monkeypatch.setattr(M, "TRANSLATE_MAX_PER_CLIENT", 2)
    monkeypatch.setattr(M, "TRANSLATE_GLOBAL_LIMIT", 3)
    head = {"X-Forwarded-For": "10.77.0.1", "X-Mochang-Test": "1"}
    body = lambda did, q: {"question_id": q, "style": "logic", "track": "tech", "idea": "강의실 아이디어", "draft_id": did}
    try:
        async with M.lifespan(M.app):
            transport = httpx.ASGITransport(app=M.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=30) as c:
                codes = []
                for did in ("draft-room-000001", "draft-room-000002"):
                    for q in ("q1", "q2", "q3_1"):
                        codes.append((await c.post("/jobs/generate", json=body(did, q), headers=head)).status_code)
                assert codes == [200] * 6                                                    # 초안 단위 3건씩
                r = await c.post("/jobs/generate", json=body("draft-room-000001", "q4_1"), headers=head)
                assert r.status_code == 429 and "동시에 진행할 수 있는 작업은 3건" in r.json()["detail"]   # 초안 4번째
                r = await c.post("/jobs/generate", json=body("draft-room-000003", "q1"), headers=head)
                assert r.status_code == 200                                                  # 7번째 = IP 천장 안
                r = await c.post("/jobs/generate", json=body("draft-room-000004", "q1"), headers=head)
                assert r.status_code == 429 and "네트워크" in r.json()["detail"]             # 8번째 = IP 천장
                # 번역: 초안당 2건, 전역 3건 (조사 큐 — 생성 IP 천장은 큐별이라 무관)
                th = {"X-Forwarded-For": "10.77.0.2"}
                t1 = await c.post("/jobs/translate", json={"lang": "en", "texts": ["가"]}, headers=th)
                t2 = await c.post("/jobs/translate", json={"lang": "en", "texts": ["나"]}, headers=th)
                t3 = await c.post("/jobs/translate", json={"lang": "en", "texts": ["다"]}, headers=th)
                assert [t1.status_code, t2.status_code, t3.status_code] == [200, 200, 429]
                t4 = await c.post("/jobs/translate", json={"lang": "en", "texts": ["라"]}, headers={"X-Forwarded-For": "10.77.0.3"})
                t5 = await c.post("/jobs/translate", json={"lang": "en", "texts": ["마"]}, headers={"X-Forwarded-For": "10.77.0.4"})
                assert t4.status_code == 200 and t5.status_code == 429 and "서버 전체" in t5.json()["detail"]
                stats = (await c.get("/jobs", headers=head)).json()
                assert stats["your_active"] == 7
                gate.set()
                for _ in range(300):
                    await asyncio.sleep(0.02)
                    if M.client.queue.stats()["running"] == 0 and M.client.queue.stats()["queued"] == 0:
                        break
    finally:
        gate.set()


@pytest.mark.asyncio
async def test_sync_endpoints_are_closed_unless_enabled(monkeypatch):
    import httpx
    from backend import main as M

    async def fake_complete(system, user, model, extra=None):
        return LLMResult(text="Hello", model=model)

    monkeypatch.setattr(M.client, "_complete", fake_complete)
    monkeypatch.setattr(M.research_client, "_complete", fake_complete)
    monkeypatch.setattr(M, "SYNC_ENDPOINTS", False)
    body = {"question_id": "q1", "track": "tech", "idea": "게이트 아이디어", "draft_id": "draft-gate-000001"}
    async with M.lifespan(M.app):
        transport = httpx.ASGITransport(app=M.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=30) as c:
            for path, b in (("/generate", body), ("/extend", {**body, "current": "x"}), ("/verify", {**body, "text": "x"}),
                            ("/intake", body), ("/research", body), ("/translate", {"lang": "en", "texts": ["가"]}),
                            ("/intake/regenerate", {**body, "slots": ["problem"]})):
                assert (await c.post(path, json=b)).status_code == 404, path
            assert (await c.post("/generate/dry-run", json=body)).status_code == 200        # 모델 호출 없음 — 열어 둔다
            r = await c.post("/jobs/generate", json=body, headers={"X-Mochang-Test": "1"})
            assert r.status_code == 200                                                    # 큐 경로는 그대로
            monkeypatch.setattr(M, "SYNC_ENDPOINTS", True)
            assert (await c.post("/translate", json={"lang": "en", "texts": ["가"]})).status_code == 200


@pytest.mark.asyncio
async def test_health_reports_storage_and_llm_reachability(monkeypatch):
    import httpx
    from backend import main as M

    async def unreachable():
        return False
    monkeypatch.setattr(M, "_llm_reachable", unreachable)
    async with M.lifespan(M.app):
        M.storage.error_count = 3
        M.storage.last_error = "generate: locked"
        transport = httpx.ASGITransport(app=M.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=30) as c:
            h = (await c.get("/health")).json()
            assert h["llm_reachable"] is False
            assert h["storage"]["enabled"] is True and h["storage"]["error_count"] == 3 and "locked" in h["storage"]["last_error"]
        M.storage.error_count = 0
        M.storage.last_error = None


def test_generate_request_defaults_to_logic_style():
    from backend import main as M
    assert M.GenerateRequest(question_id="q1", idea="x").style == "logic"
    assert M.GenerateRequest(question_id="q1", idea="x", draft_key="k").draft_key == "k"


# ────────────────────────── ④ vectorstore ──────────────────────────

def test_is_sufficient_requires_a_fresh_hit_when_freshness_days_set(tmp_path):
    from backend.rag.vectorstore import VectorStore
    old = (date.today() - timedelta(days=400)).isoformat()
    fresh = (date.today() - timedelta(days=10)).isoformat()
    store = VectorStore(str(tmp_path / "vs"), min_score=0.5, min_hits=2, freshness_days=90)
    assert store.is_sufficient([{"score": 0.9, "date": old}, {"score": 0.9, "date": old}]) is False       # 전부 낡음
    assert store.is_sufficient([{"score": 0.9, "date": old}, {"score": 0.9, "date": fresh}]) is True
    assert store.is_sufficient([{"score": 0.9, "date": None}, {"score": 0.9}]) is False                    # 날짜 모름 = 신선하지 않음
    assert store.is_sufficient([{"score": 0.9, "date": fresh}]) is False                                  # 건수 미달
    loose = VectorStore(str(tmp_path / "vs2"), min_score=0.5, min_hits=2, freshness_days=0)
    assert loose.is_sufficient([{"score": 0.9, "date": old}, {"score": 0.9, "date": old}]) is True        # 0 = 끔 (예전 동작)


def test_degraded_store_skips_index_and_query_until_embedding_server_returns(tmp_path, monkeypatch):
    from backend.rag import vectorstore as V
    alive = {"ok": False}
    logged = []
    from backend import timing
    monkeypatch.setattr(timing, "log", lambda event, **f: logged.append(event))
    store = V.VectorStore(str(tmp_path / "vs"), health_check=lambda: alive["ok"])
    page = {"url": "https://a.kr/1", "title": "t", "text": "긴 본문 " * 30, "source_type": "news"}
    assert store.upsert_pages([page]) == 0 and store.degraded is True
    assert store.query("긴 본문") == []
    assert "vectorstore_degraded" in logged
    alive["ok"] = True
    store._health_at = 0.0                                             # 재확인 주기를 강제로 지나게
    assert store.healthy() is True and store.degraded is False
    assert store.upsert_pages([page]) == 1
    assert store.query("긴 본문")[0]["url"] == "https://a.kr/1"
    # 색인 메타에 임베딩 모델명이 남는다 (오염 문서 식별용)
    meta = next(iter(store._index.ref_doc_info.values())).metadata
    assert meta["embed_model"] == "OfflineEmbedding"


def test_offline_embedding_is_test_only(tmp_path, monkeypatch):
    from backend.rag import pipeline as P
    monkeypatch.setattr(P, "_vector_store", None)
    cfg = P.ResearchConfig(vectorstore_enabled=True, vectorstore_embed_model="", vectorstore_dir=str(tmp_path / "vs"))
    monkeypatch.delenv("MOCHANG_ALLOW_OFFLINE_EMBEDDING", raising=False)
    assert P.get_vector_store(cfg) is None                              # 운영: 임베딩 모델 없음 → 벡터DB 끔
    monkeypatch.setenv("MOCHANG_ALLOW_OFFLINE_EMBEDDING", "1")
    monkeypatch.setattr(P, "_vector_store", None)
    assert P.get_vector_store(cfg) is not None                          # 테스트: 오프라인 임베딩 허용
    monkeypatch.setattr(P, "_vector_store", None)


@pytest.mark.asyncio
async def test_research_semaphores_are_per_loop_and_optional():
    from backend.rag import pipeline as P
    assert P._sem("x", 0) is None
    a = P._sem("research_llm", 3)
    assert a is P._sem("research_llm", 3) and a._value == 3
    assert P._sem("research_llm", 4) is not a


def test_runtime_limits_apply_to_extract_pool(monkeypatch):
    from backend.rag import extract_proc as X, pipeline as P
    monkeypatch.setattr(X, "POOL_SIZE", 3)
    monkeypatch.setattr(P, "EXTRACT_GLOBAL", 8)
    cfg = P.ResearchConfig.from_config({"research": {"extract_concurrency_global": 5, "extract_pool_size": 6, "llm_concurrency": 7}})
    assert cfg.llm_concurrency == 7 and P.EXTRACT_GLOBAL == 5 and X.POOL_SIZE == 6


# ────────────────────────── ⑤ allocate ──────────────────────────

def test_q7_1_is_intentionally_unallocated():
    from backend.rag import allocate as A
    facts = [{"fact": "문제 통계", "angle": "problem"}]
    assert "q7_1" not in A.QUESTION_ANGLES and A.for_question(facts, "q7_1") == []
    assert A.for_question(facts, "q2") == facts


# ────────────────────────── ⑥ db_snapshot ──────────────────────────

def test_db_snapshot_creates_gz_and_prunes_old(tmp_path):
    from scripts import db_snapshot as D
    src = tmp_path / "svc.sqlite"
    with sqlite3.connect(src) as c:
        c.execute("create table drafts (draft_id text primary key)")
        c.execute("insert into drafts values ('a')")
    out = tmp_path / "snap"
    gz = D.snapshot(src, out, stamp="2026-09-04")
    assert gz.name == "mochang-2026-09-04.sqlite.gz" and gz.exists()
    with gzip.open(gz, "rb") as f:
        raw = tmp_path / "restored.sqlite"
        raw.write_bytes(f.read())
    with sqlite3.connect(raw) as c:
        assert c.execute("select count(*) from drafts").fetchone()[0] == 1
    (out / "mochang-2026-08-01.sqlite.gz").write_bytes(b"x")
    (out / "other.txt").write_bytes(b"x")
    gone = D.prune(out, keep_days=7, now=datetime(2026, 9, 5))
    assert [p.name for p in gone] == ["mochang-2026-08-01.sqlite.gz"]
    assert gz.exists() and (out / "other.txt").exists()


# ────────────────────────── ⑦ full_stack ──────────────────────────

_WHO = ("혼자 사는 직장인은", "맞벌이 부부는", "소규모 농가는", "동네 세탁소 사장님은", "경력 단절 여성은", "독거 고령자는", "지역 소상공인은")
_DO = ("장을 본 뒤 남은 재료를 어디에 두었는지 잊어버려", "새벽에 물건을 옮길 사람을 구하지 못해", "수확한 물건을 제때 팔 곳을 찾지 못해",
       "아이를 잠깐 맡길 이웃을 찾지 못해", "약을 제때 챙겨 먹었는지 확인해 줄 사람이 없어", "손님이 몰리는 시간에 일손이 모자라",
       "믿을 만한 정보를 어디서 얻어야 할지 몰라")
_SO = ("같은 물건을 다시 사는 일이 잦다고 털어놓았습니다.", "결국 일을 포기하는 경우가 많다고 했습니다.", "매번 비싼 대안에 기대게 된다고 말했습니다.",
       "주말마다 같은 고민을 되풀이한다고 적었습니다.", "가족에게 부담을 떠넘기게 된다고 이야기했습니다.", "한 달에 며칠은 일을 쉬게 된다고 답했습니다.",
       "그래서 저는 이 흐름을 바꾸는 서비스를 준비하고 있습니다.")


def _long_text(n_sentences=40, seed=0):
    out = []
    for i in range(n_sentences):
        k = i + seed
        # 세 조각의 색인을 서로 독립으로 — 같은 주어끼리도 나머지 두 조각이 달라 유사도(4-gram)가 낮게 나온다
        out.append(f"{_WHO[k % 7]} {_DO[(k // 7) % 7]} {_SO[((k // 7) * 3 + k) % 7]}")
        if i % 6 == 5:
            out.append("")
    return "\n".join(out).replace("\n\n\n", "\n\n").strip()


SHORT = "수확한 딸기를 농가별로 모아 당도와 크기로 나누고 예약한 소비자에게 이틀 안에 포장해 보내는 공동 선별 직거래 센터"
OUTLINE = ('{"customer": "혼자 사는 직장인", "problem": "남은 재료를 잊어 다시 사는 문제", "evidence": "검증 계획: 이웃 열 명에게 인터뷰",'
           ' "solution": "냉장고 재료를 기록하고 소진 순서를 알려 주는 앱", "differentiators": ["기록을 대신해 준다"], "revenue": "미정",'
           ' "plan_6m": ["MVP 기획", "제작", "고객 검증"], "capability": "요리 경험", "gaps": ["가격"], "key_stats": []}')


class FullStackClient:
    """system 프롬프트의 절 이름으로 어떤 호출인지 알아보고 그에 맞는 가짜 답을 낸다."""

    def __init__(self):
        self.calls = []
        self.model_ids = ["fake"]

    async def complete(self, system, user, model=None, extra=None):
        self.calls.append(system[:40])
        if "사업계획의 골자" in system:
            return LLMResult(text=OUTLINE, model="fake")
        if "[문장 고쳐 쓰기]" in system:
            n = system.count("\n1.") + 1
            return LLMResult(text="\n".join(f"{i}. 저는 이 부분을 확인한 뒤 계획으로 적겠습니다." for i in range(1, n + 8)), model="fake")
        if "[이어쓰기 지시]" in system:
            return LLMResult(text=_long_text(8, seed=100), model="fake")
        if "[줄여 쓰기 지시]" in system:
            m = re.search(r"(\d+)자 한도를 넘겼습니다", system)          # 짧은 문항(100)이면 한 문장, 긴 문항이면 줄인 본문
            return LLMResult(text=SHORT[:80] if m and int(m.group(1)) <= 100 else _long_text(22, seed=50), model="fake")
        if "[늘려 쓰기 지시]" in system:
            return LLMResult(text=SHORT, model="fake")
        if "[짧은 문항 규칙]" in system:
            return LLMResult(text=SHORT, model="fake")
        return LLMResult(text=_long_text(30), model="fake")


@pytest.mark.full_stack
@pytest.mark.outline
@pytest.mark.polish
@pytest.mark.refine
@pytest.mark.asyncio
async def test_full_stack_generates_all_questions_with_production_switches(tmp_path, monkeypatch):
    """운영 스위치(골자·polish·refine·자동 이어쓰기) 전부 켠 채 문항 8개를 한 바퀴 — 어느 단계도 서로를 깨지 않는다."""
    from backend.pipeline import generate, outline
    from backend.rag.research import DiskCache
    monkeypatch.setattr(outline, "DiskCache", lambda *a, **k: DiskCache(directory=tmp_path / "cache", ttl=60))
    client = FullStackClient()
    form = {"track": "tech", "style": "logic", "idea": "냉장고에 남은 재료를 기록하고 소진 순서를 알려 주는 앱",
            "team": "팀원 없음", "capability": "요리 경험", "is_business": False, "draft_id": "draft-full-000001",
            "answers": [{"slot": "evidence", "label": "직접 확인한 근거", "answer": None, "unknown": True}],
            "outline_settings": {"enabled": True, "cache_ttl_seconds": 60}, "polish_settings": {"enabled": True},
            "refine_settings": {"enabled": True}, "auto_extend": {"enabled": True, "min_ratio": 0.7, "min_limit": 500}}
    results = {}
    for qid in ("q1", "q2", "q3_1", "q3_2", "q4_1", "q4_2", "q8", "q10"):
        results[qid] = await generate.generate_one(client, {**form, "question_id": qid})
    for qid, r in results.items():
        assert r["text"].strip(), qid
        assert r["length"] == len(r["text"]) <= r["limit"], (qid, r["length"], r["limit"])
        assert r["outline_used"] is True and isinstance(r["refined"], dict) and isinstance(r["polished"], dict), qid
        assert "저희" not in r["text"], qid                       # 팀원 없음 → 1인칭 통일
    assert len(results["q1"]["text"]) <= 100 and len(results["q10"]["text"]) <= 100
    assert results["q2"]["length"] >= 700                           # 긴 문항은 하한 근처까지 채워진다
    assert sum(1 for c in client.calls if "사업계획의 골자" in c) == 1   # 골자는 아이디어당 1회(캐시)
