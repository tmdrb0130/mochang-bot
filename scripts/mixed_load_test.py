"""혼합 부하 테스트 — 한국인 N명 + 외국인 M명이 **동시에** 프론트와 같은 전체 흐름을 돈다 (2026-09-07).

    .venv/Scripts/python scripts/mixed_load_test.py --kr 2 --fr 1 --think 5 --json docs/measurements/mix_smoke.json
    .venv/Scripts/python scripts/mixed_load_test.py --kr 40 --fr 20 --salt "3단계" --json docs/measurements/mix_40_20.json

기존 스크립트가 못 보던 것을 본다:
  - `live_load_test.py` 는 한국인만, `translate_load_test.py` 는 번역만, `foreign_e2e.py` 는 외국인 **1명**.
    한국인과 외국인이 **같은 큐를 동시에 쓰는** 상황이 측정된 적이 없다.
  - 카드 번역이 고정 25문구가 아니라 **인테이크 응답의 실제 문구**(약 80개, 2청크)를 번역한다.
  - "다른 보기 보기"(카드 재생성 + 메모 = 추가 정보 입력)와 이어쓰기를 일부 사용자가 실제로 누른다.

사용자 1명이 하는 일
  한국인: 인테이크 → 카드 답(일부는 재생성 후 답) → [think] → 조사 3동시 + 생성 3동시 → 일부는 이어쓰기
  외국인: 인테이크 → **카드 문구 번역** → 카드 답(일부 재생성) → [think] → 조사·생성 → **본문 번역 3동시** → 다른 언어 미리 받기

답하려는 질문
  ① 번역 전용 큐가 필요한가        → 외국인이 늘 때 **조사·인테이크의 queued_s** 가 오르는지 (지금 셋이 한 FIFO)
  ② 어디서 깨지나                  → 단계별 실패 + 429 를 scope(owner/ip/kind)별로 + 재시도 소진(12회 중 몇 회)
  ③ 최대 몇 명                     → 실패 0 을 유지하는 마지막 규모
  ④ 같은 IP(강의실)                → nginx 가 실제 IP 를 XFF 맨 뒤에 붙이므로 이 테스트는 이미 "같은 공인 IP" 다. scope=ip 429 가 0 이면 통과
  ⑤ 모르는 문제                    → 끝나고 무결성 감사: 초안 분리·생성문 8/8·조사 저장·client_id 집계·조용한 저장 거부

안전장치
  - 모든 요청에 `X-Mochang-Test` → 서비스 DB 를 건너뛰고 백업 DB 에만 `is_test=1`. 정리는 `drafts_delete.py --tests --yes --backup`.
  - `--abort-on-real-user`(기본 켬): nginx 로그를 보다가 **실사용자**가 작업을 제출하면 즉시 중단한다.
  - `--ramp` 로 시작을 흩는다 (강의실도 동시에 누르지 않는다).

⚠️ 공유 GPU 서버와 외부 검색 API 에 실부하를 건다. 학생이 없는 시간에, 사용자 승인 후에만.
"""
from __future__ import annotations

import argparse
import asyncio
import collections
import json
import random
import re
import sqlite3
import sys
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from live_load_test import pick_answers, pct                      # noqa: E402  카드 답 규칙·백분위는 기존 것을 그대로
from load_ideas import KR_IDEAS, FR_IDEAS, unique_tail            # noqa: E402  아이디어 풀 60 + 20, 캐시 회피 꼬리

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
B = "https://www.bustartup.kr:50001/api"
VLLM_METRICS = "http://localhost:30801/metrics"
NGINX_LOG = Path(r"C:\Users\bon505\Desktop\nginx\logs\access.log")
SERVICE_DB = ROOT / "backend" / ".data" / "mochang.sqlite"
BACKUP_DB = ROOT / "backend" / ".data" / "mochang-backup.sqlite"
TIMING = ROOT / "backend" / ".timing.jsonl"
SELF_IPS = {"127.0.0.1", "::1", "61.34.63.189"}      # 이 PC 에서 쏘므로 nginx 로그에는 이 IP 로 찍힌다

QS = ["q1", "q2", "q3_1", "q3_2", "q4_1", "q4_2", "q8", "q10"]
PARALLEL = 3                 # App.jsx PARALLEL / RESEARCH_PARALLEL
TRANSLATE_PARALLEL = 3       # App.jsx TRANSLATE_PARALLEL (본문 번역)
FOREIGN_LANGS = ["en", "zh", "ja"]

# (옛 소형 풀 — scripts/load_ideas.py 의 FR_IDEAS 20개로 옮겼다. 남겨 두지 않는다)
_UNUSED_FOREIGN_IDEAS = [
    ("gym-qr", "I scan a QR code on the treadmill or bike at the gym and get matched with other people working out nearby at the same time, "
               "then we run a real-time distance race and see the result right away. Many students quit after two weeks because working out alone is boring.",
     "Two years of part-time work at a fitness center, basic Flutter app development, ran a running crew of 40 people."),
    ("lab-share", "Many foreign graduate students cannot find which lab equipment is free at what time, so they waste days waiting. "
                  "I want a simple booking board for shared lab instruments that shows availability by hour and sends a reminder before the slot.",
     "Master's student in chemical engineering, managed the equipment log for my lab for one year."),
    ("halal-map", "Muslim students in Cheonan struggle to find halal food, and existing maps are outdated or in Korean only. "
                  "I want an app where students verify restaurants together and menus are auto-translated into English, Arabic and Indonesian.",
     "Ran a 900-member international student community group, worked part-time at a restaurant for 18 months."),
    ("visa-jobs", "Job postings are only in Korean and employers worry about visa rules, so foreign students take unsafe part-time work. "
                  "I want to list only jobs that are legal for D-2 and D-4 holders, auto-translated, with a weekly working-hours checker.",
     "TOPIK level 4, two years of part-time restaurant work in Korea, admin of a 1,500-member student group."),
    ("recycle-cam", "People want to recycle properly but do not know which bin an item goes into, and the rules change by city. "
                    "I want a phone camera app that names the item and shows the local rule, plus a weekly score for the dormitory floor.",
     "Computer science undergraduate, built two small image classifiers for a class project."),
    ("study-buddy", "Language exchange in our university is arranged through paper forms and most pairs stop after one meeting. "
                    "I want an app that matches by level and schedule, suggests a topic for each meeting, and tracks how many sessions actually happened.",
     "Exchange student, tutored Korean-English conversation for three semesters."),
    ("food-waste", "Many restaurants waste food, electricity and time because they cannot predict how many customers will come. "
                   "I want a simple tool that reads past sales and weather and suggests how much to prepare each morning.",
     "Worked at a bakery cafe and a cosmetics shop, studying business administration."),
    ("dorm-fix", "When something breaks in the dormitory, foreign students do not know how to report it in Korean and wait for weeks. "
                 "I want a photo-based repair request that writes the Korean report automatically and shows the progress.",
     "Lived in three different dormitories, volunteered as a floor representative for one year."),
]


TEAMS = ["팀원 없음", "1명", "2명", "3명 이상"]


def make_kr_user(i: int, run: str, tail: str) -> dict:
    name, idea, cap = KR_IDEAS[i % len(KR_IDEAS)]
    biz = (i % 9 == 0)                                   # 9명에 1명은 이미 사업자 → Q7-1 이 붙어 문항이 9개가 된다
    return {"kind": "kr", "name": f"{name}#{i}", "track": "tech", "idea": idea + tail, "team": TEAMS[i % len(TEAMS)],
            "capability": cap, "is_business": biz, "current_item": "같은 분야에서 소규모로 운영 중" if biz else "",
            "draft_id": f"mx{run}-k{i:03d}-{uuid.uuid4().hex[:12]}", "client_id": f"mx{run}-kc{i:03d}-{uuid.uuid4().hex[:8]}"}


def make_fr_user(i: int, run: str, tail: str) -> dict:
    name, idea, cap = FR_IDEAS[i % len(FR_IDEAS)]
    return {"kind": "fr", "name": f"{name}#{i}", "track": "tech", "idea": idea + tail,
            "team": TEAMS[i % 3], "capability": cap, "is_business": False, "current_item": "",
            "lang": FOREIGN_LANGS[i % len(FOREIGN_LANGS)],
            "draft_id": f"mx{run}-f{i:03d}-{uuid.uuid4().hex[:12]}", "client_id": f"mx{run}-fc{i:03d}-{uuid.uuid4().hex[:8]}"}


def payload(u: dict) -> dict:
    return {k: v for k, v in u.items() if k not in ("name", "kind", "lang", "client_id")}


def card_strings(intake: dict) -> list[str]:
    """프론트 App.jsx cardStrings 와 같은 목록 — 요약·슬롯 라벨·카드 질문/이유/라벨·보기 label/hint.
    실제 카드에서는 80개 안팎이 나온다(번역은 BATCH 40 이라 2청크 + 필요하면 재번역)."""
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


def adopt_key(form: dict, snap: dict) -> None:
    key = ((snap or {}).get("result") or {}).get("draft_key")
    if key:
        form["draft_key"] = key


class Aborted(Exception):
    """실사용자가 감지되어 중단."""


STOP = asyncio.Event()
BUSY_RETRY = 12          # 프론트 api.js BUSY_RETRY 와 같게
BUSY_DELAY = 5.0         # 프론트 BUSY_DELAY (지터 없음 — 그대로 재현한다)


async def job(c: httpx.AsyncClient, kind: str, body: dict, hdr: dict, rec: dict, label: str) -> dict:
    """/jobs/{kind} 제출(429 면 프론트처럼 5초 뒤 재시도, 최대 12회) → 2.5초 폴링 → 최종 snapshot.
    프론트와 같은 규칙이라 '재시도 창 60초를 넘겨 실패' 하는 지점이 그대로 드러난다."""
    if STOP.is_set():
        raise Aborted()
    retries, reasons = 0, []
    for i in range(BUSY_RETRY + 1):
        r = await c.post(f"{B}/jobs/{kind}", json=body, headers=hdr, timeout=60)
        if r.status_code != 429:
            break
        retries += 1
        try:
            reasons.append((r.json().get("detail") or "")[:60])
        except Exception:
            pass
        if i >= BUSY_RETRY:
            rec.setdefault("busy_out", []).append(label)
            raise RuntimeError(f"429 재시도 소진({retries}회)")
        await asyncio.sleep(BUSY_DELAY)
    r.raise_for_status()
    j = r.json()
    if retries:
        rec.setdefault("retries", []).append({"label": label, "n": retries, "why": reasons[-1] if reasons else ""})
    fails = 0
    while True:
        if STOP.is_set():
            raise Aborted()
        await asyncio.sleep(2.5)
        try:
            s = (await c.get(f"{B}/jobs/{j['job_id']}", timeout=60)).json()
        except Exception:
            fails += 1
            if fails > 3:
                raise
            continue
        fails = 0
        if s["status"] in ("done", "error"):
            s["_position"] = j.get("position")
            s["_retries"] = retries
            return s


def _stage(rec: dict, name: str, t0: float, snap: dict | None = None, err: Exception | None = None, **extra) -> None:
    d = {"ok": err is None and (snap or {}).get("status") == "done", "sec": round(time.time() - t0, 1), **extra}
    if snap:
        d.update(queued=snap.get("queued_seconds"), run=snap.get("elapsed_seconds"), retries=snap.get("_retries") or 0)
        if snap.get("status") != "done":
            d["error"] = (snap.get("error") or "")[:100]
    if err is not None:
        d["error"] = f"{type(err).__name__}: {str(err)[:90]}"
    rec.setdefault(name, []).append(d) if name in ("research", "generate", "translate", "extend") else rec.__setitem__(name, d)


async def run_user(c: httpx.AsyncClient, u: dict, args, t_start: float) -> dict:
    """한국인·외국인 공통 흐름. 외국인만 카드 번역·본문 번역·다른 언어 미리 받기가 붙는다."""
    hdr = {"X-Mochang-Test": "1", "X-Mochang-Client": u["client_id"]}
    form = payload(u)
    rec: dict = {"user": u["name"], "kind": u["kind"], "draft_id": u["draft_id"], "lang": u.get("lang"),
                 "t0": round(time.time() - t_start, 1)}
    foreign = u["kind"] == "fr"

    # ── ① 인테이크 ──
    t = time.time()
    cards: list = []
    try:
        s = await job(c, "intake", form, hdr, rec, "intake")
        adopt_key(form, s)
        it = s.get("result") or {}
        cards = it.get("cards") or []
        _stage(rec, "intake", t, s, cards=len(cards), facts=len(((it.get("research") or {}).get("facts") or [])))
    except Aborted:
        raise
    except Exception as e:
        _stage(rec, "intake", t, None, e)
        it = {}

    # ── ②-a 외국인: 카드 문구 번역 (프론트와 같이 인테이크 직후 같은 흐름에서) ──
    if foreign and cards:
        strings = card_strings(it)
        t = time.time()
        try:
            s = await job(c, "translate", {"lang": u["lang"], "texts": strings, "draft_id": u["draft_id"], **({"draft_key": form["draft_key"]} if form.get("draft_key") else {})}, hdr, rec, "cards")
            tr = (s.get("result") or {}).get("translations") or []
            empty = sum(1 for x in tr if not (x or "").strip())
            _stage(rec, "cards_tr", t, s, n=len(strings), empty=empty)
        except Aborted:
            raise
        except Exception as e:
            _stage(rec, "cards_tr", t, None, e, n=len(strings))

    # ── ②-b 일부 사용자는 "다른 보기 보기" (카드 재생성 + 메모 = 추가 정보 입력) ──
    if cards and random.random() < args.regen_rate:
        slot = cards[len(cards) // 2].get("slot")
        seen = {slot: [o["label"] for o in (cards[len(cards) // 2].get("options") or [])]}
        note = "제가 직접 겪은 상황에 더 가까운 보기를 보고 싶습니다" if not foreign else "Please show options closer to what I actually experienced"
        t = time.time()
        try:
            s = await job(c, "intake_regenerate", {**form, "slots": [slot], "seen": seen, "note": note, "keep": {}}, hdr, rec, "regen")
            new_cards = (s.get("result") or {}).get("cards") or []
            by_slot = {cd.get("slot"): cd for cd in cards}
            for cd in new_cards:
                by_slot[cd.get("slot")] = cd
            cards = list(by_slot.values())
            _stage(rec, "regen", t, s, slot=slot, n=len(new_cards))
        except Aborted:
            raise
        except Exception as e:
            _stage(rec, "regen", t, None, e, slot=slot)

    # ── ③ 카드 답 + 사람이 생각하는 시간 ──
    form["answers"] = pick_answers(cards, hash(u["name"]) % 4)
    rec["answers"] = len(form["answers"])
    think = args.think if cards else 0
    rec["think"] = think

    # ── ④ 조사 3동시 → 끝난 문항부터 생성 3동시 (프론트 runPipeline) ──
    refs: dict[str, list] = {}
    texts: dict[str, str] = {}
    ready: list[str] = []
    research_done = False
    first_draft = {"t": None}

    async def research(q):
        t = time.time()
        try:
            s = await job(c, "research", {**form, "question_id": q}, hdr, rec, f"research:{q}")
            adopt_key(form, s)
            res = s.get("result") or {}
            refs[q] = res.get("facts") or []
            _stage(rec, "research", t, s, q=q, facts=len(refs[q]), cached=res.get("cached"))
        except Aborted:
            raise
        except Exception as e:
            refs[q] = []
            _stage(rec, "research", t, None, e, q=q)
        ready.append(q)

    async def generate(q):
        t = time.time()
        try:
            s = await job(c, "generate", {**form, "question_id": q, "style": "logic", "references": refs.get(q) or []}, hdr, rec, f"generate:{q}")
            adopt_key(form, s)
            txt = ((s.get("result") or {}).get("text") or "")
            texts[q] = txt
            _stage(rec, "generate", t, s, q=q, chars=len(txt))
            if first_draft["t"] is None:
                first_draft["t"] = round(time.time() - t_start - rec["t0"], 1)
        except Aborted:
            raise
        except Exception as e:
            _stage(rec, "generate", t, None, e, q=q)

    async def research_pool():
        nonlocal research_done
        sem = asyncio.Semaphore(PARALLEL)

        async def one(q):
            async with sem:
                await research(q)
        try:
            await asyncio.gather(*(one(q) for q in QS))
        finally:
            research_done = True

    async def writers():
        await asyncio.sleep(think)
        sem = asyncio.Semaphore(PARALLEL)

        async def worker():
            while True:
                if ready:
                    q = ready.pop(0)
                    async with sem:
                        await generate(q)
                elif research_done:
                    return
                else:
                    await asyncio.sleep(0.25)
        await asyncio.gather(*(worker() for _ in range(PARALLEL)))

    await asyncio.gather(research_pool(), writers())
    rec["first_draft"] = first_draft["t"]

    # ── ⑤ 외국인: 본문 번역 (동시 3) + 다른 언어 미리 받기 ──
    if foreign and texts:
        sem = asyncio.Semaphore(TRANSLATE_PARALLEL)

        async def body_tr(q):
            t = time.time()
            async with sem:
                try:
                    s = await job(c, "translate", {"lang": u["lang"], "texts": [texts[q]], "draft_id": u["draft_id"], **({"draft_key": form["draft_key"]} if form.get("draft_key") else {})}, hdr, rec, f"body:{q}")
                    tr = (s.get("result") or {}).get("translations") or [""]
                    _stage(rec, "translate", t, s, q=q, chars=len(texts[q]), empty=int(not (tr[0] or "").strip()))
                except Aborted:
                    raise
                except Exception as e:
                    _stage(rec, "translate", t, None, e, q=q)
        await asyncio.gather(*(body_tr(q) for q in texts))

        if not args.no_prefetch and cards:
            others = [l for l in FOREIGN_LANGS if l != u["lang"]]
            strings = card_strings(it)
            async def pre(lang):
                t = time.time()
                try:
                    s = await job(c, "translate", {"lang": lang, "texts": strings, "draft_id": u["draft_id"], **({"draft_key": form["draft_key"]} if form.get("draft_key") else {})}, hdr, rec, f"pre:{lang}")
                    _stage(rec, "prefetch", t, s, lang=lang, n=len(strings))
                except Aborted:
                    raise
                except Exception as e:
                    _stage(rec, "prefetch", t, None, e, lang=lang)
            await asyncio.gather(*(pre(l) for l in others))

    # ── ⑥ 일부는 이어쓰기 ──
    long_qs = [q for q in texts if q not in ("q1", "q10") and len(texts[q]) > 100]
    if long_qs and random.random() < args.extend_rate:
        q = long_qs[0]
        t = time.time()
        try:
            s = await job(c, "extend", {**form, "question_id": q, "style": "logic", "current": texts[q],
                                        "references": refs.get(q) or []}, hdr, rec, f"extend:{q}")
            added = ((s.get("result") or {}).get("added") or 0)
            _stage(rec, "extend", t, s, q=q, added=added)
        except Aborted:
            raise
        except Exception as e:
            _stage(rec, "extend", t, None, e, q=q)

    rec["total_sec"] = round(time.time() - t_start - rec["t0"], 1)
    return rec


# ── 감시: 서버 상태 5초마다 ──
async def monitor(c: httpx.AsyncClient, out: list) -> None:
    while not STOP.is_set():
        row = {"t": round(time.time(), 1)}
        try:
            j = (await c.get(f"{B}/jobs", timeout=10)).json()
            row["api"] = {"gen_run": j.get("running"), "gen_q": j.get("queued"),
                          "res_run": (j.get("research") or {}).get("running"), "res_q": (j.get("research") or {}).get("queued")}
        except Exception:
            row["api"] = None
        try:
            m = (await c.get(VLLM_METRICS, timeout=10)).text
            def g(k):
                mm = re.search(rf"^{k}\{{[^}}]*}}\s+([0-9.e+]+)$", m, re.M)
                return round(float(mm.group(1)), 2) if mm else None
            row["vllm"] = {"run": g("vllm:num_requests_running"), "wait": g("vllm:num_requests_waiting"),
                           "kv": g("vllm:gpu_cache_usage_perc"), "preempt": g("vllm:num_preemptions_total")}
        except Exception:
            row["vllm"] = None
        out.append(row)
        await asyncio.sleep(5)


# ── 안전장치: 실사용자 감지 ──
async def real_user_watch(enabled: bool, found: list) -> None:
    """nginx 로그 꼬리를 보다가 우리(이 PC) 가 아닌 IP 가 작업을 제출하면 STOP 을 세운다."""
    if not enabled or not NGINX_LOG.exists():
        return
    pat = re.compile(rb'^(\S+) .* "POST /api/jobs/')
    pos = NGINX_LOG.stat().st_size
    while not STOP.is_set():
        await asyncio.sleep(4)
        try:
            size = NGINX_LOG.stat().st_size
            if size < pos:
                pos = 0
            with NGINX_LOG.open("rb") as f:
                f.seek(pos)
                chunk = f.read()
                pos = f.tell()
        except Exception:
            continue
        for line in chunk.splitlines():
            m = pat.match(line)
            if m and m.group(1).decode() not in SELF_IPS:
                found.append(m.group(1).decode())
                print(f"\n!! 실사용자 감지({found[-1]}) — 즉시 중단합니다. 진행 중 작업은 서버에서 계속 끝납니다.", flush=True)
                STOP.set()
                return


# ── 끝나고: 무결성 감사 ──
def audit(run: str, started: datetime, users: list, recs: list) -> dict:
    out: dict = {}
    exp_kr = sum(1 for u in users if u["kind"] == "kr")
    exp_fr = sum(1 for u in users if u["kind"] == "fr")
    like = f"mx{run}-%"
    try:
        con = sqlite3.connect(f"file:{BACKUP_DB.as_posix()}?mode=ro", uri=True)
        rows = con.execute("""select draft_id, client_id, is_test,
            (select count(*) from generations g where g.draft_id=d.draft_id) gc,
            (select count(*) from research r where r.draft_id=d.draft_id) rc
            from drafts d where draft_id like ?""", (like,)).fetchall()
        out["drafts"] = len(rows)
        out["expected_drafts"] = exp_kr + exp_fr
        out["split_or_missing"] = len(rows) - (exp_kr + exp_fr)
        out["gen_full"] = sum(1 for r in rows if r[3] >= 8)
        out["gen_short"] = [{"id": r[0][-8:], "gen": r[3]} for r in rows if r[3] < 8][:15]
        out["research_avg"] = round(sum(r[4] for r in rows) / len(rows), 1) if rows else 0
        out["research_low"] = sum(1 for r in rows if r[4] < 5)
        out["client_ids"] = len({r[1] for r in rows if r[1]})
        out["is_test_all"] = all(r[2] for r in rows)
        # ── 조용한 유실 직접 탐지 (2026-09-07) ──
        # "학생 화면에는 나왔는데 DB 에는 없다" 를 잡는 유일한 방법: **클라이언트가 실제로 받은 것**과 DB 를 대조한다.
        # 오늘 실사용에서 난 사고가 정확히 이 유형이었다 — 작업은 done, 화면엔 글이 떴는데 저장만 거부됐다.
        # DB 의 generations 에는 extend 도 섞이므로 **부족한 경우만** 문제로 본다.
        db = {r[0]: (r[3], r[4]) for r in rows}
        lost = []
        for r in recs:
            did = r.get("draft_id")
            if not did:
                continue
            got_gen = sum(1 for g in (r.get("generate") or []) if g.get("ok"))
            got_res = sum(1 for x in (r.get("research") or []) if x.get("ok"))
            got_intake = bool((r.get("intake") or {}).get("ok"))
            if did not in db:
                if got_gen or got_res or got_intake:
                    lost.append({"id": did[-8:], "kind": r.get("kind"), "받음": f"인테이크{int(got_intake)}·조사{got_res}·생성{got_gen}", "DB": "행 자체가 없음"})
                continue
            dgen, dres = db[did]
            if dgen < got_gen:
                lost.append({"id": did[-8:], "kind": r.get("kind"), "받음": f"생성{got_gen}", "DB": f"생성{dgen}"})
            elif dres < got_res:
                lost.append({"id": did[-8:], "kind": r.get("kind"), "받음": f"조사{got_res}", "DB": f"조사{dres}"})
        out["silent_loss"] = lost
        out["silent_loss_n"] = len(lost)
        con.close()
    except Exception as e:
        out["db_error"] = str(e)[:120]
    try:
        con = sqlite3.connect(f"file:{SERVICE_DB.as_posix()}?mode=ro", uri=True)
        out["service_db_polluted"] = con.execute("select count(*) from drafts where draft_id like ?", (like,)).fetchone()[0]
        con.close()
    except Exception:
        pass
    cut = started.isoformat(timespec="seconds")
    ev = collections.Counter()
    scopes = collections.Counter()
    qwait = collections.defaultdict(list)
    try:
        for line in TIMING.read_text(encoding="utf-8", errors="replace").splitlines()[-200000:]:
            if not line.startswith("{"):
                continue
            d = json.loads(line)
            if (d.get("ts") or "") < cut:
                continue
            e = d.get("event")
            ev[e] += 1
            if e == "limit":
                scopes[f"{d.get('kind')}/{d.get('scope')}"] += 1
            elif e == "job" and d.get("status") == "done":
                qwait[d.get("kind")].append(float(d.get("queued_s") or 0))
    except Exception as e:
        out["timing_error"] = str(e)[:120]
    out["events"] = dict(ev)
    out["limit_429"] = dict(scopes)
    out["queued_p50"] = {k: pct(v, .5) for k, v in qwait.items()}
    out["queued_p95"] = {k: pct(v, .95) for k, v in qwait.items()}
    out["storage_refused"] = ev.get("storage_refused", 0)
    out["translate_empty"] = ev.get("translate_empty", 0)
    out["storage_error"] = ev.get("storage_error", 0)
    return out


def summarize(recs: list, mon: list, aud: dict, wall: float, args) -> dict:
    def flat(kind, name):
        out = []
        for r in recs:
            if kind and r["kind"] != kind:
                continue
            v = r.get(name)
            if isinstance(v, list):
                out += v
            elif isinstance(v, dict):
                out.append(v)
        return out

    def stat(items, label):
        if not items:
            return None
        ok = [x for x in items if x.get("ok")]
        return {"n": len(items), "ok": len(ok), "fail": len(items) - len(ok),
                "sec_p50": pct([x["sec"] for x in ok], .5), "sec_p95": pct([x["sec"] for x in ok], .95),
                "queued_p50": pct([x.get("queued") for x in ok], .5), "queued_p95": pct([x.get("queued") for x in ok], .95),
                "run_p50": pct([x.get("run") for x in ok], .5),
                "retry_total": sum(x.get("retries") or 0 for x in items)}

    s: dict = {"args": {"kr": args.kr, "fr": args.fr, "think": args.think, "ramp": args.ramp,
                        "regen_rate": args.regen_rate, "extend_rate": args.extend_rate, "salt": args.salt},
               "wall_s": round(wall, 1), "users": len(recs)}
    for kind, tag in (("kr", "한국인"), ("fr", "외국인"), (None, "전체")):
        block = {}
        for name in ("intake", "cards_tr", "regen", "research", "generate", "translate", "prefetch", "extend"):
            st = stat(flat(kind, name), name)
            if st:
                block[name] = st
        rs = [r for r in recs if kind is None or r["kind"] == kind]
        if rs:
            block["user_total"] = {"p50": pct([r.get("total_sec") for r in rs], .5), "p95": pct([r.get("total_sec") for r in rs], .95),
                                   "max": pct([r.get("total_sec") for r in rs], 1.0)}
            block["first_draft"] = {"p50": pct([r.get("first_draft") for r in rs], .5), "p95": pct([r.get("first_draft") for r in rs], .95)}
            block["chars_avg"] = round(sum(g.get("chars", 0) for r in rs for g in r.get("generate", []) if g.get("ok") and g.get("q") not in ("q1", "q10"))
                                       / max(1, sum(1 for r in rs for g in r.get("generate", []) if g.get("ok") and g.get("q") not in ("q1", "q10"))))
        s[tag] = block
    # 조사가 캐시로 때워졌는지 — 높으면 이 회차는 "부하" 가 아니라 "파일 읽기" 를 잰 것이다
    rs = [x for r in recs for x in (r.get("research") or []) if x.get("ok")]
    s["research_cached_pct"] = round(100 * sum(1 for x in rs if x.get("cached")) / len(rs)) if rs else None
    s["busy_out"] = sum(len(r.get("busy_out") or []) for r in recs)      # 429 재시도 12회를 다 쓰고 실패한 요청
    s["retry_calls"] = sum(len(r.get("retries") or []) for r in recs)
    peaks = [m for m in mon if m.get("api")]
    if peaks:
        s["server_peak"] = {"gen_q": max(m["api"]["gen_q"] or 0 for m in peaks), "res_q": max(m["api"]["res_q"] or 0 for m in peaks),
                            "gen_run": max(m["api"]["gen_run"] or 0 for m in peaks), "res_run": max(m["api"]["res_run"] or 0 for m in peaks)}
    vs = [m["vllm"] for m in mon if m.get("vllm")]
    if vs:
        s["vllm_peak"] = {"run": max((v["run"] or 0) for v in vs), "wait": max((v["wait"] or 0) for v in vs),
                          "kv": max((v["kv"] or 0) for v in vs), "preempt_end": vs[-1]["preempt"]}
    s["audit"] = aud
    return s


def report(s: dict) -> None:
    a = s["args"]
    print("\n" + "=" * 78)
    print(f"혼합 부하 결과 — 한국인 {a['kr']} + 외국인 {a['fr']}  ·  전체 {s['wall_s']}초  ·  think {a['think']}s  ramp {a['ramp']}s")
    print("=" * 78)
    for tag in ("한국인", "외국인", "전체"):
        b = s.get(tag) or {}
        if not b:
            continue
        print(f"\n[{tag}]")
        print(f"  {'단계':10s} {'건수':>5s} {'실패':>4s} {'체감p50':>8s} {'p95':>7s} {'대기p50':>8s} {'대기p95':>8s} {'실행p50':>8s} {'재시도':>5s}")
        for name in ("intake", "cards_tr", "regen", "research", "generate", "translate", "prefetch", "extend"):
            st = b.get(name)
            if not st:
                continue
            print(f"  {name:10s} {st['n']:5d} {st['fail']:4d} {str(st['sec_p50']):>8s} {str(st['sec_p95']):>7s} "
                  f"{str(st['queued_p50']):>8s} {str(st['queued_p95']):>8s} {str(st['run_p50']):>8s} {st['retry_total']:5d}")
        if b.get("user_total"):
            print(f"  1명 전체 p50 {b['user_total']['p50']}s / p95 {b['user_total']['p95']}s / 최대 {b['user_total']['max']}s"
                  f"   첫 초안 p50 {b['first_draft']['p50']}s   긴 문항 평균 {b.get('chars_avg')}자")
    cp = s.get("research_cached_pct")
    mark = "정상" if (cp or 0) <= 15 else "⚠️ 캐시가 부하를 대신 먹었다 — 이 회차는 다시 재야 한다"
    print(f"\n[조사 캐시]  캐시로 때운 조사 {cp}%  ({mark})")
    print(f"[상한·재시도]  429 를 맞은 호출 {s['retry_calls']}건 · **재시도 12회를 다 쓰고 실패 {s['busy_out']}건**")
    print(f"  걸린 상한(scope): {s['audit'].get('limit_429') or '없음'}")
    if s.get("server_peak"):
        p = s["server_peak"]; print(f"[서버 최고]  생성 큐 실행 {p['gen_run']}/대기 {p['gen_q']} · 조사 큐 실행 {p['res_run']}/대기 {p['res_q']}")
    if s.get("vllm_peak"):
        v = s["vllm_peak"]; print(f"[vLLM 최고]  동시 {v['run']} · 대기 {v['wait']} · KV {v['kv']} · preemption(끝) {v['preempt_end']}")
    q5, q95 = s["audit"].get("queued_p50") or {}, s["audit"].get("queued_p95") or {}
    print(f"[큐 대기, 서버 기록]  p50 {q5}  /  p95 {q95}")
    print("   → 번역이 늘 때 research·intake 의 대기가 오르면 **번역 전용 큐**가 답이다 (지금 셋이 한 FIFO)")
    ad = s["audit"]
    print("\n[무결성 감사]")
    print(f"  초안 {ad.get('drafts')} / 기대 {ad.get('expected_drafts')}  → 차이 {ad.get('split_or_missing')} "
          f"({'정상' if ad.get('split_or_missing') == 0 else '초안 분리 또는 저장 누락!'})")
    print(f"  생성문 8건 이상: {ad.get('gen_full')}개" + (f"  · 모자란 것 {ad.get('gen_short')}" if ad.get("gen_short") else ""))
    print(f"  초안당 조사 저장 평균 {ad.get('research_avg')} (5건 미만 {ad.get('research_low')}개)")
    print(f"  client_id 고유 {ad.get('client_ids')} / 사용자 {ad.get('expected_drafts')}"
          f"  · 전부 is_test: {ad.get('is_test_all')}  · 서비스 DB 오염: {ad.get('service_db_polluted')}건")
    print(f"  storage_refused {ad.get('storage_refused')} · translate_empty {ad.get('translate_empty')} · storage_error {ad.get('storage_error')}")
    n = ad.get("silent_loss_n")
    if n:
        print(f"  ❌ **화면엔 나왔는데 DB 에 없는 것 {n}건** — 부하가 만든 조용한 유실이다:")
        for x in (ad.get("silent_loss") or [])[:12]:
            print(f"       {x['id']} ({x['kind']}) 받음 {x['받음']} / DB {x['DB']}")
    else:
        print(f"  ✅ 화면에 나온 것은 전부 DB 에 있다 (클라이언트가 받은 조사·생성 건수와 DB 대조)")
    print("=" * 78 + "\n")


async def main(args) -> int:
    run = uuid.uuid4().hex[:4]
    # 조사 캐시(7일) 회피: 사용자마다 아이디어가 다르고(풀 60/20), 그 위에 **회차마다 다른 꼬리** 한 문장을 붙인다.
    # --salt 를 주면 그 문구로 고정, 안 주면 자동 생성, --no-salt 면 안 붙인다(캐시를 일부러 태울 때).
    rng = random.Random(f"{run}{args.seed}")

    def tail(foreign: bool) -> str:
        if args.no_salt:
            return ""
        return args.salt if args.salt else unique_tail(rng, foreign)

    users = [make_kr_user(i, run, tail(False)) for i in range(args.kr)] + [make_fr_user(i, run, tail(True)) for i in range(args.fr)]
    random.Random(args.seed).shuffle(users)
    started = datetime.now()
    print(f"시작 {started:%H:%M:%S}  run={run}  한국인 {args.kr} + 외국인 {args.fr}  ramp {args.ramp}s  think {args.think}s")
    print(f"  아이디어 풀 한국인 {len(KR_IDEAS)} · 외국인 {len(FR_IDEAS)}"
          + ("  ·  꼬리 없음(캐시를 일부러 탄다)" if args.no_salt else ("  ·  꼬리 고정: " + args.salt if args.salt else "  ·  회차마다 다른 꼬리 자동 생성")))
    print(f"  대상 {B}  ·  모든 요청에 X-Mochang-Test (서비스 DB 안 건드림)"
          + ("  ·  실사용자 감지 시 중단" if not args.no_abort else "  ·  ⚠️ 실사용자 감지 꺼짐"))
    t_start = time.time()
    mon: list = []
    real: list = []
    recs: list = []

    async with httpx.AsyncClient(timeout=120, verify=False) as c:
        async def one(idx, u):
            if args.ramp:
                await asyncio.sleep(args.ramp * idx / max(1, len(users)))
            try:
                r = await run_user(c, u, args, t_start)
            except Aborted:
                r = {"user": u["name"], "kind": u["kind"], "aborted": True}
            except Exception as e:
                r = {"user": u["name"], "kind": u["kind"], "fatal": f"{type(e).__name__}: {str(e)[:100]}"}
            recs.append(r)
            done = len(recs)
            print(f"  [{time.strftime('%H:%M:%S')}] {done}/{len(users)} 완료  ({u['kind']} {u['name'][:20]})", flush=True)

        mon_task = asyncio.create_task(monitor(c, mon))
        watch_task = asyncio.create_task(real_user_watch(not args.no_abort, real))
        await asyncio.gather(*(one(i, u) for i, u in enumerate(users)))
        STOP.set()
        mon_task.cancel()
        watch_task.cancel()
        await asyncio.gather(mon_task, watch_task, return_exceptions=True)

    wall = time.time() - t_start
    print(f"\n작업 끝 — 서버 기록이 쌓이도록 8초 기다립니다…")
    await asyncio.sleep(8)
    aud = audit(run, started, users, [r for r in recs if not r.get("fatal")])
    s = summarize([r for r in recs if not r.get("fatal") and not r.get("aborted")], mon, aud, wall, args)
    s["aborted"] = bool(real)
    s["real_user_ips"] = real
    s["fatal"] = [r for r in recs if r.get("fatal")]
    report(s)
    if real:
        print(f"⚠️ 실사용자({real}) 때문에 중간에 멈췄습니다 — 이 결과는 부분 측정입니다.\n")
    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(json.dumps({"summary": s, "users": recs, "monitor": mon}, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"원본 → {args.json}")
    print(f"테스트 데이터 정리: .venv\\Scripts\\python scripts\\drafts_delete.py --tests --yes --backup")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--kr", type=int, default=10, help="한국인 수")
    ap.add_argument("--fr", type=int, default=5, help="외국인 수 (카드·본문 번역까지 한다)")
    ap.add_argument("--think", type=int, default=45, help="카드에 답하는 사람 시간(초)")
    ap.add_argument("--ramp", type=int, default=120, help="이 시간에 걸쳐 사용자가 나눠 들어온다(초). 0 이면 전원 동시")
    ap.add_argument("--regen-rate", type=float, default=0.3, help="'다른 보기 보기'(카드 재생성 + 메모)를 누르는 비율")
    ap.add_argument("--extend-rate", type=float, default=0.2, help="이어쓰기를 누르는 비율")
    ap.add_argument("--no-prefetch", action="store_true", help="외국인의 '다른 언어 미리 받기'를 끈다")
    ap.add_argument("--salt", default="", help="아이디어 끝 문구를 직접 지정 (비우면 회차마다 자동 생성 — 보통 비워 둔다)")
    ap.add_argument("--no-salt", action="store_true", help="꼬리를 안 붙인다 — 조사 캐시를 **일부러** 태우는 경우(같은 아이디어를 여러 명이 낸 상황 재현)")
    ap.add_argument("--seed", type=int, default=0, help="사용자 섞는 순서 고정")
    ap.add_argument("--no-abort", action="store_true", help="실사용자가 들어와도 멈추지 않는다 (권장하지 않음)")
    ap.add_argument("--json", default="", help="원본 결과 저장 경로")
    raise SystemExit(asyncio.run(main(ap.parse_args())))
