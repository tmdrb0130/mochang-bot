"""네이버 검색 API(NAVER API HUB) 키·규격 점검 — 실호출 최소(유형당 1건).

  .venv/Scripts/python -m scripts.naver_check                     # 기본 검색어로 news+blog 확인
  .venv/Scripts/python -m scripts.naver_check --query "1인 가구 식품 폐기" --kinds news,blog,webkr

키는 .env 의 NAVER_CLIENT_ID / NAVER_CLIENT_SECRET 을 읽는다 (NCP 콘솔의 Client ID / Client Secret).
한도는 검색 통합 월 775,000건·키당 50 RPS 라 이 점검(2~4건)은 무시할 수준이다.
키가 맞는지, 어댑터가 API HUB 규격(경로·헤더)에 맞는지 확인하는 용도 — 캐시를 거치지 않는다.
"""
from __future__ import annotations

import argparse
import asyncio


async def run(query: str, kinds: list[str], display: int = 3) -> int:
    from backend.llm.client import load_config          # .env 로드 겸용
    from backend.rag.research import NaverConfig, NaverSearch, SearchRateLimited

    load_config()
    cfg = NaverConfig.from_config(((load_config().get("research") or {})))
    if kinds:
        cfg.kinds = kinds
    if not cfg.configured:
        print("키가 없다: .env 에 NAVER_CLIENT_ID / NAVER_CLIENT_SECRET 를 넣어야 한다.")
        return 2

    print(f"엔드포인트: {cfg.base_url}/{{{'|'.join(cfg.kinds)}}}  (인증 헤더 X-NCP-APIGW-API-KEY-ID/-KEY)")
    search = NaverSearch(cfg)
    try:
        rows = await search(query, display * len(cfg.kinds))
    except SearchRateLimited as e:
        print(f"429 한도 초과: {e}")
        return 1

    if search.last_error:
        print(f"경고(일부 유형 실패): {search.last_error}")
    if not rows:
        print("결과 0건 — 키·권한(콘솔에서 검색 API 사용 설정)을 확인할 것.")
        return 1

    print(f"결과 {len(rows)}건")
    for r in rows[:6]:
        print(f"  [{r['source_type']}] {r['date'] or '날짜미상'}  {r['title'][:40]}  {r['url'][:60]}")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--query", default="1인 가구 식품 폐기 통계")
    ap.add_argument("--kinds", default="", help="쉼표 구분 (기본: config.yaml 의 research.naver.kinds)")
    a = ap.parse_args()
    kinds = [k.strip() for k in a.kinds.split(",") if k.strip()]
    raise SystemExit(asyncio.run(run(a.query, kinds)))


if __name__ == "__main__":
    main()
