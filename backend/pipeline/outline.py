"""사업계획 골자 — 아이디어당 1회 만들어 문항 9개가 공유한다 (docs/OUTLINE_PLAN.md, 작업 20).

문항이 서로를 모른 채 독립 호출되니 고객·문제·해결책·수익이 문항마다 다른 말로 반복됐다.
순차 생성(앞 글을 뒤에 넘기기)은 9문항 직렬로 4분이 걸려 못 쓴다 → **같은 골자를 모두에게 주는** 방식.

    outline = await get_outline(client, form)      # 캐시에 있으면 모델 호출 0회
    form = {**form, "outline": outline}            # assemble.build_context 가 섹션으로 넣는다

캐시 키는 아이디어 + 인테이크 답변 해시라 아이디어를 고치면 새로 만든다.
같은 키로 동시에 들어와도(프론트가 문항 3개를 함께 제출) 락으로 한 번만 만든다.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re

from ..llm.client import LLMClient
from ..rag.research import DiskCache
from . import assemble

OUTLINE_KEYS = ("customer", "problem", "evidence", "solution", "differentiators",
                "revenue", "plan_6m", "capability", "gaps", "key_stats")
LIST_KEYS = ("differentiators", "plan_6m", "gaps", "key_stats")
# 짧은 문항(Q1·Q10)에는 이 두 줄만 넣는다 — 100자 한 문장에 골자 열 줄을 붙이면 오히려 방해가 된다.
SHORT_KEYS = ("customer", "solution")

# 문항이 **자기 몫만** 보게 한다 (docs/OUTLINE_PLAN.md 4절 역할표).
# 처음에는 골자 전체를 모든 문항에 넣고 md 에 "나머지는 쓰지 않습니다" 를 적었는데, 라마가 그 금지를 무시하고
# 골자 문장을 문항마다 그대로 옮겨 적어 오히려 반복이 늘었다(2026-09-02 전/후 실측: 아이디어 B 0쌍 → 10쌍).
# 작업 15·18에서 확인한 대로 이 모델은 "쓰지 마라"가 아니라 "이것만 써라"를 지킨다 → 아예 안 보여 준다.
ROLE_KEYS = {
    "q1": ("customer", "solution"),
    "q2": ("problem", "customer", "evidence", "solution", "differentiators"),   # 대안의 한계는 Q2 도 쓴다
    "q3_1": ("differentiators", "solution", "problem"),
    "q3_2": ("revenue", "customer"),
    "q4_1": ("plan_6m", "solution", "revenue"),
    "q4_2": ("gaps", "capability"),
    "q7_1": ("problem", "solution"),
    "q8": ("capability", "plan_6m"),
    "q10": ("customer", "solution"),
}
# 같은 수치를 여러 문항이 인용하지 않도록 문항별로 하나씩 배정한다 (역할표의 '수치' 열).
STAT_FOR = {"q2": 0, "q3_1": 1}

_locks: dict[str, asyncio.Lock] = {}


def cache_key(form: dict) -> str:
    answers = json.dumps(form.get("answers") or [], ensure_ascii=False, sort_keys=True)
    raw = f"{form.get('track')}|{form.get('idea', '')}|{answers}"
    return "outline|" + hashlib.sha1(raw.encode("utf-8")).hexdigest()


def verified_stats(stats: list[str], references) -> list[str]:
    """골자의 '인용할 수치' 중 **참고자료에 실제로 있는 숫자**만 남긴다.

    실호출에서 참고자료에 없는 "1인 가구의 70% (출처: 한국통계청)" 같은 문장을 만들어 냈고,
    그게 문항 글의 인용으로 그대로 나갔다. 프롬프트로는 못 막아서 extract_facts 의 quote 검증과 같은 방식으로
    숫자를 대조한다. 숫자가 없는 항목은 수치가 아니므로 버린다."""
    if not references:
        return []
    blob = json.dumps(references, ensure_ascii=False)
    kept = []
    for stat in stats or []:
        # '의미 있는 수치'만 대조한다 — 한 자리 숫자와 인용 표기의 연도(19xx·20xx)는 뺀다.
        nums = [n.rstrip(".,") for n in re.findall(r"[0-9][0-9,.]*", stat)]
        significant = [n for n in nums if len(n) >= 2 and not re.fullmatch(r"(19|20)[0-9]{2}", n)]
        if significant and all(n in blob for n in significant):
            kept.append(stat)
    return kept


def verified_differentiators(items: list[str], form: dict) -> list[str]:
    """'기존 대안과 다른 점' 중 **근거 없는 서비스·업체 이름**이 든 항목을 버린다 (Q3_1_QUALITY 4절).

    실측에서 골자가 참고자료에 없는 경쟁사 이름을 만들어 넣었고 본문이 그대로 '○○는 시뮬레이션이 없다' 고 단정했다.
    이름 판정은 영문 브랜드 토큰(3자 이상)과 '○○ 서비스/앱/플랫폼' 꼴 — 입력·참고자료 어디에도 없으면 근거 없는 이름."""
    blob = json.dumps({k: form.get(k) for k in ("idea", "answers", "references")}, ensure_ascii=False).lower()
    kept = []
    for item in items or []:
        latin = re.findall(r"[A-Za-z][A-Za-z0-9.+-]{2,}", item)
        korean_brand = re.findall(r"([가-힣A-Za-z0-9]{2,})\s*(?:앱|서비스|플랫폼)", item)
        generic = {"ai", "mvp", "b2b", "b2c", "app", "sns", "iot",
                   "기존", "유사", "범용", "해당", "다른", "일반", "자체", "우리", "저희", "이런", "같은", "경쟁", "타사"}
        names = [n for n in latin + korean_brand if n.lower() not in generic]
        unsupported = [n for n in names if n.lower() not in blob]
        if unsupported and not any(n.lower() in blob for n in names):
            continue
        kept.append(item)
    return kept


def _clean(value) -> str:
    return " ".join(str(value or "").split())


def voice_for(form: dict) -> str:
    """전 문항이 같은 1인칭을 받도록 골자에 싣는 값. 팀원이 없으면 '저', 있으면 '저희'."""
    team = str((form or {}).get("team") or "").strip()
    solo = (not team) or ("없" in team) or team in ("0", "0명")
    return "저" if solo else "저희"


def normalize(raw: dict) -> dict:
    """모델이 낸 JSON 을 정해진 키·형식으로 정리한다. 없는 키는 빈 값으로 채워 형태를 항상 같게 유지."""
    out: dict = {}
    for key in OUTLINE_KEYS:
        value = (raw or {}).get(key)
        if key in LIST_KEYS:
            if isinstance(value, str):
                value = [value] if value.strip() else []
            items = [_clean(v) for v in (value or []) if _clean(v)]
            out[key] = items[:3] if key != "key_stats" else items[:2]
        else:
            out[key] = _clean(value)
    return out


def slice_for(outline: dict, question_id: str | None) -> dict:
    """이 문항이 맡은 항목만 남긴 골자. 모르는 문항이면 전체를 준다."""
    keys = ROLE_KEYS.get(str(question_id or ""))
    if not keys:
        return dict(outline)
    out = {k: outline.get(k) for k in keys if outline.get(k)}
    if outline.get("voice"):
        out["voice"] = outline["voice"]          # 1인칭은 모든 문항이 받는다
    stats = outline.get("key_stats") or []
    index = STAT_FOR.get(str(question_id or ""))
    if index is not None and len(stats) > index:
        out["key_stats"] = [stats[index]]      # 이 문항이 인용할 수치 하나만
    return out


def format_outline(outline: dict, short: bool = False) -> str:
    """사람이 읽는 목록으로. short=True 면 고객·해결책 두 줄만 (짧은 문항용)."""
    labels = {
        "customer": "핵심 고객", "problem": "문제", "evidence": "문제 확인 방법", "solution": "해결책",
        "differentiators": "기존 대안과 다른 점", "revenue": "수익 방식", "plan_6m": "6개월 계획",
        "capability": "지원자 역량", "gaps": "아직 모르는 것", "key_stats": "인용할 수치",
    }
    keys = SHORT_KEYS if short else OUTLINE_KEYS
    lines = []
    if outline.get("voice"):
        lines.append(f"- 1인칭: 처음부터 끝까지 '{outline['voice']}' 로 씁니다")
    for key in keys:
        value = outline.get(key)
        if not value:
            continue
        if isinstance(value, list):
            lines.append(f"- {labels[key]}: " + " / ".join(value))
        else:
            lines.append(f"- {labels[key]}: {value}")
    return "\n".join(lines)


async def build_outline(client: LLMClient, form: dict) -> dict:
    """모델 호출 1회. 실패하면 빈 dict — 골자 없이 예전처럼 쓴다(생성을 막지 않는다)."""
    system = assemble.read_prompt("outline.md")
    user = assemble.build_context({k: v for k, v in form.items() if k != "outline"})
    try:
        res = await client.complete(system, user, model=form.get("model"))
        text = res.text.replace("```json", "").replace("```", "")
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end == -1:
            return {}
        out = normalize(json.loads(text[start:end + 1]))
        out["voice"] = voice_for(form)          # 모델이 아니라 입력(팀원 수)이 정한다
        out["differentiators"] = verified_differentiators(out["differentiators"], form)
        # 참고자료가 없으면 인용할 수치도 없고, 있어도 참고자료에 실제로 있는 숫자만 남긴다.
        # 모델이 "한국통계청에 따르면 70%" 같은 출처를 지어내는 일이 실호출에서 두 번 나왔다.
        out["key_stats"] = verified_stats(out["key_stats"], form.get("references"))
        return out
    except Exception:
        return {}


async def get_outline(client: LLMClient, form: dict, cache: DiskCache | None = None,
                      ttl: int = 7 * 24 * 3600) -> dict:
    """캐시 우선. 같은 키로 동시에 들어오면 락으로 한 번만 만든다."""
    cache = cache if cache is not None else DiskCache(ttl=ttl)
    key = cache_key(form)
    hit = cache.get(key)
    if hit and isinstance(hit[0], dict):
        return hit[0]

    lock = _locks.setdefault(key, asyncio.Lock())
    async with lock:
        hit = cache.get(key)                      # 락을 기다리는 동안 다른 요청이 만들었을 수 있다
        if hit and isinstance(hit[0], dict):
            return hit[0]
        outline = await build_outline(client, form)
        if outline and any(outline.values()):
            cache.set(key, [outline])
        return outline
