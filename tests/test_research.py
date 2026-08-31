"""research.py — 검색 추상화·폴백·캐시. 외부 네트워크/모델 호출 없음 (전부 가짜 백엔드)."""
import pytest

from backend.rag import research as R


def fake_backend(results, calls=None, fail=False):
    async def fn(query, max_results):
        if calls is not None:
            calls.append(query)
        if fail:
            raise RuntimeError("boom")
        return results[:max_results]
    return fn


def test_make_result_normalizes_date_and_whitespace():
    r = R.make_result(" 제목 ", "https://a.com/x ", "본문   여러  공백", "2026년 5월 3일", "news")
    assert r == {"title": "제목", "url": "https://a.com/x", "snippet": "본문 여러 공백", "date": "2026-05-03", "source_type": "news"}
    assert R.make_result("t", "u", date="2026-08-31T10:00:00Z")["date"] == "2026-08-31"
    assert R.make_result("t", "u", date="없음")["date"] is None


def test_dedupe_by_url_ignoring_fragment_and_trailing_slash():
    rows = [R.make_result("a", "https://x.com/p/"), R.make_result("b", "https://x.com/p#frag"), R.make_result("c", "")]
    assert [r["title"] for r in R.dedupe(rows)] == ["a"]


@pytest.mark.asyncio
async def test_fallback_to_second_backend_when_first_fails(tmp_path):
    calls_a, calls_b = [], []
    good = [R.make_result("결과", "https://ok.com/1", "snip")]
    r = R.Researcher(
        backends=[("vane", fake_backend([], calls_a, fail=True)), ("ddgs", fake_backend(good, calls_b))],
        cache=R.DiskCache(tmp_path),
    )
    out = await r.search("반찬가게 폐기율")
    assert out == good
    assert r.last_backend == "ddgs"
    assert "vane: RuntimeError" in r.last_error
    assert calls_a == ["반찬가게 폐기율"] and calls_b == ["반찬가게 폐기율"]


@pytest.mark.asyncio
async def test_empty_first_backend_falls_through_and_empty_query_short_circuits(tmp_path):
    good = [R.make_result("x", "https://x.com")]
    r = R.Researcher(backends=[("a", fake_backend([])), ("b", fake_backend(good))], cache=R.DiskCache(tmp_path))
    assert await r.search("q") == good and r.last_backend == "b"
    assert await r.search("   ") == []


@pytest.mark.asyncio
async def test_disk_cache_hit_skips_backends(tmp_path):
    calls = []
    good = [R.make_result("x", "https://x.com")]
    r = R.Researcher(backends=[("a", fake_backend(good, calls))], cache=R.DiskCache(tmp_path))
    await r.search("같은 검색어")
    await r.search("같은 검색어")
    assert calls == ["같은 검색어"]          # 두 번째는 캐시
    assert r.last_backend == "cache"
    assert len(list(tmp_path.glob("*.json"))) == 1
    await r.search("같은 검색어", use_cache=False)
    assert calls == ["같은 검색어", "같은 검색어"]


def test_disk_cache_expires(tmp_path, monkeypatch):
    c = R.DiskCache(tmp_path, ttl=10)
    c.set("k", [{"title": "t"}])
    assert c.get("k") == [{"title": "t"}]
    monkeypatch.setattr(R.time, "time", lambda: 9e12)   # 먼 미래 → TTL 초과
    assert c.get("k") is None


def test_vane_parse_maps_sources_to_common_schema():
    payload = {"message": "요약", "sources": [
        {"pageContent": "반찬 폐기율 25%", "metadata": {"title": "기사", "url": "https://news.kr/a", "publishedDate": "2026-03-02"}},
        {"pageContent": "중복", "metadata": {"title": "기사2", "url": "https://news.kr/a"}},
    ]}
    out = R.VaneSearch.parse(payload)
    assert out == [{"title": "기사", "url": "https://news.kr/a", "snippet": "반찬 폐기율 25%", "date": "2026-03-02", "source_type": "vane"}]


def test_vane_build_body_matches_api_shape():
    cfg = R.VaneConfig(chat_provider_id="p1", embedding_provider_id="p2", chat_model_key="m", embedding_model_key="e")
    body = R.VaneSearch(cfg).build_body("질문")
    assert body == {"chatModel": {"providerId": "p1", "key": "m"}, "embeddingModel": {"providerId": "p2", "key": "e"},
                    "sources": ["web"], "optimizationMode": "speed", "query": "질문", "stream": False}
    assert cfg.configured and not R.VaneConfig().configured


def test_researcher_from_config_respects_vane_enabled():
    cfg = {"research": {"vane": {"enabled": False, "chat_provider_id": "a", "embedding_provider_id": "b"}}}
    assert [n for n, _ in R.researcher_from_config(cfg).backends] == ["ddgs"]
    cfg["research"]["vane"]["enabled"] = True
    assert [n for n, _ in R.researcher_from_config(cfg).backends] == ["vane", "ddgs"]
    assert [n for n, _ in R.researcher_from_config({}).backends] == ["ddgs"]


@pytest.mark.asyncio
async def test_ddgs_backend_uses_monkeypatched_client(monkeypatch):
    class FakeDDGS:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def text(self, q, region, max_results): return [{"title": "T", "href": "https://t.com", "body": "b"}]
        def news(self, q, region, max_results): return [{"title": "N", "url": "https://n.com", "body": "nb", "date": "2026-01-02T00:00:00+00:00"}]
    import ddgs
    monkeypatch.setattr(ddgs, "DDGS", FakeDDGS)
    out = await R.DDGSearch()("q", 5)
    assert [r["source_type"] for r in out] == ["web", "news"] and out[1]["date"] == "2026-01-02"
