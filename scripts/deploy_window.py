"""배포해도 안전한 순간을 기다린다 — 두 큐가 모두 빌 때까지 지켜보고 알려준다 (2026-09-07).

    .venv/Scripts/python scripts/deploy_window.py            # 창이 열리면 알려주고 종료
    .venv/Scripts/python scripts/deploy_window.py --notify   # 화면 알림도 (msg.exe)

왜 필요한가: 진행 중인 작업은 **메모리에만** 있다(backend/llm/jobs.py JobQueue._jobs).
`nssm restart mochang-api` 를 하면 job_id 가 사라지고, 프론트의 followJob 은 404 를 받는 즉시
JobExpiredError 를 던진다(재시도 없음, frontend/src/api.js). 그러면 학생 화면이 실패로 바뀐다.

  생성 중이었으면  → 문항별 오류. 그 문항만 다시 누르면 된다 (가벼움)
  번역 중이었으면  → "다시 시도" 버튼 (가벼움)
  인테이크 중이었으면 → 처음부터 다시. 실행이 44~470초로 가장 길어 맞을 확률도 제일 높다 (무거움)

/health 에는 생성 큐만 나오므로 조사 큐(인테이크·조사·번역)까지 보려면 /jobs 를 봐야 한다.

**큐가 빈 것만으로는 부족하다** (2026-09-07 확인). 두 가지가 더 있다.
  ① 번역 결과는 **어디에도 저장되지 않는다**(backend/storage.py `_record_sync`: generate·extend → generations,
     research·idea_research → research, intake → drafts. translate 는 없다). 작업이 끝났어도 학생이 아직
     2.5초 폴링으로 받아가기 전이면 재시작으로 **영영 사라진다**. 다시 눌러야 한다.
     (생성·조사·인테이크는 DB 에 남으므로 결과 자체는 살아남는다 — 화면만 실패로 뜨고 /drafts/{id} 로 복원된다.)
  ② 큐가 비어도 학생이 "생각 중" 일 수 있다. 그 학생이 다음 버튼을 누르는 순간 502 를 맞는데,
     POST 는 재시도가 없어 그대로 실패한다(폴링만 3회까지 견딘다).
그래서 큐가 빈 것에 더해 **최근 --quiet-sec 초 동안 아무 요청도 없었을 것**을 함께 본다.

**이 스크립트는 서비스를 건드리지 않는다.** GET 만 하고, 재시작 명령은 화면에 찍어 줄 뿐이다 —
서비스 제어는 사용자의 관리자 창에서 직접 한다.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

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


TIMING = Path(__file__).resolve().parent.parent / "backend" / ".timing.jsonl"


def _quiet_seconds() -> float | None:
    """마지막 **학생 활동** 이후 흐른 초. 로그를 못 읽으면 None (그때는 이 조건을 건너뛴다).

    GET 은 세지 않는다 (2026-09-07): 이 스크립트의 /jobs 폴링과 watchdog.py 의 /health·/jobs 가
    모두 미들웨어에 기록되므로(backend/main.py `_record_timing`), GET 까지 세면 **감시 도구 자신 때문에
    영원히 조용해지지 않는다.** 학생 활동은 POST(작업 제출)와 job 이벤트(작업 완료)로 판단한다.
    빠른 GET /jobs/{id} 폴링은 애초에 기록되지 않는다(500ms 미만은 건너뛴다)."""
    try:
        size = TIMING.stat().st_size
        with TIMING.open("rb") as f:
            if size > 512 * 1024:
                f.seek(size - 512 * 1024)
                f.readline()
            raw = f.read().decode("utf-8", errors="replace")
    except Exception:
        return None
    last = ""
    for line in raw.splitlines():
        if not line.startswith("{"):
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        ev = d.get("event")
        if ev == "http" and d.get("method") == "GET":
            continue                              # 감시 도구·프론트의 읽기 요청은 활동이 아니다
        if ev not in ("http", "job"):
            continue
        ts = d.get("ts") or ""
        if ts > last:
            last = ts
    if not last:
        return None
    try:
        return (datetime.now() - datetime.fromisoformat(last)).total_seconds()
    except Exception:
        return None


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


def _notify(title: str, text: str) -> None:
    """화면에 알림 창 (2026-09-07). 자동으로 닫힌다.

    msg.exe 는 이 PC 에서 "Access is denied" 로 **조용히 실패한다** — 실측으로 확인했다.
    그래서 권한이 필요 없는 WScript.Shell Popup 을 먼저 쓰고, 안 되면 msg.exe 를 시도한다.
    본문은 환경변수로 넘긴다 — 한국어·따옴표가 PowerShell 명령줄에서 깨지지 않게.
    작업 스케줄러로 돌릴 때는 "사용자가 로그온했을 때만 실행" 이어야 창이 보인다(SYSTEM 은 못 본다)."""
    env = {**os.environ, "MOCHANG_ALERT_TEXT": text[:1000], "MOCHANG_ALERT_TITLE": title[:80]}
    ps = ("$w = New-Object -ComObject Wscript.Shell; "
          "$null = $w.Popup($env:MOCHANG_ALERT_TEXT, 60, $env:MOCHANG_ALERT_TITLE, 48)")
    try:
        subprocess.run(["powershell", "-NoProfile", "-Command", ps], timeout=90, env=env,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return
    except Exception:
        pass
    try:
        subprocess.run(["msg", "*", "/TIME:60", text[:255]], timeout=10,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def main() -> int:
    ap = argparse.ArgumentParser(description="배포 안전 창 대기 (읽기 전용)")
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--interval", type=float, default=2.0, help="확인 간격(초, 기본 2)")
    ap.add_argument("--settle", type=int, default=3, help="연속 몇 번 비어야 창으로 볼지 (기본 3)")
    ap.add_argument("--quiet-sec", type=int, default=90,
                    help="마지막 요청 이후 이만큼 조용해야 안전으로 본다(초, 기본 90). "
                         "번역 결과는 DB 에 안 남고, 학생이 생각 중이면 큐가 비어도 곧 요청이 온다")
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
        quiet = _quiet_seconds()
        quiet_ok = quiet is None or quiet >= args.quiet_sec
        cur = (main_busy, res_busy)
        idle = idle + 1 if cur == (0, 0) else 0
        if cur != last or idle == args.settle:
            qtxt = "로그 못 읽음" if quiet is None else f"마지막 요청 {quiet:.0f}초 전"
            print(f"[{datetime.now():%H:%M:%S}] 생성 {main_busy} / 조사 {res_busy} · {qtxt}"
                  + (f"  … 비어 있음 {idle}/{args.settle}" if cur == (0, 0) else "  (작업 중 — 기다린다)")
                  + ("" if quiet_ok else f"  (아직 조용하지 않다 — {args.quiet_sec}초 필요)"))
            last = cur
        if idle >= args.settle and quiet_ok:
            qsay = "로그 확인 불가" if quiet is None else f"마지막 요청 {quiet:.0f}초 전"
            print()
            print(f">>> 지금 배포해도 안전합니다 (두 큐 비어 있음 · {qsay})")
            print(RESTART_STEPS)
            if args.notify:
                _notify("모창봇 배포", "안전 창이 열렸습니다 — 두 큐가 비어 있고 학생 활동도 조용합니다.")
            return 0
        time.sleep(args.interval)

    print(f"\n{args.max_min}분 안에 안전한 창이 없었습니다 — 계속 사용 중입니다. 나중에 다시 실행하세요.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
