"""아이디어·초안 저장소 (backend/storage.py) — 모델·네트워크 호출 0.

본다: ① draft_id 결정 규칙 ② 초안 upsert + 문항 생성 이력 ③ 저장 실패가 결과를 막지 않음
      ④ /jobs/generate → DB → GET /drafts/{id} 한 바퀴 (기존 응답 형태는 그대로)"""
import asyncio

import pytest

from backend import storage as S


def _mk(tmp_path, **kw) -> S.Storage:
    st = S.Storage("sqlite:///" + (tmp_path / "t.sqlite").as_posix(), **kw)
    st.init()
    return st


def test_draft_id_uses_frontend_uuid_or_none():
    assert S.draft_id_for({"draft_id": "0b1c9f2e-9d1a-4f42-8c1e-1234567890ab", "idea": "x"}) == "0b1c9f2e-9d1a-4f42-8c1e-1234567890ab"
    # 형식 밖 값(짧음·공백·따옴표)·없음 → None. 2026-09-04 이전의 sha1(track|idea) 대체는 아이디어로 키가 계산되는 구멍이라 없앴다.
    assert S.draft_id_for({"draft_id": "bad id!", "track": "tech", "idea": "  캠핑장 빈자리 알림  "}) is None
    assert S.draft_id_for({"track": "tech", "idea": "캠핑장 빈자리 알림"}) is None


def test_upsert_draft_then_generations_roundtrip(tmp_path):
    st = _mk(tmp_path)
    form = {"draft_id": "draft-000000001", "idea": "세탁소 픽업 앱", "track": "tech", "is_business": False,
            "team": "팀원 없음", "capability": "", "model": "m1"}
    did = st.upsert_draft(form, owner="203.0.113.7")
    assert did == "draft-000000001"
    # 두 번째 요청: 인테이크 답이 붙어 들어옴 → 같은 행이 갱신되고 request_count 가 는다
    form2 = {**form, "answers": [{"slot": "customer", "label": "고객", "answer": "1인 가구"}], "capability": "개발"}
    assert st.upsert_draft(form2) == did
    st.add_generation(did, "generate", {**form2, "question_id": "q1", "style": "logic"},
                      {"question_id": "q1", "style": "logic", "text": "한 줄 소개", "model": "m1", "chars": 6})
    st.add_generation(did, "extend", {**form2, "question_id": "q2"},
                      {"question_id": "q2", "style": "logic", "text": "본문" * 100, "extended": True})
    assert st.add_generation(did, "generate", form2, {"question_id": "q3_1"}) is None          # text 없으면 안 남김

    row = st.get_draft(did)
    assert row["idea"] == "세탁소 픽업 앱" and row["capability"] == "개발" and row["owner"] == "203.0.113.7"
    assert row["request_count"] == 2 and row["answers"][0]["slot"] == "customer"
    assert [g["question_id"] for g in row["generations"]] == ["q1", "q2"]
    assert row["generations"][0]["meta"] == {"chars": 6} and row["generations"][1]["chars"] == 200
    assert row["generations"][1]["kind"] == "extend" and row["generations"][1]["meta"]["extended"] is True
    assert st.get_draft("없는-초안-00000") is None
    lst = st.list_drafts()
    assert len(lst) == 1 and lst[0]["generations"] == 2 and lst[0]["requests"] == 2
    st.close()


def test_concurrent_first_requests_do_not_error(tmp_path):
    """문항 9개를 동시에 눌러 같은 초안의 첫 요청이 겹쳐도 (insert 경쟁) 예외 없이 한 행으로 모인다."""
    st = _mk(tmp_path)
    form = {"draft_id": "draft-race-0001", "idea": "동시 제출", "track": "tech"}

    async def go():
        await asyncio.gather(*(asyncio.to_thread(st.upsert_draft, form) for _ in range(9)))

    asyncio.run(go())
    assert st.get_draft("draft-race-0001")["request_count"] == 9
    st.close()


@pytest.mark.asyncio
async def test_record_swallows_failures_and_respects_enabled(tmp_path):
    st = _mk(tmp_path)
    info = await st.record("generate", {"idea": "x", "track": "tech", "draft_id": "draft-rec-000001"}, {"question_id": "q1", "text": "t"})
    assert info["draft_id"] == "draft-rec-000001" and len(info["draft_key"]) >= 16
    assert len(st.get_draft("draft-rec-000001")["generations"]) == 1
    assert await st.record("generate", {"idea": "x", "track": "tech"}, {"text": "t"}) is None   # draft_id 없음 → 저장 안 함(해시 대체 없음)
    assert st.list_drafts() and len(st.list_drafts()) == 1
    await st.record("generate", {"track": "tech"}, {"text": "t"})                    # idea 없음 → 조용히 무시
    st.close()
    await st.record("generate", {"idea": "x", "track": "tech"}, {"text": "t"})       # 엔진 닫힘 → 예외 없음
    off = S.Storage("sqlite:///" + (tmp_path / "off.sqlite").as_posix(), enabled=False)
    off.init()
    assert off.engine is None
    await off.record("generate", {"idea": "x"}, {"text": "t"})


def test_from_config_prefers_env(monkeypatch):
    monkeypatch.delenv("MOCHANG_DATABASE_URL", raising=False)
    st = S.Storage.from_config({"storage": {"enabled": False, "url": "sqlite:///a.sqlite"}})
    assert st.url == "sqlite:///a.sqlite" and st.enabled is False
    monkeypatch.setenv("MOCHANG_DATABASE_URL", "sqlite:///b.sqlite")
    assert S.Storage.from_config({}).url == "sqlite:///b.sqlite"
    assert S.Storage.from_config({}).enabled is True


@pytest.mark.asyncio
async def test_job_generate_is_saved_and_readable(monkeypatch):
    """POST /jobs/generate(draft_id 포함) → done → GET /drafts/{draft_id} 에 아이디어와 생성문이 있다."""
    import httpx

    from backend import main as M
    from backend.llm.client import LLMResult

    async def fake_complete(system, user, model):
        return LLMResult(text="저장 확인용 한 줄 소개입니다.", model=model)

    monkeypatch.setattr(M.client, "_complete", fake_complete)
    did = "test-draft-" + "a1b2c3d4e5f6"
    body = {"question_id": "q1", "style": "plain", "track": "tech", "idea": "저장 테스트 아이디어",
            "capability": "웹 개발 3년", "draft_id": did}

    async with M.lifespan(M.app):
        assert M.storage.enabled and M.storage.engine is not None and "mochang-test-db-" in M.storage.url
        transport = httpx.ASGITransport(app=M.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=30) as c:
            assert (await c.get(f"/drafts/{did}")).status_code == 404
            r = await c.post("/jobs/generate", json=body, headers={"X-Forwarded-For": "203.0.113.9"})
            assert r.status_code == 200 and set(r.json()) >= {"job_id", "kind", "position", "queue"}
            for _ in range(200):
                snap = (await c.get(f"/jobs/{r.json()['job_id']}")).json()
                if snap["status"] in ("done", "error"):
                    break
                await asyncio.sleep(0.02)
            assert snap["status"] == "done" and snap["result"]["text"]

            # 접근 열쇠 (2026-09-04): 응답의 draft_key 가 맞아야 읽힌다. 열쇠 없이는 같은 IP 라도 404(존재 여부도 숨김),
            # 열쇠가 맞으면 다른 IP 에서도 열린다(다른 기기 이어 보기). 열쇠 없는 옛 초안의 IP 규칙은 test_owner_token_* 에서 본다.
            key = snap["result"]["draft_key"]
            assert len(key) >= 16
            assert (await c.get(f"/drafts/{did}", headers={"X-Forwarded-For": "203.0.113.9"})).status_code == 404
            assert (await c.get(f"/drafts/{did}?key=wrong-key-000000000", headers={"X-Forwarded-For": "203.0.113.9"})).status_code == 404
            row = (await c.get(f"/drafts/{did}?key={key}", headers={"X-Forwarded-For": "198.51.100.3"})).json()
            assert row["draft_key"] == key
            assert row["idea"] == "저장 테스트 아이디어" and row["capability"] == "웹 개발 3년"
            assert row["owner"] == "203.0.113.9"
            assert len(row["generations"]) == 1
            assert row["generations"][0]["question_id"] == "q1" and row["generations"][0]["text"] == snap["result"]["text"]

            # 동기 /generate(테스트에서만 열림) 도 같은 초안에 쌓인다 — 열쇠를 실어 보낼 때만. 열쇠 없는 요청은 갱신도 저장도 안 된다.
            r2 = await c.post("/generate", json={**body, "draft_key": key}, headers={"X-Forwarded-For": "203.0.113.9"})
            assert r2.status_code == 200 and r2.json()["draft_key"] == key
            r3 = await c.post("/generate", json=body, headers={"X-Forwarded-For": "203.0.113.9"})     # 열쇠 없음 → 결과는 나가지만 저장 안 됨
            assert r3.status_code == 200 and "draft_key" not in r3.json()
            assert len((await c.get(f"/drafts/{did}?key={key}")).json()["generations"]) == 2


# ── 서비스용 / 백업용 두 DB (2026-09-03) ──

def _pair(tmp_path):
    bak = S.Storage("sqlite:///" + (tmp_path / "bak.sqlite").as_posix())
    st = S.Storage("sqlite:///" + (tmp_path / "svc.sqlite").as_posix(), backup=bak)
    st.init()
    assert bak.engine is not None                       # init 이 백업도 연다
    return st, bak


def test_backup_gets_everything_and_service_skips_test_requests(tmp_path):
    st, bak = _pair(tmp_path)
    real = {"draft_id": "real-0000000001", "idea": "실사용 아이디어", "track": "tech", "team": "팀원 없음"}
    test = {"draft_id": "test-0000000001", "idea": "부하 테스트용 아이디어", "track": "tech", "team": "팀원 없음"}
    gen = {"question_id": "q1", "style": "logic", "text": "한 줄", "model": "m"}
    asyncio.run(st.record("generate", {**real, "question_id": "q1", "style": "logic"}, gen, "1.1.1.1"))
    asyncio.run(st.record("generate", {**test, "question_id": "q1", "style": "logic"}, gen, "10.0.0.1", test=True))
    asyncio.run(st.record("research", {**test, "question_id": "q2"}, {"facts": [{"fact": "x"}], "queries": ["q"]}, None, test=True))
    assert st.get_draft("real-0000000001") and st.get_draft("test-0000000001") is None      # 서비스 DB: 실사용만
    assert bak.get_draft("real-0000000001") and bak.get_draft("test-0000000001")             # 백업 DB: 전부
    assert len(bak.get_draft("real-0000000001")["generations"]) == 1
    assert bak.get_research("test-0000000001", "q2") == [{"fact": "x"}]
    # record 를 안 거치는 직접 쓰기(마무리 작업자 경로)도 백업에 미러링
    st.add_generation("real-0000000001", "generate", {**real, "question_id": "q2", "style": "logic"}, {**gen, "question_id": "q2"})
    assert len(bak.get_draft("real-0000000001")["generations"]) == 2
    st.close()
    assert bak.engine is None                            # close 가 백업도 닫는다


def test_backup_failure_does_not_break_service_writes(tmp_path):
    st, bak = _pair(tmp_path)
    bak.close(); bak.engine = None
    bak.init = lambda: (_ for _ in ()).throw(RuntimeError("disk"))      # 다시 못 열게
    real = {"draft_id": "real-0000000002", "idea": "실사용", "track": "tech", "team": "팀원 없음"}
    asyncio.run(st.record("generate", {**real, "question_id": "q1", "style": "logic"}, {"question_id": "q1", "style": "logic", "text": "t"}, None))
    assert st.get_draft("real-0000000002") is not None


def test_from_config_reads_backup_url_and_ignores_same_file(monkeypatch):
    monkeypatch.delenv("MOCHANG_DATABASE_URL", raising=False)
    monkeypatch.delenv("MOCHANG_BACKUP_DATABASE_URL", raising=False)
    st = S.Storage.from_config({"storage": {"url": "sqlite:///a.sqlite", "backup_url": "sqlite:///b.sqlite"}})
    assert st.backup is not None and st.backup.url == "sqlite:///b.sqlite" and st.backup.backup is None
    assert S.Storage.from_config({"storage": {"url": "sqlite:///a.sqlite", "backup_url": "sqlite:///a.sqlite"}}).backup is None
    monkeypatch.setenv("MOCHANG_BACKUP_DATABASE_URL", "")
    assert S.Storage.from_config({"storage": {"url": "sqlite:///a.sqlite", "backup_url": "sqlite:///b.sqlite"}}).backup is None


@pytest.mark.asyncio
async def test_jobs_with_test_header_skip_service_db(tmp_path):
    """/jobs/generate 에 X-Mochang-Test 가 붙으면 GET /drafts 로는 안 보이고(서비스 DB 없음) 백업에는 있다."""
    import httpx
    from backend import main as M
    from backend.llm.client import LLMResult

    async def fake_complete(system, user, model):
        return LLMResult(text="가짜 본문입니다. " * 5, model=model)

    saved = M.client._complete
    M.client._complete = fake_complete
    try:
        async with M.lifespan(M.app):
            bak = M.storage.backup
            assert bak is not None and bak.engine is not None
            transport = httpx.ASGITransport(app=M.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=30) as c:
                body = {"question_id": "q1", "style": "logic", "track": "tech", "idea": "테스트 헤더 확인용 아이디어", "draft_id": "hdr-test-000001"}
                r = await c.post("/jobs/generate", json=body, headers={"X-Forwarded-For": "10.9.9.9", "X-Mochang-Test": "1"})
                jid = r.json()["job_id"]
                for _ in range(200):
                    await asyncio.sleep(0.02)
                    snap = (await c.get(f"/jobs/{jid}")).json()
                    if snap["status"] in ("done", "error"):
                        break
                assert snap["status"] == "done"
                await asyncio.sleep(0.1)
                assert (await c.get("/drafts/hdr-test-000001")).status_code == 404
                assert bak.get_draft("hdr-test-000001") is not None
    finally:
        M.client._complete = saved


# ── is_test 표시와 삭제 도구: 실사용 행은 어떤 인자로도 안 지워진다 (2026-09-03) ──

def test_test_flag_only_on_backup_and_delete_refuses_real_rows(tmp_path):
    st, bak = _pair(tmp_path)
    real = {"draft_id": "real-0000000009", "idea": "실사용", "track": "tech", "team": "팀원 없음"}
    test = {"draft_id": "test-0000000009", "idea": "테스트", "track": "tech", "team": "팀원 없음"}
    gen = {"question_id": "q1", "style": "logic", "text": "t", "model": "m"}
    asyncio.run(st.record("generate", {**real, "question_id": "q1", "style": "logic"}, gen, "1.1.1.1"))
    asyncio.run(st.record("generate", {**test, "question_id": "q1", "style": "logic"}, gen, "10.0.0.1", test=True))
    rows = {r["draft_id"]: r for r in bak.list_drafts()}
    assert rows["real-0000000009"]["is_test"] is False and rows["test-0000000009"]["is_test"] is True
    assert [r["draft_id"] for r in st.list_drafts()] == ["real-0000000009"]
    # 이미 실사용으로 들어온 초안은 뒤에 test 요청이 와도 테스트로 안 바뀐다
    asyncio.run(st.record("generate", {**real, "question_id": "q2", "style": "logic"}, gen, "10.0.0.1", test=True))
    assert {r["draft_id"]: r["is_test"] for r in bak.list_drafts()}["real-0000000009"] is False
    # 삭제: 실사용 id 를 콕 집어도 안 지워진다. 테스트만 지워진다
    assert bak.delete_drafts(["real-0000000009"]) == []
    assert bak.get_draft("real-0000000009") is not None
    assert bak.delete_drafts(["real-0000000009", "test-0000000009"]) == ["test-0000000009"]
    assert bak.get_draft("test-0000000009") is None and bak.get_draft("real-0000000009") is not None
    assert st.delete_drafts() == []                                  # 서비스 DB 엔 테스트 행이 없다


def test_init_adds_is_test_column_to_old_db(tmp_path):
    """v2 DB(is_test 열 없음)를 열면 ALTER TABLE 로 붙이고 기존 행은 실사용(0)."""
    import sqlite3
    path = tmp_path / "old.sqlite"
    with sqlite3.connect(path) as c:
        c.execute("create table drafts (draft_id varchar(64) primary key, idea text not null default '', track varchar(16), is_business boolean, "
                  "current_item text, team text, capability text, answers text, owner varchar(64), model varchar(160), request_count integer, "
                  "created_at datetime not null, updated_at datetime not null)")
        c.execute("insert into drafts (draft_id, idea, created_at, updated_at) values ('old-000000000001', '옛 초안', '2026-09-01 00:00:00', '2026-09-01 00:00:00')")
    st = S.Storage("sqlite:///" + path.as_posix())
    st.init()
    rows = st.list_drafts()
    assert rows and rows[0]["draft_id"] == "old-000000000001" and rows[0]["is_test"] is False
    assert st.delete_drafts(["old-000000000001"]) == []


# ── 공유 링크 토큰 (2026-09-04): 링크에 DB 키 대신 난수 토큰 ──

def test_share_token_is_random_stable_and_mirrored(tmp_path):
    bak = S.Storage("sqlite:///" + (tmp_path / "b.sqlite").as_posix())
    st = _mk(tmp_path, backup=bak)
    form = {"draft_id": "share-000000001", "idea": "공유 링크 아이디어", "track": "tech"}
    st.upsert_draft(form)
    assert st.share_token("없는-초안-00000") is None
    tok = st.share_token("share-000000001")
    assert tok and S.SHARE_TOKEN_RE.match(tok) and tok != "share-000000001"
    assert st.share_token("share-000000001") == tok                      # 두 번째는 같은 값
    assert st.draft_id_for_share(tok) == "share-000000001"
    assert st.draft_id_for_share("x" * 24) is None and st.draft_id_for_share("short") is None
    assert st.get_draft("share-000000001").get("share_token") is None    # 초안 응답에는 토큰이 안 실린다
    assert bak.draft_id_for_share(tok) == "share-000000001"              # 백업에도 같은 토큰
    st.close(); bak.close()


def test_init_adds_share_token_column_to_old_db(tmp_path):
    import sqlite3
    path = tmp_path / "old3.sqlite"
    with sqlite3.connect(path) as c:
        c.execute("create table drafts (draft_id varchar(64) primary key, idea text not null default '', track varchar(16), is_business boolean, "
                  "current_item text, team text, capability text, answers text, owner varchar(64), model varchar(160), request_count integer, "
                  "is_test boolean not null default 0, created_at datetime not null, updated_at datetime not null)")
        c.execute("insert into drafts (draft_id, idea, created_at, updated_at) values ('old-000000000002', '옛 초안', '2026-09-01 00:00:00', '2026-09-01 00:00:00')")
    st = S.Storage("sqlite:///" + path.as_posix())
    st.init()
    tok = st.share_token("old-000000000002")
    assert tok and st.draft_id_for_share(tok) == "old-000000000002"
    st.close()


@pytest.mark.asyncio
async def test_share_endpoints_roundtrip(tmp_path):
    """POST /drafts/{id}/share → GET /shared/{token} 이 GET /drafts/{id} 와 같은 초안을 준다. 모르는 토큰은 404."""
    import httpx
    from backend import main as M

    async with M.lifespan(M.app):
        key = M.storage.upsert({"draft_id": "share-api-000001", "idea": "공유 API 아이디어", "track": "tech"})["draft_key"]
        transport = httpx.ASGITransport(app=M.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=30) as c:
            assert (await c.post("/drafts/no-such-draft-0001/share")).status_code == 404
            assert (await c.post("/drafts/share-api-000001/share")).status_code == 404          # 열쇠 없이는 주인이 아니다 (2026-09-04)
            r = await c.post(f"/drafts/share-api-000001/share?key={key}")
            assert r.status_code == 200
            tok = r.json()["share"]
            assert tok != "share-api-000001" and (await c.post(f"/drafts/share-api-000001/share?key={key}")).json()["share"] == tok
            r2 = await c.get(f"/shared/{tok}")
            assert r2.status_code == 200
            body = r2.json()
            assert body["draft_id"] == "share-api-000001" and body["idea"] == "공유 API 아이디어" and body["share"] == tok
            assert body["draft_key"] == key                        # 받는 기기가 같은 초안을 이어 쓸 수 있게 열쇠도 준다
            assert "finisher" in body and "share_token" not in body and "owner_token" not in body
            assert (await c.get("/shared/" + "z" * 24)).status_code == 404
            assert (await c.get("/shared/bad")).status_code == 404
