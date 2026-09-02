"""본문 추출 프로세스 격리 (backend/rag/extract_proc.py) — 모델·네트워크 호출 0.

핵심: 워커 프로세스가 C 레벨로 죽어도(os._exit 로 흉내) API 쪽은 예외 없이 빈 문자열을 받고, 다음 호출은 정상 동작한다."""
import os

import pytest

from backend.rag import extract_proc as X

_orig = X._extract
HTML = "<html><body><article><h1>제목</h1><p>본문 첫 문단입니다. 충분히 긴 문장을 넣어 둡니다.</p>"\
       "<p>둘째 문단입니다. 트라필라투라가 본문으로 인식할 만큼 내용이 있어야 합니다.</p></article></body></html>"


def _crash(_html, _kw):
    os._exit(1)          # 파이썬 예외가 아닌 프로세스 사망 — lxml 액세스 위반과 같은 부류


@pytest.mark.asyncio
async def test_thread_fallback_when_disabled(monkeypatch):
    monkeypatch.setattr(X, "ENABLED", False)
    text = await X.extract(HTML, include_comments=False)
    assert "본문 첫 문단" in text


@pytest.mark.asyncio
async def test_worker_crash_is_isolated_and_pool_recovers(monkeypatch):
    """워커가 죽으면 그 호출만 '' 이고, 풀이 재생성돼 다음 호출은 정상."""
    monkeypatch.setattr(X, "ENABLED", True)
    monkeypatch.setattr(X, "POOL_SIZE", 1)
    X.shutdown()
    try:
        assert "본문 첫 문단" in await X.extract(HTML)                    # 프로세스 풀 경로 정상 동작
        pool_before = X._pool
        monkeypatch.setattr(X, "_extract", _crash)                          # 워커에 보내는 함수를 죽는 함수로
        assert await X.extract(HTML) == ""                                  # 예외 없이 빈 문자열
        monkeypatch.setattr(X, "_extract", _orig)
        assert X._pool is None or X._pool is not pool_before                # 풀이 버려졌다
        assert "본문 첫 문단" in await X.extract(HTML)                    # 새 풀로 복구
    finally:
        X.shutdown()

