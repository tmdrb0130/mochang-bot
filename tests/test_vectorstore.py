"""vectorstore.py — 조사 페이지 축적·조회. 임베딩 모델도 네트워크도 안 쓴다 (OfflineEmbedding).

실제 Ollama(bge-m3)를 부르는 검증은 사람이 하고 결과는 PROGRESS.md 에 적는다.
"""
from datetime import date, timedelta

import pytest

from backend.rag.vectorstore import OfflineEmbedding, VectorStore

LONG = "1인 가구의 음식물 쓰레기 배출량은 연간 130킬로그램으로 조사됐다. 식품 폐기 비중이 특히 크다. "


def page(url, title="제목", text=None, date_=None, source_type="news", **kw):
    """collect_pages 가 만드는 page dict 와 같은 형태."""
    return {"url": url, "title": title, "text": text or LONG * 3,
            "date": date_, "source_type": source_type, **kw}


def ago(days):
    return (date.today() - timedelta(days=days)).isoformat()


@pytest.fixture
def store(tmp_path):
    return VectorStore(str(tmp_path / "vs"))


# ── 색인 ──

def test_upsert_returns_new_count_and_skips_same_url(store):
    pages = [page("https://a.kr/1"), page("https://b.kr/2")]
    assert store.upsert_pages(pages) == 2
    assert store.upsert_pages(pages) == 0                      # 같은 url 은 재색인하지 않는다
    assert store.indexed_urls() == {"https://a.kr/1", "https://b.kr/2"}


def test_upsert_treats_fragment_and_trailing_slash_as_same_url(store):
    assert store.upsert_pages([page("https://a.kr/1")]) == 1
    assert store.upsert_pages([page("https://a.kr/1/"), page("https://a.kr/1#본문")]) == 0


def test_upsert_skips_pages_without_url_or_body(store):
    assert store.upsert_pages([page(""), page("https://a.kr/1", text="짧다"), page("  ")]) == 0
    assert store.indexed_urls() == set()


def test_upsert_falls_back_to_snippet_when_text_missing(store):
    row = {"url": "https://a.kr/1", "title": "t", "snippet": LONG, "source_type": "web"}
    assert store.upsert_pages([row]) == 1


def test_upsert_handles_empty_input(store):
    assert store.upsert_pages([]) == 0
    assert store.upsert_pages(None) == 0


# ── 조회 ──

def test_query_returns_page_shaped_hits_sorted_by_score(store):
    store.upsert_pages([
        page("https://a.kr/1", "1인 가구 음식물 폐기 통계"),
        page("https://b.kr/2", "전통시장 배달 중개", text="전통시장 배달 중개 서비스가 늘고 상인 매출이 올랐다. " * 5),
    ])
    hits = store.query("1인 가구 음식물 쓰레기 배출량 통계")
    assert [h["url"] for h in hits][0] == "https://a.kr/1"     # 주제가 겹치는 쪽이 위
    assert hits == sorted(hits, key=lambda h: -h["score"])
    top = hits[0]
    assert set(top) >= {"url", "title", "publisher", "date", "source_type", "query", "text", "score"}
    assert top["from_vectorstore"] is True                     # 웹에서 새로 받은 페이지와 구분된다
    assert top["source_type"] == "news"


def test_query_merges_chunks_of_one_document_into_one_hit(store):
    store.upsert_pages([page("https://a.kr/1", text=LONG * 60)])   # 512토큰 청크로 여러 개 쪼개진다
    hits = store.query("음식물 쓰레기 배출량")
    assert [h["url"] for h in hits] == ["https://a.kr/1"]


def test_query_respects_k(store):
    store.upsert_pages([page(f"https://a.kr/{i}") for i in range(6)])
    assert len(store.query("음식물 폐기", k=3)) == 3


def test_query_on_empty_text_returns_nothing(store):
    store.upsert_pages([page("https://a.kr/1")])
    assert store.query("   ") == []


def test_publisher_falls_back_to_domain(store):
    store.upsert_pages([page("https://news.example.kr/1"), page("https://b.kr/2", publisher="통계청")])
    got = {h["url"]: h["publisher"] for h in store.query("음식물 폐기 배출량 통계")}
    assert got["https://news.example.kr/1"] == "news.example.kr"
    assert got["https://b.kr/2"] == "통계청"


# ── 신선도 ──

def test_query_drops_documents_older_than_max_age_days(store):
    store.upsert_pages([
        page("https://old.kr/1", date_=ago(1000)),     # 730일 초과 → 제외
        page("https://new.kr/2", date_=ago(30)),
        page("https://nodate.kr/3", date_=None),       # 날짜를 모르는 문서는 판단하지 않고 남긴다
    ])
    urls = {h["url"] for h in store.query("음식물 쓰레기 배출량 통계")}
    assert urls == {"https://new.kr/2", "https://nodate.kr/3"}


def test_max_age_days_zero_disables_freshness_cut(tmp_path):
    store = VectorStore(str(tmp_path / "vs"), max_age_days=0)
    store.upsert_pages([page("https://old.kr/1", date_=ago(5000))])
    assert [h["url"] for h in store.query("음식물 쓰레기 배출량")] == ["https://old.kr/1"]


# ── 충분 판정 ──

def test_is_sufficient_needs_min_score_and_min_hits(tmp_path):
    store = VectorStore(str(tmp_path / "vs"), min_score=0.8, min_hits=3)
    assert store.is_sufficient([{"score": 0.9}] * 3) is True
    assert store.is_sufficient([{"score": 0.9}] * 2) is False           # 건수 미달
    assert store.is_sufficient([{"score": 0.79}] * 5) is False          # 유사도 미달
    assert store.is_sufficient([{"score": 0.9}, {"score": 0.9}, {"score": 0.5}]) is False
    assert store.is_sufficient([]) is False
    assert store.is_sufficient(None) is False


def test_is_sufficient_thresholds_are_configurable(tmp_path):
    loose = VectorStore(str(tmp_path / "vs"), min_score=0.3, min_hits=1)
    assert loose.is_sufficient([{"score": 0.4}]) is True


# ── 영속 ──

def test_reopening_same_dir_keeps_index(tmp_path):
    d = str(tmp_path / "vs")
    VectorStore(d).upsert_pages([page("https://a.kr/1"), page("https://b.kr/2")])
    again = VectorStore(d)
    assert again.upsert_pages([page("https://a.kr/1")]) == 0
    assert {h["url"] for h in again.query("음식물 쓰레기 배출량")} == {"https://a.kr/1", "https://b.kr/2"}


def test_new_dir_starts_empty(tmp_path):
    assert VectorStore(str(tmp_path / "vs")).query("아무거나") == []


# ── 오프라인 임베딩 (위 임계값 테스트들이 이 성질에 기대고 있다) ──

def test_offline_embedding_is_deterministic():
    e = OfflineEmbedding()
    assert e.get_text_embedding("음식물 쓰레기") == e.get_text_embedding("음식물 쓰레기")
    assert len(e.get_text_embedding("음식물 쓰레기")) == e.dim


def test_offline_embedding_scores_related_text_higher():
    e = OfflineEmbedding()
    dot = lambda a, b: sum(x * y for x, y in zip(a, b))
    q = e.get_query_embedding("음식물 쓰레기 배출량 통계")
    near = e.get_text_embedding("1인 가구 음식물 쓰레기 배출량 통계 조사")
    far = e.get_text_embedding("프로야구 개막전 선발 투수 명단 발표")
    assert dot(q, near) > dot(q, far)
