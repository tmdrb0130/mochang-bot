"""마무리 작업자 — 학생이 생성 도중 나가도 남은 문항을 서버가 채운다 (2026-09-03).

배경: 프론트가 문항을 3개씩 큐에 넣고 하나 끝나면 다음을 넣는 구조라, 탭을 닫는 순간 큐에 없던 문항은 영영 안 만들어졌다
(실측: 주차장 학생 3/8, 빵집 학생 7/8 에서 멈춤). 이 작업자는 주기적으로 DB 를 훑어
  "생성을 시작했고(generate 1건 이상) · 마지막 요청 뒤 idle_seconds 넘게 조용하고 · 아직 빈 문항이 있는" 초안을 찾아
남은 (문항, 스타일)을 만들어 generations 에 남긴다(meta.auto=true). 학생이 다시 접속하면 프론트가 /drafts/{id} 로 받아 채운다.

우선순위: 실시간 학생보다 뒤. (1) vLLM priority 를 본문 생성(100)보다 큰 값(기본 200)으로 보내 GPU 가 붐빌 때 뒤로 밀리고,
(2) 자체 LLMClient 의 작은 큐(concurrency, 기본 3)로 돌아 본문 워커(80)를 점유하지 않는다. IP 당 제한과도 무관.

참고자료: research 테이블(이 초안이 실제로 받았던 사실) → 없으면 조사 파이프라인(디스크 캐시 7일 → 새 조사) 순.
안전장치: 같은 (초안, 문항, 스타일)은 max_attempts 번만 시도, 한 번에 max_per_tick 초안까지, 저장 실패는 삼킨다.
모델을 직접 부르는 유일한 곳은 generate.generate_one — 테스트는 그 함수를 바꿔 끼운다.
"""
from __future__ import annotations

import asyncio
import copy
import logging
import time
from typing import Awaitable, Callable

from . import timing

log = logging.getLogger("mochang.finisher")

# 프론트 QUESTIONS 와 같은 순서. q7_1 은 사업자만. 바뀌면 frontend/src/App.jsx QUESTIONS 도 같이.
DEFAULT_QUESTIONS = ["q1", "q2", "q3_1", "q3_2", "q4_1", "q4_2", "q8", "q10"]
BUSINESS_ONLY = ["q7_1"]

DEFAULTS = {
    "enabled": False,
    "interval_seconds": 60,     # 훑는 주기
    "idle_seconds": 180,        # 마지막 요청 뒤 이만큼 조용하면 "나갔다" 고 본다
    "concurrency": 3,           # 동시에 만드는 문항 수 (자체 큐 워커 수)
    "priority": 200,            # vLLM 우선순위 — 본문 생성 100 보다 뒤
    "max_attempts": 2,          # (초안, 문항, 스타일) 당 최대 시도
    "max_per_tick": 5,          # 한 주기에 손대는 초안 수
    "lookback_days": 3,         # 이보다 오래된 초안은 안 본다
}


def finisher_client_config(config: dict, cfg: dict) -> dict:
    """본문 클라이언트(llm:)와 같은 서버·모델로, 우선순위만 뒤로 두고 작은 큐를 가진 LLMClient 설정."""
    llm = copy.deepcopy(config["llm"])
    extra = dict(llm.get("extra") or {})
    extra["priority"] = int(cfg["priority"])
    llm["extra"] = extra
    return {
        "llm": llm,
        "models": copy.deepcopy(config.get("models") or []),   # 학생이 고른 모델 id 가 통하게 목록은 그대로
        "fallback": False,
        "daily_request_limit": None,
        "max_workers": int(cfg["concurrency"]),
        "retry": config.get("retry") or {},
        "max_jobs_per_client": 0,
    }


def load_settings(config: dict) -> dict:
    raw = config.get("finisher") or {}
    out = dict(DEFAULTS)
    for k in DEFAULTS:
        if k in raw and raw[k] is not None:
            out[k] = raw[k]
    if raw.get("questions"):
        out["questions"] = list(raw["questions"])
    return out


def required_pairs(draft: dict, questions: list[str] | None = None) -> set[tuple[str, str]]:
    qs = list(questions or DEFAULT_QUESTIONS)
    if draft.get("is_business"):
        qs = qs + [q for q in BUSINESS_ONLY if q not in qs]
    return {(q, s) for q in qs for s in (draft.get("styles") or ["logic"])}


def missing_pairs(draft: dict, questions: list[str] | None = None) -> list[tuple[str, str]]:
    """아직 없는 (문항, 스타일). 문항 순서대로."""
    need = required_pairs(draft, questions)
    done = set(draft.get("done") or ())
    order = {q: i for i, q in enumerate(list(questions or DEFAULT_QUESTIONS) + BUSINESS_ONLY)}
    return sorted(need - done, key=lambda p: (order.get(p[0], 99), p[1]))


class Finisher:
    def __init__(self, storage, client, settings: dict,
                 generate_fn: Callable[[object, dict], Awaitable[dict]],
                 research_fn: Callable[[dict, str], Awaitable[list[dict] | None]] | None = None,
                 sleep=asyncio.sleep):
        """storage: backend.storage.Storage / client: 우선순위 낮은 LLMClient / generate_fn(client, form) → 생성 결과 dict
        research_fn(form, question_id) → facts (research 테이블에 없을 때만 부른다; None 이면 참고자료 없이 쓴다)."""
        self.storage = storage
        self.client = client
        self.s = settings
        self.generate_fn = generate_fn
        self.research_fn = research_fn
        self.sleep = sleep
        self.attempts: dict[tuple[str, str, str], int] = {}
        self.in_progress: set[tuple[str, str, str]] = set()
        self.stats = {"ticks": 0, "done": 0, "failed": 0, "skipped": 0}
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    # ── 수명 ──
    def start(self) -> None:
        if self._task is None and self.s.get("enabled"):
            self._stop.clear()
            self._task = asyncio.create_task(self._loop(), name="finisher")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def _loop(self) -> None:
        # 첫 주기는 한 텀 쉬고 — 재시작 직후에는 학생들이 새로고침·이어받기로 바쁘다
        await self.sleep(int(self.s["interval_seconds"]))
        while not self._stop.is_set():
            try:
                await self.tick()
            except Exception as e:           # 작업자 하나 때문에 서비스가 멈추면 안 된다
                log.warning("마무리 주기 실패: %s", str(e)[:200])
            await self.sleep(int(self.s["interval_seconds"]))

    # ── 한 주기 ──
    async def tick(self) -> int:
        """대상 초안을 찾아 빈 문항을 채운다. 만든 문항 수를 돌려준다."""
        self.stats["ticks"] += 1
        if not getattr(self.storage, "enabled", True) or getattr(self.storage, "engine", None) is None:
            return 0
        rows = await asyncio.to_thread(self.storage.list_unfinished, int(self.s["idle_seconds"]), int(self.s["lookback_days"]))
        made = 0
        touched = 0
        for draft in rows:
            todo = [(q, s) for (q, s) in missing_pairs(draft, self.s.get("questions"))
                    if self._may_try(draft["draft_id"], q, s)]
            if not todo:
                continue
            touched += 1
            if touched > int(self.s["max_per_tick"]):
                break
            results = await asyncio.gather(*(self._finish_one(draft, q, s) for q, s in todo), return_exceptions=True)
            made += sum(1 for r in results if r is True)
        return made

    def _may_try(self, did: str, q: str, s: str) -> bool:
        key = (did, q, s)
        if key in self.in_progress:
            return False
        if self.attempts.get(key, 0) >= int(self.s["max_attempts"]):
            self.stats["skipped"] += 1
            return False
        return True

    def in_progress_for(self, draft_id: str) -> list[str]:
        return sorted({q for (d, q, _s) in self.in_progress if d == draft_id})

    def _form_for(self, draft: dict, q: str, s: str) -> dict:
        model = draft.get("model")
        ids = getattr(self.client, "model_ids", None)
        if ids and model not in ids:
            model = None                     # 학생이 쓰던 모델이 지금 목록에 없으면 기본 모델
        return {
            "question_id": q, "style": s, "track": draft.get("track") or "tech", "idea": draft.get("idea") or "",
            "is_business": bool(draft.get("is_business")), "current_item": draft.get("current_item") or "",
            "team": draft.get("team") or "팀원 없음", "capability": draft.get("capability") or "",
            "model": model, "answers": draft.get("answers") or None, "draft_id": draft["draft_id"],
        }

    async def _references(self, form: dict, q: str) -> list[dict]:
        facts = await asyncio.to_thread(self.storage.get_research, form["draft_id"], q)
        if facts is not None:
            return facts
        if self.research_fn is None:
            return []
        try:
            got = await self.research_fn(form, q)
        except Exception as e:
            log.info("마무리 조사 실패 %s/%s: %s", form["draft_id"][:8], q, str(e)[:120])
            return []
        return got or []

    async def _finish_one(self, draft: dict, q: str, s: str) -> bool:
        key = (draft["draft_id"], q, s)
        self.in_progress.add(key)
        self.attempts[key] = self.attempts.get(key, 0) + 1
        t0 = time.monotonic()
        status = "done"
        try:
            form = self._form_for(draft, q, s)
            refs = await self._references(form, q)
            if refs:
                form["references"] = refs
            result = await self.generate_fn(self.client, form)
            if not isinstance(result, dict) or not result.get("text"):
                raise RuntimeError("빈 결과")
            # storage.record 를 쓰지 않는다 — upsert_draft 가 updated_at 을 올려 "조용한 초안" 판정이 흔들린다.
            await asyncio.to_thread(self.storage.add_generation, draft["draft_id"], "generate", form,
                                    {**result, "auto": True, "references_used": len(refs)})
            self.stats["done"] += 1
            return True
        except Exception as e:
            status = "error"
            self.stats["failed"] += 1
            log.warning("마무리 생성 실패 %s/%s/%s: %s", draft["draft_id"][:8], q, s, str(e)[:160])
            return False
        finally:
            self.in_progress.discard(key)
            timing.log("auto_finish", draft=draft["draft_id"][:8], question=q, style=s, status=status,
                       attempt=self.attempts[key], run_s=round(time.monotonic() - t0, 2))
