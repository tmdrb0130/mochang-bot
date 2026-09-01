"""작업 10 — vLLM(원격 GPU) 의 안전 동시성 상한 측정.

우리 큐(max_workers)를 거치지 않고 vLLM 에 **직접** 동시 호출을 넣어, 동시성을 단계적으로 올리며
문항 생성 정도 길이의 응답 시간과 서버 쪽 지표(/metrics)를 같이 기록한다.

  .venv/Scripts/python -m scripts.gpu_probe                          # 12 → 20 → 30 → 45
  .venv/Scripts/python -m scripts.gpu_probe --levels 8,16 --max-tokens 300
  .venv/Scripts/python -m scripts.gpu_probe --json out.json

판단 기준 (WORKBOARD 작업 10):
  kv_cache_usage_perc 가 90% 근처로 가거나, p95 가 꺾이거나, preemption 이 생기는 지점의
  **한 단계 아래**가 안전 상한. 그 값을 config.yaml max_workers 에 넣는다.

⚠️ 공유 GPU 에 실제 부하를 건다 (공개 서비스와 같은 서버). 사용자 승인 후 한가한 시간에만 실행.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import statistics
import time

import httpx

PROMPT = (
    "당신은 정부 창업지원사업 신청서를 대신 써 주는 조수입니다. 아래 아이디어로 '문제 인식' 문항 답변을 "
    "한국어 1200자 내외로 쓰세요. 근거 수치를 인용하는 문체로, 소제목 없이 이어지는 산문으로 쓰세요.\n\n"
    "[아이디어] 1인 가구를 위한 냉장고 속 식재료 유통기한 관리 앱. 사진 한 장으로 재고를 등록하고 "
    "임박한 식재료로 만들 수 있는 레시피를 추천한다."
)
GAUGES = ("vllm:num_requests_running", "vllm:num_requests_waiting", "vllm:kv_cache_usage_perc")
COUNTERS = ("vllm:num_preemptions_total", "vllm:generation_tokens_total")


def parse_metrics(text: str) -> dict:
    """프로메테우스 텍스트에서 관심 지표만. 라벨은 무시하고 같은 이름은 합산."""
    out: dict[str, float] = {}
    for line in text.splitlines():
        if line.startswith("#") or " " not in line:
            continue
        name = line.split("{")[0].split(" ")[0]
        if name in GAUGES or name in COUNTERS:
            m = re.search(r"\s([0-9.eE+-]+)$", line.strip())
            if m:
                try:
                    out[name] = out.get(name, 0.0) + float(m.group(1))
                except ValueError:
                    pass
    return out


async def sample_metrics(base_url: str, stop: asyncio.Event, samples: list[dict], interval: float = 1.0) -> None:
    url = base_url.rsplit("/v1", 1)[0].rstrip("/") + "/metrics"
    async with httpx.AsyncClient(timeout=5) as c:
        while not stop.is_set():
            try:
                samples.append(parse_metrics((await c.get(url)).text))
            except Exception:
                pass
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass


async def one_call(client: httpx.AsyncClient, model: str, max_tokens: int) -> tuple[float, int, str | None]:
    t0 = time.perf_counter()
    try:
        r = await client.post("/chat/completions", json={
            "model": model,
            "messages": [{"role": "user", "content": PROMPT}],
            "max_tokens": max_tokens,
            "temperature": 0.7,
        })
        r.raise_for_status()
        data = r.json()
        used = (data.get("usage") or {}).get("completion_tokens", 0)
        return time.perf_counter() - t0, int(used), None
    except Exception as e:
        return time.perf_counter() - t0, 0, f"{type(e).__name__}: {str(e)[:120]}"


async def run_level(base_url: str, api_key: str, model: str, n: int, max_tokens: int, timeout: float) -> dict:
    stop = asyncio.Event()
    samples: list[dict] = []
    sampler = asyncio.create_task(sample_metrics(base_url, stop, samples))
    t0 = time.perf_counter()
    async with httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout,
                                 headers={"Authorization": f"Bearer {api_key}"}) as c:
        rows = await asyncio.gather(*(one_call(c, model, max_tokens) for _ in range(n)))
    elapsed = time.perf_counter() - t0
    stop.set()
    await sampler

    lat = sorted(d for d, _, err in rows if err is None)
    toks = sum(t for _, t, err in rows if err is None)
    errs = [err for *_, err in rows if err]
    peak = {g: round(max((s.get(g, 0.0) for s in samples), default=0.0), 3) for g in GAUGES}
    preempt = 0.0
    if samples:
        preempt = samples[-1].get("vllm:num_preemptions_total", 0.0) - samples[0].get("vllm:num_preemptions_total", 0.0)
    return {
        "동시성": n,
        "성공": len(lat), "실패": len(errs),
        "p50": round(statistics.median(lat), 1) if lat else None,
        "p95": round(lat[max(0, int(len(lat) * 0.95) - 1)], 1) if lat else None,
        "최대": round(lat[-1], 1) if lat else None,
        "전체 소요": round(elapsed, 1),
        "생성 토큰/초": round(toks / elapsed, 1) if elapsed else 0,
        "KV캐시 최대": peak["vllm:kv_cache_usage_perc"],
        "서버 running 최대": peak["vllm:num_requests_running"],
        "서버 waiting 최대": peak["vllm:num_requests_waiting"],
        "preemption": round(preempt, 1),
        "오류 예시": errs[:2],
    }


async def run(levels: list[int], max_tokens: int, timeout: float, verbose: bool = True) -> dict:
    from backend.llm.client import load_config
    cfg = load_config()
    llm = cfg["llm"]
    base_url, api_key, model = llm["base_url"], llm.get("api_key", "dummy"), llm["model"]
    if verbose:
        print(f"대상: {base_url} / {model} / max_tokens={max_tokens}")
    rows = []
    for n in levels:
        row = await run_level(base_url, api_key, model, n, max_tokens, timeout)
        rows.append(row)
        if verbose:
            print(json.dumps(row, ensure_ascii=False))
        await asyncio.sleep(3)          # 다음 단계 전에 서버가 큐를 비우도록
    return {"levels": rows, "model": model, "max_tokens": max_tokens}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--levels", default="12,20,30,45")
    ap.add_argument("--max-tokens", type=int, default=600)
    ap.add_argument("--timeout", type=float, default=300)
    ap.add_argument("--json", help="결과 저장 경로")
    a = ap.parse_args()
    out = asyncio.run(run([int(x) for x in a.levels.split(",") if x.strip()], a.max_tokens, a.timeout))
    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        print("저장:", a.json)


if __name__ == "__main__":
    main()
