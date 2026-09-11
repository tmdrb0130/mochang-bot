"""이관 검증 — 예전 JSON 저장소(simple)와 Chroma 가 같은 질의에 같은 답을 주는지 (2026-09-11).

    .venv/Scripts/python scripts/vectorstore_compare.py
    .venv/Scripts/python scripts/vectorstore_compare.py --k 5 --queries "질의1" "질의2"

**완전 일치를 기대하지 않는다.** 예전 저장소는 전수 비교(정확한 최근접)이고 Chroma 는 HNSW(근사)다.
상위 결과의 URL 집합이 대부분 겹치고 순서가 한두 개 뒤바뀌는 것은 정상이다.
기준: **상위 k개 중 k-1개 이상 URL 일치**. 미달이면 hnsw:search_ef 를 올려 재측정한다.

점수도 함께 찍는다 — is_sufficient 가 `score >= min_score(0.6)` 인 적중이 min_hits(5)건 이상인지로
**웹 검색 생략 여부**를 정하기 때문이다. 두 저장소의 점수 척도가 어긋나면 그 판정이 통째로 달라진다.
모델 호출은 질의 임베딩뿐(질의당 1회, 로컬 Ollama). GPU 서버는 쓰지 않는다.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from backend.llm.client import load_config          # noqa: E402
from backend.rag.pipeline import ResearchConfig     # noqa: E402
from backend.rag.vectorstore import VectorStore, ollama_embedding   # noqa: E402

QUERIES = [
    "1인 가구 식품 폐기 통계",
    "대학생 창업 지원 사업",
    "발달장애인 직업 훈련 프로그램",
    "배달 플랫폼 수수료 구조",
    "청년 주거 문제 실태",
    "노인 돌봄 서비스 시장 규모",
    "중고 거래 앱 이용자 수",
    "특수교육 보조 공학 기기",
    "소상공인 온라인 판로 지원",
    "반려동물 시장 성장률",
]


def open_store(cfg: ResearchConfig, backend: str) -> VectorStore:
    embed = ollama_embedding(cfg.vectorstore_embed_model, cfg.vectorstore_ollama_url)
    path = cfg.vectorstore_chroma_dir if backend == "chroma" else cfg.vectorstore_dir
    t0 = time.monotonic()
    store = VectorStore(path, embed_model=embed, backend=backend,
                        min_score=cfg.vectorstore_min_score, min_hits=cfg.vectorstore_min_hits,
                        max_age_days=cfg.vectorstore_max_age_days,
                        freshness_days=cfg.vectorstore_freshness_days)
    print(f"  {backend:<7} 열기 {time.monotonic() - t0:>6.1f}초  ({path})")
    return store


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--queries", nargs="*", default=QUERIES)
    args = ap.parse_args()

    cfg = ResearchConfig.from_config(load_config())
    print("저장소 여는 중 (simple 은 100초 이상 걸린다)")
    chroma = open_store(cfg, "chroma")
    simple = open_store(cfg, "simple")

    print(f"\n질의 {len(args.queries)}개 · 상위 {args.k}개 비교 (기준: {args.k - 1}개 이상 URL 일치)\n")
    ok = 0
    for q in args.queries:
        a = simple.query(q, args.k)
        b = chroma.query(q, args.k)
        ua = [h["url"] for h in a]
        ub = [h["url"] for h in b]
        overlap = len(set(ua) & set(ub))
        both_empty = not ua and not ub          # 둘 다 적중 0건이면 불일치가 아니다
        passed = both_empty or overlap >= min(args.k - 1, len(ua), len(ub))
        verdict = "OK " if passed else "!! "
        ok += passed
        sa = [round(float(h.get("score", 0)), 3) for h in a]
        sb = [round(float(h.get("score", 0)), 3) for h in b]
        print(f"{verdict}{q}")
        print(f"     겹침 {overlap}/{args.k}   simple 점수 {sa}")
        print(f"                  chroma 점수 {sb}")
        print(f"     웹검색 생략 판정  simple={simple.is_sufficient(a)}  chroma={chroma.is_sufficient(b)}")
        if set(ua) - set(ub):
            print(f"     simple 에만: {[u[:60] for u in list(set(ua) - set(ub))[:2]]}")

    print(f"\n합격 {ok}/{len(args.queries)}")
    # 점수 척도가 어긋나면 min_score 재조정이 필요하다 — 분포를 한 줄로 요약해 둔다.
    all_a = [float(h.get("score", 0)) for q in args.queries for h in simple.query(q, args.k)]
    all_b = [float(h.get("score", 0)) for q in args.queries for h in chroma.query(q, args.k)]
    if all_a and all_b:
        print(f"점수 평균  simple {sum(all_a) / len(all_a):.3f}  vs  chroma {sum(all_b) / len(all_b):.3f}"
              f"   (min_score={cfg.vectorstore_min_score})")
    return 0 if ok == len(args.queries) else 1


if __name__ == "__main__":
    raise SystemExit(main())
