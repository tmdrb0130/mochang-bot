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
    # 같은 출처 아래 사실을 묶어 보여준다. "출처:" 라는 말은 목록에 쓰지 않는다 —
    # 모델이 그 문자열을 본문에 그대로 옮겨 쓰기 때문(2026-09-01).
    assert s.startswith("[웹 참고자료")
    assert "1. 2026년 서울시" in s
    assert "- 폐기율 23%" in s
    assert "원문:" in s and "URL: https://k.go.kr/x" in s
    assert P.format_references([]) == ""


def test_format_references_guards_null_like_publisher():
    """추출 모델이 문자열 'null' 을 내보내도 '출처: null' 이 프롬프트에 들어가면 안 된다."""
    s = P.format_references([{"fact": "x", "url": "https://a.kr/1", "date": None, "publisher": "null"}])
    assert "null" not in s.lower().replace("https://", "")
    assert "인용하지 말 것" in s


def test_format_references_takes_year_from_url_when_date_missing():
    """조사 모델이 date 를 비워도 기사 URL 에 날짜가 있으면 연도를 건진다."""
    s = P.format_references([{"fact": "x", "url": "https://www.newspim.com/news/view/20250313000298",
                              "date": None, "publisher": "뉴스핌"}])
    assert "2025년 뉴스핌" in s
    assert "연도 미상" not in s


def nl_join(lines):
    return chr(10).join(lines)


def test_format_references_groups_same_url():
    """같은 기사에서 뽑은 사실은 한 출처 아래로 묶는다 (출처 반복 인용 방지)."""
    facts = [{"fact": "a", "url": "https://n.kr/1", "date": "2025-01-01", "publisher": "뉴스핌"},
             {"fact": "b", "url": "https://n.kr/1", "date": None, "publisher": "뉴스핌"},
             {"fact": "c", "url": "https://n.kr/2", "date": "2024-01-01", "publisher": "한겨레"}]
    s = P.format_references(facts)
    # 머리말(sections.md)에도 예시로 매체명이 들어가므로 목록 본문만 센다
    body = nl_join(s.split(chr(10))[1:])
    assert body.count("뉴스핌") == 1
    assert "- a" in s and "- b" in s and "- c" in s
    assert "2. 2024년 한겨레" in s


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
    assert "2026년 서울시" in out["references"]
    assert len(client.calls) == 2                                   # 검색어 1회 + 추출 1회

    again = await P.run_research(FakeClient([]), researcher, form, "q2", P.ResearchConfig(), cache=cache, fetch=fetch)
    assert again["cached"] is True and again["facts"] == out["facts"]   # 두 번째는 모델 호출 0회


def test_build_context_injects_references():
    from backend.pipeline import assemble
    ctx = assemble.build_context({"track": "tech", "idea": "x", "references": [
        {"fact": "폐기율 23%", "quote": "q", "url": "https://k.go.kr", "date": "2026-01-01", "publisher": "서울시"}]})
    assert "[웹 참고자료" in ctx and "2026년 서울시" in ctx and "- 폐기율 23%" in ctx
    assert "[웹 참고자료" not in assemble.build_context({"track": "tech", "idea": "x"})


def test_build_context_skips_references_for_short_questions():
    """Q1·Q10 처럼 90자로 끝나는 문항에는 참고자료를 넣지 않는다 (인용할 자리가 없다)."""
    from backend.pipeline import assemble
    refs = [{"fact": "폐기율 23%", "quote": "q", "url": "https://k.go.kr", "date": "2026-01-01", "publisher": "서울시"}]
    for qid in ("q1", "q10"):
        ctx = assemble.build_context({"track": "tech", "idea": "x", "question_id": qid, "references": refs})
        assert "[웹 참고자료" not in ctx, f"{qid} 에 참고자료가 들어갔다"
    ctx = assemble.build_context({"track": "tech", "idea": "x", "question_id": "q2", "references": refs})
    assert "[웹 참고자료" in ctx
