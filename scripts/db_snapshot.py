"""서비스 DB 스냅샷 → 날짜 파일(.sqlite.gz) → (선택) 다른 머신으로 복사 → 오래된 스냅샷 정리 (2026-09-04, 오프사이트 백업).

    .venv/Scripts/python scripts/db_snapshot.py                          # 스냅샷만 (backend/.data/snapshots/mochang-YYYY-MM-DD.sqlite.gz)
    .venv/Scripts/python scripts/db_snapshot.py --remote gpu:mochang-backup   # + scp 로 GPU 서버 홈에 복사 (SSH 설정 'gpu' 재사용)
    .venv/Scripts/python scripts/db_snapshot.py --keep-days 14 --dir D:/backup     # 로컬 보관 일수·폴더 변경
    .venv/Scripts/python scripts/db_snapshot.py --remote gpu:mochang-backup --remote-keep-days 60
    .venv/Scripts/python scripts/db_snapshot.py --dry-run                # 무엇을 할지만 출력

왜: 지금까지의 "백업 DB"(mochang-backup.sqlite)는 같은 PC 의 같은 폴더에 있어 디스크·PC 장애에는 백업이 아니었다.
이 스크립트는 sqlite3 backup API 로 **서비스가 도는 중에도** 일관된 스냅샷을 만들고(WAL 모드라 무중단),
다른 머신에 사본을 두는 것으로 5번 문제(오프사이트 부재)를 끝낸다. 모델·네트워크(scp 제외) 호출 없음.

매일 새벽 자동 실행 (Windows 작업 스케줄러 — 관리자 PowerShell, 이 스크립트는 등록하지 않는다):
    schtasks /Create /TN "mochang-db-snapshot" /SC DAILY /ST 04:30 /RU "%USERNAME%" ^
      /TR "C:\\Users\\bon505\\Desktop\\mochang-bot\\.venv\\Scripts\\python.exe C:\\Users\\bon505\\Desktop\\mochang-bot\\scripts\\db_snapshot.py --remote gpu:/opt/mochang-backup"
  실제 등록(2026-09-04)은 PowerShell `Register-ScheduledTask` 로 했다 — 작업 이름 `mochang-db-snapshot`, 매일 04:30,
  대상 `gpu:mochang-backup`(= GPU 서버 홈. /opt 는 root 권한이 필요해 공유 서버에 시스템 변경을 남기지 않으려고 홈으로).
  `Get-ScheduledTaskInfo mochang-db-snapshot` 의 LastTaskResult 0 이면 성공. 결과는 backend/.timing.jsonl 에 event=db_snapshot 으로도 남는다.
"""
from __future__ import annotations

import argparse
import gzip
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

# 예약 작업(schtasks)으로 무인 실행되므로 콘솔 인코딩(cp949)에서 죽지 않게 — drafts_delete.py 와 같은 처방.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import storage as S  # noqa: E402
from backend import timing  # noqa: E402
from backend.llm.client import load_config  # noqa: E402

DEFAULT_DIR = S.ROOT / "backend" / ".data" / "snapshots"
PREFIX = "mochang-"


def _path(url: str) -> Path:
    assert url.startswith("sqlite:///"), f"SQLite 만 지원: {url}"
    p = Path(url[len("sqlite:///"):])
    return p if p.is_absolute() else S.ROOT / p


def snapshot(src: Path, out_dir: Path, stamp: str | None = None) -> Path:
    """sqlite3 backup API 로 일관된 사본을 만들고 gzip 으로 묶는다. → .sqlite.gz 경로."""
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = stamp or datetime.now().strftime("%Y-%m-%d")
    raw = out_dir / f"{PREFIX}{stamp}.sqlite"
    gz = out_dir / f"{PREFIX}{stamp}.sqlite.gz"
    a, b = sqlite3.connect(src), sqlite3.connect(raw)
    try:
        a.backup(b)
    finally:
        a.close()
        b.close()                      # Windows 는 열린 파일을 지우지 못한다 — with 문은 커밋만 하고 닫지 않는다
    with open(raw, "rb") as f_in, gzip.open(gz, "wb", compresslevel=6) as f_out:
        shutil.copyfileobj(f_in, f_out)
    raw.unlink()
    return gz


def prune(out_dir: Path, keep_days: int, now: datetime | None = None) -> list[Path]:
    """keep_days 보다 오래된 스냅샷을 지운다. 파일명의 날짜를 기준으로 한다 (수정 시각은 복사·복원으로 바뀔 수 있다)."""
    now = now or datetime.now()
    cutoff = now - timedelta(days=keep_days)
    gone = []
    for p in sorted(out_dir.glob(f"{PREFIX}*.sqlite.gz")):
        try:
            d = datetime.strptime(p.name[len(PREFIX):len(PREFIX) + 10], "%Y-%m-%d")
        except ValueError:
            continue
        if d < cutoff:
            p.unlink()
            gone.append(p)
    return gone


def split_remote(remote: str) -> tuple[str, str]:
    """'gpu:mochang-backup' → ('gpu', 'mochang-backup'). 경로가 없으면 홈('.')."""
    host, _, path = remote.partition(":")
    return host, (path.rstrip("/") or ".")


def _sh_quote(path: str) -> str:
    """원격 셸에 넘길 경로를 작은따옴표로 감싼다 (경로에 공백이 있어도 안전)."""
    return "'" + path.replace("'", "'\\''") + "'"


def copy_remote(gz: Path, remote: str, timeout: int = 300, keep_days: int = 30) -> bool:
    """scp 로 다른 머신에 복사한 뒤, 원격에서 권한을 조이고 오래된 스냅샷을 지운다.

    remote 는 'host:dir' (ssh config 별칭 사용 가능, 경로는 홈 기준 상대경로도 됨).
    권한: 스냅샷에는 학생들의 아이디어·경력이 그대로 들어 있다. 공유 GPU 서버이므로
          디렉터리 700 / 파일 600 으로 조여 같은 서버의 다른 사용자가 읽지 못하게 한다(2026-09-04).
    보관: 로컬(keep_days, 기본 7일)과 별개로 원격은 더 길게 둔다 — 오프사이트 사본이 본체다.
          0 이면 원격 정리를 하지 않는다(무한 누적 주의)."""
    host, rdir = split_remote(remote)
    cmd = ["scp", "-q", "-o", "BatchMode=yes", str(gz), f"{host}:{rdir}/{gz.name}"]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        print(f"원격 복사 실패: {' '.join(cmd)}\n{r.stderr.strip()}")
        return False
    d = _sh_quote(rdir)
    steps = [f"chmod 700 {d}", f"chmod 600 {d}/{PREFIX}*.sqlite.gz 2>/dev/null || true"]
    if keep_days and int(keep_days) > 0:
        # 이름을 한정해 지운다 — 다른 파일은 건드리지 않는다
        steps.append(f"find {d} -maxdepth 1 -name '{PREFIX}*.sqlite.gz' -mtime +{int(keep_days)} -delete")
    steps.append(f"ls -1 {d}/{PREFIX}*.sqlite.gz 2>/dev/null | wc -l")
    r2 = subprocess.run(["ssh", "-o", "BatchMode=yes", host, "; ".join(steps)],
                        capture_output=True, text=True, timeout=timeout)
    if r2.returncode != 0:
        print(f"원격 정리 실패(복사는 됨): {r2.stderr.strip()[:200]}")
        return True                      # 사본은 도착했다 — 정리 실패로 백업을 실패로 보지 않는다
    kept = (r2.stdout or "").strip().splitlines()
    if kept:
        print(f"원격 보관 {kept[-1]}개 (권한 700/600, {keep_days}일 초과분 정리)")
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", default=str(DEFAULT_DIR), help="스냅샷을 둘 폴더")
    ap.add_argument("--keep-days", type=int, default=7, help="로컬 스냅샷 보관 일수")
    ap.add_argument("--remote", default="", help="scp 대상 'host:dir' (예: gpu:mochang-backup). 비우면 로컬만")
    ap.add_argument("--remote-keep-days", type=int, default=30,
                    help="원격 스냅샷 보관 일수 (0 이면 정리 안 함). 오프사이트 사본이 본체라 로컬보다 길게 둔다")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    st = S.Storage.from_config(load_config())
    src = _path(st.url)
    out_dir = Path(args.dir)
    if not src.exists():
        print(f"서비스 DB 가 없습니다: {src}")
        return 1
    with sqlite3.connect(src) as c:
        n = c.execute("select count(*) from drafts").fetchone()[0]
    print(f"서비스 DB {src} (초안 {n}건) → {out_dir}/{PREFIX}{datetime.now():%Y-%m-%d}.sqlite.gz"
          + (f" → {args.remote}" if args.remote else "") + f" (보관 {args.keep_days}일)")
    if args.dry_run:
        return 0

    t0 = time.monotonic()
    gz = snapshot(src, out_dir)
    size = gz.stat().st_size
    ok_remote = copy_remote(gz, args.remote, keep_days=args.remote_keep_days) if args.remote else None
    gone = prune(out_dir, args.keep_days)
    took = round(time.monotonic() - t0, 1)
    print(f"스냅샷 {gz.name} ({size // 1024} KB, {took}s), 원격 복사 {'성공' if ok_remote else ('건너뜀' if ok_remote is None else '실패')}, "
          f"정리 {len(gone)}개")
    timing.log("db_snapshot", file=gz.name, bytes=size, drafts=n, remote=args.remote or None,
               remote_ok=ok_remote, pruned=len(gone), remote_keep_days=args.remote_keep_days, took_s=took)
    return 0 if ok_remote in (True, None) else 2


if __name__ == "__main__":
    sys.exit(main())
