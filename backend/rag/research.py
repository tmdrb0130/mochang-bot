"""웹 검색 추상화 — search(query) 하나로 통일.

백엔드(우선순위):
  1. Vane (구 Perplexica) POST /api/search — SearXNG + 모델 요약. config.yaml research.vane.* 로 설정.
     Vane 은 자체 모델 호출(질의 분류·답변)을 하므로 OpenRouter 무료 한도를 쓴다.
  2. ddgs (DuckDuckGo) — 모델 호출 없음. Vane 이 없거나 실패하면 자동 폴백.

결과 형식은 두 백엔드 공통:
  [{"title", "url", "snippet", "date", "source_type"}]
    source_type: "web" | "news" | "vane"
    date: "YYYY-MM-DD" 또는 None

같은 검색어(+백엔드+개수)는 디스크 캐시 (backend/.cache/research/*.json, 기본 7일).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Awaitable
from urllib.parse import urlparse

CACHE_DIR = Path(__file__).resolve().parents[1] / ".cache" / "research"
DEFAULT_TTL = 7 * 24 * 3600

SearchFn = Callable[[str, int], Awaitable[list[dict]]]


# ────────────────────────── 결과 정규화 ──────────────────────────

def _norm_date(value: Any) -> str | None:
    if not value:
        return None
    s = str(value)
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    m = re.search(r"(\d{4})[./년]\s*(\d{1,2})[./월]\s*(\d{1,2})", s)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    return None


def make_result(title: str, url: str, snippet: str = "", date: Any = None, source_type: str = "web") -> dict:
    return {
        "title": (title or "").strip(),
        "url": (url or "").strip(),
        "snippet": re.sub(r"\s+", " ", snippet or "").strip(),
        "date": _norm_date(date),
        "source_type": source_type,
    }


def dedupe(results: list[dict]) -> list[dict]:
    seen, out = set(), []
    for r in results:
        key = (r.get("url") or "").split("#")[0].rstrip("/")
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def domain(url: str) -> str:
    try:
        return urlparse(url).netloc.lower().removeprefix("www.")
    except Exception:
        return ""


# ────────────────────────── 디스크 캐시 ──────────────────────────

class DiskCache:
    def __init__(self, directory: Path = CACHE_DIR, ttl: int = DEFAULT_TTL):
        self.dir = directory
        self.ttl = ttl

    def _path(self, key: str) -> Path:
        return self.dir / (hashlib.sha1(key.encode("utf-8")).hexdigest() + ".json")

    def get(self, key: str) -> list[dict] | None:
        p = self._path(key)
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if time.time() - d.get("ts", 0) > self.ttl:
            return None
        return d.get("results")

    def set(self, key: str, results: list[dict]) -> None:
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            self._path(key).write_text(json.dumps({"ts": time.time(), "key": key, "results": results}, ensure_ascii=False),
                                       encoding="utf-8")
        except OSError:
            pass


# ────────────────────────── 백엔드 1: Vane ──────────────────────────

@dataclass
class VaneConfig:
    base_url: str = "http://localhost:3000"
    chat_provider_id: str = ""
    chat_model_key: str = "minimax/minimax-m3:free"
    embedding_provider_id: str = ""
    embedding_model_key: str = "Xenova/nomic-embed-text-v1"
    sources: list[str] = field(default_factory=lambda: ["web"])
    optimization_mode: str = "speed"
    timeout: int = 120

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.chat_provider_id and self.embedding_provider_id)


class VaneSearch:
    """Vane /api/search 호출. 응답 {message, sources:[{pageContent, metadata:{title,url,...}}]} 을 공통 형식으로."""

    def __init__(self, cfg: VaneConfig):
        self.cfg = cfg

    def build_body(self, query: str) -> dict:
        return {
            "chatModel": {"providerId": self.cfg.chat_provider_id, "key": self.cfg.chat_model_key},
            "embeddingModel": {"providerId": self.cfg.embedding_provider_id, "key": self.cfg.embedding_model_key},
            "sources": self.cfg.sources,
            "optimizationMode": self.cfg.optimization_mode,
            "query": query,
            "stream": False,
        }

    @staticmethod
    def parse(payload: dict) -> list[dict]:
        out = []
        for s in payload.get("sources") or []:
            meta = s.get("metadata") or {}
            out.append(make_result(
                title=meta.get("title") or s.get("title") or "",
                url=meta.get("url") or s.get("url") or "",
                snippet=s.get("pageContent") or s.get("snippet") or "",
                date=meta.get("publishedDate") or meta.get("date"),
                source_type="vane",
            ))
        return dedupe(out)

    async def __call__(self, query: str, max_results: int = 8) -> list[dict]:
        import httpx
        async with httpx.AsyncClient(timeout=self.cfg.timeout) as client:
            r = await client.post(self.cfg.base_url.rstrip("/") + "/api/search", json=self.build_body(query))
            r.raise_for_status()
            return self.parse(r.json())[:max_results]


# ────────────────────────── 백엔드 2: ddgs ──────────────────────────

class DDGSearch:
    """DuckDuckGo(ddgs). 모델 호출 없음. 기본은 text 만 — include_news=True 면 news 도 합쳐 준다.

    2026-09-02: 뉴스 호출을 기본으로 끄면서 조사 1회당 DDG 요청이 절반(8→4)이 됐다.
    뉴스 근거 자체를 버린 게 아니라 네이버 뉴스 API 로 옮길 예정이다 (RESEARCH_PLAN 3단계).
    그때까지 ddgs 뉴스가 필요하면 DDGSearch(include_news=True) 로 되살릴 수 있다."""

    def __init__(self, region: str = "kr-kr", include_news: bool = False):
        self.region = region
        self.include_news = include_news

    def _sync(self, query: str, max_results: int) -> list[dict]:
        from ddgs import DDGS
        out: list[dict] = []
        with DDGS() as d:
            for r in d.text(query, region=self.region, max_results=max_results):
                out.append(make_result(r.get("title"), r.get("href") or r.get("url"), r.get("body"), None, "web"))
            if self.include_news:
                try:
                    for r in d.news(query, region=self.region, max_results=max(3, max_results // 2)):
                        out.append(make_result(r.get("title"), r.get("url"), r.get("body"), r.get("date"), "news"))
                except Exception:
                    pass  # 뉴스는 보너스 — 실패해도 text 결과는 살린다
        return dedupe(out)[:max_results]

    async def __call__(self, query: str, max_results: int = 8) -> list[dict]:
        return await asyncio.to_thread(self._sync, query, max_results)


# ────────────────────────── 통합 인터페이스 ──────────────────────────

class Researcher:
    """search(query) 하나로 Vane → ddgs 폴백 + 캐시.

    backends 를 직접 넘기면(테스트용) 그 순서대로 시도한다.
    """

    def __init__(self, backends: list[tuple[str, SearchFn]] | None = None, cache: DiskCache | None = None,
                 vane: VaneConfig | None = None):
        if backends is None:
            backends = []
            if vane and vane.configured:
                backends.append(("vane", VaneSearch(vane)))
            backends.append(("ddgs", DDGSearch()))
        self.backends = backends
        self.cache = cache if cache is not None else DiskCache()
        self.last_backend: str | None = None
        self.last_error: str | None = None

    async def search(self, query: str, max_results: int = 8, use_cache: bool = True) -> list[dict]:
        query = query.strip()
        if not query:
            return []
        key = f"{'+'.join(n for n, _ in self.backends)}|{max_results}|{query}"
        if use_cache:
            hit = self.cache.get(key)
            if hit is not None:
                self.last_backend = "cache"
                return hit
        errors = []
        for name, fn in self.backends:
            try:
                results = await fn(query, max_results)
            except Exception as e:  # 백엔드 하나가 죽어도 다음으로
                errors.append(f"{name}: {type(e).__name__}: {str(e)[:120]}")
                continue
            if results:
                self.last_backend = name
                self.last_error = "; ".join(errors) or None
                if use_cache:
                    self.cache.set(key, results)
                return results
        self.last_backend = None
        self.last_error = "; ".join(errors) or "no results"
        return []


def researcher_from_config(config: dict) -> Researcher:
    """config.yaml 의 research 섹션으로 Researcher 생성. 섹션이 없으면 ddgs 만."""
    rc = (config or {}).get("research") or {}
    vane_cfg = rc.get("vane") or {}
    vane = VaneConfig(
        base_url=vane_cfg.get("base_url", "http://localhost:3000"),
        chat_provider_id=vane_cfg.get("chat_provider_id", ""),
        chat_model_key=vane_cfg.get("chat_model_key", "minimax/minimax-m3:free"),
        embedding_provider_id=vane_cfg.get("embedding_provider_id", ""),
        embedding_model_key=vane_cfg.get("embedding_model_key", "Xenova/nomic-embed-text-v1"),
        sources=vane_cfg.get("sources", ["web"]),
        optimization_mode=vane_cfg.get("optimization_mode", "speed"),
    ) if vane_cfg.get("enabled", False) else None
    cache = DiskCache(ttl=int(rc.get("cache_ttl_seconds", DEFAULT_TTL)))
    return Researcher(vane=vane, cache=cache)


def today_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")
