"""웹 검색 추상화 — search(query) 하나로 통일.

백엔드(우선순위):
  1. Vane (구 Perplexica) POST /api/search — SearXNG + 모델 요약. config.yaml research.vane.* 로 설정.
     Vane 은 자체 모델 호출(질의 분류·답변)을 하므로 OpenRouter 무료 한도를 쓴다.
  2. 네이버 검색 API (NAVER API HUB, 2026-06-25 이관) — 공식 키. 월 775,000건·50 RPS.
     뉴스+블로그(+웹문서·카페)를 합쳐 준다. .env 의 NAVER_CLIENT_ID/NAVER_CLIENT_SECRET 이 있어야 켜진다.
  3. ddgs (DuckDuckGo) — 비공식이라 대규모에서 봇 차단. **최후 폴백** (RESEARCH_PLAN 3단계).

결과 형식은 백엔드 공통:
  [{"title", "url", "snippet", "date", "source_type"}]
    source_type: "web" | "news" | "vane" | "blog" | "cafe"
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

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}


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
    # 네이버 뉴스 pubDate: "Mon, 01 Sep 2026 09:00:00 +0900"
    m = re.search(r"(\d{1,2})\s+([A-Za-z]{3})[a-z]*\s+(\d{4})", s)
    if m and m.group(2).lower() in _MONTHS:
        return f"{m.group(3)}-{_MONTHS[m.group(2).lower()]:02d}-{int(m.group(1)):02d}"
    # 네이버 블로그 postdate: "20260901"
    m = re.fullmatch(r"\s*(\d{4})(\d{2})(\d{2})\s*", s)
    if m and "01" <= m.group(2) <= "12":
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
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


# ────────────────────────── 백엔드 2: 네이버 검색 API (NAVER API HUB) ──────────────────────────
# 2026-06-25 developers.naver.com → 네이버클라우드플랫폼 NAVER API HUB 로 이관됐다.
# 구 규격(openapi.naver.com/v1/search/{kind}.json + X-Naver-Client-* 헤더)은 신규 발급이 끝났고
# 기존 키도 2027-06-30 까지만 산다 — 여기서는 API HUB 규격만 쓴다.
# 한도: 검색 통합 **월 775,000건**, 키당 **50 RPS**(초과 시 429). 일·월 한도와 알림은 NCP 콘솔에서 설정.

NAVER_ENDPOINTS = {                      # 검색 유형 → (경로 조각, source_type)
    "news": ("news", "news"),            # 뉴스·산업 동향
    "blog": ("blog", "blog"),            # 비정형 웹문서 대체
    "webkr": ("webkr", "web"),           # 웹문서
    "cafearticle": ("cafearticle", "cafe"),   # 카페 글 (고객 불만·후기 성격)
}
# 'doc'(전문자료)·책·쇼핑은 2026-07-31 종료 — 학술 근거는 KCI(KCI_API_KEY) → OpenAlex/Semantic Scholar 로 간다.

NAVER_MAX_DISPLAY = 100                  # display 는 1~100 (start 는 1~1000)


def strip_tags(text: str) -> str:
    """네이버 응답의 <b> 강조 태그와 HTML 엔티티 제거."""
    import html
    return html.unescape(re.sub(r"<[^>]+>", "", text or ""))


def naver_error_message(payload: Any) -> str:
    """API HUB 는 오류 본문이 두 형태다 — API 쪽 {"errorCode","errorMessage"} 와
    게이트웨이 쪽 {"error":{"errorCode","message"}}. 둘 다 읽어 한 줄로 만든다."""
    if not isinstance(payload, dict):
        return ""
    nested = payload.get("error")
    if isinstance(nested, dict):
        code = nested.get("errorCode") or nested.get("code") or ""
        msg = nested.get("message") or nested.get("errorMessage") or ""
    else:
        code = payload.get("errorCode") or ""
        msg = payload.get("errorMessage") or payload.get("message") or ""
    return " ".join(str(x) for x in (code, msg) if x).strip()


class SearchRateLimited(RuntimeError):
    """소스가 429 를 냈다. 5단계 서킷브레이커가 잡을 신호 — 지금은 다음 백엔드로 넘어가는 용도."""


@dataclass
class NaverConfig:
    client_id: str = ""
    client_secret: str = ""
    base_url: str = "https://naverapihub.apigw.ntruss.com/search/v1"
    kinds: list[str] = field(default_factory=lambda: ["news", "blog"])
    sort: str = "sim"                    # sim(정확도) | date(최신). 뉴스는 date 도 유용
    timeout: int = 10

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret)

    @classmethod
    def from_config(cls, rc: dict) -> "NaverConfig":
        """config.yaml research.naver 섹션 + .env 의 키. 키가 없으면 configured=False 라 백엔드에서 빠진다.

        env 이름은 NCP 이관 뒤에도 그대로 NAVER_CLIENT_ID / NAVER_CLIENT_SECRET 를 쓴다
        (콘솔의 Client ID / Client Secret 값을 그대로 넣으면 된다)."""
        nc = (rc or {}).get("naver") or {}
        import os
        return cls(
            client_id=os.environ.get(nc.get("client_id_env", "NAVER_CLIENT_ID"), ""),
            client_secret=os.environ.get(nc.get("client_secret_env", "NAVER_CLIENT_SECRET"), ""),
            base_url=nc.get("base_url", "https://naverapihub.apigw.ntruss.com/search/v1"),
            kinds=[k for k in nc.get("kinds", ["news", "blog"]) if k in NAVER_ENDPOINTS],
            sort=nc.get("sort", "sim"),
            timeout=int(nc.get("timeout", 10)),
        )

    @property
    def headers(self) -> dict:
        """API HUB 인증 헤더. 구 X-Naver-Client-Id/Secret 은 이 게이트웨이에서 통하지 않는다."""
        return {"X-NCP-APIGW-API-KEY-ID": self.client_id, "X-NCP-APIGW-API-KEY": self.client_secret}


class NaverSearch:
    """네이버 검색 API(NAVER API HUB). 모델 호출 없음. kinds 각각에 요청 1회씩 나간다 (기본 news+blog = 2회).

    한도는 키 단위 월 775,000건 합산 — kinds 를 늘리면 그만큼 소모가 빨라진다.
    ddgs 와 달리 공식 API 라 차단 걱정이 없어 1차 소스로 쓴다 (RESEARCH_PLAN 3단계).
    """

    def __init__(self, cfg: NaverConfig):
        self.cfg = cfg
        self.last_error: str | None = None

    def parse(self, kind: str, payload: dict) -> list[dict]:
        source_type = NAVER_ENDPOINTS[kind][1]
        out = []
        for item in (payload or {}).get("items") or []:
            out.append(make_result(
                strip_tags(item.get("title")),
                item.get("originallink") or item.get("link"),
                strip_tags(item.get("description")),
                item.get("pubDate") or item.get("postdate"),
                source_type,
            ))
        return out

    async def __call__(self, query: str, max_results: int = 8) -> list[dict]:
        import httpx
        per_kind = min(NAVER_MAX_DISPLAY, max(1, max_results // max(1, len(self.cfg.kinds))))
        out: list[dict] = []
        errors: list[str] = []
        rate_limited = False
        async with httpx.AsyncClient(timeout=self.cfg.timeout, headers=self.cfg.headers) as client:
            for kind in self.cfg.kinds:
                path = NAVER_ENDPOINTS[kind][0]
                try:
                    r = await client.get(f"{self.cfg.base_url.rstrip('/')}/{path}",
                                         params={"query": query, "display": per_kind, "sort": self.cfg.sort})
                    if r.status_code >= 400:
                        try:
                            detail = naver_error_message(r.json())
                        except Exception:
                            detail = (r.text or "")[:120]
                        errors.append(f"{kind}: HTTP {r.status_code} {detail}".strip())
                        if r.status_code == 429:
                            rate_limited = True
                            break            # 한도 초과면 남은 유형도 어차피 막힌다 — 더 쏘지 않는다
                        continue             # 유형 하나가 죽어도 나머지는 살린다
                    out.extend(self.parse(kind, r.json()))
                except Exception as e:
                    errors.append(f"{kind}: {type(e).__name__}: {str(e)[:100]}")
        self.last_error = "; ".join(errors) or None
        if rate_limited and not out:
            raise SearchRateLimited(self.last_error or "네이버 검색 API 429")
        return dedupe(out)[:max_results]


# ────────────────────────── 백엔드 3: ddgs ──────────────────────────

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
                 vane: VaneConfig | None = None, naver: NaverConfig | None = None):
        if backends is None:
            backends = []
            if vane and vane.configured:
                backends.append(("vane", VaneSearch(vane)))
            if naver and naver.configured:            # 키가 있을 때만 — 없으면 예전처럼 ddgs 단독
                backends.append(("naver", NaverSearch(naver)))
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
    return Researcher(vane=vane, naver=NaverConfig.from_config(rc), cache=cache)


def today_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")
