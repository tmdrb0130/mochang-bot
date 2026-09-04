"""vectorstore.py — 조사해온 페이지를 벡터DB 에 영구 축적하고 유사도로 다시 꺼내 쓴다 (LlamaIndex).

설계·인터페이스는 `docs/RAG_PLAN.md` 5·5-1절이 단일 기준이다. 이 파일은 **모듈만** 두고,
조사 파이프라인 연결(collect_pages 안에서 query/upsert 호출)은 세션3 몫이라 여기서 건드리지 않는다.

    vs = VectorStore("backend/.vectorstore")     # embed_model=None → Ollama 없이 도는 오프라인 임베딩
    vs.upsert_pages(pages)                       # collect_pages 가 만든 page dict 를 그대로 받는다
    hits = vs.query("1인 가구 식품 폐기 통계")     # page dict + score
    if vs.is_sufficient(hits):                   # 웹 검색을 생략해도 되는지
        ...

7일 디스크 캐시(`backend/.cache/research/`)를 대체하지 않는다 — 캐시는 같은 요청의 단기 즉시 응답용이고,
이쪽은 만료 없는 축적층이다. 캐시가 지워져도 원문은 여기 남아 재검색 없이 복원된다.

전부 동기 함수다 (LlamaIndex 기본). 파이프라인에서는 `asyncio.to_thread` 로 감싼다.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import threading
import time
from datetime import date, datetime, timezone
from typing import Any

from llama_index.core import Document, StorageContext, VectorStoreIndex, load_index_from_storage
from llama_index.core.embeddings import BaseEmbedding
from llama_index.core.node_parser import SentenceSplitter

from .research import domain

log = logging.getLogger("mochang.vectorstore")

# 이보다 짧은 본문은 사실 추출에 못 쓴다 (pipeline.MIN_BODY_CHARS 와 같은 기준).
# pipeline 을 import 하지 않는 것은 의도 — 이 모듈은 조사 파이프라인에 의존하지 않는다.
MIN_TEXT_CHARS = 40
CHUNK_SIZE = 512          # RAG_PLAN 3절: 약 512토큰 청크
CHUNK_OVERLAP = 50
# 색인 본문에 함께 넣지 않을 메타데이터 — 제목·URL 이 임베딩에 섞이면 유사도가 주제가 아니라 형식에 끌린다.
_META_KEYS = ("url", "title", "publisher", "date", "fetched_at", "source_kind", "query", "embed_model")
# Ollama 헬스체크 간격 — 죽어 있으면 이 간격으로 다시 본다 (살아나면 색인·조회가 자동으로 재개된다)
HEALTH_INTERVAL_S = 60.0
HEALTH_TIMEOUT_S = 2.0


def _url_key(url: str) -> str:
    """같은 문서를 두 번 색인하지 않기 위한 키. pipeline._url_key 와 같은 규칙."""
    return (url or "").strip().split("#")[0].rstrip("/")


def _doc_id(url: str) -> str:
    return hashlib.sha1(_url_key(url).encode("utf-8")).hexdigest()


def _parse_date(value: Any) -> date | None:
    """make_result 가 정규화한 'YYYY-MM-DD' 를 date 로. 못 읽으면 None (= 날짜 모름)."""
    s = str(value or "").strip()[:10]
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None


class OfflineEmbedding(BaseEmbedding):
    """Ollama 없이 도는 결정적 임베딩. 테스트와 `embed_model=None` 기본값에 쓴다.

    글자 2-gram 을 해시해 고정 길이 벡터에 넣고 L2 정규화한다. 의미를 아는 모델이 아니라
    **낱말이 겹칠수록 코사인 유사도가 올라가는** 정도의 성질만 있다 — 임계값(min_score) 동작을
    실제 모델 없이 검증하기에는 이걸로 충분하다.

    LlamaIndex 의 `MockEmbedding` 을 쓰지 않는 이유: 모든 문장에 같은 벡터를 줘서 유사도가 항상 1.0 이 된다.
    그러면 "유사도 0.8 이상만 채택" 같은 규칙을 테스트로 지킬 수 없다.

    운영에서는 `ollama_embedding()` 이 만든 bge-m3 임베딩을 생성자에 넘긴다.
    """

    dim: int = 256

    @classmethod
    def class_name(cls) -> str:
        return "OfflineEmbedding"

    def _vector(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        norm = re.sub(r"\s+", " ", (text or "")).strip().lower()
        for i in range(max(len(norm) - 1, 0)):
            gram = norm[i:i + 2]
            h = int.from_bytes(hashlib.md5(gram.encode("utf-8")).digest()[:4], "big")
            vec[h % self.dim] += 1.0
        length = sum(v * v for v in vec) ** 0.5
        return [v / length for v in vec] if length else vec

    def _get_text_embedding(self, text: str) -> list[float]:
        return self._vector(text)

    def _get_query_embedding(self, query: str) -> list[float]:
        return self._vector(query)

    async def _aget_query_embedding(self, query: str) -> list[float]:
        return self._vector(query)


def ollama_embedding(model_name: str = "bge-m3", base_url: str = "http://localhost:11434", **kw):
    """운영용 임베딩 (RAG_PLAN 5절 결정: 로컬 Ollama bge-m3). 세션3이 파이프라인에서 이걸 만들어 넘긴다.

    import 를 함수 안에 둔 것은 의도 — Ollama 패키지가 없거나 서버가 없어도 이 모듈은 import 된다."""
    from llama_index.embeddings.ollama import OllamaEmbedding

    return OllamaEmbedding(model_name=model_name, base_url=base_url, **kw)


def ollama_alive(base_url: str, timeout: float = HEALTH_TIMEOUT_S) -> bool:
    """Ollama 가 응답하는지 (GET /api/tags). 2초 안에 안 오면 죽은 것으로 본다."""
    import httpx
    try:
        r = httpx.get(base_url.rstrip("/") + "/api/tags", timeout=timeout)
        return r.status_code < 500
    except Exception:
        return False


class VectorStore:
    """조사 페이지의 영구 축적층. RAG_PLAN 5-1절 인터페이스 그대로.

    persist_dir 에 LlamaIndex 기본 형식으로 저장한다 (`backend/.vectorstore/`, gitignore 됨).
    같은 디렉터리로 다시 만들면 이어서 쓴다."""

    def __init__(self, persist_dir: str, embed_model=None, min_score: float = 0.8,
                 min_hits: int = 5, max_age_days: int = 730, freshness_days: int = 0,
                 health_url: str | None = None, health_check=None):
        """freshness_days: 웹 검색을 생략하려면 점수 좋은 적중 중 이 일수 안의 문서가 1건 이상 있어야 한다 (0 이면 끔, 2026-09-04).
        health_url: Ollama 주소. 주면 HEALTH_INTERVAL_S 마다 살아 있는지 보고, 죽어 있으면 색인·조회를 건너뛴다(degraded).
        health_check: 테스트용 — health_url 대신 부를 함수 () -> bool."""
        self.persist_dir = str(persist_dir)
        self.embed_model = embed_model or OfflineEmbedding()
        name = getattr(self.embed_model, "model_name", None)         # OllamaEmbedding 은 "bge-m3", 기본값은 "unknown"
        self.embed_name = name if name and name != "unknown" else type(self.embed_model).__name__
        self.min_score = float(min_score)
        self.min_hits = int(min_hits)
        self.max_age_days = int(max_age_days)     # 0 이하면 신선도 컷을 끈다
        self.freshness_days = int(freshness_days or 0)
        self._health_url = health_url
        self._health_check = health_check
        self._health_at = 0.0
        self.degraded = False                     # True 면 색인·조회를 건너뛴다 (Ollama 죽음)
        self._write_lock = threading.Lock()       # LlamaIndex 파일 persist 는 동시 쓰기에 안전하지 않다
        self._splitter = SentenceSplitter(chunk_size=CHUNK_SIZE, chunk_overlap=CHUNK_OVERLAP)
        self._index = self._open()

    # ── 건강 ──

    def healthy(self) -> bool:
        """임베딩 서버가 살아 있는지. 확인 수단이 없으면(오프라인 임베딩) 항상 True. 결과는 HEALTH_INTERVAL_S 동안 캐시."""
        check = self._health_check or (
            (lambda: ollama_alive(self._health_url)) if self._health_url else None)
        if check is None:
            return True
        now = time.monotonic()
        if now - self._health_at < HEALTH_INTERVAL_S:
            return not self.degraded
        self._health_at = now
        alive = bool(check())
        if alive and self.degraded:
            log.warning("임베딩 서버가 다시 응답합니다 — 벡터DB 색인·조회를 재개합니다")
        if not alive and not self.degraded:
            log.warning("임베딩 서버(%s)가 응답하지 않습니다 — 벡터DB 색인·조회를 건너뛰고 웹 검색만 씁니다", self._health_url or "?")
            try:
                from .. import timing
                timing.log("vectorstore_degraded", url=self._health_url or "")
            except Exception:
                pass
        self.degraded = not alive
        return alive

    # ── 저장소 열기/만들기 ──

    def _open(self) -> VectorStoreIndex:
        common = {"embed_model": self.embed_model, "transformations": [self._splitter]}
        if os.path.exists(os.path.join(self.persist_dir, "docstore.json")):
            storage = StorageContext.from_defaults(persist_dir=self.persist_dir)
            return load_index_from_storage(storage, **common)
        return VectorStoreIndex([], storage_context=StorageContext.from_defaults(), **common)

    def _persist(self) -> None:
        os.makedirs(self.persist_dir, exist_ok=True)
        self._index.storage_context.persist(persist_dir=self.persist_dir)

    def indexed_urls(self) -> set[str]:
        """이미 색인된 URL 키. 중복 색인 방지와 파이프라인의 skip_urls 계산에 쓸 수 있다."""
        return {info.metadata.get("url", "") for info in self._index.ref_doc_info.values() if info.metadata}

    # ── 쓰기 ──

    def upsert_pages(self, pages: list[dict]) -> int:
        """collect_pages 가 만든 page dict 를 색인한다. 같은 url 은 다시 색인하지 않는다. → 새로 색인된 건수.
        임베딩 서버가 죽어 있으면(degraded) 아무것도 하지 않는다 — 품질 없는 벡터로 색인을 오염시키지 않는다."""
        if not pages or not self.healthy():
            return 0
        with self._write_lock:                 # 조사 워커 여럿이 동시에 색인해도 persist 는 한 번에 하나
            known = {_doc_id(u) for u in self.indexed_urls() if u}
            added = 0
            for page in pages or []:
                key = _url_key(str(page.get("url", "")))
                text = str(page.get("text") or page.get("snippet") or "").strip()
                if not key or len(text) < MIN_TEXT_CHARS:
                    continue                       # 본문 없는 검색 결과는 근거로 못 쓴다 — 색인도 하지 않는다
                doc_id = _doc_id(key)
                if doc_id in known:
                    continue
                known.add(doc_id)
                self._index.insert(self._document(doc_id, key, text, page))
                added += 1
            if added:
                self._persist()
        return added

    def _document(self, doc_id: str, url_key: str, text: str, page: dict) -> Document:
        meta = {
            "url": url_key,
            "title": str(page.get("title") or ""),
            # publisher 가 없으면 도메인을 쓴다 — pipeline 이 사실을 만들 때와 같은 규칙.
            "publisher": str(page.get("publisher") or domain(url_key) or ""),
            "date": str(page.get("date") or ""),
            "fetched_at": datetime.now(timezone.utc).date().isoformat(),
            # RAG_PLAN 3절의 source_kind. 검색 결과의 source_type(news/web/blog/…)을 그대로 옮긴다.
            "source_kind": str(page.get("source_type") or page.get("source_kind") or ""),
            "query": str(page.get("query") or ""),
            # 어떤 임베딩으로 색인했는지 (2026-09-04) — 오프라인 임베딩으로 오염된 문서를 나중에 골라낼 수 있게
            "embed_model": self.embed_name,
        }
        return Document(
            id_=doc_id,
            text=text,
            metadata=meta,
            excluded_embed_metadata_keys=list(_META_KEYS),
            excluded_llm_metadata_keys=list(_META_KEYS),
        )

    # ── 읽기 ──

    def query(self, text: str, k: int = 8) -> list[dict]:
        """유사도 조회. page dict 와 같은 형태에 score 를 붙여 돌려준다 (신선도 컷 적용 후, 점수 내림차순).

        같은 문서의 청크가 여러 개 걸리면 가장 높은 점수 하나로 합친다 — 호출부는 '페이지 목록'을 기대한다."""
        if not (text or "").strip() or not self.healthy():
            return []
        # 청크 단위로 걸리므로 문서 수를 맞추려면 넉넉히 뽑아서 합친 뒤 자른다.
        nodes = self._index.as_retriever(similarity_top_k=max(k * 3, k)).retrieve(text)
        best: dict[str, dict] = {}
        for n in nodes:
            meta = n.node.metadata or {}
            url = meta.get("url", "")
            if not url or self._too_old(meta.get("date")):
                continue
            score = float(n.score if n.score is not None else 0.0)
            hit = best.get(url)
            if hit is None or score > hit["score"]:
                best[url] = {
                    "url": url,
                    "title": meta.get("title", ""),
                    "publisher": meta.get("publisher", ""),
                    "date": meta.get("date") or None,
                    "source_type": meta.get("source_kind", ""),
                    "query": meta.get("query", ""),
                    "text": n.node.get_content(),
                    "from_vectorstore": True,      # 웹에서 새로 받은 페이지와 구분 (파이프라인·로그용)
                    "score": score,
                }
        return sorted(best.values(), key=lambda h: -h["score"])[:k]

    def _too_old(self, value: Any) -> bool:
        """RAG_PLAN 5절: date 가 max_age_days 를 넘으면 제외. 날짜를 모르는 문서는 판단하지 않고 남긴다."""
        if self.max_age_days <= 0:
            return False
        d = _parse_date(value)
        return d is not None and (date.today() - d).days > self.max_age_days

    def _is_fresh(self, value: Any) -> bool:
        d = _parse_date(value)
        return d is not None and (date.today() - d).days <= self.freshness_days

    def is_sufficient(self, hits: list[dict]) -> bool:
        """웹 검색을 생략해도 되는지. RAG_PLAN 5절 초기값: 유사도 min_score 이상이 min_hits 건 이상,
        **그리고** (freshness_days > 0 이면) 그중 최근 freshness_days 안의 문서가 1건 이상 (2026-09-04).
        지원사업·시장 동향은 해마다 바뀐다 — 낡은 적중만으로 웹을 건너뛰면 안 된다. 날짜 모르는 문서는 신선하다고 보지 않는다.

        보수적으로 잡는 것이 중요하다 — 느슨하면 엉뚱한 주제의 자료가 근거로 끼어든다."""
        good = [h for h in hits or [] if float(h.get("score", 0)) >= self.min_score]
        if len(good) < self.min_hits:
            return False
        if self.freshness_days <= 0:
            return True
        return any(self._is_fresh(h.get("date")) for h in good)
