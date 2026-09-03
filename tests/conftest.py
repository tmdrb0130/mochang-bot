"""테스트 공통 설정."""
import os
import tempfile
from pathlib import Path

import pytest

# ① 계측 로그 격리 (작업 19) — 이걸 안 하면 pytest 가 backend/.timing.jsonl 에 가짜 job 수천 건을 남겨
#    운영 지표(대기 시간·소스 사용량)를 못 읽게 된다. backend.timing 을 import 하기 전에 경로를 바꾼다.
_TEST_TIMING = Path(tempfile.gettempdir()) / "mochang-test-timing.jsonl"
os.environ["MOCHANG_TIMING_LOG"] = str(_TEST_TIMING)
# ② 저장소 격리 — 테스트가 backend/.data/mochang.sqlite 에 가짜 초안을 쌓지 않게 임시 DB 를 쓴다 (backend.main 이 읽는다).
# ③ 본문 추출 프로세스 격리(backend/rag/extract_proc.py)는 끈다 — 테스트마다 프로세스를 띄우면 느리다. tests/test_extract_proc.py 만 켜서 본다.
os.environ.setdefault("MOCHANG_EXTRACT_ISOLATION", "0")
os.environ["MOCHANG_DATABASE_URL"] = "sqlite:///" + Path(tempfile.mkdtemp(prefix="mochang-test-db-")).joinpath("t.sqlite").as_posix()
# 백업 DB 도 임시로 — 안 그러면 config.yaml 의 backend/.data/mochang-backup.sqlite 에 가짜 초안이 쌓인다.
os.environ["MOCHANG_BACKUP_DATABASE_URL"] = "sqlite:///" + Path(tempfile.mkdtemp(prefix="mochang-test-bak-")).joinpath("b.sqlite").as_posix()

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
def _polish_off(request):
    """생성 후처리(작업 22)도 기본으로 꺼 둔다 — 켜져 있으면 문장에 흠이 있을 때 호출이 하나 더 붙는다.
    후처리를 보는 테스트(tests/test_polish.py)는 요청 단위로 직접 켠다."""
    if request.node.get_closest_marker("polish"):
        yield
        return
    saved = generate._polish_cfg
    generate._polish_cfg = {"enabled": False}
    yield
    generate._polish_cfg = saved


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


@pytest.fixture(autouse=True)
def _refine_off(request):
    """refine() 연결(작업 22·27 마무리)도 기본으로 꺼 둔다 — 켜져 있으면 가짜 응답의 수치가 '지어낸 것' 으로 지워지고
    하한 미달 신호로 이어쓰기 호출이 붙어 생성 자체를 보는 테스트가 흔들린다. tests/test_refine_hook.py 가 켜서 본다."""
    if request.node.get_closest_marker("refine"):
        yield
        return
    saved = generate._refine_cfg
    generate._refine_cfg = {"enabled": False}
    yield
    generate._refine_cfg = saved
