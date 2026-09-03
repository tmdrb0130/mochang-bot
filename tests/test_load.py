"""부하 테스트를 pytest 로도 — 워커 수보다 많은 동시 요청, 가짜 모델, 큐 제한 확인 (동기 경로 + /jobs 경로)."""
import pytest

from scripts.load_test import run


@pytest.mark.asyncio
async def test_sync_endpoint_is_limited_by_queue():
    # 요청 수는 config.yaml max_workers 보다 커야 "큐가 상한에서 막는다"를 본다 (2026-09-03 워커 80 으로 올리며 50 고정이 깨짐).
    from backend import main as M
    n = M.client.queue.max_workers + 20
    r = await run(n=n, delay=0.02, use_jobs=False, verbose=False)
    assert r["ok"] == n and r["limited"] and r["peak_concurrent_llm"] == r["max_workers"]


@pytest.mark.asyncio
async def test_jobs_submit_and_poll_path():
    r = await run(n=30, delay=0.02, use_jobs=True, verbose=False)
    assert r["ok"] == 30 and r["limited"] and r["queue"]["done"] >= 30


# ── 0단계 검색어 중복도 분석 (scripts/query_dump.py) — 모델 호출 없이 계산 부분만 ──

def test_query_dump_analyse_counts_overlap():
    from scripts.query_dump import analyse, contained, jaccard, norm, tokens

    assert norm("1인 가구, 현황!") == norm("1인가구현황")
    assert jaccard(tokens("냉장고 관리 앱"), tokens("냉장고 관리 앱")) == 1.0
    assert contained(tokens("냉장고 관리"), tokens("냉장고 관리 앱 시장 규모")) == 1.0

    dumps = [{"idea": "냉장고 관리 앱", "track": "general", "queries": {
        "idea": ["1인 가구 냉장고 관리 현황"],
        "q1": ["1인 가구 냉장고 관리 현황"],          # 아이디어 검색어와 완전히 같음
        "q2": ["식품 폐기물 발생량 통계"],            # 전혀 다른 각도
    }}]
    row = analyse(dumps)["per_idea"][0]
    assert row["총 검색어"] == 3 and row["중복 제거 후"] == 2
    assert row["아이디어 검색어가 덮는 문항 검색어"] == "1/2"
    assert row["사실상 같은 검색어 쌍(토큰 50%+)"] == 1        # q1↔q2 는 안 겹침
    assert row["가장 비슷한 문항쌍"][2] == 0.0                  # q1 vs q2


# ── GPU 동시성 측정 (scripts/gpu_probe.py) — 지표 파싱만. 실제 호출은 사람이 실행 ──

def test_gpu_probe_parses_prometheus_metrics():
    from scripts.gpu_probe import parse_metrics

    text = "\n".join([
        "# HELP vllm:num_requests_running 실행 중",
        "# TYPE vllm:num_requests_running gauge",
        'vllm:num_requests_running{engine="0",model_name="llama"} 12.0',
        'vllm:kv_cache_usage_perc{engine="0",model_name="llama"} 0.873',
        'vllm:num_preemptions_total{engine="0",model_name="llama"} 4.0',
        'vllm:generation_tokens_total{engine="0"} 1000.0',
        'vllm:generation_tokens_total{engine="1"} 500.0',
        'vllm:관심없는_지표{a="b"} 9.0',
        "쓰레기줄",
    ])
    m = parse_metrics(text)
    assert m["vllm:num_requests_running"] == 12.0
    assert m["vllm:kv_cache_usage_perc"] == 0.873
    assert m["vllm:num_preemptions_total"] == 4.0
    assert m["vllm:generation_tokens_total"] == 1500.0      # 라벨이 달라도 같은 이름은 합산
    assert "vllm:관심없는_지표" not in m
