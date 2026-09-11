"""정형(공공) API 어댑터 — 검색+페이지 fetch 없이 JSON 으로 바로 수치를 가져온다 (RESEARCH_PLAN 3단계).

웹 검색 백엔드(`research.py`)와 다른 점:
  - 폴백 사슬이 아니라 **덧붙이는 소스**다. `Researcher(extras=[...])` 로 붙어 웹 결과 앞에 합쳐진다.
  - 결과에 `no_fetch: True` 가 붙는다 → `collect_pages` 가 이 결과는 **HTTP fetch 하지 않고**
    snippet 을 그대로 본문으로 쓴다 (이미 사실 문장이라 받아올 페이지가 없다).
  - 검색어가 그 소스와 무관하면 **HTTP 요청 자체를 하지 않는다**(키워드 트리거) — 한도를 아끼고 로그도 읽기 쉽다.

소스별 담당 (RESEARCH_PLAN 3단계 표):
  KOSIS       통계표 검색 → 상위 표의 최신 수치        키: KOSIS_API_KEY
  ECOS        한국은행 100대 통계지표 (금리·환율·경기)  키: ECOS_API_KEY
  K-Startup   정부 창업지원사업 공고 검색               키: DATA_GO_KR_API_KEY
  상권정보     업종 대분류별 전국 상가업소 수            키: DATA_GO_KR_API_KEY

키가 없는 소스는 목록에서 자동으로 빠진다 — 키 없이도 지금까지와 똑같이 동작한다.

라우팅은 지금 키워드 매칭이다. 모델로 질의 유형을 분류하는 방식(RESEARCH_PLAN 3단계 원안)은
호출이 한 번 더 늘고 디버깅이 어려워서, 실사용 로그를 본 뒤에 필요하면 바꾼다.
"""
from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass, field

from .. import timing
from .breaker import Breaker
from .research import make_result

DEFAULT_TIMEOUT = 15

# 소스별 키워드 트리거 — 하나라도 걸리면 그 소스에 물어본다.
TRIGGERS = {
    "kosis": ("통계", "인구", "가구", "현황", "규모", "비중", "추이", "실태", "조사", "고용", "사업체", "소득"),
    "ecos": ("금리", "환율", "물가", "경기", "소비자심리", "가계", "대출", "예금", "생산자물가", "국내총생산", "gdp"),
    "kstartup": ("창업지원", "지원사업", "정부지원", "공고", "모집", "바우처", "사업화", "창업 지원", "예비창업"),
    "sangkwon": ("상권", "업종", "점포", "자영업", "소상공인", "업소", "상가", "매장", "가맹"),
    "kci": ("논문", "학술", "연구", "선행연구", "문헌", "학회", "효과", "요인", "인식", "실태"),
}


def triggered(name: str, query: str) -> bool:
    q = (query or "").lower()
    return any(t in q for t in TRIGGERS.get(name, ()))


def _year_date(value: str | None) -> str | None:
    """KOSIS PRD_DE('2025') · 상권정보 stdrYm('202606') 처럼 축약된 기간을 날짜로."""
    s = re.sub(r"\D", "", str(value or ""))
    if len(s) == 4:
        return f"{s}-01-01"
    if len(s) == 6:
        return f"{s[:4]}-{s[4:]}-01"
    if len(s) == 8:
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    return None


def clean_label(value) -> str:
    """KOSIS 항목명에는 줄바꿈 태그가 섞여 온다 — 반각 <br> 도 전각 ＜br＞ 도 실제로 온다."""
    text = re.sub(r"[<＜]\s*br\s*/?\s*[>＞]", " ", str(value or ""), flags=re.I)
    return re.sub(r"\s+", " ", text).strip()


def stat_result(title: str, url: str, snippet: str, date=None, source_type: str = "stat") -> dict:
    """정형 소스 결과 — fetch 하지 않고 snippet 을 본문으로 쓴다는 표시(no_fetch)를 단다."""
    row = make_result(title, url, snippet, date, source_type)
    row["no_fetch"] = True
    return row


async def _get_json(url: str, params: dict, timeout: int = DEFAULT_TIMEOUT, source: str = ""):
    """정형 API 한 번 조회. source 를 주면 외부 호출 카운터에 남는다 (작업 19)."""
    import httpx
    timing.count(source)
    async with httpx.AsyncClient(timeout=timeout) as c:
        r = await c.get(url, params=params)
        r.raise_for_status()
        return r.json()


# ────────────────────────── KOSIS (국가통계포털) ──────────────────────────

KOSIS_SEARCH = "https://kosis.kr/openapi/statisticsSearch.do"
KOSIS_DATA = "https://kosis.kr/openapi/Param/statisticsParameterData.do"
KOSIS_TABLE_URL = "https://kosis.kr/statHtml/statHtml.do?orgId={org}&tblId={tbl}"
KOSIS_TOTAL_ROWS = ("전국", "계", "합계", "전체")     # 통계표에서 먼저 인용할 행


class KosisStats:
    """통계표 검색 → 상위 표의 최신 수치. 요청 최대 2회(검색 1 + 수치 1).

    수치 조회는 분류 축이 하나인 표에서만 성공한다 — 축이 여러 개면 KOSIS 가 '40,000셀 초과'로 거절한다.
    **수치를 못 가져오면 아무것도 돌려주지 않는다.** "이런 통계표가 있다"는 문장은 근거로 쓸 수 없고
    (2026-09-02 실호출에서 관련 없는 관광 통계표가 그렇게 끼어들었다) 진짜 근거 자리만 차지하기 때문이다.
    표마다 필요한 축(objL1~8)·항목(itmId) 조합은 KOSIS 가 표별로 발급하는 값이라, 모든 표에서 수치를
    뽑으려면 그 목록을 따로 갖춰야 한다(후속 과제 — 자주 쓰는 표부터 목록화).
    """

    def __init__(self, api_key: str, timeout: int = DEFAULT_TIMEOUT, max_rows: int = 6):
        self.api_key = api_key
        self.timeout = timeout
        self.max_rows = max_rows
        self.last_error: str | None = None

    async def tables(self, query: str, count: int = 3) -> list[dict]:
        data = await _get_json(KOSIS_SEARCH, {
            "method": "getList", "apiKey": self.api_key, "format": "json", "jsonVD": "Y",
            "searchNm": query, "startCount": 1, "resultCount": count}, self.timeout, "kosis")
        return data if isinstance(data, list) else []

    async def values(self, org_id: str, tbl_id: str) -> list[dict]:
        data = await _get_json(KOSIS_DATA, {
            "method": "getList", "apiKey": self.api_key, "format": "json", "jsonVD": "Y",
            "orgId": org_id, "tblId": tbl_id, "objL1": "ALL", "itmId": "ALL",
            "prdSe": "Y", "newEstPrdCnt": 1}, self.timeout, "kosis")
        if isinstance(data, dict):                      # {"err": "31", "errMsg": "...셀 초과..."}
            self.last_error = f"KOSIS {data.get('err')}: {data.get('errMsg', '')[:80]}"
            return []
        return data if isinstance(data, list) else []

    @staticmethod
    def best_table(tables: list[dict], query: str) -> dict | None:
        """검색어 낱말이 표 이름에 가장 많이 들어간 표. 하나도 안 겹치면 무관한 표로 보고 버린다.

        KOSIS 검색은 표 이름이 아니라 표 안의 항목까지 훑어서, 첫 결과가 엉뚱할 때가 많다."""
        words = [w for w in re.split(r"[^\w가-힣]+", query or "") if len(w) >= 2]
        scored = [(sum(1 for w in words if w in str(t.get("TBL_NM", ""))), i, t)
                  for i, t in enumerate(tables)]
        best = max(scored, key=lambda x: (x[0], -x[1]), default=None)
        return best[2] if best and best[0] > 0 else None

    def _pick(self, rows: list[dict]) -> list[dict]:
        """전국·합계 행을 먼저, 없으면 앞에서부터."""
        head = [r for r in rows if str(r.get("C1_NM", "")).strip() in KOSIS_TOTAL_ROWS]
        rest = [r for r in rows if r not in head]
        return (head + rest)[: self.max_rows]

    async def __call__(self, query: str, max_results: int = 8) -> list[dict]:
        self.last_error = None
        tables = await self.tables(query)
        t = self.best_table(tables, query)
        if t is None:
            self.last_error = "검색어와 이름이 겹치는 통계표 없음"
            return []
        org, tbl = str(t.get("ORG_ID", "")), str(t.get("TBL_ID", ""))
        url = KOSIS_TABLE_URL.format(org=org, tbl=tbl)
        title = str(t.get("TBL_NM", "")).strip()
        publisher = str(t.get("ORG_NM", "국가통계포털")).strip()
        period = f"{t.get('STRT_PRD_DE', '')}~{t.get('END_PRD_DE', '')}".strip("~")

        rows = await self.values(org, tbl) if org and tbl else []
        if not rows:            # 수치를 못 가져오면 근거가 아니다 — 아무것도 주지 않는다
            self.last_error = self.last_error or f"「{title}」 수치 조회 실패"
            return []
        lines = []
        for r in self._pick(rows):
            unit = clean_label(r.get("UNIT_NM"))
            lines.append(f"{clean_label(r.get('C1_NM'))} {clean_label(r.get('ITM_NM'))}: "
                         f"{r.get('DT', '')}{unit} ({r.get('PRD_DE', '')})")
        snippet = f"{publisher} 「{title}」({period}) 최신 수치. " + " / ".join(lines)
        return [stat_result(f"{title} ({publisher})", url, snippet,
                            _year_date(str(rows[0].get("PRD_DE", ""))))][:max_results]


# ────────────────────────── ECOS (한국은행) ──────────────────────────

ECOS_KEYSTAT = "https://ecos.bok.or.kr/api/KeyStatisticList/{key}/json/kr/1/100"
ECOS_URL = "https://ecos.bok.or.kr/"


class EcosKeyStats:
    """한국은행 100대 통계지표 — 요청 1회로 환율·금리·물가·가계 지표를 다 받아 검색어와 맞는 것만 고른다.

    지표 목록은 하루에도 몇 번 안 바뀌므로 인스턴스 안에 메모리 캐시를 둔다(프로세스 수명 동안)."""

    def __init__(self, api_key: str, timeout: int = DEFAULT_TIMEOUT, max_items: int = 5):
        self.api_key = api_key
        self.timeout = timeout
        self.max_items = max_items
        self._cache: list[dict] | None = None

    async def indicators(self) -> list[dict]:
        if self._cache is None:
            data = await _get_json(ECOS_KEYSTAT.format(key=self.api_key), {}, self.timeout, "ecos")
            self._cache = ((data or {}).get("KeyStatisticList") or {}).get("row") or []
        return self._cache

    @staticmethod
    def _score(row: dict, query: str) -> int:
        text = f"{row.get('CLASS_NAME', '')} {row.get('KEYSTAT_NAME', '')}"
        words = [w for w in re.split(r"[^\w가-힣]+", query) if len(w) >= 2]
        return sum(1 for w in words if w in text)

    async def __call__(self, query: str, max_results: int = 8) -> list[dict]:
        rows = await self.indicators()
        hits = sorted(((self._score(r, query), i, r) for i, r in enumerate(rows)), key=lambda x: (-x[0], x[1]))
        picked = [r for s, _, r in hits if s > 0][: self.max_items]
        if not picked:
            return []
        lines = [f"{r.get('KEYSTAT_NAME', '')} {r.get('DATA_VALUE', '')}{r.get('UNIT_NAME', '')}"
                 f" ({r.get('CYCLE', '')} 기준)" for r in picked]
        snippet = "한국은행 ECOS 100대 통계지표. " + " / ".join(lines)
        return [stat_result("한국은행 100대 통계지표", ECOS_URL, snippet,
                            _year_date(str(picked[0].get("CYCLE", ""))), "stat")][:max_results]


# ────────────────────────── K-Startup (공공데이터포털) ──────────────────────────

KSTARTUP_URL = "https://apis.data.go.kr/B552735/kisedKstartupService01/getAnnouncementInformation01"


class KStartupSearch:
    """정부 창업지원사업 공고 검색 (공고명 LIKE). 요청 1회.

    공고는 본문(pbanc_ctnt)이 길어 그대로 근거가 된다 — 상세 페이지를 따로 받아올 필요가 없다."""

    def __init__(self, api_key: str, timeout: int = DEFAULT_TIMEOUT, max_items: int = 3):
        self.api_key = api_key
        self.timeout = timeout
        self.max_items = max_items

    @staticmethod
    def keyword(query: str) -> str:
        """공고명 검색은 짧은 낱말이 잘 맞는다 — 검색어에서 가장 긴 한국어 낱말 하나를 쓴다."""
        words = [w for w in re.split(r"[^\w가-힣]+", query or "") if len(w) >= 2]
        skip = {"통계", "현황", "지원", "사업", "정부", "공고"}
        cands = [w for w in words if w not in skip] or words
        return max(cands, key=len) if cands else (query or "")[:10]

    async def __call__(self, query: str, max_results: int = 8) -> list[dict]:
        kw = self.keyword(query)
        data = await _get_json(KSTARTUP_URL, {
            "serviceKey": self.api_key, "page": 1, "perPage": self.max_items, "returnType": "json",
            "cond[biz_pbanc_nm::LIKE]": kw}, self.timeout, "kstartup")
        out = []
        for item in (data or {}).get("data") or []:
            name = str(item.get("biz_pbanc_nm") or item.get("intg_pbanc_biz_nm") or "").strip()
            body = re.sub(r"\s+", " ", str(item.get("pbanc_ctnt") or ""))[:800]
            target = str(item.get("aply_trgt_ctnt") or item.get("aply_trgt") or "").strip()
            dept = str(item.get("biz_prch_dprt_nm") or "").strip()
            url = str(item.get("detl_pg_url") or "https://www.k-startup.go.kr/").strip()
            snippet = f"K-Startup 공고 「{name}」({dept}). 지원대상: {target[:200]}. {body}"
            if len(snippet) < 40:
                continue
            out.append(stat_result(name or "K-Startup 공고", url, snippet,
                                   item.get("pbanc_rcpt_bgng_dt"), "policy"))
        return out[:max_results]


# ────────────────────────── 상권정보 (소상공인시장진흥공단) ──────────────────────────

SDSC_BASE = "https://apis.data.go.kr/B553077/api/open/sdsc2"
SANGKWON_URL = "https://sg.sbiz.or.kr/"


class SangKwonStats:
    """업종 대분류별 전국 상가업소 수. 요청 2회(업종 목록 1 + 개수 1), 업종 목록은 메모리 캐시."""

    def __init__(self, api_key: str, timeout: int = DEFAULT_TIMEOUT):
        self.api_key = api_key
        self.timeout = timeout
        self._upjong: list[dict] | None = None

    async def upjong(self) -> list[dict]:
        if self._upjong is None:
            data = await _get_json(f"{SDSC_BASE}/largeUpjongList", {
                "serviceKey": self.api_key, "numOfRows": 100, "pageNo": 1, "type": "json"}, self.timeout, "sangkwon")
            self._upjong = ((data or {}).get("body") or {}).get("items") or []
        return self._upjong

    @staticmethod
    def match(rows: list[dict], query: str) -> dict | None:
        """업종명이 검색어에 들어 있으면 그 업종. '음식'·'교육'처럼 이름이 짧아 그대로 걸린다."""
        best = None
        for r in rows:
            name = str(r.get("indsLclsNm", ""))
            for part in re.split(r"[·]", name):
                if len(part) >= 2 and part in query and (best is None or len(part) > len(best[1])):
                    best = (r, part)
        return best[0] if best else None

    async def __call__(self, query: str, max_results: int = 8) -> list[dict]:
        rows = await self.upjong()
        hit = self.match(rows, query)
        if not hit:
            return []
        data = await _get_json(f"{SDSC_BASE}/storeListInUpjong", {
            "serviceKey": self.api_key, "divId": "indsLclsCd", "key": hit.get("indsLclsCd"),
            "numOfRows": 1, "pageNo": 1, "type": "json"}, self.timeout, "sangkwon")
        body = (data or {}).get("body") or {}
        total = body.get("totalCount")
        if not total:
            return []
        stdr = ((data or {}).get("header") or {}).get("stdrYm") or ""
        name = hit.get("indsLclsNm", "")
        snippet = (f"소상공인시장진흥공단 상권정보 기준 전국 '{name}' 업종 상가업소는 {int(total):,}개다"
                   f" ({stdr[:4]}년 {stdr[4:]}월 기준 자료).")
        return [stat_result(f"전국 {name} 업종 상가업소 수", SANGKWON_URL, snippet, _year_date(stdr), "stat")]


# ────────────────────────── KCI (한국학술지인용색인) ──────────────────────────
# 이용 준수 사항은 RESEARCH_PLAN "API 한도" 절 6항. 코드로 강제하는 것:
#   ① 메타데이터만 — no_fetch. 원문을 받아오지 않는다 (제목·저자·학술지·연도·초록까지만)
#   ② 벡터DB 색인 제외 — source_type "kci" 는 pipeline 이 upsert_pages 에 넘기지 않는다
#   ③ 왜곡 금지 — 우리는 있는 그대로 넘기고, 수치화 금지는 프롬프트(references 규칙)가 맡는다
#   ④ UI 출처 표기 — facts 에 source_kind 를 실어 프론트가 배지를 붙일 수 있게 한다
#   ⑤ 파기 — scripts/purge_kci_cache.py

KCI_SEARCH = "https://open.kci.go.kr/po/openapi/openApiSearch.kci"
KCI_ARTICLE_URL = ("https://www.kci.go.kr/kciportal/ci/sereArticleSearch/ciSereArtiView.kci"
                   "?sereArticleSearchBean.artiId={artid}")
KCI_ABSTRACT_CHARS = 400


def _cdata(node, path: str) -> str:
    found = node.find(path)
    return clean_label(found.text if found is not None and found.text else "")


class KciSearch:
    """KCI 논문 검색 — 제목·저자·학술지·연도·초록만. 요청 1회.

    KCI 의 title 검색은 낱말을 느슨하게 쪼개 매칭한다("1인가구" 로 찾으면 '1' 이 걸려 무관한 논문이 섞인다).
    그래서 **받아온 논문 제목·초록에 검색 낱말이 실제로 있는 것만** 남긴다 — 근거로 인용될 자료라
    관련 없는 논문이 끼면 안 된다.
    """

    def __init__(self, api_key: str, timeout: int = DEFAULT_TIMEOUT, max_items: int = 3):
        self.api_key = api_key
        self.timeout = timeout
        self.max_items = max_items

    @staticmethod
    def terms(query: str) -> list[str]:
        """검색에 쓸 한국어 낱말. 숫자는 뺀다 — KCI 가 숫자만 따로 매칭해 엉뚱한 결과를 준다."""
        words = [w for w in re.split(r"[^가-힣A-Za-z]+", query or "") if len(w) >= 2]
        return sorted(words, key=len, reverse=True)[:2]

    async def fetch(self, term: str) -> str:
        import httpx
        timing.count("kci")
        async with httpx.AsyncClient(timeout=self.timeout) as c:
            r = await c.get(KCI_SEARCH, params={"apiCode": "articleSearch", "key": self.api_key,
                                                "title": term, "displayCount": 10, "page": 1})
            r.raise_for_status()
            return r.text

    def parse(self, xml_text: str, terms: list[str]) -> list[dict]:
        import xml.etree.ElementTree as ET
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError:
            return []
        out = []
        for rec in root.iter("record"):
            title = _cdata(rec, ".//article-title")
            abstract = _cdata(rec, ".//abstract")[:KCI_ABSTRACT_CHARS]
            haystack = f"{title} {abstract}"
            if terms and not any(t in haystack for t in terms):
                continue                     # KCI 매칭이 헐거워 무관한 논문이 섞인다 — 제목·초록으로 다시 거른다
            info = rec.find("journalInfo")
            journal = _cdata(info, "journal-name") if info is not None else ""
            publisher = _cdata(info, "publisher-name") if info is not None else ""
            year = _cdata(info, "pub-year") if info is not None else ""
            author = _cdata(rec, ".//author")
            art = rec.find("articleInfo")
            art_id = (art.get("article-id") if art is not None else "") or ""
            url = _cdata(rec, ".//url") or KCI_ARTICLE_URL.format(artid=art_id)
            if not title or not url:
                continue
            snippet = (f"KCI 등재 논문 「{title}」 — {author or '저자 미상'}, {journal or '학술지 미상'}"
                       f"{f' {year}년' if year else ''}. 초록: {abstract}")
            out.append(stat_result(title, url, snippet, _year_date(year), "kci"))
        return out

    async def __call__(self, query: str, max_results: int = 8) -> list[dict]:
        terms = self.terms(query)
        if not terms:
            return []
        rows = self.parse(await self.fetch(terms[0]), terms)
        return rows[: min(self.max_items, max_results)]


# ────────────────────────── 묶기 ──────────────────────────

@dataclass
class OpenDataConfig:
    enabled: bool = True
    sources: list[str] = field(default_factory=lambda: ["kosis", "ecos", "kstartup", "sangkwon", "kci"])
    timeout: int = DEFAULT_TIMEOUT

    @classmethod
    def from_config(cls, rc: dict) -> "OpenDataConfig":
        oc = (rc or {}).get("opendata") or {}
        return cls(
            enabled=bool(oc.get("enabled", True)),
            sources=[s for s in oc.get("sources", ["kosis", "ecos", "kstartup", "sangkwon", "kci"])
                     if s in TRIGGERS],
            timeout=int(oc.get("timeout", DEFAULT_TIMEOUT)),
        )


def build_sources(cfg: OpenDataConfig, env: dict | None = None) -> list[tuple[str, object]]:
    """설정 + .env 키로 쓸 수 있는 소스만 만든다. 키가 없으면 그 소스는 빠진다."""
    env = env if env is not None else os.environ
    kosis, ecos, dgk = env.get("KOSIS_API_KEY", ""), env.get("ECOS_API_KEY", ""), env.get("DATA_GO_KR_API_KEY", "")
    kci = env.get("KCI_API_KEY", "")
    made: list[tuple[str, object]] = []
    if not cfg.enabled:
        return made
    for name in cfg.sources:
        if name == "kosis" and kosis:
            made.append((name, KosisStats(kosis, cfg.timeout)))
        elif name == "ecos" and ecos:
            made.append((name, EcosKeyStats(ecos, cfg.timeout)))
        elif name == "kstartup" and dgk:
            made.append((name, KStartupSearch(dgk, cfg.timeout)))
        elif name == "sangkwon" and dgk:
            made.append((name, SangKwonStats(dgk, cfg.timeout)))
        elif name == "kci" and kci:
            made.append((name, KciSearch(kci, cfg.timeout)))
    return made


class OpenDataSources:
    """검색어에 맞는 정형 소스만 골라 병렬로 물어본다. 실패한 소스는 조용히 건너뛴다(웹 검색이 본류다)."""

    def __init__(self, sources: list[tuple[str, object]], breaker: Breaker | None = None):
        self.sources = sources
        self.breaker = breaker            # 연속 실패한 소스는 쉬게 한다 (RESEARCH_PLAN 5단계)
        self.last_errors: dict[str, str] = {}
        self.last_used: list[str] = []

    async def __call__(self, query: str, max_results: int = 4) -> list[dict]:
        picked = [(n, fn) for n, fn in self.sources
                  if triggered(n, query) and not (self.breaker and self.breaker.is_open(n))]
        self.last_errors, self.last_used = {}, [n for n, _ in picked]
        if not picked:
            return []

        async def one(name, fn):
            try:
                rows = await fn(query, max_results)
                if self.breaker:
                    self.breaker.record(name, ok=True)
                return rows
            except Exception as e:
                self.last_errors[name] = f"{type(e).__name__}: {str(e)[:120]}"
                if self.breaker:
                    self.breaker.record(name, ok=False)
                return []

        results = await asyncio.gather(*(one(n, fn) for n, fn in picked))
        out: list[dict] = []
        for rows in results:
            out.extend(rows)
        return out[:max_results]
