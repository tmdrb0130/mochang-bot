# 프롬프트 파이프라인 진단 (2026-09-01)

외부에서 받은 진단 지시를 코드로 검증한 결과. 지시는 **조립된 프롬프트 9개만 읽고** 작성돼서
코드를 확인하면 사실과 다른 항목이 여럿 있다. 항목마다 근거(파일:라인)를 남긴다.

## 0. 프롬프트가 만들어지는 전체 흐름

```
[1] 인테이크   POST /intake            prompts/intake.md
      └ 아이디어 → 물어볼 카드 생성 → 사용자가 답 → answers[]

[2] 조사       POST /research          prompts/research/queries.md → extract_facts.md
      └ 검색어 생성 → ddgs 검색 → trafilatura 본문 추출 → 사실 JSON(facts[])

[3] 생성       POST /generate          assemble.build_prompts()
      system = system.md + tracks/{track}.md + questions/{qid}.md + styles/{style}.md
                                                                    (100자 문항이면 short_question.md)
      user   = assemble.build_context(form)
               지원 트랙 / 아이디어 / 창업여부 / 팀원 / 역량
               + [지원자가 확인한 사실]      ← answers 있을 때만
               + [지원자가 아직 모른다고 한 것] ← answers 있을 때만
               + [웹 참고자료]                ← references 있을 때만 (format_references)

[4] 검증       POST /verify            prompts/verify.md   ※ 생성 흐름에 연결돼 있지 않음
```

- 조립: `backend/pipeline/assemble.py:114 build_prompts`, `:59 build_context`
- 참고자료 렌더링: `backend/rag/pipeline.py:212 format_references`
- 문항 md 는 `only_business` 를 포함한 frontmatter 를 가진다: `assemble.py:46 load_question`

## 1. 지시 항목 검증

### 1-1. 사실이 아닌 것 (적용하지 않음)

| 지시 | 검증 결과 |
|---|---|
| "공통 블록이 문항별 파일에 복제되어 드리프트 중" | **거짓.** 공통 블록은 `prompts/system.md` 단일 소스. `grep -rl "반드시 지킬 것" prompts/` → system.md 하나뿐 |
| "Q2 프롬프트에만 '쓰지 않습니다.a' 오타" | **거짓.** 저장소 전체에 그런 문자열 없음 |
| "Q7-1이 예비창업자에게 생성되고 있음" | **거짓.** 프론트가 이미 거른다 — `frontend/src/App.jsx:282` `QUESTIONS.filter((q) => !q.onlyBusiness \|\| form.isBusiness)`. 내가 dry-run 으로 강제 호출해서 그렇게 보였을 뿐 |
| "[지원자가 확인한 사실] 블록이 입력 폼에 없다" | **거짓.** 인테이크(`/intake` → `answers[]`)가 그 역할. `assemble.py:85 _answer_sections`. 덤프한 프롬프트에 없었던 건 내가 `answers` 를 안 넘겼기 때문 |
| "입력 폼에 필드가 없다"며 폼 대폭 확장 요구 | **중복 설계.** 인테이크가 아이디어를 보고 물어볼 것을 동적으로 만든다. 고정 필드를 늘리면 인테이크와 이중 관리 |

### 1-2. 사실이고 고칠 가치가 있는 것 (적용함)

| # | 문제 | 근거 |
|---|---|---|
| A | **Q10 이 Q1 처럼 나온다** | `assemble.py:129` limit≤100 이면 `short_question.md` 강제. 그 파일 1행이 `[짧은 문항 규칙 — 다른 어떤 지시보다 우선]`, 5행이 "문장 끝은 '~하는 앱입니다'처럼 명사로 맺는다". `q10.md` 의 "Q1보다 감정적", "질문형·상황 제시형" 이 전부 덮인다 |
| B | **길이 규칙 3중 불일치** | Q1 `target: 60~95자` vs short_question 렌더값 `60~90`. Q10 `target: 70~95자` vs 필수요소 `70~90자` vs 렌더값 `60~90` (`assemble.py:133` `min=limit*0.6, max=limit*0.9`) |
| C | **`출처: null`** | `rag/pipeline.py:219` `pub = f.get("publisher") or ... or "출처 미상"` — 추출 모델이 문자열 `"null"` 을 내보내면 파이썬에서 참이라 폴백이 안 걸린다 |
| D | **연도가 항상 "연도 미상"** | `pipeline.py:218` `year = (f.get("date") or "")[:4] or "연도 미상"`. 실측 5건 전부 `date: null`. 그런데 URL 에 날짜가 있다 (`newspim.com/news/view/20250313000298`) |
| E | **같은 출처가 여러 줄로 분리** | 실측에서 뉴스핌 기사 하나가 3·4·5번으로 쪼개짐 → 모델이 출처를 3번 반복 인용 |
| F | **인용 형식을 프롬프트가 강제** | `system.md:23` 과 `sections.md` references 머리말이 둘 다 `"출처: 매체/기관, 연도"를 문장에 붙입니다` 를 지시. GPT-5 로 실험해도 `(출처: …)` 를 5회 쓴다 → **모델 문제가 아니라 지시 문제** |
| G | **참고자료가 100자 문항에도 들어간다** | `build_context` 는 문항과 무관하게 references 를 붙인다. Q1·Q10(90자 출력)에 1,000자 참고자료 + "인용하면 출처를 붙이라"는 낭비이자 유해 |
| H | **fact 요약이 원문과 반대** | 실측: `fact: "학부모의 인식실태는 긍정적이다"` ↔ `quote: "영어 사용능력이 부족하여 같이 대화를 나누지는 못한다"` |
| I | **system.md 내부 모순** | `:14` "다른 문항을 참조하지 않습니다" ↔ `:30` "Q2→Q3-1→…이 한 줄기로 연결되게" |
| J | **Q2 가 멘토 이야기를 쓴다** | `q2.md [피할 것]` 에 Q3-1·Q3-2·Q8 은 있는데 **Q4-2(멘토)가 없다**. 실측 출력에 `책임멘토` 2회 |
| K | **백엔드에 only_business 가드 없음** | 프론트만 막는다. `/generate` 직접 호출 시 예비창업자도 Q7-1 생성됨 |

### 1-3. 옳지만 지금 하지 않는 것 (별도 결정 필요)

- **브리프 단계 + 순차 생성** (지시 §2-7): 아이디어는 좋지만 모델 호출이 문항당 1회 이상 늘고, 지금의 병렬 처리(45건 동시 73초) 이점을 버린다. CLAUDE.md 의 "호출 수를 늘리는 설계는 PROGRESS 결정 필요에 적고 진행" 규칙 대상.
- **validator + 재생성 루프** (§2-10): 검증 자체는 호출 없이 가능하나 재생성 루프가 호출을 배로 늘린다. validator 만 먼저 만드는 것이 합리적.
- **분량 하한 1,900 → 1,200 완화** (§2-5): 신청서는 분량이 곧 성의로 읽히는 면이 있어 사업 판단이 필요하다.
- **금지 어휘 대량 추가** (§2-8): "1차 고객", "타깃" 등은 실제로 유용한 표현이라 일괄 금지는 과하다.

## 2. 이번에 적용한 수정

| # | 파일 | 변경 |
|---|---|---|
| A | `prompts/short_question.md` | `[짧은 문항 규칙 — 다른 어떤 지시보다 우선]` → `[짧은 문항 규칙]`. 마지막 줄에 "[작성 문항]의 지시와 어긋나면 [작성 문항]을 따른다" 추가. Q1 전용 규칙(명사 마무리, 누구에게 무엇을) 은 q1.md 로 이동 |
| A | `prompts/questions/q10.md` | "Q1 과 같은 문장 구조를 쓰지 않는다", "명사로 맺지 않아도 된다. 질문형·상황 제시형 허용" 명시 |
| B | `prompts/questions/q1.md`, `q10.md` | frontmatter 에 `min`/`max` 추가(Q1 60~90, Q10 70~90). `target` 도 같은 값으로 통일 |
| B | `pipeline/assemble.py:129` | 짧은 문항 길이를 `limit×0.6/0.9` 고정이 아니라 frontmatter 의 `min`/`max` 우선으로 |
| C | `rag/pipeline.py` | `_clean()` 추가 — "null", "none", "미상", "없음" 등 자리표시 문자열을 빈 값으로 정규화 |
| D | `rag/pipeline.py` | `_year_of()` 추가 — date 가 비면 URL 에서 연도를 건진다. 숫자 id 오인을 막으려 "연도+월(01~12)" 이 이어질 때만 인정 |
| E | `rag/pipeline.py` | `format_references()` — 같은 URL 의 사실을 한 출처 아래로 묶는다 |
| C·F | `rag/pipeline.py` | 목록 표기에서 `(출처: X, 연도)` 제거 → `3. 2025년 뉴스핌`. 연도 없으면 "인용할 때 연도를 쓰지 말 것", 출처 없으면 "이 항목은 인용하지 말 것" |
| F | `prompts/system.md` | 인용 규칙 재작성 — "출처:" 를 본문에 쓰지 말고 문장에 녹일 것, 좋은 예/나쁜 예 제시, 같은 자료 연속 인용 시 출처 반복 금지, 통계 두 개는 나열 말고 이어 줄 것 |
| F | `prompts/sections.md` | references 머리말의 같은 지시를 동일하게 교체(중복 소스였음) |
| G | `pipeline/assemble.py` | `_is_short_question()` 추가 — Q1·Q10 에는 참고자료 블록을 넣지 않는다 |
| I | `prompts/system.md` | "다른 문항을 참조하지 않습니다" → "다른 문항을 가리키는 말을 쓰지 않습니다. 내용이 이어지는 것은 좋다" ("한 줄기로 연결" 과의 모순 해소) |
| J | `prompts/questions/q2.md` | `[피할 것]` 에 "멘토에게 받고 싶은 도움 — Q4-2의 몫" 추가. 6번 문단을 "착안 계기 → 약속 → 자격" 으로 재작성 |
| K | `pipeline/assemble.py:build_prompts` | `only_business` 가드 — 예비창업자가 Q7-1 을 요청하면 `PromptNotFound` |

### 검증

- `pytest` **87 passed** (기존 83 + 신규 4: null 가드 / URL 연도 추출 / 같은 출처 묶기 / 짧은 문항 참고자료 제외)
- 실측 5건 + "null" 케이스로 렌더링 확인: 뉴스핌 3건이 `3. 2025년 뉴스핌` 한 묶음으로, 연도 없는 2건은 "인용할 때 연도를 쓰지 말 것", publisher `"null"` 은 "이 항목은 인용하지 말 것"
- Q1·Q10 참고자료 제외 확인, Q2 는 그대로 포함
- Q7-1: `is_business=False` → 차단, `True` → 생성

### 원본 백업

- `backend/prompts.before-fix/`
- `backend/pipeline/assemble.py.before-fix`, `backend/rag/pipeline.py.before-fix`
- `tests/test_pipeline.py.before-fix`

## 3. 아직 남은 것 (§1-2 중 미적용)

- **H. fact 요약이 원문과 반대** — `prompts/research/extract_facts.md` 수정 필요. 실측: `fact: "학부모의 인식실태는 긍정적이다"` ↔ `quote: "…같이 대화를 나누지는 못한다"`. 요약 대신 quote 를 그대로 쓰게 하거나, 극성 검증을 넣는 방법이 있다.
- **publisher 부정확** — `scienceon.kisti.re.kr` 을 "한국과학기술정보연구원"으로 붙였는데 이는 검색 사이트 운영기관이지 논문 발행처가 아니다.
- **quote 가 영어 원문** — 한국어 글에 영어 인용을 그대로 넘기고 있다.
- **validator** — 출력 검증(출처 화이트리스트, 금지어, 반복률, 문항 간 유사도)은 모델 호출 없이 가능하므로 다음 작업으로 적절하다.
