"""실사용 흐름 부하 테스트 — N명이 동시에 인테이크→카드→문항별 조사→생성(8문항)을 브라우저처럼(문항 3개 동시) 수행.
아이디어는 전부 서로 다르고 캐시·벡터DB에 없을 것들. 결과: <out>/users.jsonl(사용자별 단계 시간), <out>/monitor.jsonl(5초 간격 큐·vLLM·GPU), <out>/summary.json.
사용: python docs/tools/loadtest_fullflow.py --users 55 --out loadtest_out [--base https://www.bustartup.kr:50001/api]
모델 호출: 사용자당 약 25회(라마, 한도 무관). 외부 검색: 사용자당 네이버 ~30·fetch ~20·정형 API 0~5. 공유 GPU 서버에 실부하 — 사용자 승인 후 실행."""
import argparse, asyncio, collections, json, random, re, subprocess, time
from pathlib import Path
import httpx

IDEAS = [  # (분야, 아이디어, 역량) — 18개 분야 골고루, 서로 다른 소재
 ("IT", "반려동물 사진 한 장으로 품종·나이·건강 이상 징후를 추정해 동네 동물병원 예약까지 잇는 앱", "컴퓨터공학 전공, 앱 개발 프로젝트 2회"),
 ("IT", "소상공인 가게의 전화 주문을 자동으로 받아 적어 POS 에 넣어 주는 음성 비서", "콜센터 근무 4년"),
 ("교육", "초등 저학년 대상 주 1회 방문 실험 키트 수업", "초등 방과후 강사 3년"),
 ("교육", "은퇴 교사가 온라인으로 중학생 수학 오답을 1:1 로 봐 주는 매칭 서비스", "중학교 수학 교사 25년 퇴직"),
 ("교육", "외국인 근로자를 위한 현장 한국어(안전 용어 중심) 야간 교실", "한국어 교원 2급"),
 ("금융", "프리랜서의 들쭉날쭉한 수입을 월 단위로 평탄화해 주는 자동 적립 앱", "은행 창구 근무 6년"),
 ("금융", "소상공인 매출 데이터로 대출 가능 금액을 미리 알려주는 서비스", "회계사무소 근무 5년"),
 ("운영관리", "소규모 제조 공장의 설비 점검 일지를 사진으로 찍어 자동 정리하는 도구", "생산관리 10년"),
 ("운영관리", "동네 미용실 예약·노쇼 관리와 재방문 문자 자동화", "미용실 운영 7년"),
 ("네트워킹", "같은 아파트 단지 안에서 공구·주방기기를 빌려 쓰는 이웃 대여 플랫폼", "아파트 입주자대표 3년"),
 ("네트워킹", "지방 소도시 청년 창업자들의 월 1회 오프라인 모임과 공동 구매 협의체", "청년회 총무 2년"),
 ("농축/수산업", "소규모 딸기 농가가 공동으로 쓰는 저온 저장·직거래 택배 포장 센터", "딸기 농사 8년, 농협 작목반 총무"),
 ("농축/수산업", "양식장 수온·용존산소를 저가 센서로 재서 문자로 알려주는 장치", "어업 종사 15년"),
 ("농축/수산업", "도시 옥상에서 상추·허브를 길러 인근 식당에 당일 납품하는 소형 농장", "도시농업 관리사 자격"),
 ("농축/수산업", "못생긴 사과·배를 모아 즙·잼으로 가공해 파는 농가 협동조합", "과수원 20년"),
 ("라이프스타일", "캠핑장 빈자리 실시간 알림과 당일 취소분 할인 예약 서비스", "여행사 근무 4년"),
 ("라이프스타일", "1인 가구 냉장고 식재료를 사진으로 등록해 유통기한 임박 레시피를 추천하는 앱", "요리 유튜브 운영 2년"),
 ("라이프스타일", "동네 세탁소들이 공동으로 쓰는 새벽 세탁물 수거·배달 기사 매칭 플랫폼", "세탁소 운영 12년"),
 ("라이프스타일", "반려식물 초보를 위한 월 1회 방문 관리와 분갈이 구독", "조경학 전공"),
 ("마케팅/PR", "동네 가게 사장님이 5분 만에 인스타 홍보 영상을 만드는 템플릿 앱", "광고대행사 AE 5년"),
 ("마케팅/PR", "지역 축제 부스 참여 소상공인의 방문객 설문·재방문 쿠폰 관리", "이벤트 기획 3년"),
 ("모빌리티", "농촌 어르신 병원 통원을 위한 예약제 합승 승합차", "마을 이장 6년"),
 ("모빌리티", "전동 킥보드 배터리를 편의점에서 교환하는 거점 서비스", "물류센터 관리 5년"),
 ("모빌리티", "캠핑카·트레일러 비수기 개인 간 대여 중개", "캠핑카 소유 5년"),
 ("미디어/엔터테인먼트", "지역 소극장 공연을 당일 반값으로 파는 잔여석 앱", "연극 기획 4년"),
 ("미디어/엔터테인먼트", "어르신 구술 인생사를 녹음해 자서전 책자로 만들어 주는 서비스", "출판 편집 8년"),
 ("미디어/엔터테인먼트", "아마추어 밴드 합주실 빈 시간 예약과 공연 매칭", "실용음악 전공"),
 ("바이오/의료", "독거 고령자용 복약 시간 알림·미복용 시 보호자 통보 스마트 약통", "간호사 5년"),
 ("바이오/의료", "당뇨 환자 식단 사진을 영양사가 다음 날 코멘트해 주는 구독", "임상영양사 자격"),
 ("바이오/의료", "치과 임플란트 후 관리 알림과 정기 점검 예약 앱", "치과위생사 6년"),
 ("바이오/의료", "물리치료사가 집으로 찾아가는 수술 후 재활 방문 서비스", "물리치료사 8년"),
 ("에너지/자원", "소규모 상가 건물의 전기요금 피크를 줄여주는 절전 컨설팅+장치", "전기기사 자격, 시설관리 10년"),
 ("에너지/자원", "농촌 축사 폐열로 인근 비닐하우스 난방을 잇는 열교환 장치", "기계공학 전공"),
 ("에너지/자원", "아파트 단지 커피 찌꺼기를 모아 퇴비로 만들어 텃밭에 공급", "환경단체 활동가 4년"),
 ("유통/물류", "전통시장 상인 공동 새벽 배송 라스트마일", "시장 상인회 총무 5년"),
 ("유통/물류", "소규모 수제 식품 업체를 위한 공동 냉장 창고와 픽업 거점", "식품 도매 7년"),
 ("유통/물류", "해외 직구 반품을 국내에서 모아 대신 처리해 주는 서비스", "무역 실무 6년"),
 ("임팩트", "경력 단절 여성이 동네 아이 돌봄을 시간 단위로 맡는 지역 협동조합", "사회복지사 자격, 지역아동센터 근무 3년"),
 ("임팩트", "발달장애 청년이 만드는 수제 비누 작업장과 기업 선물 납품", "특수교육 전공"),
 ("임팩트", "폐지 줍는 어르신에게 손수레 대신 전동 카트를 빌려주고 고물상 직거래를 잇는 사업", "사회적기업 근무 3년"),
 ("임팩트", "시각장애인을 위한 마을버스 도착 음성 안내 단말", "전자공학 전공"),
 ("재무", "자영업자 세금 신고 자료를 카톡 사진으로 모아 세무사에게 넘기는 서비스", "세무사무소 근무 4년"),
 ("재무", "동아리·소모임 회비를 투명하게 관리하는 공동 장부 앱", "대학 총학생회 재무 2년"),
 ("프롭테크", "원룸 임차인의 하자 사진을 모아 집주인과 수리를 조율하는 앱", "공인중개사 5년"),
 ("프롭테크", "빈 상가를 주 단위로 팝업 매장에 빌려주는 중개", "상가 임대 관리 6년"),
 ("프롭테크", "신축 아파트 하자 점검을 대신 해 주는 사전점검 대행", "건축시공 8년"),
 ("하드웨어", "공장 작업자 안전모에 붙이는 낙하물·접근 경고 센서", "전자과 졸업, 제조업 안전관리 4년"),
 ("하드웨어", "텃밭용 태양광 자동 급수기", "농업기계 정비 10년"),
 ("하드웨어", "1인 카페용 소형 로스팅 머신 임대와 원두 구독", "바리스타 6년"),
 ("하드웨어", "반려견 급식기와 연동해 사료 소진 시 자동 주문하는 장치", "IoT 스타트업 근무 2년"),
 ("기타", "군 전역자 대상 전공 복귀 학습 플랜과 스터디 매칭", "군 장교 5년 전역"),
 ("기타", "결혼식 남은 음식을 인근 복지시설에 당일 배송하는 연결 서비스", "웨딩플래너 4년"),
 ("기타", "지역 공방 체험 수업을 모아 파는 주말 클래스 플랫폼", "가죽공방 운영 3년"),
 ("기타", "시니어 대상 스마트폰 사용법 방문 교육과 사기 예방 안내", "노인복지관 사회복지사 5년"),
 ("기타", "동네 축구 동호회 경기 영상을 찍어 하이라이트를 만들어 주는 촬영 서비스", "영상 촬영 프리랜서 3년"),
 ("IT", "중고 전자기기 상태를 사진과 문답으로 판정해 예상 시세를 알려주는 앱", "전자제품 수리 5년"),
 ("교육", "취업 준비생 이력서·지원 직무 기반 맞춤 면접 질문 생성과 답변·말투·시선·속도 분석 코칭", "컴퓨터 공학과 대학생"),
]
QS = ["q1", "q2", "q3_1", "q3_2", "q4_1", "q4_2", "q8", "q10"]


async def job(c, base, kind, body, log, user, tag):
    t0 = time.time(); attempts = 0
    while True:
        r = await c.post(f"{base}/jobs/{kind}", json=body)
        if r.status_code == 429:
            attempts += 1; await asyncio.sleep(5); continue
        r.raise_for_status(); j = r.json(); break
    jid = j["job_id"]; pos0 = j.get("position")
    while True:
        s = (await c.get(f"{base}/jobs/{jid}")).json()
        if s["status"] in ("done", "error"): break
        await asyncio.sleep(2.5)
    rec = {"user": user, "tag": tag, "kind": kind, "position0": pos0, "retry429": attempts,
           "queued_s": s.get("queued_seconds"), "run_s": s.get("elapsed_seconds"), "wall_s": round(time.time() - t0, 1),
           "status": s["status"], "error": (s.get("error") or "")[:120], "ts": time.strftime("%H:%M:%S")}
    res = s.get("result") or {}
    if kind == "generate":
        rec["len"] = len(res.get("text") or ""); rec["refined"] = res.get("refined"); rec["auto_extended"] = res.get("auto_extended")
    if kind == "research":
        rec["facts"] = len(res.get("facts") or []); rec["backend"] = res.get("backend"); rec["cached"] = res.get("cached")
    log.write(json.dumps(rec, ensure_ascii=False) + "\n"); log.flush()
    return s


async def one_user(i, field, idea, cap, base, log, start_delay):
    await asyncio.sleep(start_delay)
    async with httpx.AsyncClient(verify=False, timeout=900) as c:
        base_body = {"track": "tech", "idea": idea, "is_business": False, "current_item": "", "team": "팀원 없음", "capability": cap}
        t0 = time.time()
        try:
            it = await job(c, base, "intake", base_body, log, i, "intake")
        except Exception as e:
            log.write(json.dumps({"user": i, "tag": "intake", "kind": "intake", "status": "exception", "error": str(e)[:120]}, ensure_ascii=False) + "\n"); it = {}
        cards = ((it.get("result") or {}).get("cards")) or []
        answers = []
        for card in cards:
            opts = card.get("options") or []
            ans = [o["label"] for o in opts[:2]] if card.get("slot") == "mentoring" else (random.choice(opts)["label"] if opts else None)
            answers.append({"slot": card["slot"], "label": card["label"], "answer": ans, "unknown": ans is None})
        sem = asyncio.Semaphore(3)  # 브라우저는 문항 3개까지 동시 제출

        async def q(qid):
            async with sem:
                try:
                    r = await job(c, base, "research", {**base_body, "question_id": qid, "answers": answers}, log, i, qid)
                    facts = (r.get("result") or {}).get("facts") or []
                    await job(c, base, "generate", {**base_body, "question_id": qid, "style": "logic", "answers": answers, "references": facts}, log, i, qid)
                except Exception as e:
                    log.write(json.dumps({"user": i, "tag": qid, "kind": "exception", "status": "exception", "error": str(e)[:120]}, ensure_ascii=False) + "\n")
        await asyncio.gather(*(q(qid) for qid in QS))
        log.write(json.dumps({"user": i, "tag": "TOTAL", "field": field, "wall_s": round(time.time() - t0, 1), "cards": len(cards)}, ensure_ascii=False) + "\n"); log.flush()


async def monitor(base, out, stop):
    with open(out / "monitor.jsonl", "a", encoding="utf-8") as m:
        async with httpx.AsyncClient(verify=False, timeout=10) as c:
            while not stop.is_set():
                rec = {"ts": time.strftime("%H:%M:%S")}
                try:
                    rec["queue"] = (await c.get(f"{base}/jobs")).json()
                except Exception as e:
                    rec["queue_err"] = str(e)[:80]
                try:
                    txt = (await c.get("http://localhost:30800/metrics")).text
                    for k in ("vllm:num_requests_running", "vllm:num_requests_waiting", "vllm:kv_cache_usage_perc", "vllm:gpu_cache_usage_perc"):
                        mm = re.search(r"^" + re.escape(k) + r"\S*\s+([\d.e+-]+)", txt, re.M)
                        if mm: rec[k.split(":")[1]] = float(mm.group(1))
                except Exception as e:
                    rec["vllm_err"] = str(e)[:60]
                try:
                    g = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "gpu",
                                        "nvidia-smi --query-gpu=utilization.gpu,memory.used --format=csv,noheader"],
                                       capture_output=True, text=True, timeout=15)
                    rec["gpu"] = [l.strip() for l in g.stdout.strip().splitlines()]
                except Exception as e:
                    rec["gpu_err"] = str(e)[:60]
                m.write(json.dumps(rec, ensure_ascii=False) + "\n"); m.flush()
                await asyncio.sleep(5)


def pct(v, p):
    v = sorted(v); return v[min(len(v) - 1, int(len(v) * p))] if v else None


async def main(a):
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    random.seed(7); ideas = IDEAS[:a.users]
    stop = asyncio.Event(); mon = asyncio.create_task(monitor(a.base, out, stop))
    t0 = time.time()
    with open(out / "users.jsonl", "a", encoding="utf-8") as log:
        await asyncio.gather(*(one_user(i, f, idea, cap, a.base, log, random.uniform(0, a.ramp)) for i, (f, idea, cap) in enumerate(ideas)))
    stop.set(); await mon
    rows = [json.loads(l) for l in open(out / "users.jsonl", encoding="utf-8")]
    summ = {"users": len(ideas), "wall_total_s": round(time.time() - t0)}
    for kind in ("intake", "research", "generate"):
        rs = [r for r in rows if r.get("kind") == kind]
        summ[kind] = {"n": len(rs), "error": sum(1 for r in rs if r["status"] != "done"),
                      "queued_p50": pct([r.get("queued_s") or 0 for r in rs], .5), "queued_p95": pct([r.get("queued_s") or 0 for r in rs], .95), "queued_max": max([r.get("queued_s") or 0 for r in rs] or [0]),
                      "run_p50": pct([r.get("run_s") or 0 for r in rs], .5), "run_p95": pct([r.get("run_s") or 0 for r in rs], .95),
                      "wall_p50": pct([r.get("wall_s") or 0 for r in rs], .5), "wall_p95": pct([r.get("wall_s") or 0 for r in rs], .95), "retry429": sum(r.get("retry429", 0) for r in rs)}
    tot = [r for r in rows if r.get("tag") == "TOTAL"]
    summ["user_total_s"] = {"p50": pct([r["wall_s"] for r in tot], .5), "p95": pct([r["wall_s"] for r in tot], .95), "max": max([r["wall_s"] for r in tot] or [0]), "finished": len(tot)}
    gens = [r for r in rows if r.get("kind") == "generate" and r["status"] == "done"]
    summ["generate_len"] = {"p50": pct([r["len"] for r in gens], .5),
                            "under_1100_long": sum(1 for r in gens if r["tag"] not in ("q1", "q10") and r["len"] < 1100),
                            "over_limit": sum(1 for r in gens if r["len"] > (100 if r["tag"] in ("q1", "q10") else 2000))}
    res = [r for r in rows if r.get("kind") == "research" and r["status"] == "done"]
    summ["research"]["facts0"] = sum(1 for r in res if r.get("facts") == 0)
    summ["research"]["backend"] = dict(collections.Counter(r.get("backend") for r in res))
    summ["exceptions"] = sum(1 for r in rows if r.get("status") == "exception")
    json.dump(summ, open(out / "summary.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(json.dumps(summ, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--users", type=int, default=55); ap.add_argument("--out", default="loadtest_out")
    ap.add_argument("--base", default="https://www.bustartup.kr:50001/api")
    ap.add_argument("--ramp", type=float, default=60, help="사용자 접속을 이 초 안에 분산")
    asyncio.run(main(ap.parse_args()))
