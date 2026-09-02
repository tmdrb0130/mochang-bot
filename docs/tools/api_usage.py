"""오늘 소스별 외부 호출 수와 한도 대비 잔여량 — backend/.timing.jsonl 의 event=sources(작업 19) 집계. 모델·네트워크 호출 없음.
사용: python docs/tools/api_usage.py [--date 2026-09-02] [--since HH:MM]
한도 출처: RESEARCH_PLAN 'API 한도' 절. 네이버는 월 한도라 일 환산(÷30). 실제 잔여의 최종 확인은 각 콘솔(NCP·data.go.kr 마이페이지·KOSIS·ECOS)."""
import argparse, collections, datetime as dt, json
from pathlib import Path

LOG = Path(__file__).resolve().parents[2] / "backend" / ".timing.jsonl"
# (일 한도, 근거)  None = 미공개/미확인
LIMITS = {
    "naver":    (775_000 // 30, "월 775,000 ÷ 30 (NAVER API HUB). 실제 일 한도는 NCP 콘솔 설정값"),
    "kstartup": (1_000, "data.go.kr 개발계정 1,000/일 — 상권(sangkwon)과 같은 인증키·같은 한도를 공유"),
    "sangkwon": (1_000, "data.go.kr 개발계정 (kstartup 과 합산)"),
    "kosis":    (10_000, "KOSIS 자체 키, 일 1만~4만 — 보수적으로 1만 (개발자센터에서 실한도 확인)"),
    "ecos":     (10_000, "한국은행 ECOS 인증키 일 한도 — 미확인, 보수적 1만"),
    "kci":      (None, "KCI OpenAPI — 한도 미공개 (준수 사항만 있음)"),
    "ddgs":     (None, "비공식 — 한도 없음, 차단 위험. 목표: 1인 ≤5"),
    "fetch":    (None, "언론사 등 본문 fetch — 소스별 한도 없음, 도메인당 2건 상한"),
}
DATA_GO_KR = ("kstartup", "sangkwon")


def main(a):
    day = a.date or dt.date.today().isoformat()
    since = f"{day}T{a.since}" if a.since else f"{day}T00:00"
    tot = collections.Counter(); n = 0; kinds = collections.Counter()
    for line in open(LOG, encoding="utf-8"):
        try: r = json.loads(line)
        except Exception: continue
        if r.get("event") != "sources" or r.get("ts", "") < since or not r.get("ts", "").startswith(day): continue
        n += 1; kinds[r.get("kind")] += 1
        for k, v in r.items():
            if isinstance(v, (int, float)) and k not in ("ts",): tot[k] += int(v)
    print(f"기준: {day} {a.since or '00:00'} 이후 | 조사 {n}회 ({dict(kinds)})")
    print("소스 | 오늘 호출 | 일 한도 | 잔여(추정) | 사용률 | 근거")
    dg = sum(tot.get(k, 0) for k in DATA_GO_KR)
    for k, (lim, why) in LIMITS.items():
        used = tot.get(k, 0)
        if k in DATA_GO_KR: used_for_limit = dg; note = f"(data.go.kr 합산 {dg})"
        else: used_for_limit = used; note = ""
        if lim: print(f"{k} | {used} | {lim:,} | {lim - used_for_limit:,} | {100*used_for_limit/lim:.1f}% {note} | {why}")
        else: print(f"{k} | {used} | — | — | — | {why}")
    others = {k: v for k, v in tot.items() if k not in LIMITS}
    if others: print("기타 카운터:", others)
    if n: print(f"조사 1회당 평균: 네이버 {tot.get('naver',0)/n:.1f} · fetch {tot.get('fetch',0)/n:.1f} · ddgs {tot.get('ddgs',0)/n:.1f}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--date"); ap.add_argument("--since", help="HH:MM")
    main(ap.parse_args())
