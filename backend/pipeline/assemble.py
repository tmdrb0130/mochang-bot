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
    return "\n\n".join(parts)


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

    system = "\n\n".join([
        system_md,
        f"[지원 트랙 지침]\n{track_md}",
        question_section,
        f"[글 스타일]\n{style_md}",
    ])
    return system, build_context(form), meta


# ── Q6 사업 분야 선택지 (modoo.or.kr 도전하기 화면 기준) ──
TRACK_FIELDS = {
    "tech": ["IT", "교육", "금융", "운영관리", "네트워킹", "농축/수산업", "라이프스타일", "마케팅/PR", "모빌리티",
             "미디어/엔터테인먼트", "바이오/의료", "에너지/자원", "유통/물류", "임팩트", "재무", "프롭테크", "하드웨어", "기타"],
    "local": ["패션", "F&B", "뷰티", "생활"],
}


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


def build_recommend_prompts(form: dict) -> tuple[str, str, list[str]]:
    """Q6 분야 추천: recommend_field.md + 선택지, user 는 지원자 입력."""
    options = TRACK_FIELDS.get(form["track"], TRACK_FIELDS["tech"])
    system = _read("recommend_field.md").replace("{options}", ", ".join(options))
    return system, build_context(form), options
