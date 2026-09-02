"""실서비스 부하 테스트 — 사용자 N명이 동시에 신청서를 만드는 상황을 흉내내고 사용자 체감 대기·서버 지표를 기록한다.

    .venv/Scripts/python scripts/live_load_test.py --users 50                    # 기본 50명, 문항 8개씩
    .venv/Scripts/python scripts/live_load_test.py --users 50 --json out.json    # 원본 저장

흉내내는 것 (프론트 흐름과 같게):
  - 사용자마다 다른 X-Forwarded-For → 백엔드의 IP 당 동시 작업 제한(max_jobs_per_client, 기본 3)이 사용자별로 걸린다.
  - 사용자 1명 = 문항 8개를 최대 3개씩 동시에 /jobs/generate 로 제출 → 폴링 (프론트와 같은 경로).
  - 참고자료는 시작 전에 문항별 /jobs/research 1회로 받아 모두에게 같은 것을 준다 (실사용과 같은 프롬프트 길이).
  - 5초마다 /api/health 큐 상태 + vLLM /metrics(running·waiting·KV 사용률·preemption)을 샘플링한다.
결과: 문항 단위 대기(queued)·실행(run)·총(total) p50/p95, 사용자 단위(8문항 완료) p50/p95, 실패 수, 서버 지표 최고치.

⚠️ 공유 GPU 서버에 실부하를 건다. 사용자 승인 후 한가한 시간에.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import statistics
import sys
import time

import httpx

sys.stdout.reconfigure(encoding="utf-8")
B = "https://www.bustartup.kr:50001/api"
VLLM_METRICS = "http://localhost:30801/metrics"       # 이 PC 의 SSH 터널 (Qwen). 라마면 30800
QS = ["q1", "q2", "q3_1", "q3_2", "q4_1", "q4_2", "q8", "q10"]
IDEA = {"track": "tech", "is_business": False, "current_item": "", "team": "팀원 없음", "capability": "웹 개발 3년, 리액트·파이썬",
        "idea": "자취하는 20대는 배달 음식이 지겨운데 요리는 부담스럽다. 동네 반찬가게와 연결해 그날 남은 반찬을 저녁 7시 이후 할인 꾸러미로 예약·수령하는 앱을 만들고 싶다.",
        "answers": [{"slot": "revenue", "label": "수익 방식", "answer": "꾸러미 판매액의 15% 수수료", "unknown": False},
                    {"slot": "alternative", "label": "지금의 대안", "answer": "배달앱 또는 편의점 도시락", "unknown": False},
                    {"slot": "first_step", "label": "첫 6개월 행동", "answer": "우리 동네 반찬가게 3곳과 수기 예약으로 2주 시범 운영", "unknown": False}]}


def pct(xs, p):
    if not xs:
        return None
    xs = sorted(xs)
    return round(xs[min(len(xs) - 1, int(len(xs) * p))], 1)


async def poll(c: httpx.AsyncClient, jid: str) -> dict:
    while True:
        await asyncio.sleep(2.5)
        s = (await c.get(f"{B}/jobs/{jid}")).json()
        if s["status"] in ("done", "error"):
            return s


async def research_all(c: httpx.AsyncClient) -> dict:
    refs = {}
    for q in QS:
        r = await c.post(f"{B}/jobs/research", json={**IDEA, "question_id": q})
        s = await poll(c, r.json()["job_id"])
        refs[q] = ((s.get("result") or {}).get("facts") or []) if s["status"] == "done" else []
    return refs


async def one_question(c: httpx.AsyncClient, user: int, q: str, refs: list, sem: asyncio.Semaphore, rows: list, t_start: float):
    async with sem:                                     # 사용자당 동시 3문항 (프론트 동작)
        headers = {"X-Forwarded-For": f"10.99.{user // 250}.{user % 250 + 1}"}
        body = {**IDEA, "question_id": q, "style": "logic", "references": refs}
        t0 = time.time()
        for attempt in range(30):                        # 429(동시 작업 초과)면 프론트처럼 잠시 뒤 재시도
            r = await c.post(f"{B}/jobs/generate", json=body, headers=headers)
            if r.status_code != 429:
                break
            await asyncio.sleep(3)
        if r.status_code != 200:
            rows.append({"user": user, "q": q, "status": f"http {r.status_code}", "total": round(time.time() - t0, 1)})
            return
        j = r.json()
        s = await poll(c, j["job_id"])
        rows.append({"user": user, "q": q, "status": s["status"], "position": j.get("position"),
                     "queued": s.get("queued_seconds"), "run": s.get("elapsed_seconds"), "total": round(time.time() - t0, 1),
                     "t_submit": round(t0 - t_start, 1), "chars": len(((s.get("result") or {}).get("text") or "")),
                     "error": (s.get("error") or "")[:100] or None})


async def one_user(c, user, refs, rows, t_start):
    sem = asyncio.Semaphore(3)
    t0 = time.time()
    await asyncio.gather(*(one_question(c, user, q, refs[q], sem, rows, t_start) for q in QS))
    return round(time.time() - t0, 1)


async def sampler(stop: asyncio.Event, samples: list):
    async with httpx.AsyncClient(timeout=5) as c:
        while not stop.is_set():
            s = {"t": round(time.time(), 1)}
            try:
                h = (await c.get(f"{B}/health")).json()["queue"]
                s.update({"api_running": h.get("running"), "api_queued": h.get("queued"), "api_error": h.get("error")})
            except Exception:
                pass
            try:
                m = (await c.get(VLLM_METRICS)).text
                for key, pat in (("vllm_running", r"num_requests_running\{[^}]*\} ([\d.]+)"), ("vllm_waiting", r"num_requests_waiting\{[^}]*\} ([\d.]+)"),
                                 ("kv_perc", r"kv_cache_usage_perc\{[^}]*\} ([\d.]+)"), ("preempt", r"num_preemptions_total\{[^}]*\} ([\d.]+)")):
                    mm = re.search(pat, m)
                    if mm:
                        s[key] = float(mm.group(1))
            except Exception:
                pass
            samples.append(s)
            await asyncio.sleep(5)


async def main(users: int, out: str | None):
    async with httpx.AsyncClient(timeout=60) as c:
        h = (await c.get(f"{B}/health")).json()
        print(f"대상 {B} | 모델 {h['model']} | 워커 {h['queue']['max_workers']} | IP당 동시 {h['queue']['max_per_client']}")
        print("참고자료 준비(문항 8개 조사, 캐시되면 빠름)…")
        refs = await research_all(c)
        print("  facts:", {q: len(v) for q, v in refs.items()})
        rows, samples = [], []
        stop = asyncio.Event()
        smp = asyncio.create_task(sampler(stop, samples))
        t_start = time.time()
        print(f"{users}명 동시 시작 — 각자 문항 8개(동시 3)…")
        user_totals = await asyncio.gather(*(one_user(c, u, refs, rows, t_start) for u in range(users)))
        stop.set()
        await smp
    wall = round(time.time() - t_start)
    ok = [r for r in rows if r["status"] == "done"]
    bad = [r for r in rows if r["status"] != "done"]
    long = [r for r in ok if r["q"] not in ("q1", "q10")]
    base = samples[0].get("preempt", 0) if samples else 0
    print(f"\n═══ 결과: {users}명 × 8문항 = {len(rows)}건, 벽시계 {wall}s")
    print(f"성공 {len(ok)} · 실패 {len(bad)} {[(r['q'], r['status'], r.get('error')) for r in bad[:5]]}")
    print(f"문항 대기(queued)  p50 {pct([r['queued'] for r in ok if r['queued'] is not None], .5)}s  p95 {pct([r['queued'] for r in ok if r['queued'] is not None], .95)}s")
    print(f"문항 실행(run)     p50 {pct([r['run'] for r in ok if r['run'] is not None], .5)}s  p95 {pct([r['run'] for r in ok if r['run'] is not None], .95)}s  (긴 문항만 p50 {pct([r['run'] for r in long if r['run'] is not None], .5)}s)")
    print(f"문항 총 체감(total) p50 {pct([r['total'] for r in ok], .5)}s  p95 {pct([r['total'] for r in ok], .95)}s  최대 {max(r['total'] for r in ok) if ok else '-'}s")
    print(f"사용자 8문항 완료   p50 {pct(list(user_totals), .5)}s  p95 {pct(list(user_totals), .95)}s  최대 {max(user_totals)}s")
    print(f"서버 최고치: API running {max((s.get('api_running') or 0) for s in samples)} · API queued {max((s.get('api_queued') or 0) for s in samples)}"
          f" · vLLM running {max((s.get('vllm_running') or 0) for s in samples)} · vLLM waiting {max((s.get('vllm_waiting') or 0) for s in samples)}"
          f" · KV 최고 {max((s.get('kv_perc') or 0) for s in samples) * 100:.0f}% · preemption +{(max((s.get('preempt') or 0) for s in samples) - base):.0f}")
    print(f"긴 문항 평균 글자 {statistics.mean([r['chars'] for r in long]) if long else 0:.0f}")
    if out:
        json.dump({"users": users, "wall": wall, "rows": rows, "samples": samples, "user_totals": user_totals}, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print("저장:", out)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--users", type=int, default=50)
    ap.add_argument("--json")
    a = ap.parse_args()
    asyncio.run(main(a.users, a.json))
