"""verify.py — 필수 요소 판정. 모델은 가짜. 핵심: 모델이 present=true 라고 해도 evidence 가 글에 없으면 false 로 뒤집힌다."""
import json

import pytest

from backend.llm.client import LLMResult
from backend.pipeline import verify as V


class FakeClient:
    def __init__(self, reply):
        self.reply = reply
        self.calls = []

    async def complete(self, system, user, model=None):
        self.calls.append({"system": system, "user": user})
        return LLMResult(text=self.reply, model="fake")


TEXT = ("저는 식품회사 영업으로 일하며 저녁마다 반찬가게 진열대에 남은 반찬이 버려지는 것을 봤습니다. "
        "사장님들은 남은 반찬을 팔 방법이 없어 폐기하고 있었고, 저는 예약 꾸러미 앱을 떠올렸습니다.")


def test_parse_sections_groups_bullets_and_continuations():
    body = "[문항 의도]\n설명\n\n[필수 요소]\n- 첫 항목\n- 둘째 항목\n(부연 설명)\n\n[피할 것]\n- 나쁜 것"
    s = V.parse_sections(body)
    assert s["필수 요소"] == ["첫 항목", "둘째 항목 (부연 설명)"] and s["피할 것"] == ["나쁜 것"] and s["문항 의도"] == ["설명"]


@pytest.mark.parametrize("qid", ["q1", "q2", "q3_1", "q3_2", "q4_1", "q4_2", "q7_1", "q8", "q10"])
def test_every_question_has_required_items(qid):
    meta, reqs = V.required_items(qid)
    assert len(reqs) >= 2 and meta["id"] == qid


def test_local_format_issues():
    assert V.local_format_issues("a" * 101, 100) == ["글자 수 초과 (101/100)"]
    assert "마크다운 기호 사용" in V.local_format_issues("# 제목\n본문 **강조**", 2000)
    assert V.local_format_issues("깨끗한 글", 100) == []


@pytest.mark.asyncio
async def test_verify_text_flips_present_without_real_evidence():
    _, reqs = V.required_items("q2")
    reply = json.dumps({
        "items": [
            {"requirement": reqs[0], "present": True, "evidence": "저녁마다 반찬가게 진열대에 남은 반찬이 버려지는 것을 봤습니다"},
            {"requirement": reqs[1], "present": True, "evidence": "이 문장은 글에 없습니다 — 모델이 지어낸 근거"},
            {"requirement": reqs[2], "present": False, "evidence": None},
        ],
        "unsupported_claims": ["사장님들은 남은 반찬을 팔 방법이 없어 폐기하고 있었고", "글에 없는 주장"],
        "format_issues": [],
        "overall": "배경은 좋으나 계기가 약합니다.",
    }, ensure_ascii=False)
    client = FakeClient(reply)
    out = await V.verify_text(client, {"track": "tech", "idea": "반찬 앱"}, "q2", TEXT)

    assert out["items"][0]["present"] is True
    assert out["items"][1]["present"] is False and out["items"][1]["evidence"] is None   # 가짜 근거 → 뒤집힘
    assert out["missing"] == [reqs[1], reqs[2]]
    assert out["unsupported_claims"] == ["사장님들은 남은 반찬을 팔 방법이 없어 폐기하고 있었고"]  # 글에 있는 것만
    assert out["score"] == 33 and out["parse_ok"] is True and out["overall"].startswith("배경은")
    assert "[필수 요소]" in client.calls[0]["user"] and "[생성된 글]" in client.calls[0]["user"]


@pytest.mark.asyncio
async def test_verify_text_survives_garbage_model_output():
    out = await V.verify_text(FakeClient("이건 JSON 아님"), {"track": "tech", "idea": "x"}, "q1", "짧은 글")
    assert out["parse_ok"] is False and all(not it["present"] for it in out["items"]) and out["score"] == 0
    assert len(out["missing"]) == len(out["items"]) >= 2
