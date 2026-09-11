"""외국인 지원자가 여러 명일 때의 번역 부하 — 서버 전체 한도(translate.global_limit)를 실제로 때려 본다 (2026-09-05).

    .venv/Scripts/python scripts/translate_load_test.py --users 15
    .venv/Scripts/python scripts/translate_load_test.py --users 15 --json docs/measurements/2026-09-05_translate15.json
    .venv/Scripts/python scripts/translate_load_test.py --users 15 --base http://localhost:8000

왜 따로 있나: `live_load_test.py` 는 **한국어 사용자만** 흉내 낸다(translate 호출이 0건). 그래서 2026-09-04 에 넣은
번역 상한 세 개 — 초안당 10 · 서버 전체 30 · vLLM 우선순위 50 — 가 실측되지 않는다. 특히 **전체 한도는 서버 단위**라
몇 명으로는 절대 안 걸리고, 외국인이 여러 명 몰릴 때만 드러난다.

한 명이 하는 일 (프론트 App.jsx 와 같은 순서·같은 동시성):
  ① 카드 문구 번역 — 목록 모드 한 번(짧은 문구 CARD_STRINGS 개). 실제로는 보고 있는 카드 먼저 → 나머지 순이지만
     서버 부하 관점에서는 같은 목록 모드 호출이라 한 번으로 묶는다.
  ② 문항 본문 번역 — 평문 모드 8회를 **동시 3개**까지 (App.jsx TRANSLATE_PARALLEL).
  ③ 다른 외국어 미리 받기 — 외국어 화면에서는 나머지 두 언어의 카드 문구를 미리 받는다(--prefetch, 기본 켬).

보는 것:
  - 429 가 어느 상한에서 나는지: `owner`(초안당 10) / `ip`(천장) / `kind`(전체 30). 서버는 이유를 detail 문구로 준다.
  - 429 를 맞고 재시도해서 결국 성공하는지(프론트는 5초 뒤 재시도한다 — 여기서도 같게).
  - 본문 생성 부하와 함께 돌릴 때 번역이 여전히 빨리 오는지(우선순위 50 의 효과).

데이터: 요청에 X-Mochang-Test 를 붙이므로 서비스 DB 에 남지 않는다. 번역은 원래 storage 에 안 남지만(idea 없음)
헤더는 규칙대로 붙인다. 모델 호출: 사용자당 약 1 + 8 + 2 = 11회.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
import uuid
from pathlib import Path

import httpx

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

B = "https://bustartup.kr:50001/api"
LANGS = ["en", "zh", "ja"]
PARALLEL = 3          # App.jsx TRANSLATE_PARALLEL — 한 사람이 동시에 돌리는 본문 번역 수
QUESTIONS = 8

# 카드 문구를 흉내 낸 짧은 문장들 (질문·보기·힌트). 실제로는 80개 안팎이 한 번에 간다.
CARD_STRINGS = [
    "어떤 불편이 가장 가까운가요?", "대상 고객", "해결할 불편", "지금의 대안", "수익 방식",
    "직접 겪었다", "주변 사람에게 물어봤다", "현장을 지켜봤다", "아직 확인하지 않았다",
    "겪은 상황이 Q2 첫 문단이 됩니다", "몇 명에게 물었는지 숫자를 함께 적어 주세요",
    "가장 단순한 형태로 먼저 만들어 본다", "소수 고객에게 먼저 써 보게 한다",
    "첫 거래·계약을 한 건 성사시킨다", "필요한 허가·자격·장소를 먼저 갖춘다",
    "고객이 정말 원하는지 확인하는 방법", "가격을 어떻게 정하고 검증할지",
    "첫 고객을 어디서 어떻게 모을지", "법·인허가·위생·안전·개인정보",
    "재료·제조·유통·기관 협약 등 파트너 확보", "이 문제를 직접 겪어 봤다",
    "관련 일을 해 본 경험이 있다", "관련 전공·자격·수업을 들었다",
    "무엇을 직접 만들어 본 적이 있다", "고객이 될 사람들을 알고 있다",
]
# 문항 본문을 흉내 낸 긴 글 (실제 1,200~1,900자). 사용자·문항마다 조금씩 달라 캐시가 없다.
BODY_SEED = (
    "저는 자취를 시작한 뒤 장을 본 재료를 어디에 두었는지 잊어버려 같은 물건을 다시 사는 일이 잦았습니다. "
    "냉장고를 열어도 무엇이 남았는지 한눈에 들어오지 않아 결국 배달을 시키게 되었습니다. "
    "주변 자취생들에게 물어보니 비슷한 경험을 하고 있었고, 남은 재료를 버리는 일이 반복된다고 했습니다. "
    "그래서 재료를 기록하고 소진 순서를 알려 주는 방법이 필요하다고 판단했습니다. "
)


def body_text(user: int, q: int) -> str:
    """문항 본문 길이(1,200~1,900자)에 맞춘 글. 사용자·문항마다 달라 서버 캐시가 안 걸린다."""
    head = f"({user}-{q}번 문항) "
    return head + (BODY_SEED * 6)[: 1200 + (user * 37 + q * 53) % 700]


def hdr(i: int) -> dict:
    return {"X-Mochang-Test": "1", "X-Forwarded-For": f"10.88.{i // 250}.{i % 250 + 1}"}


async def one_call(c: httpx.AsyncClient, kind: str, body: dict, h: dict, rec: list, label: str) -> None:
    """/jobs/translate 제출 → 폴링. 429 면 프론트처럼 5초 뒤 재시도(최대 12회)하고 그 사실을 기록한다."""
    t0 = time.time()
    retries, scopes = 0, []
    for _ in range(13):
        r = await c.post(f"{B}/jobs/{kind}", json=body, headers=h, timeout=60)
        if r.status_code != 429:
            break
        retries += 1
        try:
            scopes.append((r.json().get("detail") or "")[:40])
        except Exception:
            scopes.append("?")
        await asyncio.sleep(5)
    if r.status_code != 200:
        rec.append({"label": label, "ok": False, "sec": round(time.time() - t0, 1),
                    "http": r.status_code, "retries": retries, "reasons": scopes})
        return
    jid = r.json()["job_id"]
    fails = 0
    while True:
        await asyncio.sleep(1.5)
        try:
            s = (await c.get(f"{B}/jobs/{jid}", timeout=60)).json()
        except Exception:
            fails += 1
            if fails > 3:
                rec.append({"label": label, "ok": False, "sec": round(time.time() - t0, 1),
                            "http": "poll-fail", "retries": retries, "reasons": scopes})
                return
            continue
        if s["status"] in ("done", "error"):
            out = (s.get("result") or {}).get("translations") or []
            empty = sum(1 for t in out if not str(t).strip())
            rec.append({"label": label, "ok": s["status"] == "done", "sec": round(time.time() - t0, 1),
                        "queued": s.get("queued_seconds"), "run": s.get("elapsed_seconds"),
                        "n": len(out), "empty": empty, "retries": retries, "reasons": scopes,
                        "error": (s.get("error") or "")[:80] or None})
            return


async def run_user(c: httpx.AsyncClient, i: int, prefetch: bool) -> dict:
    """외국인 한 명: 카드 문구 목록 번역 → 본문 8개(동시 3) → 다른 언어 카드 미리 받기."""
    h = hdr(i)
    lang = LANGS[i % len(LANGS)]
    # 프론트와 같이 draft_id 를 실어 보낸다 — 서버가 동시 제한을 초안 단위로 건다 (2026-09-05).
    did = f"tr{i:04d}-{uuid.uuid4().hex[:16]}"
    rec: list = []
    t0 = time.time()
    # ① 카드 문구 (목록 모드)
    await one_call(c, "translate", {"lang": lang, "texts": CARD_STRINGS, "draft_id": did}, h, rec, "cards")
    # ② 문항 본문 (평문 모드) — 동시 PARALLEL
    sem = asyncio.Semaphore(PARALLEL)

    async def body(q):
        async with sem:
            await one_call(c, "translate", {"lang": lang, "texts": [body_text(i, q)], "draft_id": did}, h, rec, f"body{q}")
    await asyncio.gather(*(body(q) for q in range(QUESTIONS)))
    # ③ 다른 외국어 미리 받기
    if prefetch:
        await asyncio.gather(*(one_call(c, "translate", {"lang": L, "texts": CARD_STRINGS, "draft_id": did}, h, rec, f"pre-{L}")
                               for L in LANGS if L != lang))
    return {"user": i, "lang": lang, "total_sec": round(time.time() - t0, 1), "calls": rec}


def pct(xs, p):
    xs = sorted(x for x in xs if x is not None)
    return round(xs[min(len(xs) - 1, int(len(xs) * p))], 1) if xs else None


async def main_async(users: int, prefetch: bool, out: str | None) -> int:
    print(f"외국인 {users}명 번역 부하 — 대상 {B}")
    print(f"  한 명당: 카드 목록 1회 + 본문 {QUESTIONS}회(동시 {PARALLEL})" + (f" + 다른 언어 미리 {len(LANGS)-1}회" if prefetch else ""))
    print(f"  이론상 최대 동시 번역 = {users} × {PARALLEL} = {users * PARALLEL}건 (서버 전체 한도와 비교할 것)\n")
    t0 = time.time()
    async with httpx.AsyncClient(verify=False, timeout=60,
                                 limits=httpx.Limits(max_connections=400, max_keepalive_connections=100)) as c:
        rows = await asyncio.gather(*(run_user(c, i, prefetch) for i in range(users)))
    took = round(time.time() - t0, 1)

    calls = [x for r in rows for x in r["calls"]]
    ok = [x for x in calls if x["ok"]]
    fail = [x for x in calls if not x["ok"]]
    retried = [x for x in calls if x["retries"]]
    reasons: dict[str, int] = {}
    for x in calls:
        for s in x["reasons"]:
            key = ("전체 한도" if "서버 전체" in s else "IP 천장" if "네트워크" in s else "초안당" if "동시에 진행" in s else s[:20])
            reasons[key] = reasons.get(key, 0) + 1
    summary = {
        "users": users, "prefetch": prefetch, "elapsed_s": took,
        "calls": len(calls), "ok": len(ok), "fail": len(fail),
        "sec_p50": pct([x["sec"] for x in ok], 0.5), "sec_p95": pct([x["sec"] for x in ok], 0.95),
        "run_p50": pct([x.get("run") for x in ok], 0.5),
        "queued_p50": pct([x.get("queued") for x in ok], 0.5),
        "empty_items": sum(x.get("empty") or 0 for x in ok),
        "calls_with_429": len(retried), "retry_total": sum(x["retries"] for x in calls),
        "429_reasons": reasons,
        "user_total_p50": pct([r["total_sec"] for r in rows], 0.5),
        "user_total_max": max((r["total_sec"] for r in rows), default=None),
        "errors": [x.get("error") for x in fail if x.get("error")][:5],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    if out:
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_text(json.dumps({"summary": summary, "users": rows}, ensure_ascii=False, indent=1), encoding="utf-8")
        print("저장:", out)
    return 0 if not fail else 1


def main() -> int:
    global B
    ap = argparse.ArgumentParser(description="외국인 다수의 번역 부하 (서버 전체 한도 확인)")
    ap.add_argument("--users", type=int, default=15, help="동시에 화면을 보는 외국인 수")
    ap.add_argument("--base", default=B)
    ap.add_argument("--json", default="")
    ap.add_argument("--no-prefetch", action="store_true", help="다른 외국어 미리 받기를 끈다")
    a = ap.parse_args()
    B = a.base
    return asyncio.run(main_async(a.users, not a.no_prefetch, a.json or None))


if __name__ == "__main__":
    sys.exit(main())
