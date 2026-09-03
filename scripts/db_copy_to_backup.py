"""서비스 DB 를 백업 DB 로 통째로 복사한다 (2026-09-03, 백업 DB 를 처음 만들 때 한 번).

    .venv/Scripts/python scripts/db_copy_to_backup.py            # 미리보기 (경로·건수만)
    .venv/Scripts/python scripts/db_copy_to_backup.py --yes      # 복사 (백업 파일이 이미 있으면 덮어쓴다)

이후로는 backend/storage.py 가 모든 쓰기를 백업에 미러링하므로 다시 돌릴 일이 없다.
SQLite 끼리만 지원 (sqlite3 backup API — 서비스가 도는 중에도 일관된 스냅샷).
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import storage as S  # noqa: E402
from backend.llm.client import load_config  # noqa: E402


def _path(url: str) -> Path:
    assert url.startswith("sqlite:///"), f"SQLite 만 지원: {url}"
    p = Path(url[len("sqlite:///"):])
    return p if p.is_absolute() else S.ROOT / p


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--yes", action="store_true")
    args = ap.parse_args()
    st = S.Storage.from_config(load_config())
    if st.backup is None:
        print("config.yaml storage.backup_url 이 없습니다."); return 1
    src, dst = _path(st.url), _path(st.backup.url)
    with sqlite3.connect(src) as c:
        n = c.execute("select count(*) from drafts").fetchone()[0]
    print(f"서비스 DB {src} (초안 {n}건) → 백업 DB {dst}{' (있음, 덮어씀)' if dst.exists() else ''}")
    if not args.yes:
        print("복사하려면 --yes"); return 0
    dst.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(src) as a, sqlite3.connect(dst) as b:
        a.backup(b)
    with sqlite3.connect(dst) as c:
        print("백업 초안", c.execute("select count(*) from drafts").fetchone()[0], "건, 생성", c.execute("select count(*) from generations").fetchone()[0], "건")
    return 0


if __name__ == "__main__":
    sys.exit(main())
