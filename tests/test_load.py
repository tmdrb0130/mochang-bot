"""부하 테스트를 pytest 로도 — 동시 50 요청, 가짜 모델, 큐 제한 확인 (동기 경로 + /jobs 경로)."""
import pytest

from scripts.load_test import run


@pytest.mark.asyncio
async def test_sync_endpoint_is_limited_by_queue():
    r = await run(n=50, delay=0.02, use_jobs=False, verbose=False)
    assert r["ok"] == 50 and r["limited"] and r["peak_concurrent_llm"] == r["max_workers"]


@pytest.mark.asyncio
async def test_jobs_submit_and_poll_path():
    r = await run(n=30, delay=0.02, use_jobs=True, verbose=False)
    assert r["ok"] == 30 and r["limited"] and r["queue"]["done"] >= 30
