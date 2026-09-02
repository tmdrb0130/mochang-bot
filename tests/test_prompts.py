"""prompts/ md 세트가 PROJECT_CONTEXT 4절 구조·5절 항목을 갖추고, 코드가 md 로더로만 프롬프트를 만드는지 확인."""
import pytest

from backend.pipeline import assemble

QUESTIONS = ["q1", "q2", "q3_1", "q3_2", "q4_1", "q4_2", "q7_1", "q8", "q10"]


@pytest.mark.parametrize("qid", QUESTIONS)
def test_every_question_md_has_required_sections(qid):
    meta, body = assemble.load_question(qid)
    for key in ("id", "label", "title", "limit", "target"):
        assert key in meta, f"{qid}.md frontmatter 에 {key} 없음"
    for section in ("[문항 의도]", "[프로세스 위치]", "[골자 사용 규칙]", "[필수 요소]", "[권장 요소]", "[피할 것]"):
        assert section in body, f"{qid}.md 에 {section} 없음"


@pytest.mark.parametrize("relpath", ["system.md", "tracks/tech.md", "tracks/local.md", "styles/story.md", "styles/logic.md",
                                     "styles/plain.md", "extend.md", "intake.md", "intake_regenerate.md", "short_question.md", "sections.md",
                                     "verify.md", "research/queries.md", "research/extract_facts.md",
                                     "research/followup_queries.md", "q1_structures.md", "process.md", "shorten.md", "outline.md", "polish.md", "lengthen.md", "q10_structures.md", "intake_fallback.md"])
def test_prompt_files_exist(relpath):
    assert assemble.read_prompt(relpath)


def test_render_fills_placeholders_only():
    s = assemble.render("short_question.md", min=60, max=90, limit=100, style="story")
    assert "60~90자" in s and "100자를" in s and "(story)" in s and "{" not in s


def test_section_headers_loaded_from_md():
    h = assemble.section_headers()
    assert set(h) >= {"facts", "unknowns", "references", "extend_current"}
    assert h["facts"].startswith("[")


def test_build_prompts_assembles_in_documented_order():
    system, user, meta = assemble.build_prompts({"track": "local", "style": "logic", "question_id": "q2", "idea": "아이디어",
                                                 "team": "2명", "capability": "경험"})
    i_sys = system.index("모두의 창업 프로젝트")
    i_track = system.index("[지원 트랙 지침]")
    i_q = system.index("[작성 문항]")
    i_style = system.index("[글 스타일]")
    assert i_sys < i_track < i_q < i_style                      # system + track + question + style
    assert "로컬 분야" in system and "Q2." in system and "2000자를 넘기지 말 것" in system
    assert "지원 트랙: 로컬 분야" in user and "아이디어" in user and "팀원 수(본인 제외): 2명" in user
    assert meta["limit"] == 2000


def test_short_questions_use_short_rule_instead_of_style():
    for qid in ("q1", "q10"):
        system, _, _ = assemble.build_prompts({"track": "tech", "style": "story", "question_id": qid, "idea": "x"})
        assert "[짧은 문항 규칙" in system and "[글 스타일]" not in system


def test_unknown_question_raises():
    with pytest.raises(assemble.PromptNotFound):
        assemble.build_prompts({"track": "tech", "style": "story", "question_id": "q99", "idea": "x"})


# ── 작업 16: 창업교육 MVP 8단계 프레임 ──

def test_process_block_lists_eight_stages_in_order():
    text = assemble.process_block()
    for n, name in enumerate(["문제 발견", "고객 정의", "문제 검증", "해결책 설계",
                              "비즈니스 모델", "MVP 기획", "MVP 제작", "고객 검증"], start=1):
        assert f"{n}. {name}" in text, f"{n}단계({name}) 줄이 없음"
    assert "한 문항에 한두 번" in text                      # 용어 남용 금지 규칙


def test_process_block_goes_into_system_prompt_once():
    system, _, _ = assemble.build_prompts({"question_id": "q2", "track": "tech", "style": "logic", "idea": "x"})
    assert system.count("[창업 프로세스 8단계]") == 1
    # 순서: 공통 지침 → 프로세스 → 트랙 → 문항
    assert system.index("[창업 프로세스 8단계]") < system.index("[지원 트랙 지침]") < system.index("[작성 문항]")


def test_intake_prompt_carries_the_same_block():
    from backend.pipeline import intake
    assert "[창업 프로세스 8단계]" in intake._regenerate_prompt({}, {}, {}, "")


@pytest.mark.parametrize("qid,stage", [("q1", "고객 정의"), ("q2", "문제 검증"), ("q3_1", "해결책 설계"),
                                       ("q3_2", "비즈니스 모델"), ("q4_1", "MVP 기획"), ("q7_1", "문제 발견")])
def test_each_question_names_its_stage(qid, stage):
    _, body = assemble.load_question(qid)
    section = body.split("[프로세스 위치]")[1].split("[필수 요소]")[0]
    assert stage in section, f"{qid} 의 [프로세스 위치]에 '{stage}' 가 없음"


def test_q4_1_roadmap_follows_plan_build_verify():
    """작업 30 — 순서 선언 → 1차 범위 → 고객 검증 → 초기 고객 → 유료화 → 자금 → 가정 → 확장."""
    _, body = assemble.load_question("q4_1")
    marks = ["① 순서 선언", "② 1차 범위", "③ 고객 검증", "④ 초기 고객 확보", "⑤ 유료화 검증", "⑥ 자금 배분", "⑦ 핵심 가정", "⑧ 확장"]
    positions = [body.index(m) for m in marks]
    assert positions == sorted(positions)
    assert "1,400~1,800자" in body and "2,000자를 채웁니다" not in body
    assert "500명" not in body and "60%" not in body            # 라마가 베끼던 예시 숫자 삭제 (Q4_1_QUALITY 2-1)


@pytest.mark.parametrize("qid,target", [("q2", "1,400~1,800자"), ("q3_1", "1,400~1,800자"), ("q3_2", "1,300~1,700자"),
                                        ("q4_1", "1,400~1,800자"), ("q4_2", "1,200~1,600자"), ("q8", "1,200~1,600자")])
def test_long_questions_target_below_the_limit(qid, target):
    """WORKORDER_QUALITY 1-3 — 분량 목표를 한도 아래로, '2,000자를 채웁니다' 전부 삭제."""
    meta, body = assemble.load_question(qid)
    assert target in body and "2,000자를 채웁니다" not in body
    assert int(meta["min"]) < int(meta["limit"])


@pytest.mark.parametrize("qid", ["q2", "q3_1", "q3_2", "q4_1", "q4_2", "q8", "q1", "q10"])
def test_rules_have_no_field_specific_vocabulary(qid):
    """일반화 원칙 2 — 규칙 문장에 특정 소재(면접·이력서·레시피 등)가 들어가면 안 된다."""
    _, body = assemble.load_question(qid)
    for word in ("면접", "이력서", "레시피", "세탁", "냉장고", "캠핑", "딸기", "돌봄"):
        assert word not in body, f"{qid}.md 에 소재 어휘 '{word}'"


def test_q8_has_the_principles_block_first():
    _, body = assemble.load_question("q8")
    assert body.index("[원칙]") < body.index("[문항 의도]")
    assert "나이·성별·학력·직업·신분" in body and "존재하지 않는 역량" in body


def test_q4_2_has_capability_axes_and_bans_contradiction():
    _, body = assemble.load_question("q4_2")
    assert "[역량 축" in body and "반대편 축" in body
    assert "모른다·부족하다" in body                       # 입력된 강점을 부족으로 쓰지 않는다


def test_q3_2_bans_data_sale_and_made_up_unit_economics():
    _, body = assemble.load_question("q3_2")
    assert "데이터 판매" in body and "단위 경제·매출 추정" in body and "수익원 3개 이상" in body


def test_system_prompt_has_generalization_rules():
    text = assemble.read_prompt("system.md")
    assert "인물상을 전제하지 않습니다" in text and "되풀이해서 채우느니 짧게" in text
    assert "소프트웨어가 아닐 수도" in text


def test_q2_requires_real_verification_or_a_plan():
    _, body = assemble.load_question("q2")
    assert "검증 계획" in body and "만들어 내면 실격" in body
    assert "아직 확인하지 못했다" in body               # 근거가 없으면 인정하고 계획을 쓴다


def test_q2_has_the_six_paragraph_structure():
    """작업 26 — 골든 예문 기준 문단 구조. 하는 일 설명은 ⑤ 한 곳뿐이어야 한다."""
    _, body = assemble.load_question("q2")
    order = [body.index(m) for m in ("① 겪은 어려움", "② 누가 겪는가", "③ 지금 방식으로는 왜 안 되는가",
                                     "④ 사람들이 이미", "⑤ 그래서 무엇이 필요한가", "⑥ 확인했거나")]
    assert order == sorted(order)
    assert "1,400~1,800자" in body and "한 번만" in body
    assert "자기소개로 시작하기" in body and "고유한 가치" in body        # 진단표 5·8



