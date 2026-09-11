# 작업 20 — 사업계획 골자(outline) 공유로 문항 간 반복 제거·일관성 확보

> 2026-09-02 13:1x 사용자 승인("그렇게 진행해보자"). 구현: 세션3. 검증 스크립트: `docs/tools/fullrun_repeat_check.py`(세션1, 실서비스 1인분 생성 + 반복 측정).

## 1. 문제

문항 9개가 **서로를 모른 채 독립 호출**된다 (`assemble.build_context`: 아이디어 + 인테이크 답변 + 그 문항 참고자료만).
같은 재료를 매 문항이 처음부터 서술하니 고객·문제·해결책·수익이 여러 문항에서 거의 같은 문장으로 반복되고,
`system.md` 의 "Q2→Q3-1→Q3-2→Q4-1→Q8 한 줄기" 지시는 서로 못 보는 구조라 작동할 수 없다.
순차 생성(앞 글을 뒤에 넘김)은 9문항 직렬로 4분이 걸리고 라마가 "반복 금지"를 우회하는 것이 작업 15·18 에서 확인됐으므로 택하지 않는다.

## 2. 방식 — 아이디어당 골자 1회, 전 문항 공유 + 문항별 역할표

```
인테이크 답변 + 공통 조사 facts ──▶ [골자 생성 1회, 캐시] ──▶ 모든 문항 프롬프트에 같은 골자 주입
                                                           └▶ 문항 md 의 [골자 사용 규칙]: 펼칠 것 / 한 문장만 / 쓰지 말 것
```

- 병렬 생성(프론트 3개 동시 제출) 유지. 아이디어당 라마 +1회(약 20초), 공통 조사와 같은 방식으로 **캐시**(키 = 아이디어+답변 해시).
- 프론트 변경 없음: `generate_one` 이 골자를 없으면 만들고(같은 키 동시 요청은 `asyncio.Lock` 으로 1회만), 있으면 캐시에서 읽는다.
- 응답에 `outline_used: bool` 추가(필드 추가만). `config.yaml generate.outline.enabled` 로 on/off.

## 3. 골자 형식 (`prompts/outline.md` 신규 — `tests/test_prompts.py` 등록. 출력은 JSON)

| 키 | 내용 | 길이 |
|---|---|---|
| customer | 핵심 고객(페르소나 한 줄) | 1문장 |
| problem | 그 고객의 문제 상황 | 1문장 |
| evidence | 문제를 확인한 방법(인테이크 evidence). 없으면 "검증 계획: …" | 1문장 |
| solution | 제품·서비스 형태와 핵심 기능 2개 | 1~2문장 |
| differentiators | 기존 대안(alternative) 대비 다른 점 | 2개 |
| revenue | 누가 무엇에 어떤 방식으로 돈을 내나 | 1문장 (금액 미정이면 미정) |
| plan_6m | ⑥ MVP 기획 → ⑦ 제작 → ⑧ 고객 검증 | 3단계, 각 1문장 |
| capability | 이 아이디어에 쓰이는 지원자 역량 1개 | 1문장 |
| gaps | 지원자가 모른다고 한 것 → 멘토 요청 재료 | 1~3개 |
| key_stats | 참고자료 중 인용할 수치 **2개**(출처 포함). 없으면 빈 배열 | ≤2 |

규칙: 입력에 없는 것을 만들지 않는다(system.md 그대로). 금액·인원 수치 금지. 골자는 심사위원에게 안 보이는 내부 문서이므로 문체 규칙 불필요.

## 4. 문항별 역할표 — 각 문항 md 에 `[골자 사용 규칙]` 절로 넣는다

| 문항 | 펼쳐 쓴다 | 한 문장만 | 쓰지 않는다 | 수치 |
|---|---|---|---|---|
| Q1 | customer + solution 을 한 문장으로 | — | 나머지 전부 | 없음 |
| Q2 | problem, customer, evidence | solution | differentiators, revenue, plan_6m, capability | key_stats[0] |
| Q3-1 | differentiators, solution | problem | revenue, plan_6m, capability | key_stats[1] |
| Q3-2 | revenue | customer | problem, differentiators, plan_6m | 참고자료 중 가격·시장 규모만 |
| Q4-1 | plan_6m (⑥⑦⑧ 순서가 뼈대) | solution, revenue | problem, differentiators | 없음 |
| Q4-2 | gaps → 멘토 요청 | capability | 나머지 | 없음 |
| Q8 | capability | plan_6m 중 본인이 직접 할 단계 | 나머지 | 없음 |
| Q10 | customer + solution 을 대중 언어로 | — | 나머지 | 없음 |
| Q7-1 | problem (기존 사업과 다른 점) | solution | 나머지 | 없음 |

"한 문장만"은 **골자의 문장을 그대로 쓰지 말고 한 절로 줄여** 언급. 같은 수치는 지정 문항에서만 — 다른 문항에서 다시 인용하지 않는다.

## 5. 주입 위치

`assemble.build_context` 가 `[사업계획 골자 — 모든 문항이 같은 골자를 공유합니다. 아래 사실만 쓰고 바꾸지 않습니다]`(머리말은 `sections.md` 에)
섹션으로 골자 JSON 을 사람이 읽는 목록으로 넣고, 그 아래 해당 문항의 `[골자 사용 규칙]` 을 붙인다. 짧은 문항(Q1·Q10)은 골자 중 customer·solution 두 줄만.

## 6. 검증 (완료 조건)

`docs/tools/fullrun_repeat_check.py "<아이디어>" out.json` 을 **같은 아이디어 2개로 전/후** 실행(실서비스, 아이디어당 라마 약 20회).
- 문항 간 유사 문장 쌍(4-gram Jaccard ≥ 0.5): 전 대비 **절반 이하**
- 3회 이상 반복된 수치: **0**
- 문항별 글자수 하한 유지, Q1·Q10 100자 이내
- 문항 텍스트 8개를 사람이 읽었을 때 고객·문제·해결책 명칭이 전 문항에서 동일한 단어인지 (세션1이 읽고 판정)
- pytest 통과(골자 생성·캐시·역할표 주입은 FakeClient mock)
