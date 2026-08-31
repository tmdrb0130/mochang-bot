"""OpenAI 호환 LLM 클라이언트 — OpenRouter / Ollama / vLLM / LiteLLM(Claude·Gemini) 공용.

config.yaml의 base_url / api_key / model 세 값만 바꾸면 어떤 서버든 붙는다.
api_key 는 "${ENV_NAME}" 형식이면 환경변수(프로젝트 루트 .env 포함)에서 치환한다.
extra 는 요청 본문에 그대로 추가되는 서버별 옵션 (예: OpenRouter reasoning 끄기).
"""
import asyncio
import os
import re
from pathlib import Path

import yaml
from dotenv import load_dotenv
from openai import AsyncOpenAI

ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = Path(__file__).resolve().parents[1] / "config.yaml"

_ENV_REF = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")

load_dotenv(ROOT / ".env")


class LLMError(RuntimeError):
    """LLM 서버가 정상 형식의 응답을 주지 않았을 때 (무료 티어 일시 오류 등)."""


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


class LLMClient:
    def __init__(self, config: dict):
        c = config["llm"]
        self.model = c["model"]
        self.max_tokens = c.get("max_tokens", 4096)
        self.temperature = c.get("temperature", 0.7)
        self.extra = c.get("extra") or {}
        # 동시 요청 수 제한 — 로컬 모델 과부하 방지 + OpenRouter 무료 티어 분당 한도 보호
        self._sem = asyncio.Semaphore(int(config.get("concurrency") or 3))
        headers = {}
        if "openrouter.ai" in c["base_url"]:
            headers = {"HTTP-Referer": "http://localhost", "X-Title": "modoo-writer"}
        self._client = AsyncOpenAI(
            base_url=c["base_url"], api_key=c["api_key"], default_headers=headers, max_retries=3
        )

    async def complete(self, system: str, user: str) -> str:
        async with self._sem:
            return await self._complete(system, user)

    async def _complete(self, system: str, user: str) -> str:
        res = await self._client.chat.completions.create(
            model=self.model,
            max_tokens=self.max_tokens,
            temperature=self.temperature,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            extra_body=self.extra or None,
        )
        if not res.choices:
            # OpenRouter 무료 티어가 HTTP 200에 choices 없이(에러 본문만) 응답하는 경우가 있음
            raise LLMError(f"LLM 응답에 choices 가 없습니다 (model={self.model}): {res.model_dump_json()[:500]}")
        choice = res.choices[0]
        text = choice.message.content or ""
        if choice.finish_reason == "length" and not text.strip():
            raise LLMError(f"max_tokens({self.max_tokens}) 안에 본문을 못 냈습니다. 추론 모델이면 extra.reasoning 을 끄세요.")
        return text
