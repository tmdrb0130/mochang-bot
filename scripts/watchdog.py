"""모창봇 서버 감시 — /health·/jobs·timing 로그를 보고 이상하면 alerts.log 에 남긴다 (2026-09-07).

    .venv/Scripts/python scripts/watchdog.py            # 한 번 검사하고 끝 (작업 스케줄러용)
    .venv/Scripts/python scripts/watchdog.py --watch    # 5분마다 계속 (창을 띄워 두고 볼 때)
    .venv/Scripts/python scripts/watchdog.py --notify   # 이상하면 화면 알림도 (msg.exe, 실패해도 무시)

**서버를 건드리지 않는다** — GET 두 번(/health, /jobs)과 로그 파일 읽기가 전부다. 모델 호출 0, 재시작 0.
종료 코드: 0 정상 · 1 경고 · 2 심각 → 작업 스케줄러에서 조건부 동작에 쓸 수 있다.

무엇을 보나
  심각  API 무응답 · llm_reachable=false · 저장소 꺼짐
        llm_reachable 이 false 면 SSH 터널(30801)이나 vLLM 이 죽은 것이고, 그동안 **생성·번역이 전부 실패**한다.
        터널은 서비스가 아니라 콘솔 프로세스라 RDP 로그오프 한 번에 죽는다 (2026-09-07 확인).
  경고  저장 실패 · 큐 적체 · 최근 429 급증 · 작업 오류 · translate_empty(번역이 빈 채로 done 된 건)

상태가 바뀔 때만 남긴다(정상→이상, 이상→정상). 이상이 계속되면 --repeat 분마다 다시 남긴다 —
5분마다 같은 줄이 쌓여 로그를 못 읽게 되는 것을 막는다.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

# 콘솔(cp949)에서 "—" 같은 글자로 죽지 않게 — foreign_e2e.py·drafts_delete.py 와 같은 처방.
# 주의: from __future__ 는 파일의 첫 문장이어야 하므로 이 블록은 반드시 임포트 뒤에 온다.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "backend" / ".data"
ALERTS = DATA / "alerts.log"
STATE = DATA / "watchdog-state.json"
TIMING = ROOT / "backend" / ".timing.jsonl"
TIMING_TAIL_BYTES = 2 * 1024 * 1024      # 로그가 7MB 를 넘으므로 꼬리만 읽는다

CRIT, WARN, OK = "심각", "경고", "정상"


def _get(url: str, timeout: float) -> tuple[int, dict | None, str]:
    """(상태코드, 본문, 오류). 예외를 밖으로 내보내지 않는다 — 감시가 죽으면 안 된다."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8")), ""
    except urllib.error.HTTPError as e:
        return e.code, None, f"HTTP {e.code}"
    except Exception as e:
        return 0, None, f"{type(e).__name__}: {str(e)[:120]}"


def _recent(minutes: int) -> list[dict]:
    """timing.jsonl 의 최근 N 분 줄. 파일이 없거나 깨져도 빈 목록."""
    try:
        size = TIMING.stat().st_size
        with TIMING.open("rb") as f:
            if size > TIMING_TAIL_BYTES:
                f.seek(size - TIMING_TAIL_BYTES)
                f.readline()                       # 잘린 첫 줄 버리기
            raw = f.read().decode("utf-8", errors="replace")
    except Exception:
        return []
    cut = (datetime.now() - timedelta(minutes=minutes)).isoformat(timespec="seconds")
    out = []
    for line in raw.splitlines():
        if not line.startswith("{"):
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        if (d.get("ts") or "") >= cut:
            out.append(d)
    return out


def _worse(level: str, candidate: str) -> str:
    """지금 등급과 새 등급 중 더 나쁜 쪽 (심각 > 경고 > 정상)."""
    order = {OK: 0, WARN: 1, CRIT: 2}
    return candidate if order[candidate] > order[level] else level


def check(base: str, args) -> dict:
    """한 번 검사한 결과 → {level, problems: [...], info: {...}}"""
    problems: list[str] = []
    level = OK
    info: dict = {}

    code, health, err = _get(base.rstrip("/") + "/health", args.timeout)
    if code != 200 or not health:
        return {"level": CRIT, "problems": [f"API 무응답 ({err or code})"], "info": {}}

    info["model"] = health.get("model")
    info["오늘_모델호출"] = (health.get("usage") or {}).get("used")

    if health.get("llm_reachable") is False:
        problems.append("모델 서버 연결 끊김 — SSH 터널(30801) 또는 vLLM 확인. 생성·번역이 전부 실패 중")
        level = _worse(level, CRIT)

    st = health.get("storage") or {}
    if not st.get("enabled"):
        problems.append("저장소가 꺼져 있음 — 초안이 저장되지 않는다")
        level = _worse(level, CRIT)
    errors = int(st.get("error_count") or 0)
    info["저장실패"] = errors
    if errors:
        problems.append(f"저장 실패 {errors}건 — 마지막: {str(st.get('last_error'))[:100]}")
        level = _worse(level, WARN)

    # 두 큐 — /health 는 생성 큐만 준다. 조사 큐(인테이크·조사·번역)는 /jobs 에만 있다.
    code2, jobs, _ = _get(base.rstrip("/") + "/jobs", args.timeout)
    queues: dict = {}
    if code2 == 200 and jobs:
        queues["생성"] = {"running": jobs.get("running"), "queued": jobs.get("queued")}
        r = jobs.get("research")
        if r:
            queues["조사"] = {"running": r.get("running"), "queued": r.get("queued")}
    info["큐"] = queues
    for name, q in queues.items():
        if (q.get("queued") or 0) >= args.queue_warn:
            problems.append(f"{name} 큐 적체 — 대기 {q['queued']}건 (임계 {args.queue_warn})")
            level = _worse(level, WARN)

    rows = _recent(args.window)
    n429 = sum(1 for d in rows if d.get("event") == "limit")
    nerr = sum(1 for d in rows if d.get("event") == "job" and d.get("status") not in (None, "done"))
    empty = [d for d in rows if d.get("event") == "translate_empty"]
    info[f"최근{args.window}분"] = {
        "요청": sum(1 for d in rows if d.get("event") == "http"),
        "429": n429,
        "작업오류": nerr,
        "빈번역": sum(int(d.get("empty") or 0) for d in empty),
    }
    if n429 >= args.limit_warn:
        scopes: dict = {}
        for d in rows:
            if d.get("event") == "limit":
                key = f"{d.get('kind')}/{d.get('scope')}"
                scopes[key] = scopes.get(key, 0) + 1
        problems.append(f"최근 {args.window}분 429 {n429}건 (임계 {args.limit_warn}) — 걸린 상한: {scopes}")
        level = _worse(level, WARN)
    if nerr >= args.error_warn:
        problems.append(f"최근 {args.window}분 작업 오류 {nerr}건 (임계 {args.error_warn})")
        level = _worse(level, WARN)
    if empty:
        tot = sum(int(d.get("empty") or 0) for d in empty)
        problems.append(f"번역이 빈 채로 완료된 항목 {tot}개 / {len(empty)}건 — 학생 화면엔 한국어가 그대로 보인다")
        level = _worse(level, WARN)

    return {"level": level, "problems": problems, "info": info}


def _state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(level: str, problems: list[str]) -> None:
    try:
        DATA.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps({"level": level, "problems": problems, "at": time.time()},
                                    ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def _alert(line: str) -> None:
    try:
        DATA.mkdir(parents=True, exist_ok=True)
        with ALERTS.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _notify(text: str) -> None:
    """콘솔 세션에 메시지 창. 없는 환경이면 조용히 넘어간다."""
    try:
        subprocess.run(["msg", "*", "/TIME:60", text[:255]], timeout=10,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def run_once(base: str, args) -> int:
    res = check(base, args)
    level, problems = res["level"], res["problems"]
    now = datetime.now().isoformat(timespec="seconds")
    body = "; ".join(problems) if problems else "이상 없음"
    summary = f"[{now}] {level} {body} | {json.dumps(res['info'], ensure_ascii=False)}"
    print(summary)

    prev = _state()
    changed = prev.get("level") != level or prev.get("problems") != problems
    stale = (time.time() - float(prev.get("at") or 0)) > args.repeat * 60
    if level != OK and (changed or stale):
        _alert(summary)
        if args.notify:
            _notify(f"모창봇 {level}: {body[:200]}")
    elif level == OK and prev.get("level") not in (None, OK):
        _alert(f"[{now}] 복구됨 — 이전: {prev.get('level')} {prev.get('problems')}")
    _save(level, problems)
    return {OK: 0, WARN: 1, CRIT: 2}[level]


def main() -> int:
    ap = argparse.ArgumentParser(description="모창봇 서버 감시 (읽기 전용 — 서버를 건드리지 않는다)")
    ap.add_argument("--base", default="http://127.0.0.1:8000", help="백엔드 주소 (기본 로컬)")
    ap.add_argument("--watch", action="store_true", help="한 번이 아니라 계속 감시")
    ap.add_argument("--interval", type=int, default=300, help="--watch 간격(초, 기본 300)")
    ap.add_argument("--window", type=int, default=15, help="최근 몇 분의 로그를 볼지 (기본 15)")
    ap.add_argument("--queue-warn", type=int, default=20, help="큐 대기 경고 임계 (기본 20)")
    ap.add_argument("--limit-warn", type=int, default=30, help="429 경고 임계 (기본 30)")
    ap.add_argument("--error-warn", type=int, default=3, help="작업 오류 경고 임계 (기본 3)")
    ap.add_argument("--repeat", type=int, default=30, help="같은 이상을 다시 남기기까지의 분 (기본 30)")
    ap.add_argument("--timeout", type=float, default=20.0, help="HTTP 타임아웃(초)")
    ap.add_argument("--notify", action="store_true", help="이상하면 msg.exe 로 화면 알림")
    args = ap.parse_args()

    if not args.watch:
        return run_once(args.base, args)
    print(f"감시 시작 — {args.base}, {args.interval}초 간격. 기록: {ALERTS}")
    rc = 0
    try:
        while True:
            rc = run_once(args.base, args)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("중지")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
