"""부하 테스트: 동시 요청 50개를 FastAPI 앱에 쏘아 큐가 OpenRouter 동시 호출을 max_workers 로 제한하는지 확인.

모델 호출은 가짜(monkeypatch) — OpenRouter 로 요청이 나가지 않는다. 네트워크 없이 ASGI 인메모리 전송으로 실행.

  python -m scripts.load_test                 # 기본: 50개 동시 /generate, 가짜 응답 0.2초
  python -m scripts.load_test --n 100 --delay 0.05 --jobs   # /jobs/generate 제출 + 폴링 경로
  python -m scripts.load_test --jobs --same-ip                # 전부 같은 IP → 동시 작업 제한(429) 확인
"""
from __future__ import annotations

import argparse
import asyncio
import time

import httpx


async def run(n: int = 50, delay: float = 0.2, use_jobs: bool = False, verbose: bool = True,
              same_ip: bool = False) -> dict:
    """same_ip=False 면 요청마다 다른 X-Forwarded-For 를 붙여 서로 다른 사용자 n 명을 흉내낸다.
    (백엔드가 IP 당 동시 작업 수를 제한하므로, 한 IP 로 몰면 부하가 아니라 429 를 재는 셈이 된다.)"""
    from backend import main as M
    from backend.llm.client import LLMResult

    state = {"running": 0, "peak": 0, "calls": 0}

    async def fake_complete(system, user, model):
        state["running"] += 1
        state["peak"] = max(state["peak"], state["running"])
        state["calls"] += 1
        await asyncio.sleep(delay)
        state["running"] -= 1
        return LLMResult(text="가짜 응답 본문입니다. " * 10, model=model)

    M.client._complete = fake_complete                      # 진짜 OpenRouter 호출 차단
    body = {"question_id": "q1", "style": "plain", "track": "tech", "idea": "부하 테스트용 아이디어 한 줄입니다"}

    def head(i: int) -> dict:
        return {"X-Forwarded-For": "10.0.0.1" if same_ip else f"10.{i // 65536 % 256}.{i // 256 % 256}.{i % 256}"}

    async with M.lifespan(M.app):                           # 워커 기동
        transport = httpx.ASGITransport(app=M.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=120) as c:
            t0 = time.time()
            if not use_jobs:
                rs = await asyncio.gather(*(c.post("/generate", json=body, headers=head(i)) for i in range(n)))
                ok = sum(1 for r in rs if r.status_code == 200)
            else:
                subs = await asyncio.gather(*(c.post("/jobs/generate", json=body, headers=head(i)) for i in range(n)))
                rejected = [r for r in subs if r.status_code == 429]
                accepted = [r for r in subs if r.status_code == 200]
                ids = [r.json()["job_id"] for r in accepted]
                positions = [r.json()["position"] for r in accepted]
                ok = 0
                pending = set(ids)
                while pending:
                    await asyncio.sleep(0.05)
                    for jid in list(pending):
                        s = (await c.get(f"/jobs/{jid}")).json()
                        if s["status"] in ("done", "error"):
                            pending.discard(jid)
                            ok += s["status"] == "done"
                if verbose:
                    q = [p for p in positions if p is not None]
                    print(f"제출 {len(accepted)}건 수락 / {len(rejected)}건 429(동시 작업 제한) / "
                          f"대기 순번 최대: {max(q) if q else 0}")
            elapsed = time.time() - t0
            stats = (await c.get("/jobs")).json()

    result = {"n": n, "ok": ok, "peak_concurrent_llm": state["peak"], "max_workers": M.client.queue.max_workers,
              "max_per_client": M.client.queue.max_per_client, "same_ip": same_ip,
              "elapsed": round(elapsed, 2), "queue": stats, "limited": state["peak"] <= M.client.queue.max_workers}
    if verbose:
        print(f"요청 {n}개 / 성공 {ok} / 걸린 시간 {elapsed:.2f}s")
        print(f"LLM 동시 실행 최대 {state['peak']} (max_workers={M.client.queue.max_workers}) → "
              f"{'제한 OK' if result['limited'] else '제한 실패!'}")
        print(f"이론상 최소 시간 ≈ {n / M.client.queue.max_workers * delay:.1f}s (n/workers×delay)")
        print("큐 상태:", stats)
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=50)
    ap.add_argument("--delay", type=float, default=0.2)
    ap.add_argument("--jobs", action="store_true", help="/jobs 제출+폴링 경로로 테스트")
    ap.add_argument("--same-ip", action="store_true", help="전부 같은 IP 로 보내 동시 작업 제한(429)을 확인")
    a = ap.parse_args()
    r = asyncio.run(run(a.n, a.delay, a.jobs, same_ip=a.same_ip))
    if a.same_ip:                       # 같은 IP 면 제한 때문에 일부만 수락되는 게 정상
        raise SystemExit(0 if r["limited"] else 1)
    raise SystemExit(0 if r["limited"] and r["ok"] == r["n"] else 1)


if __name__ == "__main__":
    main()
