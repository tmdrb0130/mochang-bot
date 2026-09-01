# PROGRESS — 2026-09-01 세션 (다음: Llama 70B 머신에서 클론 후 이어서)

> 이 절이 최신. 08-31 자율 세션 보고는 아래에 그대로 둠.

## 오늘 한 것

| # | 항목 | 상태 |
|---|---|---|
| 1 | **Q4-2 멘토링 카드** — 인테이크 슬롯 `mentoring` 추가(9번째). 항상 카드 생성, ready 계산에선 제외, MAX_CARDS(7)에 잘려도 자리 보장, multi 보기 최대 8 | ✅ |
| 2 | **카드 재생성** — `POST /intake/regenerate` (+ `/jobs/intake_regenerate`). `prompts/intake_regenerate.md`, 이미 본 보기 금지(코드로도 필터), 지원자 메모, 다른 카드 답을 사실로 주입 | ✅ |
| 3 | 프론트 — `RegenerateBar`(메모 + "다른 보기 보기 (n)") 인테이크 카드·정보 보태기 패널 양쪽. ready 여도 카드 있으면 보여주고 머리말만 다르게 | ✅ vite build 통과 |
| 4 | 테스트 `tests/test_intake.py` 12개 추가 → **81 passed** (모델 호출 0). ASGI 스모크로 엔드포인트·jobs 경로 확인 | ✅ |
| 5 | README · PROJECT_CONTEXT 6-1 갱신 | ✅ |

**실호출은 안 했음** — 멘토링 카드 보기 품질, 재생성이 실제로 "다른 방향"을 내는지는 다음 세션에서 `/intake` 1회 + `/intake/regenerate` 1회로 확인할 것.

## 조사 결과: 무료 한도 카운터가 부정확한 이유 (오늘 확인)

배지 53/50 인데도 생성이 되던 문제. **카운터는 막지 않고 세기만 하는 게 설계**이고(README), 문제는 숫자가 틀리는 것:
- `client.py _complete()` 가 **요청 보내기 전에** `hit()` → 429·504·5xx 실패, 폴백(최대 3회), 백오프 재시도까지 전부 카운트. `.usage.json` count 57 vs by_model 합 42.
- 서버 밖 호출(CLI `test_generate --call`, curl, Vane 자체 호출)은 안 잡힘.
- OpenRouter 쪽: `GET /auth/key` 는 크레딧($) 기준이라 무료 요청은 0, 채팅 응답 헤더에 `x-ratelimit-*` **없음**, **`GET /api/v1/activity` 만 일자·모델별 requests 를 주는데 관리 키(management key) 필요** (`403 Only management keys can fetch activity`).

**할 일**: ① openrouter.ai/settings/keys 에서 management key 발급 → `.env` `OPENROUTER_MANAGEMENT_KEY` → `/health` 가 `/activity` 의 오늘 `:free` requests 합계를 60초 캐시로 표시(없으면 로컬 카운터 폴백). ② 로컬 카운터는 **성공 응답 후에만** `hit()`, 배지 문구 "약 N회(추정)".

## 결정 사항

- **검색은 Vane(구 Perplexica)** 로 간다. Vane 은 SearXNG+페이지 읽기+임베딩만 내장, **채팅 LLM 은 외부에서 붙여야 함**(검색 1회에 최소 2회 호출: 검색어 재작성 + 요약). → 무료 한도를 안 먹게 **Vane 의 채팅 LLM 은 로컬 Ollama 에 붙인다** (`vane_setup.py` 에 ollama 제공자 등록 추가 필요. 작은 모델이면 충분 — 우리 백엔드는 Vane 의 요약문은 버리고 sources 만 씀).
- **RAG(로컬 문서 `/ingest`)는 LlamaIndex** 로 간다 (PROJECT_CONTEXT 는 "필요해지면 검토"였음 → 확정). 미착수.

## 다음 세션 (Llama 70B 머신) 시작 순서

```bash
git clone https://github.com/tmdrb0130/mochang-bot.git && cd mochang-bot
cp .env.example .env            # OPENROUTER_API_KEY 입력 (+ 위 관리 키)
python -m venv .venv && .venv/Scripts/activate   # (Linux: source .venv/bin/activate)
pip install -r backend/requirements.txt -r requirements-dev.txt
python -m pytest -q             # 81 passed 확인
cd frontend && npm install && cd ..
# 서버: uvicorn backend.main:app --reload --port 8000 / 프론트: cd frontend && npm run dev
# Vane: docker run -d -p 3000:3000 -v vane-data:/home/vane/data --name vane itzcrazykns1337/vane:latest
```

작업 순서 제안:
1. `/intake` + `/intake/regenerate` 실호출 1회씩 → 멘토링 보기·재생성 품질 확인
2. `config.yaml llm` 을 로컬 Ollama(70B)로 전환 → 같은 입력으로 MiniMax 무료 vs Llama 70B 비교 (PROJECT_CONTEXT 로드맵 "2~3단계에서 반드시")
3. `vane_setup.py` 에 Ollama 제공자 등록 옵션(`--ollama http://host.docker.internal:11434 --model ...`) → `research.vane.enabled: true` → `/research` 1회로 `backend == "vane"` 확인, 통계 페이지(JS) 본문이 읽히는지
4. **조사 단계(조사 B)**: 문항 단위 `/research` → **주제 단위**(시장 통계 / 기존 서비스·경쟁 / 고객 페인포인트 / 유사 BM / 정책·규제 / 논문·보고서)로 검색어 1회 + 사실 추출 1~2회, 각 사실에 `use_for` 문항 태그. 프론트에 "조사 결과" 단계(사실 카드 체크 → 문항별 `references`)
5. **조사 기반 카드(조사 A)**: 인테이크 1차 판정 → customer/problem/alternative/revenue 슬롯은 조사 결과를 넣어 2차 카드 생성, 보기마다 `source` 표시. "조사해서 보기 만들기" 토글
6. 한도 카운터 수정(위 "할 일")
7. LlamaIndex `/ingest`

---

# PROGRESS — 2026-08-31 자율 작업 세션 보고

> 사용자가 자리를 비운 사이(약 2~3시간) 진행한 작업의 결과. 다음 세션은 맨 아래 "다음 세션 시작 명령"부터.

## 한눈에

| # | 항목 | 상태 | 커밋 |
|---|---|---|---|
| 1 | Vane 모델 설정 (브라우저 없이) | ✅ 완료 — API 로 등록, `/api/search` 실호출은 한도로 **생략** | `23e1751` |
| 2 | `backend/rag/research.py` search(query) 추상화 + ddgs 폴백 + 캐시 | ✅ | `8644700` |
| 3 | 조사 파이프라인 (검색어 → 검색 → 본문 → 사실 JSON → 참고자료 주입) | ✅ + 실호출 검증 | `d10f632` |
| 4 | prompts/ md 세트 · 하드코딩 프롬프트 → md 로더 | ✅ | `cd6346f` |
| 5 | `verify.py` 필수 요소 판정 | ✅ (실호출 미검증) | `e6285ef` |
| 6 | 동시성 층 (큐·워커·백오프·`/jobs`) + 부하 테스트 | ✅ | `e6ed0bb` |
| 7 | 테스트 + pytest 통과 | ✅ **69 passed** (모델·네트워크 호출 0) | `bbd6adf` |
| 8 | PROGRESS.md | ✅ 이 문서 | — |

무료 한도: 시작 시 48/50 → **남은 2회를 마지막 실호출에 사용**(조사 1회 + 생성 1회). 지금 **50/50 소진**, 한국시간 09:00 초기화.

---

## 1. Vane (구 Perplexica)

**결론: 브라우저 없이 설정 가능.** 설정은 컨테이너 볼륨 `/home/vane/data/config.json` 에 저장되고 HTTP API 로 다룰 수 있다. 컴파일된 라우트(`.next/server/app/api/*/route.js`)에서 확인한 형식:

| 엔드포인트 | 형식 |
|---|---|
| `GET /api/providers` | `{providers:[{id,name,chatModels:[{name,key}],embeddingModels}]}` |
| `POST /api/providers` | `{type:"openai", name, config:{apiKey, baseURL}}` |
| `POST /api/providers/{id}/models` | `{type:"chat"\|"embedding", key, name}` — **필수**: OpenAI 타입은 baseURL 이 api.openai.com 이 아니면 모델 목록이 비어 있음 |
| `POST /api/config/setup-complete` | 첫 실행 마법사 건너뛰기 |
| `POST /api/search` | `{chatModel:{providerId,key}, embeddingModel:{providerId,key}, sources:["web"], optimizationMode, query, stream:false}` → `{message, sources:[{pageContent, metadata:{title,url}}]}` |
| `GET /api/config` | `fields.modelProviders` 에 타입별 필드 정의. env: `OPENAI_API_KEY`, `OPENAI_BASE_URL`, `SEARXNG_API_URL` |

- 스크립트 `python -m backend.rag.vane_setup` 이 OpenRouter 제공자(`1722fce3-…`) + `minimax/minimax-m3:free` 채팅 모델을 등록했고, 임베딩은 내장 Transformers(`Xenova/nomic-embed-text-v1`, 로컬·무료). `config.yaml research.vane.*` 에 id 기입.
- **`/api/search` 실호출은 하지 않았다** — Vane 은 검색 1회에 자체 모델 호출(질의 분류·답변)을 여러 번 하므로 남은 2회 안에 못 들어감. `research.vane.enabled: false` 로 두고 ddgs 만 사용 중.
- 컨테이너 로그에 SearXNG 의 wikidata 엔진 403 경고가 있지만 검색엔 영향 없음.

## 2~3. 조사 파이프라인 — 실호출 결과 (`docs/reports/2026-08-31-real-check.json`)

검색어 3개(수동) → ddgs 18건 → 정부·언론 우선 랭킹 → 6페이지 본문 → 사실 추출 **1회 호출 12초** → 원문 대조 통과 **2건**:
- "배달의민족 한그릇 서비스 출시 5개월 만에 월 주문량 12배, 누적 2,250만 건" (쿡앤셰프, 2026 외식업 트렌드)
- "건강 라벨 활용 점포 2년간 최대 9배 증가" (같은 출처)

관찰:
- **정부 통계 페이지(자원순환정보시스템, 서울시 통계)는 본문이 106~128자만 추출됨** — JS 렌더링/표 구조라 trafilatura 가 못 읽음. 통계는 스니펫 수준만 남아 사실이 안 나옴. → 개선 후보: KOSIS/공공데이터 API 직접 연결, 또는 Vane(Playwright 내장) 경로 사용.
- 뉴스 사이트는 잘 읽힘(2,773자).
- 캐시 동작: 같은 (아이디어, 문항) 재요청은 모델 호출 0회.

## 3. 생성 경로 — 실호출 결과

`/generate` Q2 스토리형 + 위 2개 사실을 `references` 로 주입 → **1,967자 / 19초**, 본문에 `(출처: 쿡앤셰프)` 인용 포함 → 참고자료 주입·출처 표기 규칙 **동작 확인**.

⚠️ 같은 글에 **지어낸 개인 사실**이 여전히 있음: "저 또한 자취 3년 차입니다", "새벽 다섯 시면 어묵 튀김 냄새" (입력에 없음). 웹 사실은 통제됐지만 지원자 개인 서사는 스토리 스타일이 채운다. → `verify.py` 의 `unsupported_claims` 가 잡도록 설계됐으나 실호출 미검증(한도). **다음 세션 첫 실호출 후보.**

## 4~5. 프롬프트 md · 검증

- 새 md: `short_question.md`(Q1·Q10 한 문장 규칙), `sections.md`(섹션 머리말), `verify.md`, `research/queries.md`, `research/extract_facts.md`. 코드에 프롬프트 문구 없음(`assemble.render()`, `section_headers()`).
- `tests/test_prompts.py` 가 9문항 md 의 4절 구조·5절 섹션([문항 의도][필수 요소][권장 요소][피할 것])을 강제.
- `verify.py`: [필수 요소] 불릿을 체크리스트로 → 모델 JSON → **evidence 가 글 원문에 없으면 present=false 로 뒤집음**, `unsupported_claims` 도 글에 있는 문장만 인정, 글자수·마크다운·이모지 로컬 검사. `POST /verify`.

## 6. 동시성 층 — 부하 테스트 결과

`scripts/load_test.py` (가짜 모델 0.05~0.2초, ASGI 인메모리):
- 동기 `/generate` 동시 50 → 성공 50, **LLM 동시 실행 최대 2 = max_workers** ✅
- `/jobs/generate` 제출 50 → 대기 순번 최대 49 표시, 폴링으로 50 완료, 동시 실행 최대 2 ✅
- 발견·수정한 결함 2개: (a) 외부 작업이 워커 안에서 다시 `queue.run()` 하면 **교착** → 워커 컨텍스트에선 즉시 실행; (b) `stop()` 후 다른 이벤트 루프에서 `start()` 하면 워커가 조용히 죽음 → 큐 재생성.
- 429 백오프: 2s→4s→8s(+지터, 상한 30s), `retry.attempts: 3`. `TransientError`(504 aborted)는 기존처럼 다른 모델로 전환.

---

## 내가(사용자) 직접 해야 하는 일

1. (완료) 커밋 9개 모두 `origin/main` 에 푸시됨 — `git log origin/main --oneline` 으로 확인
2. **Vane 실검색 검증** (한도 초기화 후): `config.yaml research.vane.enabled: true` → `POST /research` 1회 → `backend` 가 `vane` 으로 나오는지. Vane 이 몇 회의 모델 호출을 쓰는지 `/health.usage` 로 측정해 두기.
3. **`/verify` 실호출 1회**: 위 Q2 결과(`docs/reports/…json` 의 `B_generate.text`)를 넣어 `unsupported_claims` 에 "자취 3년 차"가 잡히는지.
4. SearXNG 한국어 엔진(네이버) 켜기 — Vane UI(`localhost:3000`) 또는 컨테이너 안 `searxng/settings.yml`. 브라우저 필요할 수 있음.
5. Docker Desktop 은 켜져 있음(제가 켰음). 안 쓰면 종료.

## 결정 필요

| 항목 | 내가 택한 기본값 | 대안 |
|---|---|---|
| `research.vane.enabled` | **false** (ddgs 만) — Vane 은 검색마다 자체 모델 호출로 무료 한도 소모 | 한도 해결($10 충전 또는 로컬 모델) 후 true |
| `max_workers` | **2** (무료 20회/분 보호) | 유료/로컬이면 4~8 |
| `/jobs` 대기 순번 의미 | 큐에 앞선 **미시작 작업 수**(LLM 호출 단위 아님) | 예상 대기 시간(초)으로 환산해 표시 |
| 조사 실행 시점 | 별도 `/research` 호출(프론트 미연결) | 인테이크 직후 자동 실행(문항당 모델 2회 추가 → 한 번 생성 = 27회, 무료 한도에 부적합) |
| 통계 페이지 추출 실패 | 그대로(스니펫만) | KOSIS/공공데이터 API 어댑터 추가 |
| 일일 카운터 | 파일 기반, **다중 프로세스에서 경합**(CLI 와 서버가 동시에 쓰면 1회 누락 가능) | SQLite 또는 서버만 카운트 |
| ddgs 지역 | `kr-kr`, text+news | 뉴스만 / 도메인 화이트리스트 |

## 알려진 이슈 / 미구현

- 프론트 미연결: `/research` 결과 표시·참고자료 선택, `/verify` 결과(누락·근거 없는 문장) 하이라이트, `/jobs` 폴링 전환. 백엔드만 있음.
- 스토리형 생성이 지원자 개인 서사를 지어냄(위 3절). system.md 규칙 강화 + verify UI 가 다음 단계.
- 인테이크 결과 저장 없음(새로고침 시 유실).
- `.usage.json` 카운터 다중 프로세스 경합.
- Vane 컨테이너의 SearXNG 는 기본 엔진 구성(한국어 약함).

## 다음 세션 시작 명령

```bash
# 1) 상태 확인
git status && git log --oneline -3
docker ps --filter name=vane          # Vane 컨테이너 (없으면 README "웹 조사" 절)

# 2) 서버
.venv\Scripts\python -m uvicorn backend.main:app --port 8000      # 터미널 1
cd frontend && npm run dev                                          # 터미널 2 → http://localhost:5173

# 3) 테스트 (모델 호출 없음)
.venv\Scripts\python -m pytest -q
.venv\Scripts\python -m scripts.load_test --n 50 --jobs

# 4) 한도 확인 후 실호출 (09:00 KST 초기화)
curl http://localhost:8000/health
```

첫 작업 추천 순서: ① `/verify` 실호출로 unsupported_claims 확인 → ② 프론트에 검증 결과(노란 문장) 표시 → ③ `/research` 를 인테이크 뒤 선택 실행으로 연결 → ④ Vane 실검색 1회 측정.
