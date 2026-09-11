"""브라우저 익명 id(client_id, 2026-09-07) — 집계용. 저장 규칙과 헤더 전달을 본다. 모델 호출은 전부 가짜."""
import asyncio
import uuid

import httpx
import pytest

from backend import storage as S
from backend.llm.client import LLMResult


def _store(tmp_path):
    st = S.Storage(url="sqlite:///" + (tmp_path / "t.sqlite").as_posix())
    st.init()
    return st


def test_first_writer_sets_client_id_and_later_writes_do_not_overwrite(tmp_path):
    st = _store(tmp_path)
    did = str(uuid.uuid4())
    form = {"draft_id": did, "idea": "아이디어", "track": "tech", "client_id": "browser-A-00000001"}
    first = st.upsert(dict(form), "1.2.3.4")
    assert first and st.get_draft(did)["client_id"] == "browser-A-00000001"
    # 같은 초안을 다른 브라우저 id 로 갱신해도(공유 링크로 옮긴 기기가 상속에 실패한 경우) 처음 값이 남는다
    st.upsert({**form, "draft_key": first["draft_key"], "client_id": "browser-B-00000002"}, "1.2.3.4")
    assert st.get_draft(did)["client_id"] == "browser-A-00000001"


def test_legacy_row_without_client_id_is_filled_on_next_write(tmp_path):
    st = _store(tmp_path)
    did = str(uuid.uuid4())
    form = {"draft_id": did, "idea": "아이디어", "track": "tech"}          # id 없이 만들어진 옛 행
    first = st.upsert(dict(form), "1.2.3.4")
    assert st.get_draft(did)["client_id"] is None
    st.upsert({**form, "draft_key": first["draft_key"], "client_id": "browser-C-00000003"}, "1.2.3.4")
    assert st.get_draft(did)["client_id"] == "browser-C-00000003"


def test_malformed_client_id_is_ignored(tmp_path):
    st = _store(tmp_path)
    did = str(uuid.uuid4())
    st.upsert({"draft_id": did, "idea": "아이디어", "track": "tech", "client_id": "bad id!"}, "1.2.3.4")
    assert st.get_draft(did)["client_id"] is None


@pytest.mark.asyncio
async def test_header_reaches_storage_and_shared_response_carries_it():
    """프론트가 X-Mochang-Client 로 보내면 초안 행에 남고, 공유 링크 응답에 실려 다른 기기가 물려받을 수 있다."""
    from backend import main as M

    async def fake_complete(system, user, model, extra=None, on_delta=None):
        await asyncio.sleep(0.01)
        return LLMResult(text="가짜 본문", model=model)

    saved = M.client._complete
    M.client._complete = fake_complete
    try:
        async with M.lifespan(M.app):
            transport = httpx.ASGITransport(app=M.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=30) as c:
                did = str(uuid.uuid4())
                head = {"X-Forwarded-For": "10.0.0.9", "X-Mochang-Client": "phone-browser-0001"}
                body = {"track": "tech", "idea": "헬스장 QR 거리 대결 앱", "is_business": False, "team": "팀원 없음",
                        "capability": "x", "question_id": "q10", "style": "logic", "draft_id": did}
                sub = await c.post("/jobs/generate", json=body, headers=head)
                assert sub.status_code == 200
                jid = sub.json()["job_id"]
                for _ in range(200):
                    snap = (await c.get(f"/jobs/{jid}")).json()
                    if snap["status"] in ("done", "error"):
                        break
                    await asyncio.sleep(0.02)
                assert snap["status"] == "done"
                key = snap["result"]["draft_key"]
                assert M.storage.get_draft(did)["client_id"] == "phone-browser-0001"
                # 공유 링크 → 다른 기기: 응답에 client_id 가 실려야 상속할 수 있다
                sh = await c.post(f"/drafts/{did}/share?key={key}", headers=head)
                assert sh.status_code == 200
                other = await c.get(f"/shared/{sh.json()['share']}")
                assert other.status_code == 200 and other.json()["client_id"] == "phone-browser-0001"
                # 헤더가 형식 밖이면 무시되고 요청은 그대로 처리된다
                did2 = str(uuid.uuid4())
                sub2 = await c.post("/jobs/generate", json={**body, "draft_id": did2},
                                    headers={**head, "X-Mochang-Client": "no good"})
                assert sub2.status_code == 200
    finally:
        M.client._complete = saved
