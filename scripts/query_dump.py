"""자료조사 0단계 — 문항별 검색어를 실제로 뽑아 중복도를 측정한다 (RESEARCH_PLAN 0단계).

"문항 9개가 조사를 공유해도 되는가"(2단계)를 숫자로 판단하기 위한 사전 분석이다.
검색어 생성(LLM)만 하고 **검색·본문 fetch·사실 추출은 하지 않는다** — 외부 검색 요청 0건.

  .venv/Scripts/python -m scripts.query_dump                    # 기본 아이디어 2개
  .venv/Scripts/python -m scripts.query_dump --ideas 1 --json out.json
  .venv/Scripts/python -m scripts.query_dump --track tech

모델 호출 수 = 아이디어 수 × 10 (아이디어 조사 1 + 문항 9). 로컬 라마라 한도 부담은 없지만
공유 GPU 를 쓰므로 한가한 때 돌린다.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
from itertools import combinations

QUESTIONS = ["q1", "q2", "q3_1", "q3_2", "q4_1", "q4_2", "q7_1", "q8", "q10"]

IDEAS = [
    "1인 가구를 위한 냉장고 속 식재료 유통기한 관리 앱. 사진 한 장으로 재고를 등록하고 임박 식재료 레시피를 추천한다.",
    "지역 전통시장 상인을 위한 배달 대행 중개 플랫폼. 상인은 주문만 받고 배달은 지역 라이더가 묶음으로 처리한다.",
    "고령자 대상 복약 알림·모니터링 서비스. 약통 IoT 센서와 보호자 앱을 연동해 미복용을 가족에게 알린다.",
]

_STOP = {"현황", "통계", "시장", "규모", "동향", "사례", "전망", "분석", "관련", "국내", "최근", "2025", "2026"}


def norm(q: str) -> str:
    """비교용 정규화 — 공백·기호 제거, 소문자."""
    return re.sub(r"[^\w가-힣]", "", (q or "").lower())


def tokens(q: str) -> set[str]:
    return {w for w in re.split(r"[^\w가-힣]+", (q or "").lower()) if len(w) >= 2}


def jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if (a or b) else 0.0


def contained(part: set, whole: set) -> float:
    """part 의 토큰 중 whole 에 이미 있는 비율. '아이디어 검색어가 이 검색어를 덮는가' 판단용
    (jaccard 는 whole 이 크면 자동으로 낮아져서 이 판단엔 못 쓴다)."""
    return len(part & whole) / len(part) if part else 0.0


async def dump_one(client, cfg, idea: str, track: str) -> dict:
    from backend.pipeline import assemble
    from backend.rag import pipeline as P

    form = {"idea": idea, "track": track, "style": "logic"}

    async def for_question(qid: str | None) -> tuple[str, list[str]]:
        if qid is None:
            meta = P._idea_meta(form)                       # 인테이크 단계의 '아이디어 조사'
        else:
            m, body = assemble.load_question(qid)
            meta = {**m, "body": body}
        return (qid or "idea"), await P.generate_queries(client, form, meta, cfg)

    pairs = await asyncio.gather(*(for_question(q) for q in [None] + QUESTIONS))
    return {"idea": idea, "track": track, "queries": dict(pairs)}


def analyse(dumps: list[dict]) -> dict:
    rows = []
    for d in dumps:
        per_q = d["queries"]
        flat = [q for qs in per_q.values() for q in qs]
        uniq = {norm(q) for q in flat}
        idea_tokens = tokens(" ".join(per_q.get("idea", [])))
        covered = sum(1 for qid, qs in per_q.items() if qid != "idea"
                      for q in qs if contained(tokens(q), idea_tokens) >= 0.6)
        sims = [(a, b, round(jaccard(tokens(" ".join(per_q[a])), tokens(" ".join(per_q[b]))), 2))
                for a, b in combinations([k for k in per_q if k != "idea"], 2)]
        # 문자열은 달라도 토큰이 겹치면 검색 결과는 거의 같다 — "사실상 같은 검색어" 쌍 수
        near = sum(1 for (qa, a), (qb, b) in combinations([(qid, q) for qid, qs in per_q.items() for q in qs], 2)
                   if qa != qb and jaccard(tokens(a), tokens(b)) >= 0.5)
        rows.append({
            "idea": d["idea"][:30] + "…",
            "총 검색어": len(flat),
            "중복 제거 후": len(uniq),
            "중복률": f"{(1 - len(uniq) / len(flat)) * 100:.0f}%" if flat else "-",
            "아이디어 검색어가 덮는 문항 검색어": f"{covered}/{len(flat) - len(per_q.get('idea', []))}",
            "사실상 같은 검색어 쌍(토큰 50%+)": near,
            "문항쌍 유사도 평균": round(sum(s for *_, s in sims) / len(sims), 2) if sims else 0,
            "가장 비슷한 문항쌍": max(sims, key=lambda s: s[2]) if sims else None,
        })
    return {"per_idea": rows}


async def run(n_ideas: int, track: str, verbose: bool = True) -> dict:
    from backend import main as M
    from backend.rag import pipeline as P

    cfg = P.ResearchConfig.from_config(M.config)
    async with M.lifespan(M.app):
        dumps = await asyncio.gather(*(dump_one(M.client, cfg, idea, track) for idea in IDEAS[:n_ideas]))
    out = {"config": {"max_queries": cfg.max_queries, "track": track}, "dumps": list(dumps), **analyse(list(dumps))}
    if verbose:
        for d in out["dumps"]:
            print(f"\n■ {d['idea'][:40]}…")
            for qid, qs in d["queries"].items():
                print(f"  {qid:>6}: " + " | ".join(qs))
        print("\n── 중복도 ──")
        for r in out["per_idea"]:
            print(json.dumps(r, ensure_ascii=False, indent=2))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ideas", type=int, default=2, help=f"샘플 아이디어 수 (최대 {len(IDEAS)})")
    ap.add_argument("--track", default="general")
    ap.add_argument("--json", help="결과를 이 경로에 저장")
    a = ap.parse_args()
    out = asyncio.run(run(min(a.ideas, len(IDEAS)), a.track))
    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        print(f"\n저장: {a.json}")


if __name__ == "__main__":
    main()
