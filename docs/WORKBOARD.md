# 작업 상태판 (WORKBOARD) — 2026-09-02

> 실시간 조율 문서. 규칙은 `docs/TEAMWORK.md`.
> 각 세션은 **자기 로그 절에만** 쓴다. 배정표·조율 필요 절은 세션1이 관리한다.

## 배정표

| # | 작업 | 담당 | 상태 | 근거 |
|---|---|---|---|---|
| 1 | **V1 — 프론트를 작업 큐 API 로 전환** | 세션2 | 대기 | PROGRESS "다음 순서" 1순위. V5·V6·V9 가 같이 풀림 |
| 2 | **V4 — 사용자별 동시 작업 제한** | 세션3 | 대기 | 한 명이 27건으로 큐 독점 가능 |
| 3 | 모델 결정 (라마 이물 82%·출처 날조) | **사용자** | 결정 대기 | PROGRESS 2절 — 선택지 3개 |
| 4 | V7 — SSH 터널 NSSM 서비스화 | 보류 | 승인 대기 | 관리자 권한·키 인증 확인 필요 (PROJECT_CONTEXT 12절에 절차 있음) |
| 5 | 조사 재사용(아이디어당 1회) | 세션3 | 후순위 | #2 끝난 뒤 |

### 작업 1 상세 — V1 (세션2)

- 현재: `frontend/src/api.js` 의 `generate()`/`extend()` 가 동기 `POST /generate` 를 부르고 3분간 연결을 붙잡음.
- 목표: `POST /jobs/generate` → 응답의 job id 로 `GET /jobs/{job_id}` 폴링(2~3초 간격) 전환.
  - 큐가 주는 `position`(대기 순번)을 화면에 표시 (V5)
  - job id 를 `sessionStorage` 에 보관해 새로고침해도 이어받기 (V6)
- 건드릴 파일: `frontend/src/api.js`, `frontend/src/App.jsx`. 백엔드는 완성돼 있음 — **수정 금지**.
  응답 스키마는 `backend/main.py:261~284`, `backend/llm/jobs.py` 를 읽고 맞추기.
- 검증: `npm run dev` (:5173) 로 로컬 확인 → `npx vite build` 통과.
- ⚠️ **`frontend/dist` 재빌드는 즉시 공개 배포다** (nginx 가 dist 를 그대로 서빙).
  개발 중엔 dev 서버로만 확인하고, dist 빌드·반영은 사용자 확인 후.

### 작업 2 상세 — V4 (세션3)

- 목표: 클라이언트당 동시 작업 수 제한 (인증이 없으므로 우선 IP 기준, 예: 동시 3건).
  초과 시 429 + 안내 메시지.
- 건드릴 파일: `backend/llm/jobs.py`, `backend/main.py`, `tests/test_jobs.py`(테스트 추가).
- 제약: `POST /jobs/{kind}` / `GET /jobs/{job_id}` / `GET /jobs` **응답 필드 삭제·개명 금지** (세션2가 의존).
  필드 추가는 허용 — 추가하면 아래 "조율 필요"에 공지.
- 검증: `.venv/Scripts/python -m pytest -q` 전체 통과 (현재 87 passed). 실모델 호출 없이 mock 으로.
- 참고: 개발용 백엔드가 필요하면 **:8001** 로 (`uvicorn backend.main:app --port 8001`). :8000 은 운영 중.

## 조율 필요 / 결정 대기

- [ ] **모델 결정** (사용자): ① OpenRouter $10 충전 ② 라마+후처리 ③ 하이브리드 — PROGRESS.md 2절 참고
- [ ] **SSH 터널 서비스화** (사용자 승인): nssm install 은 관리자 권한 필요, 키 인증 전제
- [ ] dist 재빌드(=배포) 시점 (사용자): 세션2 V1 완료 후

## 세션 로그

> 형식: `- HH:MM 시작|진행|완료|막힘 — 내용 (커밋 해시)`

### 세션1 (조율)

- 2026-09-02 세션 시작. TEAMWORK.md·WORKBOARD.md 생성, 작업 1·2 배정.

### 세션2 (프론트)

- (아직 없음)

### 세션3 (백엔드)

- (아직 없음)
