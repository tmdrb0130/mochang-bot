"""모두의 창업 신청서 자동 작성기 — FastAPI 백엔드 (MVP 뼈대).

실행 (프로젝트 루트에서):  uvicorn backend.main:app --reload --port 8000
엔드포인트: /health, /models, /intake, /research, /generate, /extend, /verify, /generate/dry-run,
           /jobs/{kind} (제출) · /jobs/{id} (폴링) · /jobs (큐 상태)
Q6(사업 분야)·Q10 공개 여부는 사람이 프론트에서 직접 고른다 — AI 호출 없음.
"""
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .llm.client import (RESEARCH_USAGE_PATH, LLMClient, LLMError, RateLimited,
                         load_config, research_client_config)
from .llm.jobs import TooManyJobs
from . import timing
from .pipeline import assemble, generate, intake, verify
from .rag import pipeline as research_pipeline
from .rag.research import researcher_from_config

config = load_config()
client = LLMClient(config)

# 조사(검색어 생성·사실 추출) 전용 클라이언트. config.yaml 의 research.llm 이 있으면 그 모델(로컬 70B)을 쓰고,
# 없으면 주 클라이언트를 그대로 쓴다. 지원서 본문 작성은 항상 client(= llm:) 가 한다.
_research_llm_config = research_client_config(config)
research_client = (
    LLMClient(_research_llm_config, usage_path=RESEARCH_USAGE_PATH, pin_model=True)
    if _research_llm_config else client
)
researcher = researcher_from_config(config)
research_cfg = research_pipeline.ResearchConfig.from_config(config)

@asynccontextmanager
async def lifespan(_: FastAPI):
    await client.queue.start()      # 워커 N개 기동 (config.yaml max_workers)
    if research_client is not client:
        await research_client.queue.start()
    yield
    await client.queue.stop()
    if research_client is not client:
        await research_client.queue.stop()


app = FastAPI(title="modoo-writer backend", lifespan=lifespan)


@app.middleware("http")
async def _record_timing(request: Request, call_next):
    """모든 요청의 소요 시간을 backend/.timing.jsonl 에 남긴다 (사용자 대기 시간 추적용)."""
    t0 = time.perf_counter()
    response = await call_next(request)
    ms = round((time.perf_counter() - t0) * 1000, 1)
    # 폴링(/jobs/{id})은 초당 여러 번 들어와 로그를 뒤덮으므로 느린 것만 남긴다.
    path = request.url.path
    if not (path.startswith("/jobs/") and request.method == "GET" and ms < 500):
        timing.log("http", method=request.method, path=path, status=response.status_code, ms=ms)
    return response


# 작업이 끝날 때마다 대기 시간 / 실행 시간을 기록한다.
client.queue.on_done = timing.job_done
if research_client is not client:
    research_client.queue.on_done = timing.job_done
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
        # 조사 단계가 쓰는 모델 (본문 작성 모델과 다를 수 있음)
        "research_model": research_client.model,
        "research_base_url": research_client.base_url,
        "fallback": client.fallback,
        "usage": client.usage.snapshot(),   # 오늘 요청 수 / 한도 (OpenRouter 가 아니면 limit=None)
        "queue": client.queue.stats(),      # 동시성 층 상태 (워커 수, 대기, 실행 중)
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


async def _with_idea_research(form: dict) -> dict:
    """인테이크·재생성 전에 아이디어 단위 웹 조사를 돌리고 결과를 form["references"] 에 넣는다.
    조사는 조사 전용 모델(로컬 라마)이 하고, 카드 생성은 client(작성 모델)가 한다.
    조사가 실패해도 카드 생성은 계속한다 — 근거 없이 만들 뿐 흐름을 막지 않는다."""
    try:
        found = await research_pipeline.run_idea_research(research_client, researcher, form, research_cfg)
    except Exception as e:                      # 검색 실패·타임아웃 등
        return {"facts": [], "queries": [], "error": str(e)[:200]}
    form["references"] = found.get("facts") or []
    return found


@app.post("/intake")
async def intake_endpoint(req: IntakeRequest):
    """웹 조사(라마) → 슬롯별 확인/부족 판정 + 부족한 슬롯의 '보기 고르기' 카드 생성.
    보기는 조사로 확인된 사실에 근거해 만들어지고, 근거가 있으면 보기마다 source 가 붙는다.
    ready=True 면 프론트는 카드 단계를 건너뛰고 바로 생성."""
    form = req.model_dump()
    found = await _with_idea_research(form)
    out = await intake.run_intake(client, form)
    out["research"] = {"facts": found.get("facts") or [], "queries": found.get("queries") or [],
                       "backend": found.get("backend"), "cached": found.get("cached"),
                       **({"error": found["error"]} if found.get("error") else {})}
    return out


class IntakeRegenerateRequest(IntakeRequest):
    slots: list[str]                          # 다시 만들 슬롯 (예: ["problem"])
    seen: dict[str, list[str]] | None = None  # {slot: [이미 보여준 보기 label]} — 같은 보기 금지
    note: str = ""                            # 지원자 메모 ("너무 일반적이에요" 등, 선택)
    keep: dict[str, list[dict]] | None = None # {slot: [{label, hint}]} — 이미 고른 보기, 그대로 유지하고 나머지만 교체
    answers: list[dict] | None = None         # 다른 카드의 답 — 그와 어울리는 보기를 내도록


def _regenerate_form(f: dict) -> dict:
    return {k: v for k, v in f.items() if k not in ("slots", "seen", "note", "keep")}


@app.post("/intake/regenerate")
async def intake_regenerate_endpoint(req: IntakeRegenerateRequest):
    """지원자가 '보기가 안 맞아요'라고 한 슬롯의 카드만 다시 생성 (LLM 1회). → {cards, slots, model, error?}
    프론트는 돌아온 cards 를 슬롯 기준으로 기존 카드와 바꿔 끼운다."""
    f = req.model_dump()
    form = _regenerate_form(f)
    # 재생성 보기도 조사 근거 위에서 만든다. 같은 아이디어면 디스크 캐시가 걸려 추가 조사 비용이 거의 없다.
    await _with_idea_research(form)
    return await intake.regenerate_cards(client, form, f["slots"], f.get("seen") or {}, f.get("note") or "", f.get("keep") or {})


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
        return await research_pipeline.run_research(research_client, researcher, form, req.question_id, research_cfg)
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


class VerifyRequest(GenerateRequest):
    text: str                 # 검증할 생성문 (사용자가 고친 글도 가능)


@app.post("/verify")
async def verify_endpoint(req: VerifyRequest):
    """문항 md 의 [필수 요소] 포함 여부를 모델이 판정 → {items, missing, unsupported_claims, format_issues, score}.
    evidence 가 글에 없으면 present 를 false 로 뒤집는다 (모델 판정 재검증). 모델 호출 1회."""
    form = req.model_dump()
    text = form.pop("text")
    try:
        return await verify.verify_text(client, form, req.question_id, text)
    except assemble.PromptNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))


# ── 비동기 작업: 제출 → 작업 ID → 폴링 ──
# 긴 작업(문항 생성 30~60초, 조사 1~2분)을 HTTP 연결을 붙잡지 않고 처리. 대기 순번(position)을 보여줄 수 있다.

_JOB_KINDS = {
    "generate": (GenerateRequest, lambda f: generate.generate_one(client, f)),
    "extend": (ExtendRequest, lambda f: generate.extend_one(client, {k: v for k, v in f.items() if k != "current"}, f["current"])),
    "intake": (IntakeRequest, lambda f: intake.run_intake(client, f)),
    "intake_regenerate": (IntakeRegenerateRequest, lambda f: intake.regenerate_cards(
        client, _regenerate_form(f), f["slots"], f.get("seen") or {}, f.get("note") or "", f.get("keep") or {})),
    # 조사는 조사 전용 클라이언트로 — 동기 /research 엔드포인트와 같게 맞춘다(작업 22).
    # 지금은 둘 다 같은 로컬 라마라 결과는 같지만, 모델을 나누면 경로마다 조사 품질이 달라진다.
    "research": (ResearchRequest, lambda f: research_pipeline.run_research(research_client, researcher, f, f["question_id"], research_cfg)),
    "verify": (VerifyRequest, lambda f: verify.verify_text(client, {k: v for k, v in f.items() if k != "text"}, f["question_id"], f["text"])),
}


def _client_key(request: Request) -> str:
    """동시 작업 제한용 클라이언트 식별자. 인증이 없으므로 IP 를 쓴다.

    공개 배포는 nginx(50001) 뒤라 request.client.host 가 전부 127.0.0.1 이 된다 →
    프록시가 붙여 주는 X-Forwarded-For 의 첫 항목(원 클라이언트)을 우선 본다."""
    xff = request.headers.get("x-forwarded-for")
    if xff:
        first = xff.split(",")[0].strip()
        if first:
            return first
    return request.client.host if request.client else "unknown"


@app.post("/jobs/{kind}")
async def submit_job(kind: str, body: dict, request: Request):
    """kind ∈ generate|extend|intake|intake_regenerate|research|verify. 본문은 해당 동기 엔드포인트와 같다. → {job_id, position}
    같은 IP 가 동시에 가질 수 있는 작업은 config.yaml max_jobs_per_client 건까지 — 넘으면 429."""
    if kind not in _JOB_KINDS:
        raise HTTPException(status_code=404, detail=f"알 수 없는 작업 종류: {kind}")
    model_cls, runner = _JOB_KINDS[kind]
    form = model_cls(**body).model_dump()
    try:
        job = client.queue.submit(lambda: runner(form), kind=kind, owner=_client_key(request))
    except TooManyJobs as e:
        raise HTTPException(status_code=429, detail=str(e),
                            headers={"Retry-After": "10"})
    return {"job_id": job.id, "kind": kind, "position": client.queue.position(job.id), "queue": client.queue.stats()}


@app.get("/jobs/{job_id}")
async def get_job(job_id: str):
    """{status: queued|running|done|error, position, result, error}. done 이면 result 에 동기 엔드포인트와 같은 응답."""
    snap = client.queue.get(job_id)
    if snap is None:
        raise HTTPException(status_code=404, detail="작업이 없습니다 (만료되었거나 잘못된 ID).")
    return snap


@app.get("/jobs")
async def queue_stats(request: Request):
    """큐 전체 상태 + 이 클라이언트(IP)가 지금 점유 중인 작업 수(your_active). 기존 필드는 그대로."""
    return {**client.queue.stats(), "your_active": client.queue.active_for(_client_key(request))}


@app.post("/generate/dry-run")
async def dry_run(req: GenerateRequest):
    """LLM 호출 없이 조립된 프롬프트만 확인 (프롬프트 튜닝용)."""
    try:
        system, user, meta = assemble.build_prompts(req.model_dump())
    except assemble.PromptNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"system": system, "user": user, "meta": meta}
