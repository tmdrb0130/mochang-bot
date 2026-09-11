"""조사 파이프라인 — 모델·검색·HTTP 전부 가짜. 특히 '지어낸 quote 는 버려진다'를 검증."""
import asyncio
import json
import threading

import pytest

from backend.rag import pipeline as P
from backend.rag import research as R
from backend.rag import opendata as O
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


# ── 작업 39: 사실 추출을 페이지별 호출로 분리 ──

PAGE_B_TEXT = ("경기도 조사에서 반찬가게 사장 10명 중 7명이 재고 관리에 어려움을 겪는다고 답했다. "
               "같은 조사에서 폐기 비용은 월평균 38만 원으로 집계됐다.")


def _page(url, text, title="문서", kind="news"):
    return {"url": url, "title": title, "date": "2026-03-01", "text": text, "source_type": kind}


@pytest.mark.asyncio
async def test_extract_facts_calls_the_model_once_per_page_with_only_that_page(form):
    """페이지 하나당 한 번씩 부른다 — 프롬프트에 그 페이지 본문만 들어가고, 본문은 자르지 않는다."""
    pages = [_page("https://kostat.go.kr/x", PAGE_TEXT), _page("https://gg.go.kr/y", PAGE_B_TEXT)]
    a = json.dumps([{"fact": "하루 폐기율 23%", "quote": "하루 폐기율은 평균 23%로 나타났다",
                     "source_title": "통계", "url": "https://kostat.go.kr/x", "date": "2026-03-01"}], ensure_ascii=False)
    b = json.dumps([{"fact": "10명 중 7명이 재고 관리에 어려움", "quote": "10명 중 7명이 재고 관리에 어려움을 겪는다고 답했다",
                     "source_title": "경기도", "url": "https://gg.go.kr/y", "date": None}], ensure_ascii=False)
    client = FakeClient([a, b])
    facts = await P.extract_facts(client, form, {"label": "Q2", "title": "배경", "body": ""}, pages, P.ResearchConfig())

    assert len(client.calls) == 2
    assert client.calls[0]["user"].count("<문서 1>") == 1 and "gg.go.kr" not in client.calls[0]["user"]
    assert PAGE_TEXT in client.calls[0]["user"]                  # 본문을 자르지 않는다
    assert PAGE_B_TEXT in client.calls[1]["user"] and "kostat" not in client.calls[1]["user"]
    assert [f["fact"] for f in facts] == ["하루 폐기율 23%", "10명 중 7명이 재고 관리에 어려움"]


@pytest.mark.asyncio
async def test_extract_facts_survives_one_bad_page_and_dedupes_same_quote(form):
    """한 페이지가 깨진 JSON·오류여도 나머지 페이지의 사실은 남고, 같은 quote 는 한 번만 담는다."""
    same = json.dumps([{"fact": "하루 폐기율 23%", "quote": "하루 폐기율은 평균 23%로 나타났다",
                        "source_title": "통계", "url": "https://a.kr/1", "date": None}], ensure_ascii=False)
    pages = [_page("https://a.kr/1", PAGE_TEXT), _page("https://b.kr/2", PAGE_TEXT), _page("https://c.kr/3", PAGE_TEXT)]
    client = FakeClient(["JSON 아님", same, same])
    facts = await P.extract_facts(client, form, {}, pages, P.ResearchConfig())
    assert len(client.calls) == 3 and [f["fact"] for f in facts] == ["하루 폐기율 23%"]


@pytest.mark.asyncio
async def test_extract_facts_runs_at_most_three_pages_at_a_time(form):
    """조사 워커 안에서 페이지 병렬은 최대 3장 — 워커 수를 낮춘 근거(config.yaml)와 짝이다."""
    live, peak = 0, 0

    class SlowClient:
        def __init__(self):
            self.calls = 0

        async def complete(self, system, user, model=None):
            nonlocal live, peak
            live += 1
            peak = max(peak, live)
            self.calls += 1
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            live -= 1
            return LLMResult(text="[]", model="fake")

    client = SlowClient()
    pages = [_page(f"https://ex{i}.kr/", PAGE_TEXT) for i in range(8)]
    await P.extract_facts(client, form, {}, pages, P.ResearchConfig())
    assert client.calls == 8 and peak == P.EXTRACT_CONCURRENCY == 3


@pytest.mark.asyncio
async def test_extract_facts_takes_turns_between_pages_up_to_the_total_cap(form):
    """합칠 때 페이지를 번갈아 담는다 — 한 문서가 8칸을 다 차지하면 근거가 한쪽으로 쏠린다."""
    def reply(prefix, quotes):
        return json.dumps([{"fact": f"{prefix}{i}", "quote": q, "source_title": "t", "url": f"https://{prefix}.kr/"}
                           for i, q in enumerate(quotes)], ensure_ascii=False)

    pages = [_page("https://a.kr/", PAGE_TEXT), _page("https://b.kr/", PAGE_B_TEXT)]
    client = FakeClient([
        reply("a", ["하루 폐기율은 평균 23%로 나타났다", "1인 가구의 62%가 주 3회 이상 배달을 이용한다",
                    "반찬가게 120곳과 1인 가구 800명을 대상으로 진행됐다"]),
        reply("b", ["10명 중 7명이 재고 관리에 어려움을 겪는다고 답했다", "폐기 비용은 월평균 38만 원으로 집계됐다"]),
    ])
    facts = await P.extract_facts(client, form, {}, pages, P.ResearchConfig())
    assert [f["fact"] for f in facts] == ["a0", "b0", "a1", "b1", "a2"]    # 각 페이지의 1순위부터 번갈아
    assert len(facts) <= P.MAX_FACTS


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

    # 예전 경로(문항마다 처음부터 조사) — share_idea_research: false 로 되돌렸을 때의 동작
    cfg = P.ResearchConfig(max_queries=2, share_idea_research=False)
    out = await P.run_research(client, researcher, form, "q2", cfg, cache=cache, fetch=fetch)
    assert out["queries"] == ["반찬가게 폐기율", "1인 가구 식사"]
    assert out["facts"][0]["fact"] == "폐기율 23%" and out["cached"] is False
    assert "2026년 서울시" in out["references"]
    assert len(client.calls) == 2                                   # 검색어 1회 + 추출 1회

    again = await P.run_research(FakeClient([]), researcher, form, "q2", cfg, cache=cache, fetch=fetch)
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


@pytest.mark.asyncio
async def test_collect_pages_fetches_only_as_many_pages_as_needed(tmp_path):
    """RESEARCH_PLAN 1단계 — 예전엔 max_pages_to_extract*2 를 받아 절반을 버렸다. 이제 필요한 만큼만 fetch."""
    results = [R.make_result(f"제목{i}", f"https://ex{i}.com/a", "스니펫") for i in range(20)]
    researcher = R.Researcher(backends=[("fake", lambda q, n: _aret(results))], cache=R.DiskCache(tmp_path))
    fetched = []

    async def fetch(url, timeout):
        fetched.append(url)
        return PAGE_TEXT

    await P.collect_pages(researcher, ["q1"], P.ResearchConfig(max_pages_to_extract=6), fetch=fetch)
    assert len(fetched) == 6                                     # 12 가 아니라 6

    fetched.clear()
    await P.collect_pages(researcher, ["q1"], P.ResearchConfig(max_pages_to_extract=6, max_pages_to_fetch=8),
                          fetch=fetch)
    assert len(fetched) == 8                                     # 실패 대비 여유는 설정으로만


# ── 2단계: 아이디어당 1회 조사 + 문항별 보완 검색 ──

def _next(queue, fallback):
    """큐에서 하나 꺼내고, 다 쓰면 마지막 값을 계속 쓴다."""
    if not queue:
        return fallback
    return queue.pop(0) if len(queue) > 1 else queue[0]


class ScriptedClient:
    """system 프롬프트를 보고 어떤 단계(검색어/보완 검색어/사실 추출)인지 구분해 답하는 가짜 모델.

    세 인자 모두 '호출 순서대로 꺼내 쓰는 큐' — 문항마다 다른 검색어·사실이 나오는 실제 상황을 흉내낸다.
    실제 모델 호출은 없다.
    """

    def __init__(self, queries, facts, followups):
        self.queries = list(queries)
        self.facts = list(facts)
        self.followups = list(followups)
        self.calls = []
        self._facts_used = False        # 작업 39: 사실 추출은 페이지마다 호출된다 —
        #  한 '조사 라운드' 안의 페이지들은 같은 대본을 보고, 다음 라운드(검색어·보완 검색어 호출)에서 대본이 넘어간다.

    def stage(self, system: str) -> str:
        if "이미 조사가 한 번 끝난 상태" in system:
            return "followup"
        if "웹 검색창에 그대로 넣을" in system:
            return "queries"
        return "facts"

    async def complete(self, system, user, model=None):
        stage = self.stage(system)
        self.calls.append(stage)
        if stage in ("queries", "followup"):
            if self._facts_used:                 # 새 조사 라운드 시작 → 사실 대본을 다음 것으로
                _next(self.facts, "[]")
                self._facts_used = False
        if stage == "queries":
            return LLMResult(text=json.dumps(_next(self.queries, []), ensure_ascii=False), model="fake")
        if stage == "followup":
            return LLMResult(text=json.dumps(_next(self.followups, []), ensure_ascii=False), model="fake")
        self._facts_used = True
        return LLMResult(text=(self.facts[0] if self.facts else "[]"), model="fake")


def facts_json(*rows) -> str:
    return json.dumps([{"fact": f, "quote": q, "source_title": "서울시 조사", "url": u,
                        "date": "2026-02-01", "publisher": "서울시"} for f, q, u in rows], ensure_ascii=False)


# quote 는 PAGE_TEXT 안에 실제로 있는 문장이어야 _verify_quotes 를 통과한다.
SHARED_FACTS = facts_json(("반찬가게 하루 폐기율 23%", "하루 폐기율은 평균 23%", "https://kostat.go.kr/x"),
                          ("1인 가구 62%가 주 3회 이상 배달", "1인 가구의 62%가 주 3회 이상 배달을 이용한다",
                           "https://kostat.go.kr/x"))
FOLLOWUP_FACTS = facts_json(("표본은 반찬가게 120곳", "반찬가게 120곳과 1인 가구 800명을 대상으로 진행됐다",
                             "https://seoul.go.kr/y"))


def counting_researcher(tmp_path, counters):
    """검색어당 결과를 주는 가짜 백엔드 — 검색 횟수를 센다. 결과 URL 은 검색어마다 다르다."""
    async def backend(query, max_results):
        counters["search"] += 1
        n = abs(hash(query)) % 100000
        return [R.make_result(f"{query} 결과{i}", f"https://ex{n}-{i}.co.kr/a", "요약", "2026-03-01", "news")
                for i in range(max_results)]
    return R.Researcher(backends=[("fake", backend)], cache=R.DiskCache(tmp_path / "search"))


def counting_fetch(counters):
    async def fetch(url, timeout=15):
        counters["fetch"] += 1
        return PAGE_TEXT
    return fetch


@pytest.mark.asyncio
async def test_run_research_reuses_shared_facts_and_only_searches_the_gap(tmp_path, form):
    counters = {"search": 0, "fetch": 0}
    researcher = counting_researcher(tmp_path, counters)
    client = ScriptedClient([["공통1", "공통2"]], [SHARED_FACTS, FOLLOWUP_FACTS], [["보완 검색어"]])
    cfg = P.ResearchConfig(max_queries=2, max_pages_to_extract=2, max_followup_queries=2, max_followup_pages=1)
    cache = R.DiskCache(tmp_path / "facts")

    out = await P.run_research(client, researcher, form, "q2", cfg, cache=cache, fetch=counting_fetch(counters))

    assert out["shared_queries"] == ["공통1", "공통2"]           # 아이디어 공통 조사에서 온 것
    assert out["followup_queries"] == ["보완 검색어"]             # 이 문항에서만 추가한 것
    assert out["queries"] == ["공통1", "공통2", "보완 검색어"]    # 기존 필드는 둘을 합쳐서 유지
    assert [f["fact"] for f in out["facts"]][0] == "표본은 반찬가게 120곳"   # 문항 전용 사실이 앞
    assert out["shared_facts_used"] == 2 and len(out["facts"]) == 3          # 문항 1 + 공통 2
    assert out["references"] and out["cached"] is False
    # 공통 조사: 검색어 1 + 페이지 2장 추출 2 / 이 문항: 보완 검색어 1 + 페이지 1장 추출 1 (작업 39 — 페이지별 호출)
    assert client.calls == ["queries", "facts", "facts", "followup", "facts"]
    assert counters == {"search": 3, "fetch": 3}                       # 공통 2검색+2fetch, 보완 1검색+1fetch


@pytest.mark.asyncio
async def test_run_research_does_not_search_when_shared_research_is_enough(tmp_path, form):
    """모델이 '충분하다'(빈 배열)고 답하면 이 문항의 외부 요청은 0건, 모델 호출도 1회뿐."""
    counters = {"search": 0, "fetch": 0}
    researcher = counting_researcher(tmp_path, counters)
    client = ScriptedClient([["공통1"]], [SHARED_FACTS], [[]])
    cfg = P.ResearchConfig(max_queries=1, max_results_per_query=1, max_pages_to_extract=1, max_followup_pages=1)
    cache = R.DiskCache(tmp_path / "facts")
    fetch = counting_fetch(counters)

    await P.run_idea_research(client, researcher, form, cfg, cache=cache, fetch=fetch)   # 공통 조사 먼저
    before = dict(counters)
    client.calls.clear()

    out = await P.run_research(client, researcher, form, "q8", cfg, cache=cache, fetch=fetch)
    assert counters == before                                    # 검색·fetch 추가 0건
    assert client.calls == ["followup"]                          # 사실 추출도 안 한다 (새 페이지가 없으므로)
    assert out["followup_queries"] == [] and out["shared_facts_used"] == 2
    assert [f["fact"] for f in out["facts"]] == [f["fact"] for f in json.loads(SHARED_FACTS)]


@pytest.mark.asyncio
async def test_run_research_does_not_refetch_pages_from_shared_research(tmp_path, form):
    """보완 검색 결과가 공통 조사에서 이미 본 페이지면 다시 받지 않는다."""
    same = [R.make_result("같은 글", "https://ex.co.kr/a", "요약", "2026-03-01", "news"),
            R.make_result("새 글", "https://new.co.kr/b", "요약", "2026-03-02", "news")]
    counters = {"search": 0, "fetch": 0}

    async def backend(query, max_results):
        counters["search"] += 1
        return same[:max_results]

    researcher = R.Researcher(backends=[("fake", backend)], cache=R.DiskCache(tmp_path / "s"))
    client = ScriptedClient([["공통1"]], [SHARED_FACTS, FOLLOWUP_FACTS], [["보완"]])
    cfg = P.ResearchConfig(max_queries=1, max_results_per_query=1, max_pages_to_extract=1,
                           max_followup_queries=1, max_followup_pages=2)
    cache = R.DiskCache(tmp_path / "f")
    fetch = counting_fetch(counters)

    await P.run_idea_research(client, researcher, form, cfg, cache=cache, fetch=fetch)   # ex.co.kr/a 만 받음
    fetched_in_shared = counters["fetch"]
    cfg2 = P.ResearchConfig(max_queries=1, max_results_per_query=2, max_pages_to_extract=1,
                            max_followup_queries=1, max_followup_pages=2)
    out = await P.run_research(client, researcher, form, "q2", cfg2, cache=cache, fetch=fetch)

    assert fetched_in_shared == 1
    assert counters["fetch"] == 2                                # 새 글 1건만 추가로 받음
    assert [p["url"] for p in out["pages"]] == ["https://ex.co.kr/a", "https://new.co.kr/b"]


QUESTION_IDS = ["q1", "q2", "q3_1", "q3_2", "q4_1", "q4_2", "q7_1", "q8", "q10"]


async def _one_person_scenario(tmp_path, form, share: bool) -> dict:
    """1인 시나리오 — 인테이크(아이디어 조사) + 문항 9개. 외부 요청(검색+fetch) 수를 센다.

    0단계 실측대로 문항마다 검색어가 서로 다르다고 본다 (문자열 중복률 3~7% → 검색 캐시는 거의 안 먹는다)."""
    counters = {"search": 0, "fetch": 0}
    researcher = counting_researcher(tmp_path, counters)
    cfg = P.ResearchConfig(max_queries=4, max_results_per_query=6, max_pages_to_extract=6,
                           max_pages_to_fetch=6, share_idea_research=share,
                           max_followup_queries=2, max_followup_pages=3)
    cache = R.DiskCache(tmp_path / f"facts-{share}")
    fetch = counting_fetch(counters)
    client = ScriptedClient(
        [[f"공통{i}" for i in range(4)]] + [[f"{qid} 검색{i}" for i in range(4)] for qid in QUESTION_IDS],
        [SHARED_FACTS, FOLLOWUP_FACTS],
        # 문항마다 다른 각도의 보완 검색어 2개 — 모델이 상한을 꽉 채워 답하는 최악의 경우
        [[f"{qid} 보완A", f"{qid} 보완B"] for qid in QUESTION_IDS])

    await P.run_idea_research(client, researcher, form, cfg, cache=cache, fetch=fetch)   # 인테이크
    for qid in QUESTION_IDS:
        await P.run_research(client, researcher, form, qid, cfg, cache=cache, fetch=fetch)
    return {**counters, "총 외부 요청": counters["search"] + counters["fetch"], "모델 호출": len(client.calls)}


@pytest.mark.asyncio
async def test_one_person_scenario_external_requests_before_and_after(tmp_path, form):
    """RESEARCH_PLAN 2단계 완료 조건 — 1인 시나리오의 외부 요청 수가 실제로 줄었는지 센다."""
    before = await _one_person_scenario(tmp_path / "before", form, share=False)
    after = await _one_person_scenario(tmp_path / "after", form, share=True)

    # 예전: 조사 10회 × (검색어 4 + 페이지 6) = 100건. 모델 호출은 10 × (검색어 1 + 페이지 6) = 70
    # (작업 39 전에는 페이지 6장을 한 번에 넣어 20회였다 — 외부 요청 수는 그대로다)
    assert before == {"search": 40, "fetch": 60, "총 외부 요청": 100, "모델 호출": 70}
    # 지금: 공통 조사 1회(4+6) + 문항 9개 × (보완 검색어 2 + 페이지 3) = 55건
    # 모델 호출은 공통(1+6) + 문항 9 × (보완 1 + 페이지 3) = 43
    assert after == {"search": 22, "fetch": 33, "총 외부 요청": 55, "모델 호출": 43}
    assert after["총 외부 요청"] <= before["총 외부 요청"] * 0.6


@pytest.mark.asyncio
async def test_one_person_scenario_when_shared_research_covers_everything(tmp_path, form):
    """보완 검색이 필요 없다고 모델이 판단하면(0단계 실측 기준 문항의 36~58%) 요청은 공통 조사 10건뿐."""
    counters = {"search": 0, "fetch": 0}
    researcher = counting_researcher(tmp_path, counters)
    cfg = P.ResearchConfig(max_queries=4, max_pages_to_extract=6, max_pages_to_fetch=6)
    cache = R.DiskCache(tmp_path / "facts")
    client = ScriptedClient([[f"공통{i}" for i in range(4)]], [SHARED_FACTS], [[]])
    fetch = counting_fetch(counters)

    await P.run_idea_research(client, researcher, form, cfg, cache=cache, fetch=fetch)
    for qid in QUESTION_IDS:
        await P.run_research(client, researcher, form, qid, cfg, cache=cache, fetch=fetch)
    assert counters["search"] + counters["fetch"] == 10
    # 사실 추출은 공통 조사 때만 — 페이지 6장이라 호출 6회(작업 39), 문항 9개는 한 번도 부르지 않는다
    assert client.calls.count("facts") == 6


# ── 도메인별 상한 + 정형 API 결과(no_fetch) 처리 ──

@pytest.mark.asyncio
async def test_collect_pages_caps_results_per_domain(tmp_path):
    """근거가 한 신문사에 쏠리지 않게 같은 도메인은 상한까지만 (작업 7)."""
    results = ([R.make_result(f"한 신문사 {i}", f"https://news.co.kr/{i}", "요약") for i in range(5)]
               + [R.make_result("다른 곳", "https://other.co.kr/1", "요약")])
    researcher = R.Researcher(backends=[("fake", lambda q, n: _aret(results))], cache=R.DiskCache(tmp_path))
    fetched = []

    async def fetch(url, timeout=15):
        fetched.append(url)
        return PAGE_TEXT

    cfg = P.ResearchConfig(max_results_per_query=6, max_pages_to_extract=6, max_pages_per_domain=2)
    _, pages = await P.collect_pages(researcher, ["q"], cfg, fetch=fetch)
    assert sum(1 for u in fetched if "news.co.kr" in u) == 2          # 5건 중 2건만
    assert "https://other.co.kr/1" in fetched
    assert len(pages) == 3

    cfg0 = P.ResearchConfig(max_results_per_query=6, max_pages_to_extract=6, max_pages_per_domain=0)
    fetched.clear()
    await P.collect_pages(researcher, ["q"], cfg0, fetch=fetch)
    assert len(fetched) == 6                                          # 0 이면 제한 없음


@pytest.mark.asyncio
async def test_collect_pages_uses_stat_snippets_without_fetching(tmp_path):
    """정형 API 결과는 HTTP fetch 없이 snippet 을 본문으로 쓴다 — 도메인 상한도 적용받지 않는다."""
    stat = O.stat_result("전국 음식 업종 상가업소 수", "https://sg.sbiz.or.kr/",
                         "소상공인시장진흥공단 상권정보 기준 전국 '음식' 업종 상가업소는 840,255개다 (2026년 06월 기준 자료).",
                         "202606")
    stat2 = O.stat_result("통계표", "https://kosis.kr/statHtml/statHtml.do?orgId=101&tblId=T",
                          "국가데이터처 「행정구역별 인구수」 최신 수치. 전국 총인구수: 51117378명 (2025)", "2025")
    web = [R.make_result(f"기사 {i}", f"https://news.co.kr/{i}", "요약 " * 20) for i in range(3)]
    researcher = R.Researcher(backends=[("fake", lambda q, n: _aret([stat, stat2] + web))],
                              cache=R.DiskCache(tmp_path))
    fetched = []

    async def fetch(url, timeout=15):
        fetched.append(url)
        return PAGE_TEXT

    cfg = P.ResearchConfig(max_results_per_query=6, max_pages_to_extract=6, max_pages_per_domain=1)
    _, pages = await P.collect_pages(researcher, ["음식 업종 점포 수"], cfg, fetch=fetch)

    assert not any("sbiz" in u or "kosis" in u for u in fetched)      # 정형 결과는 안 받아온다
    assert sum(1 for u in fetched if "news.co.kr" in u) == 1          # 웹 결과만 도메인 상한 적용
    by_url = {p["url"]: p for p in pages}
    assert "840,255개" in by_url["https://sg.sbiz.or.kr/"]["text"]     # snippet 이 본문으로
    assert by_url["https://sg.sbiz.or.kr/"]["from_snippet"] is True


@pytest.mark.asyncio
async def test_researcher_merges_extra_sources_ahead_of_web_results(tmp_path):
    """extras(정형 API)는 폴백이 아니라 항상 같이 물어보고 웹 결과 앞에 붙는다."""
    web = [R.make_result("웹 기사", "https://news.co.kr/1", "요약")]
    stat = O.stat_result("통계", "https://kosis.kr/x", "충분히 긴 사실 문장. " * 3)

    async def extra(query, n):
        return [stat]

    r = R.Researcher(backends=[("ddgs", lambda q, n: _aret(web))], extras=[("opendata", extra)],
                     cache=R.DiskCache(tmp_path))
    out = await r.search("1인 가구 통계")
    assert [x["url"] for x in out] == ["https://kosis.kr/x", "https://news.co.kr/1"]
    assert r.last_backend == "ddgs"

    again = await r.search("1인 가구 통계")                            # 캐시에도 합쳐진 결과가 들어간다
    assert [x["url"] for x in again] == [x["url"] for x in out] and r.last_backend == "cache"


@pytest.mark.asyncio
async def test_researcher_returns_extras_even_when_all_web_backends_fail(tmp_path):
    """웹 검색이 전부 죽어도 정형 API 결과가 있으면 그것만이라도 준다."""
    async def extra(query, n):
        return [O.stat_result("통계", "https://kosis.kr/x", "충분히 긴 사실 문장. " * 3)]

    async def dead(query, n):
        raise RuntimeError("차단")

    r = R.Researcher(backends=[("ddgs", dead)], extras=[("opendata", extra)], cache=R.DiskCache(tmp_path))
    out = await r.search("1인 가구 통계")
    assert [x["url"] for x in out] == ["https://kosis.kr/x"] and r.last_backend == "opendata"


@pytest.mark.asyncio
async def test_extra_source_failure_does_not_break_web_search(tmp_path):
    web = [R.make_result("웹 기사", "https://news.co.kr/1", "요약")]

    async def broken(query, n):
        raise RuntimeError("정형 소스 오류")

    r = R.Researcher(backends=[("ddgs", lambda q, n: _aret(web))], extras=[("opendata", broken)],
                     cache=R.DiskCache(tmp_path))
    out = await r.search("질의")
    assert [x["url"] for x in out] == ["https://news.co.kr/1"]
    assert "opendata: RuntimeError" in r.last_error


# ── 작업 9 연결: 조사해온 페이지를 벡터DB 에 색인 (기본 꺼짐, 색인만) ──

class FakeStore:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail
        self.threads = []          # 어느 스레드에서 돌았는지 (이벤트 루프 밖인지 확인용)

    def upsert_pages(self, pages):
        self.threads.append(threading.current_thread().name)
        if self.fail:
            raise RuntimeError("색인 실패")
        self.calls.append([p["url"] for p in pages])
        return len(pages)


@pytest.mark.asyncio
async def test_vectorstore_is_off_by_default(tmp_path, monkeypatch):
    """기본값에서는 저장소를 열지도 않는다 — llama_index import 도 임베딩 호출도 없다."""
    opened = []
    monkeypatch.setattr(P, "get_vector_store", lambda cfg: opened.append(cfg) or None)
    cfg = P.ResearchConfig(max_pages_to_extract=2)
    assert cfg.vectorstore_enabled is False

    researcher = R.Researcher(backends=[("fake", lambda q, n: _aret(fake_results("https://a.co.kr/1")))],
                              cache=R.DiskCache(tmp_path))
    _, pages = await P.collect_pages(researcher, ["q"], cfg, fetch=make_fetch({"https://a.co.kr/1": PAGE_TEXT}))
    # 조회·색인 두 군데서 물어보지만 None 이라 저장소를 열지도, 임베딩을 부르지도 않는다
    assert pages and opened and all(c is cfg for c in opened)


@pytest.mark.asyncio
async def test_collect_pages_indexes_pages_when_enabled(tmp_path, monkeypatch):
    store = FakeStore()
    monkeypatch.setattr(P, "get_vector_store", lambda cfg: store)
    cfg = P.ResearchConfig(max_pages_to_extract=2, vectorstore_enabled=True)
    researcher = R.Researcher(backends=[("fake", lambda q, n: _aret(fake_results("https://a.co.kr/1")))],
                              cache=R.DiskCache(tmp_path))

    _, pages = await P.collect_pages(researcher, ["q"], cfg, fetch=make_fetch({"https://a.co.kr/1": PAGE_TEXT}))
    assert pages[0]["text"] == PAGE_TEXT                    # 조사 결과는 색인을 기다리지 않고 먼저 나온다
    await P.wait_for_index_tasks()                          # 색인은 백그라운드다 (2026-09-11)
    assert store.calls == [["https://a.co.kr/1"]]          # 본문까지 있는 page dict 를 그대로 넘긴다


@pytest.mark.asyncio
async def test_indexing_failure_does_not_break_research(tmp_path, monkeypatch):
    """색인은 부가 기능이다 — 실패해도 조사 결과는 그대로 나와야 한다."""
    monkeypatch.setattr(P, "get_vector_store", lambda cfg: FakeStore(fail=True))
    cfg = P.ResearchConfig(max_pages_to_extract=2, vectorstore_enabled=True)
    researcher = R.Researcher(backends=[("fake", lambda q, n: _aret(fake_results("https://a.co.kr/1")))],
                              cache=R.DiskCache(tmp_path))
    _, pages = await P.collect_pages(researcher, ["q"], cfg, fetch=make_fetch({"https://a.co.kr/1": PAGE_TEXT}))
    assert [p["url"] for p in pages] == ["https://a.co.kr/1"]


@pytest.mark.asyncio
async def test_index_pages_runs_off_the_event_loop(monkeypatch):
    """LlamaIndex 는 동기다 — 이벤트 루프를 막지 않도록 **색인 전용 스레드**에서 돌린다 (2026-09-11).

    수단(to_thread)이 아니라 보장(루프 밖 · 전용 풀)을 검사한다. 공용 to_thread 풀을 쓰면
    동시 접속 때 락 대기 스레드가 풀을 점유해 본체 작업과 경합한다."""
    store = FakeStore()
    monkeypatch.setattr(P, "get_vector_store", lambda cfg: store)
    n = await P.index_pages([{"url": "https://a.co.kr/1", "text": PAGE_TEXT}],
                            P.ResearchConfig(vectorstore_enabled=True))
    assert n == 1
    assert store.threads, "upsert_pages 가 불리지 않았다"
    assert store.threads[0] != threading.current_thread().name      # 이벤트 루프 스레드가 아니다
    assert store.threads[0].startswith("mochang-index")             # 공용 풀이 아니라 전용 실행기


@pytest.mark.asyncio
async def test_schedule_index_pages_does_not_make_the_caller_wait(monkeypatch):
    """색인은 이번 답에 안 쓰이는 축적이다 — 호출부가 기다리지 않는다 (2026-09-11).

    태스크는 **전역 set 에 강한 참조**로 잡아 둔다. 안 그러면 GC 가 실행 도중에 수거한다."""
    started, release = asyncio.Event(), threading.Event()

    class SlowStore(FakeStore):
        def upsert_pages(self, pages):
            release.wait(5)                 # 색인이 느린 상황을 흉내
            return super().upsert_pages(pages)

    store = SlowStore()
    monkeypatch.setattr(P, "get_vector_store", lambda cfg: store)
    cfg = P.ResearchConfig(vectorstore_enabled=True)

    task = P.schedule_index_pages([{"url": "https://a.co.kr/1", "text": PAGE_TEXT}], cfg)
    assert task is not None and not task.done()          # 호출부는 즉시 돌아왔다
    assert task in P._index_tasks                        # 강한 참조를 잡고 있다
    release.set()
    assert await asyncio.wait_for(task, 5) == 1
    await asyncio.sleep(0)                               # done 콜백이 돌 틈
    assert task not in P._index_tasks                    # 끝나면 참조를 푼다
    assert store.calls == [["https://a.co.kr/1"]]
    started  # noqa: B018  (미사용 경고 방지)


@pytest.mark.asyncio
async def test_schedule_index_pages_logs_failure_instead_of_dying_silently(monkeypatch, caplog):
    """아무도 await 하지 않는 태스크라, 실패하면 로그로 남겨야 아무도 모르는 일이 안 생긴다."""
    def boom(cfg):
        raise RuntimeError("저장소 열기 실패")

    monkeypatch.setattr(P, "get_vector_store", boom)
    cfg = P.ResearchConfig(vectorstore_enabled=True)

    with caplog.at_level("WARNING", logger="mochang.research"):
        task = P.schedule_index_pages([{"url": "https://a.co.kr/1", "text": PAGE_TEXT}], cfg)
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0)

    assert task not in P._index_tasks
    assert any("색인 실패" in r.getMessage() for r in caplog.records), caplog.text


@pytest.mark.asyncio
async def test_collect_pages_does_not_wait_for_indexing(monkeypatch, tmp_path):
    """조사 결과는 색인이 끝나기 전에 돌아온다 — 학생을 붙잡지 않는다."""
    scheduled = {}
    monkeypatch.setattr(P, "schedule_index_pages", lambda pages, cfg: scheduled.setdefault("pages", pages))
    # 진짜 저장소를 열면 backend/.vectorstore(수백 MB)를 파싱해 테스트가 수십 초 걸린다 — 반드시 가짜로
    monkeypatch.setattr(P, "get_vector_store", lambda cfg: FakeStore())
    researcher = R.Researcher(backends=[("fake", lambda q, n: _aret(fake_results("https://a.co.kr/1")))],
                              cache=R.DiskCache(tmp_path))
    cfg = P.ResearchConfig(max_pages_to_extract=2, vectorstore_enabled=True,
                           vectorstore_use_for_search=False)
    _, pages = await P.collect_pages(researcher, ["q"], cfg,
                                     fetch=make_fetch({"https://a.co.kr/1": PAGE_TEXT}))
    assert [p["url"] for p in pages] == ["https://a.co.kr/1"]
    assert "pages" in scheduled          # 색인은 예약만 됐다 (await 되지 않았다)


def test_vectorstore_config_is_read_from_yaml():
    cfg = P.ResearchConfig.from_config({"research": {"vectorstore": {
        "enabled": True, "dir": "x/y", "embed_model": "", "min_score": 0.7, "min_hits": 3, "max_age_days": 365}}})
    assert cfg.vectorstore_enabled and cfg.vectorstore_dir == "x/y" and cfg.vectorstore_embed_model == ""
    assert (cfg.vectorstore_min_score, cfg.vectorstore_min_hits, cfg.vectorstore_max_age_days) == (0.7, 3, 365)
    assert P.ResearchConfig.from_config({}).vectorstore_enabled is False       # 설정이 없으면 꺼짐
    assert P.ResearchConfig.from_config({}).vectorstore_min_score == 0.6      # 기본 하한 (0.8 은 실측상 과함)


# ── 작업 9 조회 단계: 쌓아 둔 자료로 웹 검색을 대신하거나 보탠다 ──

class QueryStore:
    """벡터DB 대역 — 미리 정한 적중을 돌려주고, is_sufficient 는 min_score/min_hits 로 판정."""

    def __init__(self, hits, min_score=0.6, min_hits=2, fail=False):
        self.hits = hits
        self.min_score = min_score
        self.min_hits = min_hits
        self.fail = fail
        self.queries = []
        self.indexed = []

    def query(self, text, k=8):
        if self.fail:
            raise RuntimeError("조회 실패")
        self.queries.append(text)
        return list(self.hits)

    def is_sufficient(self, hits):
        return sum(1 for h in hits if float(h.get("score", 0)) >= self.min_score) >= self.min_hits

    def upsert_pages(self, pages):
        self.indexed.append([p["url"] for p in pages])
        return len(pages)


def hit(url, score, text=PAGE_TEXT):
    return {"url": url, "title": f"쌓아둔 {url}", "publisher": "서울시", "date": "2026-03-01",
            "source_type": "news", "text": text, "from_vectorstore": True, "score": score}


def searching_researcher(tmp_path, calls, results):
    async def backend(query, n):
        calls.append(query)
        return results
    return R.Researcher(backends=[("fake", backend)], cache=R.DiskCache(tmp_path))


@pytest.mark.asyncio
async def test_enough_stored_pages_skip_web_search_entirely(tmp_path, monkeypatch):
    """적중이 충분하면 그 조사에서 외부 요청은 0건이 된다."""
    store = QueryStore([hit("https://a.co.kr/1", 0.81), hit("https://b.co.kr/2", 0.75)])
    monkeypatch.setattr(P, "get_vector_store", lambda cfg: store)
    calls, fetched = [], []

    async def fetch(url, timeout=15):
        fetched.append(url)
        return PAGE_TEXT

    cfg = P.ResearchConfig(max_pages_to_extract=6, vectorstore_enabled=True)
    researcher = searching_researcher(tmp_path, calls, fake_results("https://web.co.kr/1"))
    unique, pages = await P.collect_pages(researcher, ["1인 가구 통계", "식품 폐기"], cfg, fetch=fetch)

    assert calls == [] and fetched == [] and unique == []          # 검색 0, fetch 0
    assert [p["url"] for p in pages] == ["https://a.co.kr/1", "https://b.co.kr/2"]
    assert all(p["from_vectorstore"] for p in pages) and pages[0]["text"] == PAGE_TEXT
    assert store.queries == ["1인 가구 통계", "식품 폐기"]           # 검색어마다 한 번씩 조회
    await P.wait_for_index_tasks()                                 # 색인은 백그라운드다 (2026-09-11)
    assert store.indexed == []                                     # 다시 색인하지 않는다


@pytest.mark.asyncio
async def test_partial_hits_are_reused_and_web_fills_the_rest(tmp_path, monkeypatch):
    """모자라면 웹 검색을 하되, 쌓아 둔 문서는 다시 받지 않고 근거로 같이 쓴다."""
    store = QueryStore([hit("https://a.co.kr/1", 0.9)], min_hits=3)     # 1건뿐 → 부족
    monkeypatch.setattr(P, "get_vector_store", lambda cfg: store)
    calls, fetched = [], []

    async def fetch(url, timeout=15):
        fetched.append(url)
        return PAGE_TEXT

    web = fake_results("https://a.co.kr/1", "https://new.co.kr/2")      # 첫 건은 이미 가진 문서
    cfg = P.ResearchConfig(max_pages_to_extract=4, vectorstore_enabled=True)
    researcher = searching_researcher(tmp_path, calls, web)
    _, pages = await P.collect_pages(researcher, ["질의"], cfg, fetch=fetch)

    assert calls == ["질의"]                                        # 웹 검색은 했고
    assert fetched == ["https://new.co.kr/2"]                       # 이미 가진 문서는 다시 안 받는다
    urls = [p["url"] for p in pages]
    assert urls[0] == "https://a.co.kr/1" and "https://new.co.kr/2" in urls
    await P.wait_for_index_tasks()                                  # 색인은 백그라운드다 (2026-09-11)
    assert store.indexed == [["https://new.co.kr/2"]]               # 새로 받은 것만 색인


@pytest.mark.asyncio
async def test_low_score_hits_are_not_reused(tmp_path, monkeypatch):
    """유사도가 하한 미만이면 근거로 쓰지 않는다 — 엉뚱한 주제가 끼어들면 안 된다."""
    store = QueryStore([hit("https://old.co.kr/1", 0.31)], min_hits=1, min_score=0.6)
    monkeypatch.setattr(P, "get_vector_store", lambda cfg: store)
    calls = []
    cfg = P.ResearchConfig(max_pages_to_extract=4, vectorstore_enabled=True, vectorstore_min_score=0.6)
    researcher = searching_researcher(tmp_path, calls, fake_results("https://web.co.kr/1"))
    _, pages = await P.collect_pages(researcher, ["질의"], cfg, fetch=make_fetch({"https://web.co.kr/1": PAGE_TEXT}))

    assert calls == ["질의"]                                        # 부족하니 웹 검색
    assert [p["url"] for p in pages] == ["https://web.co.kr/1"]     # 점수 낮은 적중은 버림


@pytest.mark.asyncio
async def test_query_failure_falls_back_to_web_search(tmp_path, monkeypatch):
    """조회가 죽어도 조사는 그대로 간다."""
    monkeypatch.setattr(P, "get_vector_store", lambda cfg: QueryStore([], fail=True))
    calls = []
    cfg = P.ResearchConfig(max_pages_to_extract=2, vectorstore_enabled=True)
    researcher = searching_researcher(tmp_path, calls, fake_results("https://web.co.kr/1"))
    _, pages = await P.collect_pages(researcher, ["질의"], cfg, fetch=make_fetch({"https://web.co.kr/1": PAGE_TEXT}))
    assert calls == ["질의"] and [p["url"] for p in pages] == ["https://web.co.kr/1"]


@pytest.mark.asyncio
async def test_use_for_search_off_keeps_indexing_only(tmp_path, monkeypatch):
    """use_for_search: false 면 조회는 안 하고 색인만 한다 (이전 단계 동작)."""
    store = QueryStore([hit("https://a.co.kr/1", 0.99)])
    monkeypatch.setattr(P, "get_vector_store", lambda cfg: store)
    calls = []
    cfg = P.ResearchConfig(max_pages_to_extract=2, vectorstore_enabled=True, vectorstore_use_for_search=False)
    researcher = searching_researcher(tmp_path, calls, fake_results("https://web.co.kr/1"))
    _, pages = await P.collect_pages(researcher, ["질의"], cfg, fetch=make_fetch({"https://web.co.kr/1": PAGE_TEXT}))

    assert store.queries == [] and calls == ["질의"]
    assert [p["url"] for p in pages] == ["https://web.co.kr/1"]
    await P.wait_for_index_tasks()                                  # 색인은 백그라운드다 (2026-09-11)
    assert store.indexed == [["https://web.co.kr/1"]]


def test_reuse_page_shape_matches_collect_pages_output():
    p = P.reuse_page(hit("https://a.co.kr/1", 0.7))
    assert set(p) >= {"url", "title", "date", "text", "source_type", "from_snippet", "from_vectorstore"}
    assert p["from_snippet"] is False and p["from_vectorstore"] is True and p["score"] == 0.7


def test_kci_pages_are_never_indexed():
    """KCI 이용 준수 2항 — source_type 이 kci 인 것도, 웹 검색이 물어온 kci.go.kr 페이지도 색인 대상이 아니다."""
    assert P.indexable({"url": "https://news.co.kr/a", "source_type": "news"}) is True
    assert P.indexable({"url": "https://x.com/a", "source_type": "kci"}) is False
    assert P.indexable({"url": "https://www.kci.go.kr/kciportal/ci/x", "source_type": "web"}) is False
    assert P.indexable({"url": "https://kci.go.kr/x", "source_type": "news"}) is False
    assert P.indexable({"url": "https://evil.com/kci.go.kr/x", "source_type": "web"}) is True   # 경로 위장은 도메인이 아니다
    assert P.indexable({"url": "https://a.co/x", "from_vectorstore": True}) is False


# ── 페이지 받기: 커넥션 재사용 + 전역 상한 (2026-09-08) ──

@pytest.mark.asyncio
async def test_fetch_page_reuses_one_client(monkeypatch):
    """호출마다 새 클라이언트를 만들지 않는다 — 장마다 TLS 악수를 다시 하던 것이 부하 때 늘어짐의 한 축이었다."""
    import httpx

    made = []
    real = httpx.AsyncClient

    class Counted(real):
        def __init__(self, *a, **k):
            made.append(k)
            super().__init__(*a, **k)

        async def get(self, url, **k):
            return httpx.Response(200, headers={"content-type": "text/html"}, text="<html>본문</html>")

    monkeypatch.setattr(httpx, "AsyncClient", Counted)
    monkeypatch.setattr(P, "_https", __import__("weakref").WeakKeyDictionary())
    monkeypatch.setattr(P.extract_proc, "extract", lambda html, **k: _aret("본문 텍스트"))
    for i in range(5):
        assert await P.fetch_page(f"https://ex.co.kr/{i}") == "본문 텍스트"
    assert len(made) == 1                                   # 다섯 장을 한 클라이언트로
    assert made[0]["limits"].max_connections == P.FETCH_GLOBAL


@pytest.mark.asyncio
async def test_fetch_page_respects_global_limit(monkeypatch):
    """동시에 받는 장수가 상한을 넘지 않고, 기다린 시간이 계측에 남는다."""
    import httpx

    live = {"now": 0, "peak": 0}

    class Slow(httpx.AsyncClient):
        async def get(self, url, **k):
            live["now"] += 1
            live["peak"] = max(live["peak"], live["now"])
            await asyncio.sleep(0.02)
            live["now"] -= 1
            return httpx.Response(200, headers={"content-type": "text/html"}, text="<html>본문</html>")

    monkeypatch.setattr(httpx, "AsyncClient", Slow)
    monkeypatch.setattr(P, "_https", __import__("weakref").WeakKeyDictionary())
    monkeypatch.setattr(P, "_sems", __import__("weakref").WeakKeyDictionary())
    monkeypatch.setattr(P, "FETCH_GLOBAL", 3)
    monkeypatch.setattr(P.extract_proc, "extract", lambda html, **k: _aret("본문"))
    from backend import timing
    monkeypatch.setattr(P, "timing", timing)
    timing._counts.clear()
    await asyncio.gather(*(P.fetch_page(f"https://ex.co.kr/{i}") for i in range(12)))
    assert live["peak"] <= 3                                # 상한을 넘지 않는다
    assert "fetch_wait_ms" in timing._counts                # 기다린 시간이 남는다
    timing._counts.clear()


# ── 실패를 실패로 전달 (2026-09-08, 리뷰 지적) ──

def _fresh(monkeypatch, **mod):
    """세마포어·클라이언트 캐시를 비우고 모듈 상한을 바꾼다."""
    import weakref
    monkeypatch.setattr(P, "_https", weakref.WeakKeyDictionary())
    monkeypatch.setattr(P, "_sems", weakref.WeakKeyDictionary())
    for k, v in mod.items():
        monkeypatch.setattr(P, k, v)


def _fake_client(monkeypatch, handler):
    """_http() 가 돌려줄 가짜 클라이언트 — get() 만 흉내 낸다."""
    class C:
        is_closed = False

        async def get(self, url, **k):
            return await handler(url)

    monkeypatch.setattr(P, "_http", lambda: C())


@pytest.mark.asyncio
async def test_fetch_page_raises_on_failure_instead_of_empty_string(monkeypatch):
    """예전엔 5xx·연결 실패가 전부 빈 문자열이었다 — '본문 없는 페이지' 와 구별이 안 됐다."""
    import httpx
    _fresh(monkeypatch, FETCH_GLOBAL=4, FETCH_PER_HOST=2)

    async def h(url):
        if "boom" in url:
            raise httpx.ReadTimeout("timed out")
        return httpx.Response(500, headers={"content-type": "text/html"}, text="서버 오류")

    _fake_client(monkeypatch, h)
    with pytest.raises(P.FetchFailed) as e1:
        await P.fetch_page("https://ex.co.kr/boom")
    assert e1.value.reason == "read"
    with pytest.raises(P.FetchFailed) as e2:
        await P.fetch_page("https://ex.co.kr/500")
    assert e2.value.reason == "status" and e2.value.detail == "500"


@pytest.mark.asyncio
async def test_fetch_page_returns_empty_when_there_is_nothing_to_read(monkeypatch):
    """비HTML·제외 확장자는 실패가 아니다 — 받긴 받았고 쓸 것이 없을 뿐이라 빈 문자열."""
    import httpx
    _fresh(monkeypatch, FETCH_GLOBAL=4, FETCH_PER_HOST=2)
    _fake_client(monkeypatch, lambda url: _aret(
        httpx.Response(200, headers={"content-type": "application/pdf"}, text="%PDF")))
    assert await P.fetch_page("https://ex.co.kr/x") == ""
    assert await P.fetch_page("https://ex.co.kr/a.zip") == ""      # 확장자로 거른 것


@pytest.mark.asyncio
async def test_fetch_page_retries_a_half_dead_keepalive_socket_once(monkeypatch):
    """상대 idle timeout 이 우리 keepalive 보다 짧으면 죽은 소켓을 집는다 — 새 소켓으로 한 번만 다시."""
    import httpx
    _fresh(monkeypatch, FETCH_GLOBAL=4, FETCH_PER_HOST=2)
    tries = {"n": 0}

    async def h(url):
        tries["n"] += 1
        if tries["n"] == 1:
            raise httpx.RemoteProtocolError("Server disconnected without sending a response.")
        return httpx.Response(200, headers={"content-type": "text/html"}, text="<html>본문</html>")

    _fake_client(monkeypatch, h)
    monkeypatch.setattr(P.extract_proc, "extract", lambda html, **k: _aret("본문 텍스트"))
    assert await P.fetch_page("https://ex.co.kr/a") == "본문 텍스트"
    assert tries["n"] == 2                                   # 정확히 한 번만 더


@pytest.mark.asyncio
async def test_fetch_page_does_not_retry_a_timeout(monkeypatch):
    """시간 초과는 다시 해도 기다림만 두 배다."""
    import httpx
    _fresh(monkeypatch, FETCH_GLOBAL=4, FETCH_PER_HOST=2)
    tries = {"n": 0}

    async def h(url):
        tries["n"] += 1
        raise httpx.ReadTimeout("timed out")

    _fake_client(monkeypatch, h)
    with pytest.raises(P.FetchFailed):
        await P.fetch_page("https://ex.co.kr/a")
    assert tries["n"] == 1


@pytest.mark.asyncio
async def test_fetch_page_limits_per_host_not_just_globally(monkeypatch):
    """차단은 전역 수가 아니라 호스트당 동시 연결에서 온다 — 한 호스트가 상한을 넘지 않으면서 다른 호스트는 안 막힌다."""
    import httpx, collections
    _fresh(monkeypatch, FETCH_GLOBAL=32, FETCH_PER_HOST=2)
    live = collections.Counter()
    peak = collections.Counter()

    async def h(url):
        d = P.domain(url)
        live[d] += 1
        peak[d] = max(peak[d], live[d])
        await asyncio.sleep(0.02)
        live[d] -= 1
        return httpx.Response(200, headers={"content-type": "text/html"}, text="<html>본문</html>")

    _fake_client(monkeypatch, h)
    monkeypatch.setattr(P.extract_proc, "extract", lambda html, **k: _aret("본문"))
    urls = [f"https://a.co.kr/{i}" for i in range(8)] + [f"https://b.co.kr/{i}" for i in range(8)]
    await asyncio.gather(*(P.fetch_page(u) for u in urls))
    assert peak["a.co.kr"] <= 2 and peak["b.co.kr"] <= 2      # 호스트마다 2장까지
    assert peak["a.co.kr"] == 2 and peak["b.co.kr"] == 2      # 서로를 막지는 않는다 (전역 32 는 여유)


@pytest.mark.asyncio
async def test_collect_pages_records_failed_fetches_and_marks_the_page(tmp_path, monkeypatch):
    """한 장이 실패해도 나머지는 살고, 몇 장이 왜 빠졌는지 로그에 남는다 — 전에는 아무 데도 안 남았다."""
    results = [
        R.make_result("살아있는 곳", "https://kostat.go.kr/ok", "스니펫 " * 20, "2026-01-01", "web"),
        R.make_result("죽은 곳", "https://dead.example.com/x",
                      "이 스니펫은 본문을 못 받아도 근거로 쓰일 만큼 깁니다. " * 3, "2026-01-01", "web"),
    ]
    researcher = R.Researcher(backends=[("fake", lambda q, n: _aret(results))], cache=R.DiskCache(tmp_path))

    async def fetch(url, timeout=15):
        if "dead" in url:
            raise P.FetchFailed(url, "connect", "getaddrinfo failed")
        return PAGE_TEXT

    rows = []
    monkeypatch.setattr(P.timing, "log", lambda event, **f: rows.append((event, f)))
    _u, pages = await P.collect_pages(researcher, ["q1"], P.ResearchConfig(max_pages_to_extract=5), fetch=fetch)

    ok = [p for p in pages if p["url"].endswith("/ok")][0]
    dead = [p for p in pages if "dead" in p["url"]][0]
    assert ok["text"] == PAGE_TEXT and not ok.get("fetch_failed")
    assert dead["fetch_failed"] is True and dead["from_snippet"] is True   # 스니펫으로 살아남되 표시가 붙는다
    fails = [f for e, f in rows if e == "fetch_failed"]
    assert fails and fails[0]["n"] == 1 and fails[0]["reasons"] == ["connect"]


@pytest.mark.asyncio
async def test_collect_pages_does_not_swallow_cancellation(tmp_path):
    """FetchFailed 가 아닌 예외(취소·프로그래밍 오류)는 그대로 올린다."""
    results = [R.make_result("x", "https://ex.co.kr/a", "스니펫 " * 20, "2026-01-01", "web")]
    researcher = R.Researcher(backends=[("fake", lambda q, n: _aret(results))], cache=R.DiskCache(tmp_path))

    async def fetch(url, timeout=15):
        raise ValueError("프로그래밍 오류")

    with pytest.raises(ValueError):
        await P.collect_pages(researcher, ["q1"], P.ResearchConfig(max_pages_to_extract=5), fetch=fetch)

