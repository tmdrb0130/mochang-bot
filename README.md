# 모창봇 — 모두의 창업 신청서 자동 작성기

중소벤처기업부 **「모두의 창업 프로젝트」**(modoo.or.kr) 도전신청서를, 창업 아이디어 **한 문단**만 입력하면 문항별로 자동 작성해 주는 도구입니다.

> 아이디어 입력 → (설명이 짧으면) AI가 만든 보기 카드 몇 장 고르기 → 문항별 초안 생성 → 읽고 고르고 다듬기 → 복사해서 modoo.or.kr에 붙여넣기

modoo.or.kr은 자동 접근이 차단되어 있어 폼 자동 입력은 하지 않고, **복사·붙여넣기 방식**으로 동작합니다.

---

## 무엇을 만들어 주나

도전신청서 「02. 아이디어 작성」의 서술형 문항 전부를 생성합니다.

| 문항 | 내용 | 글자 제한 |
|---|---|---|
| Q1 | 아이디어 한줄 소개 | 100자 |
| Q2 | 아이디어를 떠올린 배경 이야기 | 2000자 |
| Q3-1 | 기존 제품·서비스 대비 차별점, 해결 문제 | 2000자 |
| Q3-2 | 수익 창출 계획 | 2000자 |
| Q4-1 | 사업화 계획 | 2000자 |
| Q4-2 | 책임멘토에게 받고 싶은 도움 | 2000자 |
| Q7-1 | (사업자만) 기존 사업과 이번 아이디어의 차이 | 2000자 |
| Q8 | 사업화할 수 있는 본인 역량 | 2000자 |
| Q10 | 대중 공개용 아이디어 자랑 — **공개/비공개를 먼저 선택**, 비공개면 AI 호출 없이 안내 문장 | 100자 |

Q6(사업 분야)은 AI가 추천하지 않고 modoo.or.kr 과 같은 선택지에서 **사람이 직접 고릅니다**.

- 글 스타일 3종: **스토리텔링형 / 논리·근거형 / 간결·실무형** — 원하는 것만 골라 생성
- 트랙 2종: **일반/기술 분야 / 로컬 분야** — 트랙별 강조점이 프롬프트에 반영
- "내용 더 보태기"(이어쓰기), "새로 생성", 글자수 게이지, 문항별/전체 복사

---

## 빠른 시작 (클론 → 5분)

### 필요한 것

| 항목 | 버전 | 비고 |
|---|---|---|
| Python | 3.10 이상 | 백엔드 |
| Node.js | 18 이상 | 프론트엔드 |
| OpenRouter API 키 | — | https://openrouter.ai/keys 에서 무료 발급. **무료 모델만 쓰므로 결제 없이 동작** |

### 1. 클론 후 API 키 설정

```bash
git clone https://github.com/tmdrb0130/mochang-bot.git
cd mochang-bot
cp .env.example .env        # Windows PowerShell: Copy-Item .env.example .env
```

`.env` 파일을 열어 키를 넣습니다.

```
OPENROUTER_API_KEY=sk-or-v1-여기에_발급받은_키
```

### 2. 백엔드 실행 (터미널 1)

```bash
# 가상환경
python -m venv .venv
.venv\Scripts\activate          # Windows
source .venv/bin/activate       # macOS / Linux

pip install -r backend/requirements.txt
uvicorn backend.main:app --port 8000
```

브라우저에서 http://localhost:8000/health 를 열어 `{"ok":true, "model": ...}` 가 보이면 성공입니다.

### 3. 프론트엔드 실행 (터미널 2)

```bash
cd frontend
npm install
npm run dev
```

http://localhost:5173 을 열면 화면 상단에 `모델: minimax/minimax-m3:free` 가 표시됩니다.
표시되지 않고 빨간 안내가 뜨면 백엔드(2번)가 꺼져 있는 것입니다.

### 4. 써보기

1. 트랙 선택 → 아이디어 입력(10자 이상) → (있으면) 경력·경험 입력 → 사업 분야(Q6)·Q10 공개 여부·스타일 선택
2. **신청서 초안 만들기** 클릭 → AI가 아이디어를 읽습니다(30~60초)
   - 설명이 충분하면 바로 초안 생성으로 넘어갑니다
   - 부족한 부분이 있으면 **"정보 보충"** 카드가 최대 6장 나옵니다 — 가장 가까운 보기를 클릭, 또는 "기타" 한 줄, 또는 "모르겠어요"(지어내지 않고 가정으로 씀). "나머지 건너뛰고 바로 만들기"도 됩니다
3. 문항당 25~40초. 완성된 문항부터 읽고, 스타일 탭에서 고르고, 직접 고쳐도 됩니다. 문항 아래 **"정보 보태기"**를 열면 그 문항에 들어갈 정보를 고친 뒤 새로 생성·이어쓰기할 수 있습니다.
4. **제출용으로 정리** → 문항별 복사 → modoo.or.kr 도전하기 화면에 붙여넣기

> 무료 모델은 요청 제한(429)이 종종 걸립니다. 문항이 빨간 "다시 생성"으로 남으면 몇 초 뒤 다시 누르세요.
> 기본 스타일이 1개(스토리텔링형)로 설정된 것도 무료 티어 요청 수를 아끼기 위해서입니다.

화면 상단에서 **모델을 고를 수 있고**, 오늘 무료 요청을 몇 회 썼는지 배지로 보입니다.

---

## OpenRouter 무료 한도와 대응

[OpenRouter 공식 문서](https://openrouter.ai/docs/api-reference/limits) 기준 (2026-08-31 확인):

| 항목 | 한도 |
|---|---|
| 분당 요청 | **20회** (모든 `:free` 모델 공통) |
| 하루 요청 — 누적 결제 $10 미만 | **50회** |
| 하루 요청 — 누적 결제 $10 이상 | **1000회** (모델은 계속 무료) |
| 리셋 | UTC 00:00 = 한국 09:00 |

문항 9개 × 스타일 1개 = **한 번 생성에 9회** (Q10 비공개면 8회) → 무료 계정은 하루 5번 정도 돌릴 수 있습니다.

이 프로젝트가 하는 것:
- **자동 폴백** (`config.yaml` `fallback: true`): 고른 모델이 429/다운이면 OpenRouter가 목록의 다른 무료 모델로 넘김. 실제 응답 모델은 문항 아래 "(폴백)"으로 표시
- **요청 수 카운트**: 백엔드가 `backend/.usage.json`에 하루 요청 수를 세어 헤더 배지에 표시 (10회 이하 노랑, 0 빨강). OpenRouter API가 무료 요청 잔여량을 알려주지 않아 직접 셉니다
- **한도 도달 시 안내**: 429가 오면 "리셋 시각 / $10 충전 / 로컬 모델 전환" 중 해야 할 일을 오류 메시지에 담아 보냄

한도에 걸리면:
1. 다른 모델 선택 → 대개 폴백이 이미 시도했으므로 **몇 분 기다리기** (분당 20회 초과인 경우)
2. 하루 50회를 다 썼으면 **한국 시간 09:00**에 초기화
3. 자주 쓰면 [OpenRouter에 $10 충전](https://openrouter.ai/credits) → 하루 1000회. 충전 후 `config.yaml`의 `daily_request_limit: 1000`으로 수정
4. 여러 사람이 쓰는 단계면 로컬 모델(Ollama/vLLM)로 전환 — 아래 "모델 바꾸기"

---

## 프로젝트 구조

```
mochang-bot/
├── backend/                    # Python · FastAPI
│   ├── main.py                 #   API: /health /models /generate /extend /generate/dry-run
│   ├── config.yaml             #   LLM 연결 설정 (base_url / api_key / model) — 여기만 바꾸면 모델 교체
│   ├── llm/client.py           #   OpenAI 호환 클라이언트 (OpenRouter · Ollama · vLLM · LiteLLM 공용)
│   ├── pipeline/
│   │   ├── assemble.py         #   프롬프트 조립: system + track + question + style + 지원자 입력
│   │   ├── generate.py         #   생성 · 이어쓰기 · 후처리(글자수 절단, 마크다운 제거)
│   │   └── intake.py           #   인테이크 결과 파싱·정규화·충분도(ready) 판정
│   ├── prompts/                #   프롬프트는 전부 md 파일 (코드에 하드코딩 없음)
│   │   ├── system.md           #     페르소나 · 공통 규칙 (1인칭, 허위 금지, 마크다운 금지 …)
│   │   ├── tracks/             #     tech.md · local.md
│   │   ├── questions/          #     q1.md … q10.md — 문항 의도 · 필수/권장 요소 · 피할 것
│   │   ├── styles/             #     story.md · logic.md · plain.md
│   │   ├── extend.md           #     이어쓰기 지시
│   │   └── intake.md           #     아이디어 읽기: 슬롯 판정 + 보기 카드 생성 규칙
│   └── test_generate.py        #   CLI 테스트 도구 (아래 참고)
├── frontend/                   # Vite · React 19 · Tailwind v4
│   └── src/
│       ├── App.jsx             #   4단계 UI (입력 → 정보 보충 카드 → 초안 고르기 → 제출용 정리)
│       └── api.js              #   백엔드 호출 (주소: VITE_API_BASE, 기본 http://localhost:8000)
├── docs/PROJECT_CONTEXT.md     # 설계 결정 · 문항 분석 · 진행 상황 인수인계 문서 (자세한 건 여기)
├── .env.example                # OPENROUTER_API_KEY 자리
└── .gitignore
```

**동작 흐름**

```
브라우저(App.jsx) ──POST /generate──▶ FastAPI(main.py)
                                        │ assemble.build_prompts()  ← prompts/*.md + 지원자 입력
                                        │ LLMClient.complete()      ← config.yaml (OpenRouter 등)
                                        │ generate.postprocess()    ← 글자수·마크다운 정리
                                        ▼
                                  { text, length, limit }
```

---

## 모델 바꾸기

`backend/config.yaml` 의 세 값만 바꾸면 **OpenAI 호환 API를 제공하는 어떤 서버**든 붙습니다.

```yaml
llm:
  base_url: "https://openrouter.ai/api/v1"
  api_key: "${OPENROUTER_API_KEY}"      # ${환경변수} 형식 → .env 에서 읽음
  model: "minimax/minimax-m3:free"
```

| 대상 | base_url | api_key | model 예 |
|---|---|---|---|
| OpenRouter (기본) | `https://openrouter.ai/api/v1` | `${OPENROUTER_API_KEY}` | `minimax/minimax-m3:free` |
| 로컬 Ollama | `http://localhost:11434/v1` | `ollama` | `llama3.3:70b`, `gemma3:27b` |
| vLLM 서버 | `http://<서버IP>:8000/v1` | `dummy` | 서빙 중인 모델명 |
| Claude (LiteLLM 프록시) | `http://localhost:4000/v1` | LiteLLM 키 | `claude-sonnet-4-6` |

화면에서 고를 수 있는 모델 목록은 `config.yaml` 의 `models:` 에 있습니다. 항목을 추가/삭제하면 드롭다운과 폴백 순서에 바로 반영됩니다.
OpenRouter 에서는 `extra: { reasoning: { enabled: false } }` 가 기본으로 켜져 있어 추론(thinking) 모델도 본문이 비지 않습니다. Ollama/vLLM 으로 바꾸면 `extra` 를 비우세요.

---

## 개발용 CLI

프론트 없이 백엔드만 확인할 때 씁니다 (프로젝트 루트에서).

```bash
# 조립된 프롬프트만 출력 (LLM 호출 없음, 키 불필요)
python -m backend.test_generate q2
python -m backend.test_generate q3_1 --style logic --track local

# 실제 생성
python -m backend.test_generate q2 --call

# config 안 바꾸고 모델만 바꿔서 비교
python -m backend.test_generate q1 --call --model z-ai/glm-5.2:free
```

API 문서는 백엔드 실행 후 http://localhost:8000/docs (Swagger) 에서 볼 수 있습니다.

| 엔드포인트 | 역할 |
|---|---|
| `GET /health` | 연결 상태 · 기본 모델 · 오늘 요청 수/한도 |
| `GET /models` | 선택 가능한 모델 목록 |
| `POST /intake` | 아이디어 읽기: 슬롯별 확인/부족 판정 + 부족한 슬롯의 보기 카드 생성 (`ready`면 카드 생략) |
| `POST /generate` | 문항 하나 생성 (`model`, `answers`[카드 답변] 선택) |
| `POST /extend` | 기존 글 뒤에 새 근거·사례만 이어쓰기 |
| `POST /generate/dry-run` | 조립된 프롬프트만 반환 (프롬프트 튜닝용) |

---

## 자주 겪는 문제

| 증상 | 원인 · 해결 |
|---|---|
| 프론트 상단에 "백엔드에 연결할 수 없어요" | 백엔드가 안 켜져 있음. 터미널 1에서 `uvicorn backend.main:app --port 8000` |
| `환경변수 OPENROUTER_API_KEY 가 없습니다` | 루트에 `.env` 가 없거나 이름이 다름. `.env.example` 을 복사해 키 입력 |
| 생성 실패 → "다시 생성" | 무료 모델 요청 제한(429). 오류 메시지에 이유가 적혀 있음. 몇 초 후 재시도, 또는 상단에서 다른 모델 선택 |
| 헤더 배지가 "한도 도달" | 하루 50회 소진. 한국 시간 09:00 리셋. 위 "OpenRouter 무료 한도와 대응" 참고 |
| `업스트림 오류 504 'The operation was aborted'` | 무료 제공자가 생성 도중 끊음 (OpenRouter 가 HTTP 200 안에 error 로 돌려줌). 백엔드가 다른 모델로 최대 3회 자동 재시도하며, 그래도 실패하면 잠시 후 "다시 생성" |
| 문항당 1분 넘게 걸림 | 무료 풀 혼잡. 동시 요청 수는 `config.yaml` 의 `concurrency` 로 조절 |
| 문항 아래 "(폴백)" 표시 | 고른 모델이 막혀 다른 모델이 대신 썼다는 뜻. 품질이 다르면 "새로 생성" |
| 백엔드를 다른 PC에 둠 | `frontend/.env` 에 `VITE_API_BASE=http://<그 PC IP>:8000` |

---

## 현재 상태와 로드맵

- [x] 프롬프트 md 세트 (9문항 × 3스타일 × 2트랙) + 조립기 + 후처리
- [x] FastAPI 백엔드 (`/generate` `/extend` `/models`)
- [x] OpenRouter 무료 모델로 실호출 검증
- [x] React 프론트 백엔드 연결
- [ ] 생성문 검증 — 입력에 없는 사실을 지어낸 문장 표시 및 수정 흐름
- [x] 아이디어 구조화 → 부족한 항목만 "보기 + 모르겠어요" 카드로 묻는 입력 UX, 문항별 "정보 보태기"
- [ ] 로컬 문서 RAG (`/ingest`), 웹 검색 근거 자료
- [ ] 로컬 70B 모델(Ollama/vLLM) 전환 및 모델 품질 비교

설계 배경, 문항별 필수 요소 분석, 결정 사항은 [`docs/PROJECT_CONTEXT.md`](docs/PROJECT_CONTEXT.md) 에 있습니다.

---

## 주의

- 초안의 **수치·사례는 반드시 본인이 확인한 사실로 바꿔 넣으세요.** 심사에서 사실 확인이 있을 수 있습니다.
- `.env` 는 절대 커밋하지 마세요 (`.gitignore` 에 포함되어 있음).
- 이 도구는 신청서 작성을 돕는 초안 생성기이며, 모두의 창업 프로젝트 주관기관과 무관합니다.
