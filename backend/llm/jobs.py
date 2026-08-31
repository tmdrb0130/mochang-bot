"""동시성 층: asyncio.Queue + 워커 N개.

모든 모델 호출(LLMClient.complete)은 이 큐를 거친다 → OpenRouter 로 나가는 동시 요청 수 = max_workers.
429(RateLimited)/일시 오류(TransientError) 는 워커가 지수 백오프로 재시도한다.

두 가지 사용법:
  await queue.run(lambda: client._complete(...))       # 결과를 바로 기다림 (LLMClient 내부)
  job_id = queue.submit(lambda: heavy_coroutine(...))  # 작업 ID 발급 → GET /jobs/{id} 로 폴링
"""
from __future__ import annotations

import asyncio
import contextvars
import random
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

Factory = Callable[[], Awaitable[Any]]

# 워커 태스크 안에서 True. 워커가 실행 중인 작업이 다시 queue.run() 을 부르면(예: /jobs/generate 안의 client.complete)
# 같은 큐에 중첩 제출되어 워커가 모두 자기 자식을 기다리는 교착이 생긴다 → 그 경우 큐를 거치지 않고 바로 실행한다.
_in_worker: contextvars.ContextVar[bool] = contextvars.ContextVar("llm_in_worker", default=False)


class QueueError(RuntimeError):
    pass


@dataclass
class Job:
    id: str
    factory: Factory
    kind: str = "llm"
    status: str = "queued"          # queued | running | done | error
    result: Any = None
    error: str | None = None
    attempts: int = 0
    created: float = field(default_factory=time.time)
    started: float | None = None
    finished: float | None = None
    future: asyncio.Future | None = None

    def snapshot(self, position: int | None) -> dict:
        return {
            "job_id": self.id,
            "kind": self.kind,
            "status": self.status,
            "position": position,            # 대기 순번 (0 = 다음 차례). running/done 이면 None
            "attempts": self.attempts,
            "queued_seconds": round((self.started or time.time()) - self.created, 1),
            "elapsed_seconds": round(((self.finished or time.time()) - self.started), 1) if self.started else None,
            "result": self.result if self.status == "done" else None,
            "error": self.error,
        }


class JobQueue:
    def __init__(self, max_workers: int = 4, retry_attempts: int = 3, base_delay: float = 2.0, max_delay: float = 30.0,
                 retry_on: tuple[type[BaseException], ...] = (), history_limit: int = 500, sleep=asyncio.sleep):
        self.max_workers = max(1, int(max_workers))
        self.retry_attempts = max(1, int(retry_attempts))
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.retry_on = retry_on
        self.history_limit = history_limit
        self._sleep = sleep
        self._queue: asyncio.Queue[Job] = asyncio.Queue()
        self._jobs: OrderedDict[str, Job] = OrderedDict()
        self._workers: list[asyncio.Task] = []
        self._running = 0
        self.peak_running = 0
        self.total_done = 0
        self.total_error = 0
        self.total_retries = 0

    # ── 수명 ──
    async def start(self) -> None:
        if self._workers:
            return
        # asyncio.Queue 는 처음 쓰인 이벤트 루프에 묶인다. stop() 뒤 다른 루프에서 다시 start() 하면(테스트, 재기동)
        # 워커가 "bound to a different event loop" 로 조용히 죽어 작업이 영원히 대기하므로, 큐를 현재 루프에 새로 만든다.
        pending: list[Job] = []
        while not self._queue.empty():
            pending.append(self._queue.get_nowait())
        self._queue = asyncio.Queue()
        for job in pending:
            self._queue.put_nowait(job)
        self._workers = [asyncio.create_task(self._worker(i), name=f"llm-worker-{i}") for i in range(self.max_workers)]

    async def stop(self) -> None:
        for t in self._workers:
            t.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers = []

    @property
    def started(self) -> bool:
        return bool(self._workers)

    # ── 제출 ──
    def submit(self, factory: Factory, kind: str = "llm") -> Job:
        if not self._workers:
            raise QueueError("JobQueue 가 시작되지 않았습니다 (app startup 에서 await queue.start()).")
        job = Job(id=uuid.uuid4().hex[:12], factory=factory, kind=kind, future=asyncio.get_running_loop().create_future())
        self._jobs[job.id] = job
        self._trim_history()
        self._queue.put_nowait(job)
        return job

    async def run(self, factory: Factory, kind: str = "llm") -> Any:
        """제출하고 결과를 기다린다. 실패하면 마지막 예외를 그대로 올린다.
        이미 워커 안(외부 작업이 점유한 슬롯)에서 불리면 중첩 제출 대신 바로 실행 — 동시 실행 수는 여전히 워커 수로 제한된다."""
        if _in_worker.get():
            return await self._run_with_retry(factory)
        job = self.submit(factory, kind)
        return await job.future

    async def _run_with_retry(self, factory: Factory) -> Any:
        attempts = 0
        while True:
            attempts += 1
            try:
                return await factory()
            except self.retry_on:
                if attempts >= self.retry_attempts:
                    raise
                self.total_retries += 1
                await self._sleep(self._delay(attempts))

    # ── 조회 ──
    def get(self, job_id: str) -> dict | None:
        job = self._jobs.get(job_id)
        if not job:
            return None
        return job.snapshot(self.position(job_id))

    def position(self, job_id: str) -> int | None:
        """큐에서 앞에 있는 queued 작업 수. running/완료면 None."""
        job = self._jobs.get(job_id)
        if not job or job.status != "queued":
            return None
        ahead = 0
        for j in self._jobs.values():
            if j.id == job_id:
                break
            if j.status == "queued":
                ahead += 1
        return ahead

    def stats(self) -> dict:
        statuses = [j.status for j in self._jobs.values()]
        return {
            "max_workers": self.max_workers,
            "running": self._running,
            "queued": statuses.count("queued"),
            "done": self.total_done,
            "error": self.total_error,
            "retries": self.total_retries,
            "peak_running": self.peak_running,
        }

    # ── 내부 ──
    def _trim_history(self) -> None:
        while len(self._jobs) > self.history_limit:
            oldest_id, oldest = next(iter(self._jobs.items()))
            if oldest.status in ("done", "error"):
                self._jobs.pop(oldest_id)
            else:
                break

    def _delay(self, attempt: int) -> float:
        # 2, 4, 8 … + 지터. 상한 max_delay
        return min(self.max_delay, self.base_delay * (2 ** (attempt - 1))) * (0.8 + 0.4 * random.random())

    async def _worker(self, index: int) -> None:
        _in_worker.set(True)
        while True:
            job = await self._queue.get()
            self._running += 1
            self.peak_running = max(self.peak_running, self._running)
            job.status, job.started = "running", time.time()
            try:
                while True:
                    job.attempts += 1
                    try:
                        job.result = await job.factory()
                        job.status = "done"
                        self.total_done += 1
                        if job.future and not job.future.done():
                            job.future.set_result(job.result)
                        break
                    except self.retry_on as e:
                        if job.attempts >= self.retry_attempts:
                            raise
                        self.total_retries += 1
                        job.error = f"{type(e).__name__}: {str(e)[:160]} (재시도 {job.attempts}/{self.retry_attempts})"
                        await self._sleep(self._delay(job.attempts))
            except asyncio.CancelledError:
                job.status, job.error = "error", "cancelled"
                if job.future and not job.future.done():
                    job.future.cancel()
                raise
            except Exception as e:  # 재시도 소진 또는 재시도 대상이 아닌 오류
                job.status, job.error = "error", f"{type(e).__name__}: {str(e)[:300]}"
                self.total_error += 1
                if job.future and not job.future.done():
                    job.future.set_exception(e)
            finally:
                job.finished = time.time()
                self._running -= 1
                self._queue.task_done()
