# 모창봇 — 모두의 창업 신청서 자동 작성기

중소벤처기업부 **「모두의 창업 프로젝트」**(modoo.or.kr) 도전신청서를, 창업 아이디어 **한 문단**만 입력하면 문항별로 자동 작성해 주는 도구입니다.

> 아이디어 입력 → AI가 문항별 초안을 여러 글 스타일로 생성 → 읽고 고르고 다듬기 → 복사해서 modoo.or.kr에 붙여넣기

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
| Q10 | 대중 공개용 아이디어 자랑 | 100자 |
| Q6 | 사업 분야 추천 (선택지 중 하나 + 이유) | — |

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

1. 트랙 선택 → 아이디어 30자 이상 입력 → (있으면) 경력·경험 입력 → 스타일 선택
2. **초안 N개 만들기** 클릭
3. 문항당 25~40초. 완성된 문항부터 읽고, 스타일 탭에서 고르고, 직접 고쳐도 됩니다.
4. **제출용으로 정리** → 문항별 복사 → modoo.or.kr 도전하기 화면에 붙여넣기

> 무료 모델은 요청 제한(429)이 종종 걸립니다. 문항이 빨간 "다시 생성"으로 남으면 몇 초 뒤 다시 누르세요.
> 기본 스타일이 1개(스토리텔링형)로 설정된 것도 무료 티어 요청 수를 아끼기 위해서입니다.

---

## 프로젝트 구조

```
mochang-bot/
├── backend/                    # Python · FastAPI
│   ├── main.py                 #   API: /health /generate /extend /recommend-field /generate/dry-run
│   ├── config.yaml             #   LLM 연결 설정 (base_url / api_key / model) — 여기만 바꾸면 모델 교체
│   ├── llm/client.py           #   OpenAI 호환 클라이언트 (OpenRouter · Ollama · vLLM · LiteLLM 공용)
│   ├── pipeline/
│   │   ├── assemble.py         #   프롬프트 조립: system + track + question + style + 지원자 입력
│   │   └── generate.py         #   생성 · 이어쓰기 · Q6 추천 · 후처리(글자수 절단, 마크다운 제거)
│   ├── prompts/                #   프롬프트는 전부 md 파일 (코드에 하드코딩 없음)
│   │   ├── system.md           #     페르소나 · 공통 규칙 (1인칭, 허위 금지, 마크다운 금지 …)
│   │   ├── tracks/             #     tech.md · local.md
│   │   ├── questions/          #     q1.md … q10.md — 문항 의도 · 필수/권장 요소 · 피할 것
│   │   ├── styles/             #     story.md · logic.md · plain.md
│   │   ├── extend.md           #     이어쓰기 지시
│   │   └── recommend_field.md  #     Q6 분야 추천 지시
│   └── test_generate.py        #   CLI 테스트 도구 (아래 참고)
├── frontend/                   # Vite · React 19 · Tailwind v4
│   └── src/
│       ├── App.jsx             #   3단계 UI (입력 → 초안 고르기 → 제출용 정리)
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

OpenRouter 무료 모델 테스트 결과(2026-08-31)는 `config.yaml` 주석에 정리돼 있습니다.
추론(thinking) 모델은 `extra: { reasoning: { enabled: false } }` 를 켜지 않으면 본문이 비어서 나옵니다.

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
| `GET /health` | 연결 상태 · 현재 모델 |
| `POST /generate` | 문항 하나 생성 |
| `POST /extend` | 기존 글 뒤에 새 근거·사례만 이어쓰기 |
| `POST /recommend-field` | Q6 사업 분야 추천 (JSON) |
| `POST /generate/dry-run` | 조립된 프롬프트만 반환 (프롬프트 튜닝용) |

---

## 자주 겪는 문제

| 증상 | 원인 · 해결 |
|---|---|
| 프론트 상단에 "백엔드에 연결할 수 없어요" | 백엔드가 안 켜져 있음. 터미널 1에서 `uvicorn backend.main:app --port 8000` |
| `환경변수 OPENROUTER_API_KEY 가 없습니다` | 루트에 `.env` 가 없거나 이름이 다름. `.env.example` 을 복사해 키 입력 |
| 생성 실패 → "다시 생성" | 무료 모델 요청 제한(429). 몇 초 후 재시도. 계속되면 `config.yaml` 에서 다른 무료 모델로 교체 |
| 문항당 1분 넘게 걸림 | 무료 풀 혼잡. 동시 요청 수는 `config.yaml` 의 `concurrency` 로 조절 |
| 본문이 비어서 옴 | 추론 모델 사용 중. `config.yaml` 의 `extra.reasoning.enabled: false` 주석 해제 |
| 백엔드를 다른 PC에 둠 | `frontend/.env` 에 `VITE_API_BASE=http://<그 PC IP>:8000` |

---

## 현재 상태와 로드맵

- [x] 프롬프트 md 세트 (9문항 × 3스타일 × 2트랙) + 조립기 + 후처리
- [x] FastAPI 백엔드 (`/generate` `/extend` `/recommend-field`)
- [x] OpenRouter 무료 모델로 실호출 검증
- [x] React 프론트 백엔드 연결
- [ ] 생성문 검증 — 입력에 없는 사실을 지어낸 문장 표시 및 수정 흐름
- [ ] 아이디어 구조화(사실 카드) → 빠진 항목만 "보기 + 모르겠어요"로 묻는 입력 UX
- [ ] 로컬 문서 RAG (`/ingest`), 웹 검색 근거 자료
- [ ] 로컬 70B 모델(Ollama/vLLM) 전환 및 모델 품질 비교

설계 배경, 문항별 필수 요소 분석, 결정 사항은 [`docs/PROJECT_CONTEXT.md`](docs/PROJECT_CONTEXT.md) 에 있습니다.

---

## 주의

- 초안의 **수치·사례는 반드시 본인이 확인한 사실로 바꿔 넣으세요.** 심사에서 사실 확인이 있을 수 있습니다.
- `.env` 는 절대 커밋하지 마세요 (`.gitignore` 에 포함되어 있음).
- 이 도구는 신청서 작성을 돕는 초안 생성기이며, 모두의 창업 프로젝트 주관기관과 무관합니다.
