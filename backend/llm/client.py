"""OpenAI 호환 LLM 클라이언트 — OpenRouter / Ollama / vLLM / LiteLLM(Claude·Gemini) 공용.

config.yaml의 base_url / api_key / model 세 값만 바꾸면 어떤 서버든 붙는다.
- api_key 가 "${ENV_NAME}" 형식이면 환경변수(프로젝트 루트 .env 포함)에서 치환.
- models: UI에서 고를 수 있는 모델 목록. 요청마다 model 을 지정해 덮어쓸 수 있다.
- fallback: OpenRouter 의 `models` 파라미터로 429/다운 시 목록의 다른 모델로 자동 전환.
- daily_request_limit: 무료 티어 하루 요청 한도. 백엔드가 요청 수를 세어(.usage.json) /health 로 알린다.
"""
import asyncio
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple

import yaml
from dotenv import load_dotenv
from openai import APIStatusError, AsyncOpenAI, RateLimitError

ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = Path(__file__).resolve().parents[1]
CONFIG_PATH = BACKEND_DIR / "config.yaml"
USAGE_PATH = BACKEND_DIR / ".usage.json"

_ENV_REF = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")

load_dotenv(ROOT / ".env")


class LLMError(RuntimeError):
    """LLM 서버가 정상 형식의 응답을 주지 않았을 때 (무료 티어 일시 오류 등)."""


class RateLimited(LLMError):
    """429 — 분당/일일 한도 또는 업스트림 무료 풀 혼잡."""


class TransientError(LLMError):
    """업스트림 타임아웃(504 'The operation was aborted')·과부하 등 — 다른 모델로 재시도하면 대개 됨."""


class LLMResult(NamedTuple):
    text: str
    model: str      # 실제로 응답한 모델 (폴백되면 요청한 것과 다를 수 있음)


def _expand_env(value: str) -> str:
    m = _ENV_REF.match(value or "")
    if not m:
        return value
    name = m.group(1)
    resolved = os.environ.get(name)
    if not resolved:
        raise RuntimeError(f"환경변수 {name} 가 없습니다. 프로젝트 루트 .env 에 {name}=... 을 추가하세요.")
    return resolved


def load_config(path: Path = CONFIG_PATH) -> dict:
    with open(path, encoding="utf-8") as f:
        config = yaml.safe_load(f)
    config["llm"]["api_key"] = _expand_env(config["llm"].get("api_key", ""))
    return config


class UsageCounter:
    """하루(UTC 기준 — OpenRouter 리셋 기준) 요청 수를 파일에 누적. 서버 재시작에도 유지."""

    def __init__(self, path: Path, daily_limit: int | None):
        self.path = path
        self.daily_limit = daily_limit
        self._lock = asyncio.Lock()
        self._data = self._load()

    @staticmethod
    def _today() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _load(self) -> dict:
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
            if d.get("date") == self._today():
                return d
        except (OSError, ValueError):
            pass
        return {"date": self._today(), "count": 0, "by_model": {}}

    def _roll(self):
        if self._data.get("date") != self._today():
            self._data = {"date": self._today(), "count": 0, "by_model": {}}
        self._data.setdefault("by_model", {})

    async def hit(self, model: str = "") -> int:
        async with self._lock:
            self._roll()
            self._data["count"] += 1
            if model:
                self._data["by_model"][model] = self._data["by_model"].get(model, 0) + 1
            try:
                self.path.write_text(json.dumps(self._data), encoding="utf-8")
            except OSError:
                pass
            return self._data["count"]

    def snapshot(self) -> dict:
        self._roll()
        used = self._data["count"]
        return {
            "date_utc": self._data["date"],
            "used": used,
            "limit": self.daily_limit,
            "remaining": None if self.daily_limit is None else max(self.daily_limit - used, 0),
            "reset": "UTC 00:00 (한국 09:00)",
            "by_model": dict(self._data.get("by_model", {})),   # 참고용 — 한도는 계정 단위, 모델별 한도는 없음
        }


class LLMClient:
    def __init__(self, config: dict):
        c = config["llm"]
        self.base_url = c["base_url"]
        self.model = c["model"]
        self.max_tokens = c.get("max_tokens", 4096)
        self.temperature = c.get("temperature", 0.7)
        self.extra = c.get("extra") or {}
        self.models = list(config.get("models") or [])
        self.model_ids = [m["id"] for m in self.models] or [self.model]
        self.is_openrouter = "openrouter.ai" in self.base_url
        self.fallback = bool(config.get("fallback", True)) and self.is_openrouter and len(self.model_ids) > 1
        self.usage = UsageCounter(USAGE_PATH, config.get("daily_request_limit") if self.is_openrouter else None)
        # 동시 요청 수 제한 — 로컬 모델 과부하 방지 + OpenRouter 무료 티어 분당 한도(20 RPM) 보호
        self._sem = asyncio.Semaphore(int(config.get("concurrency") or 3))
        headers = {"HTTP-Referer": "http://localhost", "X-Title": "modoo-writer"} if self.is_openrouter else {}
        self._client = AsyncOpenAI(
            base_url=self.base_url, api_key=c["api_key"], default_headers=headers, max_retries=2
        )

    def resolve_model(self, model: str | None) -> str:
        """요청에서 온 model 을 검증. 목록에 없는 id 는 거부(유료 모델 오남용 방지)."""
        if not model:
            return self.model
        if self.models and model not in self.model_ids:
            raise ValueError(f"허용되지 않은 모델입니다: {model}. /models 에서 목록을 확인하세요.")
        return model

    def _extra_for(self, model: str) -> dict:
        extra = dict(self.extra)
        for m in self.models:
            if m["id"] == model and m.get("extra"):
                extra.update(m["extra"])
        if self.fallback:
            # 선택 모델을 첫 번째로, 나머지를 폴백 순서로. OpenRouter 가 429/다운 시 다음 모델로 넘긴다.
            # OpenRouter 는 models 배열을 최대 3개까지만 받는다 (초과 시 400) → 선택 모델 + 목록 순서상 2개.
            extra["models"] = ([model] + [i for i in self.model_ids if i != model])[:3]
        return extra

    # OpenRouter 가 HTTP 200 본문 안에 error 객체로 돌려주는 일시 오류 코드 (업스트림 타임아웃·과부하)
    _TRANSIENT = {408, 429, 500, 502, 503, 504, 520, 522, 524, 529}

    async def complete(self, system: str, user: str, model: str | None = None) -> LLMResult:
        model = self.resolve_model(model)
        async with self._sem:
            # 일시 오류면 목록의 다음 모델로 바꿔 최대 3번 시도 (같은 제공자가 연속으로 끊는 경우가 많음)
            order = [model] + [i for i in self.model_ids if i != model]
            last: Exception | None = None
            for attempt, m in enumerate(order[:3]):
                try:
                    return await self._complete(system, user, m)
                except TransientError as e:
                    last = e
                    await asyncio.sleep(1.5 * (attempt + 1))
            raise LLMError(f"{last} — 무료 제공자가 연속으로 응답을 끊었습니다. 잠시 후 '다시 생성'을 눌러주세요.")

    async def _complete(self, system: str, user: str, model: str) -> LLMResult:
        used = await self.usage.hit(model)
        try:
            res = await self._client.chat.completions.create(
                model=model,
                max_tokens=self.max_tokens,
                temperature=self.temperature,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                extra_body=self._extra_for(model) or None,
            )
        except RateLimitError as e:
            snap = self.usage.snapshot()
            if snap["limit"] and used >= snap["limit"]:
                hint = (f"오늘 무료 요청 {snap['used']}/{snap['limit']}회 — 일일 한도에 도달했을 수 있습니다. "
                        f"{snap['reset']}에 초기화됩니다. 그 전에 쓰려면 OpenRouter에 $10 충전(하루 1000회) 또는 로컬 모델로 전환하세요.")
            else:
                hint = "무료 모델 풀이 혼잡합니다(429). 몇 초 후 다시 시도하거나 다른 모델을 선택하세요."
            raise RateLimited(hint) from e
        except APIStatusError as e:
            # 400(잘못된 파라미터)·402(크레딧 음수)·5xx 등 — 빈 500 대신 이유를 프론트까지 전달
            raise LLMError(f"LLM 서버 오류 {e.status_code} (model={model}): {getattr(e, 'message', str(e))[:300]}") from e
        if not res.choices:
            # OpenRouter 는 업스트림 오류를 HTTP 200 + {"error": {"message", "code"}} 로 돌려주기도 한다
            err = (res.model_dump().get("error") or {}) if hasattr(res, "model_dump") else {}
            code, msg = err.get("code"), err.get("message", "")
            if code in self._TRANSIENT or "aborted" in str(msg).lower() or "timeout" in str(msg).lower():
                raise TransientError(f"업스트림 오류 {code} '{msg}' (model={model})")
            raise LLMError(f"LLM 응답에 choices 가 없습니다 (model={model}): {res.model_dump_json()[:400]}")
        choice = res.choices[0]
        text = choice.message.content or ""
        if choice.finish_reason == "length" and not text.strip():
            raise LLMError(f"max_tokens({self.max_tokens}) 안에 본문을 못 냈습니다. 추론 모델이면 extra.reasoning 을 끄세요.")
        return LLMResult(text=text, model=getattr(res, "model", None) or model)
