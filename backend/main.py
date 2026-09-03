"""모두의 창업 신청서 자동 작성기 — FastAPI 백엔드 (MVP 뼈대).

실행 (프로젝트 루트에서):  uvicorn backend.main:app --reload --port 8000
엔드포인트: /health, /models, /intake, /research, /generate, /extend, /verify, /generate/dry-run,
           /jobs/{kind} (제출) · /jobs/{id} (폴링) · /jobs (큐 상태)
Q6(사업 분야)·Q10 공개 여부는 사람이 프론트에서 직접 고른다 — AI 호출 없음.
"""
import asyncio
import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .llm.client import (RESEARCH_USAGE_PATH, LLMClient, LLMError, RateLimited,
                         load_config, research_client_config)
from .llm.jobs import TooManyJobs
from . import finisher as finisher_mod
from . import storage as storage_mod
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

# 아이디어 입력·생성 초안 저장소 (backend/storage.py). 기본 SQLite, config.yaml storage / MOCHANG_DATABASE_URL 로 전환.
storage = storage_mod.Storage.from_config(config)

# 마무리 작업자 (backend/finisher.py): 생성 도중 나간 학생의 빈 문항을 낮은 우선순위로 채운다. config.yaml finisher.
_finisher_settings = finisher_mod.load_settings(config)
finisher_client = LLMClient(finisher_mod.finisher_client_config(config, _finisher_settings))
finisher_client.queue.on_done = timing.job_done


async def _finisher_research(form: dict, question_id: str) -> list[dict]:
    """research 테이블에 없을 때만: 디스크 캐시(7일) → 새 조사. 결과는 다음을 위해 저장해 둔다."""
    found = await research_pipeline.run_research(research_client, researcher, form, question_id, research_cfg)
    await storage.record("research", {**form, "question_id": question_id}, found, None)
    return found.get("facts") or []


finisher = finisher_mod.Finisher(storage, finisher_client, _finisher_settings,
                                 generate_fn=lambda c, f: generate.generate_one(c, f), research_fn=_finisher_research)

@asynccontextmanager
async def lifespan(_: FastAPI):
    await client.queue.start()      # 워커 N개 기동 (config.yaml max_workers)
    if research_client is not client:
        await research_client.queue.start()
    try:
        await asyncio.to_thread(storage.init)
    except Exception as e:          # DB 를 못 열어도 서비스는 뜬다 — 저장만 꺼진다
        logging.getLogger("mochang.storage").warning("저장소를 열 수 없어 저장을 끕니다 (%s): %s", storage.url, e)
        storage.enabled = False
    if storage.enabled:
        finisher.start()            # config.yaml finisher.enabled 가 아니면 아무것도 안 한다
    yield
    await finisher.stop()
    await finisher_client.queue.stop()
    await client.queue.stop()
    if research_client is not client:
        await research_client.queue.stop()
    storage.close()
    research_pipeline.extract_proc.shutdown()   # 본문 추출 프로세스 풀 정리


async def _persisted(kind: str, form: dict, coro, owner: str | None):
    """작업 결과를 돌려주기 전에 DB 에 남긴다. 저장 실패는 storage.record 가 삼킨다 — 결과는 항상 그대로 나간다."""
    result = await coro
    await storage.record(kind, form, result, owner)
    return result


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
    draft_id: str | None = None  # 신청서 한 벌을 묶는 키(프론트 생성 UUID). 없으면 track|idea 해시로 묶어 저장 (storage.py)


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
        # 마무리 작업자 (2026-09-03): 나간 학생의 빈 문항을 채운 수. enabled=false 면 ticks 가 0 에 머문다.
        "finisher": {"enabled": bool(_finisher_settings.get("enabled")), **finisher.stats,
                     "in_progress": len(finisher.in_progress)},
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
    draft_id: str | None = None


async def _with_idea_research(form: dict) -> dict:
    """인테이크·재생성 전에 아이디어 단위 웹 조사를 돌리고 결과를 form["references"] 에 넣는다.
    조사는 조사 전용 모델(로컬 라마)이 하고, 카드 생성은 client(작성 모델)가 한다.
    조사가 실패해도 카드 생성은 계속한다 — 근거 없이 만들 뿐 흐름을 막지 않는다."""
    try:
        found = await research_pipeline.run_idea_research(research_client, researcher, form, research_cfg)
    except Exception as e:                      # 검색 실패·타임아웃 등
        return {"facts": [], "queries": [], "error": str(e)[:200]}
    form["references"] = found.get("facts") or []
    # 진단용(작업 24): 검색 결과 수와 본문 페이지 수를 같이 돌려준다 — 실측에서 '검색 23건 → facts 0' 이 어디서
    # 사라진 것인지(fetch 실패 / 추출 0 / quote 검증 탈락) 볼 수 있게. 기존 필드는 그대로.
    found.setdefault("result_count", 0)
    found["pages_count"] = len(found.get("pages") or [])
    return found


async def _intake_full(form: dict, owner: str | None) -> dict:
    """인테이크 전체: 아이디어 공통 웹 조사 → 카드 생성 → research 요약 첨부. 동기 /intake 와 /jobs/intake 가 같이 쓴다.
    (2026-09-03 이전엔 /jobs/intake 가 조사 없이 카드만 만들어 근거 없는 보기가 나왔다.)

    카드 생성은 research_client(조사 클라이언트)로 돌린다 — 본문 생성이 채운 본문 큐 뒤에서 기다리지 않게(40명 실측: 최대 3분 47초),
    그리고 vLLM 우선순위(extra.priority)가 조사와 같은 '높음'이 되게. 지금은 조사·본문 모델이 같은 Qwen 이라 품질 차이는 없다."""
    found = await _with_idea_research(form)
    await storage.record("idea_research", form, found, owner)     # research 테이블 (question_id = "idea")
    out = await _persisted("intake", form, intake.run_intake(research_client, form), owner)
    out["research"] = {"facts": found.get("facts") or [], "queries": found.get("queries") or [],
                       "backend": found.get("backend"), "cached": found.get("cached"),
                       **({"error": found["error"]} if found.get("error") else {})}
    return out


@app.post("/intake")
async def intake_endpoint(req: IntakeRequest, request: Request):
    """웹 조사(라마) → 슬롯별 확인/부족 판정 + 부족한 슬롯의 '보기 고르기' 카드 생성.
    보기는 조사로 확인된 사실에 근거해 만들어지고, 근거가 있으면 보기마다 source 가 붙는다.
    ready=True 면 프론트는 카드 단계를 건너뛰고 바로 생성."""
    return await _intake_full(req.model_dump(), _client_key(request))


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
    return await _intake_regenerate_full(req.model_dump())


async def _intake_regenerate_full(f: dict) -> dict:
    form = _regenerate_form(f)
    # 재생성 보기도 조사 근거 위에서 만든다. 같은 아이디어면 디스크 캐시가 걸려 추가 조사 비용이 거의 없다.
    await _with_idea_research(form)
    return await intake.regenerate_cards(research_client, form, f["slots"], f.get("seen") or {}, f.get("note") or "", f.get("keep") or {})


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
    draft_id: str | None = None


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
async def generate_endpoint(req: GenerateRequest, request: Request):
    form = req.model_dump()
    try:
        return await _persisted("generate", form, generate.generate_one(client, form), _client_key(request))
    except assemble.PromptNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))


class ExtendRequest(GenerateRequest):
    current: str              # 이미 작성된 글 (이 뒤에 이어 씀)


@app.post("/extend")
async def extend_endpoint(req: ExtendRequest, request: Request):
    """기존 글 뒤에 새 근거·사례·계획만 이어 쓰고, 합친 전체 글을 반환."""
    form = req.model_dump()
    current = form.pop("current")
    try:
        return await _persisted("extend", form, generate.extend_one(client, form, current), _client_key(request))
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
    # 인테이크·조사 작업은 동기 엔드포인트와 완전히 같은 일을 한다 (아이디어 조사 포함).
    "intake": (IntakeRequest, lambda f: _intake_full(f, None)),
    "intake_regenerate": (IntakeRegenerateRequest, lambda f: _intake_regenerate_full(f)),
    "research": (ResearchRequest, lambda f: research_pipeline.run_research(research_client, researcher, f, f["question_id"], research_cfg)),
    "verify": (VerifyRequest, lambda f: verify.verify_text(client, {k: v for k, v in f.items() if k != "text"}, f["question_id"], f["text"])),
}
# 인테이크·조사 작업은 **조사 큐**(research_client.queue, 워커 수 = research.llm.max_workers)에서 돈다.
# 본문 큐에 넣으면 웹 검색·페이지 수집으로 수십 초씩 네트워크를 기다리는 동안 본문 워커를 점유해 생성이 굶고,
# 반대로 생성이 몰리면 인테이크가 그 뒤에 선다(40명 실측: 카드 생성 대기 최대 3분 47초). 큐를 나누면 서로 막지 않는다.
# 조사 큐 워커 안에서 부르는 LLM 호출은 중첩 제출 없이 바로 나간다(jobs.py _in_worker) — 동시 호출 ≤ 워커 × 페이지 동시(3).
_RESEARCH_KINDS = {"intake", "intake_regenerate", "research"}


def _queue_for(kind: str):
    return research_client.queue if kind in _RESEARCH_KINDS else client.queue


def _find_job(job_id: str):
    """두 큐 중 어느 쪽에 있든 찾는다 (job_id 는 uuid 라 겹치지 않는다)."""
    snap = client.queue.get(job_id)
    if snap is None and research_client is not client:
        snap = research_client.queue.get(job_id)
    return snap


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
    owner = _client_key(request)
    q = _queue_for(kind)
    try:
        # 결과가 나오면 DB 에 남기고(아이디어·초안, storage.py) 그대로 job.result 가 된다.
        # intake 는 _intake_full 안에서 이미 저장하므로 여기서 다시 저장하지 않는다(kind 가 저장 대상이 아니어서 무해).
        job = q.submit(lambda: _persisted(kind, form, runner(form), owner) if kind != "intake" else _intake_full(form, owner),
                       kind=kind, owner=owner)
    except TooManyJobs as e:
        raise HTTPException(status_code=429, detail=str(e),
                            headers={"Retry-After": "10"})
    return {"job_id": job.id, "kind": kind, "position": q.position(job.id), "queue": q.stats()}


@app.get("/jobs/{job_id}")
async def get_job(job_id: str):
    """{status: queued|running|done|error, position, result, error}. done 이면 result 에 동기 엔드포인트와 같은 응답."""
    snap = _find_job(job_id)
    if snap is None:
        raise HTTPException(status_code=404, detail="작업이 없습니다 (만료되었거나 잘못된 ID).")
    return snap


@app.get("/jobs")
async def queue_stats(request: Request):
    """본문 큐 전체 상태 + 이 클라이언트(IP)가 지금 점유 중인 작업 수(your_active). 기존 필드는 그대로.
    research 에 조사 큐(인테이크·조사 작업) 상태를 같이 준다."""
    out = {**client.queue.stats(), "your_active": client.queue.active_for(_client_key(request))}
    if research_client is not client:
        out["research"] = {**research_client.queue.stats(), "your_active": research_client.queue.active_for(_client_key(request))}
    return out


@app.get("/drafts/{draft_id}")
async def get_draft(draft_id: str):
    """저장된 초안 한 벌: 입력(아이디어·인테이크 답) + 문항별 생성 이력(오래된 것부터).
    draft_id 는 프론트가 만든 UUID(추측 불가) 또는 서버가 매긴 해시. 목록 조회는 두지 않는다 — 인증이 없어 남의 아이디어가
    보이면 안 되므로 운영자는 scripts/drafts_report.py 로 본다."""
    row = await asyncio.to_thread(storage.get_draft, draft_id) if storage.enabled else None
    if row is None:
        raise HTTPException(status_code=404, detail="저장된 초안이 없습니다.")
    # 프론트 복원용 (2026-09-03): 마무리 작업자가 켜져 있으면 빈 문항이 곧 채워질 수 있으니 폴링하라는 신호.
    row["finisher"] = {"enabled": bool(_finisher_settings.get("enabled")), "in_progress": finisher.in_progress_for(draft_id)}
    return row


@app.post("/generate/dry-run")
async def dry_run(req: GenerateRequest):
    """LLM 호출 없이 조립된 프롬프트만 확인 (프롬프트 튜닝용)."""
    try:
        system, user, meta = assemble.build_prompts(req.model_dump())
    except assemble.PromptNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"system": system, "user": user, "meta": meta}
