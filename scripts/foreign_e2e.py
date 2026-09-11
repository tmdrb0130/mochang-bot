"""외국인(영어 입력) 실사용 흉내 — 공개 API 로 인테이크 → 카드 번역 → 조사 → 생성 → 영·중·일 번역 (2026-09-03).

    .venv/Scripts/python scripts/foreign_e2e.py                       # bustartup.kr:50001 에 실제 모델 호출 (약 5분, 호출 ≈ 45회)
    .venv/Scripts/python scripts/foreign_e2e.py --base http://localhost:8000

결과: scripts/.foreign_e2e_result.json (gitignore) + 진행 로그 stdout. 요청에 X-Mochang-Test 헤더를 붙이므로 서비스 DB 에는
안 남고 백업 DB 에만 남는다 (backend/storage.py). 첫 실측(13:20~13:25): 카드·초안 전부 한국어, 번역 26개 전부 성공,
일본어 번역 1건에 '여부를' 이 남아 translate.py 에 한글 잔류 재시도를 넣었다.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import uuid
from pathlib import Path

import httpx

# 콘솔(cp949)에서 "—" 같은 글자로 죽지 않게 — drafts_delete.py·db_snapshot.py 와 같은 처방 (2026-09-05).
# 주의: from __future__ 는 파일의 첫 문장이어야 하므로 이 블록은 반드시 임포트 **뒤**에 온다.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

QIDS = ["q1", "q2", "q3_1", "q3_2", "q4_1", "q4_2", "q8", "q10"]
OUT = Path(__file__).with_name(".foreign_e2e_result.json")
HEAD = {"X-Mochang-Test": "1"}

IDEA = ("I'm an international student from Vietnam studying in Cheonan. Many foreign students struggle to find part-time jobs "
        "because job postings are only in Korean and employers worry about visa rules. I want to build a mobile app that lists "
        "part-time jobs verified as legal for D-2/D-4 visa holders, with postings auto-translated into English and Vietnamese and a "
        "simple weekly visa-hours checker. I have already talked to 12 foreign students at my university and 3 restaurant owners near "
        "campus, and all of them said they would use it.")
CAP = ("2 years as a part-time restaurant worker in Korea, TOPIK level 4, basic Flutter app development, "
       "admin of a 1,500-member Vietnamese student Facebook group.")


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


async def run_job(c: httpx.AsyncClient, kind: str, body: dict, note: str = "") -> dict:
    for _ in range(40):
        r = await c.post(f"/jobs/{kind}", json=body, headers=HEAD)
        if r.status_code == 429:
            await asyncio.sleep(5)
            continue
        r.raise_for_status()
        break
    else:
        raise RuntimeError("429 계속")
    jid = r.json()["job_id"]
    t0 = time.time()
    while True:
        await asyncio.sleep(2.5)
        s = (await c.get(f"/jobs/{jid}")).json()
        if s["status"] == "done":
            log(f"{kind} {note} done {time.time() - t0:.0f}s")
            return s["result"]
        if s["status"] == "error":
            raise RuntimeError(f"{kind} {note}: {s.get('error')}")


def card_strings(intake: dict) -> list[str]:
    """프론트(App.jsx cardStrings)와 같은 목록 — 요약·슬롯 라벨·카드 질문/이유/라벨·보기 label/hint."""
    out: list[str] = []
    if intake.get("summary"):
        out.append(intake["summary"])
    for sl in intake.get("slots") or []:
        if sl.get("label"):
            out.append(sl["label"])
    for cd in intake.get("cards") or []:
        out += [v for v in (cd.get("question"), cd.get("why"), cd.get("label")) if v]
        for o in cd.get("options") or []:
            out += [v for v in (o.get("label"), o.get("hint")) if v]
    return list(dict.fromkeys(out))


async def main(base: str) -> None:
    draft_id = str(uuid.uuid4())
    form = {"track": "tech", "idea": IDEA, "is_business": False, "current_item": "", "team": "1명", "capability": CAP, "draft_id": draft_id}
    result: dict = {"draft_id": draft_id, "idea": IDEA, "capability": CAP}
    async with httpx.AsyncClient(base_url=base, timeout=120, verify=False) as c:
        intake = await run_job(c, "intake", form, "intake")
        result["intake"] = intake
        cards = intake.get("cards") or []
        log(f"summary={intake.get('summary')!r} ready={intake.get('ready')} cards={[x['slot'] for x in cards]}")
        strings = card_strings(intake)
        tr = await run_job(c, "translate", {"lang": "en", "texts": strings}, f"cards en ({len(strings)})")
        result["card_i18n_en"] = dict(zip(strings, tr["translations"]))
        answers = []
        for cd in cards:                                       # 첫 보기 선택 (number 카드는 12명)
            if cd.get("options"):
                label = cd["options"][0]["label"]
                ans = [f"{label} (12명)"] if cd.get("type") == "number" else [label]
                answers.append({"slot": cd["slot"], "label": cd.get("label", ""), "answer": ans, "unknown": False})
        form_a = {**form, "answers": answers}
        result["answers"] = answers
        r_sem, g_sem = asyncio.Semaphore(3), asyncio.Semaphore(3)
        texts: dict[str, str] = {}
        research: dict[str, dict] = {}

        async def one(qid: str) -> None:
            async with r_sem:
                rs = await run_job(c, "research", {**form_a, "question_id": qid}, qid)
            facts = rs.get("facts") or []
            research[qid] = {"facts": len(facts), "queries": rs.get("queries")}
            async with g_sem:
                g = await run_job(c, "generate", {**form_a, "question_id": qid, "style": "logic", **({"references": facts} if facts else {})}, qid)
            texts[qid] = g["text"]

        await asyncio.gather(*(one(q) for q in QIDS))
        result["research"], result["texts"] = research, texts
        trans: dict[str, dict[str, str]] = {}
        t_sem = asyncio.Semaphore(4)

        async def tr_one(qid: str, lang: str) -> None:
            async with t_sem:
                r = await run_job(c, "translate", {"lang": lang, "texts": [texts[qid]]}, f"{qid} {lang}")
            trans.setdefault(qid, {})[lang] = r["translations"][0]

        await asyncio.gather(*(tr_one(q, lang) for q in QIDS for lang in ("en", "zh", "ja")))
        result["translations"] = trans
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"ALL DONE → {OUT}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default="https://bustartup.kr:50001/api")
    asyncio.run(main(ap.parse_args().base))
