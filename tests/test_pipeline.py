"""조사 파이프라인 — 모델·검색·HTTP 전부 가짜. 특히 '지어낸 quote 는 버려진다'를 검증."""
import json

import pytest

from backend.rag import pipeline as P
from backend.rag import research as R
from backend.llm.client import LLMResult


class FakeClient:
    """complete() 호출 순서대로 미리 정한 답을 돌려준다. 실제 모델 호출 없음."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    async def complete(self, system, user, model=None):
        self.calls.append({"system": system, "user": user, "model": model})
        reply = self.replies.pop(0) if self.replies else ""
        return LLMResult(text=reply, model="fake")


PAGE_TEXT = ("서울시 조사에 따르면 반찬가게의 하루 폐기율은 평균 23%로 나타났다. 1인 가구의 62%가 주 3회 이상 배달을 이용한다. "
             "조사는 2026년 2월 서울 시내 반찬가게 120곳과 1인 가구 800명을 대상으로 진행됐다.")


def fake_results(*urls):
    return [R.make_result(f"제목{i}", u, "요약", "2026-03-0%d" % (i + 1), "news") for i, u in enumerate(urls)]


def make_fetch(mapping):
    async def fetch(url, timeout=15):
        return mapping.get(url, "")
    return fetch


@pytest.fixture
def form():
    return {"track": "tech", "idea": "동네 반찬가게 남은 반찬을 저녁에 할인 꾸러미로 예약하는 앱", "style": "story"}


# ── 검색어 생성 ──

@pytest.mark.asyncio
async def test_generate_queries_parses_array_and_dedupes(form):
    client = FakeClient(['```json\n["반찬가게 폐기율", "1인 가구 저녁 식사 실태", "반찬가게 폐기율"]\n```'])
    meta, body = {"label": "Q2", "title": "배경", "body": "문제 크기"}, ""
    qs = await P.generate_queries(client, form, meta, P.ResearchConfig(max_queries=4))
    assert qs == ["반찬가게 폐기율", "1인 가구 저녁 식사 실태"]
    assert "검색어 4개" in client.calls[0]["system"]


@pytest.mark.asyncio
async def test_generate_queries_falls_back_when_model_output_is_garbage(form):
    client = FakeClient(["모델이 이상한 말"])
    qs = await P.generate_queries(client, form, {"label": "Q2", "title": "", "body": ""}, P.ResearchConfig(max_queries=3))
    assert len(qs) == 3 and all(isinstance(q, str) and q for q in qs)


# ── 검색 + 본문 ──

@pytest.mark.asyncio
async def test_collect_pages_ranks_trusted_domains_and_uses_snippet_fallback(tmp_path):
    results = [
        R.make_result("블로그", "https://blog.example.com/a", "블로그 글 스니펫입니다. 본문을 못 가져오면 이 스니펫이 대신 쓰여야 합니다. 길이는 충분히 길게."),
        R.make_result("통계청", "https://kostat.go.kr/x", "정부 자료 " * 20, "2026-01-01", "web"),
    ]
    researcher = R.Researcher(backends=[("fake", lambda q, n: _aret(results))], cache=R.DiskCache(tmp_path))
    fetch = make_fetch({"https://kostat.go.kr/x": PAGE_TEXT})
    unique, pages = await P.collect_pages(researcher, ["q1"], P.ResearchConfig(max_pages_to_extract=5), fetch=fetch)
    assert [p["url"] for p in pages][0] == "https://kostat.go.kr/x"      # 정부 도메인이 먼저
    assert pages[0]["text"] == PAGE_TEXT and pages[0]["from_snippet"] is False
    assert pages[1]["from_snippet"] is True                                # 본문 없으면 스니펫으로
    assert len(unique) == 2


async def _aret(v):
    return v


# ── 사실 추출 + quote 검증 ──

@pytest.mark.asyncio
async def test_extract_facts_drops_fabricated_quotes(form):
    pages = [{"url": "https://kostat.go.kr/x", "title": "통계", "date": "2026-03-01", "text": PAGE_TEXT}]
    model_out = json.dumps([
        {"fact": "반찬가게 하루 폐기율 평균 23%", "quote": "반찬가게의 하루 폐기율은 평균 23%로 나타났다",
         "source_title": "통계", "url": "https://kostat.go.kr/x", "date": "2026-03-01", "publisher": "서울시", "use_for": "문제 크기"},
        {"fact": "폐기율 45%라는 지어낸 사실", "quote": "폐기율은 45%에 달한다",   # 원문에 없음 → 버려져야 함
         "source_title": "통계", "url": "https://kostat.go.kr/x", "date": None},
        {"fact": "1인 가구 배달 이용", "quote": "1인 가구의 62%가  주 3회 이상 배달을 이용한다",  # 공백 차이는 허용
         "source_title": "통계", "url": "https://kostat.go.kr/x", "date": "null"},
    ], ensure_ascii=False)
    client = FakeClient([model_out])
    facts = await P.extract_facts(client, form, {"label": "Q2", "title": "배경", "body": ""}, pages, P.ResearchConfig())
    assert [f["fact"] for f in facts] == ["반찬가게 하루 폐기율 평균 23%", "1인 가구 배달 이용"]
    assert facts[1]["date"] is None and facts[0]["publisher"] == "서울시"
    assert "지어내기 금지" in client.calls[0]["system"] and "<문서 1>" in client.calls[0]["user"]


@pytest.mark.asyncio
async def test_extract_facts_returns_empty_on_bad_json_or_no_pages(form):
    assert await P.extract_facts(FakeClient(["x"]), form, {}, [], P.ResearchConfig()) == []
    pages = [{"url": "u", "title": "t", "text": PAGE_TEXT}]
    assert await P.extract_facts(FakeClient(["이건 JSON 아님"]), form, {}, pages, P.ResearchConfig()) == []


def test_format_references_includes_source_year_and_rules():
    facts = [{"fact": "폐기율 23%", "quote": "폐기율은 평균 23%", "url": "https://k.go.kr/x", "date": "2026-03-01", "publisher": "서울시"}]
    s = P.format_references(facts)
    assert s.startswith("[웹 참고자료") and "1. 폐기율 23% (출처: 서울시, 2026)" in s and "원문:" in s and "URL: https://k.go.kr/x" in s
    assert P.format_references([]) == ""


# ── 전체 + 캐시 ──

@pytest.mark.asyncio
async def test_run_research_end_to_end_with_cache(tmp_path, form):
    results = fake_results("https://kostat.go.kr/x")
    researcher = R.Researcher(backends=[("fake", lambda q, n: _aret(results))], cache=R.DiskCache(tmp_path / "s"))
    facts_json = json.dumps([{"fact": "폐기율 23%", "quote": "하루 폐기율은 평균 23%", "source_title": "통계",
                              "url": "https://kostat.go.kr/x", "date": "2026-03-01", "publisher": "서울시"}], ensure_ascii=False)
    client = FakeClient(['["반찬가게 폐기율", "1인 가구 식사"]', facts_json])
    fetch = make_fetch({"https://kostat.go.kr/x": PAGE_TEXT})
    cache = R.DiskCache(tmp_path / "f")

    out = await P.run_research(client, researcher, form, "q2", P.ResearchConfig(max_queries=2), cache=cache, fetch=fetch)
    assert out["queries"] == ["반찬가게 폐기율", "1인 가구 식사"]
    assert out["facts"][0]["fact"] == "폐기율 23%" and out["cached"] is False
    assert "출처: 서울시, 2026" in out["references"]
    assert len(client.calls) == 2                                   # 검색어 1회 + 추출 1회

    again = await P.run_research(FakeClient([]), researcher, form, "q2", P.ResearchConfig(), cache=cache, fetch=fetch)
    assert again["cached"] is True and again["facts"] == out["facts"]   # 두 번째는 모델 호출 0회


def test_build_context_injects_references():
    from backend.pipeline import assemble
    ctx = assemble.build_context({"track": "tech", "idea": "x", "references": [
        {"fact": "폐기율 23%", "quote": "q", "url": "https://k.go.kr", "date": "2026-01-01", "publisher": "서울시"}]})
    assert "[웹 참고자료" in ctx and "폐기율 23% (출처: 서울시, 2026)" in ctx
    assert "[웹 참고자료" not in assemble.build_context({"track": "tech", "idea": "x"})
