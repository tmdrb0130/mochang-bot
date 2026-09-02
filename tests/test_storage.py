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


def test_draft_id_uses_frontend_uuid_or_falls_back_to_hash():
    assert S.draft_id_for({"draft_id": "0b1c9f2e-9d1a-4f42-8c1e-1234567890ab", "idea": "x"}) == "0b1c9f2e-9d1a-4f42-8c1e-1234567890ab"
    # 형식 밖 값(짧음·공백·따옴표)은 무시하고 해시로
    a = S.draft_id_for({"draft_id": "bad id!", "track": "tech", "idea": "  캠핑장 빈자리 알림  "})
    b = S.draft_id_for({"track": "tech", "idea": "캠핑장 빈자리 알림"})
    assert a == b and a.startswith("h") and len(a) == 24
    assert S.draft_id_for({"track": "local", "idea": "캠핑장 빈자리 알림"}) != b       # 트랙이 다르면 다른 초안


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
    await st.record("generate", {"idea": "x", "track": "tech"}, {"question_id": "q1", "text": "t"})
    assert len(st.get_draft(S.draft_id_for({"idea": "x", "track": "tech"}))["generations"]) == 1
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

            row = (await c.get(f"/drafts/{did}")).json()
            assert row["idea"] == "저장 테스트 아이디어" and row["capability"] == "웹 개발 3년"
            assert row["owner"] == "203.0.113.9"
            assert len(row["generations"]) == 1
            assert row["generations"][0]["question_id"] == "q1" and row["generations"][0]["text"] == snap["result"]["text"]

            # 동기 /generate 도 같은 초안에 쌓인다 (draft_id 없이 보내면 해시로 묶이므로 다른 초안)
            r2 = await c.post("/generate", json=body)
            assert r2.status_code == 200
            assert len((await c.get(f"/drafts/{did}")).json()["generations"]) == 2
