"""ACCEPTANCE_CRITERIA 자동 항목(A·B·C1·C7·D3 일부) 점검. 입력: fullrun_repeat_check.py 가 만든 run JSON.
사용: python docs/tools/acceptance_check.py runA.json [runC.json ...]  — 모델·네트워크 호출 없음."""
import json, re, sys, itertools
from collections import Counter

RANGES = {"q1": (70, 95), "q10": (60, 95), "q2": (1100, 1900), "q3_1": (1100, 1900), "q4_1": (1100, 1900),
          "q3_2": (1100, 1800), "q4_2": (1100, 1700), "q8": (1100, 1700)}
LIMITS = {"q1": 100, "q10": 100}
NO_REF_Q = {"q1", "q10", "q4_1", "q4_2", "q8"}
FOREIGN = re.compile(r"[一-鿿぀-ヿ฀-๿ÀÁÂÃÈÉÊÌÍÒÓÔÕÙÚĂĐĨŨƠƯăđĩũơưạ-ỹ]")
ABSTRACT = ["혁신적", "고유한 가치", "자신감을 얻", "큰 도움이", "효율화해", "스마트한"]
BANNED = ["데이터 판매", "데이터를 판매"]
NEG_SELF = ["결과물이 없습니다", "역량이 없습니다", "개발 방법을 모", "팀을 구성할 필요가 없"]
XREF = ["앞서 말씀", "다음 문항", "위에서 설명", "앞 문항"]

def sents(t): return [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", t) if len(s.strip()) >= 15]
def grams(s, n=4):
    s = re.sub(r"[\s\W]", "", s); return {s[i:i+n] for i in range(max(0, len(s)-n+1))}
def jac(a, b): ga, gb = grams(a), grams(b); return len(ga & gb) / max(1, len(ga | gb))

def check(path):
    d = json.load(open(path, encoding="utf-8")); T = d["texts"]; refs = d.get("refs", {}); ans = d.get("answers", [])
    allrefs = " ".join(json.dumps(v, ensure_ascii=False) for v in refs.values())
    known = " ".join(str(a.get("answer")) for a in ans if a.get("answer")) + " " + d.get("idea", "")
    team_none = True
    rows = []; fails = []
    S = {q: sents(t) for q, t in T.items()}
    for q, t in T.items():
        lo, hi = RANGES.get(q, (1100, 1900)); lim = LIMITS.get(q, 2000)
        r = {"q": q, "len": len(t)}
        r["A1_over"] = len(t) > lim
        r["A2_cut"] = not re.search(r"[.!?다요]\s*$", t.strip()) or t.strip().endswith("…")
        r["A3_range"] = lo <= len(t) <= hi
        r["A4_foreign"] = len(FOREIGN.findall(t))
        r["A5_plain"] = len(re.findall(r"(?<![가-힣])(한다|이다|된다|있다)\.", t))
        r["A6_xref"] = sum(t.count(x) for x in XREF)
        r["A7_we"] = t.count("저희") if team_none else 0
        r["B2_inner"] = sum(1 for a, b in itertools.combinations(S[q], 2) if jac(a, b) >= 0.5)
        nums = re.findall(r"\d[\d,.]*\s*(?:%|억|만\s*원|천만|만\s*명|명)", t)
        r["C1_invented"] = sum(1 for n in nums if n.replace(" ", "") not in known.replace(" ", "") and n.replace(" ", "") not in allrefs.replace(" ", ""))
        r["C7_negself"] = sum(t.count(x) for x in NEG_SELF)
        r["F6_abstract"] = sum(t.count(x) for x in ABSTRACT)
        r["F3_banned"] = sum(t.count(x) for x in BANNED)
        cites = len(re.findall(r"(?:에 따르면|보도에 따르면|조사에 따르면)", t))
        r["D3_cite"] = cites
        r["D3_bad"] = cites if q in NO_REF_Q else 0
        rows.append(r)
    pairs = [(a, b) for qa, qb in itertools.combinations(T.keys(), 2) for a in S[qa] for b in S[qb] if jac(a, b) >= 0.5]
    stats = Counter(re.findall(r"\d[\d,.]*\s*(?:%|억|만\s*원)", " ".join(T.values())))
    restate = sum(1 for q in T if q not in ("q1", "q3_1") and S[q] and jac(S[q][0], d.get("idea", "")) >= 0.5)
    print(f"\n=== {path} | idea: {d.get('idea','')[:50]} ===")
    hdr = ["q", "len", "A1_over", "A2_cut", "A3_range", "A4_foreign", "A5_plain", "A6_xref", "A7_we", "B2_inner", "C1_invented", "C7_negself", "F6_abstract", "F3_banned", "D3_cite", "D3_bad"]
    print(" | ".join(hdr))
    for r in rows: print(" | ".join(str(r[h]) for h in hdr))
    print("B1 문항 간 유사 쌍:", len(pairs), "| B3 통계 3회+:", [(k, v) for k, v in stats.items() if v >= 3], "| B4 재진술 첫 문장:", restate)
    tot = {"A1": sum(r["A1_over"] for r in rows), "A2": sum(r["A2_cut"] for r in rows), "A3_out": sum(not r["A3_range"] for r in rows),
           "A4": sum(r["A4_foreign"] for r in rows), "A7": sum(r["A7_we"] for r in rows), "B1": len(pairs), "B2": sum(r["B2_inner"] for r in rows),
           "C1": sum(r["C1_invented"] for r in rows), "C7": sum(r["C7_negself"] for r in rows), "D3_bad": sum(r["D3_bad"] for r in rows), "F3": sum(r["F3_banned"] for r in rows), "F6": sum(r["F6_abstract"] for r in rows)}
    print("합계:", tot)
    return tot

if __name__ == "__main__":
    for p in sys.argv[1:]: check(p)
