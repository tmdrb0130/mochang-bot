"""모두의 창업 신청서 자동 작성기 — FastAPI 백엔드 (MVP 뼈대).

실행 (프로젝트 루트에서):  uvicorn backend.main:app --reload --port 8000
엔드포인트: /health, /generate, /extend, /recommend-field, /generate/dry-run
"""
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from .llm.client import LLMClient, load_config
from .pipeline import assemble, generate

config = load_config()
client = LLMClient(config)

app = FastAPI(title="modoo-writer backend")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class GenerateRequest(BaseModel):
    question_id: str          # q1, q2, q3_1, q3_2, q4_1, q4_2, q7_1, q8, q10
    style: str = "story"      # story | logic | plain
    track: str = "tech"       # tech | local
    idea: str
    is_business: bool = False
    current_item: str = ""
    team: str = "팀원 없음"
    capability: str = ""


@app.get("/health")
async def health():
    return {"ok": True, "model": config["llm"]["model"], "base_url": config["llm"]["base_url"]}


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


class RecommendRequest(BaseModel):
    track: str = "tech"
    idea: str
    is_business: bool = False
    current_item: str = ""
    team: str = "팀원 없음"
    capability: str = ""


@app.post("/recommend-field")
async def recommend_field_endpoint(req: RecommendRequest):
    """Q6 사업 분야 추천 → {"field", "reason", "options"}. 실패 시 field 는 빈 문자열."""
    return await generate.recommend_field(client, req.model_dump())


@app.post("/generate/dry-run")
async def dry_run(req: GenerateRequest):
    """LLM 호출 없이 조립된 프롬프트만 확인 (프롬프트 튜닝용)."""
    try:
        system, user, meta = assemble.build_prompts(req.model_dump())
    except assemble.PromptNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"system": system, "user": user, "meta": meta}
