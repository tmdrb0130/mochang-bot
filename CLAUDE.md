# 모창봇 (mochang-bot) — Claude Code 작업 지침

중소벤처기업부 「모두의 창업 프로젝트」 도전신청서 자동 작성기. FastAPI 백엔드(`backend/`) + React/Vite 프론트(`frontend/`). 한국어 프로젝트 — 응답·커밋 메시지·주석 모두 한국어.

## 세션 시작 시 먼저 읽을 것 (순서대로)
1. `PROGRESS.md` **맨 위 절** — 직전 세션이 어디까지 했고 다음에 뭘 할지. 여러 PC 를 오가며 작업하므로 이 파일이 인수인계 문서다.
2. `docs/PROJECT_CONTEXT.md` — 설계 결정·문항 분석·로드맵 (필요한 절만).
3. `README.md` — 실행 방법·API 표.

세션을 끝낼 때는 `PROGRESS.md` 맨 위에 새 절(날짜, 한 것, 실호출 여부, 결정, 다음 순서)을 추가하고 커밋·푸시한다.

## 실행·검증
```bash
.venv/Scripts/python -m pytest -q          # 모델·네트워크 호출 0 (전부 mock). 커밋 전 반드시 통과
cd frontend && npx vite build              # 프론트 문법 검증
uvicorn backend.main:app --reload --port 8000   # 백엔드 / 프론트: cd frontend && npm run dev
```
Windows 는 `.venv\Scripts\python`, Linux/mac 은 `.venv/bin/python`. 의존성: `backend/requirements.txt`, `requirements-dev.txt`.

## 규칙
- **프롬프트 문구는 전부 `backend/prompts/*.md`** — 코드에 한국어 프롬프트를 박지 않는다. 새 md 는 `tests/test_prompts.py` 목록에 등록.
- 테스트는 실제 모델을 호출하지 않는다 (`FakeClient` 패턴, `tests/test_intake.py` 참고). 실호출 검증은 사람이 하고 결과를 PROGRESS.md 에 적는다.
- 무료 OpenRouter 한도(하루 50회)를 의식한다: 실호출은 최소로, 호출 수를 늘리는 설계는 PROGRESS "결정 필요"에 적고 진행.
- `.env`(API 키)는 커밋 금지. `backend/.usage.json`, `backend/.cache/` 도 로컬 전용.
- 검색은 Vane(구 Perplexica), RAG 는 LlamaIndex 로 가기로 결정됨 (2026-09-01). Vane 의 채팅 LLM 은 로컬 Ollama 에 붙인다.
