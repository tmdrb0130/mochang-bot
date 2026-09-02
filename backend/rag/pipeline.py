"""조사 파이프라인: 아이디어 + 문항 의도 → 검색어 → 검색 → 본문 추출 → 사실(출처·날짜·원문) JSON → 참고자료 섹션.

    facts = await run_research(client, researcher, form, "q2", cfg)
    form["references"] = facts["facts"]      # assemble.build_context 가 [웹 참고자료] 섹션으로 주입

지어내기 방지 장치 두 겹:
  1. extract_facts.md — 원문 그대로 quote, 없는 사실 금지
  2. 프로그램 검증 — quote 가 실제 페이지 본문에 없으면 그 사실을 버린다 (_verify_quotes)
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
from dataclasses import dataclass

from .. import timing
from ..llm.client import LLMClient
from ..pipeline import assemble
from .research import DiskCache, Researcher, domain

MAX_PAGE_CHARS = 6000
MIN_BODY_CHARS = 40      # 이보다 짧은 본문/스니펫은 사실 추출에 못 쓴다
MAX_FACTS = 8
# 이 소스에서 온 페이지는 벡터DB 에 쌓지 않는다 — KCI 이용 준수 사항 2항(원천 데이터 영구 보유 금지).
# source_type 뿐 아니라 **도메인**으로도 막는다: ddgs·네이버가 kci.go.kr 문서를 물어 오는 경우가 실제로 있어
# (backend/.cache 에 사례 다수) 그 페이지가 색인되면 같은 조항을 어기게 된다.
NO_INDEX_SOURCE = "kci"
NO_INDEX_DOMAINS = ("kci.go.kr",)


def indexable(page: dict) -> bool:
    """벡터DB 에 쌓아도 되는 페이지인지 (KCI 계열 제외)."""
    if page.get("from_vectorstore") or page.get("source_type") == NO_INDEX_SOURCE:
        return False
    host = domain(str(page.get("url", "")))
    return not any(host == d or host.endswith("." + d) for d in NO_INDEX_DOMAINS)
PREFERRED_DOMAINS = ("go.kr", "or.kr", "re.kr", "kostat", "kosis", "news", "yna.co.kr", "hankyung", "mk.co.kr",
                     "chosun", "joongang", "donga", "hani.co.kr", "khan.co.kr", "sedaily", "edaily", "etnews", "zdnet")
SKIP_EXT = (".pdf", ".hwp", ".xls", ".xlsx", ".ppt", ".pptx", ".doc", ".docx", ".zip")


@dataclass
class ResearchConfig:
    max_queries: int = 4
    max_results_per_query: int = 6
    max_pages_to_extract: int = 6
    # 실제로 본문을 받아오는 페이지 수. 예전엔 max_pages_to_extract*2 를 받아 절반을 버렸다(요청 낭비).
    # 0 이면 max_pages_to_extract 와 같다. fetch 실패가 잦으면 8 정도로 조금만 올린다.
    max_pages_to_fetch: int = 0
    fetch_timeout: int = 15
    cache_ttl: int = 7 * 24 * 3600
    # ── 아이디어당 1회 조사 (RESEARCH_PLAN 2단계) ──
    # True 면 문항 조사가 아이디어 공통 조사 결과를 그대로 물려받고, 모자란 각도만 보완 검색한다.
    # False 면 예전처럼 문항마다 처음부터 조사한다 (되돌리기용 한 줄 스위치).
    share_idea_research: bool = True
    max_followup_queries: int = 2      # 문항당 보완 검색어 **상한**. 모델이 0개를 내면 검색을 아예 안 한다
    max_followup_pages: int = 3        # 보완 검색으로 본문을 받아올 페이지 수 상한
    # 같은 도메인에서 가져올 페이지 수 상한 (작업 7 — 근거가 한 신문사에 쏠리는 것 방지). 0 이면 제한 없음.
    max_pages_per_domain: int = 2
    # ── 벡터DB 축적 (RAG_PLAN, backend/rag/vectorstore.py) ──
    # 지금은 **색인만** 한다 — 조사해온 페이지를 쌓아 두는 단계. 조회(웹 검색 대체)는 다음 단계에서 붙인다.
    # 기본 False: 켜면 임베딩 호출이 늘고 디스크에 쌓이므로 사용자 결정 뒤에 켠다.
    vectorstore_enabled: bool = False
    vectorstore_dir: str = "backend/.vectorstore"
    vectorstore_embed_model: str = "bge-m3"      # Ollama 모델명. 빈 값이면 오프라인 임베딩(품질 없음 — 테스트용)
    vectorstore_ollama_url: str = "http://localhost:11434"
    vectorstore_min_score: float = 0.6      # 실측 근거: bge-m3 는 같은 주제 문장끼리도 0.668 (0.8 은 과하다)
    # 쌓인 자료로 웹 검색을 대신할지 (RAG_PLAN 5절). 적중이 충분하면 그 조사에서는 외부 요청이 0건이 된다.
    vectorstore_use_for_search: bool = True
    vectorstore_min_hits: int = 5
    vectorstore_max_age_days: int = 730

    @classmethod
    def from_config(cls, config: dict) -> "ResearchConfig":
        rc = (config or {}).get("research") or {}
        vs = rc.get("vectorstore") or {}
        return cls(
            max_queries=int(rc.get("max_queries", 4)),
            max_results_per_query=int(rc.get("max_results_per_query", 6)),
            max_pages_to_extract=int(rc.get("max_pages_to_extract", 6)),
            max_pages_to_fetch=int(rc.get("max_pages_to_fetch", 0)),
            fetch_timeout=int(rc.get("fetch_timeout", 15)),
            share_idea_research=bool(rc.get("share_idea_research", True)),
            max_followup_queries=int(rc.get("max_followup_queries", 2)),
            max_followup_pages=int(rc.get("max_followup_pages", 3)),
            max_pages_per_domain=int(rc.get("max_pages_per_domain", 2)),
            vectorstore_enabled=bool(vs.get("enabled", False)),
            vectorstore_dir=str(vs.get("dir", "backend/.vectorstore")),
            vectorstore_embed_model=str(vs.get("embed_model", "bge-m3")),
            vectorstore_ollama_url=str(vs.get("ollama_url", "http://localhost:11434")),
            vectorstore_min_score=float(vs.get("min_score", 0.6)),
            vectorstore_use_for_search=bool(vs.get("use_for_search", True)),
            vectorstore_min_hits=int(vs.get("min_hits", 5)),
            vectorstore_max_age_days=int(vs.get("max_age_days", 730)),
            cache_ttl=int(rc.get("cache_ttl_seconds", 7 * 24 * 3600)),
        )


# ────────────────────────── JSON 파싱 ──────────────────────────

def _parse_json_array(raw: str) -> list:
    cleaned = raw.replace("```json", "").replace("```", "")
    start, end = cleaned.find("["), cleaned.rfind("]")
    if start == -1 or end == -1:
        raise ValueError(f"JSON 배열을 찾지 못함: {raw[:160]!r}")
    data = json.loads(cleaned[start:end + 1])
    if not isinstance(data, list):
        raise ValueError("배열이 아님")
    return data


# ────────────────────────── 1) 검색어 생성 ──────────────────────────

def fallback_queries(form: dict, meta: dict, n: int) -> list[str]:
    """모델 실패 시: 아이디어에서 명사 덩어리를 뽑아 단순 검색어를 만든다."""
    idea = re.sub(r"[^\w\s가-힣]", " ", form.get("idea", ""))
    words = [w for w in idea.split() if len(w) >= 2][:6]
    base = " ".join(words[:3]) if words else form.get("idea", "")[:20]
    cands = [f"{base} 현황", f"{base} 통계", f"{base} 시장", f"{base} 문제점", f"{base} 서비스"]
    return cands[:n]


async def generate_queries(client: LLMClient, form: dict, meta: dict, cfg: ResearchConfig) -> list[str]:
    system = assemble.render("research/queries.md", n=cfg.max_queries)
    user = "\n\n".join([
        assemble.build_context(form),
        f"[문항] {meta.get('label', '')}. {meta.get('title', '')}",
        f"[문항 의도·필수 요소]\n{meta.get('body', '')[:1200]}",
    ])
    try:
        res = await client.complete(system, user, model=form.get("model"))
        queries = [str(q).strip() for q in _parse_json_array(res.text) if str(q).strip()]
    except Exception:
        queries = []
    queries = list(dict.fromkeys(queries))[:cfg.max_queries]
    return queries or fallback_queries(form, meta, cfg.max_queries)


def _fact_digest(facts: list[dict], limit: int = 10) -> str:
    """이미 확보한 사실을 모델에게 짧게 보여 주기 위한 요약 (원문 quote 는 뺀다 — 길기만 하다)."""
    lines = []
    for f in facts[:limit]:
        pub = _clean(f.get("publisher")) or domain(str(f.get("url", "")))
        year = _clean(str(f.get("date") or ""))[:4]
        tail = f" ({pub}{', ' + year if year else ''})" if pub else ""
        lines.append(f"- {_clean(f.get('fact'))}{tail}")
    return "\n".join(lines) or "- (없음)"


async def generate_followup_queries(client: LLMClient, form: dict, meta: dict, shared: dict,
                                    cfg: ResearchConfig) -> list[str]:
    """공통 조사(shared)로 못 채우는 각도만 검색어로. 충분하면 빈 리스트 — 그러면 이 문항은 외부 요청 0건.

    generate_queries 와 달리 **실패 시 폴백 검색어를 만들지 않는다**. 이미 공통 조사 사실이 있으므로
    억지로 검색하는 것보다 안 하는 쪽이 낫다 (요청 수를 늘리지 않는 것이 2단계의 목적)."""
    if cfg.max_followup_queries <= 0:
        return []
    from .allocate import ANGLE_LABEL, QUESTION_ANGLES
    qid = str(meta.get("id") or "")
    angles = QUESTION_ANGLES.get(qid)
    angle_text = ("\n".join(f"- {a}: {ANGLE_LABEL[a]}" for a in angles) if angles
                  else "- 이 문항은 외부 근거를 쓰지 않습니다. 빈 배열을 출력합니다.")
    system = assemble.render("research/followup_queries.md", n=cfg.max_followup_queries, angles=angle_text)
    shared_queries = list(shared.get("queries") or [])
    user = "\n\n".join([
        assemble.build_context({k: v for k, v in form.items() if k != "references"}),
        f"[문항] {meta.get('label', '')}. {meta.get('title', '')}",
        f"[문항 의도·필수 요소]\n{meta.get('body', '')[:1200]}",
        f"[이미 확보한 사실]\n{_fact_digest(shared.get('facts') or [])}",
        f"[이미 사용한 검색어] {' | '.join(shared_queries) or '(없음)'}",
    ])
    try:
        res = await client.complete(system, user, model=form.get("model"))
        queries = [str(q).strip() for q in _parse_json_array(res.text) if str(q).strip()]
    except Exception:
        return []
    used = {_norm(q) for q in shared_queries}
    out = []
    for q in queries:
        if _norm(q) in used:            # 공통 조사와 글자만 같은 검색어는 버린다
            continue
        used.add(_norm(q))
        out.append(q)
    return out[: cfg.max_followup_queries]


# ────────────────────────── 2) 검색 + 3) 본문 추출 ──────────────────────────

def rank_results(results: list[dict]) -> list[dict]:
    """신뢰도 높은 도메인(정부·통계·언론)과 날짜 있는 결과를 앞으로."""
    def score(r):
        d = domain(r.get("url", ""))
        s = 0
        if any(p in d for p in PREFERRED_DOMAINS):
            s += 2
        if r.get("date"):
            s += 1
        if r.get("source_type") == "news":
            s += 1
        return -s
    return sorted(results, key=score)


async def fetch_page(url: str, timeout: int = 15) -> str:
    """HTML → 본문 텍스트 (trafilatura). 실패/비HTML 이면 빈 문자열."""
    if not url or url.lower().split("?")[0].endswith(SKIP_EXT):
        return ""
    import httpx
    import trafilatura
    timing.count("fetch")          # 성공·실패 무관하게 '나간 요청' 을 센다
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True,
                                     headers={"User-Agent": "Mozilla/5.0 (modoo-writer research)"}) as c:
            r = await c.get(url)
            if r.status_code >= 400 or "html" not in (r.headers.get("content-type") or "html"):
                return ""
            html = r.text
    except Exception:
        return ""
    text = await asyncio.to_thread(trafilatura.extract, html, include_comments=False, include_tables=True,
                                   favor_precision=True)
    return (text or "")[:MAX_PAGE_CHARS]


def _url_key(url: str) -> str:
    return (url or "").split("#")[0].rstrip("/")


_vector_store = None          # 프로세스당 하나 (색인 저장소를 열어 두는 비용이 크다)


def get_vector_store(cfg: ResearchConfig):
    """설정이 켜져 있을 때만 벡터 저장소를 연다. llama_index import 도 이때 처음 일어난다."""
    global _vector_store
    if not cfg.vectorstore_enabled:
        return None
    if _vector_store is None:
        from .vectorstore import VectorStore, ollama_embedding
        embed = (ollama_embedding(cfg.vectorstore_embed_model, cfg.vectorstore_ollama_url)
                 if cfg.vectorstore_embed_model else None)
        _vector_store = VectorStore(cfg.vectorstore_dir, embed_model=embed,
                                    min_score=cfg.vectorstore_min_score, min_hits=cfg.vectorstore_min_hits,
                                    max_age_days=cfg.vectorstore_max_age_days)
    return _vector_store


async def index_pages(pages: list[dict], cfg: ResearchConfig) -> int:
    """조사해온 페이지를 벡터DB 에 쌓는다 (색인만 — 조회는 다음 단계).

    LlamaIndex 는 동기라 to_thread 로 감싼다. **색인 실패는 조사를 막지 않는다** — 부가 기능이다."""
    store = get_vector_store(cfg)
    if store is None or not pages:
        return 0
    try:
        return await asyncio.to_thread(store.upsert_pages, pages)
    except Exception:
        return 0


def reuse_page(hit: dict) -> dict:
    """벡터DB 적중을 collect_pages 의 page dict 모양으로. 본문이 이미 있으므로 fetch 하지 않는다."""
    return {
        "url": hit.get("url", ""),
        "title": hit.get("title", ""),
        "date": hit.get("date"),
        "snippet": "",
        "source_type": hit.get("source_type") or "vectorstore",
        "text": hit.get("text", ""),
        "from_snippet": False,
        "from_vectorstore": True,
        "score": hit.get("score"),
    }


async def query_vector_store(store, queries: list[str], cfg: ResearchConfig) -> list[dict]:
    """검색어마다 벡터DB 를 조회해 URL 기준으로 합친다 (같은 문서는 최고 점수 하나). 실패하면 빈 목록.

    조회는 로컬 임베딩 호출이라 외부 요청이 아니다 — 웹 검색·fetch 를 줄이는 것이 목적이다."""
    try:
        rows = await asyncio.gather(*(asyncio.to_thread(store.query, q, cfg.max_pages_to_extract)
                                      for q in queries))
    except Exception:
        return []
    best: dict[str, dict] = {}
    for group in rows:
        for hit in group or []:
            url = hit.get("url", "")
            if not url:
                continue
            cur = best.get(url)
            if cur is None or float(hit.get("score", 0)) > float(cur.get("score", 0)):
                best[url] = hit
    return sorted(best.values(), key=lambda h: -float(h.get("score", 0)))


async def _no_fetch() -> str:
    """fetch 자리에 끼우는 빈 코루틴 — 정형 API 결과는 HTTP 로 받아올 페이지가 없다."""
    return ""


def _cap_by_domain(results: list[dict], limit: int) -> list[dict]:
    """같은 도메인에서 limit 건까지만. 순위는 유지한다 (작업 7 — 한 신문사·한 글 쏠림 방지).
    정형 API 결과(no_fetch)는 제한 대상이 아니다 — 소스가 하나뿐인 공식 통계라 쏠림이 문제되지 않는다."""
    if limit <= 0:
        return results
    seen: dict[str, int] = {}
    out = []
    for r in results:
        if r.get("no_fetch"):
            out.append(r)
            continue
        d = domain(r.get("url", ""))
        if seen.get(d, 0) >= limit:
            continue
        seen[d] = seen.get(d, 0) + 1
        out.append(r)
    return out


async def collect_pages(researcher: Researcher, queries: list[str], cfg: ResearchConfig,
                        fetch=fetch_page, skip_urls: set[str] | None = None,
                        max_pages: int | None = None) -> tuple[list[dict], list[dict]]:
    """검색 → 랭킹 → 상위 N 페이지 본문. (검색 결과 전체, 본문 있는 페이지) 반환.

    skip_urls: 이미 다른 조사에서 본문을 받아온 URL — 다시 받지 않는다 (요청 절약).
    max_pages: 이번 호출에서 쓸 페이지 수 상한 (기본 cfg.max_pages_to_extract)."""
    want = cfg.max_pages_to_extract if max_pages is None else max_pages
    store = get_vector_store(cfg)

    # ① 이미 쌓아 둔 자료부터 본다. 충분하면 웹 검색·fetch 를 아예 하지 않는다 (RAG_PLAN 5절).
    reuse: list[dict] = []
    if store is not None and cfg.vectorstore_use_for_search and queries:
        reuse = await query_vector_store(store, queries, cfg)
        if reuse and store.is_sufficient(reuse):
            return [], [reuse_page(h) for h in reuse[:want]]

    # ② 모자라면 웹 검색을 하되, 쌓아 둔 문서는 다시 받지 않고 근거로 같이 쓴다.
    kept = [h for h in reuse if float(h.get("score", 0)) >= cfg.vectorstore_min_score][: max(1, want // 2)]
    skip_urls = set(skip_urls or ()) | {h.get("url", "") for h in kept}

    all_results: list[dict] = []
    for q in queries:
        rows = await researcher.search(q, cfg.max_results_per_query)
        for r in rows:
            all_results.append({**r, "query": q})
    skip = {_url_key(u) for u in (skip_urls or set())}
    seen, unique = set(), []
    for r in all_results:
        k = _url_key(r["url"])
        if k and k not in seen and k not in skip:
            seen.add(k)
            unique.append(r)
    budget = cfg.max_pages_to_fetch or cfg.max_pages_to_extract      # 받아올 페이지 수 (본문 실패 대비 여유 포함)
    ranked = _cap_by_domain(rank_results(unique), cfg.max_pages_per_domain)
    ranked = ranked[: min(budget, want) if max_pages is not None else budget]
    # 정형 API 결과(no_fetch)는 받아올 페이지가 없다 — snippet 이 이미 사실 문장이라 그대로 본문으로 쓴다.
    texts = await asyncio.gather(*(_no_fetch() if r.get("no_fetch") else fetch(r["url"], cfg.fetch_timeout)
                                   for r in ranked))
    pages = [reuse_page(h) for h in kept]        # 쌓아 둔 문서를 먼저 채우고 남는 자리만 웹에서
    for r, t in zip(ranked, texts):
        body = t or r.get("snippet") or ""
        if len(body) >= MIN_BODY_CHARS:
            pages.append({**r, "text": body, "from_snippet": not bool(t)})
        if len(pages) >= want:
            break
    # 새로 받아온 것만 색인한다. KCI 계열은 영구 축적 금지(RESEARCH_PLAN 준수 사항 2항)라 여기서 뺀다.
    await index_pages([p for p in pages if indexable(p)], cfg)
    return unique, pages


# ────────────────────────── 4) 사실 추출 + 검증 ──────────────────────────

def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s or "")


def _verify_quotes(facts: list[dict], pages: list[dict]) -> list[dict]:
    """quote 가 해당 URL(또는 아무 페이지) 본문에 실제로 있는지 확인. 없으면 버림 — 지어내기 방지의 마지막 관문."""
    by_url = {p["url"]: _norm(p["text"]) for p in pages}
    all_text = "".join(by_url.values())
    kept = []
    for f in facts:
        if not isinstance(f, dict):
            continue
        q = _norm(str(f.get("quote", "")))
        if len(q) < 8:
            continue
        target = by_url.get(str(f.get("url", "")), "")
        if q in target or q in all_text:
            kept.append(f)
    return kept


async def extract_facts(client: LLMClient, form: dict, meta: dict, pages: list[dict], cfg: ResearchConfig) -> list[dict]:
    if not pages:
        return []
    system = assemble.render("research/extract_facts.md", max_facts=MAX_FACTS)
    docs = []
    for i, p in enumerate(pages, 1):
        docs.append(f"<문서 {i}>\n제목: {p.get('title', '')}\nURL: {p['url']}\n날짜: {p.get('date') or '미상'}\n"
                    f"본문:\n{p['text']}\n</문서 {i}>")
    user = "\n\n".join([
        f"[문항] {meta.get('label', '')}. {meta.get('title', '')}",
        f"[문항 의도]\n{meta.get('body', '')[:800]}",
        f"[아이디어]\n{form.get('idea', '')}",
        "[수집한 웹 문서]\n" + "\n\n".join(docs),
    ])
    res = await client.complete(system, user, model=form.get("model"))
    try:
        raw_facts = _parse_json_array(res.text)
    except Exception:
        return []
    facts = _verify_quotes(raw_facts, pages)
    kind_by_url = {_url_key(p.get("url", "")): p.get("source_type", "") for p in pages}
    out = []
    for f in facts[:MAX_FACTS]:
        out.append({
            # 어느 소스에서 온 근거인지 (프론트가 KCI 배지·출처 표기에 쓴다 — 세션2 요청)
            "source_kind": kind_by_url.get(_url_key(str(f.get("url", ""))), ""),
            "fact": str(f.get("fact", "")).strip(),
            "quote": str(f.get("quote", "")).strip(),
            "source_title": str(f.get("source_title", "")).strip(),
            "url": str(f.get("url", "")).strip(),
            "date": (str(f["date"]).strip() if f.get("date") not in (None, "", "null") else None),
            "publisher": str(f.get("publisher") or domain(str(f.get("url", "")))).strip(),
            "use_for": str(f.get("use_for", "")).strip(),
            # 문항별 배분에 쓰는 각도 (작업 33). 모델이 안 붙였으면 배분 단계가 단서로 추정한다
            "angle": str(f.get("angle", "")).strip().lower() if str(f.get("angle", "")).strip().lower()
            in ("problem", "competitor", "pricing", "trend") else "",
        })
    for row in out:
        # 경쟁 서비스 사실은 프론트·골자가 알아볼 수 있게 표시 (KCI 표시는 그대로 둔다)
        if row["angle"] == "competitor" and row["source_kind"] != "kci":
            row["source_kind"] = "competitor"
    return out


# ────────────────────────── 5) 참고자료 섹션 ──────────────────────────

# 추출 모델이 값을 못 채우면 None 이 아니라 문자열 "null"·"미상" 등을 내보낼 때가 있다.
# 파이썬에서 이런 문자열은 참이라 `or` 폴백을 그냥 통과해 "출처: null" 이 프롬프트에 박힌다.
_EMPTYISH = {"", "null", "none", "nil", "n/a", "na", "unknown", "미상", "없음", "-"}


def _clean(v) -> str:
    """비어 있거나 'null' 같은 자리표시 문자열이면 빈 문자열로 정규화."""
    s = (v or "")
    if not isinstance(s, str):
        s = str(s)
    return "" if s.strip().lower() in _EMPTYISH else s.strip()


def _year_of(fact: dict) -> str:
    """date 가 비었으면 URL 에서 연도를 건진다.

    조사 모델이 date 를 거의 항상 비워 보내는데, 기사 URL 에는 날짜가 들어 있는 경우가 많다
    (예: newspim.com/news/view/20250313000298 → 2025). 연도가 없으면 "연도 미상" 같은 말을
    만들지 말고 빈 문자열을 돌려준다 — 모델이 그 표현을 본문에 그대로 옮겨 쓰기 때문이다.
    """
    d = _clean(fact.get("date"))
    if len(d) >= 4 and d[:4].isdigit():
        return d[:4]

    url = _clean(fact.get("url"))
    # 임의의 숫자 id 를 연도로 오인하지 않도록 "연도 + 월(01~12)" 이 이어질 때만 인정한다.
    #   https://www.newspim.com/news/view/20250313000298  -> 2025 (2025 + 03)
    #   https://example.kr/2024/07/13/article             -> 2024
    #   https://example.kr/view/20259999                  -> 인정하지 않음 (99월은 없다)
    m = re.search(r"(?:^|/)((?:19|20)\d{2})[-/]?(?:0[1-9]|1[0-2])", url)
    if not m:
        # /2024/ 처럼 연도만 한 조각으로 들어 있는 경우
        m = re.search(r"(?:^|/)((?:19|20)\d{2})(?:/|$)", url)
    return m.group(1) if m else ""


def format_references(facts: list[dict]) -> str:
    """문항 생성 프롬프트에 넣는 [웹 참고자료] 섹션. 비어 있으면 빈 문자열.

    같은 URL 에서 뽑은 사실들은 한 출처 아래로 묶는다. 따로 나열하면 모델이
    같은 출처를 여러 번 반복 인용한다(실측: 뉴스핌 기사 하나가 3줄로 갈려 3회 인용됨).
    """
    if not facts:
        return ""

    groups: list[dict] = []
    by_url: dict[str, dict] = {}
    for f in facts:
        url = _clean(f.get("url"))
        key = url or ("no-url-%d" % len(groups))
        g = by_url.get(key)
        if g is None:
            g = {"url": url,
                 "pub": _clean(f.get("publisher")) or _clean(f.get("source_title")),
                 "year": _year_of(f),
                 "items": []}
            by_url[key] = g
            groups.append(g)
        g["items"].append(f)
        if not g["year"]:
            g["year"] = _year_of(f)

    lines = [assemble.section_headers().get("references", "[웹 참고자료]")]
    for i, g in enumerate(groups, 1):
        if g["pub"] and g["year"]:
            head = f"{i}. {g['year']}년 {g['pub']}"
        elif g["pub"]:
            head = f"{i}. {g['pub']} (연도 확인 불가 — 인용할 때 연도를 쓰지 말 것)"
        else:
            head = f"{i}. 출처 확인 불가 — 이 항목은 인용하지 말 것"
        lines.append(head)
        for f in g["items"]:
            lines.append(f"   - {f['fact']}")
            if f.get("quote"):
                lines.append(f"     원문: \"{f['quote'][:160]}\"")
        if g["url"]:
            lines.append(f"   URL: {g['url']}")
    return "\n".join(lines)


# ────────────────────────── 전체 ──────────────────────────

def _cache_key(form: dict, question_id: str) -> str:
    h = hashlib.sha1(f"{form.get('track')}|{form.get('idea', '')}|{question_id}".encode("utf-8")).hexdigest()
    return f"research-facts|{h}"


def _merge_facts(primary: list[dict], secondary: list[dict], limit: int = MAX_FACTS) -> list[dict]:
    """문항 전용 사실을 먼저 넣고 남는 자리를 공통 조사 사실로 채운다. 같은 (URL, 사실) 은 한 번만."""
    out, seen = [], set()
    for f in list(primary) + list(secondary):
        key = (_url_key(str(f.get("url", ""))), _norm(str(f.get("fact", ""))))
        if not key[1] or key in seen:
            continue
        seen.add(key)
        out.append(f)
        if len(out) >= limit:
            break
    return out


def _page_row(p: dict) -> dict:
    return {"url": p["url"], "title": p.get("title", ""), "from_snippet": p.get("from_snippet", False)}


async def run_research(client: LLMClient, researcher: Researcher, form: dict, question_id: str,
                       cfg: ResearchConfig | None = None, cache: DiskCache | None = None, fetch=fetch_page) -> dict:
    """문항 조사. 결과는 (아이디어, 문항) 단위로 디스크 캐시.

    기본 경로(cfg.share_idea_research=True, RESEARCH_PLAN 2단계):
      아이디어 공통 조사를 한 번만 하고(두 번째 문항부터는 캐시 적중 = 외부 요청 0건),
      그 사실로 못 채우는 각도만 보완 검색한다. 모델 호출 1~2회, 외부 요청 0~(보완 검색어+페이지)건.
    예전 경로(False): 문항마다 검색어 4개로 처음부터 조사. 모델 호출 2회, 외부 요청 10건."""
    cfg = cfg or ResearchConfig()
    cache = cache if cache is not None else DiskCache(ttl=cfg.cache_ttl)
    key = _cache_key(form, question_id)
    hit = cache.get(key)
    if hit is not None and hit and isinstance(hit[0], dict) and "facts" in hit[0]:
        # 2단계 이전에 저장된 캐시에는 새 필드가 없다 — 기본값을 깔아 응답 모양을 항상 같게 유지한다.
        return {"shared_queries": [], "followup_queries": [], "shared_facts_used": 0, **hit[0], "cached": True}

    meta, body = assemble.load_question(question_id)
    meta = {**meta, "body": body}

    if not cfg.share_idea_research:
        queries = await generate_queries(client, form, meta, cfg)
        results, pages = await collect_pages(researcher, queries, cfg, fetch=fetch)
        facts = await extract_facts(client, form, meta, pages, cfg)
        shared_queries: list[str] = []
        shared_used = 0
        shared_pages: list[dict] = []
        shared_result_count = 0
    else:
        # 아이디어 공통 조사 — 이 아이디어의 첫 문항에서만 실제로 돌고, 나머지 8문항은 캐시에서 온다.
        shared = await run_idea_research(client, researcher, form, cfg, cache=cache, fetch=fetch)
        shared_queries = list(shared.get("queries") or [])
        shared_pages = list(shared.get("pages") or [])
        shared_facts = list(shared.get("facts") or [])
        shared_result_count = int(shared.get("result_count") or 0)

        queries = await generate_followup_queries(client, form, meta, shared, cfg)
        results, pages = [], []
        new_facts: list[dict] = []
        if queries:
            results, pages = await collect_pages(
                researcher, queries, cfg, fetch=fetch,
                skip_urls={p.get("url", "") for p in shared_pages},   # 공통 조사에서 이미 본 페이지는 다시 안 받는다
                max_pages=cfg.max_followup_pages)
            new_facts = await extract_facts(client, form, meta, pages, cfg)
        facts = _merge_facts(new_facts, shared_facts)
        shared_used = sum(1 for f in facts if f not in new_facts)

    out = {
        "question_id": question_id,
        "queries": (shared_queries + queries) if cfg.share_idea_research else queries,
        "backend": researcher.last_backend,
        "result_count": shared_result_count + len(results),
        "pages": (shared_pages if cfg.share_idea_research else []) + [_page_row(p) for p in pages],
        "facts": facts,
        "references": format_references(facts),
        "cached": False,
        # ── 2단계에서 추가된 필드 (기존 필드는 그대로) ──
        "shared_queries": shared_queries,        # 아이디어 공통 조사에서 쓴 검색어
        "followup_queries": queries if cfg.share_idea_research else [],   # 이 문항에서만 추가로 검색한 것
        "shared_facts_used": shared_used,        # facts 중 공통 조사에서 물려받은 개수
    }
    out["sources"] = timing.flush_counts(kind="research", question_id=question_id) or {}
    cache.set(key, [out])
    return out

# ────────────────────────── 아이디어 단위 조사 (인테이크 카드용) ──────────────────────────
# 문항 단위 조사(run_research)와 달리, 아이디어 자체를 주제별로 훑는다.
# 인테이크 카드의 보기를 지어내지 않고 "실제로 있는 서비스·실제 고객 불만·실제 통계"에 근거해 만들기 위한 재료다.

IDEA_TOPICS = [
    "이미 있는 비슷한 서비스·경쟁 업체 (이름과 방식)",
    "이 분야 고객이 실제로 겪는 불편·불만",
    "시장 규모·이용자 수·성장률 같은 통계",
    "비슷한 서비스가 돈을 버는 방식(수수료·구독·광고 등)",
]

IDEA_RESEARCH_KEY = "__idea__"


def _idea_meta(form: dict) -> dict:
    """run_research 의 문항 meta 자리에 넣을 '아이디어 조사' 의사(擬似) 문항."""
    return {
        "label": "아이디어 조사",
        "title": "이 아이디어를 판단하는 데 필요한 사실 모으기",
        "body": (
            "아래 주제별로 실제 사례와 수치를 찾는다. 지원서 문항이 아니라 아이디어 자체를 파악하기 위한 조사다.\n"
            + "\n".join(f"- {t}" for t in IDEA_TOPICS)
        ),
    }


async def run_idea_research(client: LLMClient, researcher: Researcher, form: dict,
                            cfg: ResearchConfig | None = None, cache: DiskCache | None = None,
                            fetch=fetch_page) -> dict:
    """아이디어 전체에 대한 주제 단위 조사. 모델 호출 2회(검색어 생성, 사실 추출).

        facts = await run_idea_research(research_client, researcher, form, cfg)
        form["references"] = facts["facts"]      # 인테이크 프롬프트에 [웹 참고자료] 로 주입

    run_research 와 같은 부품을 쓰고 문항 meta 만 '아이디어 조사'로 바꾼 것이다."""
    cfg = cfg or ResearchConfig()
    cache = cache if cache is not None else DiskCache(ttl=cfg.cache_ttl)
    key = _cache_key(form, IDEA_RESEARCH_KEY)
    hit = cache.get(key)
    if hit is not None and hit and isinstance(hit[0], dict) and "facts" in hit[0]:
        return {**hit[0], "cached": True}

    meta = _idea_meta(form)
    queries = await generate_queries(client, form, meta, cfg)
    results, pages = await collect_pages(researcher, queries, cfg, fetch=fetch)
    facts = await extract_facts(client, form, meta, pages, cfg)
    out = {
        "question_id": IDEA_RESEARCH_KEY,
        "queries": queries,
        "backend": researcher.last_backend,
        "result_count": len(results),
        "pages": [{"url": p["url"], "title": p.get("title", ""), "from_snippet": p.get("from_snippet", False)} for p in pages],
        "facts": facts,
        "references": format_references(facts),
        "cached": False,
    }
    out["sources"] = timing.flush_counts(kind="idea_research") or {}
    cache.set(key, [out])
    return out
