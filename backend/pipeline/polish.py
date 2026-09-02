"""생성 후처리 — 지어낸 근거·이물 문자·1인칭 불일치·같은 통계 재인용을 잡는다 (작업 22).

사용자 결정(2026-09-02): 모델은 라마 단독으로 가고, 라마가 못 지키는 것은 **코드가 잡는다**.
실측에서 확인된 것(세션1·2 기준선, after3 사람 판정):
  - 한자·가나 이물이 문항마다 섞인다 (生活·不断·まず·更多·需求·途中)
  - 입력에 없는 인터뷰("10명에게 확인")·금액·인원·배분율을 지어낸다. 게다가 **아이디어가 달라도 같은 숫자**가
    나온다(500명·30명·1억·60/20/10/10) — 라마의 상용구다
  - 팀원이 없는데 문항마다 '저'와 '저희'가 섞인다
  - 같은 통계를 한 문항 안에서 두세 번 인용한다

되돌리는 순서: ① 코드로 확실히 되는 것(1인칭·중복 통계)을 먼저 고치고 ② 남은 문장만 모델에게 한 번 고쳐 쓰게 한 뒤
③ 고쳐 온 문장이 그래도 규칙을 어기면 그 문장을 버린다. 모델 호출은 문항당 최대 1회 늘어난다.
"""
from __future__ import annotations

import json
import re

from ..llm.client import LLMClient
from . import assemble

# 한글도 ASCII 도 아닌 '글자' 는 전부 이물로 본다 — 한자·가나뿐 아니라 태국어·키릴도 실제로 섞여 나왔다
# (실측: 生活·需求·まず·หมด·ใกล). 특정 문자표를 나열하면 새로운 언어가 나올 때마다 놓친다.
# 통계로 셀 값 (일반 수량 '6개·1인' 은 제외 — 반복 판정에 넣으면 오탐이 많다)
STAT = re.compile(r"\d[\d,.]*\s*(?:%|퍼센트|억|천만|만\s*원|원|배|만\s*명|만\s*가구)")
# 확인하지 않은 조사를 한 것처럼 쓰는 표현
CLAIM = re.compile(r"(인터뷰|설문|여쭤|물어보|물어봤|응답자|조사한 결과|만나 보았|만나봤)")
# 라마가 아이디어와 무관하게 반복해 내놓는 상용구 수치 (실측 2건에서 같은 값이 나왔다)
BOILERPLATE = ("500명", "30명", "1억", "1,000만 원", "1000만 원", "60%", "20%", "10%", "5,000만 원")
SENTENCE_SPLIT = re.compile(r"(?<=[.다!?])\s+")
# 어느 아이디어에나 붙어 아무것도 설명하지 못하는 말 (작업 26 진단표 5)
ABSTRACT = ("고유한 가치", "혁신적", "효율적", "큰 도움", "차별화된 경쟁력", "획기적")
# 첫 문장 자기소개 — 신분·전공·경력으로 시작하는 글 (작업 26 진단표 8). 겪은 어려움으로 시작해야 한다.
SELF_INTRO = re.compile(r"^\s*(저는|제가|본인은)[^.]{0,40}"
                        r"(예비\s*창업|창업자|창업을 준비|학과|전공|대학생|대학원생|재학|경험을 가지고)")
MIN_SENTENCE = 8


def sentences(text: str) -> list[str]:
    return [s for s in SENTENCE_SPLIT.split(text or "") if s.strip()]


def has_foreign(text: str) -> bool:
    return any(ch.isalpha() and not ch.isascii() and not ("가" <= ch <= "힣") for ch in text or "")


# ────────────────────────── ④ 1인칭 통일 ──────────────────────────

_SOLO_MAP = [("저희는", "저는"), ("저희가", "제가"), ("저희의", "저의"), ("저희도", "저도"),
             ("저희를", "저를"), ("저희에게", "저에게"), ("저희와", "저와"), ("저희 팀은", "저는"),
             ("저희 팀이", "제가"), ("저희 팀", "저"), ("저희", "저")]


def solo_applicant(form: dict) -> bool:
    """팀원이 없다고 입력한 경우인지. 있으면 '저희' 를 그대로 둔다."""
    team = str(form.get("team") or "").strip()
    return (not team) or ("없" in team) or team in ("0", "0명")


def drop_self_intro(text: str, question_id: str | None) -> tuple[str, int]:
    """Q2 의 첫 문장이 자기소개면 지운다 (겪은 어려움으로 시작해야 한다).

    프롬프트에 '자기소개로 시작하지 않는다' 를 적어도 라마가 '저는 예비창업자로서…' 로 시작한다(실측 6건 중 2건).
    뒤 문장이 충분히 남을 때만 지운다 — 첫 문장뿐인 짧은 글을 비우면 안 된다."""
    if str(question_id or "") != "q2":
        return text, 0
    parts = sentences(text)
    if len(parts) >= 3 and SELF_INTRO.match(parts[0]):
        return " ".join(parts[1:]).strip(), 1
    return text, 0


def unify_person(text: str, form: dict) -> tuple[str, int]:
    """팀원이 없으면 '저희' 를 '저' 로 통일한다. (고친 글, 바꾼 횟수)"""
    if not solo_applicant(form):
        return text, 0
    count = text.count("저희")
    for old, new in _SOLO_MAP:
        text = text.replace(old, new)
    return text, count


# ────────────────────────── ⑥ 아이디어 재진술·중복 문장 ──────────────────────────

def grams(text: str, n: int = 4) -> set:
    packed = re.sub(r"[\s\W]", "", text or "")
    return {packed[i:i + n] for i in range(max(0, len(packed) - n + 1))}


def similarity(a: str, b: str) -> float:
    ga, gb = grams(a), grams(b)
    return len(ga & gb) / len(ga | gb) if (ga or gb) else 0.0


def contains(part: str, whole: str) -> float:
    """part 의 4-gram 중 whole 에 들어 있는 비율. 아이디어 문장이 짧으면 Jaccard 가 낮게 나와
    '재진술' 을 놓치므로 포함률로 함께 본다."""
    gp, gw = grams(part), grams(whole)
    return len(gp & gw) / len(gp) if gp else 0.0


# 아이디어를 그대로 되풀이하는 첫 문장을 지우지 않을 문항 — Q1 은 그것이 답이고, Q3-1 은 해결책 설명이 본론이다.
KEEP_RESTATEMENT = ("q1", "q10", "q3_1")


def drop_idea_restatement(text: str, form: dict) -> tuple[str, int]:
    """첫 문장이 아이디어(또는 골자의 해결책) 재진술이면 지운다 (작업 22 ⑥).

    실측에서 문항 간 유사 문장 쌍이 전부 이 유형이었다 — 문항마다 '저는 … 서비스를 만들고자 합니다' 로 시작한다.
    프롬프트로 금지해도 라마가 무시해 코드로 지운다."""
    if str(form.get("question_id") or "") in KEEP_RESTATEMENT:
        return text, 0
    parts = sentences(text)
    if len(parts) < 3:
        return text, 0
    seeds = [str(form.get("idea") or "")]
    outline = form.get("outline")
    if isinstance(outline, dict):
        seeds.append(str(outline.get("solution") or ""))
    if any(seed and (similarity(parts[0], seed) >= 0.5 or contains(seed, parts[0]) >= 0.6)
           for seed in seeds):
        return " ".join(parts[1:]).strip(), 1
    return text, 0


def dedupe_sentences(text: str, threshold: float = 0.6) -> tuple[str, int]:
    """한 글 안에서 앞 문장과 거의 같은 뒤 문장을 지운다.

    자동 이어쓰기(작업 18)가 앞 문단을 통째로 되풀이하는 일이 실측에서 나왔다 — 모델 호출 없이 잡는다."""
    kept, dropped = [], 0
    for sentence in sentences(text):
        if len(sentence.strip()) >= MIN_SENTENCE and any(similarity(sentence, k) >= threshold for k in kept):
            dropped += 1
            continue
        kept.append(sentence)
    return (" ".join(kept) if dropped else text), dropped


# ────────────────────────── ⑤ 같은 통계 재인용 ──────────────────────────

def drop_repeated_stats(text: str) -> tuple[str, int]:
    """한 문항 안에서 이미 인용한 통계를 다시 인용하는 문장을 지운다. 새 통계가 같이 있으면 남긴다."""
    seen: set[str] = set()
    kept, dropped = [], 0
    for sentence in sentences(text):
        stats = {re.sub(r"\s+", "", s) for s in STAT.findall(sentence)}
        if stats and stats <= seen:
            dropped += 1
            continue
        seen |= stats
        kept.append(sentence)
    return (" ".join(kept) if dropped else text), dropped


# ────────────────────────── ①②③ 문장 표시 ──────────────────────────

def allowed_numbers(form: dict) -> str:
    """지원자 입력·인테이크 답변·참고자료를 한 덩어리로. 여기 없는 수치는 지어낸 것으로 본다."""
    keep = {k: v for k, v in (form or {}).items()
            if k in ("idea", "team", "capability", "current_item", "answers", "references", "outline")}
    return json.dumps(keep, ensure_ascii=False)


def unsupported_numbers(sentence: str, blob: str) -> list[str]:
    """문장 속 통계 중 입력·참고자료에 없는 것."""
    out = []
    for raw in STAT.findall(sentence):
        value = re.match(r"\d[\d,.]*", raw.strip())
        if not value:
            continue
        number = value.group(0).rstrip(".,")
        if len(number) >= 2 and number not in blob and number.replace(",", "") not in blob.replace(",", ""):
            out.append(raw.strip())
    return out


def flag(sentence: str, blob: str, evidence_known: bool) -> list[str]:
    """이 문장이 어떤 규칙을 어겼는지."""
    reasons = []
    if has_foreign(sentence):
        reasons.append("이물")
    if any(b in sentence for b in BOILERPLATE):
        reasons.append("상용구 수치")
    if unsupported_numbers(sentence, blob):
        reasons.append("근거 없는 수치")
    if CLAIM.search(sentence) and not evidence_known:
        reasons.append("하지 않은 조사")
    if any(a in sentence for a in ABSTRACT):
        reasons.append("추상어")
    return reasons


def evidence_known(form: dict) -> bool:
    """지원자가 '직접 확인한 근거' 를 실제로 냈는지 (인테이크 답변 기준)."""
    for answer in form.get("answers") or []:
        if isinstance(answer, dict) and answer.get("slot") == "evidence":
            return bool(answer.get("answer")) and not answer.get("unknown")
    return False


def find_problems(text: str, form: dict) -> list[tuple[str, list[str]]]:
    blob = allowed_numbers(form)
    known = evidence_known(form)
    out = []
    for sentence in sentences(text):
        if len(sentence.strip()) < MIN_SENTENCE:
            continue
        reasons = flag(sentence, blob, known)
        if reasons:
            out.append((sentence, reasons))
    return out


# ────────────────────────── 고쳐 쓰기 ──────────────────────────

async def rewrite(client: LLMClient, problems: list[tuple[str, list[str]]], form: dict) -> dict:
    """문제 문장들을 한 번에 고쳐 쓰게 한다 (모델 호출 1회). {원문: 고친 문장}"""
    listing = "\n".join(f"{i}. [{'·'.join(r)}] {s}" for i, (s, r) in enumerate(problems, 1))
    system = assemble.render("polish.md", count=len(problems))
    user = f"[지원자 입력]\n{assemble.build_context(form)[:1500]}\n\n[고쳐 쓸 문장]\n{listing}"
    try:
        res = await client.complete(system, user, model=form.get("model"))
    except Exception:
        return {}
    fixed = {}
    for line in (res.text or "").splitlines():
        m = re.match(r"\s*(\d+)[.)]\s*(.+)", line.strip())
        if not m:
            continue
        index = int(m.group(1)) - 1
        if 0 <= index < len(problems):
            fixed[problems[index][0]] = m.group(2).strip()
    return fixed


async def polish_text(client: LLMClient, text: str, form: dict) -> tuple[str, dict]:
    """후처리 전체. (고친 글, 무엇을 몇 건 고쳤는지)"""
    report = {"person": 0, "repeat": 0, "rewritten": 0, "dropped": 0, "left": 0,
              "self_intro": 0, "restated": 0, "duplicate": 0}
    text, report["self_intro"] = drop_self_intro(text, form.get("question_id"))
    text, report["restated"] = drop_idea_restatement(text, form)
    text, report["person"] = unify_person(text, form)
    text, report["repeat"] = drop_repeated_stats(text)
    text, report["duplicate"] = dedupe_sentences(text)

    problems = find_problems(text, form)
    if not problems:
        return text, report

    blob = allowed_numbers(form)
    known = evidence_known(form)
    fixed = await rewrite(client, problems, form)
    for sentence, _ in problems:
        candidate = fixed.get(sentence, "")
        if candidate and not flag(candidate, blob, known):
            text = text.replace(sentence, candidate)
            report["rewritten"] += 1
        elif has_foreign(sentence) or any(b in sentence for b in BOILERPLATE):
            # 고쳐 오지 못했고 눈에 띄는 흠(이물·상용구 수치)이 있으면 그 문장을 버린다.
            text = text.replace(sentence, "").strip()
            report["dropped"] += 1
        else:
            report["left"] += 1        # 판단이 애매한 것은 그대로 둔다 (지우면 글이 빈다)
    return re.sub(r"\s{2,}", " ", text).strip(), report
