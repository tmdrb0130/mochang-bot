"""프롬프트 조립기.

조립 순서: system + track + question + style (+ 나중에 RAG 참고자료) → system 프롬프트
지원자 입력(아이디어·창업여부·역량·팀원) → user 프롬프트
"""
import hashlib
import itertools
import re
from pathlib import Path

import yaml

PROMPTS_DIR = Path(__file__).resolve().parents[1] / "prompts"

TRACK_NAMES = {"tech": "일반/기술 분야", "local": "로컬 분야"}

_FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.S)


class PromptNotFound(Exception):
    pass

# ── Q1 문장 짜임 회전 (작업 15 후속) ──
# 라마는 "넷 중 골라 써라" 같은 선택형·금지형 지시를 지키지 못한다(실호출 25건 확인) — 그래서 코드가 **하나만**
# 골라 넣는다. 문구는 여전히 md 에만 있고(q1_structures.md), 코드는 어느 것을 넣을지만 정한다.
STRUCTURES_FILE = "q1_structures.md"
_STRUCT_SEP = re.compile("^-{3,}$", re.M)
_rotation = itertools.count()          # 요청마다 한 칸씩 — 같은 아이디어를 다시 눌러도 다른 짜임이 나온다


def structures() -> list[str]:
    """q1_structures.md 를 '---' 줄로 나눈 짜임 목록."""
    return [b.strip() for b in _STRUCT_SEP.split(_read(STRUCTURES_FILE)) if b.strip()]


def pick_structure(idea: str, offset: int) -> str:
    """아이디어 해시 + 회전값으로 짜임 하나를 고른다. 아이디어가 다르면 시작점이 다르고, 요청마다 한 칸씩 돈다."""
    items = structures()
    base = int(hashlib.sha1((idea or "").encode("utf-8")).hexdigest()[:8], 16)
    return items[(base + int(offset)) % len(items)]


def _read(relpath: str) -> str:
    path = PROMPTS_DIR / relpath
    if not path.exists():
        raise PromptNotFound(f"프롬프트 파일이 없습니다: {relpath}")
    return path.read_text(encoding="utf-8").strip()


read_prompt = _read  # 다른 모듈(intake 등)에서 쓰는 공개 이름


def process_block() -> str:
    """창업교육 MVP 8단계 프레임 (process.md). system 프롬프트와 인테이크에 같은 블록을 한 번씩 넣는다.

    문항별로 '어느 단계를 다루는 글인지'는 각 문항 md 의 [프로세스 위치] 절이 말한다 (docs/MVP_PROCESS.md 2절)."""
    return _read("process.md")


def section_headers() -> dict:
    """prompts/sections.md frontmatter — user 프롬프트 섹션 머리말 문구. 코드에 한국어 문구를 박지 않기 위함."""
    m = _FRONTMATTER.match(_read("sections.md"))
    return yaml.safe_load(m.group(1)) if m else {}


def render(relpath: str, **vars) -> str:
    """md 를 읽고 {name} 자리표시자를 채운다. 값에 있는 중괄호는 건드리지 않는다."""
    text = _read(relpath)
    for k, v in vars.items():
        text = text.replace("{" + k + "}", str(v))
    return text


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


def _is_short_question(question_id: str | None) -> bool:
    """100자 이내로 끝나는 문항인지(Q1·Q10). 참고자료 삽입 여부를 정할 때 쓴다."""
    if not question_id:
        return False
    try:
        meta, _ = load_question(question_id)
    except Exception:
        return False
    return int(meta.get("limit", 2000)) <= 100


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
    refs = form.get("references")
    if refs and _is_short_question(form.get("question_id")):
        # Q1·Q10 처럼 90자 안팎으로 끝나는 문항: 인용할 자리가 없는데
        # 참고자료 1,000자와 "인용하면 출처를 붙이라"는 지시가 붙으면
        # 한 문장에 통계를 욱여넣게 된다. 아예 넣지 않는다.
        refs = None
    if refs:
        # 조사 파이프라인(backend/rag/pipeline.py)이 만든 사실 목록 → 참고자료 섹션. 문자열이면 그대로.
        if isinstance(refs, str):
            parts.append(refs)
        else:
            from ..rag.pipeline import format_references
            section = format_references(refs)
            if section:
                parts.append(section)
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
    h = section_headers()
    if facts:
        out.append(h.get("facts", "[지원자가 확인한 사실]") + "\n" + "\n".join(facts))
    if unknowns:
        out.append(h.get("unknowns", "[지원자가 아직 모른다고 한 것]") + "\n" + "\n".join(unknowns))
    return out


def build_prompts(form: dict) -> tuple[str, str, dict]:
    """(system 프롬프트, user 프롬프트, 문항 메타) 반환."""
    meta_check, _ = load_question(form["question_id"])
    if meta_check.get("only_business") and not form.get("is_business"):
        # Q7-1("현재 사업 아이템과 어떻게 다른가")은 기창업자 전용이다.
        # 프론트는 이미 거르지만(App.jsx QUESTIONS.filter), API 를 직접 부르면 뚫린다.
        # 막지 않으면 있지도 않은 기존 사업을 지어내서 쓴다.
        raise PromptNotFound(
            f"{meta_check['label']} 문항은 현재 사업자에게만 해당합니다 (지원자 창업 여부: 예비창업자).")

    system_md = _read("system.md")
    track_md = _read(f"tracks/{form['track']}.md")
    style_md = _read(f"styles/{form['style']}.md")
    meta, question_body = load_question(form["question_id"])
    structure_offset = form.get("structure_offset")
    if "{structure}" in question_body:
        # 회전값은 요청마다 하나씩 소비한다. 재생성(generate_one)은 meta 로 받은 값에 +1 해서 다른 짜임을 부른다.
        structure_offset = next(_rotation) if structure_offset is None else int(structure_offset)
        question_body = question_body.replace("{structure}", pick_structure(form.get("idea", ""), structure_offset))
    meta = {**meta, "structure_offset": structure_offset}

    question_section = "\n".join([
        "[작성 문항]",
        f"{meta['label']}. {meta['title']}",
        f"분량: {meta['target']} (절대 {meta['limit']}자를 넘기지 말 것)",
        "",
        question_body,
    ])

    if int(meta.get("limit", 2000)) <= 100:
        # Q1·Q10 같은 100자 문항: 스타일(장면 묘사·1인칭 서술)을 적용하면 문장 중간에서 잘린다.
        # 스타일 섹션 대신 prompts/short_question.md 의 '한 문장' 규칙을 쓴다.
        # 길이는 문항 md 의 min/max 를 우선 쓴다 — 없으면 limit 의 60~90%.
        # (Q1 60~90, Q10 70~90 처럼 문항마다 다르고, frontmatter 의 target 과 어긋나면 안 된다)
        limit = int(meta["limit"])
        lo = int(meta.get("min") or limit * 0.6)
        hi = int(meta.get("max") or limit * 0.9)
        style_section = render("short_question.md", min=lo, max=hi, limit=limit, style=form["style"])
    else:
        style_section = f"[글 스타일]\n{style_md}"

    system = "\n\n".join([
        system_md,
        process_block(),
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
    user = "\n\n".join([user, f"{section_headers().get('extend_current', '이미 작성된 글:')}\n{current}"])
    meta = {**meta, "room": room}
    return system, user, meta
