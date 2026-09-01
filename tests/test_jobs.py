"""동시성 층 — 큐가 실제로 동시 실행 수를 제한하는지, 429 백오프 재시도, 순번, 오류 전파. 모델 호출 없음."""
import asyncio

import pytest

from backend.llm.jobs import JobQueue, QueueError, TooManyJobs


class Probe:
    """동시 실행 수를 기록하는 가짜 작업."""

    def __init__(self, delay=0.02):
        self.delay = delay
        self.running = 0
        self.peak = 0
        self.done = 0

    def make(self, value):
        async def job():
            self.running += 1
            self.peak = max(self.peak, self.running)
            await asyncio.sleep(self.delay)
            self.running -= 1
            self.done += 1
            return value
        return job


async def no_sleep(_):
    return None


@pytest.mark.asyncio
async def test_queue_limits_concurrency_to_max_workers():
    q = JobQueue(max_workers=4)
    await q.start()
    probe = Probe()
    results = await asyncio.gather(*(q.run(probe.make(i)) for i in range(50)))   # 동시 50개 제출
    await q.stop()
    assert sorted(results) == list(range(50)) and probe.done == 50
    assert probe.peak == 4 and q.peak_running == 4                                 # 절대 4 초과 안 함
    assert q.stats()["done"] == 50 and q.stats()["error"] == 0


@pytest.mark.asyncio
async def test_submit_reports_queue_position_and_snapshot():
    q = JobQueue(max_workers=1)
    await q.start()
    gate = asyncio.Event()

    async def blocker():
        await gate.wait()
        return "first"

    j1 = q.submit(blocker, kind="generate")
    await asyncio.sleep(0)                       # 워커가 j1 을 잡을 시간
    j2 = q.submit(Probe().make("second"), kind="generate")
    j3 = q.submit(Probe().make("third"), kind="research")
    assert q.get(j1.id)["status"] == "running" and q.get(j1.id)["position"] is None
    assert q.get(j2.id)["position"] == 0 and q.get(j3.id)["position"] == 1
    assert q.stats()["queued"] == 2 and q.stats()["running"] == 1
    gate.set()
    await asyncio.gather(j1.future, j2.future, j3.future)
    snap = q.get(j3.id)
    assert snap["status"] == "done" and snap["result"] == "third" and snap["kind"] == "research"
    assert q.get("nope") is None
    await q.stop()


class Flaky(Exception):
    pass


@pytest.mark.asyncio
async def test_retries_with_backoff_on_configured_errors():
    slept = []

    async def fake_sleep(d):
        slept.append(d)

    q = JobQueue(max_workers=2, retry_attempts=3, base_delay=2.0, retry_on=(Flaky,), sleep=fake_sleep)
    await q.start()
    calls = {"n": 0}

    async def job():
        calls["n"] += 1
        if calls["n"] < 3:
            raise Flaky("429")
        return "ok"

    assert await q.run(job) == "ok"
    assert calls["n"] == 3 and q.total_retries == 2
    assert len(slept) == 2 and 1.6 <= slept[0] <= 2.4 and 3.2 <= slept[1] <= 4.8   # 2s, 4s (+지터)
    await q.stop()


@pytest.mark.asyncio
async def test_gives_up_after_max_attempts_and_propagates_error():
    q = JobQueue(max_workers=1, retry_attempts=2, retry_on=(Flaky,), sleep=no_sleep)
    await q.start()

    async def always_fail():
        raise Flaky("still 429")

    with pytest.raises(Flaky):
        await q.run(always_fail)
    job = next(iter(q._jobs.values()))
    assert job.status == "error" and "Flaky" in job.error and job.attempts == 2
    assert q.stats()["error"] == 1
    await q.stop()


@pytest.mark.asyncio
async def test_non_retryable_error_fails_immediately():
    q = JobQueue(max_workers=1, retry_attempts=3, retry_on=(Flaky,), sleep=no_sleep)
    await q.start()

    async def boom():
        raise ValueError("bad input")

    with pytest.raises(ValueError):
        await q.run(boom)
    assert q.total_retries == 0
    await q.stop()


@pytest.mark.asyncio
async def test_submit_before_start_raises():
    q = JobQueue()
    with pytest.raises(QueueError):
        q.submit(Probe().make(1))


@pytest.mark.asyncio
async def test_llm_client_routes_complete_through_queue(monkeypatch):
    """LLMClient.complete 가 큐를 거쳐 max_workers 로 제한되는지 — 실제 모델 호출은 가짜로 대체."""
    from backend.llm import client as C
    cfg = {"llm": {"base_url": "http://fake/v1", "api_key": "x", "model": "m"}, "max_workers": 3, "retry": {"attempts": 1}}
    llm = C.LLMClient(cfg)
    probe = Probe(delay=0.02)

    async def fake_complete(system, user, model):
        return await probe.make(C.LLMResult(text=f"{user}", model=model))()

    monkeypatch.setattr(llm, "_complete", fake_complete)
    results = await asyncio.gather(*(llm.complete("s", f"u{i}") for i in range(20)))
    assert len(results) == 20 and probe.peak == 3 and llm.queue.max_workers == 3
    assert all(r.model == "m" for r in results)
    await llm.queue.stop()


@pytest.mark.asyncio
async def test_llm_client_retries_rate_limited_via_queue(monkeypatch):
    from backend.llm import client as C
    cfg = {"llm": {"base_url": "http://fake/v1", "api_key": "x", "model": "m"}, "max_workers": 1,
           "retry": {"attempts": 3, "base_delay_seconds": 0}}
    llm = C.LLMClient(cfg)
    llm.queue._sleep = no_sleep
    n = {"calls": 0}

    async def flaky(system, user, model):
        n["calls"] += 1
        if n["calls"] < 3:
            raise C.RateLimited("429 무료 풀 혼잡")
        return C.LLMResult(text="ok", model=model)

    monkeypatch.setattr(llm, "_complete", flaky)
    assert (await llm.complete("s", "u")).text == "ok"
    assert n["calls"] == 3 and llm.queue.total_retries == 2
    await llm.queue.stop()


@pytest.mark.asyncio
async def test_nested_run_inside_worker_does_not_deadlock():
    """/jobs/generate 같은 외부 작업이 워커 안에서 다시 queue.run(LLM 호출) 을 부르는 상황 — 교착 없이 끝나야 한다."""
    q = JobQueue(max_workers=1)
    await q.start()

    async def inner():
        await asyncio.sleep(0.01)
        return "llm"

    async def outer():
        a = await q.run(inner)          # 워커 안 → 중첩 제출 대신 즉시 실행
        b = await q.run(inner)
        return a + b

    results = await asyncio.wait_for(asyncio.gather(*(q.run(outer) for _ in range(5))), timeout=5)
    assert results == ["llmllm"] * 5 and q.peak_running == 1
    await q.stop()


# ────────────── V4: 클라이언트(IP)당 동시 작업 제한 ──────────────

@pytest.mark.asyncio
async def test_per_client_limit_rejects_and_recovers():
    """같은 owner 는 상한까지만 동시 제출 가능, 다른 owner 는 영향 없음, 하나 끝나면 다시 열린다."""
    q = JobQueue(max_workers=2, max_per_client=2)
    await q.start()
    gate = asyncio.Event()

    async def blocker():
        await gate.wait()
        return "ok"

    a1 = q.submit(blocker, kind="generate", owner="1.1.1.1")
    a2 = q.submit(blocker, kind="generate", owner="1.1.1.1")
    with pytest.raises(TooManyJobs) as e:
        q.submit(blocker, kind="generate", owner="1.1.1.1")
    assert e.value.limit == 2 and e.value.active == 2 and "2건" in str(e.value)

    b1 = q.submit(Probe().make("b"), kind="generate", owner="2.2.2.2")   # 다른 IP 는 통과
    assert q.active_for("1.1.1.1") == 2 and q.active_for("2.2.2.2") == 1

    gate.set()
    await asyncio.gather(a1.future, a2.future, b1.future)
    assert q.active_for("1.1.1.1") == 0                                   # 끝난 작업은 안 센다
    a3 = q.submit(Probe().make("a3"), kind="generate", owner="1.1.1.1")   # 다시 열림
    assert await a3.future == "a3"
    assert q.stats()["max_per_client"] == 2
    await q.stop()


@pytest.mark.asyncio
async def test_per_client_limit_off_by_default():
    """owner 를 안 주거나 상한이 0 이면 제한 없음 — 내부 queue.run() 경로가 막히면 안 된다."""
    q = JobQueue(max_workers=2)                       # max_per_client 기본 0
    await q.start()
    assert q.stats()["max_per_client"] == 0
    probe = Probe()
    for _ in range(10):
        q.submit(probe.make(1), owner="9.9.9.9")      # 상한 0 → 무제한
    await q.stop()

    q2 = JobQueue(max_workers=2, max_per_client=1)
    await q2.start()
    for _ in range(10):
        q2.submit(Probe().make(1))                    # owner 없음 → 제한 대상 아님
    await q2.stop()


@pytest.mark.asyncio
async def test_submit_endpoint_returns_429_over_limit(monkeypatch):
    """POST /jobs/{kind} 가 같은 IP 의 초과 제출을 429 로 막고, 다른 IP 는 정상 수락한다."""
    import httpx

    from backend import main as M
    from backend.llm.client import LLMResult

    gate = asyncio.Event()

    async def fake_complete(system, user, model):
        await gate.wait()
        return LLMResult(text="가짜 응답", model=model)

    monkeypatch.setattr(M.client, "_complete", fake_complete)
    monkeypatch.setattr(M.client.queue, "max_per_client", 2)
    body = {"question_id": "q1", "style": "plain", "track": "tech", "idea": "동시 작업 제한 테스트 아이디어"}
    mine, other = {"X-Forwarded-For": "203.0.113.7"}, {"X-Forwarded-For": "203.0.113.8"}

    async with M.lifespan(M.app):
        transport = httpx.ASGITransport(app=M.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=30) as c:
            ok = [await c.post("/jobs/generate", json=body, headers=mine) for _ in range(2)]
            assert [r.status_code for r in ok] == [200, 200]
            assert all("job_id" in r.json() and "position" in r.json() for r in ok)   # 기존 필드 유지

            over = await c.post("/jobs/generate", json=body, headers=mine)
            assert over.status_code == 429 and "2건" in over.json()["detail"]
            assert over.headers.get("retry-after") == "10"

            assert (await c.post("/jobs/generate", json=body, headers=other)).status_code == 200

            stats = (await c.get("/jobs", headers=mine)).json()
            assert stats["your_active"] == 2 and stats["max_per_client"] == 2 and "max_workers" in stats

            gate.set()
            for r in ok:
                for _ in range(100):
                    snap = (await c.get(f"/jobs/{r.json()['job_id']}")).json()
                    if snap["status"] in ("done", "error"):
                        break
                    await asyncio.sleep(0.02)
                assert snap["status"] == "done"

            assert (await c.post("/jobs/generate", json=body, headers=mine)).status_code == 200
