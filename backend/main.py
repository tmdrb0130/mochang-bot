"""모두의 창업 신청서 자동 작성기 — FastAPI 백엔드 (MVP 뼈대).

실행 (프로젝트 루트에서):  uvicorn backend.main:app --reload --port 8000
엔드포인트: /health, /models, /intake, /research, /generate, /extend, /verify, /translate, /generate/dry-run,
           /jobs/{kind} (제출) · /jobs/{id} (폴링) · /jobs (큐 상태)
Q6(사업 분야)·Q10 공개 여부는 사람이 프론트에서 직접 고른다 — AI 호출 없음.
"""
import asyncio
import logging
import os
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .llm.client import (RESEARCH_USAGE_PATH, LLMClient, LLMError, RateLimited,
                         load_config, research_client_config)
from .llm.jobs import TooManyJobs
from . import finisher as finisher_mod
from . import storage as storage_mod
from . import timing
from .pipeline import assemble, generate, intake, translate, verify
from .rag import pipeline as research_pipeline
from .rag.research import researcher_from_config

config = load_config()
client = LLMClient(config)

# ── 접근·상한 설정 (2026-09-04, config.yaml 주석 참고) ──
TRUSTED_PROXIES = set(str(x) for x in (config.get("trusted_proxies") or ["127.0.0.1", "::1"]))
# 동기 엔드포인트(/intake /research /generate …)는 프론트가 쓰지 않는다 → 운영은 404. 환경변수로 개발·테스트에서만 연다.
SYNC_ENDPOINTS = os.environ.get("MOCHANG_SYNC_ENDPOINTS", "").strip().lower() in ("1", "true", "yes") \
    or bool(config.get("sync_endpoints", False))
MAX_JOBS_PER_IP = int(config.get("max_jobs_per_ip") or 0)
MAX_INTAKES_PER_IP_HOUR = int(config.get("max_intakes_per_ip_hour") or 0)
_tr = config.get("translate") or {}
TRANSLATE_MAX_PER_CLIENT = int(_tr.get("max_jobs_per_client") or 10)
TRANSLATE_GLOBAL_LIMIT = int(_tr.get("global_limit") or 0)
TRANSLATE_EXTRA = {"priority": int(_tr["priority"])} if _tr.get("priority") is not None else None

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
    # 벡터DB 를 백그라운드에서 미리 연다 (2026-09-04): 열기에 30초 넘게 걸려서, 첫 조사 때 열면 그 사용자가 기다린다.
    # 스레드에서 돌므로 이벤트 루프를 막지 않고, 로딩 중 조사가 들어와도 같은 락에서 순서대로 기다린다.
    # 테스트는 건너뛴다(conftest 가 환경변수를 세운다) — 운영 저장소를 열면 테스트가 30초씩 느려진다.
    warm = None
    if not os.environ.get("MOCHANG_SKIP_VECTORSTORE_WARMUP"):
        warm = asyncio.create_task(research_pipeline.open_vector_store(research_cfg))
    yield
    if warm is not None:
        warm.cancel()
    await finisher.stop()
    await finisher_client.queue.stop()
    await client.queue.stop()
    if research_client is not client:
        await research_client.queue.stop()
    storage.close()
    research_pipeline.extract_proc.shutdown()   # 본문 추출 프로세스 풀 정리


async def _persisted(kind: str, form: dict, coro, owner: str | None, test: bool = False):
    """작업 결과를 돌려주기 전에 DB 에 남긴다. 저장 실패는 storage.record 가 삼킨다 — 결과는 항상 그대로 나간다.
    test=True(헤더 X-Mochang-Test) 면 서비스 DB 대신 백업 DB 에만 남긴다 (2026-09-03).
    저장됐으면 응답에 draft_key(초안 접근 열쇠)를 붙인다 — 프론트가 저장본에 보관해 이후 요청·복원에 실어 보낸다 (2026-09-04)."""
    result = await coro
    info = await storage.record(kind, form, result, owner, test=test)
    if isinstance(result, dict) and info and info.get("draft_key"):
        result["draft_key"] = info["draft_key"]
    return result


def _require_sync():
    """동기 엔드포인트 게이트 — 운영(sync_endpoints: false)에서는 404. 큐(/jobs)만이 상한·저장 규칙을 다 거친다."""
    if not SYNC_ENDPOINTS:
        raise HTTPException(status_code=404, detail="Not Found")


def _is_test(request: Request) -> bool:
    """부하 테스트·E2E 스크립트가 붙이는 헤더. 실제 사용자(브라우저)는 안 붙이므로 서비스 DB 에 남는다."""
    v = str(request.headers.get("x-mochang-test") or "").strip().lower()
    return v not in ("", "0", "false", "no")


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
    style: str = "logic"      # logic | story | plain (운영 기본은 논리·근거형 — 프론트도 항상 logic 을 보낸다)
    track: str = "tech"       # tech | local
    idea: str
    is_business: bool = False
    current_item: str = ""
    team: str = "팀원 없음"
    capability: str = ""
    model: str | None = None  # /models 목록 중 하나. 없으면 config.yaml 기본 모델
    answers: list[dict] | None = None  # 인테이크 카드 답변 [{slot,label,answer,unknown}] — 프롬프트에 사실/가정으로 주입
    references: list[dict] | None = None  # /research 가 돌려준 facts — [웹 참고자료] 섹션으로 주입
    draft_id: str | None = None  # 신청서 한 벌을 묶는 키(프론트 생성 UUID). 형식 밖이면 저장하지 않는다 (storage.py)
    draft_key: str | None = None  # 초안 접근 열쇠 — 첫 저장 응답의 draft_key. 없거나 틀리면 그 초안은 갱신되지 않는다 (2026-09-04)


_llm_ping: dict = {"at": 0.0, "ok": None}


async def _llm_reachable() -> bool | None:
    """모델 서버(SSH 터널 → vLLM)가 응답하는지. 30초 캐시 — /health 는 프론트가 작업마다 부른다 (2026-09-04)."""
    now = time.monotonic()
    if now - _llm_ping["at"] < 30:
        return _llm_ping["ok"]
    ok: bool | None
    try:
        async with httpx.AsyncClient(timeout=2.0) as c:
            r = await c.get(client.base_url.rstrip("/") + "/models")
        ok = r.status_code < 500
    except Exception:
        ok = False
    _llm_ping.update(at=now, ok=ok)
    return ok


@app.get("/health")
async def health():
    return {
        "ok": True,
        "model": client.model,
        "base_url": client.base_url,
        # 모델 서버 연결 (2026-09-04): False 면 SSH 터널(30801) 또는 vLLM 이 죽은 것 — 생성이 전부 실패한다
        "llm_reachable": await _llm_reachable(),
        # 저장소 (2026-09-04): 삼킨 저장 실패 수. 0 이 아니면 timing.jsonl 의 storage_error 줄과 scripts/timing_report.py 로 본다
        "storage": {"enabled": bool(storage.enabled and storage.engine is not None),
                    "backup": bool(storage.backup is not None and storage.backup.engine is not None),
                    "error_count": storage.error_count, "last_error": storage.last_error},
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
    draft_key: str | None = None


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


async def _intake_full(form: dict, owner: str | None, test: bool = False) -> dict:
    """인테이크 전체: 아이디어 공통 웹 조사 → 카드 생성 → research 요약 첨부. 동기 /intake 와 /jobs/intake 가 같이 쓴다.
    (2026-09-03 이전엔 /jobs/intake 가 조사 없이 카드만 만들어 근거 없는 보기가 나왔다.)

    카드 생성은 research_client(조사 클라이언트)로 돌린다 — 본문 생성이 채운 본문 큐 뒤에서 기다리지 않게(40명 실측: 최대 3분 47초),
    그리고 vLLM 우선순위(extra.priority)가 조사와 같은 '높음'이 되게. 지금은 조사·본문 모델이 같은 Qwen 이라 품질 차이는 없다."""
    found = await _with_idea_research(form)
    # 쓰기 순서 주의 (2026-09-04 스모크 테스트에서 잡은 버그): **클라이언트가 받는 쓰기(intake)를 먼저** 한다.
    # 예전 순서(idea_research 먼저)에서는 그 INSERT 가 열쇠(owner_token)를 발급하는데 응답으로 나가지 않아,
    # 바로 뒤 intake 쓰기가 "열쇠 없음"으로 거부되고 브라우저는 열쇠를 영영 못 받았다 → 그 초안의 이후 저장이 전부 막혔다.
    # 지금 순서면 (1) 첫 응답에 draft_key 가 실리고 (2) run_intake 가 실패하면 초안 행 자체가 안 생겨,
    # 프론트가 생성으로 넘어갈 때 그 요청이 INSERT 하며 열쇠를 받는다.
    out = await _persisted("intake", form, intake.run_intake(research_client, form), owner, test)
    if isinstance(out, dict) and out.get("draft_key"):
        form["draft_key"] = out["draft_key"]        # 방금 발급된 열쇠로 아래 조사 저장이 통과하게
    await storage.record("idea_research", form, found, owner, test=test)     # research 테이블 (question_id = "idea")
    out["research"] = {"facts": found.get("facts") or [], "queries": found.get("queries") or [],
                       "backend": found.get("backend"), "cached": found.get("cached"),
                       **({"error": found["error"]} if found.get("error") else {})}
    return out


@app.post("/intake", dependencies=[Depends(_require_sync)])
async def intake_endpoint(req: IntakeRequest, request: Request):
    """웹 조사(라마) → 슬롯별 확인/부족 판정 + 부족한 슬롯의 '보기 고르기' 카드 생성.
    보기는 조사로 확인된 사실에 근거해 만들어지고, 근거가 있으면 보기마다 source 가 붙는다.
    ready=True 면 프론트는 카드 단계를 건너뛰고 바로 생성."""
    return await _intake_full(req.model_dump(), _client_key(request), _is_test(request))


class IntakeRegenerateRequest(IntakeRequest):
    slots: list[str]                          # 다시 만들 슬롯 (예: ["problem"])
    seen: dict[str, list[str]] | None = None  # {slot: [이미 보여준 보기 label]} — 같은 보기 금지
    note: str = ""                            # 지원자 메모 ("너무 일반적이에요" 등, 선택)
    keep: dict[str, list[dict]] | None = None # {slot: [{label, hint}]} — 이미 고른 보기, 그대로 유지하고 나머지만 교체
    answers: list[dict] | None = None         # 다른 카드의 답 — 그와 어울리는 보기를 내도록


def _regenerate_form(f: dict) -> dict:
    return {k: v for k, v in f.items() if k not in ("slots", "seen", "note", "keep")}


@app.post("/intake/regenerate", dependencies=[Depends(_require_sync)])
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
    draft_key: str | None = None


@app.post("/research", dependencies=[Depends(_require_sync)])
async def research_endpoint(req: ResearchRequest):
    """아이디어+문항 → 검색어 생성 → 웹 검색 → 본문 추출 → 출처 있는 사실 JSON (모델 호출 2회).
    응답의 facts 를 /generate 의 references 로 넘기면 [웹 참고자료] 섹션으로 주입된다."""
    form = req.model_dump()
    try:
        return await research_pipeline.run_research(research_client, researcher, form, req.question_id, research_cfg)
    except assemble.PromptNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.post("/generate", dependencies=[Depends(_require_sync)])
async def generate_endpoint(req: GenerateRequest, request: Request):
    form = req.model_dump()
    try:
        return await _persisted("generate", form, generate.generate_one(client, form), _client_key(request), _is_test(request))
    except assemble.PromptNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))


class ExtendRequest(GenerateRequest):
    current: str              # 이미 작성된 글 (이 뒤에 이어 씀)


@app.post("/extend", dependencies=[Depends(_require_sync)])
async def extend_endpoint(req: ExtendRequest, request: Request):
    """기존 글 뒤에 새 근거·사례·계획만 이어 쓰고, 합친 전체 글을 반환."""
    form = req.model_dump()
    current = form.pop("current")
    try:
        return await _persisted("extend", form, generate.extend_one(client, form, current), _client_key(request), _is_test(request))
    except assemble.PromptNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))


class VerifyRequest(GenerateRequest):
    text: str                 # 검증할 생성문 (사용자가 고친 글도 가능)


@app.post("/verify", dependencies=[Depends(_require_sync)])
async def verify_endpoint(req: VerifyRequest):
    """문항 md 의 [필수 요소] 포함 여부를 모델이 판정 → {items, missing, unsupported_claims, format_issues, score}.
    evidence 가 글에 없으면 present 를 false 로 뒤집는다 (모델 판정 재검증). 모델 호출 1회."""
    form = req.model_dump()
    text = form.pop("text")
    try:
        return await verify.verify_text(client, form, req.question_id, text)
    except assemble.PromptNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))


class TranslateRequest(BaseModel):
    """외국인 지원자용 읽기 번역 (2026-09-03). 신청서는 한국어로만 만들고 제출도 한국어 — 번역은 이해용이다."""
    lang: str                          # en | zh | ja (pipeline/translate.py LANG_NAMES)
    texts: list[str]                   # 한국어 원문 목록. 하나면 평문 모드(문항 본문), 여럿이면 목록 모드(카드 문구)
    model: str | None = None


@app.post("/translate", dependencies=[Depends(_require_sync)])
async def translate_endpoint(req: TranslateRequest):
    """→ {lang, translations: [원문과 같은 길이], model}. 실패한 항목은 ""."""
    return await translate.translate_texts(research_client, req.texts, req.lang, req.model, extra=TRANSLATE_EXTRA)


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
    # 번역은 조사 클라이언트(워커 80)로 — 외국인이 화면을 읽으려고 기다리는 대화형 작업이라 본문 생성 뒤에 세우지 않는다.
    # vLLM 우선순위는 config.yaml translate.priority(50): 조사·인테이크(0)보다 뒤, 생성(100)보다 앞 (2026-09-04).
    "translate": (TranslateRequest, lambda f: translate.translate_texts(research_client, f["texts"], f["lang"], f.get("model"),
                                                                        extra=TRANSLATE_EXTRA)),
}
# 인테이크·조사 작업은 **조사 큐**(research_client.queue, 워커 수 = research.llm.max_workers)에서 돈다.
# 본문 큐에 넣으면 웹 검색·페이지 수집으로 수십 초씩 네트워크를 기다리는 동안 본문 워커를 점유해 생성이 굶고,
# 반대로 생성이 몰리면 인테이크가 그 뒤에 선다(40명 실측: 카드 생성 대기 최대 3분 47초). 큐를 나누면 서로 막지 않는다.
# 조사 큐 워커 안에서 부르는 LLM 호출은 중첩 제출 없이 바로 나간다(jobs.py _in_worker) — 동시 호출 ≤ 워커 × 페이지 동시(3).
_RESEARCH_KINDS = {"intake", "intake_regenerate", "research", "translate"}
# 번역은 조사·생성이 초안당 슬롯 3개를 다 쓰는 동안에도 들어와야 한다(카드가 뜨자마자 카드 번역, 문항이 끝나자마자 본문 번역).
# 예전엔 제한 예외(무제한)였다 → 2026-09-04: 초안당 translate.max_jobs_per_client(10) + 서버 전체 translate.global_limit(30).
# 정상 사용은 카드+본문을 합쳐도 동시 10건을 넘지 않고, 프론트는 429 를 받으면 5초 뒤 재시도한다.


def _queue_for(kind: str):
    return research_client.queue if kind in _RESEARCH_KINDS else client.queue


def _find_job(job_id: str):
    """두 큐 중 어느 쪽에 있든 찾는다 (job_id 는 uuid 라 겹치지 않는다)."""
    snap = client.queue.get(job_id)
    if snap is None and research_client is not client:
        snap = research_client.queue.get(job_id)
    return snap


def _client_key(request: Request) -> str:
    """클라이언트 IP. 인증이 없으므로 IP 를 남용 추적·IP 천장·옛 초안 접근(과도기)에 쓴다.

    공개 배포는 nginx(50001) 뒤라 request.client.host 가 전부 127.0.0.1 이다. nginx 는
    `X-Forwarded-For $proxy_add_x_forwarded_for` 로 **실제 접속 IP 를 맨 뒤에 덧붙이므로** 신뢰 프록시(trusted_proxies)에서 온
    요청은 XFF 의 **마지막** 항목을 쓴다 — 클라이언트가 앞에 붙인 가짜 XFF 는 무시된다 (2026-09-04, 예전엔 첫 항목을 믿었다).
    프록시를 거치지 않은 요청(개발 서버·직접 접속)은 접속 IP 그대로."""
    peer = request.client.host if request.client else "unknown"
    if peer in TRUSTED_PROXIES:
        xff = request.headers.get("x-forwarded-for") or ""
        parts = [p.strip() for p in xff.split(",") if p.strip()]
        if parts:
            return parts[-1]
    return peer


def _job_owner(form: dict, ip: str) -> str:
    """동시 작업 제한(max_jobs_per_client)의 단위 — **초안(draft_id)**. 강의실처럼 같은 공인 IP 40명이 서로를 막지 않게 (2026-09-04).
    draft_id 가 없거나 형식 밖이면 IP. 접두어로 두 종류를 구분한다."""
    did = storage_mod.draft_id_for(form)
    return f"d:{did}" if did else f"ip:{ip}"


# IP 당 시간당 인테이크(= 새 초안 시작) 수 — 초안 id 를 무한히 만들어 초안 단위 제한을 우회하는 봇을 막는다. 메모리에만 둔다.
_intake_times: dict[str, deque] = defaultdict(deque)


def _check_intake_rate(ip: str, now: float | None = None) -> None:
    if not MAX_INTAKES_PER_IP_HOUR:
        return
    now = time.time() if now is None else now
    dq = _intake_times[ip]
    while dq and now - dq[0] > 3600:
        dq.popleft()
    if len(dq) >= MAX_INTAKES_PER_IP_HOUR:
        raise TooManyJobs(len(dq), MAX_INTAKES_PER_IP_HOUR, "ip")
    dq.append(now)


@app.post("/jobs/{kind}")
async def submit_job(kind: str, body: dict, request: Request):
    """kind ∈ generate|extend|intake|intake_regenerate|research|verify|translate. 본문은 해당 동기 엔드포인트와 같다. → {job_id, position}
    같은 IP 가 동시에 가질 수 있는 작업은 config.yaml max_jobs_per_client 건까지 — 넘으면 429."""
    if kind not in _JOB_KINDS:
        raise HTTPException(status_code=404, detail=f"알 수 없는 작업 종류: {kind}")
    model_cls, runner = _JOB_KINDS[kind]
    form = model_cls(**body).model_dump()
    ip = _client_key(request)                 # 저장(owner)·IP 천장·옛 초안 접근용
    owner = _job_owner(form, ip)              # 동시 작업 제한 단위 (초안 → 없으면 IP)
    test = _is_test(request)
    q = _queue_for(kind)
    try:
        if kind == "intake":
            _check_intake_rate(ip)
        per_owner = TRANSLATE_MAX_PER_CLIENT if kind == "translate" else None      # None = 큐 기본값(max_jobs_per_client)
        per_kind = TRANSLATE_GLOBAL_LIMIT if kind == "translate" else 0
        # 결과가 나오면 DB 에 남기고(아이디어·초안, storage.py) 그대로 job.result 가 된다.
        # intake 는 _intake_full 안에서 이미 저장하므로 여기서 다시 저장하지 않는다(kind 가 저장 대상이 아니어서 무해).
        job = q.submit(lambda: _persisted(kind, form, runner(form), ip, test) if kind != "intake" else _intake_full(form, ip, test),
                       kind=kind, owner=owner, ip=ip, max_per_owner=per_owner, max_per_ip=MAX_JOBS_PER_IP, max_per_kind=per_kind)
    except TooManyJobs as e:
        # 어느 상한에 걸렸는지 남긴다 (2026-09-04): 부하 테스트에서 owner(초안)·ip(천장)·kind(번역 전역) 를 나눠 봐야
        # 값을 어디서 올릴지 정할 수 있다. scripts/timing_report.py "[동시 상한 429]" 절.
        timing.log("limit", kind=kind, scope=e.scope, active=e.active, limit=e.limit)
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
    ip = _client_key(request)
    out = {**client.queue.stats(), "your_active": client.queue.active_ip(ip)}
    if research_client is not client:
        out["research"] = {**research_client.queue.stats(), "your_active": research_client.queue.active_ip(ip)}
    return out


async def _draft_row(draft_id: str) -> dict:
    row = await asyncio.to_thread(storage.get_draft, draft_id) if storage.enabled else None
    if row is None:
        raise HTTPException(status_code=404, detail="저장된 초안이 없습니다.")
    # 프론트 복원용 (2026-09-03): 마무리 작업자가 켜져 있으면 빈 문항이 곧 채워질 수 있으니 폴링하라는 신호.
    row["finisher"] = {"enabled": bool(_finisher_settings.get("enabled")), "in_progress": finisher.in_progress_for(draft_id)}
    return row


async def _authorize(draft_id: str, key: str | None, request: Request) -> str:
    """초안 접근 확인 (2026-09-04). 열쇠(draft_key)가 맞거나, 열쇠 없는 옛 초안이면 요청 IP 가 저장된 owner 와 같아야 한다.
    아니면 404 — 존재 여부도 드러내지 않는다. 통과하면 열쇠를 돌려준다(옛 초안은 이때 열쇠가 생긴다)."""
    if not storage.enabled or not await asyncio.to_thread(storage.check_access, draft_id, key, _client_key(request)):
        raise HTTPException(status_code=404, detail="저장된 초안이 없습니다.")
    return await asyncio.to_thread(storage.owner_key, draft_id) or ""


@app.get("/drafts/{draft_id}")
async def get_draft(draft_id: str, request: Request, key: str | None = None):
    """저장된 초안 한 벌: 입력(아이디어·인테이크 답) + 문항별 생성 이력(오래된 것부터). 재접속 복원용(이 브라우저의 draft_id).
    `?key=<draft_key>`(첫 저장 응답의 열쇠)가 맞아야 한다. 열쇠 없는 옛 초안은 같은 IP 에서만 열리고, 응답의 draft_key 로 열쇠를 받는다.
    목록 조회는 두지 않는다 — 인증이 없어 남의 아이디어가 보이면 안 되므로 운영자는 scripts/drafts_report.py 로 본다.
    다른 기기 공유는 /drafts/{id}/share → /shared/{token}."""
    draft_key = await _authorize(draft_id, key, request)
    row = await _draft_row(draft_id)
    row["draft_key"] = draft_key
    return row


@app.post("/drafts/{draft_id}/share")
async def share_draft(draft_id: str, request: Request, key: str | None = None):
    """공유 링크 토큰 (2026-09-04): { draft_id, share }. 처음 부르면 난수 토큰을 만들어 저장하고, 그 뒤로는 같은 값.
    링크(`?share=<token>`)에 DB 키가 드러나지 않게 하려고 draft_id 와 별개로 둔다. 주인(열쇠 또는 같은 IP)만 만들 수 있다.
    테스트 모드 초안은 서비스 DB 에 없어 404."""
    await _authorize(draft_id, key, request)
    token = await asyncio.to_thread(storage.share_token, draft_id)
    if not token:
        raise HTTPException(status_code=404, detail="저장된 초안이 없습니다.")
    return {"draft_id": draft_id, "share": token}


@app.get("/shared/{token}")
async def get_shared(token: str):
    """공유 토큰으로 초안 한 벌 (GET /drafts/{id} 와 같은 모양 + share + draft_key). 다른 PC·폰에서 이어 보기 —
    공유 링크는 주인이 직접 만들어 건넨 것이므로 받는 기기가 같은 초안을 이어 쓸 수 있게 열쇠도 준다."""
    did = await asyncio.to_thread(storage.draft_id_for_share, token) if storage.enabled else None
    if not did:
        raise HTTPException(status_code=404, detail="공유 링크에 해당하는 초안이 없습니다.")
    row = await _draft_row(did)
    row["share"] = token
    row["draft_key"] = await asyncio.to_thread(storage.owner_key, did) or ""
    return row


@app.post("/generate/dry-run")
async def dry_run(req: GenerateRequest):
    """LLM 호출 없이 조립된 프롬프트만 확인 (프롬프트 튜닝용)."""
    try:
        system, user, meta = assemble.build_prompts(req.model_dump())
    except assemble.PromptNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"system": system, "user": user, "meta": meta}
