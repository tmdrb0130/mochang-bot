"""backend/.timing.jsonl 을 읽어 단계별 대기 시간을 요약한다.

    .venv/Scripts/python scripts/timing_report.py            # 전체
    .venv/Scripts/python scripts/timing_report.py --last 30  # 최근 30분
    .venv/Scripts/python scripts/timing_report.py --tail     # 최근 20건 그대로

모델을 호출하지 않는다. 읽기만 한다.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

PATH = Path(__file__).resolve().parent.parent / "backend" / ".timing.jsonl"

# 사용자 눈에 보이는 단계 이름
STEP = {
    "/intake": "① 인테이크 카드 생성",
    "/intake/regenerate": "① 카드 다시 뽑기",
    "/research": "② 웹 조사",
    "/generate": "③ 문항 생성",
    "/extend": "③ 이어쓰기",
    "/verify": "④ 검증",
}
JOB_KIND = {
    "intake": "① 인테이크 카드 생성",
    "intake_regenerate": "① 카드 다시 뽑기",
    "research": "② 웹 조사",
    "generate": "③ 문항 생성",
    "extend": "③ 이어쓰기",
    "verify": "④ 검증",
}


def fmt(seconds: float) -> str:
    return "%.1f초" % seconds if seconds < 60 else "%d분 %d초" % (seconds // 60, seconds % 60)


def summarize(label: str, values: list[float], unit_s: bool = True) -> str:
    if not values:
        return ""
    v = sorted(values)
    med = statistics.median(v)
    p90 = v[min(len(v) - 1, int(len(v) * 0.9))]
    f = fmt if unit_s else (lambda x: "%.0fms" % x)
    return "  %-22s %4d건   중앙 %-9s 최대 %-9s p90 %s" % (label, len(v), f(med), f(v[-1]), f(p90))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--last", type=int, default=0, help="최근 N분만")
    ap.add_argument("--tail", action="store_true", help="최근 20건 원본 출력")
    args = ap.parse_args()

    if not PATH.exists():
        print("기록이 없습니다: %s" % PATH)
        print("백엔드를 재시작한 뒤 사이트에서 한 번 생성해 보세요.")
        return

    rows = []
    for line in PATH.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except Exception:
            pass

    if args.last:
        cut = datetime.now() - timedelta(minutes=args.last)
        rows = [r for r in rows if datetime.fromisoformat(r["ts"]) >= cut]

    if args.tail:
        for r in rows[-20:]:
            print(json.dumps(r, ensure_ascii=False))
        return

    if not rows:
        print("해당 구간에 기록이 없습니다.")
        return

    print("=" * 74)
    print(" 기간: %s ~ %s   총 %d건" % (rows[0]["ts"], rows[-1]["ts"], len(rows)))
    print("=" * 74)

    jobs = [r for r in rows if r["event"] == "job"]
    if jobs:
        print("\n[작업 큐] 사용자가 실제로 기다린 시간")
        print("  ─ 대기: 앞 작업이 끝나기를 기다린 시간 / 실행: 모델이 글을 쓴 시간")
        kinds = {}
        for r in jobs:
            kinds.setdefault(r.get("kind", "?"), []).append(r)
        for kind, rs in sorted(kinds.items()):
            ok = [r for r in rs if r.get("status") == "done"]
            print("\n  %s   (%d건, 실패 %d건)" % (JOB_KIND.get(kind, kind), len(rs), len(rs) - len(ok)))
            for name, key in (("대기", "queued_s"), ("실행", "run_s"), ("합계", "total_s")):
                vals = [r[key] for r in ok if r.get(key) is not None]
                line = summarize(name, vals)
                if line:
                    print(line)

    https = [r for r in rows if r["event"] == "http" and r.get("ms") is not None]
    slow = [r for r in https if r["path"] in STEP]
    if slow:
        print("\n[동기 호출] 큐를 거치지 않는 직접 호출")
        paths = {}
        for r in slow:
            paths.setdefault(r["path"], []).append(r["ms"])
        for p, ms in sorted(paths.items()):
            print(summarize(STEP.get(p, p), [m / 1000 for m in ms]))

    errs = [r for r in rows if r.get("status") in (500, 502, 503) or (r["event"] == "job" and r.get("status") == "error")]
    if errs:
        print("\n[실패] %d건" % len(errs))
        for r in errs[-5:]:
            print("  %s  %s  %s" % (r["ts"], r.get("kind") or r.get("path"), (r.get("error") or r.get("status"))))

    # 조사 1회마다 남는 소스별 외부 요청 수(작업 19) + 벡터DB 적중(2026-09-03): vectorstore = 저장 문서 재사용 수,
    # vectorstore_only = 웹 검색 없이 벡터DB 만으로 끝낸 조사 수
    src = [r for r in rows if r.get("event") == "sources"]
    if src:
        totals: dict[str, int] = {}
        for r in src:
            for k, v in r.items():
                if k not in ("ts", "event", "kind", "question_id") and isinstance(v, (int, float)):
                    totals[k] = totals.get(k, 0) + int(v)
        only = totals.get("vectorstore_only", 0)
        print("\n[외부 소스] 조사 %d건 — 벡터DB 만으로 해결 %d건 (%.0f%%)" % (len(src), only, 100.0 * only / len(src)))
        for k, v in sorted(totals.items(), key=lambda kv: -kv[1]):
            print("  %-16s %6d" % (k, v))

    print()


if __name__ == "__main__":
    main()
