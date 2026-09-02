"""세션2 postprocess.refine() 을 generate_one / extend_one 끝에 연결한 것(작업 22·27 마무리). 모델 호출 없음.

확인하는 것:
  ① refine 이 지운 것(지어낸 수치·금지어)이 결과에 반영되고 보고서가 `refined` 필드로 나온다
  ② 긴 문항 한도 초과 → 자르지 않고 shorten.md 로 1회 압축, 그래도 넘으면 문단 끝에서만 자른다 (작업 27)
  ③ 하한 미달 → 이어쓰기 후 ⑦ 되풀이 제거, 재생성은 문항당 최대 2회
  ④ 이어쓴 글이 전부 되풀이면(extend_empty) 그 이어쓰기는 버린다
  ⑤ 짧은 문항의 흠(*_flagged)은 지우지 않고 짜임을 바꿔 1회 재생성, 흠이 줄었을 때만 받는다
  ⑥ 끄면 예전처럼 한도에서 자른다 (기존 동작 보존)
"""
import pytest

from backend.llm.client import LLMResult
from backend.pipeline import generate

pytestmark = pytest.mark.refine


class Client:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []                     # (system, user)

    async def complete(self, system, user, model=None):
        self.calls.append((system, user))
        return LLMResult(text=self.replies.pop(0) if self.replies else "", model="fake")


FORM = {"question_id": "q2", "track": "tech", "style": "logic", "idea": "냉장고 식재료 관리 앱",
        "team": "팀원 없음", "capability": "경험", "auto_extend": {"enabled": False}}


_WHO = ("혼자 사는 직장인은", "맞벌이 부부는", "소규모 농가는", "동네 세탁소 사장님은", "경력 단절 여성은",
        "독거 고령자는", "지역 소상공인은")
_DO = ("장을 본 뒤 남은 재료를 어디에 두었는지 잊어버려", "새벽에 물건을 옮길 사람을 구하지 못해",
       "수확한 물건을 제때 팔 곳을 찾지 못해", "아이를 잠깐 맡길 이웃을 찾지 못해", "약을 제때 챙겨 먹었는지 확인해 줄 사람이 없어",
       "손님이 몰리는 시간에 일손이 모자라", "믿을 만한 정보를 어디서 얻어야 할지 몰라")
_SO = ("같은 물건을 다시 사는 일이 잦다고 털어놓았습니다.", "결국 일을 포기하는 경우가 많다고 했습니다.",
       "매번 비싼 대안에 기대게 된다고 말했습니다.", "주말마다 같은 고민을 되풀이한다고 적었습니다.",
       "가족에게 부담을 떠넘기게 된다고 이야기했습니다.", "한 달에 며칠은 일을 쉬게 된다고 답했습니다.",
       "믿을 만한 곳을 소개받고 싶다고 덧붙였습니다.")


def sentence(i: int) -> str:
    """서로 다른 낱말 조합으로 만든 문장 (i < 49). 두 문장이 세 조각 중 둘 이상을 공유하지 않도록 짜서
    되풀이·재인용 판정(4-gram 유사도)에 걸리지 않게 한다."""
    assert 0 <= i < 49
    return f"{_WHO[i % 7]} {_DO[(i // 7) % 7]} {_SO[(i % 7 + i // 7) % 7]}"


def paragraphs(count: int, per: int = 5) -> str:
    return "\n\n".join(" ".join(sentence(p * per + s) for s in range(per)) for p in range(count))


def test_settings_from_config_and_override():
    assert generate.refine_settings()["enabled"] is True                       # config.yaml 기본값
    assert generate.refine_settings({"refine_settings": {"enabled": False}})["enabled"] is False


def test_ctx_gives_this_questions_reference_share_only():
    facts = [{"fact": "1인 가구 비율이 35%다", "angle": "problem", "quote": "q", "url": "u1", "source_title": "t"},
             {"fact": "경쟁 앱 A 가 월 9,900원을 받는다", "angle": "pricing", "quote": "q", "url": "u2", "source_title": "t"}]
    form = {**FORM, "references": facts, "team": "2명"}
    meta = {"id": "q2", "limit": 2000, "min": 1400}
    ctx = generate.refine_ctx(form, meta)
    assert ctx["question_id"] == "q2" and ctx["limit"] == 2000 and ctx["target_range"] == (1400, 2000)
    assert [f["angle"] for f in ctx["references"]] == ["problem"]              # pricing 사실은 Q3-2 몫
    assert ctx["team"] == "2명" and "current" not in ctx
    assert generate.refine_ctx(form, meta, current="기존 글")["current"] == "기존 글"


@pytest.mark.asyncio
async def test_refine_drops_invented_numbers_and_reports():
    """참고자료·입력에 없는 수치 문장은 refine 이 지우고, 보고서가 결과 필드로 나온다."""
    body = paragraphs(6)
    invented = "국내 1인 가구는 750만 가구로 전체의 35%에 이른다는 조사 결과가 있습니다."
    client = Client(body + "\n\n" + invented)
    out = await generate.generate_one(client, dict(FORM))
    assert invented not in out["text"]
    assert out["refined"]["invented"] == 1 and out["refined"]["regenerated"] == 0
    assert out["refined"]["length"] == len(out["text"])
    assert len(client.calls) == 1                                               # refine 은 모델을 부르지 않는다


@pytest.mark.asyncio
async def test_over_limit_long_question_is_compressed_not_cut():
    """긴 문항이 한도를 넘으면 자르기 전에 shorten.md 로 1회 압축한다 (작업 27)."""
    too_long = paragraphs(9)                                                    # 약 2,300자
    assert len(too_long) > 2000
    compressed = paragraphs(6)                                                  # 약 1,500자
    client = Client(too_long, compressed)
    out = await generate.generate_one(client, dict(FORM))
    assert out["text"] == compressed
    assert out["refined"]["compressed"] is True and out["refined"]["regenerated"] == 1
    assert "줄" in client.calls[1][0] or "압축" in client.calls[1][0]           # shorten.md 지시가 붙었다
    assert len(client.calls) == 2


@pytest.mark.asyncio
async def test_still_over_after_compress_cuts_at_paragraph_end():
    """압축해도 넘치면 문단 끝에서만 자른다 — 문장 중간·어절 자름·'…' 없음."""
    too_long = paragraphs(9)
    client = Client(too_long, too_long)                                         # 모델이 줄여 주지 않았다
    out = await generate.generate_one(client, dict(FORM))
    assert len(out["text"]) <= 2000
    assert out["text"].endswith(".") and "…" not in out["text"]
    assert out["text"] in too_long                                              # 앞부분을 문단 단위로 잘랐다
    assert out["text"] == "\n\n".join(too_long.split("\n\n")[:len(out["text"].split("\n\n"))])


def test_cut_at_paragraph_end_falls_back_to_sentence_end():
    single = " ".join(sentence(i) for i in range(49))                           # 문단 경계 없음
    cut = generate._cut_at_paragraph_end(single, 2000)
    assert len(cut) <= 2000 and cut.endswith(".")
    assert generate._cut_at_paragraph_end("짧은 글.", 2000) == "짧은 글."


@pytest.mark.asyncio
async def test_below_floor_extends_and_drops_overlap_then_stops_at_two():
    """하한(Q2 1,400자) 미달 → 이어쓰기. 이어 쓴 글이 기존 본문을 되풀이한 문장은 ⑦ 로 지운다. 재생성은 최대 2회."""
    first = paragraphs(3)                                                       # 약 750자 → 하한 미달
    overlap = first.split("\n\n")[0]                                            # 첫 문단을 그대로 되풀이
    fresh = " ".join(sentence(20 + i) for i in range(5))
    client = Client(first, overlap + "\n\n" + fresh, " ".join(sentence(30 + i) for i in range(5)))
    form = {**FORM, "auto_extend": {"enabled": True, "min_ratio": 0.7, "min_limit": 500}}
    out = await generate.generate_one(client, form)
    assert out["auto_extended"] is True
    assert out["text"].count(sentence(0)) == 1                                  # 되풀이 문단은 한 번만 남는다
    assert fresh.split(" ")[0] in out["text"]
    assert out["refined"]["extend_overlap"] >= 1
    assert out["refined"]["regenerated"] <= 2 and len(client.calls) <= 3        # 생성 1 + 이어쓰기 ≤ 2


@pytest.mark.asyncio
async def test_extend_that_only_repeats_is_discarded():
    """이어쓴 글이 전부 기존 본문의 되풀이면(extend_empty) 그 이어쓰기는 버리고 기존 글을 그대로 둔다."""
    current = paragraphs(3)
    client = Client(current.split("\n\n")[1])
    out = await generate.extend_one(client, dict(FORM), current)
    assert out["text"] == current and out["added"] == ""
    assert out["refined"]["extend_empty"] is True


@pytest.mark.asyncio
async def test_extend_one_keeps_fresh_text_and_reports():
    current = paragraphs(3)
    fresh = " ".join(sentence(40 + i) for i in range(4))
    client = Client(fresh)
    out = await generate.extend_one(client, dict(FORM), current)
    assert out["text"].startswith(current) and out["added"] == fresh
    assert out["refined"]["extend_overlap"] == 0 and out["refined"]["extend_empty"] is False


@pytest.mark.asyncio
async def test_short_question_flag_regenerates_once_and_keeps_better_one():
    """Q10 에 지어낸 수치가 있으면 지우지 않고(한 문장이 답이다) 짜임을 바꿔 1회 다시 쓴다. 흠이 줄면 그것을 받는다."""
    flawed = "혼자 장을 보다 남은 식재료를 버리던 분들 가운데 92%가 우리 앱으로 냉장고 속 재료의 유통기한을 챙기고 있습니다."
    clean = "혼자 장을 보다 남은 식재료를 버리던 분들이 냉장고 속 재료의 유통기한을 앱으로 챙기고 남은 재료로 저녁을 차립니다."
    assert 60 <= len(clean) <= 100
    client = Client(flawed, clean)
    out = await generate.generate_one(client, {**FORM, "question_id": "q10"})
    assert out["text"] == clean
    assert out["refined"]["regenerated"] == 1 and out["refined"]["invented_flagged"] == []
    assert len(client.calls) == 2
    assert client.calls[0] != client.calls[1]                                   # 짜임을 바꿔 다시 물었다


@pytest.mark.asyncio
async def test_short_question_keeps_original_when_retry_is_not_better():
    flawed = "혼자 장을 보다 남은 식재료를 버리던 분들 가운데 92%가 우리 앱으로 냉장고 속 재료의 유통기한을 챙기고 있습니다."
    worse = "이용자 1만 명 가운데 92%가 냉장고 속 재료의 유통기한을 우리 앱으로 챙기며 남은 재료로 저녁을 차립니다."
    client = Client(flawed, worse)
    out = await generate.generate_one(client, {**FORM, "question_id": "q10"})
    assert out["text"] == flawed and out["refined"]["regenerated"] == 1


@pytest.mark.asyncio
async def test_disabled_refine_cuts_at_limit_like_before():
    too_long = paragraphs(9)
    client = Client(too_long)
    out = await generate.generate_one(client, {**FORM, "refine_settings": {"enabled": False}})
    assert len(out["text"]) <= 2000 and out["refined"] == {}
    assert len(client.calls) == 1                                               # 압축 호출 없음


@pytest.mark.asyncio
async def test_response_fields_are_only_added():
    client = Client(paragraphs(6))
    out = await generate.generate_one(client, dict(FORM))
    for key in ("question_id", "style", "text", "length", "limit", "model",
                "retried", "auto_extended", "shortened", "outline_used", "polished", "refined"):
        assert key in out


@pytest.mark.asyncio
async def test_short_question_below_floor_gets_a_second_lengthen():
    """Q1 하한(70자) 미달 → _lengthen 1회, 그래도 미달이면 남은 재생성 예산으로 한 번 더 (실측 Q1|C 52자·Q1|F 43자)."""
    tiny = "소규모 농가의 수확물 보관을 공동 센터로 해결합니다."
    still = "소규모 농가의 수확물 보관과 택배 발송을 공동 저온 센터로 해결합니다."
    good = "소규모 농가가 수확한 물건을 공동 저온 센터에 맡기면 보관부터 선별·포장·택배 발송까지 한곳에서 대신 처리하고 판매까지 이어 줍니다."
    assert len(tiny) < 70 and len(still) < 70 and 70 <= len(good) <= 100
    client = Client(tiny, still, good)
    out = await generate.generate_one(client, {**FORM, "question_id": "q1"})
    assert out["text"] == good and len(client.calls) == 3
    assert out["refined"]["lengthened_again"] is True and out["refined"]["regenerated"] == 1
    assert out["refined"]["needs_extend"] is False
