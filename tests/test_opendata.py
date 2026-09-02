"""정형 공공 API 어댑터 (KOSIS·ECOS·K-Startup·상권정보) — 실제 HTTP 호출 없이 가짜 응답으로.

실제 응답 형태는 2026-09-02 실호출로 확인한 것을 그대로 옮겼다 (scripts/opendata_check.py 로 재확인 가능).
"""
import pytest

from backend.rag import opendata as O


def fake_get_json(mapping, calls=None):
    """URL 에 들어 있는 조각으로 응답을 고르는 가짜 _get_json."""
    async def _get(url, params, timeout=15):
        if calls is not None:
            calls.append({"url": url, "params": params})
        for frag, value in mapping.items():
            if frag in url:
                if isinstance(value, Exception):
                    raise value
                return value
        raise AssertionError(f"예상 못한 URL: {url}")
    return _get


# ── 트리거 ──

def test_triggers_pick_only_relevant_sources():
    assert O.triggered("kosis", "1인 가구 현황 통계") and not O.triggered("ecos", "1인 가구 현황 통계")
    assert O.triggered("ecos", "기준금리 추이") and O.triggered("kstartup", "예비창업 지원사업 공고")
    assert O.triggered("sangkwon", "음식 업종 점포 수") and not O.triggered("kstartup", "음식 업종 점포 수")
    assert not any(O.triggered(n, "고양이 사진") for n in O.TRIGGERS)


def test_year_date_normalizes_short_periods():
    assert O._year_date("2025") == "2025-01-01"          # KOSIS PRD_DE
    assert O._year_date("202606") == "2026-06-01"        # 상권정보 stdrYm
    assert O._year_date("20260901") == "2026-09-01"      # ECOS CYCLE
    assert O._year_date("2024Q4") is None and O._year_date(None) is None


def test_clean_label_strips_kosis_line_break_tags():
    assert O.clean_label("1인가구<br>(A)") == "1인가구 (A)"
    assert O.clean_label("1인가구비율＜br＞(A÷B×100)") == "1인가구비율 (A÷B×100)"   # KOSIS 는 전각으로도 준다
    assert O.clean_label(None) == ""


def test_stat_result_marks_no_fetch():
    r = O.stat_result("제목", "https://kosis.kr/x", "본문 " * 20, "2025")
    assert r["no_fetch"] is True and r["source_type"] == "stat" and r["url"] == "https://kosis.kr/x"


# ── KOSIS ──

KOSIS_TABLES = [{"ORG_ID": "101", "ORG_NM": "국가데이터처", "TBL_ID": "DT_1B040A3",
                 "TBL_NM": "행정구역(시군구)별 성별 인구수", "STRT_PRD_DE": "2015", "END_PRD_DE": "2025"}]
KOSIS_ROWS = [
    {"C1_NM": "서울특별시", "ITM_NM": "총인구수", "DT": "9386034", "UNIT_NM": "명", "PRD_DE": "2025"},
    {"C1_NM": "전국", "ITM_NM": "총인구수<br>(A)", "DT": "51117378", "UNIT_NM": "명", "PRD_DE": "2025"},
]


@pytest.mark.asyncio
async def test_kosis_returns_latest_values_of_top_table(monkeypatch):
    calls = []
    monkeypatch.setattr(O, "_get_json", fake_get_json(
        {"statisticsSearch": KOSIS_TABLES, "statisticsParameterData": KOSIS_ROWS}, calls))
    out = await O.KosisStats("key")("인구수 통계", 4)

    assert len(out) == 1 and out[0]["no_fetch"] is True
    assert out[0]["url"] == "https://kosis.kr/statHtml/statHtml.do?orgId=101&tblId=DT_1B040A3"
    assert "전국 총인구수 (A): 51117378명" in out[0]["snippet"]     # 전국 행을 먼저 인용, br 태그는 정리
    assert "(2015~2025)" in out[0]["snippet"]                      # 통계표 제공 기간도 같이
    assert out[0]["snippet"].index("전국") < out[0]["snippet"].index("서울특별시")
    assert out[0]["date"] == "2025-01-01"
    assert calls[0]["params"]["searchNm"] == "인구수 통계"
    assert calls[1]["params"]["objL1"] == "ALL" and calls[1]["params"]["newEstPrdCnt"] == 1


@pytest.mark.asyncio
async def test_kosis_gives_nothing_when_numbers_are_unavailable(monkeypatch):
    """축이 여러 개인 표는 KOSIS 가 '40,000셀 초과'로 거절한다 — 그러면 근거로 못 쓰므로 아무것도 안 준다.
    ("이런 통계표가 있다"는 문장은 근거가 아니고 진짜 근거 자리만 차지한다.)"""
    monkeypatch.setattr(O, "_get_json", fake_get_json(
        {"statisticsSearch": KOSIS_TABLES,
         "statisticsParameterData": {"err": "31", "errMsg": " 40,000셀을 초과한 결과값은 요청하실 수 없습니다."}}))
    stats = O.KosisStats("key")
    assert await stats("인구수 통계", 4) == []
    assert stats.last_error.startswith("KOSIS 31")


@pytest.mark.asyncio
async def test_kosis_picks_the_table_whose_name_matches_the_query(monkeypatch):
    """KOSIS 검색은 표 안 항목까지 훑어서 첫 결과가 엉뚱할 때가 많다 (실호출에서 관광 통계표가 1위로 나왔다)."""
    tables = [{"ORG_ID": "113", "ORG_NM": "문화체육관광부", "TBL_ID": "T_TOUR",
               "TBL_NM": "관광객이용시설업 객실 판매 현황", "END_PRD_DE": "2024"},
              {"ORG_ID": "101", "ORG_NM": "국가데이터처", "TBL_ID": "DT_1B040A3",
               "TBL_NM": "행정구역(시군구)별 성별 인구수", "END_PRD_DE": "2025"}]
    calls = []
    monkeypatch.setattr(O, "_get_json", fake_get_json(
        {"statisticsSearch": tables, "statisticsParameterData": KOSIS_ROWS}, calls))
    out = await O.KosisStats("key")("인구수 통계", 4)
    assert calls[1]["params"]["tblId"] == "DT_1B040A3"          # 이름이 겹치는 표를 고른다
    assert "행정구역" in out[0]["title"]

    stats = O.KosisStats("key")
    assert await stats("고양이 통계", 4) == []                    # 어떤 표 이름과도 안 겹치면 버린다
    assert stats.last_error == "검색어와 이름이 겹치는 통계표 없음"


@pytest.mark.asyncio
async def test_kosis_returns_nothing_when_no_table_matches(monkeypatch):
    monkeypatch.setattr(O, "_get_json", fake_get_json({"statisticsSearch": []}))
    assert await O.KosisStats("key")("없는 통계", 4) == []


# ── ECOS ──

ECOS_PAYLOAD = {"KeyStatisticList": {"row": [
    {"CLASS_NAME": "환율", "KEYSTAT_NAME": "원/달러 환율(종가)", "DATA_VALUE": "1370.4",
     "CYCLE": "20260901", "UNIT_NAME": "원"},
    {"CLASS_NAME": "금리", "KEYSTAT_NAME": "한국은행 기준금리", "DATA_VALUE": "2.50",
     "CYCLE": "20260828", "UNIT_NAME": "연%"},
    {"CLASS_NAME": "가계", "KEYSTAT_NAME": "가구당월평균소득", "DATA_VALUE": "6969.4",
     "CYCLE": "2024Q4", "UNIT_NAME": "천원"},
]}}


@pytest.mark.asyncio
async def test_ecos_matches_indicators_and_caches_the_list(monkeypatch):
    calls = []
    monkeypatch.setattr(O, "_get_json", fake_get_json({"KeyStatisticList": ECOS_PAYLOAD}, calls))
    ecos = O.EcosKeyStats("key")

    out = await ecos("기준금리 인상 추이", 4)
    assert len(out) == 1 and "한국은행 기준금리 2.50연%" in out[0]["snippet"]
    assert "원/달러" not in out[0]["snippet"]              # 안 걸린 지표는 빼고

    await ecos("환율 전망")                                 # 두 번째 호출은 캐시 — 요청 1회뿐
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_ecos_returns_nothing_when_no_indicator_matches(monkeypatch):
    monkeypatch.setattr(O, "_get_json", fake_get_json({"KeyStatisticList": ECOS_PAYLOAD}))
    assert await O.EcosKeyStats("key")("반찬가게 폐기율", 4) == []


# ── K-Startup ──

KSTARTUP_PAYLOAD = {"data": [
    {"biz_pbanc_nm": "제11차 청년창업 제휴사업자 모집공고", "biz_prch_dprt_nm": "유통기획처",
     "aply_trgt_ctnt": "만 18세 이상 39세 이하 예비창업자", "pbanc_ctnt": "청년 창업자를 위한  지원 내용입니다. " * 3,
     "detl_pg_url": "https://www.k-startup.go.kr/web/contents/bizpbanc-ongoing.do?pbancSn=1",
     "pbanc_rcpt_bgng_dt": "20260901"},
]}


@pytest.mark.asyncio
async def test_kstartup_searches_by_keyword_and_keeps_body_as_evidence(monkeypatch):
    calls = []
    monkeypatch.setattr(O, "_get_json", fake_get_json({"kisedKstartupService": KSTARTUP_PAYLOAD}, calls))
    out = await O.KStartupSearch("key")("청년 창업지원 공고", 4)

    assert calls[0]["params"]["cond[biz_pbanc_nm::LIKE]"] == "창업지원"   # 가장 긴 낱말 (공고명 LIKE 검색)
    assert len(out) == 1 and out[0]["source_type"] == "policy" and out[0]["no_fetch"] is True
    assert out[0]["url"].startswith("https://www.k-startup.go.kr/")
    assert "지원대상: 만 18세 이상" in out[0]["snippet"] and out[0]["date"] == "2026-09-01"


def test_kstartup_keyword_picks_longest_meaningful_word():
    assert O.KStartupSearch.keyword("소상공인 지원사업 공고") == "소상공인"   # '공고' 같은 흔한 낱말은 제외어
    assert O.KStartupSearch.keyword("통계 현황") in ("통계", "현황")      # 전부 제외어면 그중 하나라도


# ── 상권정보 ──

UPJONG_PAYLOAD = {"body": {"items": [{"indsLclsCd": "I2", "indsLclsNm": "음식"},
                                     {"indsLclsCd": "P1", "indsLclsNm": "교육"},
                                     {"indsLclsCd": "S2", "indsLclsNm": "수리·개인"}]}}
STORE_PAYLOAD = {"header": {"stdrYm": "202606"}, "body": {"totalCount": 840255, "items": []}}


@pytest.mark.asyncio
async def test_sangkwon_counts_stores_for_matched_industry(monkeypatch):
    calls = []
    monkeypatch.setattr(O, "_get_json", fake_get_json(
        {"largeUpjongList": UPJONG_PAYLOAD, "storeListInUpjong": STORE_PAYLOAD}, calls))
    out = await O.SangKwonStats("key")("음식 업종 점포 수", 4)

    assert calls[1]["params"]["key"] == "I2"
    assert len(out) == 1 and "840,255개" in out[0]["snippet"] and "2026년 06월" in out[0]["snippet"]
    assert out[0]["date"] == "2026-06-01" and out[0]["no_fetch"] is True


@pytest.mark.asyncio
async def test_sangkwon_skips_when_no_industry_matches(monkeypatch):
    monkeypatch.setattr(O, "_get_json", fake_get_json({"largeUpjongList": UPJONG_PAYLOAD}))
    assert await O.SangKwonStats("key")("상권 분석 방법", 4) == []      # 업종명이 검색어에 없으면 2번째 요청도 안 한다


def test_sangkwon_match_prefers_longer_name_part():
    rows = UPJONG_PAYLOAD["body"]["items"]
    assert O.SangKwonStats.match(rows, "개인 수리 업소")["indsLclsCd"] == "S2"   # '수리'·'개인' 둘 다 걸림
    assert O.SangKwonStats.match(rows, "교육 스타트업")["indsLclsCd"] == "P1"
    assert O.SangKwonStats.match(rows, "화장품 판매") is None


# ── 묶음 ──

@pytest.mark.asyncio
async def test_sources_query_only_triggered_ones_and_survive_failures(monkeypatch):
    async def ok(query, n):
        return [O.stat_result("좋은 소스", "https://a.go.kr/1", "충분히 긴 사실 문장입니다. " * 3)]

    async def boom(query, n):
        raise RuntimeError("서버 오류")

    called = []

    async def counted(query, n):
        called.append(query)
        return []

    src = O.OpenDataSources([("kosis", ok), ("ecos", boom), ("kstartup", counted)])
    out = await src("1인 가구 현황 통계 금리", 4)          # kosis·ecos 만 걸린다 (창업 키워드 없음)

    assert [r["title"] for r in out] == ["좋은 소스"]
    assert src.last_used == ["kosis", "ecos"] and called == []
    assert "RuntimeError" in src.last_errors["ecos"]

    assert await src("고양이 사진") == [] and src.last_used == []      # 아무 소스도 안 부른다


def test_build_sources_needs_keys():
    cfg = O.OpenDataConfig()
    assert build_names(O.build_sources(cfg, env={})) == []
    assert build_names(O.build_sources(cfg, env={"ECOS_API_KEY": "e"})) == ["ecos"]
    full = {"KOSIS_API_KEY": "k", "ECOS_API_KEY": "e", "DATA_GO_KR_API_KEY": "d"}
    assert build_names(O.build_sources(cfg, env=full)) == ["kosis", "ecos", "kstartup", "sangkwon"]
    assert build_names(O.build_sources(O.OpenDataConfig(enabled=False), env=full)) == []
    assert build_names(O.build_sources(O.OpenDataConfig(sources=["ecos"]), env=full)) == ["ecos"]


def build_names(sources):
    return [n for n, _ in sources]


def test_config_reads_yaml_section():
    cfg = O.OpenDataConfig.from_config({"opendata": {"enabled": True, "sources": ["ecos", "없는소스"], "timeout": 30}})
    assert cfg.sources == ["ecos"] and cfg.timeout == 30
    assert O.OpenDataConfig.from_config({}).sources == ["kosis", "ecos", "kstartup", "sangkwon"]
