"""관리자 대시보드 — 지금 누가 붙어 있고, 오늘 얼마나 처리했고, 누적 몇 명이 썼는지 (2026-09-07).

    .venv/Scripts/python scripts/admin_dashboard.py                 # http://127.0.0.1:8001  (이 PC 에서만)
    .venv/Scripts/python scripts/admin_dashboard.py --password 비밀  # 열 때 비밀번호를 묻는다 (외부 노출 전에 반드시)
    .venv/Scripts/python scripts/admin_dashboard.py --host 0.0.0.0 --port 50002 --password 비밀   # 외부에서 (방화벽 열어야 함)

**라이브 서비스(mochang-api, 8000)와 완전히 별개의 프로세스**다. 읽기만 한다:
  - nginx access.log 꼬리        → 지금 접속 중인 IP (프론트가 작업 중엔 /health·/jobs/{id} 를 계속 부르므로 좋은 신호)
  - backend/.timing.jsonl 꼬리   → 오늘의 요청·완료·429·오류·빈 번역·저장 거부, 최근 이벤트
  - backend/.data/mochang.sqlite → 누적 초안·이용자·생성문, 일별 추이, 외국인 비율 (mode=ro)
  - GET /health, GET /jobs       → 터널·저장소·두 큐 상태 (작업을 만들지 않으므로 상한 카운터에 안 잡힌다)
학생 아이디어 본문은 보여주지 않는다 — 인증 없는 서비스라 관리 화면이라도 원문은 안 띄운다. 집계와 id 앞 8자리까지.

죽어도 서비스에 영향이 없고, 시작·중지에 재시작이 필요 없다.
"""
from __future__ import annotations

import argparse
import collections
import json
import re
import secrets
import sqlite3
import sys
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "backend" / ".data" / "mochang.sqlite"
TIMING = ROOT / "backend" / ".timing.jsonl"
NGINX_LOG = Path(r"C:\Users\bon505\Desktop\nginx\logs\access.log")
API = "http://127.0.0.1:8000"
SELF_IPS = {"127.0.0.1", "::1", "61.34.63.189"}      # 서버 자신 — 접속자 수에서 뺀다

# 모창봇만의 API 경로 (bustartup.kr 의 /api/auth·/api/cardnews 와 구분)
_MOCHANG = re.compile(rb'"(GET|POST) /(api/(health|models|jobs|drafts|shared)|assets/index-)')
_NGX = re.compile(rb'^(\S+) \S+ \S+ \[(\d{2})/(\w{3})/(\d{4}):(\d{2}):(\d{2}):(\d{2})[^\]]*\] "(\w+) ([^ "]+)[^"]*" (\d{3})')
_MON = {b"Jan": 1, b"Feb": 2, b"Mar": 3, b"Apr": 4, b"May": 5, b"Jun": 6, b"Jul": 7, b"Aug": 8, b"Sep": 9, b"Oct": 10, b"Nov": 11, b"Dec": 12}
_HANGUL = re.compile(r"[가-힣]")
_LATIN = re.compile(r"[A-Za-z]")


def _tail(path: Path, nbytes: int) -> str:
    try:
        size = path.stat().st_size
        with path.open("rb") as f:
            if size > nbytes:
                f.seek(size - nbytes)
                f.readline()
            return f.read().decode("utf-8", errors="replace")
    except Exception:
        return ""


def _get(path: str) -> dict | None:
    try:
        with urllib.request.urlopen(API + path, timeout=5) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


# ── nginx: 접속 중인 사람 ──
def nginx_stats(now: datetime) -> dict:
    """최근 2·5·15분 안에 모창봇 요청을 낸 IP 와 오늘 방문·인테이크 IP."""
    raw = _tail(NGINX_LOG, 24 * 1024 * 1024).encode("utf-8", errors="replace")
    last_seen: dict[str, datetime] = {}
    kinds: dict[str, set] = collections.defaultdict(set)
    today_visit, today_intake = set(), set()
    today = now.date()
    for line in raw.splitlines():
        if not _MOCHANG.search(line):
            continue
        m = _NGX.match(line)
        if not m:
            continue
        ip, dd, mon, yyyy, hh, mi, ss, meth, path, st = m.groups()
        ipd = ip.decode()
        if ipd in SELF_IPS:
            continue
        try:
            ts = datetime(int(yyyy), _MON[mon], int(dd), int(hh), int(mi), int(ss))
        except Exception:
            continue
        if ts > (last_seen.get(ipd) or datetime.min):
            last_seen[ipd] = ts
        p = path.decode()
        if ts.date() == today:
            today_visit.add(ipd)
            if meth == b"POST" and p.startswith("/api/jobs/intake"):
                today_intake.add(ipd)
        if meth == b"POST" and p.startswith("/api/jobs/"):
            kinds[ipd].add(p.split("/api/jobs/")[1].split("?")[0])
    def within(minutes):
        cut = now - timedelta(minutes=minutes)
        return sorted([ip for ip, t in last_seen.items() if t >= cut], key=lambda ip: -last_seen[ip].timestamp())
    active2 = within(2)
    return {
        "active_2m": len(active2), "active_5m": len(within(5)), "active_15m": len(within(15)),
        "active_list": [{"ip": _mask(ip), "last": last_seen[ip].strftime("%H:%M:%S"),
                         "did": sorted(kinds.get(ip, []))} for ip in active2[:20]],
        "today_visitors": len(today_visit), "today_intake_ips": len(today_intake),
        "log_ok": bool(raw),
    }


def _mask(ip: str) -> str:
    """IP 는 뒤 한 자리를 가린다 — 화면에 그대로 띄울 이유가 없다."""
    parts = ip.split(".")
    return ".".join(parts[:3]) + ".x" if len(parts) == 4 else ip[:12] + "…"


# ── timing.jsonl: 오늘의 처리량 ──
def timing_stats(now: datetime) -> dict:
    raw = _tail(TIMING, 6 * 1024 * 1024)
    today = now.strftime("%Y-%m-%d")
    jobs = collections.Counter(); jobs_err = collections.Counter()
    http = collections.Counter(); limit = collections.Counter()
    empty_tr = 0; refused = 0; n_test = 0; run_s = collections.defaultdict(list)
    hourly = collections.defaultdict(lambda: {"intake": 0, "generate": 0, "translate": 0, "research": 0})
    recent = []
    owners_recent: dict[str, datetime] = {}
    for line in raw.splitlines():
        if not line.startswith("{"):
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        ts = d.get("ts") or ""
        if not ts.startswith(today):
            continue
        # 부하 테스트(X-Mochang-Test)가 낸 것은 실사용 지표에서 뺀다 (2026-09-08).
        # 이 표시가 없던 때, 밤사이 부하 테스트가 남긴 오류 51건·저장 거부 57건이 "오늘의 장애" 로 보였다.
        # 표시는 2026-09-08 이후 재시작한 서버부터 찍힌다 — 그 전 줄은 표시가 없어 그대로 실사용으로 센다.
        if d.get("test"):
            n_test += 1
            continue
        ev = d.get("event")
        if ev == "job":
            k = d.get("kind") or "?"
            if d.get("status") == "done":
                jobs[k] += 1
                run_s[k].append(float(d.get("run_s") or 0))
                if k in hourly[ts[11:13]]:
                    hourly[ts[11:13]][k] += 1
            else:
                jobs_err[k] += 1
            if k in ("intake", "generate", "translate", "intake_regenerate") or d.get("status") != "done":
                recent.append({"ts": ts[11:19], "what": f"{k} {d.get('status')}", "s": d.get("run_s"), "err": (d.get("error") or "")[:80]})
            ow = d.get("owner")
            if ow and str(ow).startswith("d:"):
                try:
                    owners_recent[ow] = max(owners_recent.get(ow, datetime.min), datetime.fromisoformat(ts))
                except Exception:
                    pass
        elif ev == "http":
            if d.get("method") == "POST":
                http[str(d.get("status"))] += 1
        elif ev == "limit":
            limit[f"{d.get('kind')}/{d.get('scope')}"] += 1
        elif ev == "translate_empty":
            empty_tr += int(d.get("empty") or 0)
            recent.append({"ts": ts[11:19], "what": "빈 번역", "s": None, "err": f"{d.get('lang')} {d.get('empty')}/{d.get('requested')}"})
        elif ev == "storage_refused":
            refused += 1
            recent.append({"ts": ts[11:19], "what": "저장 거부", "s": None, "err": f"{d.get('kind')} key={d.get('has_key')}"})
        elif ev == "storage_error":
            recent.append({"ts": ts[11:19], "what": "저장 오류", "s": None, "err": (d.get("error") or "")[:80]})
    def p50(v):
        v = sorted(v); return round(v[len(v) // 2], 1) if v else None
    # 테스트 모드(?test=1) 작업도 로그엔 같은 모양으로 남는다. 서비스 DB 에 있는 초안만 센다 — 테스트 초안은 백업 DB 에만 있다.
    # (2026-09-07 22:09 운영자 스모크가 "작업 중 1" 로 보인 뒤 추가)
    known: set = set()
    try:
        con = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
        ids = [ow[2:] for ow in owners_recent]
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            known.update(r[0] for r in con.execute(f"select draft_id from drafts where draft_id in ({','.join('?' * len(chunk))})", chunk))
        con.close()
    except Exception:
        known = {ow[2:] for ow in owners_recent}        # DB 를 못 읽으면 거르지 않는다
    def owners_within(minutes):
        cut = now - timedelta(minutes=minutes)
        return sum(1 for ow, t in owners_recent.items() if t >= cut and ow[2:] in known)
    return {
        # 작업 로그에 owner 가 찍히는 서버(2026-09-07 이후 재시작)에서만 값이 있다. 없으면 None → 화면은 DB 값을 쓴다.
        "owners_2m": owners_within(2) if owners_recent else None,
        "owners_5m": owners_within(5) if owners_recent else None,
        "owner_logging": bool(owners_recent),
        "jobs_done": dict(jobs), "jobs_error": dict(jobs_err),
        "post_status": dict(http), "limit_429": dict(limit), "limit_total": sum(limit.values()),
        "translate_empty": empty_tr, "storage_refused": refused, "test_skipped": n_test,
        "p50_run_s": {k: p50(v) for k, v in run_s.items()},
        "hourly": [{"h": h, **hourly[h]} for h in sorted(hourly)],
        "recent": list(reversed(recent))[:30],
    }


# ── sqlite: 누적 ──
def db_stats(now: datetime) -> dict:
    out = {"ok": False}
    try:
        con = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
    except Exception as e:
        out["error"] = str(e)[:100]
        return out
    try:
        today = now.strftime("%Y-%m-%d")
        out["ok"] = True
        out["drafts"] = con.execute("select count(*) from drafts").fetchone()[0]
        out["owners"] = con.execute("select count(distinct owner) from drafts").fetchone()[0]
        out["generations"] = con.execute("select count(*) from generations").fetchone()[0]
        out["research"] = con.execute("select count(*) from research").fetchone()[0]
        out["today_drafts"] = con.execute("select count(*) from drafts where created_at like ?", (today + "%",)).fetchone()[0]
        # 사람 수 (2026-09-07): 브라우저 익명 id. 공유 링크로 옮긴 기기는 같은 id 를 물려받으므로 폰→PC 도 한 사람.
        # client_id 가 NULL 인 초안(이 기능 배포 전)은 셀 수 없어 "미상" 으로 따로 센다.
        cols = {r[1] for r in con.execute("PRAGMA table_info(drafts)")}
        if "client_id" in cols:
            out["people"] = con.execute("select count(distinct client_id) from drafts where client_id is not null").fetchone()[0]
            out["people_unknown"] = con.execute("select count(*) from drafts where client_id is null").fetchone()[0]
            out["today_people"] = con.execute("select count(distinct client_id) from drafts where client_id is not null and created_at like ?", (today + "%",)).fetchone()[0]
            cut2 = (now - timedelta(minutes=2)).strftime("%Y-%m-%d %H:%M:%S")
            out["active_people_2m"] = con.execute("select count(distinct client_id) from drafts where client_id is not null and updated_at >= ?", (cut2,)).fetchone()[0]
        else:
            out["people"] = None
        out["today_generations"] = con.execute("select count(*) from generations where created_at like ?", (today + "%",)).fetchone()[0]
        last = con.execute("select max(updated_at) from drafts").fetchone()[0]
        out["last_activity"] = (last or "")[:19]
        # 지금 작업 중인 초안 — upsert 가 저장할 때마다 updated_at 을 올리므로(인테이크·조사·생성) 브라우저 단위로 센다.
        # 교내처럼 공인 IP 하나를 여럿이 쓰는 곳에서도 사람 수에 가깝다. 번역만 하는 학생은 저장이 없어 안 잡힌다.
        for label, minutes in (("active_2m", 2), ("active_5m", 5), ("active_15m", 15)):
            cut = (now - timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")
            out[label] = con.execute("select count(*) from drafts where updated_at >= ?", (cut,)).fetchone()[0]
        # 일별 7일
        days = []
        for i in range(6, -1, -1):
            d = (now - timedelta(days=i)).strftime("%Y-%m-%d")
            n, o = con.execute("select count(*), count(distinct owner) from drafts where created_at like ?", (d + "%",)).fetchone()
            g = con.execute("select count(*) from generations where created_at like ?", (d + "%",)).fetchone()[0]
            days.append({"day": d[5:], "drafts": n, "owners": o, "generations": g})
        out["days"] = days
        # 외국인 비율(아이디어 문자 기준) — 본문은 밖으로 내지 않고 판정만
        ko = en = 0
        for (idea,) in con.execute("select idea from drafts"):
            s = idea or ""
            if len(_LATIN.findall(s)) > len(_HANGUL.findall(s)):
                en += 1
            else:
                ko += 1
        out["lang"] = {"ko": ko, "foreign": en}
        # 최근 초안 (본문 없이)
        rows = con.execute("""select d.draft_id, d.created_at, d.updated_at, d.request_count, d.idea,
            (select count(*) from generations g where g.draft_id=d.draft_id),
            (select count(*) from research r where r.draft_id=d.draft_id)
            from drafts d order by d.updated_at desc limit 12""").fetchall()
        out["recent_drafts"] = [{
            "id": r[0][:8], "created": (r[1] or "")[5:16], "updated": (r[2] or "")[11:16], "req": r[3],
            "lang": "외국어" if len(_LATIN.findall(r[4] or "")) > len(_HANGUL.findall(r[4] or "")) else "한국어",
            "gen": r[5], "res": r[6], "done": (r[5] or 0) >= 8,
        } for r in rows]
    except Exception as e:
        out["error"] = str(e)[:100]
    finally:
        con.close()
    return out


def collect() -> dict:
    now = datetime.now()
    health = _get("/health")
    jobs = _get("/jobs")
    live = {"api_up": bool(health), "llm": None, "storage": None, "queue": None, "model": None, "usage_today": None}
    if health:
        live.update(llm=health.get("llm_reachable"), model=health.get("model"),
                    storage={"enabled": (health.get("storage") or {}).get("enabled"),
                             "archive": (health.get("storage") or {}).get("archive"),
                             "errors": (health.get("storage") or {}).get("error_count")},
                    usage_today=(health.get("usage") or {}).get("used"),
                    finisher=health.get("finisher"))
    if jobs:
        r = jobs.get("research") or {}
        live["queue"] = {"gen_running": jobs.get("running"), "gen_queued": jobs.get("queued"),
                         "res_running": r.get("running"), "res_queued": r.get("queued"),
                         "gen_done": jobs.get("done"), "res_done": r.get("done"),
                         "gen_peak": jobs.get("peak_running"), "res_peak": r.get("peak_running")}
    return {"now": now.strftime("%Y-%m-%d %H:%M:%S"), "live": live,
            "nginx": nginx_stats(now), "timing": timing_stats(now), "db": db_stats(now)}


# ── 웹 ──
app = FastAPI(title="모창봇 관리자", docs_url=None, redoc_url=None)
PASSWORD: str | None = None


def _auth(request: Request):
    if not PASSWORD:
        return
    auth = request.headers.get("authorization") or ""
    ok = False
    if auth.lower().startswith("basic "):
        import base64
        try:
            _, pw = base64.b64decode(auth[6:]).decode("utf-8").split(":", 1)
            ok = secrets.compare_digest(pw, PASSWORD)
        except Exception:
            ok = False
    if not ok:
        raise HTTPException(status_code=401, headers={"WWW-Authenticate": 'Basic realm="mochang-admin"'})


@app.get("/api/stats")
def stats(_=Depends(_auth)):
    return JSONResponse(collect())


PAGE = """<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>모창봇 관리자</title>
<script src="https://cdn.tailwindcss.com"></script>
<style>body{font-family:"Apple SD Gothic Neo","Noto Sans KR","Malgun Gothic",sans-serif}</style>
</head><body class="bg-slate-50 text-slate-900">
<div class="max-w-6xl mx-auto p-5 space-y-5">
  <div class="flex items-end justify-between flex-wrap gap-2">
    <div><h1 class="text-2xl font-bold">모창봇 관리자</h1><p class="text-sm text-slate-500">10초마다 새로 읽습니다 · 서비스에는 읽기만 합니다</p></div>
    <div class="text-sm text-slate-500" id="now">—</div>
  </div>

  <div class="grid grid-cols-2 md:grid-cols-4 gap-3" id="live"></div>

  <div class="grid md:grid-cols-3 gap-3">
    <div class="bg-white rounded-xl border border-slate-200 p-4">
      <div class="text-xs text-slate-500 mb-1">지금 작업 중인 초안 (최근 2분에 저장이 있었던 초안 수)</div>
      <div class="text-4xl font-bold" id="active2">—</div>
      <div class="text-xs text-slate-500 mt-1"><span id="active5"></span> · <span id="active15"></span></div>
      <div class="text-xs text-slate-400 mt-2" id="netnote"></div>
      <ul class="mt-3 text-xs space-y-1" id="activelist"></ul>
    </div>
    <div class="bg-white rounded-xl border border-slate-200 p-4">
      <div class="text-xs text-slate-500 mb-1">오늘</div>
      <div class="grid grid-cols-2 gap-x-3 gap-y-1 text-sm" id="today"></div>
    </div>
    <div class="bg-white rounded-xl border border-slate-200 p-4">
      <div class="text-xs text-slate-500 mb-1">누적 (서비스 DB)</div>
      <div class="grid grid-cols-2 gap-x-3 gap-y-1 text-sm" id="total"></div>
    </div>
  </div>

  <div class="grid md:grid-cols-2 gap-3">
    <div class="bg-white rounded-xl border border-slate-200 p-4">
      <div class="text-xs text-slate-500 mb-2">최근 7일 — 초안 / 네트워크(IP) / 생성문</div>
      <div id="days" class="space-y-1 text-xs"></div>
    </div>
    <div class="bg-white rounded-xl border border-slate-200 p-4">
      <div class="text-xs text-slate-500 mb-2">오늘 시간대별 완료 — 인테이크 · 생성 · 번역</div>
      <div id="hourly" class="space-y-1 text-xs"></div>
    </div>
  </div>

  <div class="grid md:grid-cols-2 gap-3">
    <div class="bg-white rounded-xl border border-slate-200 p-4">
      <div class="text-xs text-slate-500 mb-2">최근 초안 (본문은 보여주지 않습니다)</div>
      <table class="w-full text-xs"><thead class="text-slate-400"><tr><th class="text-left">id</th><th class="text-left">시작</th><th>최근</th><th>언어</th><th>생성</th><th>조사</th></tr></thead><tbody id="drafts"></tbody></table>
    </div>
    <div class="bg-white rounded-xl border border-slate-200 p-4">
      <div class="text-xs text-slate-500 mb-2">최근 이벤트</div>
      <ul class="text-xs space-y-0.5 font-mono" id="recent"></ul>
    </div>
  </div>
</div>
<script>
const $ = (id) => document.getElementById(id);
const card = (label, value, tone) => `<div class="bg-white rounded-xl border p-3 ${tone||'border-slate-200'}"><div class="text-xs text-slate-500">${label}</div><div class="text-xl font-semibold mt-0.5">${value}</div></div>`;
const bar = (label, vals, colors, max) => {
  const w = (v) => Math.max(2, Math.round(v / (max || 1) * 100));
  return `<div class="flex items-center gap-2"><span class="w-12 text-slate-500 shrink-0">${label}</span>
    <div class="flex-1 flex gap-1 items-center">${vals.map((v,i)=>`<div class="${colors[i]} h-3 rounded" style="width:${w(v)}%" title="${v}"></div>`).join("")}</div>
    <span class="w-20 text-right text-slate-600 shrink-0">${vals.join(" / ")}</span></div>`;
};
async function load() {
  let s;
  try { s = await (await fetch("/api/stats", { cache: "no-store" })).json(); }
  catch (e) { $("now").textContent = "대시보드 오류: " + e; return; }
  $("now").textContent = s.now;
  const L = s.live, Q = L.queue || {}, N = s.nginx, T = s.timing, D = s.db;
  const bad = "border-red-300 bg-red-50", warn = "border-amber-300 bg-amber-50", ok = "border-emerald-200";
  $("live").innerHTML =
    card("API", L.api_up ? "정상" : "무응답", L.api_up ? ok : bad) +
    card("모델 서버(터널)", L.llm === true ? "연결됨" : L.llm === false ? "끊김 — 생성 전부 실패 중" : "?", L.llm === true ? ok : bad) +
    card("저장소", L.storage ? (L.storage.enabled ? `정상 · 오류 ${L.storage.errors}` : "꺼짐") : "?", L.storage && L.storage.enabled && !L.storage.errors ? ok : bad) +
    card("진행 중 작업", Q.gen_running == null ? "?" : `생성 ${Q.gen_running}+${Q.gen_queued}대기 · 조사/번역 ${Q.res_running}+${Q.res_queued}대기`, (Q.gen_queued||0)+(Q.res_queued||0) > 10 ? warn : ok);
  // 사람 수: 초안 단위(DB updated_at). 교내는 공인 IP 하나를 여럿이 써서 IP 로 세면 강의실이 1명이 된다.
  // 작업 로그에 owner 가 찍히는 서버면 그 값(번역까지 포함)을 우선 쓴다.
  const a2 = T.owner_logging ? T.owners_2m : D.active_2m, a5 = T.owner_logging ? T.owners_5m : D.active_5m;
  $("active2").textContent = a2 ?? "—";
  $("active5").textContent = `5분 ${a5 ?? "—"}개`; $("active15").textContent = `15분 ${D.active_15m ?? "—"}개` + (D.active_people_2m != null ? ` · 사람 ${D.active_people_2m}명(2분)` : "");
  $("netnote").textContent = `네트워크(IP) 기준 2분 ${N.active_2m} · 5분 ${N.active_5m} · 15분 ${N.active_15m} — 교내는 여러 명이 IP 하나로 보입니다. 초안 수도 사람 수는 아닙니다(한 사람이 아이디어를 바꿔 여러 초안을 만들 수 있음)`;
  $("activelist").innerHTML = (N.active_list || []).map(a => `<li class="flex justify-between"><span class="font-mono">${a.ip}</span><span class="text-slate-500">${a.did.join(",") || "보는 중"}</span><span class="text-slate-400">${a.last}</span></li>`).join("") || `<li class="text-slate-400">${N.log_ok ? "없음" : "nginx 로그를 못 읽음"}</li>`;
  const jd = T.jobs_done || {}, je = T.jobs_error || {};
  const errs = Object.values(je).reduce((a,b)=>a+b,0);
  $("today").innerHTML = [
    ["<b>초안 수</b>", `<b>${D.today_drafts}</b>`], ["사람(브라우저 id)", D.people == null ? "배포 전" : D.today_people], ["생성문(DB)", D.today_generations],
    ["방문 네트워크(IP)", N.today_visitors], ["인테이크 낸 네트워크", N.today_intake_ips],
    ["인테이크 완료", jd.intake||0], ["생성 완료", jd.generate||0], ["번역 완료", jd.translate||0], ["조사 완료", jd.research||0],
    ["모델 호출", L.usage_today ?? "?"], ["생성 p50", T.p50_run_s?.generate != null ? T.p50_run_s.generate + "초" : "—"],
    ["429", `<b class="${T.limit_total ? "text-amber-700" : ""}">${T.limit_total}</b>`], ["작업 오류", `<b class="${errs ? "text-red-600" : ""}">${errs}</b>`],
    ["빈 번역", `<b class="${T.translate_empty ? "text-amber-700" : ""}">${T.translate_empty}</b>`], ["저장 거부", `<b class="${T.storage_refused ? "text-red-600" : ""}">${T.storage_refused}</b>`],
    ["부하 테스트(제외됨)", `<span class="text-slate-400">${T.test_skipped || 0}</span>`],
  ].map(([k,v]) => `<div class="text-slate-500">${k}</div><div class="text-right font-medium">${v}</div>`).join("");
  $("total").innerHTML = D.ok ? [
    ["<b>초안 수</b>", `<b>${D.drafts}</b>`], ["사람(브라우저 id) · 미상", D.people == null ? "배포 전" : `${D.people} · ${D.people_unknown}`], ["네트워크(IP) — 교내는 여럿이 하나", D.owners], ["생성문", D.generations], ["조사 자료", D.research],
    ["한국어 / 외국어", `${D.lang.ko} / ${D.lang.foreign}`], ["마지막 활동", D.last_activity],
  ].map(([k,v]) => `<div class="text-slate-500">${k}</div><div class="text-right font-medium">${v}</div>`).join("") : `<div class="col-span-2 text-red-600">DB 읽기 실패: ${D.error||""}</div>`;
  const dmax = Math.max(1, ...(D.days||[]).map(d => Math.max(d.drafts, d.owners, d.generations/8)));
  $("days").innerHTML = (D.days||[]).map(d => bar(d.day, [d.drafts, d.owners, Math.round(d.generations/8)], ["bg-indigo-500","bg-sky-400","bg-emerald-400"], dmax)).join("") + `<div class="text-slate-400 mt-1">파랑 초안 · 하늘 네트워크(IP) · 초록 생성문÷8(≈완주 초안)</div>`;
  const hmax = Math.max(1, ...(T.hourly||[]).map(h => Math.max(h.intake, h.generate, h.translate)));
  $("hourly").innerHTML = (T.hourly||[]).map(h => bar(h.h + "시", [h.intake, h.generate, h.translate], ["bg-indigo-500","bg-emerald-400","bg-amber-400"], hmax)).join("") || `<div class="text-slate-400">오늘 완료된 작업 없음</div>`;
  $("drafts").innerHTML = (D.recent_drafts||[]).map(r => `<tr class="border-t border-slate-100"><td class="font-mono">${r.id}</td><td>${r.created}</td><td class="text-center">${r.updated}</td><td class="text-center">${r.lang}</td><td class="text-center ${r.done ? "text-emerald-700" : "text-amber-700"}">${r.gen}</td><td class="text-center">${r.res}</td></tr>`).join("");
  $("recent").innerHTML = (T.recent||[]).map(e => `<li class="${/거부|오류|error/.test(e.what) ? "text-red-600" : /빈 번역/.test(e.what) ? "text-amber-700" : "text-slate-600"}">${e.ts} ${e.what}${e.s != null ? " " + e.s + "s" : ""} ${e.err||""}</li>`).join("") || `<li class="text-slate-400">없음</li>`;
}
load(); setInterval(load, 10000);
</script></body></html>"""


@app.get("/", response_class=HTMLResponse)
def page(_=Depends(_auth)):
    return PAGE


def main() -> int:
    global PASSWORD
    ap = argparse.ArgumentParser(description="모창봇 관리자 대시보드 (읽기 전용, 별도 프로세스)")
    ap.add_argument("--host", default="127.0.0.1", help="기본 127.0.0.1 = 이 PC 에서만. 외부 노출은 --password 와 함께")
    ap.add_argument("--port", type=int, default=8001)
    ap.add_argument("--password", default=None, help="지정하면 열 때 비밀번호를 묻는다 (아이디는 아무거나)")
    ap.add_argument("--nginx-log", default=str(NGINX_LOG))
    args = ap.parse_args()
    if args.host != "127.0.0.1" and not args.password:
        print("외부에 열려면 --password 가 필요합니다 (인증 없는 관리 화면은 열지 않는다).", file=sys.stderr)
        return 2
    PASSWORD = args.password
    globals()["NGINX_LOG"] = Path(args.nginx_log)
    import uvicorn
    print(f"관리자 대시보드: http://{args.host}:{args.port}/   (Ctrl+C 로 중지 — 서비스에는 영향 없음)")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
