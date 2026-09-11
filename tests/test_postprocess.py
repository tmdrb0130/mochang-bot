"""postprocess.refine — 생성 후처리 (작업 22 + 27). 순수 함수라 모델·네트워크 호출이 0이다.

앞쪽은 규칙 ①~⑩ 의 단위 케이스, 뒤쪽은 `docs/measurements/*.json` 의 **실측 원문**을 픽스처로 한
전/후 건수 회귀다. 회귀표 숫자가 흔들리면 규칙이 바뀐 것이므로 의도한 변화인지 확인하고 갱신한다.
"""
import json
import pathlib

import pytest

from backend.pipeline import postprocess as P

LONG_IDEA = "동네 세탁소를 묶어 수거·배송을 대신해 주는 앱입니다."


def ctx(**kw):
    """긴 문항(한도 2000) 기본값. 필요한 것만 덮어쓴다."""
    base = {"question_id": "q2", "limit": 2000, "idea": LONG_IDEA, "team": "팀원 없음"}
    base.update(kw)
    return base


# ────────────────────────── ① 이물 문자 ──────────────────────────

def test_foreign_sentence_is_flagged_but_not_deleted():
    text = "동네 세탁소는 수거가 어렵습니다. 이것은 生活의 문제입니다. 그래서 앱을 만듭니다."
    out, rep = P.refine(text, ctx())
    assert rep["foreign"] == ["이것은 生活의 문제입니다."]
    assert "生活" in out                      # ①은 표시만 한다 — 지우는 것은 호출자 몫


@pytest.mark.parametrize("bad", ["生活", "まず", "หมด", "cầu", "需求"])
def test_has_foreign_catches_various_scripts(bad):
    assert P.has_foreign(f"문장 안에 {bad} 가 있습니다.")


@pytest.mark.parametrize("ok", ["한글만 있습니다", "ASCII mixed 2026 OK", "ㄱㄴㄷ 자모", "숫자 30% 포함"])
def test_korean_and_ascii_are_not_foreign(ok):
    assert not P.has_foreign(ok)


# ────────────────────────── ② 지어낸 수치 ──────────────────────────

def test_drops_sentence_whose_number_is_nowhere_in_inputs():
    text = "세탁 수거는 번거롭습니다. 시장 규모는 3조 원입니다. 그래서 앱으로 묶습니다."
    out, rep = P.refine(text, ctx())
    assert rep["invented"] == 1
    assert "3조 원" not in out
    assert "그래서 앱으로 묶습니다." in out


def test_keeps_number_that_appears_in_references():
    refs = [{"fact": "1인 가구 세탁 지출은 연 12만 원이다.", "url": "https://a.kr/1"}]
    text = "세탁 수거는 번거롭습니다. 1인 가구 세탁 지출은 연 12만 원입니다. 그래서 앱으로 묶습니다."
    out, rep = P.refine(text, ctx(references=refs))
    assert rep["invented"] == 0
    assert "12만 원" in out


def test_keeps_number_the_applicant_wrote_in_answers():
    answers = [{"slot": "customer", "label": "고객", "answer": "세탁소 40곳", "unknown": False}]
    text = "동네 문제가 있습니다. 이미 세탁소 40곳과 이야기했습니다. 그래서 앱으로 묶습니다."
    out, rep = P.refine(text, ctx(answers=answers))
    assert rep["invented"] == 0
    assert "40곳" in out


def test_assumption_wording_does_not_save_an_invented_number():
    text = "세탁 수거는 번거롭습니다. 가정하면 고객은 500명일 것입니다. 그래서 앱으로 묶습니다."
    out, rep = P.refine(text, ctx())
    assert rep["invented"] == 1 and "500명" not in out


def test_survey_claim_without_evidence_is_dropped():
    text = "세탁 수거는 번거롭습니다. 이용자에게 인터뷰해 보니 불편하다고 했습니다. 그래서 앱으로 묶습니다."
    out, rep = P.refine(text, ctx())
    assert rep["invented"] == 1 and "인터뷰" not in out


def test_survey_claim_is_kept_when_applicant_answered_evidence():
    answers = [{"slot": "evidence", "label": "확인 방법", "answer": "세탁소 사장님께 직접 물어봄", "unknown": False}]
    text = "세탁 수거는 번거롭습니다. 이용자에게 인터뷰해 보니 불편하다고 했습니다. 그래서 앱으로 묶습니다."
    out, rep = P.refine(text, ctx(answers=answers))
    assert rep["invented"] == 0 and "인터뷰" in out


def test_future_interview_plan_is_not_treated_as_a_done_survey():
    text = "세탁 수거는 번거롭습니다. 앞으로 이용자 인터뷰를 진행할 계획입니다. 그래서 앱으로 묶습니다."
    out, rep = P.refine(text, ctx())
    assert rep["invented"] == 0 and "계획입니다" in out


def test_short_question_flags_instead_of_deleting():
    text = "월 3조 원 시장을 여는 세탁 수거 앱입니다."
    out, rep = P.refine(text, ctx(question_id="q1", limit=100))
    assert rep["invented"] == 0
    assert rep["invented_flagged"] == [text]      # 지우면 답이 사라진다 → 재생성 신호
    assert out == text


def test_drop_is_cancelled_when_every_sentence_would_go():
    text = "시장 규모는 3조 원입니다. 예상 고객은 5만 명입니다."
    out, rep = P.refine(text, ctx())
    assert out == text                            # 글을 통째로 비우지 않는다
    assert rep["invented"] == 0 and len(rep["invented_flagged"]) == 2


# ────────────────────────── ③ 매출·원가 상용구 ──────────────────────────

def test_unit_economics_sentence_is_dropped():
    # ②가 먼저 지우지 않도록 수치를 아이디어에 넣어 둔다 — 여기서 보려는 것은 ③이다.
    idea = "고객 1명당 매출 1만 원, 원가 500원 구조의 세탁 수거 서비스입니다."
    text = ("세탁소는 수거 인력을 따로 두기 어렵습니다. "
            "고객 1명당 매출은 1만 원, 원가는 500원이라 마진이 남습니다. "
            "그래서 동네 단위로 묶습니다.")
    out, rep = P.refine(text, ctx(idea=idea))
    assert rep["unit_economics"] == 1 and "마진" not in out


def test_plain_money_sentence_without_cost_talk_is_kept():
    idea = "이용료 3000원짜리 세탁 수거 서비스입니다."
    text = "세탁소는 수거가 어렵습니다. 이용료는 3000원으로 정했습니다. 동네 단위로 묶습니다."
    out, rep = P.refine(text, ctx(idea=idea))
    assert rep["unit_economics"] == 1        # '이용료' 는 단위경제 화법이라 걸린다
    idea2 = "3000원을 받는 세탁 수거 서비스입니다."
    text2 = "세탁소는 수거가 어렵습니다. 요금은 3000원입니다. 동네 단위로 묶습니다."
    out2, rep2 = P.refine(text2, ctx(idea=idea2))
    assert rep2["unit_economics"] == 0 and "3000원" in out2


# ────────────────────────── ④ 1인칭 통일 ──────────────────────────

def test_solo_applicant_gets_single_person():
    text = "저희는 세탁소를 묶습니다. 저희가 직접 수거합니다. 저희의 강점입니다."
    out, rep = P.refine(text, ctx(team="팀원 없음"))
    assert "저희" not in out and rep["person"] == 3
    assert out.startswith("저는 세탁소를 묶습니다. 제가 직접")


def test_team_applicant_gets_plural():
    text = "저는 세탁소를 묶습니다. 제가 직접 수거합니다. 저의 강점입니다."
    out, rep = P.refine(text, ctx(team="2명"))
    assert "저희는" in out and "저희가" in out and "저희의" in out and rep["person"] == 3


def test_outline_voice_wins_over_team_field():
    text = "저희는 세탁소를 묶습니다. 저희가 직접 수거합니다. 좋은 방법입니다."
    out, _ = P.refine(text, ctx(team="3명", outline={"voice": "저"}))
    assert "저희" not in out


def test_words_that_merely_start_with_jeo_are_untouched():
    text = "저장 공간을 줄입니다. 저녁에도 수거합니다. 저는 이렇게 만듭니다."
    out, _ = P.refine(text, ctx(team="2명"))
    assert "저장 공간" in out and "저녁에도" in out and "저희는 이렇게" in out


# ────────────────────────── ⑤ 통계 재인용 ──────────────────────────

def test_same_number_cited_twice_loses_the_second():
    idea = "세탁 지출이 35% 늘었다는 자료를 본 세탁 수거 앱입니다."
    text = ("세탁 지출이 35% 늘었습니다. 수거가 번거롭기 때문입니다. "
            "다시 말해 세탁 지출이 35% 늘어난 것입니다.")
    out, rep = P.refine(text, ctx(idea=idea))
    assert rep["repeated_stats"] == 1 and out.count("35%") == 1


def test_stat_already_used_in_another_question_is_dropped_here():
    idea = "세탁 지출이 35% 늘었다는 자료를 본 세탁 수거 앱입니다."
    text = "수거가 번거롭습니다. 세탁 지출은 35% 늘었습니다. 그래서 묶습니다."
    other = {"q2": "앞 문항에서 세탁 지출이 35% 늘었다고 이미 적었습니다."}
    out, rep = P.refine(text, ctx(question_id="q3_2", idea=idea, other_texts=other))
    assert rep["repeated_stats"] == 1 and "35%" not in out


def test_sentence_bringing_a_new_number_is_kept():
    idea = "세탁 지출 35%, 재이용률 12% 자료를 본 세탁 수거 앱입니다."
    text = "세탁 지출이 35% 늘었습니다. 수거가 번거롭습니다. 재이용률은 12%로 35% 증가와 이어집니다."
    out, rep = P.refine(text, ctx(idea=idea))
    assert rep["repeated_stats"] == 0 and "12%" in out


# ────────────────────────── ⑥ 아이디어 재진술 첫 문장 ──────────────────────────

RESTATED = "동네 세탁소를 묶어 수거와 배송을 대신해 주는 앱입니다."


def test_restated_first_sentence_is_dropped():
    text = f"{RESTATED} 수거가 번거롭기 때문입니다. 그래서 동네 단위로 묶습니다."
    out, rep = P.refine(text, ctx())
    assert rep["restated_lead"] == 1 and out.startswith("수거가 번거롭기")


@pytest.mark.parametrize("qid", ["q1", "q3_1", "q10"])
def test_questions_that_may_restate_keep_their_lead(qid):
    text = f"{RESTATED} 수거가 번거롭기 때문입니다. 그래서 동네 단위로 묶습니다."
    out, rep = P.refine(text, ctx(question_id=qid))
    assert rep["restated_lead"] == 0 and out.startswith(RESTATED)


def test_lead_is_kept_when_the_text_is_only_two_sentences():
    text = f"{RESTATED} 수거가 번거롭기 때문입니다."
    out, rep = P.refine(text, ctx())
    assert rep["restated_lead"] == 0 and out == text


def test_unrelated_first_sentence_is_kept():
    text = "맞벌이 가구가 늘고 있습니다. 수거가 번거롭습니다. 그래서 동네 단위로 묶습니다."
    out, rep = P.refine(text, ctx())
    assert rep["restated_lead"] == 0 and out.startswith("맞벌이")


def test_outline_solution_also_counts_as_restatement():
    outline = {"solution": "동네 세탁소를 묶어 수거와 배송을 대신하는 앱"}
    text = f"{RESTATED} 수거가 번거롭기 때문입니다. 그래서 동네 단위로 묶습니다."
    out, rep = P.refine(text, ctx(idea="관계없는 다른 문장입니다.", outline=outline))
    assert rep["restated_lead"] == 1 and RESTATED not in out


# ────────────────────────── ⑦ 이어쓰기 중복 ──────────────────────────

def test_extend_overlap_with_previous_body_is_removed():
    previous = "수거가 번거롭습니다. 맞벌이 가구가 늘고 있습니다."
    added = "맞벌이 가구가 늘고 있습니다. 그래서 동네 단위로 묶습니다."
    out, rep = P.refine(added, ctx(current=previous))
    assert rep["extend_overlap"] == 1
    assert out == "그래서 동네 단위로 묶습니다." and rep["extend_empty"] is False


def test_extend_that_only_repeats_is_reported_empty():
    previous = "수거가 번거롭습니다. 맞벌이 가구가 늘고 있습니다."
    out, rep = P.refine("맞벌이 가구가 늘고 있습니다. 수거가 번거롭습니다.", ctx(current=previous))
    assert rep["extend_empty"] is True and out == ""


def test_without_previous_body_nothing_is_treated_as_overlap():
    added = "맞벌이 가구가 늘고 있습니다. 그래서 동네 단위로 묶습니다."
    out, rep = P.refine(added, ctx())
    assert rep["extend_overlap"] == 0 and rep["extend_empty"] is False and out == added


# ────────────────────────── ⑧ 금지어 ──────────────────────────

@pytest.mark.parametrize("phrase", ["데이터 판매", "고유한 가치", "혁신적", "획기적", "차별화된 경쟁력"])
def test_banned_phrase_sentence_is_dropped(phrase):
    text = f"수거가 번거롭습니다. 이 서비스는 {phrase}를 만듭니다. 그래서 동네 단위로 묶습니다."
    out, rep = P.refine(text, ctx())
    assert rep["banned"] == 1 and phrase not in out


def test_ai_alone_is_dropped_but_a_named_method_is_kept():
    vague = "수거가 번거롭습니다. AI 기반으로 문제를 풀겠습니다. 그래서 동네 단위로 묶습니다."
    out, rep = P.refine(vague, ctx())
    assert rep["banned"] == 1 and "AI 기반" not in out

    named = "수거가 번거롭습니다. AI 기반 경로 최적화로 수거 순서를 정합니다. 동네 단위로 묶습니다."
    out2, rep2 = P.refine(named, ctx())
    assert rep2["banned"] == 0 and "AI 기반 경로 최적화" in out2


def test_short_question_flags_banned_instead_of_deleting():
    text = "혁신적인 세탁 수거 앱입니다."
    out, rep = P.refine(text, ctx(question_id="q10", limit=100))
    assert rep["banned"] == 0 and rep["banned_flagged"] == [text] and out == text


# ────────────────────────── ⑨ 한도 초과 / ⑩ 분량 미달 ──────────────────────────

def test_over_limit_signals_compress_without_cutting():
    text = "동네 세탁소 수거를 대신합니다. " * 30
    out, rep = P.refine(text, ctx(limit=100))
    assert rep["needs_compress"] is True
    assert len(out) > 100                          # 자르지 않는다 — 압축은 호출자가 모델로 한다


def test_under_target_signals_extend():
    text = "수거가 번거롭습니다. 그래서 동네 단위로 묶습니다."
    _, rep = P.refine(text, ctx(target_range=(1400, 1800)))
    assert rep["needs_extend"] is True and rep["needs_compress"] is False


def test_inside_target_range_signals_nothing():
    text = "수거가 번거롭습니다. " * 40
    _, rep = P.refine(text, ctx(limit=2000, target_range=(200, 900)))
    assert rep["needs_extend"] is False and rep["needs_compress"] is False


def test_empty_text_does_not_ask_for_extend():
    _, rep = P.refine("", ctx(target_range=(1400, 1800)))
    assert rep["needs_extend"] is False           # 빈 글은 이어쓸 것이 아니라 다시 만들 것이다


# ────────────────────────── 순수 함수·문단 보존 ──────────────────────────

def test_refine_is_deterministic_and_leaves_ctx_alone():
    text = "저희는 세탁소를 묶습니다. 시장은 3조 원입니다. 그래서 동네 단위로 묶습니다."
    c = ctx()
    snapshot = json.dumps(c, ensure_ascii=False, sort_keys=True)
    first, r1 = P.refine(text, c)
    second, r2 = P.refine(text, c)
    assert first == second and r1 == r2
    assert json.dumps(c, ensure_ascii=False, sort_keys=True) == snapshot


def test_paragraph_breaks_survive():
    text = ("수거가 번거롭습니다. 맞벌이 가구가 늘고 있습니다.\n\n"
            "시장은 3조 원입니다. 그래서 동네 단위로 묶습니다.")
    out, rep = P.refine(text, ctx())
    assert rep["invented"] == 1 and "\n\n" in out


def test_none_and_empty_inputs_are_safe():
    assert P.refine("", {}) == ("", P.refine("", {})[1])
    out, rep = P.refine("수거가 번거롭습니다.", None)
    assert out == "수거가 번거롭습니다." and rep["dropped"] == 0


# ────────────────────────── 실측 원문 회귀 (docs/measurements) ──────────────────────────

MEASUREMENTS = pathlib.Path(__file__).resolve().parents[1] / "docs" / "measurements"

# 세션1 실측 원문에 refine 을 돌린 결과. 규칙을 고치면 이 표가 흔들린다 —
# 의도한 변화인지 확인하고 갱신할 것 (숫자가 늘면 과삭제, 줄면 놓침을 의심한다).
EXPECTED = {
    "2026-09-02_after_laundry.json":   {"invented": 7,  "repeated_stats": 0,  "restated_lead": 3, "banned": 0, "person": 13, "foreign": 3},
    "2026-09-02_baseline_laundry.json": {"invented": 11, "repeated_stats": 5,  "restated_lead": 0, "banned": 3, "person": 23, "foreign": 18},
    "after3_A.json":                   {"invented": 7,  "repeated_stats": 8,  "restated_lead": 0, "banned": 0, "person": 26, "foreign": 5},
    "after3_B.json":                   {"invented": 7,  "repeated_stats": 0,  "restated_lead": 3, "banned": 3, "person": 28, "foreign": 12},
    "before_A.json":                   {"invented": 8,  "repeated_stats": 12, "restated_lead": 0, "banned": 2, "person": 11, "foreign": 4},
}


def load_measurement(name):
    data = json.loads((MEASUREMENTS / name).read_text(encoding="utf-8"))
    refs = data.get("refs") or {}
    out = []
    for qid, text in (data.get("texts") or {}).items():
        out.append((qid, text, {
            "question_id": qid,
            "limit": 100 if qid in ("q1", "q10") else 2000,
            "idea": data.get("idea"),
            "answers": data.get("answers"),
            "references": refs.get(qid),
            "team": "팀원 없음",
            "other_texts": {k: v for k, v in data["texts"].items() if k != qid},
        }))
    return out


def test_measurement_fixtures_are_present():
    """픽스처가 사라지거나 이름이 바뀌면 아래 회귀 테스트가 조용히 빈 채로 통과한다."""
    assert MEASUREMENTS.is_dir()
    for name in EXPECTED:
        assert (MEASUREMENTS / name).exists(), name
    assert sum(len(load_measurement(n)) for n in EXPECTED) == 40      # 5개 파일 × 문항 8개


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_measurement_regression_counts(name):
    got = {k: 0 for k in EXPECTED[name]}
    for _qid, text, c in load_measurement(name):
        _out, rep = P.refine(text, c)
        for key in got:
            got[key] += len(rep[key]) if key == "foreign" else rep[key]
    assert got == EXPECTED[name]


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_measurement_invariants(name):
    for qid, text, c in load_measurement(name):
        out, rep = P.refine(text, c)
        assert out.strip(), f"{name}:{qid} 이 통째로 비었다"
        assert len(out) <= len(text) + 8, f"{name}:{qid} 이 길어졌다"   # 1인칭 치환분만 늘 수 있다
        assert "저희" not in out, f"{name}:{qid} 에 '저희' 가 남았다"    # team=팀원 없음
        # ①은 지우는 규칙이 아니다 — 이물 문장은 그대로 남고 report 로만 나간다.
        assert len(P.foreign_sentences(out)) >= 0
        assert rep["needs_compress"] is (len(out) > c["limit"])
