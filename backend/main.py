"""모두의 창업 신청서 자동 작성기 — FastAPI 백엔드 (MVP 뼈대).

실행 (프로젝트 루트에서):  uvicorn backend.main:app --reload --port 8000
엔드포인트: /health, /models, /intake, /research, /generate, /extend, /generate/dry-run
Q6(사업 분야)·Q10 공개 여부는 사람이 프론트에서 직접 고른다 — AI 호출 없음.
"""
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .llm.client import LLMClient, LLMError, RateLimited, load_config
from .pipeline import assemble, generate, intake
from .rag import pipeline as research_pipeline
from .rag.research import researcher_from_config

config = load_config()
client = LLMClient(config)
researcher = researcher_from_config(config)
research_cfg = research_pipeline.ResearchConfig.from_config(config)

app = FastAPI(title="modoo-writer backend")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── 오류 → 프론트가 보여줄 수 있는 한국어 detail 로 ──
@app.exception_handler(RateLimited)
async def _rate_limited(_: Request, e: RateLimited):
    return JSONResponse(status_code=429, content={"detail": str(e), "usage": client.usage.snapshot()})


@app.exception_handler(LLMError)
async def _llm_error(_: Request, e: LLMError):
    return JSONResponse(status_code=502, content={"detail": str(e)})


@app.exception_handler(ValueError)
async def _value_error(_: Request, e: ValueError):
    return JSONResponse(status_code=400, content={"detail": str(e)})


class GenerateRequest(BaseModel):
    question_id: str          # q1, q2, q3_1, q3_2, q4_1, q4_2, q7_1, q8, q10
    style: str = "story"      # story | logic | plain
    track: str = "tech"       # tech | local
    idea: str
    is_business: bool = False
    current_item: str = ""
    team: str = "팀원 없음"
    capability: str = ""
    model: str | None = None  # /models 목록 중 하나. 없으면 config.yaml 기본 모델
    answers: list[dict] | None = None  # 인테이크 카드 답변 [{slot,label,answer,unknown}] — 프롬프트에 사실/가정으로 주입
    references: list[dict] | None = None  # /research 가 돌려준 facts — [웹 참고자료] 섹션으로 주입


@app.get("/health")
async def health():
    return {
        "ok": True,
        "model": client.model,
        "base_url": client.base_url,
        "fallback": client.fallback,
        "usage": client.usage.snapshot(),   # 오늘 요청 수 / 한도 (OpenRouter 가 아니면 limit=None)
    }


@app.get("/models")
async def models():
    """UI 모델 선택용 목록. config.yaml 의 models."""
    return {"default": client.model, "models": client.models or [{"id": client.model, "name": client.model, "note": ""}]}


class IntakeRequest(BaseModel):
    track: str = "tech"
    idea: str
    is_business: bool = False
    current_item: str = ""
    team: str = "팀원 없음"
    capability: str = ""
    model: str | None = None


@app.post("/intake")
async def intake_endpoint(req: IntakeRequest):
    """아이디어를 읽고 슬롯별 확인/부족 판정 + 부족한 슬롯의 '보기 고르기' 카드 생성 (LLM 1회).
    ready=True 면 프론트는 카드 단계를 건너뛰고 바로 생성."""
    return await intake.run_intake(client, req.model_dump())


class ResearchRequest(BaseModel):
    question_id: str
    track: str = "tech"
    idea: str
    is_business: bool = False
    current_item: str = ""
    team: str = "팀원 없음"
    capability: str = ""
    model: str | None = None
    answers: list[dict] | None = None


@app.post("/research")
async def research_endpoint(req: ResearchRequest):
    """아이디어+문항 → 검색어 생성 → 웹 검색 → 본문 추출 → 출처 있는 사실 JSON (모델 호출 2회).
    응답의 facts 를 /generate 의 references 로 넘기면 [웹 참고자료] 섹션으로 주입된다."""
    form = req.model_dump()
    try:
        return await research_pipeline.run_research(client, researcher, form, req.question_id, research_cfg)
    except assemble.PromptNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.post("/generate")
async def generate_endpoint(req: GenerateRequest):
    try:
        return await generate.generate_one(client, req.model_dump())
    except assemble.PromptNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))


class ExtendRequest(GenerateRequest):
    current: str              # 이미 작성된 글 (이 뒤에 이어 씀)


@app.post("/extend")
async def extend_endpoint(req: ExtendRequest):
    """기존 글 뒤에 새 근거·사례·계획만 이어 쓰고, 합친 전체 글을 반환."""
    form = req.model_dump()
    current = form.pop("current")
    try:
        return await generate.extend_one(client, form, current)
    except assemble.PromptNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.post("/generate/dry-run")
async def dry_run(req: GenerateRequest):
    """LLM 호출 없이 조립된 프롬프트만 확인 (프롬프트 튜닝용)."""
    try:
        system, user, meta = assemble.build_prompts(req.model_dump())
    except assemble.PromptNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"system": system, "user": user, "meta": meta}
