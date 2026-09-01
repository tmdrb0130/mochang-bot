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

from ..llm.client import LLMClient
from ..pipeline import assemble
from .research import DiskCache, Researcher, domain

MAX_PAGE_CHARS = 6000
MIN_BODY_CHARS = 40      # 이보다 짧은 본문/스니펫은 사실 추출에 못 쓴다
MAX_FACTS = 8
PREFERRED_DOMAINS = ("go.kr", "or.kr", "re.kr", "kostat", "kosis", "news", "yna.co.kr", "hankyung", "mk.co.kr",
                     "chosun", "joongang", "donga", "hani.co.kr", "khan.co.kr", "sedaily", "edaily", "etnews", "zdnet")
SKIP_EXT = (".pdf", ".hwp", ".xls", ".xlsx", ".ppt", ".pptx", ".doc", ".docx", ".zip")


@dataclass
class ResearchConfig:
    max_queries: int = 4
    max_results_per_query: int = 6
    max_pages_to_extract: int = 6
    fetch_timeout: int = 15
    cache_ttl: int = 7 * 24 * 3600

    @classmethod
    def from_config(cls, config: dict) -> "ResearchConfig":
        rc = (config or {}).get("research") or {}
        return cls(
            max_queries=int(rc.get("max_queries", 4)),
            max_results_per_query=int(rc.get("max_results_per_query", 6)),
            max_pages_to_extract=int(rc.get("max_pages_to_extract", 6)),
            fetch_timeout=int(rc.get("fetch_timeout", 15)),
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


async def collect_pages(researcher: Researcher, queries: list[str], cfg: ResearchConfig,
                        fetch=fetch_page) -> tuple[list[dict], list[dict]]:
    """검색 → 랭킹 → 상위 N 페이지 본문. (검색 결과 전체, 본문 있는 페이지) 반환."""
    all_results: list[dict] = []
    for q in queries:
        rows = await researcher.search(q, cfg.max_results_per_query)
        for r in rows:
            all_results.append({**r, "query": q})
    seen, unique = set(), []
    for r in all_results:
        k = r["url"].split("#")[0].rstrip("/")
        if k and k not in seen:
            seen.add(k)
            unique.append(r)
    ranked = rank_results(unique)[: cfg.max_pages_to_extract * 2]
    texts = await asyncio.gather(*(fetch(r["url"], cfg.fetch_timeout) for r in ranked))
    pages = []
    for r, t in zip(ranked, texts):
        body = t or r.get("snippet") or ""
        if len(body) >= MIN_BODY_CHARS:
            pages.append({**r, "text": body, "from_snippet": not bool(t)})
        if len(pages) >= cfg.max_pages_to_extract:
            break
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
    out = []
    for f in facts[:MAX_FACTS]:
        out.append({
            "fact": str(f.get("fact", "")).strip(),
            "quote": str(f.get("quote", "")).strip(),
            "source_title": str(f.get("source_title", "")).strip(),
            "url": str(f.get("url", "")).strip(),
            "date": (str(f["date"]).strip() if f.get("date") not in (None, "", "null") else None),
            "publisher": str(f.get("publisher") or domain(str(f.get("url", "")))).strip(),
            "use_for": str(f.get("use_for", "")).strip(),
        })
    return out


# ────────────────────────── 5) 참고자료 섹션 ──────────────────────────

def format_references(facts: list[dict]) -> str:
    """문항 생성 프롬프트에 넣는 [웹 참고자료] 섹션. 비어 있으면 빈 문자열."""
    if not facts:
        return ""
    lines = [assemble.section_headers().get("references", "[웹 참고자료]")]
    for i, f in enumerate(facts, 1):
        year = (f.get("date") or "")[:4] or "연도 미상"
        pub = f.get("publisher") or f.get("source_title") or "출처 미상"
        lines.append(f"{i}. {f['fact']} (출처: {pub}, {year})")
        if f.get("quote"):
            lines.append(f"   원문: \"{f['quote'][:160]}\"")
        if f.get("url"):
            lines.append(f"   URL: {f['url']}")
    return "\n".join(lines)


# ────────────────────────── 전체 ──────────────────────────

def _cache_key(form: dict, question_id: str) -> str:
    h = hashlib.sha1(f"{form.get('track')}|{form.get('idea', '')}|{question_id}".encode("utf-8")).hexdigest()
    return f"research-facts|{h}"


async def run_research(client: LLMClient, researcher: Researcher, form: dict, question_id: str,
                       cfg: ResearchConfig | None = None, cache: DiskCache | None = None, fetch=fetch_page) -> dict:
    """전체 파이프라인. 모델 호출 2회(검색어 생성, 사실 추출). 결과는 (아이디어, 문항) 단위로 디스크 캐시."""
    cfg = cfg or ResearchConfig()
    cache = cache if cache is not None else DiskCache(ttl=cfg.cache_ttl)
    key = _cache_key(form, question_id)
    hit = cache.get(key)
    if hit is not None and hit and isinstance(hit[0], dict) and "facts" in hit[0]:
        return {**hit[0], "cached": True}

    meta, body = assemble.load_question(question_id)
    meta = {**meta, "body": body}
    queries = await generate_queries(client, form, meta, cfg)
    results, pages = await collect_pages(researcher, queries, cfg, fetch=fetch)
    facts = await extract_facts(client, form, meta, pages, cfg)
    out = {
        "question_id": question_id,
        "queries": queries,
        "backend": researcher.last_backend,
        "result_count": len(results),
        "pages": [{"url": p["url"], "title": p.get("title", ""), "from_snippet": p.get("from_snippet", False)} for p in pages],
        "facts": facts,
        "references": format_references(facts),
        "cached": False,
    }
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
    cache.set(key, [out])
    return out
