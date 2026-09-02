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
                                     "research/followup_queries.md", "q1_structures.md", "process.md", "shorten.md", "outline.md", "polish.md", "lengthen.md"])
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
    _, body = assemble.load_question("q4_1")
    road = body.split("2) 단계별 로드맵")[1].split("3)")[0]
    assert road.index("최소한으로") < road.index("어떻게 만들") < road.index("고객에게 어떻게 확인")


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


def test_q2_rules_have_no_field_specific_vocabulary():
    """일반화 원칙 2 — 규칙 문장에 특정 소재(면접·이력서·레시피 등)가 들어가면 안 된다."""
    _, body = assemble.load_question("q2")
    for word in ("면접", "이력서", "레시피", "세탁", "냉장고", "캠핑"):
        assert word not in body, f"q2.md 에 소재 어휘 '{word}' 가 들어갔다"
