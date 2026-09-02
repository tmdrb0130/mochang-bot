"""실서비스에서 프론트 흐름 그대로 1인분 생성(인테이크→조사→생성 8문항) 후 문항 간 반복 측정."""
import httpx, time, json, re, sys, itertools
B="https://www.bustartup.kr:50001/api"; c=httpx.Client(verify=False,timeout=600)
IDEA=sys.argv[1]; OUT=sys.argv[2]
QS=["q1","q2","q3_1","q3_2","q4_1","q4_2","q8","q10"]
base={"track":"tech","idea":IDEA,"is_business":False,"current_item":"","team":"팀원 없음","capability":"대학에서 경영학 전공, 카페 아르바이트 2년"}
def job(kind, body):
    jid=c.post(f"{B}/jobs/{kind}",json=body).json()["job_id"]
    while True:
        j=c.get(f"{B}/jobs/{jid}").json()
        if j["status"] in("done","error"): return j
        time.sleep(3)
t0=time.time(); log=[]
it=c.post(f"{B}/intake",json=base).json()
answers=[]
for card in it.get("cards",[]):
    opts=card.get("options") or []
    if card.get("slot")=="mentoring": ans=[o["label"] for o in opts[:2]]
    else: ans=opts[0]["label"] if opts else None
    answers.append({"slot":card["slot"],"label":card["label"],"answer":ans,"unknown":ans is None})
log.append(("intake",round(time.time()-t0),len(it.get("cards",[]))))
texts={}; refs={}
for q in QS:
    t1=time.time()
    r=job("research",{**base,"question_id":q,"answers":answers})
    facts=(r.get("result") or {}).get("facts",[]) if r["status"]=="done" else []
    refs[q]=facts; tr=round(time.time()-t1)
    t2=time.time()
    g=job("generate",{**base,"question_id":q,"style":"logic","answers":answers,"references":facts})
    res=g.get("result") or {}
    texts[q]=res.get("text","") if g["status"]=="done" else f"[ERROR] {g.get('error')}"
    log.append((q,"research",tr,len(facts),"generate",round(time.time()-t2),len(texts[q]),res.get("auto_extended")))
json.dump({"idea":IDEA,"answers":answers,"texts":texts,"refs":refs,"log":log},open(OUT,"w",encoding="utf-8"),ensure_ascii=False,indent=1)
# ---- 반복 측정 ----
def sents(t): return [s.strip() for s in re.split(r"(?<=[.다!?])\s+|\n+", t) if len(s.strip())>=15]
def grams(s,n=4):
    s=re.sub(r"[\s\W]","",s); return {s[i:i+n] for i in range(max(0,len(s)-n+1))}
S={q:sents(t) for q,t in texts.items()}
dup=[]; per={q:0 for q in QS}
for qa,qb in itertools.combinations(QS,2):
    for a in S[qa]:
        ga=grams(a)
        for b in S[qb]:
            gb=grams(b); j=len(ga&gb)/max(1,len(ga|gb))
            if j>=0.5: dup.append((qa,qb,round(j,2),a[:70],b[:70])); per[qa]+=1; per[qb]+=1
nums=re.findall(r"\d[\d,.]*\s*(?:%|억|만|명|개|원|배)", " ".join(texts.values()))
from collections import Counter
print("=== 소요 ===");[print(l) for l in log]
print("=== 문항별 글자수 / 문장수 / 타 문항과 유사문장 수 ===")
for q in QS: print(q, len(texts[q]), len(S[q]), per[q])
print("=== 유사 문장 쌍 (Jaccard>=0.5) 총", len(dup), "===")
for d in sorted(dup,key=lambda x:-x[2])[:15]: print(d)
print("=== 3회 이상 반복된 수치 ===", [(k,v) for k,v in Counter(nums).items() if v>=3])
