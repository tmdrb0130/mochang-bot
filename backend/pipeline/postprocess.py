"""생성 후처리 — 라마가 못 지키는 것을 코드가 잡는다 (작업 22 + 27). 모델 호출 0.

    text, report = refine(text, ctx)

`ctx` 는 이 모듈이 읽는 것만 담으면 된다 (없는 키는 그냥 안 쓴다):

    question_id   "q2" 등. 짧은 문항(Q1·Q10)은 지우지 않고 재생성 신호만 준다
    limit         이 문항의 글자 한도. 넘어도 **자르지 않는다** (⑨ needs_compress)
    target_range  (하한, 상한). 하한 미만이면 ⑩ needs_extend
    idea          지원자가 쓴 아이디어 원문
    outline       골자 dict (solution·voice 를 본다)
    answers       인테이크 답변 [{slot,label,answer,unknown}]
    references    이 문항에 넣어 준 facts
    team          "팀원 없음" | "2명" …  (1인칭 통일 기준)
    other_texts   이미 확정된 다른 문항 본문 {qid: text} — 통계 재인용 판정에 쓴다
    current       이어쓰기(extend)일 때 **기존 본문**. 이때 text 는 이어붙일 부분만 넘긴다

규칙은 지시서(`docs/WORKORDER_QUALITY.md` 4절) 순서 그대로 돌고, 각각 report 에 건수를 남긴다:

    ① foreign          이물 문자 문장 — **지우지 않고 표시만** 한다 (호출자가 그 문장만 재생성)
    ② invented         입력·참고자료·골자에 없는 수치 문장 삭제
    ③ unit_economics   매출·원가 상용구 문장 삭제
    ④ person           1인칭 통일 (바꾼 횟수)
    ⑤ repeated_stats   같은 통계 재인용 삭제 (문항 안 + other_texts 대조)
    ⑥ restated_lead    아이디어 재진술 첫 문장 삭제
    ⑦ extend_overlap   이어쓰기가 기존 본문을 되풀이한 문장 삭제 / extend_empty 면 그 이어쓰기는 무효
    ⑧ banned           금지어 문장 삭제
    ⑨ needs_compress   한도 초과 — 자르지 않고 신호만 (호출자가 shorten.md 로 1회 압축)
    ⑩ needs_extend     하한 미달 — 신호만 (호출자가 1회 이어쓰기 → ⑦ 재적용)

②③⑧ 은 **글이 통째로 비게 되는 경우 삭제를 취소하고** `*_flagged` 목록으로 넘긴다.
짧은 문항(Q1·Q10)도 지우지 않고 `*_flagged` 로만 준다 — 한 문장이 곧 답이라 지우면 남는 게 없다.

**순수 함수다.** 네트워크·모델·파일을 건드리지 않고 같은 입력에 늘 같은 결과를 낸다.
`polish.py` 와 규칙이 겹치지만 그쪽은 모델을 부르는 경로라 일부러 합치지 않았다 —
연결(`generate_one`/`extend_one` 끝에 한 줄)과 정리는 세션3 몫이다.
"""
from __future__ import annotations

import json
import re

# ────────────────────────── 문장·문단 ──────────────────────────

# 문단은 살린다. 한도 초과분을 "문단 끝에서만" 자르려면 문단 경계가 남아 있어야 한다 (작업 27).
_PARAGRAPH_SPLIT = re.compile(r"\n\s*\n+")
_SENTENCE_SPLIT = re.compile(r"(?<=[.다!?])\s+")
# 이보다 짧은 조각은 문장으로 안 보고 규칙 판정에서 건너뛴다 (제목·군더더기).
MIN_SENTENCE = 8


def paragraphs(text: str) -> list[list[str]]:
    """글 → 문단 목록 → 문단마다 문장 목록."""
    out = []
    for block in _PARAGRAPH_SPLIT.split(text or ""):
        parts = [s.strip() for s in _SENTENCE_SPLIT.split(block) if s.strip()]
        if parts:
            out.append(parts)
    return out


def join(blocks: list[list[str]]) -> str:
    return "\n\n".join(" ".join(p) for p in blocks if p).strip()


def sentences(text: str) -> list[str]:
    return [s for block in paragraphs(text) for s in block]


def _drop(text: str, should_drop) -> tuple[str, list[str], bool]:
    """should_drop(문장) 이 참인 문장을 지운다. → (남은 글, 걸린 문장들, 취소했는지)

    전부 지워지게 되면 **아무것도 지우지 않고 취소한다** — 글을 통째로 비우는 것보다
    흠 있는 문장이 남는 편이 낫다. 이때 걸린 문장은 report 의 *_flagged 로 나가고,
    호출자가 그 문항을 다시 생성한다."""
    blocks, dropped = [], []
    for block in paragraphs(text):
        kept = []
        for s in block:
            if len(s) >= MIN_SENTENCE and should_drop(s):
                dropped.append(s)
            else:
                kept.append(s)
        if kept:
            blocks.append(kept)
    if not dropped:
        return text, [], False
    if not blocks:
        return text, dropped, True
    return join(blocks), dropped, False


# ────────────────────────── 유사도 ──────────────────────────

def grams(text: str, n: int = 4) -> set:
    packed = re.sub(r"[\s\W_]", "", text or "")
    return {packed[i:i + n] for i in range(max(0, len(packed) - n + 1))}


def similarity(a: str, b: str) -> float:
    """4-gram Jaccard. 0~1."""
    ga, gb = grams(a), grams(b)
    return len(ga & gb) / len(ga | gb) if (ga or gb) else 0.0


# ────────────────────────── ① 이물 문자 ──────────────────────────

def _is_korean(ch: str) -> bool:
    return "가" <= ch <= "힣" or "ᄀ" <= ch <= "ᇿ" or "ㄱ" <= ch <= "ㆎ"


def has_foreign(text: str) -> bool:
    """한글도 ASCII 도 아닌 '글자'. 한자·가나뿐 아니라 태국어·베트남어도 실제로 섞여 나왔다
    (실측: 生活·需求·まず·หมด·nhu cầu). 문자표를 나열하면 새 언어가 나올 때마다 놓친다."""
    return any(ch.isalpha() and not ch.isascii() and not _is_korean(ch) for ch in text or "")


def foreign_sentences(text: str) -> list[str]:
    return [s for s in sentences(text) if has_foreign(s)]


# ────────────────────────── ② 지어낸 수치 ──────────────────────────

# 통계로 셀 수치 — 단위가 붙은 것만 본다 ('3개월·주 1회' 같은 일반 수량은 오탐이 많다).
# 만·억 같은 자릿수 말이 사이에 끼는 형태('5만 명')를 놓치지 않도록 두 갈래로 적는다.
_MAGNITUDE = r"(?:억|조|천만|만|천)"
_UNIT = r"(?:%|퍼센트|원|배|명|가구|건|곳)"
STAT = re.compile(rf"\d[\d,.]*\s*{_MAGNITUDE}\s*{_UNIT}?|\d[\d,.]*\s*{_UNIT}")
# 하지 않은 조사를 한 것처럼 쓰는 표현. '인터뷰할 계획' 같은 미래형을 지우지 않도록 완료 표시를 함께 요구한다.
SURVEY_DONE = re.compile(r"(인터뷰|설문|여쭤|물어보|물어봤|응답자|만나 ?보)[^.]{0,24}?"
                         r"(했|한 결과|해 ?보니|였|이었|나타났|확인|들었|답)")
_EVIDENCE_SLOTS = ("evidence", "근거")


def _blob(ctx: dict) -> str:
    """지어냄 판정의 기준 — 여기 없는 수치는 모델이 만들어낸 것으로 본다.

    지시서가 든 네 가지(idea·answers·references·outline)에 지원자가 직접 쓴 입력
    (capability·current_item·team)을 더한다. 지원자가 적어 넣은 숫자를 지우면 안 되기 때문이다."""
    keep = {k: v for k, v in (ctx or {}).items()
            if k in ("idea", "answers", "references", "outline", "capability", "current_item", "team")}
    return json.dumps(keep, ensure_ascii=False)


def _digits(s: str) -> str:
    return re.sub(r"[^\d]", "", s or "")


_NUMBER = re.compile(rf"\d[\d,.]*\s*{_MAGNITUDE}?")


def _num_key(raw: str) -> str:
    """수치의 지문 — 자릿수 + 자릿수 말. '5만' 과 '5' 를 다른 수로 본다.

    자릿수 말을 버리면 입력 어딘가의 '5' 때문에 지어낸 '5만 명' 이 통과해 버린다."""
    digits = _digits(raw)
    if not digits:
        return ""
    mag = re.search(_MAGNITUDE, raw or "")
    return digits + (mag.group(0) if mag else "")


def known_numbers(blob: str) -> set:
    """입력·참고자료·골자에 실제로 적힌 수.

    '자릿수를 이어 붙인 한 덩어리에서 부분 문자열로 찾기' 는 쓰지 않는다 —
    참고자료에 130 이 있으면 모델이 지어낸 30 도 통과해 버린다."""
    return {k for k in (_num_key(n) for n in _NUMBER.findall(blob or "")) if k}


def unsupported_numbers(sentence: str, blob: str) -> list[str]:
    """문장 속 수치 중 입력·참고자료·골자 어디에도 없는 것."""
    known = known_numbers(blob)
    return [raw.strip() for raw in STAT.findall(sentence)
            if _num_key(raw) and _num_key(raw) not in known]


def has_own_evidence(ctx: dict) -> bool:
    """지원자가 '직접 확인한 근거' 를 실제로 냈는지 (인테이크 답변 기준)."""
    for answer in ctx.get("answers") or []:
        if not isinstance(answer, dict):
            continue
        if str(answer.get("slot", "")) in _EVIDENCE_SLOTS:
            return bool(answer.get("answer")) and not answer.get("unknown")
    return False


def is_invented(sentence: str, blob: str, own_evidence: bool) -> bool:
    """근거 없는 수치가 있거나, 하지 않은 조사를 한 것처럼 쓴 문장.

    '가정하면' 이라고 적어 두어도 지운다 — 심사자에게는 지어낸 숫자로 보인다 (지시서 ②)."""
    if unsupported_numbers(sentence, blob):
        return True
    return bool(SURVEY_DONE.search(sentence)) and not own_evidence


# ────────────────────────── ③ 매출·원가 상용구 ──────────────────────────

MONEY = re.compile(r"\d[\d,.]*\s*(?:억|조|천만|만\s*원|원)")
# 단위경제(매출·원가) 이야기임을 알리는 말
UNIT_ECONOMICS = re.compile(r"(객단가|단가|건당|월\s*이용료|이용료|구독료|수수료|마진|영업이익|매출|원가|비용|정산)")
# 아이디어가 달라도 같은 값이 나오는 라마 상용구 (실측 2건에서 동일). 문맥과 무관하게 지운다.
BOILERPLATE = ("500명", "30명", "1억", "1,000만 원", "1000만 원", "5,000만 원", "5000만 원",
               "1만 원", "5천 원", "60%", "20%", "10%")


def is_unit_economics(sentence: str) -> bool:
    """매출·원가를 숫자로 늘어놓는 문장인지.

    실측 5개 파일에서 11문장이 걸리는데, 그 수치들이 입력 어디에도 없어 **②가 먼저 지운다** —
    그래서 회귀 테스트의 unit_economics 는 0으로 나온다. ②가 못 잡는(입력에 있는 숫자로 쓴)
    단위경제 문장을 위해 남겨 둔 규칙이다."""
    if MONEY.search(sentence) and UNIT_ECONOMICS.search(sentence):
        return True
    return any(b in sentence for b in BOILERPLATE) and bool(MONEY.search(sentence))


# ────────────────────────── ④ 1인칭 통일 ──────────────────────────

_TO_SOLO = [("저희는", "저는"), ("저희가", "제가"), ("저희의", "저의"), ("저희도", "저도"),
            ("저희를", "저를"), ("저희에게", "저에게"), ("저희와", "저와"), ("저희 팀은", "저는"),
            ("저희 팀이", "제가"), ("저희 팀", "저"), ("저희", "저")]
_TO_TEAM = [("저는", "저희는"), ("제가", "저희가"), ("저의", "저희의"), ("저도", "저희도"),
            ("저를", "저희를"), ("저에게", "저희에게"), ("저와", "저희와")]


def solo_applicant(ctx: dict) -> bool:
    """혼자 신청하는 경우인지. 골자의 voice 가 있으면 그걸 따르고, 없으면 팀원 수를 본다."""
    voice = str((ctx.get("outline") or {}).get("voice") or "").strip() if isinstance(ctx.get("outline"), dict) else ""
    if voice in ("저", "저희"):
        return voice == "저"
    team = str(ctx.get("team") or "").strip()
    return (not team) or ("없" in team) or team in ("0", "0명")


def unify_person(text: str, ctx: dict) -> tuple[str, int]:
    """1인칭을 한쪽으로 통일한다. (고친 글, 바꾼 횟수)

    '저장·저녁' 처럼 저로 시작하는 낱말은 조사까지 붙은 형태만 바꾸므로 건드리지 않는다."""
    rules = _TO_SOLO if solo_applicant(ctx) else _TO_TEAM
    changed = 0
    for old, new in rules:
        if old in text:
            changed += text.count(old)
            text = text.replace(old, new)
    return text, changed


# ────────────────────────── ⑤ 통계 재인용 ──────────────────────────

def stat_keys(sentence: str) -> set:
    """이 문장이 인용한 통계의 지문. 같은 수치가 다시 나오면 재인용으로 본다."""
    return {k for k in (_num_key(s) for s in STAT.findall(sentence)) if k}


def drop_repeated_stats(text: str, other_texts: dict | None) -> tuple[str, int, list[str]]:
    """이미 인용한 통계를 다시 인용하는 문장을 지운다.

    같은 문항 안에서 두 번째부터, 그리고 **다른 문항에 이미 있는 통계**면 이 문항에서 지운다
    (심사자는 한 신청서를 통으로 읽는다 — 같은 숫자가 문항마다 나오면 분량 채우기로 보인다)."""
    seen = set()
    for other in (other_texts or {}).values():
        for s in sentences(str(other or "")):
            seen |= stat_keys(s)
    kept_blocks, dropped = [], []
    for block in paragraphs(text):
        kept = []
        for s in block:
            keys = stat_keys(s)
            if keys and len(s) >= MIN_SENTENCE and keys <= seen:
                dropped.append(s)
                continue
            seen |= keys
            kept.append(s)
        if kept:
            kept_blocks.append(kept)
    if not dropped or not kept_blocks:
        return text, 0, []
    return join(kept_blocks), len(dropped), dropped


# ────────────────────────── ⑥ 아이디어 재진술 첫 문장 ──────────────────────────

# Q1 은 아이디어 한 줄 소개가 곧 답이고, Q3-1 은 해결책 설명이 본론이다 — 여기선 지우지 않는다.
KEEP_RESTATEMENT = ("q1", "q3_1", "q10")
RESTATE_THRESHOLD = 0.5


def drop_restated_lead(text: str, ctx: dict) -> tuple[str, int]:
    """첫 문장이 아이디어(또는 골자의 해결책) 재진술이면 지운다.

    실측에서 문항 간 유사 문장 쌍이 전부 이 유형이었다 — 문항마다 '저는 … 서비스를 만들고자 합니다' 로 시작한다."""
    if str(ctx.get("question_id") or "") in KEEP_RESTATEMENT:
        return text, 0
    blocks = paragraphs(text)
    if not blocks or sum(len(b) for b in blocks) < 3:
        return text, 0                      # 문장이 두 개뿐이면 지우면 글이 남지 않는다
    outline = ctx.get("outline") if isinstance(ctx.get("outline"), dict) else {}
    seeds = [str(ctx.get("idea") or ""), str(outline.get("solution") or "")]
    lead = blocks[0][0]
    if not any(seed and similarity(lead, seed) >= RESTATE_THRESHOLD for seed in seeds):
        return text, 0
    blocks[0] = blocks[0][1:]
    return join(blocks), 1


# ────────────────────────── ⑦ 이어쓰기 중복 ──────────────────────────

OVERLAP_THRESHOLD = 0.5


def drop_extend_overlap(text: str, previous: str) -> tuple[str, int]:
    """이어쓴 부분에서 기존 본문과 사실상 같은 문장을 지운다.

    남는 문장이 하나도 없으면 빈 글을 돌려준다 — 그 이어쓰기는 무효이므로 호출자가 버려야 한다.
    (다른 규칙과 달리 여기서는 '전부 지워짐' 이 오류가 아니라 결론이다.)"""
    old = sentences(previous)
    if not old:
        return text, 0
    blocks, dropped = [], 0
    for block in paragraphs(text):
        kept = []
        for s in block:
            if len(s) >= MIN_SENTENCE and any(similarity(s, o) >= OVERLAP_THRESHOLD for o in old):
                dropped += 1
            else:
                kept.append(s)
        if kept:
            blocks.append(kept)
    if not dropped:
        return text, 0
    return join(blocks), dropped


# ────────────────────────── ⑧ 금지어 ──────────────────────────

# 어느 아이디어에나 붙어 아무것도 설명하지 못하는 말. 심사에서 감점 요인이고, 지우면 글이 오히려 또렷해진다.
BANNED = ("데이터 판매", "고유한 가치", "혁신적", "획기적", "차별화된 경쟁력", "큰 도움이 될 것")
# "AI 기반" 이 뒤에 무엇이 오는지 말하지 않고 홀로 쓰인 경우만 잡는다.
# ("AI 기반 매칭 알고리즘" 은 남기고, "AI 기반으로 해결합니다" 는 지운다.)
AI_ALONE = re.compile(r"AI\s*기반(?!\s*[가-힣A-Za-z]{2,})|AI\s*기반\s*(?:으로|이|가|을|를|은|는|의|입니다|이다|이며|이라는)")


def has_banned(sentence: str) -> bool:
    return any(b in sentence for b in BANNED) or bool(AI_ALONE.search(sentence))


# ────────────────────────── ⑨⑩ 분량 ──────────────────────────

# 이 길이 이하의 문항(Q1·Q10)은 문장을 지우면 답이 사라진다 — 지우는 대신 재생성 신호를 준다.
SHORT_LIMIT = 200


def is_short_question(ctx: dict) -> bool:
    limit = ctx.get("limit")
    return bool(limit) and int(limit) <= SHORT_LIMIT


def _target_range(ctx: dict) -> tuple[int, int] | None:
    tr = ctx.get("target_range")
    if isinstance(tr, (list, tuple)) and len(tr) >= 2:
        return int(tr[0]), int(tr[1])
    return None


# ────────────────────────── 본체 ──────────────────────────

def new_report() -> dict:
    return {
        "foreign": [], "invented": 0, "invented_flagged": [],
        "unit_economics": 0, "unit_economics_flagged": [],
        "person": 0, "repeated_stats": 0, "restated_lead": 0,
        "extend_overlap": 0, "extend_empty": False, "banned": 0, "banned_flagged": [],
        "needs_compress": False, "needs_extend": False,
        "dropped": 0, "length": 0, "limit": None, "changed": False,
    }


def refine(text: str, ctx: dict) -> tuple[str, dict]:
    """지시서 4절의 ①~⑩ 을 순서대로 적용한다. 모델을 부르지 않는다.

    돌려주는 report 의 키 뜻은 이 파일 맨 위 표에 있다. 호출자가 볼 것:
      - report["foreign"] / ["invented_flagged"] / ["banned_flagged"] → 그 문장만 재생성(각 1회)
      - report["extend_empty"] → 이번 이어쓰기는 버린다
      - report["needs_compress"] → shorten.md 로 1회 압축 (여기서 자르지 않았다)
      - report["needs_extend"] → 1회 이어쓰기 후 refine 재적용
    """
    ctx = ctx or {}
    original = text or ""
    text = original
    report = new_report()
    report["limit"] = ctx.get("limit")
    short = is_short_question(ctx)

    # ① 이물 문자 — 표시만. 지우면 문항이 통째로 비는 경우가 실측에 있었다.
    report["foreign"] = foreign_sentences(text)

    # ② 지어낸 수치
    blob = _blob(ctx)
    own_evidence = has_own_evidence(ctx)
    invented_pred = lambda s: is_invented(s, blob, own_evidence)          # noqa: E731 — 규칙 순서를 표로 읽히게
    if short:
        report["invented_flagged"] = [s for s in sentences(text)
                                      if len(s) >= MIN_SENTENCE and invented_pred(s)]
    else:
        text, gone, cancelled = _drop(text, invented_pred)
        report["invented_flagged" if cancelled else "invented"] = gone if cancelled else len(gone)

    # ③ 매출·원가 상용구
    if not short:
        text, gone, cancelled = _drop(text, is_unit_economics)
        report["unit_economics_flagged" if cancelled else "unit_economics"] = gone if cancelled else len(gone)

    # ④ 1인칭 통일
    text, report["person"] = unify_person(text, ctx)

    # ⑤ 같은 통계 재인용
    if not short:
        text, report["repeated_stats"], _ = drop_repeated_stats(text, ctx.get("other_texts"))

    # ⑥ 아이디어 재진술 첫 문장
    if not short:
        text, report["restated_lead"] = drop_restated_lead(text, ctx)

    # ⑦ 이어쓰기 중복 (기존 본문을 받았을 때만)
    previous = str(ctx.get("current") or ctx.get("previous") or "")
    if previous:
        text, report["extend_overlap"] = drop_extend_overlap(text, previous)
        report["extend_empty"] = not text.strip()

    # ⑧ 금지어
    if short:
        report["banned_flagged"] = [s for s in sentences(text) if len(s) >= MIN_SENTENCE and has_banned(s)]
    else:
        text, gone, cancelled = _drop(text, has_banned)
        report["banned_flagged" if cancelled else "banned"] = gone if cancelled else len(gone)

    text = re.sub(r"[ \t]{2,}", " ", text).strip()

    # ⑨ 한도 초과 — 자르지 않는다
    report["length"] = len(text)
    limit = ctx.get("limit")
    report["needs_compress"] = bool(limit) and len(text) > int(limit)

    # ⑩ 하한 미달
    target = _target_range(ctx)
    report["needs_extend"] = bool(target) and 0 < len(text) < target[0]

    report["dropped"] = report["invented"] + report["unit_economics"] + report["repeated_stats"] \
        + report["restated_lead"] + report["extend_overlap"] + report["banned"]
    report["changed"] = text != original
    return text, report
