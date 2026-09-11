"""초안·화면 문구 번역 (2026-09-03, 외국인 지원자용).

    out = await translate_texts(client, ["안녕하세요", "..."], "en")
    out["translations"]  → 원문과 같은 길이의 목록. 실패한 항목은 "" (프론트는 그 자리에 한국어 원문을 그대로 보여준다).

신청서 본문은 **한국어로만** 만들고 제출도 한국어로 한다 — 번역은 외국인이 자기 초안을 이해하기 위한 읽기용이다.
- 항목이 하나(문항 본문 2,000자)면 번역문만 내는 평문 모드(translate.md). JSON 으로 받으면 줄바꿈·따옴표가 깨지기 쉽다.
- 여럿(인테이크 카드의 질문·보기·힌트)이면 번호 붙인 목록 → JSON 객체(translate_batch.md). 한 번에 BATCH 개씩 나눠 부른다.
모델 호출: 평문 1회, 목록은 ceil(n / BATCH) 회(청크 병렬). 결과가 비었거나 한글이 남은 항목은 1회 더 부른다.
"""
from __future__ import annotations

import asyncio
import json
import math
import re

from ..llm.client import LLMClient
from .. import timing
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


_HANGUL = re.compile(r"[가-힣]")


def _kw(extra: dict | None) -> dict:
    """extra 가 있을 때만 complete(extra=...) 를 넘긴다 — 테스트의 가짜 클라이언트는 그 인자를 모른다."""
    return {"extra": extra} if extra else {}


def _bad(text: str) -> bool:
    """다시 번역해야 하는 결과: 비었거나 한글이 남아 있음 (실측 2026-09-03: 일본어 번역에 '여부를' 이 그대로 남았다)."""
    return not text.strip() or bool(_HANGUL.search(text))


async def _plain(client: LLMClient, system: str, header: str, text: str, model: str | None,
                 extra: dict | None = None) -> tuple[str, str]:
    res = await client.complete(system, f"{header}\n{text}", model=model, **_kw(extra))
    out = _clean_plain(res.text)
    if _bad(out):                                     # 1회만 다시 — temperature 가 있어 두 번째는 대개 온전히 온다
        res2 = await client.complete(system, f"{header}\n{text}", model=model, **_kw(extra))
        out2 = _clean_plain(res2.text)
        if not _bad(out2) or (out2.strip() and not out.strip()):
            out = out2
    return out, res.model


def _closed(raw: str) -> bool:
    """응답에 여는 중괄호와 닫는 중괄호가 모두 있는지 — 잘렸는지(max_tokens) 판별용."""
    t = _FENCE.sub("", raw or "")
    return t.find("{") != -1 and t.rfind("}") > t.find("{")


async def _batch(client: LLMClient, system: str, header: str, items: list[str], model: str | None,
                 extra: dict | None = None, depth: int = 0) -> tuple[list[str], str]:
    """목록 한 청크를 번역한다. 실패하면 ① 실패한 항목만 1회 더 → ② 그래도 절반 넘게 비면 **반으로 쪼개 다시**.

    ②를 넣은 이유 (2026-09-07): 부하 테스트에서 중국어 카드 번역 60개 중 **40개가 통째로 빈 채로 done** 됐다
    (`translate_empty` 계측의 첫 실전 검출). 청크 하나가 파싱에 실패하면 그 안의 40개가 다 날아가는 구조였다.
    원인은 간헐적이라(같은 조건 재현에서는 40/40 성공) 원인을 못 박아도 **결과는 지키도록** 쪼개서 다시 묻는다.
    쪼개기는 실패했을 때만 돌고 depth 2 까지라 정상 경로의 호출 수는 그대로다.
    """
    res = await client.complete(system, f"{header}\n{_numbered(items)}", model=model, **_kw(extra))
    out = _parse_batch(res.text, len(items))
    if items and all(not t.strip() for t in out):
        # 청크가 통째로 죽었다 = 파싱 실패. 다음에 원인을 좁힐 수 있게 응답의 모양을 남긴다.
        try:
            from .. import timing
            timing.log("translate_parse_fail", n=len(items), chars=len(res.text or ""),
                       closed=_closed(res.text or ""), depth=depth, head=(res.text or "")[:120])
        except Exception:
            pass
    redo = [i for i, t in enumerate(out) if _bad(t)]
    if redo:                                          # 빈 항목·한글 잔류만 모아 1회 더
        res2 = await client.complete(system, f"{header}\n{_numbered([items[i] for i in redo])}", model=model, **_kw(extra))
        for i, t in zip(redo, _parse_batch(res2.text, len(redo))):
            if not _bad(t) or (t.strip() and not out[i].strip()):
                out[i] = t
    still = [i for i, t in enumerate(out) if _bad(t)]
    if depth < 2 and len(items) >= 8 and len(still) > len(items) // 2:
        mid = len(items) // 2
        left, _m = await _batch(client, system, header, items[:mid], model, extra, depth + 1)
        right, _m = await _batch(client, system, header, items[mid:], model, extra, depth + 1)
        for i, t in enumerate(left + right):
            if not _bad(t) or (t.strip() and not out[i].strip()):
                out[i] = t
    return out, res.model


def _log_empty(lang: str, mode: str, todo: list[int], result: list[str]) -> None:
    """번역이 비어 나온 항목 수를 남긴다 (2026-09-07).

    작업 자체는 성공(done)으로 끝나므로 이 줄이 없으면 **번역이 비었다는 사실을 사후에 알 수 없다**.
    학생 화면에는 "n개가 아직 한국어로 보입니다"로만 나타난다 (frontend/src/App.jsx cardUntranslated).
    빈 항목이 있을 때만 남긴다 — 정상 번역까지 남기면 timing.jsonl 이 배로 커진다.
    읽기: scripts/timing_report.py, 또는 grep translate_empty backend/.timing.jsonl
    """
    empty = sum(1 for i in todo if not result[i].strip())
    if empty:
        timing.log("translate_empty", lang=lang, mode=mode, requested=len(todo), empty=empty)


async def translate_texts(client: LLMClient, texts: list[str], lang: str, model: str | None = None,
                          extra: dict | None = None) -> dict:
    """texts 를 lang 으로. → {lang, translations: [str]*len(texts), model}. 빈 항목은 호출 없이 "".
    목록 모드는 BATCH 개 청크를 **동시에** 부른다 (카드 150개 = 4청크 → 순차 60초가 15~20초로).
    extra: 모델 호출에 덧붙일 extra_body — main 이 config.yaml translate.priority 를 넣어 조사(0)보다 뒤, 생성(100)보다 앞에 세운다."""
    code = normalize_lang(lang)
    if not isinstance(texts, list):
        raise ValueError("texts 는 문자열 목록이어야 합니다.")
    if len(texts) > MAX_ITEMS:
        raise ValueError(f"한 번에 {MAX_ITEMS}개까지 번역할 수 있습니다.")
    items = [str(t or "")[:MAX_CHARS] for t in texts]
    todo = [i for i, t in enumerate(items) if t.strip()]
    result = [""] * len(items)
    if not todo:
        return {"lang": code, "translations": result, "model": ""}

    headers = assemble.section_headers()
    if len(todo) == 1:
        idx = todo[0]
        system = assemble.render("translate.md", lang_name=LANG_NAMES[code])
        out, used = await _plain(client, system, headers.get("translate_source", "[원문]"), items[idx], model, extra)
        result[idx] = out
        _log_empty(code, "plain", todo, result)
        return {"lang": code, "translations": result, "model": used}

    system = assemble.render("translate_batch.md", lang_name=LANG_NAMES[code])
    header = headers.get("translate_list", "[원문 목록]")
    chunks = [todo[i * BATCH:(i + 1) * BATCH] for i in range(math.ceil(len(todo) / BATCH))]
    outs = await asyncio.gather(*(_batch(client, system, header, [items[i] for i in ch], model, extra) for ch in chunks))
    used = ""
    for ch, (out, m) in zip(chunks, outs):
        used = m
        for i, t in zip(ch, out):
            result[i] = t
    _log_empty(code, "batch", todo, result)
    return {"lang": code, "translations": result, "model": used}
