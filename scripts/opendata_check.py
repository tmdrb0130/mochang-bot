"""정형 공공 API 어댑터 실호출 점검 — 소스마다 1건씩만 부른다 (KOSIS 는 검색+수치 2건).

  .venv/Scripts/python -m scripts.opendata_check
  .venv/Scripts/python -m scripts.opendata_check --source ecos --query "기준금리 추이"

키는 .env 의 KOSIS_API_KEY / ECOS_API_KEY / DATA_GO_KR_API_KEY 를 읽는다.
키가 없는 소스는 '키 없음'으로 표시하고 건너뛴다. 캐시를 거치지 않으므로 어댑터 자체만 검증한다.
"""
from __future__ import annotations

import argparse
import asyncio

# 소스별 기본 점검 검색어 — 각 소스의 트리거 키워드를 포함해야 한다.
SAMPLES = {
    "kosis": "1인 가구 현황 통계",
    "ecos": "기준금리 환율 추이",
    "kstartup": "청년 창업지원 공고",
    "sangkwon": "음식 업종 점포 수",
}


async def run(only: str = "", query: str = "") -> int:
    from backend.llm.client import load_config
    from backend.rag.opendata import OpenDataConfig, build_sources, triggered

    cfg = OpenDataConfig.from_config((load_config().get("research") or {}))
    sources = dict(build_sources(cfg))
    names = [only] if only else list(SAMPLES)
    bad = 0

    for name in names:
        q = query or SAMPLES.get(name, "")
        fn = sources.get(name)
        if fn is None:
            print(f"[{name}] 키 없음 또는 설정에서 제외 — 건너뜀")
            continue
        if not triggered(name, q):
            print(f"[{name}] 검색어에 트리거 키워드가 없어 호출하지 않음: {q!r}")
            continue
        try:
            rows = await fn(q, 4)
        except Exception as e:
            print(f"[{name}] 실패 — {type(e).__name__}: {str(e)[:200]}")
            bad += 1
            continue
        if not rows:
            print(f"[{name}] 결과 0건 (검색어: {q!r})")
            continue
        print(f"[{name}] {len(rows)}건 (검색어: {q!r})")
        for r in rows:
            print(f"    제목: {r['title'][:60]}")
            print(f"    출처: {r['url'][:80]}  날짜: {r['date']}  fetch생략: {r.get('no_fetch')}")
            print(f"    본문: {r['snippet'][:220]}")
    return bad


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default="", choices=["", *SAMPLES])
    ap.add_argument("--query", default="")
    a = ap.parse_args()
    raise SystemExit(asyncio.run(run(a.source, a.query)))


if __name__ == "__main__":
    main()
