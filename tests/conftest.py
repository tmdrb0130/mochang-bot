"""테스트 공통 설정."""
import os
import tempfile
from pathlib import Path

import pytest

# ① 계측 로그 격리 (작업 19) — 이걸 안 하면 pytest 가 backend/.timing.jsonl 에 가짜 job 수천 건을 남겨
#    운영 지표(대기 시간·소스 사용량)를 못 읽게 된다. backend.timing 을 import 하기 전에 경로를 바꾼다.
_TEST_TIMING = Path(tempfile.gettempdir()) / "mochang-test-timing.jsonl"
os.environ["MOCHANG_TIMING_LOG"] = str(_TEST_TIMING)

from backend import timing            # noqa: E402  (환경변수를 먼저 세팅해야 한다)
from backend.pipeline import generate  # noqa: E402

timing.PATH = _TEST_TIMING            # 이미 import 돼 있었을 경우까지 확실히


@pytest.fixture(autouse=True)
def _isolate_timing():
    """테스트가 남긴 계측 줄과 소스 카운터가 다음 테스트·운영 로그로 새지 않게."""
    timing._counts.clear()
    yield
    timing._counts.clear()


@pytest.fixture(autouse=True)
def _outline_off(request):
    """사업계획 골자(작업 20)는 기본으로 꺼 둔다.

    켜져 있으면 generate_one 이 문항 생성 전에 골자 생성 호출을 한 번 더 한다 —
    문항 생성 자체를 보는 테스트들이 가짜 응답을 하나씩 더 소비하게 된다.
    골자를 보는 테스트(tests/test_outline.py)는 @pytest.mark.outline 로 이 기본값을 끈다.
    """
    if request.node.get_closest_marker("outline"):
        yield
        return
    saved = generate._outline_cfg
    generate._outline_cfg = {"enabled": False}
    yield
    generate._outline_cfg = saved
