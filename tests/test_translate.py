"""번역(외국인 지원자용 읽기 번역) — 평문/목록 모드, 언어 코드 검증, JSON 파싱 실패 처리. 모델 호출은 전부 가짜."""
import json

import pytest

from backend.llm.client import LLMResult
from backend.pipeline import translate as T


class FakeClient:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    async def complete(self, system, user, model=None):
        self.calls.append({"system": system, "user": user, "model": model})
        reply = self.replies.pop(0) if self.replies else ""
        return LLMResult(text=reply, model="fake")


@pytest.mark.parametrize("raw,expected", [("en", "en"), ("EN", "en"), ("zh-CN", "zh"), ("ja_JP", "ja")])
def test_normalize_lang_accepts_region_suffix(raw, expected):
    assert T.normalize_lang(raw) == expected


@pytest.mark.parametrize("bad", ["ko", "fr", "", None])
def test_normalize_lang_rejects_unknown(bad):
    with pytest.raises(ValueError):
        T.normalize_lang(bad)


@pytest.mark.asyncio
async def test_single_text_uses_plain_mode_and_strips_wrappers():
    c = FakeClient(["```\nTranslation:\nHello, judges.\n\nSecond paragraph.\n```"])
    out = await T.translate_texts(c, ["안녕하세요 심사위원님.\n\n둘째 문단."], "en")
    assert out == {"lang": "en", "translations": ["Hello, judges.\n\nSecond paragraph."], "model": "fake"}
    assert len(c.calls) == 1
    assert "영어(English)" in c.calls[0]["system"] and "[원문]" in c.calls[0]["user"]
    assert "JSON" not in c.calls[0]["system"]                     # 평문 모드 — translate.md


@pytest.mark.asyncio
async def test_empty_items_skip_the_model_entirely():
    c = FakeClient([])
    out = await T.translate_texts(c, ["", "   "], "ja")
    assert out["translations"] == ["", ""] and c.calls == []


@pytest.mark.asyncio
async def test_list_mode_maps_numbered_json_back_by_index_and_keeps_blanks():
    c = FakeClient([json.dumps({"1": "Who is it for?", "2": "Office workers", "3": ""})])
    out = await T.translate_texts(c, ["누구를 위한 것인가요?", "직장인", "자영업자"], "en")
    assert out["translations"] == ["Who is it for?", "Office workers", ""]
    user = c.calls[0]["user"]
    assert "1. 누구를 위한 것인가요?" in user and "3. 자영업자" in user
    assert "[원문 목록" in user and "JSON" in c.calls[0]["system"]      # 목록 모드 — translate_batch.md


@pytest.mark.asyncio
async def test_list_mode_skips_empty_items_but_keeps_positions():
    c = FakeClient([json.dumps({"1": "A", "2": "C"})])
    out = await T.translate_texts(c, ["가", "", "다"], "zh")
    assert out["translations"] == ["A", "", "C"]
    assert "2. " not in c.calls[0]["user"].replace("2. 다", "")        # 빈 항목은 목록에 안 들어간다


@pytest.mark.asyncio
async def test_list_mode_chunks_by_batch_size(monkeypatch):
    monkeypatch.setattr(T, "BATCH", 2)
    c = FakeClient([json.dumps({"1": "a", "2": "b"}), json.dumps({"1": "c", "2": "d"}), json.dumps({"1": "e"})])
    out = await T.translate_texts(c, ["ㄱ", "ㄴ", "ㄷ", "ㄹ", "ㅁ"], "en")
    assert out["translations"] == ["a", "b", "c", "d", "e"] and len(c.calls) == 3


@pytest.mark.asyncio
async def test_broken_json_yields_blanks_not_errors():
    c = FakeClient(["죄송합니다, 번역할 수 없습니다"])
    out = await T.translate_texts(c, ["가", "나"], "en")
    assert out["translations"] == ["", ""]


@pytest.mark.asyncio
async def test_rejects_too_many_items_and_truncates_long_ones():
    c = FakeClient(["x"])
    with pytest.raises(ValueError):
        await T.translate_texts(c, ["가"] * (T.MAX_ITEMS + 1), "en")
    await T.translate_texts(c, ["가" * (T.MAX_CHARS + 500)], "en")
    assert len(c.calls[0]["user"]) < T.MAX_CHARS + 100


# ── /translate · /jobs/translate 연결 — 조사 큐를 타고, 같은 IP 의 동시 작업 제한(3건)을 받지 않는다 ──

@pytest.mark.asyncio
async def test_jobs_translate_bypasses_per_ip_limit_and_returns_translations():
    import asyncio
    import httpx
    from backend import main as M

    async def fake_complete(system, user, model):
        await asyncio.sleep(0.02)
        return LLMResult(text="Hello from fake", model=model)

    saved = M.research_client._complete
    M.research_client._complete = fake_complete
    try:
        async with M.lifespan(M.app):
            transport = httpx.ASGITransport(app=M.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=30) as c:
                head = {"X-Forwarded-For": "10.0.0.7"}
                sync = await c.post("/translate", json={"lang": "en", "texts": ["안녕하세요"]}, headers=head)
                assert sync.status_code == 200 and sync.json()["translations"] == ["Hello from fake"]
                bad = await c.post("/translate", json={"lang": "fr", "texts": ["안녕하세요"]}, headers=head)
                assert bad.status_code == 400
                # 같은 IP 로 6건 동시 제출 — 생성이라면 3건에서 429 지만 번역은 전부 수락
                subs = await asyncio.gather(*(c.post("/jobs/translate", json={"lang": "ja", "texts": [f"문장 {i}"]}, headers=head) for i in range(6)))
                assert [r.status_code for r in subs] == [200] * 6
                pending = {r.json()["job_id"] for r in subs}
                results = {}
                for _ in range(200):
                    await asyncio.sleep(0.02)
                    for jid in list(pending):
                        snap = (await c.get(f"/jobs/{jid}")).json()
                        if snap["status"] in ("done", "error"):
                            pending.discard(jid)
                            results[jid] = snap
                    if not pending:
                        break
                assert not pending and all(s["status"] == "done" for s in results.values())
                assert all(s["result"]["translations"] == ["Hello from fake"] and s["result"]["lang"] == "ja" for s in results.values())
                stats = (await c.get("/jobs", headers=head)).json()
                assert stats["research"]["done"] >= 6                      # 조사 큐에서 돌았다
    finally:
        M.research_client._complete = saved


# ── 2026-09-03 실측 보정: 일본어 번역에 '여부를' 이 남았다 → 비었거나 한글이 남은 결과는 1회 더 부른다. 청크는 병렬 ──

@pytest.mark.asyncio
async def test_plain_mode_retries_once_when_hangul_remains():
    c = FakeClient(["ビザ規定の遵守 여부를 検証", "ビザ規定の遵守の有無を検証"])
    out = await T.translate_texts(c, ["비자 규정 준수 여부를 검증"], "ja")
    assert out["translations"] == ["ビザ規定の遵守の有無を検証"] and len(c.calls) == 2


@pytest.mark.asyncio
async def test_plain_mode_keeps_first_if_retry_is_no_better():
    c = FakeClient(["遵守 여부", ""])
    out = await T.translate_texts(c, ["준수 여부"], "ja")
    assert out["translations"] == ["遵守 여부"] and len(c.calls) == 2      # 빈 것보다는 한글 섞인 것이 낫다


@pytest.mark.asyncio
async def test_list_mode_retries_only_blank_or_hangul_items():
    c = FakeClient([json.dumps({"1": "Who?", "2": "", "3": "Office 직장인"}), json.dumps({"1": "Students", "2": "Office workers"})])
    out = await T.translate_texts(c, ["누구?", "학생", "직장인"], "en")
    assert out["translations"] == ["Who?", "Students", "Office workers"]
    assert "1. 학생" in c.calls[1]["user"] and "2. 직장인" in c.calls[1]["user"] and "누구" not in c.calls[1]["user"]


@pytest.mark.asyncio
async def test_list_mode_runs_chunks_concurrently(monkeypatch):
    import asyncio
    monkeypatch.setattr(T, "BATCH", 2)
    state = {"running": 0, "peak": 0}

    class SlowClient(FakeClient):
        async def complete(self, system, user, model=None):
            state["running"] += 1
            state["peak"] = max(state["peak"], state["running"])
            await asyncio.sleep(0.02)
            state["running"] -= 1
            return await super().complete(system, user, model)

    c = SlowClient([json.dumps({"1": "a", "2": "b"}), json.dumps({"1": "c", "2": "d"}), json.dumps({"1": "e"})])
    out = await T.translate_texts(c, ["ㄱ", "ㄴ", "ㄷ", "ㄹ", "ㅁ"], "en")
    assert sorted(out["translations"]) == ["a", "b", "c", "d", "e"] and state["peak"] == 3
