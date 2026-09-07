"""배포해도 안전한 순간을 기다린다 — 두 큐가 모두 빌 때까지 지켜보고 알려준다 (2026-09-07).

    .venv/Scripts/python scripts/deploy_window.py            # 창이 열리면 알려주고 종료
    .venv/Scripts/python scripts/deploy_window.py --notify   # 화면 알림도 (msg.exe)

왜 필요한가: 진행 중인 작업은 **메모리에만** 있다(backend/llm/jobs.py JobQueue._jobs).
`nssm restart mochang-api` 를 하면 job_id 가 사라지고, 프론트의 followJob 은 404 를 받는 즉시
JobExpiredError 를 던진다(재시도 없음, frontend/src/api.js). 그러면 학생 화면이 실패로 바뀐다.

  생성 중이었으면  → 문항별 오류. 그 문항만 다시 누르면 된다 (가벼움)
  번역 중이었으면  → "다시 시도" 버튼 (가벼움)
  인테이크 중이었으면 → 처음부터 다시. 실행이 44~470초로 가장 길어 맞을 확률도 제일 높다 (무거움)

두 큐가 모두 running 0 / queued 0 인 순간에 재시작하면 **잃는 작업이 0** 이다.
/health 에는 생성 큐만 나오므로 조사 큐(인테이크·조사·번역)까지 보려면 /jobs 를 봐야 한다.

**이 스크립트는 서비스를 건드리지 않는다.** GET 만 하고, 재시작 명령은 화면에 찍어 줄 뿐이다 —
서비스 제어는 사용자의 관리자 창에서 직접 한다.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.request
from datetime import datetime

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

RESTART_STEPS = """
관리자 PowerShell 창에서:

    nssm restart mochang-api
    Start-Sleep -Seconds 5
    Invoke-RestMethod http://127.0.0.1:8000/health | Select-Object ok, llm_reachable

프론트도 함께 배포한다면(무중단, 단일 번들이라 작업 중인 학생에게 영향 없음):

    Copy-Item frontend\\dist frontend\\dist.bak -Recurse -Force     # 롤백용
    cd frontend
    npx vite build
"""


def _jobs(base: str, timeout: float) -> dict | None:
    try:
        with urllib.request.urlopen(base.rstrip("/") + "/jobs", timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


def _busy(j: dict) -> tuple[int, int]:
    """(생성 큐 점유, 조사 큐 점유) = running + queued."""
    main = int(j.get("running") or 0) + int(j.get("queued") or 0)
    r = j.get("research") or {}
    res = int(r.get("running") or 0) + int(r.get("queued") or 0)
    return main, res


def _notify(text: str) -> None:
    try:
        subprocess.run(["msg", "*", "/TIME:120", text[:255]], timeout=10,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def main() -> int:
    ap = argparse.ArgumentParser(description="배포 안전 창 대기 (읽기 전용)")
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--interval", type=float, default=2.0, help="확인 간격(초, 기본 2)")
    ap.add_argument("--settle", type=int, default=3, help="연속 몇 번 비어야 창으로 볼지 (기본 3)")
    ap.add_argument("--max-min", type=int, default=30, help="이 시간 안에 창이 없으면 포기(분, 기본 30)")
    ap.add_argument("--timeout", type=float, default=10.0)
    ap.add_argument("--notify", action="store_true", help="창이 열리면 화면 알림")
    args = ap.parse_args()

    print(f"대기 시작 — {args.base}, {args.interval}초마다 확인. 두 큐가 연속 {args.settle}회 비면 알린다.")
    deadline = time.time() + args.max_min * 60
    idle = 0
    last = None
    while time.time() < deadline:
        j = _jobs(args.base, args.timeout)
        if j is None:
            print(f"[{datetime.now():%H:%M:%S}] /jobs 응답 없음 — 서버 확인 필요")
            idle = 0
            time.sleep(args.interval)
            continue
        main_busy, res_busy = _busy(j)
        cur = (main_busy, res_busy)
        idle = idle + 1 if cur == (0, 0) else 0
        if cur != last or idle == args.settle:
            print(f"[{datetime.now():%H:%M:%S}] 생성 {main_busy} / 조사 {res_busy}"
                  + (f"  … 비어 있음 {idle}/{args.settle}" if cur == (0, 0) else "  (작업 중 — 기다린다)"))
            last = cur
        if idle >= args.settle:
            print("\n>>> 지금 배포해도 안전합니다 (두 큐 모두 비어 있음, 잃을 작업 0)")
            print(RESTART_STEPS)
            if args.notify:
                _notify("모창봇: 배포 안전 창이 열렸습니다 (큐 비어 있음)")
            return 0
        time.sleep(args.interval)

    print(f"\n{args.max_min}분 안에 안전한 창이 없었습니다 — 계속 사용 중입니다. 나중에 다시 실행하세요.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
