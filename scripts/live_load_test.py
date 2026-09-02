"""실서비스 부하 테스트 — 사용자 N명이 **프론트와 똑같은 전체 흐름**으로 동시에 신청서를 만드는 상황.

    .venv/Scripts/python scripts/live_load_test.py --users 40 --json docs/measurements/load40.json
    .venv/Scripts/python scripts/live_load_test.py --users 60 --json docs/measurements/load60.json

사용자 1명이 하는 일 (App.jsx 와 같은 순서·같은 동시성):
  ① 아이디어·팀원·사업자 여부·역량 입력 → /jobs/intake + 폴링 (웹 조사 포함)  → 카드
  ② 카드마다 다르게 답 (첫 보기 / 둘째 보기+직접 문장 / 모름 / 직접 입력)
  ③+④ 문항별 파이프라인: 조사(/jobs/research, 동시 3)가 끝난 문항부터 바로 생성(/jobs/generate, 동시 3) — 프론트 runPipeline 과 같음
사용자마다 **아이디어 문장이 다르다** (60개 풀) → 조사 캐시가 안 걸려 검색·본문 추출·사실 추출이 실제로 돈다.
사용자마다 다른 X-Forwarded-For → IP 당 동시 작업 제한(max_jobs_per_client)이 사용자별로 걸린다.

기록: 단계별(인테이크·조사·생성) 성공/실패·소요 p50/p95, 문항 대기(queued)/실행(run), 사용자 1명 전체 완료 시간,
      5초마다 API 큐(running/queued)·vLLM(running/waiting/KV/preemption). 본문 글자 수 분포.
서버 쪽 단계별 시간은 backend/.timing.jsonl → scripts/timing_report.py --last N 으로 같이 본다.

⚠️ 공유 GPU 서버·외부 검색 API 에 실부하를 건다. 사용자 승인 후에만.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import statistics
import sys
import time

import httpx

sys.stdout.reconfigure(encoding="utf-8")
B = "https://www.bustartup.kr:50001/api"
VLLM_METRICS = "http://localhost:30801/metrics"       # 이 PC 의 SSH 터널 (Qwen). 라마면 30800
QS = ["q1", "q2", "q3_1", "q3_2", "q4_1", "q4_2", "q8", "q10"]
PARALLEL = 3                                          # App.jsx PARALLEL / RESEARCH_PARALLEL

# ── 아이디어 풀: 15개 분야 × 변형 4 = 60명 (문장이 전부 달라 조사 캐시가 안 걸린다) ──
BASE_IDEAS = [
    ("반찬 꾸러미", "자취하는 20대는 배달 음식이 지겨운데 요리는 부담스럽다. 동네 반찬가게와 연결해 그날 남은 반찬을 저녁 7시 이후 할인 꾸러미로 예약·수령하는 앱을 만들고 싶다."),
    ("딸기 농장 체험", "겨울철 딸기 농장에 가족 단위 체험객이 오지만 예약·결제가 전화뿐이라 놓치는 손님이 많다. 인근 농가 6곳을 묶어 체험 시간대 예약과 수확 딸기 택배 주문을 한 번에 받는 서비스."),
    ("경단녀 돌봄", "경력이 끊긴 보육교사 출신 여성이 동네에서 시간제 돌봄을 맡고, 맞벌이 부모가 앱으로 당일 오후 돌봄을 예약하는 플랫폼. 자격증과 신원을 확인한 사람만 등록한다."),
    ("캠핑장 빈자리", "주말 캠핑장은 예약이 꽉 차는데 당일 취소분은 그냥 비어 있다. 취소분을 실시간으로 알려주고 당일 할인 예약까지 이어주는 앱."),
    ("헬스장 유휴", "직장인이 점심시간에 회사 근처 헬스장 빈 자리를 실시간으로 확인하고 30분 단위로 예약해 운동하는 서비스. 헬스장은 유휴 시간을 판다."),
    ("동네 세탁", "1인 가구는 이불·운동화처럼 큰 빨래를 맡길 곳이 마땅치 않다. 동네 세탁소가 저녁에 수거해 이틀 뒤 문 앞에 두는 정기 구독 세탁 서비스."),
    ("주차 공유", "상가·교회·회사의 비는 주차공간을 시간 단위로 빌려주는 지역 기반 플랫폼. 소유주는 유휴 시간을 팔고 운전자는 목적지 근처에서 바로 예약한다."),
    ("반려견 산책", "장시간 집을 비우는 직장인 반려인을 위해 검증된 동네 산책 도우미를 시간 단위로 매칭하는 앱. 산책 경로와 사진을 실시간으로 보낸다."),
    ("어르신 약 관리", "혼자 사는 어르신이 약 먹는 시간을 놓치는 일이 잦다. 약통에 붙이는 센서와 자녀 앱을 연결해 복용 여부를 알려주고, 놓치면 전화를 거는 서비스."),
    ("농산물 직거래", "소농은 판로가 없어 도매상에 헐값에 넘긴다. 같은 시군 소비자에게 주 1회 제철 채소 꾸러미를 정기 배송하는 지역 직거래 서비스."),
    ("소상공인 SNS", "동네 식당 사장님은 인스타그램에 올릴 사진과 글을 만들 시간이 없다. 메뉴 사진만 찍으면 게시글·해시태그를 자동으로 만들어 예약 발행하는 도구."),
    ("중고 교재", "대학생은 한 학기 쓴 전공 교재를 되팔 곳이 마땅치 않다. 같은 학교·같은 과목 기준으로 교재를 사고파는 캠퍼스 안 중고 교재 장터."),
    ("공유 주방", "배달 전문 초보 창업자는 주방 임대료가 부담이다. 심야에 비는 식당 주방을 시간 단위로 빌려주는 공유 주방 예약 서비스."),
    ("외국인 행정", "한국에 온 유학생·근로자는 출입국·은행·휴대폰 개통 서류가 어렵다. 모국어로 절차를 안내하고 필요 서류를 체크리스트로 만들어 주는 앱."),
    ("동네 공방 클래스", "퇴근 후 취미를 찾는 30대는 동네 공방 원데이 클래스 정보를 찾기 어렵다. 반경 2km 안 공방의 당일·주말 빈 자리를 모아 예약하는 서비스."),
]
VARIANTS = [
    ("", "팀원 없음", "대학에서 경영학 전공, 카페 아르바이트 2년", False, ""),
    (" 우선 천안 지역 대학가에서 시작해 반응을 보고 넓히려 한다.", "2명", "웹 개발 3년, 리액트·파이썬. 팀원은 디자이너", False, ""),
    (" 40대 이상 이용자도 쉽게 쓰도록 전화 접수를 병행할 생각이다.", "3명 이상", "같은 업종에서 5년 근무, 거래처 30곳 확보", True, "같은 분야 오프라인 매장 3년 운영, 월 매출 2천만 원"),
    (" 첫 달에는 지인 20명에게 무료로 써 보게 하고 불편을 모을 계획이다.", "팀원 없음", "컴퓨터공학 3학년, 앱 개발 동아리 활동", False, ""),
]


THINK = 45  # --think: 카드에 답하는 사람 시간(초). 이 동안 선조사가 돈다. 0 이면 1~3차와 같은 조건
SALT = ""   # --salt: 아이디어 끝에 붙여 조사 캐시(7일)를 피한다 — 같은 아이디어로 재측정할 때 이전 실행의 캐시가 결과를 왜곡하지 않게


def make_user(i: int) -> dict:
    name, idea = BASE_IDEAS[i % len(BASE_IDEAS)]
    extra, team, cap, biz, cur = VARIANTS[(i // len(BASE_IDEAS)) % len(VARIANTS)]
    return {"name": f"{name}#{i}", "track": "tech", "idea": idea + extra + SALT, "team": team, "capability": cap,
            "is_business": biz, "current_item": cur}


def pick_answers(cards: list[dict], mode: int) -> list[dict]:
    """카드마다 다르게: 첫 보기 / 둘째 보기+직접 문장 / 모름 / 직접 입력 (mentoring 은 보기 2개)."""
    out = []
    for i, card in enumerate(cards):
        opts = card.get("options") or []
        k = (i + mode) % 4
        if card.get("slot") == "mentoring" and len(opts) >= 2:
            ans = opts[0]["label"] + ", " + opts[1]["label"]
        elif k == 0 and opts:
            ans = opts[0]["label"]
        elif k == 1 and len(opts) > 1:
            ans = opts[1]["label"] + " — 제가 직접 겪어 본 것입니다"
        elif k == 2:
            out.append({"slot": card["slot"], "label": card["label"], "answer": "", "unknown": True})
            continue
        else:
            ans = "아직 정하지 못했지만 첫 달 안에 10명에게 물어볼 계획입니다"
        out.append({"slot": card["slot"], "label": card["label"], "answer": ans, "unknown": False})
    return out


def pct(xs, p):
    xs = sorted(x for x in xs if x is not None)
    return round(xs[min(len(xs) - 1, int(len(xs) * p))], 1) if xs else None


def payload(u: dict) -> dict:
    return {k: v for k, v in u.items() if k != "name"}


async def job(c: httpx.AsyncClient, kind: str, body: dict, hdr: dict) -> dict:
    """/jobs/{kind} 제출(429 면 프론트처럼 잠시 뒤 재시도) → 2.5초 폴링 → 최종 snapshot (+ _position: 제출 때 순번)."""
    for _ in range(120):
        r = await c.post(f"{B}/jobs/{kind}", json=body, headers=hdr, timeout=60)
        if r.status_code != 429:
            break
        await asyncio.sleep(3)
    r.raise_for_status()
    j = r.json()
    while True:
        await asyncio.sleep(2.5)
        s = (await c.get(f"{B}/jobs/{j['job_id']}", timeout=60)).json()
        if s["status"] in ("done", "error"):
            s["_position"] = j.get("position")
            return s


async def run_user(c: httpx.AsyncClient, i: int, log: list, t_start: float) -> dict:
    u = make_user(i)
    hdr = {"X-Forwarded-For": f"10.99.{i // 250}.{i % 250 + 1}"}
    rec = {"user": i, "name": u["name"], "t0": round(time.time() - t_start, 1)}
    # ① 인테이크 (/jobs/intake + 폴링 — 2026-09-03 프론트와 같음)
    t = time.time()
    try:
        s = await job(c, "intake", payload(u), hdr)
        it = s.get("result") or {}
        if s["status"] != "done":
            raise RuntimeError(s.get("error") or "job error")
        cards = it.get("cards") or []
        rec["intake"] = {"ok": True, "sec": round(time.time() - t, 1), "cards": len(cards), "queued": s.get("queued_seconds"), "run": s.get("elapsed_seconds"),
                         "research_facts": len((it.get("research") or {}).get("facts") or [])}
    except Exception as e:
        cards = []
        rec["intake"] = {"ok": False, "sec": round(time.time() - t, 1), "error": f"{type(e).__name__}: {str(e)[:80]}"}
    # ② 카드 답
    answers = pick_answers(cards, i % 4)
    form = {**payload(u), "answers": answers}
    # ③+④ 문항별 파이프라인 (2026-09-03 프론트 runPipeline 과 같음): 조사 3개 동시, 끝난 문항부터 생성 3개 동시.
    refs: dict[str, list] = {}
    rec["research"], rec["generate"] = {}, {}
    ready: list[str] = []
    research_done = False

    async def research(q):
        t = time.time()
        try:
            s = await job(c, "research", {**form, "question_id": q}, hdr)
            res = s.get("result") or {}
            if s["status"] != "done":
                raise RuntimeError(s.get("error") or "job error")
            facts = res.get("facts") or []
            refs[q] = facts
            rec["research"][q] = {"ok": True, "sec": round(time.time() - t, 1), "facts": len(facts), "cached": res.get("cached"),
                                  "queued": s.get("queued_seconds"), "run": s.get("elapsed_seconds")}
        except Exception as e:
            refs[q] = []
            rec["research"][q] = {"ok": False, "sec": round(time.time() - t, 1), "error": f"{type(e).__name__}: {str(e)[:80]}"}
        ready.append(q)

    async def generate(q):
        t = time.time()
        body = {**form, "question_id": q, "style": "logic", "references": refs.get(q) or []}
        try:
            s = await job(c, "generate", body, hdr)
            text = ((s.get("result") or {}).get("text") or "")
            rec["generate"][q] = {"ok": s["status"] == "done", "sec": round(time.time() - t, 1), "position": s.get("_position"),
                                  "queued": s.get("queued_seconds"), "run": s.get("elapsed_seconds"), "chars": len(text),
                                  "error": (s.get("error") or "")[:100] or None}
        except Exception as e:
            rec["generate"][q] = {"ok": False, "sec": round(time.time() - t, 1), "error": f"{type(e).__name__}: {str(e)[:80]}"}

    async def research_pool():
        nonlocal research_done
        sem = asyncio.Semaphore(PARALLEL)

        async def one(q):
            async with sem:
                await research(q)
        await asyncio.gather(*(one(q) for q in QS))
        research_done = True

    first_draft = {"t": None}

    async def writer():
        while True:
            if ready:
                await generate(ready.pop(0))
                if first_draft["t"] is None:
                    first_draft["t"] = round(time.time() - t_start - rec["t0"], 1)
            elif research_done:
                return
            else:
                await asyncio.sleep(0.25)

    async def writers_after_think():
        # 카드에 답하는 사람 시간(THINK 초) — 그동안 선조사(research_pool)가 뒤에서 돈다 (프론트 prefetchResearch 와 같음).
        await asyncio.sleep(THINK if cards else 0)
        rec["think"] = THINK if cards else 0
        await asyncio.gather(*(writer() for _ in range(PARALLEL)))
    await asyncio.gather(research_pool(), writers_after_think())
    rec["first_draft"] = first_draft["t"]          # 시작부터 첫 문항 초안이 도착하기까지 (인테이크·카드 시간 포함)
    rec["total"] = round(time.time() - t_start - rec["t0"], 1)
    log.append(rec)
    g = rec["generate"]
    print(f"  #{i:>2} {u['name']:<14} 완료 {rec['total']:>6.0f}s | 인테이크 {rec['intake'].get('sec')}s {'✓' if rec['intake']['ok'] else '✗'} 카드 {rec['intake'].get('cards', 0)}"
          f" | 조사 ✓{sum(1 for v in rec['research'].values() if v['ok'])}/8 facts {sum(v.get('facts', 0) for v in rec['research'].values())}"
          f" | 생성 ✓{sum(1 for v in g.values() if v['ok'])}/8 글자 {statistics.mean([v['chars'] for q, v in g.items() if v.get('chars') and q not in ('q1', 'q10')] or [0]):.0f}", flush=True)
    return rec


async def sampler(stop: asyncio.Event, samples: list):
    async with httpx.AsyncClient(timeout=5) as c:
        while not stop.is_set():
            s = {"t": round(time.time(), 1)}
            try:
                h = (await c.get(f"{B}/health")).json()["queue"]
                s.update({"api_running": h.get("running"), "api_queued": h.get("queued"), "api_error": h.get("error")})
            except Exception:
                pass
            try:
                m = (await c.get(VLLM_METRICS)).text
                for key, pat in (("vllm_running", r"num_requests_running\{[^}]*\} ([\d.]+)"), ("vllm_waiting", r"num_requests_waiting\{[^}]*\} ([\d.]+)"),
                                 ("kv_perc", r"kv_cache_usage_perc\{[^}]*\} ([\d.]+)"), ("preempt", r"num_preemptions_total\{[^}]*\} ([\d.]+)")):
                    mm = re.search(pat, m)
                    if mm:
                        s[key] = float(mm.group(1))
            except Exception:
                pass
            samples.append(s)
            await asyncio.sleep(5)


def summarize(users: int, recs: list, samples: list, wall: int) -> dict:
    it = [r["intake"] for r in recs]
    rs = [v for r in recs for v in r["research"].values()]
    gs = [v for r in recs for v in r["generate"].values()]
    gl = [v for r in recs for q, v in r["generate"].items() if q not in ("q1", "q10")]
    ok_g = [v for v in gs if v["ok"]]
    base_pre = next((s.get("preempt") for s in samples if s.get("preempt") is not None), 0) or 0
    mx = lambda k: max((s.get(k) or 0) for s in samples) if samples else 0
    out = {
        "users": users, "wall_s": wall,
        "intake": {"ok": sum(1 for x in it if x["ok"]), "fail": sum(1 for x in it if not x["ok"]), "p50": pct([x["sec"] for x in it], .5), "p95": pct([x["sec"] for x in it], .95),
                   "cards_avg": round(statistics.mean([x.get("cards", 0) for x in it]), 1), "errors": [x.get("error") for x in it if not x["ok"]][:3]},
        "research": {"ok": sum(1 for x in rs if x["ok"]), "fail": sum(1 for x in rs if not x["ok"]), "p50": pct([x["sec"] for x in rs], .5), "p95": pct([x["sec"] for x in rs], .95),
                     "facts_avg": round(statistics.mean([x.get("facts", 0) for x in rs if x["ok"]] or [0]), 1), "errors": [x.get("error") for x in rs if not x["ok"]][:3]},
        "generate": {"ok": len(ok_g), "fail": len(gs) - len(ok_g), "queued_p50": pct([x.get("queued") for x in ok_g], .5), "queued_p95": pct([x.get("queued") for x in ok_g], .95),
                     "run_p50": pct([x.get("run") for x in ok_g], .5), "run_p95": pct([x.get("run") for x in ok_g], .95),
                     "total_p50": pct([x["sec"] for x in ok_g], .5), "total_p95": pct([x["sec"] for x in ok_g], .95), "total_max": max([x["sec"] for x in ok_g] or [0]),
                     "long_chars_avg": round(statistics.mean([x["chars"] for x in gl if x.get("chars")] or [0])), "long_under_1000": sum(1 for x in gl if x.get("chars") and x["chars"] < 1000),
                     "errors": [x.get("error") for x in gs if not x["ok"]][:3]},
        "user_total": {"p50": pct([r["total"] for r in recs], .5), "p95": pct([r["total"] for r in recs], .95), "max": max(r["total"] for r in recs)},
        "first_draft": {"p50": pct([r.get("first_draft") for r in recs], .5), "p95": pct([r.get("first_draft") for r in recs], .95), "think_s": recs[0].get("think")},
        "server_peak": {"api_running": mx("api_running"), "api_queued": mx("api_queued"), "vllm_running": mx("vllm_running"), "vllm_waiting": mx("vllm_waiting"),
                        "kv_perc": round(mx("kv_perc") * 100), "preemptions": round(mx("preempt") - base_pre)},
    }
    return out


async def main(users: int, out: str | None):
    async with httpx.AsyncClient(timeout=60, limits=httpx.Limits(max_connections=400, max_keepalive_connections=100)) as c:
        h = (await c.get(f"{B}/health")).json()
        print(f"대상 {B} | 모델 {h['model']} | 워커 {h['queue']['max_workers']} | IP당 동시 {h['queue']['max_per_client']} | 사용자 {users}명 (아이디어 전부 다름)")
        recs, samples = [], []
        stop = asyncio.Event()
        smp = asyncio.create_task(sampler(stop, samples))
        t_start = time.time()
        await asyncio.gather(*(run_user(c, i, recs, t_start) for i in range(users)))
        stop.set()
        await smp
    wall = round(time.time() - t_start)
    s = summarize(users, recs, samples, wall)
    print("\n═══ 요약 " + json.dumps(s, ensure_ascii=False, indent=1))
    if out:
        json.dump({"summary": s, "users": recs, "samples": samples}, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print("저장:", out)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--users", type=int, default=40)
    ap.add_argument("--json")
    ap.add_argument("--salt", default="", help="아이디어 끝에 붙일 문구 (조사 캐시 회피용, 예: ' 2차 측정')")
    ap.add_argument("--think", type=int, default=45, help="카드에 답하는 사람 시간(초). 선조사 효과 측정용. 1~3차 조건은 0")
    a = ap.parse_args()
    SALT = a.salt
    THINK = a.think
    asyncio.run(main(a.users, a.json))
