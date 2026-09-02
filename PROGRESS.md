# PROGRESS — 2026-09-02 저녁 (아이디어·초안 DB 저장, 작업 40)

> 이 절이 최신. 같은 날 낮·전날 기록은 아래에 그대로 둠.

## 한 것

사용자가 입력한 아이디어(+인테이크 답)와 문항별 생성 초안을 **서버 DB 에 저장**하기 시작했다. 지금까지는 브라우저 localStorage 에만
있었고 서버는 아무것도 남기지 않았다 (작업 결과는 메모리 큐에 500건만 잠깐).

- `backend/storage.py` — SQLAlchemy Core. 테이블 `drafts`(초안 한 벌: 아이디어·트랙·팀·역량·인테이크 답 JSON·IP·요청 수)
  + `generations`(문항별 생성문: question_id·kind generate/extend·text·글자수·모델·meta JSON) + `schema_meta`(버전).
  기본 **SQLite WAL** `backend/.data/mochang.sqlite`(gitignore). `config.yaml storage.url` / 환경변수 `MOCHANG_DATABASE_URL` 에
  PostgreSQL URL 을 주면 코드 수정 없이 전환 (수백 명 규모 대비 — 사용자 지시).
- 연결: `main.py _persisted()` — `/jobs/{kind}` 전부 + 동기 `/intake`·`/generate`·`/extend`. 결과가 나온 뒤 스레드에서 저장하고
  결과는 그대로 반환. **저장 실패는 삼킨다**(timing.py 와 같은 원칙) — DB 를 못 열면 서비스는 뜨고 저장만 꺼진다.
- 묶는 키 `draft_id`: 요청 스키마에 선택 필드로 추가. 없으면 `track|idea` 해시로 묶는다 → 구 프론트로도 동작.
- **프론트** (`api.js newDraftId`·`toPayload draft_id`, `App.jsx withDraft`): form 에 `draftId`·`draftIdea` 를 두고 모든 요청에 실어 보낸다.
  **아이디어를 조금 고치면 같은 초안, 완전히 다르면 새 초안** (사용자 규칙) — 글자 2-gram 겹침 비율(공통 ÷ 짧은 쪽) 0.5 문턱.
  실측: 오타 수정 0.95 · 문장 덧붙임 1.00 · 절반 삭제 1.00 → 유지 / 다른 아이디어 0.19·0.12·0.06 → 새 id. 기준 글은 id 생성 시점과
  인테이크 시작 시점의 아이디어. 옛 localStorage 저장본은 `migrate()` 가 id 를 부여. "처음부터" 는 저장본을 지우므로 자연히 새 id.
- `GET /drafts/{draft_id}` 조회(초안 + 생성 이력). 목록 API 는 일부러 안 둠(인증 없음). 운영자는 `scripts/drafts_report.py`
  (`--last N` / `--id` / `--dump out.jsonl`).
- 테스트 `tests/test_storage.py` 6건 (upsert·동시 첫 요청 9건 경쟁·실패 삼킴·설정 우선순위·/jobs→DB→/drafts 한 바퀴).
  `conftest.py` 가 `MOCHANG_DATABASE_URL` 을 임시 파일로 돌려 운영 DB 오염 방지. **390 passed**.

## 실호출 검증 (2026-09-02 20:2x, 공개 서비스 `bustartup.kr:50001` — 재시작·dist 배포 후)

프론트 `withDraft` 규칙을 파이썬으로 옮긴 스크립트로 실제 흐름(인테이크 → 카드별 답 4종 → 문항 3개 동시 생성 → 아이디어 수정 → 생성 1건 → `GET /drafts`)을 3종 아이디어로 돌렸다.

| 시나리오 | 카드 | 생성 | 수정 방식 | 유사도 | 판정 | DB |
|---|---|---|---|---|---|---|
| A 반찬 꾸러미 (비사업자·팀원 없음·웹개발) | 5장 | q1·q2·q3_1 | 오타 수정 + 문장 덧붙임 | 0.94 | **유지** ✓ | 요청 5·생성 4·답 5·idea 갱신됨 |
| B 딸기 농장 체험 (사업자·팀 2명·농업 경영) | 5장 | q1·q2·q7_1 | 헬스장 예약으로 **전부 교체** | 0.11 | **새 초안** ✓ | 원 초안 생성 3 그대로, 새 초안 생성 1 |
| C 경단녀 돌봄 매칭 (팀 3명 이상·보육교사) | 3장 | q1·q3_2·q8 | 절반으로 줄임 | 1.00 | **유지** ✓ | 요청 5·생성 4·답 3 |

- 동시 생성 3건이 같은 초안의 첫 요청으로 겹쳐도(insert 경쟁) 한 행으로 모였다. 인테이크 답(첫 보기·둘째 보기+직접 문장·모름·직접 입력) 그대로 저장됨. IP 기록됨.
- 구 프론트 호환: `draft_id` 없는 요청 → `h…` 해시 초안으로 저장·조회 200. 없는 id → 404.
- 로컬 `scripts/drafts_report.py --last 10` 과 공개 `GET /api/drafts/{id}` 가 같은 내용. 총 5초안 13문항, 실패 0.
- 스크립트: 세션 scratch `live_draft_test.py` (리포 밖). 보는 도구: DB Browser for SQLite 설치(사용자 결정 A — MariaDB 로 옮기지 않음).

## 결정

- **저장 사실을 사용자에게 고지하지 않는다** (사용자, 9/2 저녁): 동의 단계가 UX 를 늘려 이탈률을 높인다. `config.yaml storage` 주석에 기록.
- DB 는 SQLite 로 시작, PostgreSQL 은 URL 교체로. 근거: 쓰기량이 문항당 1행(초당 1건 미만)이라 병목은 DB 가 아니라 GPU(동시 42) — 수백 명도 SQLite WAL 이 충분.
  다만 서버를 2대 이상으로 늘리는 순간 파일 DB 는 공유가 안 되므로 그때 PostgreSQL.

## 같은 저녁 추가 2건 (사용자 실사용 중 발견)

**① 서버 크래시 → "조사 실패 (502)"** — 20:34:36 사용자의 인테이크 조사 중 `python.exe` 가 `etree.cp311-win_amd64.pyd`(lxml 6.1.2) 액세스 위반
(0xc0000005)으로 죽음. 이벤트 로그상 **오늘 처음**. NSSM 이 5초 뒤 자동 재시작했고 그 사이 `/research` 가 연결 거부 → nginx 502.
파이썬 예외가 아니라 잡을 수 없고, 추출이 스레드 여러 개(EXTRACT_CONCURRENCY 3 × 조사 워커 6)에서 동시에 돌던 상황.
- 처방: `backend/rag/extract_proc.py` — trafilatura 추출을 **spawn 프로세스 풀(3)** 에서 돌린다. 워커가 죽으면 그 페이지만 `""`, 풀 재생성,
  API·큐·진행 중 작업은 살아남는다. 페이지당 30초 상한. `MOCHANG_EXTRACT_ISOLATION=0` 이면 예전 스레드 방식(테스트 기본).
- 검증: `tests/test_extract_proc.py` — 워커를 `os._exit(1)` 로 죽여도 예외 없이 `""` 반환 + 다음 호출 정상(실제 spawn). 운영 조건 스모크: 첫 호출(풀 기동) 수 초, 이후 수십 ms.
- 근본 원인(lxml 스레드 안전성 vs 특정 페이지)은 미확정 — 재발하면 `C:\logs\mochang-api.err*.log` 와 Application 이벤트 로그(모듈명) 확인.

**② 배포해도 열어 둔 탭에 새 기능이 안 먹음** (사용자: "사람이 새로고침 해야만 적용되는 건 이상하다") — `App.jsx` 에 새 배포 감지 추가:
1분마다·탭이 다시 보일 때 `/` 를 `no-store` 로 받아 번들 이름(`index-XXXX.js`)이 지금 것과 다르면, 생성·인테이크가 안 도는 순간에 새로고침.
초안은 localStorage, 진행 작업은 sessionStorage 라 잃는 것 없음. **이번 배포부터 적용** — 지금 열려 있는 탭은 한 번은 손으로 새로고침해야 한다.

`pytest` **392 passed**, dist-check 통과.

## 같은 밤 — Qwen3.8-27B-FP8 을 GPU2 에 올리고 라마와 A/B (사용자 지시 20:4x)

**배포** (`k8s/vllm-qwen-deployment.yaml`, 파드 `vllm-qwen`, NodePort 30801, 이 PC 터널 30801):
- 모델 `Qwen/Qwen3.8-27B-FP8` (HF 2026-08-14, 31 GB, 구조 Qwen3_5ForConditionalGeneration). vLLM 0.25.0(서버 기존 이미지) 지원 확인.
- **서버→huggingface.co 차단(20 B/s)**, hf-mirror.com·ModelScope 4.5 MB/s. 파드 안 huggingface_hub 는 미러를 거부(FileMetadataError)
  → 호스트에서 curl 3병렬로 `/opt/llm-models/qwen3.8-27b-fp8/` 에 직접 받아 경로로 로드 (20:59~22:31, 81파일 크기 전부 일치).
- GPU 고정: k8s 자원 요청 대신 `NVIDIA_VISIBLE_DEVICES=<GPU2 UUID>` (GPU0 은 다른 사람 Docker 가 점유). 파드 안 nvidia-smi 로 GPU2 만 보임 확인.
- 함정 3개: ① 파일 목록을 Windows 에서 만들어 `
` 이 URL 에 섞임 → curl 전부 거부 ② `pkill -f` 패턴이 내 SSH 명령줄에 매칭돼 세션 3번 끊김 → `pgrep -x`
  ③ **서버의 `vllm/vllm-openai:latest` 태그가 사라져 있어(kubelet 이미지 GC) kubelet 이 도커허브에서 8.8 GB 를 다시 받으려다 100 B/s 에 멈춤 →
  kubelet 이 새 파드를 하나도 못 만드는 상태**(기본 런타임 테스트 파드도 ContainerCreating). `ss -K` 로 그 TCP 연결을 끊자 즉시 풀림. 이미지는 digest 로 고정.
- `--kv-cache-dtype fp8` 은 뺐다 (미보정 스케일 경고). 로드 5초, 엔진 초기화 183초, GPU2 88 GB(가중치 28 + KV).
- thinking 은 요청마다 `chat_template_kwargs.enable_thinking=false` 로 끈다 — 켜 두면 1,500 토큰을 전부 추론에 쓰고 본문 0자(실측).

**A/B 실호출** (아이디어 3 × 문항 5, 같은 참고자료(각 8건)·같은 답변·같은 후처리, 5문항 동시. 원본 `docs/measurements/2026-09-02_ab_llama_qwen.json`):

| | 라마 70B AWQ (GPU1) | **Qwen3.8-27B-FP8 (GPU2)** |
|---|---|---|
| 성공 | 15/15 | 15/15 |
| 문항 평균 소요 (5 동시) | 61s | **32s** |
| 긴 문항 평균 글자 (목표 1,400~1,800) | 1,153자 | **1,609자** |
| 긴 문항 평균 문단 | 3.2 | **5.1** |
| 이물 문자(후처리 뒤) | 0 | 0 (후처리 전 스모크에서 `供需` 1건 — 중국어 누출 있음) |
| 출처 표기 | 0 | 0 |
| 자동 이어쓰기 발동 | 11/15 | 10/15 |
| 단일 스트림 속도 | ~35 tok/s | **46~47 tok/s** |
| 눈으로 본 것 | 문장 반복·되풀이 문단, 관련 없는 인용(설 연휴 47.3%) | 문단 구조 뚜렷, 자연스러움. 단 "반찬가게 3곳을 찾아가 관찰한 결과" 처럼 계획을 이미 한 일로 쓰는 지어냄 있음 |

→ Qwen 이 속도·분량·구조 모두 우위. 지어냄은 양쪽 다 있어 refine ②(입력에 없는 수치·경험 삭제)가 계속 필요.

**전환 (사용자 결정 00:1x, 실행 완료)**: `config.yaml` = Qwen(본문+조사), 라마는 `config.yaml.llama.bak`. `scripts/switch_model.py llama|qwen` + `nssm restart` 로 왕복.

### 40명 전체 흐름 부하 실측 (`scripts/live_load_test.py` — 인테이크→카드 답→문항 8개 실조사→생성, 사용자마다 다른 아이디어·IP)

| | 1차 (조사 워커 6) | **2차 (조사 워커 24)** |
|---|---|---|
| 벽시계 | 29분 | **22분** |
| 사용자 1명 전체 p50 / 최대 | 27분 49초 / 29분 | **21분 28초 / 22분** |
| ① 인테이크(동기) 성공 | 2/40 (38건 600s=nginx 상한 초과) | 17/40 (23건 600s 초과). 서버 실측 중앙 11분 4초, 최대 17분 |
| ② 문항 조사(동기, 320) | 306 성공, p50 115s, p95 503s | **320 성공, p50 17s, p95 42s** |
| ③ 문항 생성(큐, 320) | 320 성공, 대기 p50 106s, 실행 94~99s, 체감 p50 208s·p95 309s | 320 성공, 대기 p50 124s, 실행 94s, 체감 p50 215s·p95 330s |
| 긴 문항 글자 | 1,543자 (1,000 미만 4) | 1,552자 (1,000 미만 2) |
| 서버 최고 | API 대기 73, vLLM 44, KV 34%, preempt 0 | API 대기 98, vLLM 52, KV 39%, preempt 0 |
| 조사 LLM 호출 큐 대기 | 중앙 2분 36초 (988건) | **중앙 9초** (643건) |

원본: `docs/measurements/2026-09-02_load40_qwen.json`, `2026-09-03_load40b_qwen_rw24.json`.

읽기:
- 조사 워커 상향으로 문항 조사는 해결(115s→17s). **인테이크는 여전히 11분** — 조사 LLM 대기는 9초뿐인데, 인테이크 안의 호출(검색어 1 + 페이지 추출 6 + 카드 생성 1)이
  GPU 를 생성 320건과 나눠 쓰느라 호출당 1~1.5분(p90 실행 1분 34초)씩 걸리고, 카드 생성은 본문 큐(생성이 채움)에서 최대 3분 47초 대기.
  즉 **GPU 총량이 한계**: 40명 = LLM 호출 ~1,000건 ≈ 20분치 GPU 시간. 워커를 어떻게 나눠도 총 시간은 못 줄이고, 누가 먼저 받느냐만 바뀐다.
- APITimeoutError 1건 (00:26, 연결 30초 반영 전 코드? 재시작 후라 반영됨 — 재발 시 확인).

다음 손잡이 (효과 순): ① GPU1 의 라마를 내리고 **Qwen 2번째 복제본**(같은 Service, replicas 2) → 처리량 2배 (사용자 결정 필요)
② vLLM `--scheduling-policy priority` + 요청 `priority` 로 인테이크·조사 호출을 생성보다 먼저 → 카드가 1~2분에 나오고 생성이 대기(순번 표시)를 흡수
③ 프론트 인테이크·조사를 `/jobs` 로 (600s 절벽 제거, 순번 표시).

**전환 준비(기록)**: `backend/config.yaml.qwen` (llm·models·research.llm 세 블록만 다름). 사용자 결정 후 `config.yaml` 을 라마 백업(`config.yaml.llama.bak`)으로 두고 교체 → `nssm restart mochang-api`.
라마 파드는 그대로 두어 언제든 되돌릴 수 있다.

## 다음 순서

1. 운영 반영: `mochang-api` 재시작 (+ 서버 venv `pip install -r backend/requirements.txt`) **+ 프론트 dist 재빌드·배포 승인** (draft_id 전송은 빌드해야 반영).
   재시작 후 `scripts/drafts_report.py` 로 첫 행 확인.
2. 보존 기간·삭제 정책은 미정 — 쌓이는 양을 보고 결정 (문항당 2KB, 하루 100명이면 약 2MB/일).

---

# PROGRESS — 2026-09-01 저녁 세션 (라마 전환 · 공개 배포 · 부하 진단)

> 이 절이 최신. 같은 날 낮 세션 기록은 아래에 그대로 둠.

## 한 줄 요약

OpenRouter 무료 한도 때문에 하루 1~2명밖에 못 쓰던 것을 **사내 GPU 라마 70B 로 전환**하고,
`https://www.bustartup.kr:50001/` 로 **공개**했다. 50명 부하 테스트를 돌려 병목을 측정했고,
프롬프트 버그 11건을 고쳤다. **품질(이물 문자·분량·출처 날조)은 아직 미해결이라 모델 결정이 남았다.**

---

## 1. 배포 — 50001 공개

`https://www.bustartup.kr:50001/` 에서 접속된다. 같은 서버의 WeldLoop(30001) 패턴을 복제했다.

- 프론트: `frontend/dist` 를 nginx 가 정적 서빙 (SPA fallback)
- 백엔드: `location /api/ { proxy_pass http://127.0.0.1:8000/; }` — **끝 슬래시가 `/api` 접두어를 떼어낸다**
  (라우트가 `/generate`, `/jobs/{id}` 처럼 루트에 있어서). 백엔드 코드는 안 고쳤다.
- `frontend/.env.production` 의 `VITE_API_BASE=/api` 가 빌드에 박힌다.
  **프론트를 고치면 `npx vite build` 를 다시 해야 반영된다.**
- 백엔드는 `mochang-api` NSSM 서비스로 등록됨. 개발용 uvicorn 을 직접 띄우려면 먼저 `nssm stop mochang-api`.

**접근 제한이 없다.** nginx 50001 블록 안에 `auth_basic` 두 줄이 주석으로 들어 있어 필요하면 풀면 된다.

## 2. 모델 — 사내 GPU 라마 70B 로 전환

`base_url` 이 openrouter.ai 가 아니면 `daily_request_limit`·`fallback` 이 자동으로 꺼지는 구조라
(`client.py` `is_openrouter`) 설정만 바꾸면 됐다. 되돌리려면 `backend/config.yaml.openrouter.bak`.

### 실호출 검증 (사람이 직접 확인)

| 항목 | 라마 70B | minimax-m3 |
|---|---|---|
| 하루 한도 | 없음 | 50회 (하루 1~2명) |
| 45명 동시 | 45/45 성공, 73초 | 워커 2로 불가 |
| 글자수 (목표 1900~2000) | **1,050자** | **2,056자** |
| 이물 문자 혼입 | **3/3건**, 45건 테스트에선 **37/45(82%)** | 0/3건 |
| 출처 날조 (참고자료 없이) | **2/2건** | 0/2건 |
| 속도 | 21초 | 23초 |

- 이물 예: `ดมหกลใ`(태국어) `くよ`(일본어) `浪費 牛肉`(한자) `nhu cầu`(베트남어) `계정を作成`
- 날조 예: "출처: 한국소비자원, 2020년 「식품浪費 현황 및 개선방안」 보고서에 따르면 13.5%" — 전부 존재하지 않음

### 결정 필요

**라마 단독으로는 서비스 불가.** 정부 신청서에 가짜 통계·가짜 출처가 들어가는 건 감점이 아니라 사고다.
선택지:

1. **OpenRouter $10 충전** → 하루 1000회 = 하루 37명. 품질은 검증된 그대로. `daily_request_limit: 1000` 한 줄.
2. **라마 유지 + 후처리** — 이물 검출 재생성 / 출처 문장 검증 / 분량 미달 시 extend 자동 호출.
   출처 날조는 완전히 막기 어렵다.
3. **하이브리드** — 초안은 라마(무한), 최종본만 minimax(한도 내).

## 3. 프롬프트 수정 11건 — 근거는 `REPORT.md`

외부에서 받은 진단 지시를 코드로 검증해 **사실인 것만** 반영했다.
지시 중 "공통 블록 복제 드리프트", "Q2 오타", "Q7-1 라우팅 없음", "인테이크 필드 부재"는 **거짓**이었다.

고친 것 중 큰 것 셋:

- **Q10 이 Q1 과 같은 문장으로 나오던 버그** — `assemble.py` 가 100자 문항에 `short_question.md` 를 붙이는데
  그 파일 1행이 "다른 어떤 지시보다 우선", 5행이 "명사로 맺는다" 였다. `q10.md` 의 "감정적", "질문형 허용"이 덮였다.
- **인용이 "출처: 한경매거진, 연도 미상에 따르면" 으로 나오던 것** — `system.md` 와 `sections.md` 가 둘 다
  그 형식을 지시하고 있었다. **모델 문제가 아니라 지시 문제**였다 (GPT-5 로 같은 프롬프트를 돌려도 `(출처: …)` 를 5회 씀).
- **문체가 간헐적으로 반말체가 되던 것** — `system.md` 에 문체 규칙이 아예 없었고,
  생성 직전에 읽는 참고자료 머리말이 평서체였다. 모델이 최근 문체를 따라갔다.

참고자료 렌더링도 고쳤다: `"null"` 문자열 방어, `date` 가 비면 URL 에서 연도 추출, 같은 URL 묶기.

`pytest` **87 passed** (기존 83 + 신규 4).

## 4. 계측 추가

각 단계 소요 시간을 추적할 방법이 아예 없었다 (nginx 기본 로그 형식, uvicorn 로그에 시각 없음).

- `backend/timing.py` — JSONL 기록. 모델 호출 없음, 실패해도 서비스에 영향 없음
- `JobQueue.on_done` 훅 — 큐가 이미 재고 있던 `queued_s`(대기) / `run_s`(실행)를 남긴다
- `scripts/timing_report.py` — 단계별 요약 (`--last N` / `--tail`)

**대기와 실행이 분리되는 게 핵심이다.** 느린 원인이 "줄서기"인지 "모델"인지 갈리고 처방이 다르다.

## 5. 부하 진단 — `docs/BOTTLENECKS.md`

50명이 실제 사용자처럼 쓰는 상황을 재현했다 (인테이크 1 + 문항 3 = 200건, 90초에 걸쳐 도착).

```
세션 성공  50/50, 오류 0
문항 1개   대기 2분 49초  |  실행 23.9초   ← 대기가 실행의 7배
큐         496초 내내 running=12 고정, queued 최대 90
처리량     분당 24건
전체 세션  중앙 5분 09초, 최대 6분 49초
```

### 먼저 무너지는 순서

```
1층  외부 웹 검색 (ddgs)   ← 수십 명 규모에서 이미 위험. 여기가 먼저 터진다
2층  SSH 터널              ← 단일 장애점, 서비스화 안 됨
3층  사내 GPU vLLM         ← 45명 검증. 그나마 튼튼
```

**LLM 은 사내로 옮겼지만 조사는 외부 인터넷에 통째로 의존한다.**
사용자 1명이 9문항을 채우면 검색 36회 + 외부 페이지 54회. 50명이면 검색 1,800회.
`config.yaml` 주석에 이미 캡차를 맞은 기록이 있다.
**자체 SearXNG 는 답이 아니다** — 메타서치라 결국 우리 서버가 남의 검색엔진을 긁게 되고 똑같이 차단된다.

### 가장 큰 발견 — V1

**프론트가 작업 큐를 안 쓴다.** `frontend/src/api.js:60` 이 `post("/generate", …)` 로 동기 엔드포인트만 부른다.
백엔드 큐 API 는 완성돼 있는데 안 쓰고 있다. 그래서:

- 사용자가 3분 넘게 HTTP 연결을 열어둔 채 빈 화면을 본다
- 50명 × 3문항 = 연결 150개가 수 분간 유지 (nginx `worker_processes 1`, 1024를 5개 사이트가 공유)
- 브라우저는 호스트당 동시 연결 6개 제한
- 큐가 계산해 둔 `position`(앞에 몇 명)을 받을 방법이 없다

---

## 다음 순서

1. **V1 — 프론트를 작업 큐로 전환.** 이거 하나로 V5(대기 순번 미표시)·V6(새로고침 유실)·V9(연결 고갈)가
   함께 풀린다. 백엔드는 이미 준비돼 있어 **프론트만** 고치면 된다. **1순위.**
2. **모델 결정** (위 2절). 이물 82% 를 안고 갈 수는 없다.
3. **SSH 터널 서비스화** — 작업량 최소, 방치하면 전면 장애.
4. **사용자별 동시 작업 제한** — 한 명이 27건을 던져 큐를 독점할 수 있다. 인증도 없다.
5. **조사 재사용(아이디어당 1회)** — 검색량 1/9.
6. **공공데이터 색인(RAG)** — 외부 의존을 0으로 만드는 유일한 길. KOSIS·data.go.kr 정식 API.

## 미해결로 남긴 것

- **조사가 물어온 출처가 블로그·도메인명** (`restata`, `zachutip`, `1conomynews`).
  신청서에 "restata에 따르면"은 쓸 수 없다. `extract_facts.md` 와 publisher 검증이 필요하다.
- **fact 요약이 원문과 반대인 경우** — `fact: "학부모의 인식실태는 긍정적이다"` ↔
  `quote: "…같이 대화를 나누지는 못한다"`. 라마가 그 대목에서 헷갈린 직접 원인.
- **없는 숫자 날조** — Q4-1 "자금 1억 원, 40:30:30", Q3-2 "첫해 1,000명".
  `system.md` 가 금지하는데도 나온다.
- **Q2·Q8 의 멘토 침범** — `q2.md [피할 것]` 에 금지를 넣었는데도 여전. 모델 한계.

## 이 PC 운영 메모

`bustartup.kr` 이 같은 PC 에서 25시간 다운됐던 건을 함께 처리했다. 상세는 바탕화면 `vsp자동복구.txt`.
6개 서비스(`vsp-nginx/faiss/llama/spring/front`, `mochang-api`)를 NSSM 으로 등록해 자동 복구된다.

**로그오프하면 안 된다.** SSH 터널(30800)과 Ollama 는 서비스가 아니라 로그인 세션(세션 2) 프로세스라
로그오프하면 죽는다. 자리를 뜰 때는 **RDP 연결 끊기 또는 화면 잠금(Win+L)** 만 한다.

---

# PROGRESS — 2026-09-01 세션 (70B는 이 PC가 아니라 원격 GPU 서버 — 아래 '로컬 모델 환경' 절)

> (2026-09-01 낮 세션. 최신 절은 맨 위.)


## 전체 구조·진행 상황 분석 (2026-09-01)

### 무엇을 만드는 물건인가

모두의창업 2기 지원서(9문항, 대부분 2000자) 자동 작성기. 아이디어 한 문단에서 문항별 초안을 뽑아
사용자가 고르고 다듬어 붙여넣게 한다. 설계 사상은 **"빈 칸을 묻지 말고 보기를 준다"**
(회상은 어렵고 인식은 쉽다 — NN/g 휴리스틱 #6).

### 구성도

```
[브라우저] :5173
    │  fetch (CORS *)
    ▼
[프론트 Vite :5173]  React, App.jsx 739줄 단일 파일, 라우터 없음(step 상태 하나)
    │  API_BASE = http://localhost:8000
    ▼
[백엔드 FastAPI :8000]  ← 지휘자
    │
    ├─ LLM ──▶ JobQueue(asyncio.Queue + 워커 2) ──▶ ❶ OpenRouter MiniMax M3  (현재 기본)
    │                                               ❷ Llama 70B (전환 대기)
    │
    └─ 조사 ──▶ ❸ Vane :3000   (research.vane.enabled: false — 꺼짐)
                ❹ ddgs 검색 + trafilatura 본문추출   ← 실제 동작 경로
                   └ 7일 디스크 캐시 backend/.cache/research/
```

프롬프트 문구는 코드에 없다. `prompts/` md 세트(9문항 × 3스타일 × 2트랙 = 54조합)를 `assemble.render()` 가 조립한다.

### 통신 경로

| 출발 | 도착 | 방법 | 인증 | 상태 |
|---|---|---|---|---|
| 브라우저 | 백엔드 `:8000` | HTTP fetch | **없음** (CORS `*`) | 동작 |
| 백엔드 | OpenRouter | HTTPS `/api/v1` | `${OPENROUTER_API_KEY}` | **동작 — 기본 경로** |
| 백엔드 | Vane `:3000` | HTTP `/api/search` | 없음 | **꺼짐** |
| 백엔드 | ddgs + 웹 | HTTPS | 없음 | 동작 — 조사의 실제 경로 |
| 브라우저 | 70B | SSH터널 50000 → NodePort 30500 → Vane → `vllm:8000` | 없음 | 2026-09-01 배포 |
| 백엔드 | 70B | 터널 50000 | `dummy` | **미연결** (`config.yaml` 미변경) |

### 화면 4단계 (App.jsx `step` 상태)

| step | 화면 | 진입 조건 | 내용 |
|---|---|---|---|
| 0 | 아이디어 입력 | 항상 | 트랙·아이디어·사업자여부·팀원수·역량 |
| 1 | **정보 보충** | 카드 1장 이상 | 인테이크 카드 한 장씩 최대 7장. 다중선택 / 기타 / 모르겠어요 / 다른 보기 / 건너뛰기 |
| 2 | 초안 고르기 | 생성 시작 후 | 문항 × 스타일 그리드, "정보 보태기" 인라인 패널, 이어쓰기 |
| 3 | 제출용 정리 | 완성 문항 1개 이상 | 문항별·전체 복사, 직접 입력 항목(Q5·Q6·Q9·Q10·Q11·멘토기관) 안내 |

정보 보충이 이 프로젝트의 차별점. 슬롯 9개(`customer, problem, alternative, solution, revenue,
first_step, evidence, capability, mentoring`)를 LLM이 판정해 부족한 것만 카드로 묻는다.
우선순위 `problem > customer > revenue > alternative > evidence > capability > mentoring > first_step > solution`.

### 엔드포인트 12개

| 엔드포인트 | LLM 호출 | 상태 |
|---|---|---|
| `/health` `/models` | 0 | ✅ |
| `/intake` | 1 | ✅ 실호출 검증 (짧은 입력 → 카드 6장 62초) |
| `/intake/regenerate` | 1 | ✅ 코드만 — 실호출 미검증 |
| `/research` | 검색어 1 + 사실추출 1~2 | ✅ 실호출 검증. **단 프론트에 화면 없음** |
| `/generate` | 문항×스타일당 1 | ✅ Q2 1600자 40초 |
| `/extend` | 1 | ✅ 659자 추가 10초 |
| `/verify` | 1 | ⚠️ **실호출 미검증** |
| `/jobs/{kind}` `/jobs/{id}` `/jobs` | — | ✅ 백엔드만. **프론트는 아직 동기 호출** |
| `/generate/dry-run` | 0 | ✅ |

동시성: 모든 모델 호출이 `JobQueue` 경유. 워커 `max_workers: 2`, 429 지수 백오프 3회. 부하 테스트 동시 50건 통과.

### 진행 상황

**된 것** — 백엔드 파이프라인 전부, 프론트 연결, 인테이크+재생성, 조사 API, 검증 API, 동시성 층,
모델 드롭다운·폴백, 테스트 83건(전부 mock)

**안 된 것**
1. **조사 결과 UI** — `/research` 는 API만 있고 화면이 없어 브라우저로는 조사 단계를 탈 수 없다
2. **검증 결과 UI** — `unsupported_claims`(근거 없는 문장) 하이라이트 미구현
3. **`/jobs` 폴링 프론트 전환** — 지금은 동기 호출이라 긴 생성에서 타임아웃 위험
4. **인테이크 결과 저장** — 새로고침하면 통째로 유실
5. LlamaIndex `/ingest` — 미착수
6. `styles/story.md`·`plain.md` 품질 — 스토리형이 개인 서사를 지어냄
7. 무료 한도 카운터 부정확 — 요청 **전에** 세서 실패·재시도까지 카운트 (57 vs 실제 42)

### 가장 큰 리스크 4가지

1. **지어내기** — 이어쓰기에서 "자취생 친구 여섯 명에게 물었다"처럼 입력에 없는 사례를 만들어냈다.
   이걸 잡으라고 `/verify` 를 만들었으나 **실호출 검증을 한 번도 안 했다.** 지원서에 없는 사실이 들어가면 치명적.
2. **무료 한도 50회/일** — 전체 흐름 1회 ≈ 생성 9 + 인테이크 2 + 조사 3~4 = **15회**. 하루 3명이면 소진.
   70B로 넘어가면 이 문제가 통째로 사라진다.
3. **70B의 한국어 결함** — 1500자 요청에 750자, 한자 혼입. 전환 시 **초안→확장 2단계**와 **한자 필터**가 파이프라인에 필요.
4. **`App.jsx` 739줄 단일 파일** — 4개 화면 + 인테이크 + 재생성이 한 컴포넌트에. 조사·검증 UI를 더 넣으면 유지보수가 급격히 어려워진다.

### ⚠️ 브라우저 테스트로 확인할 수 있는 것 / 없는 것

| 확인 가능 | 확인 불가 |
|---|---|
| 입력 → 인테이크 카드 → 생성 → 이어쓰기 → 복사 | **조사(`/research`) 전 과정** — 화면이 없다 |
| OpenRouter 응답 품질·속도 | Vane 연동 — `enabled: false` 라 백엔드가 안 부른다 |
| 모델 드롭다운·폴백·한도 배지 | 검증(`/verify`) 결과 — 화면이 없다 |

**"조사한 값을 라마가 만들어 OpenRouter에 전달"하는 경로는 아직 없다.**
현재 조사는 ddgs로 검색하고 사실 추출도 **OpenRouter**가 한다. 라마는 이 흐름에 참여하지 않는다.
조사를 테스트하려면 브라우저가 아니라 `POST /research` 를 직접 호출해야 한다.

---

## 로컬 모델 환경 (2026-09-01 실측)

> **가장 중요한 정정: 이 PC는 70B 머신이 아니다.** 70B는 **원격 GPU 서버**에 있고, 이 PC(RTX 3080 10GB×2)에서는 절대 못 돌린다.
> `C:\dev\llama-gpu-deploy` 는 그 원격 서버를 `ssh gpu` 로 조종하는 **문서·매니페스트 전용 저장소**다 (애플리케이션 코드 없음).
> 이번 세션에서는 **읽기 전용 조사만** 했다 — 파일 수정·컨테이너 재시작·`kubectl apply` 없음.

### A. 이 PC 하드웨어

| 항목 | 값 |
|---|---|
| GPU | **NVIDIA RTX 3080 10GB × 2** (총 20GB, compute 8.6, 드라이버 591.86 / CUDA 13.1) |
| CPU / RAM | i9-12900K / **31.8GB** |
| C: 여유 | 249GB |
| GPU 메모리 점유 | GPU0 **0MiB**, GPU1 **1,015MiB** — 전부 Chrome·Edge·VS Code·explorer 등 **데스크톱 WDDM 앱**. 추론 프로세스 없음 |
| 서빙 런타임 | **Ollama 0.32.9** 설치됨 (127.0.0.1:11434 대기 중, 로드된 모델 0). **Docker·kubectl 없음**, vLLM·LM Studio 없음 |
| 로컬 Ollama 모델 | `deepseek-r1:32b`(19GB) / `deepseek-r1:14b`(9GB) / `deepseek-r1:8b`(5.2GB) — 전부 Q4_K_M, **전부 thinking 모델** |

**→ 70B AWQ는 ~40GB라 VRAM 20GB에 안 올라간다. 이 PC에서 70B는 불가능.**

### B. `C:\dev\llama-gpu-deploy` 분석 (읽기 전용)

**1) 구조** — `.md` 문서 + `k8s/*.yaml` 2개 + `scripts/0{1,2,3}-*.sh` + `prompts/`·`knowledge/`(모두의창업 지원서 에이전트용). 실행 코드 없음.

**2) 무엇으로 서빙하나**

| 항목 | 값 |
|---|---|
| 런타임 | **vLLM 0.25.0** (`vllm/vllm-openai:latest`, **공식 이미지 그대로 — 커스텀 빌드·소스 수정 없음**) |
| 모델 | `casperhansen/llama-3.3-70b-instruct-awq` (Llama-3.3-70B, **AWQ 4bit**, `awq_marlin`) |
| GPU 할당 | **1장** (`nvidia.com/gpu: 1`), `--gpu-memory-utilization 0.90`, `--enforce-eager` |
| 컨텍스트 | **32,768** (`--max-model-len 32768`) |
| 오케스트레이션 | k3s v1.36.2+k3s1, `runtimeClassName: nvidia`, 모델 캐시 hostPath `/opt/llm-models` |
| 서버 | Dell Precision-7960-T2, Ubuntu 24.04, **RTX PRO 6000 Blackwell 96GB × 3장** |

**3) 엔드포인트 / 인증**

| | |
|---|---|
| 서버 LAN | `http://192.168.8.55:30800/v1` (NodePort 30800 → 컨테이너 8000) |
| 채팅 웹UI | `http://192.168.8.55:30080` (Open WebUI) |
| SSH | `ssh gpu` → `211.253.154.145:22`, 계정 `user`, 키 `~/.ssh/id_gpu` (무passphrase) |
| **인증** | **없음.** 틀린 키(`Bearer totally-wrong-key`)로 보내도 **HTTP 200**. `api_key` 는 아무 값이나 가능 |
| **이 PC에서 직접 접근** | **❌ 불가.** `211.253.154.145:30800` / `192.168.8.55:30800` 둘 다 **timeout**. 열린 건 22번뿐 |

**4) 지금 실제로 떠 있나 — ✅ 떠 있다** (2026-09-01 09:48 직접 확인)

- 서버 uptime **49일 23시간**
- `vllm-5dfb8b869f-h54ss` **1/1 Running, restarts=0, 19시간** (2026-08-31 05:47 재배포 — 컨텍스트 8k→32k 확장 시점)
- `open-webui-99f674bb4-jhl4s` **1/1 Running, 49일**
- svc `vllm` NodePort 8000:30800, `open-webui` 8080:30080
- GPU1 **90,086MiB** = `VLLM::EngineCore`. GPU2는 15MiB로 유휴
- `GET /v1/models` → `casperhansen/llama-3.3-70b-instruct-awq`, `"max_model_len":32768`

**5) 기동·정지 / 자동 시작**

- 기동은 `kubectl apply -f k8s/vllm-deployment.yaml` (deployment라 상시 유지). 별도 시작 스크립트 없음
- `systemctl is-enabled k3s` → **enabled / active** → **재부팅하면 vLLM·Open WebUI 자동 복구됨**
- ⚠️ 재배포 = 다운타임. 이미지 `imagePullPolicy: IfNotPresent`(기관 회선 0.24MB/s 대비), `strategy: Recreate`(GPU 교착 방지)

### C. 그 외 서빙

- **이 PC**: Ollama 0.32.9만. Docker·kubectl·vLLM·LM Studio 전부 없음
- **서버**: k3s 외에 문서에 없는 **Docker 컨테이너 `hunyuan3d_env`** 가 2주째 가동 중 (`nvidia/cuda:12.4.1-devel`, 호스트 **8080** 노출, **GPU0에 14,658MiB** 점유, `restart=no`). 이전 담당자 문서 어디에도 없음 → **다른 사람의 작업으로 추정**

### D. 응답 속도 (서버 로컬 curl 기준)

| 테스트 | 결과 |
|---|---|
| 짧은 프롬프트(`1+1은?`, 8토큰) | **0.25초** |
| 한국어 장문(창업 배경 요청, 467토큰) | **13.2초 → 약 35 tok/s** |
| SSH 터널 경유(29토큰) | **0.84초** — 터널 오버헤드 사실상 없음 |

### E. 우리 연결 관점

- **`config.yaml` 에 넣을 값** (터널을 켠 상태에서):
  - `base_url: "http://localhost:30800/v1"`
  - `api_key: "dummy"` (검증 안 함)
  - `model: "casperhansen/llama-3.3-70b-instruct-awq"`
  - `extra:` **비울 것** (OpenRouter 전용 `reasoning` 옵션은 vLLM에서 불필요)
  - `max_workers`: 무료 한도가 없으므로 **4~8**로 올릴 수 있음
  - `daily_request_limit`: 로컬 서버엔 의미 없음
- **전제 조건**: `ssh -L 30800:localhost:30800 gpu` 터널이 켜져 있어야 함. 창을 닫으면 백엔드 호출이 전부 실패한다.
- **포트 충돌 없음**: 우리 백엔드 8000, Vite 5173, Ollama 11434, 터널 30800 — 겹치지 않음. (터널 로컬 포트를 8000으로 잡으면 충돌하니 30800 그대로 쓸 것)
- **Vane 채팅 LLM용 작은 모델**: 로컬에 8B가 있긴 하나 `deepseek-r1:8b` 는 **thinking 모델**이라 검색어 재작성·요약에 `<think>` 블록이 섞일 위험이 크다. → **`qwen2.5:7b-instruct` 또는 `llama3.1:8b`(Q4 약 5GB) 를 새로 받는 것을 권장.** 3080 10GB 1장에 여유 있게 올라간다.

### F. 신뢰성 검증 결과

**1) 정체 확인** — `/v1/models` 가 매니페스트와 **일치**(모델명·32k). 단 **문서와 실제가 다름**: `docs/REPORT-AND-USAGE.md` 는 아직 **"최대 컨텍스트 8,192"** 로 적혀 있다(2026-07-13 작성, 08-31 확장 미반영). **신뢰할 문서는 `docs/HANDOVER.md`**. 우리 요구(3~4k 입력 + ~1.5k 출력)는 32k로 **충분**.

**2) 한국어 장문** — 13.2초, 그러나 **1500자 요청에 약 750자만 생성**(467토큰). 결함:
   - **한자·일본어 혼입 재현됨**: `현대人の`, `인공지능聊天봇` — 3회 시도 중 2회 발생
   - **어색한 반복**: `윈-윈-win-win`
   - 번역투·중간끊김·존댓말 흐트러짐은 **없었음**. 문장 자체는 자연스러움
   - HANDOVER 8-7의 "한 응답에 약 600자 한계 + 한자 혼입"을 **독립적으로 재현 확인**

**3) 동시 요청 2건** — A 2.46초 / B 3.50초, **벽시계 3.52초**. 직렬이면 ~5초여야 하므로 **vLLM 연속 배칭이 동작**한다. 직렬 처리 아님. ✅

**4) 안정성 10회 연속** — **10/10 HTTP 200**, 응답 형식·내용 모두 일관. 실패·타임아웃 0. ✅

**5) 운영 위험**
   - 🔴 **서버 `ufw` 비활성 + vLLM 무인증**. NodePort는 iptables로 전 인터페이스에 열리므로 **사내망(192.168.8.55) 안의 누구나 인증 없이 70B를 호출**할 수 있다. 외부 인터넷에서는 기관 방화벽이 막고 있어 우리 PC에서도 timeout.
   - 🟡 **디스크 82% 사용 (937G 중 164G 여유)**. `/opt/llm-models`: hub 38G + **`qwen25-32b-awq` 19G**. HANDOVER는 "다운로드 진행 중"이라 했으나 **다운로드 프로세스는 없고 19G로 완료 상태** — 받아놓고 안 쓰는 중.
   - 🟡 **문서에 없는 `hunyuan3d_env` 컨테이너가 GPU0 14.6GB 점유**. `restart=no`라 재부팅 시엔 안 살아남음.
   - 🟢 재부팅 자동복구: k3s enabled → vLLM·WebUI 자동 복구 **됨**
   - 🟢 커스텀 빌드·수정 소스 **없음** (공식 `vllm/vllm-openai:latest`)

**6) 우리가 모르는 것 / 확인 못 한 것**
   - `hunyuan3d_env` 가 **누구 것이고 왜 GPU0을 쓰는지** — 우리가 GPU를 더 쓰려 하면 충돌할 수 있음
   - `qwen25-32b-awq` 19G를 **받아만 두고 왜 안 띄웠는지**. 한국어 품질 개선 후보였는데 결론이 없음
   - GPU2(96GB)가 **완전히 유휴**인 이유 — 텐서 병렬이나 두 번째 모델을 안 올린 근거가 문서에 없음
   - vLLM 파드가 **19시간 전 재배포된 정확한 경위** (8k→32k 확장으로 추정하나 기록 없음)
   - 서버 계정 `user` 의 **권한 범위**와 이 서버를 우리가 계속 써도 되는지에 대한 **합의 여부**
   - 사내망 다른 사용자가 이 엔드포인트를 쓰고 있는지 — **쓰고 있다면 우리 부하가 그쪽에 영향**을 준다

### G. 판정 — **조건부 사용**

**쓸 수 있다.** 성능·안정성은 우리 용도에 충분하다: 35 tok/s, 32k 컨텍스트, 동시 요청 배칭 정상, 10/10 무결. 무료 한도도 없다.
**다만 `config.yaml` 만 바꿔서는 안 된다.** 아래 4가지가 전제다.

| # | 조건 | 내용 |
|---|---|---|
| 1 | **SSH 터널이 필수** | 이 PC에서 30800에 직접 못 닿는다. `ssh -L 30800:localhost:30800 gpu` 가 살아 있어야만 동작하고, 끊기면 모든 호출이 실패한다. 백엔드에 **연결 실패 시 명확한 에러·재시도**가 필요하고, 터널을 상시 유지할 방법(별도 창 유지 또는 autossh)을 정해야 한다. |
| 2 | **출력 길이 한계 대응** | 1500자 요청에 750자만 나온다. 우리 프롬프트가 **1회 생성으로 2000자**를 기대한다면 그대로는 못 쓴다. HANDOVER 8-7의 **"초안 → 확장" 2단계**를 우리 파이프라인에도 넣어야 한다. |
| 3 | **한자 혼입 후처리** | `現代人の`·`聊天` 같은 CJK 혼입이 재현된다. 프롬프트로 못 막으므로 **생성 후 한자/가나 검출 필터**를 넣거나 육안 검수 단계를 두어야 한다. |
| 4 | **공용 자원임을 전제** | 무인증·방화벽 없음이고, GPU0엔 남의 작업이 돌고 있다. 부하를 올리기 전에 서버 담당자와 합의할 것. `max_workers` 는 일단 **4** 정도에서 시작. |

**"사용 안 함"은 아니다** — 이 PC의 Ollama로 대체하면 최대 32B(deepseek-r1:32b, 19GB)인데 10GB 카드 1장에 안 올라가 CPU 스필로 매우 느려지고, 그마저 thinking 모델이라 지원서 생성에 부적합하다. 70B 서버가 압도적으로 낫다.

**Vane 채팅 LLM은 별개로 로컬 Ollama에 붙인다** (터널과 무관하게 동작해야 하므로). `qwen2.5:7b-instruct` 또는 `llama3.1:8b` 를 받을 것.

> 이전 담당자 구성은 **아무것도 건드리지 않았다.** 위는 전부 읽기 전용 조회와 엔드포인트 호출 결과다.

---

## 오늘 한 것

| # | 항목 | 상태 |
|---|---|---|
| 1 | **Q4-2 멘토링 카드** — 인테이크 슬롯 `mentoring` 추가(9번째). 항상 카드 생성, ready 계산에선 제외, MAX_CARDS(7)에 잘려도 자리 보장, multi 보기 최대 8 | ✅ |
| 2 | **카드 재생성** — `POST /intake/regenerate` (+ `/jobs/intake_regenerate`). `prompts/intake_regenerate.md`, 이미 본 보기 금지(코드로도 필터), 지원자 메모, 다른 카드 답을 사실로 주입 | ✅ |
| 3 | 프론트 — `RegenerateBar`(메모 + "다른 보기 보기 (n)") 인테이크 카드·정보 보태기 패널 양쪽. ready 여도 카드 있으면 보여주고 머리말만 다르게 | ✅ vite build 통과 |
| 4 | 테스트 `tests/test_intake.py` 12개 추가 → **81 passed** (모델 호출 0). ASGI 스모크로 엔드포인트·jobs 경로 확인 | ✅ |
| 5 | README · PROJECT_CONTEXT 6-1 갱신 | ✅ |
| 6 | 재생성 시 **이미 고른 보기는 유지**(`keep`, 카드 맨 앞·원래 hint), 나머지만 교체. 답(answers)은 건드리지 않음 | ✅ 83 passed |
| 7 | **기본 글 스타일 → 논리·근거형**(`logic`), 목록에서도 첫 번째. 스토리형·실무형은 품질 낮음 → `styles/story.md`·`plain.md` 다듬기 **할 일** | ✅ 기본값만 |

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
python -m pytest -q             # 83 passed 확인
cd frontend && npm install && cd ..
# 서버: uvicorn backend.main:app --reload --port 8000 / 프론트: cd frontend && npm run dev
# Vane: docker run -d -p 3000:3000 -v vane-data:/home/vane/data --name vane itzcrazykns1337/vane:latest
#       → python -m backend.rag.vane_setup  (새 머신은 볼륨이 비어 있으므로 제공자 재등록, 출력된 id 로 config.yaml research.vane.* 갱신)
```

이 PC(원래 작업 PC) 상태: Docker Desktop 꺼짐, Vane 컨테이너 정지, `.env` 는 커밋 안 됨. 여기로 돌아오면 `git pull` 부터.

작업 순서 제안:
1. `/intake` + `/intake/regenerate` 실호출 1회씩 → 멘토링 보기·재생성 품질 확인
2. `config.yaml llm` 을 로컬 Ollama(70B)로 전환 → 같은 입력으로 MiniMax 무료 vs Llama 70B 비교 (PROJECT_CONTEXT 로드맵 "2~3단계에서 반드시")
3. `vane_setup.py` 에 Ollama 제공자 등록 옵션(`--ollama http://host.docker.internal:11434 --model ...`) → `research.vane.enabled: true` → `/research` 1회로 `backend == "vane"` 확인, 통계 페이지(JS) 본문이 읽히는지
4. **조사 단계(조사 B)**: 문항 단위 `/research` → **주제 단위**(시장 통계 / 기존 서비스·경쟁 / 고객 페인포인트 / 유사 BM / 정책·규제 / 논문·보고서)로 검색어 1회 + 사실 추출 1~2회, 각 사실에 `use_for` 문항 태그. 프론트에 "조사 결과" 단계(사실 카드 체크 → 문항별 `references`)
5. **조사 기반 카드(조사 A)**: 인테이크 1차 판정 → customer/problem/alternative/revenue 슬롯은 조사 결과를 넣어 2차 카드 생성, 보기마다 `source` 표시. "조사해서 보기 만들기" 토글
6. 한도 카운터 수정(위 "할 일")
7. LlamaIndex `/ingest`
8. 08-31 절에서 아직 남은 것: **`/verify` 실호출**(Q2 결과의 "자취 3년 차"가 `unsupported_claims` 에 잡히는지) → 검증 결과 UI(노란 문장), `/jobs` 폴링 프론트 전환, 인테이크 결과 저장(새로고침 유실), SearXNG 한국어 엔진(네이버)
9. `styles/story.md`·`plain.md` 다듬기 — 같은 입력으로 logic 과 나란히 실호출 비교하며 (스토리형: 지원자 개인 서사 지어내기 금지 규칙 강화)

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
