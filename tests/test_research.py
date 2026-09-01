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
    calls = []

    class FakeDDGS:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def text(self, q, region, max_results):
            calls.append("text")
            return [{"title": "T", "href": "https://t.com", "body": "b"}]
        def news(self, q, region, max_results):
            calls.append("news")
            return [{"title": "N", "url": "https://n.com", "body": "nb", "date": "2026-01-02T00:00:00+00:00"}]
    import ddgs
    monkeypatch.setattr(ddgs, "DDGS", FakeDDGS)

    out = await R.DDGSearch()("q", 5)                       # 기본: text 만 (뉴스는 네이버 API 로 이관 예정)
    assert [r["source_type"] for r in out] == ["web"] and calls == ["text"]

    calls.clear()
    out = await R.DDGSearch(include_news=True)("q", 5)      # 명시하면 뉴스도 (되살리기 가능)
    assert [r["source_type"] for r in out] == ["web", "news"] and out[1]["date"] == "2026-01-02"
    assert calls == ["text", "news"]


# ── 네이버 검색 API 어댑터 (RESEARCH_PLAN 3단계). 키 없이 mock 으로만 검증 ──

def test_naver_config_off_without_keys(monkeypatch):
    monkeypatch.delenv("NAVER_CLIENT_ID", raising=False)
    monkeypatch.delenv("NAVER_CLIENT_SECRET", raising=False)
    assert R.NaverConfig.from_config({}).configured is False
    assert [n for n, _ in R.researcher_from_config({}).backends] == ["ddgs"]   # 키 없으면 예전 그대로

    monkeypatch.setenv("NAVER_CLIENT_ID", "id")
    monkeypatch.setenv("NAVER_CLIENT_SECRET", "secret")
    cfg = R.NaverConfig.from_config({"naver": {"kinds": ["news", "doc", "없는유형"]}})
    assert cfg.configured and cfg.kinds == ["news", "doc"]                     # 모르는 유형은 버린다
    assert [n for n, _ in R.researcher_from_config({}).backends] == ["naver", "ddgs"]


def test_naver_parse_strips_tags_and_normalizes():
    payload = {"items": [{
        "title": "1인 가구 <b>식품</b> 폐기 &amp; 낭비",
        "originallink": "https://news.co.kr/a", "link": "https://n.news.naver.com/a",
        "description": "본문   <b>요약</b>입니다", "pubDate": "Mon, 01 Sep 2026 09:00:00 +0900",
    }]}
    rows = R.NaverSearch(R.NaverConfig(client_id="i", client_secret="s")).parse("news", payload)
    assert rows == [{"title": "1인 가구 식품 폐기 & 낭비", "url": "https://news.co.kr/a",   # 원문 링크 우선
                     "snippet": "본문 요약입니다", "date": "2026-09-01", "source_type": "news"}]
    assert R.NaverSearch(R.NaverConfig()).parse("doc", {})== []
    assert R.NaverSearch(R.NaverConfig()).parse("doc", {"items": [{"title": "논문", "link": "https://k.re.kr/1"}]}
                                                )[0]["source_type"] == "academic"


@pytest.mark.asyncio
async def test_naver_search_requests_each_kind_and_survives_one_failure(monkeypatch):
    """kinds 마다 요청 1회. 유형 하나가 죽어도 나머지 결과는 살린다."""
    seen = []

    class FakeResponse:
        def __init__(self, path):
            self.path = path

        def raise_for_status(self):
            if "blog" in self.path:
                raise RuntimeError("500")

        def json(self):
            return {"items": [{"title": "T", "link": f"https://x.com/{self.path}", "description": "d"}]}

    class FakeClient:
        def __init__(self, **kw):
            self.headers = kw.get("headers", {})

        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False

        async def get(self, url, params=None):
            seen.append((url.rsplit("/", 1)[-1], params["display"], self.headers.get("X-Naver-Client-Id")))
            return FakeResponse(url)

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    cfg = R.NaverConfig(client_id="id", client_secret="secret", kinds=["news", "blog", "doc"])
    out = await R.NaverSearch(cfg)("반찬가게 폐기율", 6)
    assert [p for p, *_ in seen] == ["news.json", "blog.json", "doc.json"]
    assert seen[0][1] == 2 and seen[0][2] == "id"          # max_results 를 유형 수로 나눠 요청, 키는 헤더로
    assert [r["source_type"] for r in out] == ["news", "academic"]   # blog 실패분만 빠짐


def test_norm_date_understands_naver_formats():
    assert R.make_result("t", "u", date="Mon, 01 Sep 2026 09:00:00 +0900")["date"] == "2026-09-01"   # 뉴스 pubDate
    assert R.make_result("t", "u", date="20260901")["date"] == "2026-09-01"                          # 블로그 postdate
    assert R.make_result("t", "u", date="20261301")["date"] is None                                  # 13월은 날짜 아님
