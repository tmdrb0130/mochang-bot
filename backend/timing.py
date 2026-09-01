"""요청·작업 소요 시간 기록 (JSONL).

사용자가 "신청서 만들어줘"를 누르고 결과를 볼 때까지 각 단계에서 얼마나 기다리는지 추적하기 위한 것.
모델을 호출하지 않으며, 기록에 실패해도 서비스 동작에 영향을 주지 않는다(조용히 무시).

  backend/.timing.jsonl   기본 경로. 환경변수 MOCHANG_TIMING_LOG 로 바꿀 수 있다.

읽는 법:  .venv/Scripts/python -m scripts.timing_report
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path

_DEFAULT = Path(__file__).resolve().parent / ".timing.jsonl"
PATH = Path(os.environ.get("MOCHANG_TIMING_LOG") or _DEFAULT)

# 파일이 무한정 커지지 않게. 넘으면 .1 로 밀고 새로 시작한다.
MAX_BYTES = 20 * 1024 * 1024


def log(event: str, **fields) -> None:
    """한 줄 기록. event 는 'http' 또는 'job'."""
    try:
        rec = {"ts": datetime.now().isoformat(timespec="seconds"), "event": event}
        rec.update(fields)
        line = json.dumps(rec, ensure_ascii=False)
        try:
            if PATH.exists() and PATH.stat().st_size > MAX_BYTES:
                PATH.replace(PATH.with_suffix(PATH.suffix + ".1"))
        except OSError:
            pass
        with PATH.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass          # 계측 때문에 서비스가 멈추면 안 된다


def job_done(job) -> None:
    """JobQueue.on_done 에 물리는 콜백. 대기 시간과 실행 시간을 나눠 기록한다."""
    started, created, finished = job.started, job.created, job.finished
    log("job",
        kind=job.kind,
        status=job.status,
        attempts=job.attempts,
        queued_s=round((started - created), 2) if started else None,
        run_s=round((finished - started), 2) if (started and finished) else None,
        total_s=round((finished - created), 2) if finished else None,
        error=(job.error or "")[:120] or None)


class Timer:
    """with Timer('label') as t: ... → t.ms"""

    def __init__(self, label: str = ""):
        self.label = label
        self.ms = 0.0

    def __enter__(self):
        self._t0 = time.perf_counter()
        return self

    def __exit__(self, *exc):
        self.ms = round((time.perf_counter() - self._t0) * 1000, 1)
        return False
