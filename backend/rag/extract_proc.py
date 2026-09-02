"""trafilatura 본문 추출을 **별도 프로세스**에서 돌린다 — lxml 네이티브 크래시 격리.

2026-09-02 20:34, 사용자의 조사 처리 중 python.exe 가 `etree.cp311-win_amd64.pyd`(lxml) 액세스 위반(0xc0000005)으로 죽어
서비스 전체가 NSSM 재시작됐다 (재시작 5초 동안 /research 502 → "조사 실패 — 참고자료 없이 작성"). 파이썬 예외가 아니라
C 레벨 크래시라 try/except 로는 못 잡는다. 추출은 스레드 여러 개(EXTRACT_CONCURRENCY × 조사 워커)에서 동시에 돌고 있었다.

처방: 추출을 작은 프로세스 풀에서 돌린다. 워커 프로세스가 죽으면 그 페이지만 빈 문자열이 되고 풀을 새로 만든다 —
API 프로세스와 큐·진행 중 작업은 살아남는다.

  환경변수 MOCHANG_EXTRACT_ISOLATION=0 이면 예전처럼 스레드에서 돌린다 (테스트가 이렇게 둔다 — 프로세스 생성이 느리다).
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import multiprocessing
import os
import threading

log = logging.getLogger("mochang.extract")

ENABLED = os.environ.get("MOCHANG_EXTRACT_ISOLATION", "1") != "0"
POOL_SIZE = 3            # EXTRACT_CONCURRENCY 와 같게. 추출 1건은 수십~수백 ms 라 이 정도면 조사 워커 6개를 감당한다
TIMEOUT_S = 30.0         # 한 페이지 추출 상한 (풀 워커가 멈춘 경우 대비)

_pool: concurrent.futures.ProcessPoolExecutor | None = None
_lock = threading.Lock()


def _warm() -> None:
    """워커 프로세스 초기화 — trafilatura 를 미리 import 해 첫 요청이 느리지 않게."""
    import trafilatura  # noqa: F401


def _extract(html: str, kw: dict) -> str:
    """워커 프로세스에서 실행. 모듈 최상위 함수여야 spawn 이 pickle 할 수 있다."""
    import trafilatura
    return trafilatura.extract(html, **kw) or ""


def _get_pool() -> concurrent.futures.ProcessPoolExecutor:
    global _pool
    with _lock:
        if _pool is None:
            _pool = concurrent.futures.ProcessPoolExecutor(
                max_workers=POOL_SIZE, mp_context=multiprocessing.get_context("spawn"), initializer=_warm)
        return _pool


def _reset_pool() -> None:
    global _pool
    with _lock:
        old, _pool = _pool, None
    if old is not None:
        try:
            old.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass


def shutdown() -> None:
    _reset_pool()


async def extract(html: str, **kw) -> str:
    """trafilatura.extract 와 같은 인자. 실패·크래시·타임아웃이면 빈 문자열 — 조사는 그 페이지 없이 계속된다."""
    if not ENABLED:
        return await asyncio.to_thread(_extract, html, kw)
    loop = asyncio.get_running_loop()
    try:
        fut = loop.run_in_executor(_get_pool(), _extract, html, kw)
        return await asyncio.wait_for(fut, TIMEOUT_S)
    except concurrent.futures.process.BrokenProcessPool as e:
        # 워커가 죽었다 (lxml 크래시 등). 이 페이지는 버리고 풀을 새로 만든다 — 다음 페이지부터 정상.
        log.warning("본문 추출 프로세스가 죽어 풀을 재생성합니다: %s", str(e)[:120])
        _reset_pool()
        return ""
    except asyncio.TimeoutError:
        log.warning("본문 추출 %ss 초과 — 페이지를 건너뜁니다", TIMEOUT_S)
        return ""
    except Exception as e:
        log.warning("본문 추출 실패: %s", str(e)[:120])
        return ""
