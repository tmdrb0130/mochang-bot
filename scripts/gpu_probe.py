"""작업 10 — vLLM(원격 GPU) 의 안전 동시성 상한 측정.

우리 큐(max_workers)를 거치지 않고 vLLM 에 **직접** 동시 호출을 넣어, 동시성을 단계적으로 올리며
문항 생성 정도 길이의 응답 시간과 서버 쪽 지표(/metrics)를 같이 기록한다.

  .venv/Scripts/python -m scripts.gpu_probe                          # 12 → 20 → 30 → 45
  .venv/Scripts/python -m scripts.gpu_probe --levels 60,80,100,150   # 상한 탐색
  .venv/Scripts/python -m scripts.gpu_probe --prompt short --max-tokens 300   # 가벼운 프롬프트로 비교
  .venv/Scripts/python -m scripts.gpu_probe --json out.json

프롬프트는 기본이 **실사용과 같은 것**이다 (system.md + 트랙 + 문항 md + 참고자료가 붙은 컨텍스트,
max_tokens 도 config.yaml 값). 짧은 프롬프트로 재면 prefill 비용과 KV 점유를 과소평가한다.

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

SHORT_PROMPT = (
    "당신은 정부 창업지원사업 신청서를 대신 써 주는 조수입니다. 아래 아이디어로 '문제 인식' 문항 답변을 "
    "한국어 1200자 내외로 쓰세요. 근거 수치를 인용하는 문체로, 소제목 없이 이어지는 산문으로 쓰세요.\n\n"
    "[아이디어] 1인 가구를 위한 냉장고 속 식재료 유통기한 관리 앱. 사진 한 장으로 재고를 등록하고 "
    "임박한 식재료로 만들 수 있는 레시피를 추천한다."
)

IDEA = ("1인 가구를 위한 냉장고 속 식재료 유통기한 관리 앱. 사진 한 장으로 재고를 등록하고 "
        "임박한 식재료로 만들 수 있는 레시피를 추천한다. 유통기한이 임박하면 알림을 주고, "
        "주변 마트의 할인 정보와 묶어 장보기 목록을 만들어 준다.")

# 조사 결과가 붙은 실사용 프롬프트를 만들기 위한 가짜 참고자료 (프롬프트 길이를 실제와 맞추는 게 목적).
FAKE_REFERENCES = [
    {"fact": "2025년 국내 1인 가구는 전체 가구의 35.5%로 역대 최고를 기록했다.",
     "quote": "1인 가구 비중은 35.5%로 집계됐다", "source_title": "인구총조사",
     "url": "https://kostat.go.kr/a", "date": "2025-12-01", "publisher": "통계청",
     "use_for": "시장 규모"},
    {"fact": "가정에서 버려지는 식품은 하루 평균 1인당 130g으로 조사됐다.",
     "quote": "1인당 하루 130g의 식품이 폐기된다", "source_title": "음식물류 폐기물 통계",
     "url": "https://me.go.kr/b", "date": "2025-08-11", "publisher": "환경부", "use_for": "문제 크기"},
    {"fact": "식품 유통기한 표시가 소비기한으로 전면 전환되며 소비자 혼란이 보고됐다.",
     "quote": "소비기한 표시제 전환 이후 혼란이 이어지고 있다", "source_title": "소비기한 표시제 안착 현황",
     "url": "https://mfds.go.kr/c", "date": "2026-01-20", "publisher": "식품의약품안전처", "use_for": "제도 배경"},
    {"fact": "냉장고 관리 앱 이용자의 62%가 '재고 입력이 번거롭다'를 이탈 이유로 꼽았다.",
     "quote": "재고 입력이 번거롭다는 응답이 62%였다", "source_title": "푸드테크 이용 실태",
     "url": "https://kfoodtech.or.kr/d", "date": "2026-03-05", "publisher": "한국푸드테크협회",
     "use_for": "경쟁 서비스 한계"},
    {"fact": "국내 푸드테크 시장은 2025년 61조 원 규모로 연평균 12% 성장했다.",
     "quote": "국내 푸드테크 시장은 61조 원 규모다", "source_title": "푸드테크 산업 동향",
     "url": "https://mafra.go.kr/e", "date": "2026-02-14", "publisher": "농림축산식품부", "use_for": "성장성"},
    {"fact": "장보기 앱 사용자의 주간 평균 접속은 3.4회로 나타났다.",
     "quote": "주간 평균 접속 횟수는 3.4회였다", "source_title": "모바일 장보기 이용 행태",
     "url": "https://kisdi.re.kr/f", "date": "2026-04-02", "publisher": "정보통신정책연구원",
     "use_for": "사용 빈도"},
]


def real_messages(question_id: str = "q2") -> list[dict]:
    """실제 생성 경로와 같은 프롬프트 (system.md + 트랙 + 문항 md + 스타일, user 는 참고자료 포함 컨텍스트).

    부하 측정의 목적상 중요한 건 길이다 — 짧은 프롬프트로 재면 prefill 비용과 KV 점유를 과소평가한다."""
    from backend.pipeline import assemble
    form = {"question_id": question_id, "track": "tech", "style": "logic", "idea": IDEA,
            "team": "개발 2명, 기획 1명", "capability": "식품 유통 데이터 분석 경험 3년",
            "references": FAKE_REFERENCES}
    system, user, _ = assemble.build_prompts(form)
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]
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


async def one_call(client: httpx.AsyncClient, model: str, max_tokens: int,
                   messages: list[dict]) -> tuple[float, int, int, str | None]:
    t0 = time.perf_counter()
    try:
        r = await client.post("/chat/completions", json={
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": 0.7,
        })
        r.raise_for_status()
        usage = (r.json().get("usage") or {})
        return (time.perf_counter() - t0, int(usage.get("completion_tokens", 0)),
                int(usage.get("prompt_tokens", 0)), None)
    except Exception as e:
        return time.perf_counter() - t0, 0, 0, f"{type(e).__name__}: {str(e)[:120]}"


async def run_level(base_url: str, api_key: str, model: str, n: int, max_tokens: int, timeout: float,
                    messages: list[dict]) -> dict:
    stop = asyncio.Event()
    samples: list[dict] = []
    sampler = asyncio.create_task(sample_metrics(base_url, stop, samples))
    t0 = time.perf_counter()
    async with httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout,
                                 headers={"Authorization": f"Bearer {api_key}"}) as c:
        rows = await asyncio.gather(*(one_call(c, model, max_tokens, messages) for _ in range(n)))
    elapsed = time.perf_counter() - t0
    stop.set()
    await sampler

    lat = sorted(d for d, _, _, err in rows if err is None)
    toks = sum(t for _, t, _, err in rows if err is None)
    prompt_toks = [p for _, _, p, err in rows if err is None]
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
        "평균 프롬프트 토큰": round(sum(prompt_toks) / len(prompt_toks)) if prompt_toks else 0,
        "평균 생성 토큰": round(toks / len(lat)) if lat else 0,
        "KV캐시 최대": peak["vllm:kv_cache_usage_perc"],
        "서버 running 최대": peak["vllm:num_requests_running"],
        "서버 waiting 최대": peak["vllm:num_requests_waiting"],
        "preemption": round(preempt, 1),
        "오류 예시": errs[:2],
    }


async def run(levels: list[int], max_tokens: int | None, timeout: float, verbose: bool = True,
              prompt: str = "real", question_id: str = "q2") -> dict:
    from backend.llm.client import load_config
    cfg = load_config()
    llm = cfg["llm"]
    base_url, api_key, model = llm["base_url"], llm.get("api_key", "dummy"), llm["model"]
    max_tokens = max_tokens or int(llm.get("max_tokens", 4096))       # 기본값은 실사용과 같은 상한
    messages = (real_messages(question_id) if prompt == "real"
                else [{"role": "user", "content": SHORT_PROMPT}])
    chars = sum(len(m["content"]) for m in messages)
    if verbose:
        print(f"대상: {base_url} / {model} / max_tokens={max_tokens} / 프롬프트={prompt} ({chars}자)")
    rows = []
    for n in levels:
        row = await run_level(base_url, api_key, model, n, max_tokens, timeout, messages)
        rows.append(row)
        if verbose:
            print(json.dumps(row, ensure_ascii=False))
        await asyncio.sleep(3)          # 다음 단계 전에 서버가 큐를 비우도록
    return {"levels": rows, "model": model, "max_tokens": max_tokens, "prompt": prompt}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--levels", default="12,20,30,45")
    ap.add_argument("--max-tokens", type=int, default=None, help="기본: config.yaml llm.max_tokens (실사용과 동일)")
    ap.add_argument("--timeout", type=float, default=600)
    ap.add_argument("--prompt", choices=["real", "short"], default="real",
                    help="real: 실제 생성 프롬프트(문항 md + 참고자료 포함), short: 짧은 한 줄 프롬프트")
    ap.add_argument("--question", default="q2")
    ap.add_argument("--json", help="결과 저장 경로")
    a = ap.parse_args()
    out = asyncio.run(run([int(x) for x in a.levels.split(",") if x.strip()], a.max_tokens, a.timeout,
                          prompt=a.prompt, question_id=a.question))
    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        print("저장:", a.json)


if __name__ == "__main__":
    main()
