"""프롬프트 조립기.

조립 순서: system + track + question + style (+ 나중에 RAG 참고자료) → system 프롬프트
지원자 입력(아이디어·창업여부·역량·팀원) → user 프롬프트
"""
import re
from pathlib import Path

import yaml

PROMPTS_DIR = Path(__file__).resolve().parents[1] / "prompts"

TRACK_NAMES = {"tech": "일반/기술 분야", "local": "로컬 분야"}

_FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.S)


class PromptNotFound(Exception):
    pass


def _read(relpath: str) -> str:
    path = PROMPTS_DIR / relpath
    if not path.exists():
        raise PromptNotFound(f"프롬프트 파일이 없습니다: {relpath}")
    return path.read_text(encoding="utf-8").strip()


read_prompt = _read  # 다른 모듈(intake 등)에서 쓰는 공개 이름


def load_question(question_id: str) -> tuple[dict, str]:
    """questions/<id>.md 를 (frontmatter 메타, 본문)으로 파싱.

    메타의 '필수 요소' 목록은 나중에 검증(verify) 단계에서도 재사용한다.
    """
    text = _read(f"questions/{question_id}.md")
    m = _FRONTMATTER.match(text)
    if not m:
        raise PromptNotFound(f"questions/{question_id}.md 에 frontmatter(---)가 없습니다")
    meta = yaml.safe_load(m.group(1))
    return meta, m.group(2).strip()


def build_context(form: dict) -> str:
    """지원자 입력 → user 프롬프트."""
    parts = [
        f"지원 트랙: {TRACK_NAMES.get(form['track'], form['track'])}",
        f"아이디어 설명:\n{form['idea']}",
        "지원자 창업 여부: " + ("현재 사업자" if form.get("is_business") else "예비창업자(현재 사업자 아님)"),
    ]
    if form.get("is_business") and form.get("current_item"):
        parts.append(f"현재 운영 중인 사업: {form['current_item']}")
    parts.append(f"팀원 수(본인 제외): {form.get('team', '팀원 없음')}")
    capability = form.get("capability") or "(입력 없음 — 아이디어 설명에서 유추 가능한 범위만 언급하고 지어내지 말 것)"
    parts.append(f"지원자 역량·경력: {capability}")
    parts.extend(_answer_sections(form.get("answers") or []))
    return "\n\n".join(parts)


def _answer_sections(answers: list[dict]) -> list[str]:
    """인테이크 카드 답변 → 프롬프트 섹션.

    answers 항목: {"slot", "label", "answer": str|list|None, "unknown": bool}
    - answer 있음  → [지원자가 확인한 사실]  그대로 사실로 써도 됨
    - unknown=True → [지원자가 모른다고 한 것] 가정임이 드러나게 쓰고, Q4-2(멘토 도움)의 재료로 삼을 것
    """
    facts, unknowns = [], []
    for a in answers:
        if not isinstance(a, dict):
            continue
        label = a.get("label") or a.get("slot") or ""
        ans = a.get("answer")
        if isinstance(ans, list):
            ans = ", ".join(str(x) for x in ans if str(x).strip())
        ans = (str(ans).strip() if ans is not None else "")
        if a.get("unknown") or not ans:
            unknowns.append(f"- {label}")
        else:
            facts.append(f"- {label}: {ans}")
    out = []
    if facts:
        out.append("[지원자가 확인한 사실 — 그대로 사실로 써도 됨]\n" + "\n".join(facts))
    if unknowns:
        out.append("[지원자가 아직 모른다고 한 것 — 확정된 것처럼 쓰지 말고 '가정'·'계획'으로 표현. 멘토 도움(Q4-2)에서 다룰 것]\n" + "\n".join(unknowns))
    return out


def build_prompts(form: dict) -> tuple[str, str, dict]:
    """(system 프롬프트, user 프롬프트, 문항 메타) 반환."""
    system_md = _read("system.md")
    track_md = _read(f"tracks/{form['track']}.md")
    style_md = _read(f"styles/{form['style']}.md")
    meta, question_body = load_question(form["question_id"])

    question_section = "\n".join([
        "[작성 문항]",
        f"{meta['label']}. {meta['title']}",
        f"분량: {meta['target']} (절대 {meta['limit']}자를 넘기지 말 것)",
        "",
        question_body,
    ])

    if int(meta.get("limit", 2000)) <= 100:
        # Q1·Q10 같은 100자 문항: 스타일(장면 묘사·1인칭 서술)을 적용하면 문장 중간에서 잘린다.
        # 스타일 섹션을 빼고 '한 문장' 규칙을 최우선으로 둔다.
        style_section = "\n".join([
            "[짧은 문항 규칙 — 다른 어떤 지시보다 우선]",
            f"- 한 문장(길어도 두 문장), 줄바꿈 없이 {int(meta['limit'] * 0.6)}~{int(meta['limit'] * 0.9)}자. {meta['limit']}자를 넘기면 안 되므로 여유를 둔다.",
            "- 서두나 배경 문장을 따로 두지 않는다. 첫 문장이 곧 본문이다.",
            "- 장면 묘사, 배경 설명, '저는 ~입니다' 같은 자기소개로 시작하지 않는다. 바로 '누구에게 무엇을 어떻게 해주는 무엇'을 말한다.",
            "- 문장 끝은 '~하는 앱입니다', '~해 주는 서비스입니다'처럼 명사로 맺는다.",
            f"- 글 스타일({form['style']})은 어휘 톤에만 살짝 반영하고 구조는 바꾸지 않는다.",
        ])
    else:
        style_section = f"[글 스타일]\n{style_md}"

    system = "\n\n".join([
        system_md,
        f"[지원 트랙 지침]\n{track_md}",
        question_section,
        style_section,
    ])
    return system, build_context(form), meta


def build_extend_prompts(form: dict, current: str) -> tuple[str, str, dict]:
    """이어쓰기: 문항 생성 프롬프트 + extend.md 지시, user 에 기존 글 첨부."""
    system, user, meta = build_prompts(form)
    room = max(meta["limit"] - len(current), 0)
    room = min(room - 30, 700) if room > 30 else room
    extend_md = _read("extend.md").replace("{room}", str(room))
    system = "\n\n".join([system, extend_md])
    user = "\n\n".join([user, f"이미 작성된 글:\n{current}"])
    meta = {**meta, "room": room}
    return system, user, meta
