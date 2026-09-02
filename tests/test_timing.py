"""작업 19 — 계측 정리: 로그 격리 · 소스별 외부 호출 카운터 · 소스 서킷브레이커. 네트워크·모델 호출 없음."""
import json

import pytest

from backend import timing
from backend.rag import research as R
from backend.rag.breaker import Breaker


# ── ① 로그 격리 ──

def test_timing_log_is_not_the_repo_file_during_tests():
    """pytest 가 backend/.timing.jsonl 을 오염시키면 운영 지표를 못 읽는다 (conftest 가 임시 경로로 돌린다)."""
    assert "mochang-test-timing" in str(timing.PATH)
    assert "backend" not in str(timing.PATH).replace("\\", "/").split("/")[-2:][0]


def test_log_writes_one_json_line(tmp_path, monkeypatch):
    monkeypatch.setattr(timing, "PATH", tmp_path / "t.jsonl")
    timing.log("job", kind="generate", run_s=1.5)
    rows = [json.loads(l) for l in (tmp_path / "t.jsonl").read_text(encoding="utf-8").splitlines()]
    assert rows[0]["event"] == "job" and rows[0]["kind"] == "generate" and "ts" in rows[0]


# ── ② 소스별 카운터 ──

def test_counts_accumulate_and_flush_once(tmp_path, monkeypatch):
    monkeypatch.setattr(timing, "PATH", tmp_path / "t.jsonl")
    timing._counts.clear()
    for _ in range(3):
        timing.count("naver")
    timing.count("fetch", 2)
    timing.count("")                                    # 이름 없는 것은 세지 않는다

    got = timing.flush_counts(kind="research", question_id="q2")
    assert got == {"naver": 3, "fetch": 2}
    assert timing.flush_counts() == {}                  # 비운 뒤에는 남기지 않는다

    row = json.loads((tmp_path / "t.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert row["event"] == "sources" and row["naver"] == 3 and row["question_id"] == "q2"


@pytest.mark.asyncio
async def test_naver_and_ddgs_calls_are_counted(monkeypatch):
    """어느 소스로 몇 번 나갔는지 우리 로그로 알 수 있어야 한다 (지금까지는 각 콘솔에서만 봤다)."""
    timing._counts.clear()

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"items": []}

    class FakeClient:
        def __init__(self, **kw):
            self.headers = kw.get("headers", {})

        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url, params=None): return FakeResponse()

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    await R.NaverSearch(R.NaverConfig(client_id="i", client_secret="s", kinds=["news", "blog"]))("질의", 4)
    assert timing._counts["naver"] == 2                 # 유형마다 1건

    class FakeDDGS:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def text(self, q, region, max_results): return []

    import ddgs
    monkeypatch.setattr(ddgs, "DDGS", FakeDDGS)
    await R.DDGSearch()("질의", 3)
    assert timing._counts["ddgs"] == 1


# ── ③ 서킷브레이커 ──

def test_breaker_opens_after_consecutive_failures_and_recovers():
    clock = {"t": 1000.0}
    b = Breaker(fails=3, cooldown=60, now=lambda: clock["t"])

    b.record("ddgs", ok=False)
    b.record("ddgs", ok=False)
    assert b.is_open("ddgs") is False                   # 아직 2회
    b.record("ddgs", ok=False)
    assert b.is_open("ddgs") is True and "ddgs" in b.state()
    assert b.is_open("naver") is False                  # 다른 소스는 영향 없음

    clock["t"] += 59
    assert b.is_open("ddgs") is True
    clock["t"] += 2
    assert b.is_open("ddgs") is False                   # 쉬는 시간이 끝나면 다시 시도
    assert b.state() == {}


def test_breaker_success_resets_the_streak():
    b = Breaker(fails=2, cooldown=60)
    b.record("naver", ok=False)
    b.record("naver", ok=True)
    b.record("naver", ok=False)
    assert b.is_open("naver") is False                  # 연속이 아니면 열리지 않는다


@pytest.mark.asyncio
async def test_researcher_skips_a_broken_backend(tmp_path):
    """연속 실패한 백엔드는 쉬는 동안 호출하지 않고 바로 다음 소스로 간다."""
    calls = {"dead": 0, "good": 0}

    async def dead(q, n):
        calls["dead"] += 1
        raise RuntimeError("차단")

    async def good(q, n):
        calls["good"] += 1
        return [R.make_result("결과", "https://ok.com/1", "요약")]

    breaker = Breaker(fails=2, cooldown=600)
    r = R.Researcher(backends=[("ddgs", dead), ("naver", good)], cache=R.DiskCache(tmp_path), breaker=breaker)
    for i in range(4):
        assert await r.search(f"질의 {i}") != []
    assert calls["dead"] == 2 and calls["good"] == 4     # 2회 실패 뒤로는 아예 안 부른다
    assert "ddgs" in breaker.state()


@pytest.mark.asyncio
async def test_broken_open_data_source_is_skipped(tmp_path):
    from backend.rag.opendata import OpenDataSources

    calls = {"n": 0}

    async def dead(q, n):
        calls["n"] += 1
        raise RuntimeError("500")

    breaker = Breaker(fails=1, cooldown=600)
    src = OpenDataSources([("kosis", dead)], breaker=breaker)
    await src("1인 가구 통계")
    await src("사업체 수 통계")
    assert calls["n"] == 1 and src.last_used == []       # 두 번째는 트리거돼도 건너뛴다
