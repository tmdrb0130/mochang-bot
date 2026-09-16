"""출처 편중 회귀 테스트. 네트워크·운영 데이터 접근 없이 검증한다."""
from types import SimpleNamespace

import pytest

from backend.rag import pipeline as P, allocate as A
from backend.rag.diversity import diverse_results, source_key
from backend.rag.research import DiskCache
from backend.pipeline.assemble import build_context


def row(url, kind="news", query="problem"):
    return {"url": url, "title": url, "source_type": kind, "query": query,
            "date": "2026-01-01", "snippet": "verified page content " * 5}


def fact(url, angle, text):
    return {"url": url, "angle": angle, "fact": text, "quote": text,
            "publisher": "publisher", "date": "2026-01-01"}


def test_selection_keeps_papers_statistics_and_other_queries():
    news = [row(f"https://news{i}.kr/a") for i in range(8)]
    paper = row("https://kci.go.kr/paper?id=1", "kci", "research")
    stats = row("https://kosis.kr/table?id=1", "stat", "market")
    service = row("https://service.kr/prices", "web", "pricing")
    selected = diverse_results(P.rank_results(news + [paper, stats, service]))[:6]
    assert all(r in selected for r in [paper, stats, service])
    assert len({r["query"] for r in selected}) == 4


def test_tracking_urls_do_not_count_as_new_sources():
    assert source_key("https://News.kr/a/?utm_source=test#x") == source_key("https://news.kr/a")
    assert source_key("https://kci.kr/p?id=1") != source_key("https://kci.kr/p?id=2")
    selected = diverse_results([row("https://news.kr/a"), row("https://news.kr/a?utm_source=x"),
                                row("https://other.kr/a")])
    assert len(selected) == 2


def test_initial_vector_pages_count_toward_domain_limit():
    initial = [row("https://news.kr/a")]
    selected = diverse_results([row("https://news.kr/b"), row("https://news.kr/c"),
                                row("https://other.kr/a")], 2, initial)
    assert len([r for r in selected if "news.kr" in r["url"]]) == 1
    assert selected[0]["url"] == "https://other.kr/a"


def test_one_article_is_not_spread_over_three_questions():
    refs = [fact("https://news.kr/a", "problem", "customer difficulty"),
            fact("https://news.kr/a?utm_source=x", "competitor", "existing competitor"),
            fact("https://news.kr/a#pricing", "pricing", "subscription price"),
            fact("https://paper.kr/b", "problem", "research findings"),
            fact("https://service.kr/a", "competitor", "alternative service"),
            fact("https://prices.kr/a", "pricing", "actual fee")]
    allocation = A.assign(refs)
    for url in {source_key(f["url"]) for f in refs}:
        assert sum(any(source_key(f["url"]) == url for f in group) for group in allocation.values()) == 1
    assert refs[4] in allocation["q3_1"] and refs[5] in allocation["q3_2"]


def test_new_sources_are_selected_before_more_facts_from_same_article():
    refs = [fact("https://news.kr/a", "problem", f"fact {i}") for i in range(4)]
    refs += [fact("https://paper.kr/a", "problem", "paper result"),
             fact("https://stats.kr/a", "problem", "statistics result")]
    assert len({r["url"] for r in A.for_question(refs, "q2")}) == 3


def test_duplicate_fact_across_articles_is_not_repeated():
    refs = [fact("https://news.kr/a", "problem", "same claim"),
            fact("https://news.kr/b", "pricing", "same claim")]
    assert sum(map(len, A.assign(refs).values())) == 1


def test_merge_preserves_shared_sources_when_followup_has_many_facts():
    primary = [fact("https://news.kr/a", "problem", f"new fact {i}") for i in range(8)]
    shared = [fact(f"https://source{i}.kr/a", "trend", f"shared fact {i}") for i in range(4)]
    merged = P._merge_facts(primary, shared)
    assert len(merged) == 8 and all(f in merged for f in shared)
    assert merged[0] == primary[0]


class Search:
    last_backend = "fake"
    def __init__(self, rows):
        self.rows, self.calls = rows, []
    async def search(self, query, n):
        self.calls.append(query)
        return self.rows[:n]


class Store:
    def __init__(self, rows):
        self.rows = rows
    def query(self, query, n):
        return self.rows
    def is_sufficient(self, rows):
        return bool(rows)


async def fetch(url, timeout=15):
    return "verified page content " * 5


@pytest.mark.asyncio
@pytest.mark.parametrize("single_domain", [True, False])
async def test_biased_vector_hits_do_not_suppress_web_search(monkeypatch, single_domain):
    stored = [{**row(f"https://{'same' if single_domain else 'news' + str(i)}.kr/{i}"),
               "score": 0.9, "text": "stored page content " * 5} for i in range(6)]
    monkeypatch.setattr(P, "get_vector_store", lambda cfg: Store(stored))
    monkeypatch.setattr(P, "schedule_index_pages", lambda *args: None)
    paper = row("https://kci.go.kr/paper", "kci", "research")
    paper["no_fetch"] = True
    search = Search([paper])
    cfg = P.ResearchConfig(vectorstore_enabled=True, max_pages_to_extract=6)
    _, pages = await P.collect_pages(search, ["research"], cfg, fetch=fetch)
    assert search.calls and any(p["source_type"] == "kci" for p in pages)
    assert len(pages) <= 6


@pytest.mark.asyncio
async def test_vector_reuse_respects_followup_skip_urls(monkeypatch):
    url = "https://news.kr/a"
    stored = [{**row(url), "score": 0.9, "text": "stored page content " * 5}]
    monkeypatch.setattr(P, "get_vector_store", lambda cfg: Store(stored))
    monkeypatch.setattr(P, "schedule_index_pages", lambda *args: None)
    search = Search([row(url), row("https://other.kr/b")])
    cfg = P.ResearchConfig(vectorstore_enabled=True, max_pages_to_extract=3)
    _, pages = await P.collect_pages(search, ["research"], cfg, fetch=fetch, skip_urls={url}, max_pages=1)
    assert len(pages) == 1 and pages[0]["url"] == "https://other.kr/b"


@pytest.mark.asyncio
async def test_followup_only_sees_facts_allocated_to_its_question():
    captured = []
    class Client:
        async def complete(self, system, user, **kwargs):
            captured.append(user)
            return SimpleNamespace(text="[]")
    shared = {"facts": [fact("https://a.kr", "problem", "problem-only"),
                        fact("https://b.kr", "pricing", "price-only")]}
    await P.generate_followup_queries(Client(), {"track": "tech", "idea": "sample"},
                                     {"id": "q3_2"}, shared, P.ResearchConfig())
    assert "price-only" in captured[0] and "problem-only" not in captured[0]


@pytest.mark.asyncio
async def test_shared_assignment_survives_different_followup_results(monkeypatch, tmp_path):
    shared = [fact("https://a.kr", "problem", "shared problem"),
              fact("https://b.kr", "competitor", "shared competitor"),
              fact("https://c.kr", "pricing", "shared price")]
    async def common(*args, **kwargs):
        return {"facts": shared, "pages": [], "queries": ["common"]}
    async def queries(*args, **kwargs):
        return ["followup"]
    async def collect(*args, **kwargs):
        return [], []
    async def extract(client, form, meta, pages, cfg):
        # 각도가 불명확해도 이 문항에서 찾은 새 자료가 다른 문항으로 재배분되지 않는다.
        return [fact("https://new.kr/" + meta["id"], "trend", "new " + meta["id"])]
    monkeypatch.setattr(P, "run_idea_research", common)
    monkeypatch.setattr(P, "generate_followup_queries", queries)
    monkeypatch.setattr(P, "collect_pages", collect)
    monkeypatch.setattr(P, "extract_facts", extract)
    form = {"track": "tech", "idea": "sample"}
    for qid, own, other in [("q2", "shared problem", "shared competitor"),
                            ("q3_1", "shared competitor", "shared price"),
                            ("q3_2", "shared price", "shared problem")]:
        result = await P.run_research(None, Search([]), form, qid, cache=DiskCache(tmp_path))
        context = build_context({**form, "question_id": qid, "references": result["facts"]})
        assert own in context and other not in context and "new " + qid in context


def test_new_fact_cache_does_not_reuse_old_biased_results():
    assert P._cache_key({"track": "tech", "idea": "sample"}, "q2").startswith("research-facts-v2-diverse|")
