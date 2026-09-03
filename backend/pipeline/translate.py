"""초안·화면 문구 번역 (2026-09-03, 외국인 지원자용).

    out = await translate_texts(client, ["안녕하세요", "..."], "en")
    out["translations"]  → 원문과 같은 길이의 목록. 실패한 항목은 "" (프론트는 그 자리에 한국어 원문을 그대로 보여준다).

신청서 본문은 **한국어로만** 만들고 제출도 한국어로 한다 — 번역은 외국인이 자기 초안을 이해하기 위한 읽기용이다.
- 항목이 하나(문항 본문 2,000자)면 번역문만 내는 평문 모드(translate.md). JSON 으로 받으면 줄바꿈·따옴표가 깨지기 쉽다.
- 여럿(인테이크 카드의 질문·보기·힌트)이면 번호 붙인 목록 → JSON 객체(translate_batch.md). 한 번에 BATCH 개씩 나눠 부른다.
모델 호출: 평문 1회, 목록은 ceil(n / BATCH) 회.
"""
from __future__ import annotations

import json
import math
import re

from ..llm.client import LLMClient
from . import assemble

# 화면 언어 코드 → 프롬프트에 넣는 언어 이름. 프론트 i18n.jsx 의 LANGS 와 맞춘다.
LANG_NAMES = {
    "en": "영어(English)",
    "zh": "중국어 간체(简体中文)",
    "ja": "일본어(日本語)",
}
BATCH = 40                 # 목록 모드에서 한 번에 넘기는 항목 수 (카드 8장 × 보기·힌트 ≈ 110개 → 3회)
MAX_ITEMS = 200
MAX_CHARS = 4000           # 항목 하나의 최대 길이 (문항 한도 2,000자의 두 배)

_FENCE = re.compile(r"```[a-zA-Z]*\n?|```")


def normalize_lang(lang: str | None) -> str:
    code = str(lang or "").strip().lower().replace("_", "-")
    code = code.split("-")[0]            # zh-CN → zh, en-US → en
    if code not in LANG_NAMES:
        raise ValueError(f"지원하지 않는 언어: {lang!r} (가능: {', '.join(LANG_NAMES)})")
    return code


def _clean_plain(raw: str) -> str:
    text = _FENCE.sub("", raw or "").strip()
    # 모델이 "번역:" / "Translation:" 머리말을 붙이는 경우만 떼어낸다 (본문 첫 줄이 그 단어로 끝날 때).
    first, _, rest = text.partition("\n")
    if rest and re.fullmatch(r"\s*(번역|translation|翻訳|翻译|译文)\s*[:：]?\s*", first, re.I):
        text = rest.strip()
    return text


def _parse_batch(raw: str, n: int) -> list[str]:
    cleaned = _FENCE.sub("", raw or "")
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end == -1:
        return [""] * n
    try:
        data = json.loads(cleaned[start:end + 1])
    except json.JSONDecodeError:
        return [""] * n
    if not isinstance(data, dict):
        return [""] * n
    out = []
    for i in range(1, n + 1):
        v = data.get(str(i))
        out.append(str(v).strip() if isinstance(v, (str, int, float)) and str(v).strip() else "")
    return out


def _numbered(items: list[str]) -> str:
    return "\n".join(f"{i}. {t.replace(chr(10), ' ')}" for i, t in enumerate(items, start=1))


async def translate_texts(client: LLMClient, texts: list[str], lang: str, model: str | None = None) -> dict:
    """texts 를 lang 으로. → {lang, translations: [str]*len(texts), model}. 빈 항목은 호출 없이 ""."""
    code = normalize_lang(lang)
    if not isinstance(texts, list):
        raise ValueError("texts 는 문자열 목록이어야 합니다.")
    if len(texts) > MAX_ITEMS:
        raise ValueError(f"한 번에 {MAX_ITEMS}개까지 번역할 수 있습니다.")
    items = [str(t or "")[:MAX_CHARS] for t in texts]
    todo = [i for i, t in enumerate(items) if t.strip()]
    result = [""] * len(items)
    used_model = ""
    if not todo:
        return {"lang": code, "translations": result, "model": used_model}

    headers = assemble.section_headers()
    if len(todo) == 1:
        idx = todo[0]
        system = assemble.render("translate.md", lang_name=LANG_NAMES[code])
        user = f"{headers.get('translate_source', '[원문]')}\n{items[idx]}"
        res = await client.complete(system, user, model=model)
        result[idx] = _clean_plain(res.text)
        return {"lang": code, "translations": result, "model": res.model}

    system = assemble.render("translate_batch.md", lang_name=LANG_NAMES[code])
    for chunk_no in range(math.ceil(len(todo) / BATCH)):
        chunk = todo[chunk_no * BATCH:(chunk_no + 1) * BATCH]
        user = f"{headers.get('translate_list', '[원문 목록]')}\n{_numbered([items[i] for i in chunk])}"
        res = await client.complete(system, user, model=model)
        used_model = res.model
        for i, t in zip(chunk, _parse_batch(res.text, len(chunk))):
            result[i] = t
    return {"lang": code, "translations": result, "model": used_model}
