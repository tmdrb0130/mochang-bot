"""문항별 근거 배분 — 조사로 얻은 facts 를 문항 역할에 맞게 겹치지 않게 나눈다 (작업 33).

배경: 2단계(공통 조사)로 문항 9개가 같은 facts 를 받아 같은 통계를 각자 인용했다(실측: 한 신청서에 같은 인용 9회).
번호 순서로 2건씩 끊어 주던 임시 배분(`references_for`)을, **문항 역할에 맞는 각도의 사실을 우선** 주는 배분으로 바꾼다.

문항별 조사 각도 (분야 중립):
  Q2   problem    문제의 크기 · 고객 행동 변화 · 현황 통계
  Q3-1 competitor 기존 대안·경쟁 서비스가 하는 일 · 기술·방식 동향
  Q3-2 pricing    유사 서비스 과금 · 가격대 · 지불 행태 · 시장 규모
  trend(산업·시장 변화)는 위 세 문항이 나눠 쓴다. Q4-1·Q4-2·Q8·Q1·Q10 은 근거를 받지 않는다.

배분은 facts 목록만으로 결정되는 순수 함수라, 문항마다 따로 불러도 **같은 사실이 두 문항에 가지 않는다.**
"""
from __future__ import annotations

import re

ANGLES = ("problem", "competitor", "pricing", "trend")
ANGLE_LABEL = {
    "problem": "문제의 크기·고객 행동·현황 통계",
    "competitor": "기존 대안·경쟁 서비스가 하는 일, 기술·방식 동향",
    "pricing": "유사 서비스의 과금·가격대·지불 행태·시장 규모",
    "trend": "산업·시장의 변화",
}
QUESTION_ANGLES = {"q2": ("problem",), "q3_1": ("competitor",), "q3_2": ("pricing",), "q7_1": ("problem",)}
RECEIVERS = ("q2", "q3_1", "q3_2")            # 근거를 받는 긴 문항 (순서 = trend·미분류 사실을 돌려 주는 순서)
MAX_PER_QUESTION = 3

# 각도가 안 붙은 사실을 키워드로 분류할 때 쓰는 단서 (분야 중립)
ANGLE_HINTS = {
    "problem": ("불편", "어려움", "비율", "가구", "인구", "명이", "%가", "실태", "현황", "겪"),
    "competitor": ("서비스", "플랫폼", "앱", "출시", "기능", "경쟁", "업체", "제공하", "운영하"),
    "pricing": ("원", "가격", "요금", "구독", "결제", "지불", "매출", "시장 규모", "억", "만 원", "수수료"),
}


def angle_of(fact: dict) -> str:
    """사실의 각도. 추출 단계에서 붙인 angle 이 있으면 그것, 없으면 use_for·fact 문장의 단서로 추정. 모르면 'trend'."""
    given = str(fact.get("angle") or "").strip().lower()
    if given in ANGLES:
        return given
    text = f"{fact.get('use_for', '')} {fact.get('fact', '')}"
    scores = {a: sum(1 for h in hints if h in text) for a, hints in ANGLE_HINTS.items()}
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else "trend"


def _tokens(text: str) -> set[str]:
    return {w for w in re.split(r"[^\w가-힣]+", text or "") if len(w) >= 2}


def assign(facts: list[dict], question_hints: dict[str, str] | None = None) -> dict[str, list[dict]]:
    """facts → {문항: [facts]}. 각도 일치 우선, 남는 것은 문항 키워드 매칭, 그래도 남으면 돌려 준다. 한 사실은 한 문항에만.

    question_hints: {문항: 그 문항이 다루는 내용(골자 몫 텍스트)} — 키워드 매칭에 쓴다. 없으면 각도만으로."""
    out: dict[str, list[dict]] = {q: [] for q in RECEIVERS}
    leftovers: list[dict] = []
    for fact in facts or []:
        if not isinstance(fact, dict):
            continue
        angle = angle_of(fact)
        target = next((q for q, angles in QUESTION_ANGLES.items() if angle in angles and q in out), None)
        if target and len(out[target]) < MAX_PER_QUESTION:
            out[target].append(fact)
        else:
            leftovers.append(fact)

    hints = {q: _tokens(t) for q, t in (question_hints or {}).items() if q in out}
    for fact in list(leftovers):
        if not hints:
            break
        words = _tokens(f"{fact.get('fact', '')} {fact.get('use_for', '')}")
        scored = sorted(((len(words & hints[q]), q) for q in hints if len(out[q]) < MAX_PER_QUESTION), reverse=True)
        if scored and scored[0][0] > 0:
            out[scored[0][1]].append(fact)
            leftovers.remove(fact)

    i = 0
    for fact in leftovers:                    # 각도도 키워드도 없는 사실은 자리가 남은 문항에 차례로
        for _ in range(len(RECEIVERS)):
            q = RECEIVERS[i % len(RECEIVERS)]
            i += 1
            if len(out[q]) < MAX_PER_QUESTION:
                out[q].append(fact)
                break
    return out


def for_question(facts: list[dict], question_id: str, question_hints: dict[str, str] | None = None) -> list[dict]:
    """이 문항 몫의 사실. 근거를 받지 않는 문항이면 빈 목록."""
    qid = str(question_id or "")
    if qid not in RECEIVERS:
        return []
    return assign(facts, question_hints).get(qid, [])
