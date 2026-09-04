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

    # 동시 상한 429 (2026-09-04): scope 별로 나눠 봐야 어느 값을 올릴지 알 수 있다.
    #   owner = 초안당 max_jobs_per_client / ip = max_jobs_per_ip 천장 / kind = translate.global_limit
    lim = [r for r in rows if r.get("event") == "limit"]
    if lim:
        by = {}
        for r in lim:
            by.setdefault((r.get("scope", "?"), r.get("kind", "?")), []).append(r)
        print("
[동시 상한 429] %d건 — 정상 운영에서는 0 이어야 한다" % len(lim))
        for (scope, kind), rs in sorted(by.items()):
            worst = max(int(r.get("active") or 0) for r in rs)
            print("  %-6s %-18s %4d건  (상한 %s, 최대 동시 %d)" % (scope, kind, len(rs), rs[-1].get("limit"), worst))

    # 저장 실패(2026-09-04): storage.record 가 삼킨 오류. 0 이 아니면 DB 잠금·디스크·스키마 문제를 의심한다. /health.storage 와 같은 수.
    serr = [r for r in rows if r.get("event") == "storage_error"]
    if serr:
        print("
[저장 실패] %d건 — 마지막: %s %s" % (len(serr), serr[-1]["ts"], (serr[-1].get("error") or "")[:100]))
    # 벡터DB 임베딩 서버(Ollama) 다운(2026-09-04): 이 기간 색인·조회를 건너뛰고 웹 검색만 썼다.
    deg = [r for r in rows if r.get("event") == "vectorstore_degraded"]
    if deg:
        print("
[벡터DB 중단] Ollama 무응답 %d회 — 마지막 %s" % (len(deg), deg[-1]["ts"]))
    # 조사 경로 대기(2026-09-04): sources 이벤트의 *_ms 합계 ÷ *_n 으로 페이지·호출당 평균 대기·실행을 본다.
    if src:
        def avg(total, count):
            return (totals.get(total, 0) / totals[count]) if totals.get(count) else None
        parts = [("본문 추출 대기", avg("extract_wait_ms", "extract_n")), ("본문 추출 실행", avg("extract_run_ms", "extract_n")),
                 ("조사 모델 대기", avg("llm_wait_ms", "llm_n")), ("조사 모델 실행", avg("llm_run_ms", "llm_n"))]
        if any(v is not None for _, v in parts):
            print("
[조사 경로 평균] " + " · ".join("%s %.0fms" % (k, v) for k, v in parts if v is not None))
    # 마무리 작업자(backend/finisher.py, 2026-09-03): 생성 도중 나간 학생의 빈 문항을 서버가 채운 기록
    auto = [r for r in rows if r.get("event") == "auto_finish"]
    if auto:
        ok = [r for r in auto if r.get("status") == "done"]
        print("\n[마무리 작업자] 나간 학생의 빈 문항 %d건 시도 — 성공 %d, 실패 %d, 초안 %d개"
              % (len(auto), len(ok), len(auto) - len(ok), len({r.get("draft") for r in auto})))
        line = summarize("실행", [r["run_s"] for r in ok if r.get("run_s") is not None])
        if line:
            print(line)
        for r in [r for r in auto if r.get("status") != "done"][-3:]:
            print("  실패  %s  %s/%s  시도 %s" % (r["ts"], r.get("draft"), r.get("question"), r.get("attempt")))

    print()


if __name__ == "__main__":
    main()
