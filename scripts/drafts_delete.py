"""서비스 DB 에서 테스트 초안을 지운다 (2026-09-03). 백업 DB 는 건드리지 않는다 — 백업은 전부 남기는 곳이다.

    .venv/Scripts/python scripts/drafts_delete.py --list                       # 초안 목록 (id 앞 8자, IP, 생성 수, 아이디어 앞부분)
    .venv/Scripts/python scripts/drafts_delete.py f3a165a2 72d5e24c            # 미리보기 (지우지 않음)
    .venv/Scripts/python scripts/drafts_delete.py f3a165a2 72d5e24c --yes      # 실제 삭제 (drafts + generations + research)

id 는 앞부분만 적어도 된다 (유일하게 맞는 것만 지운다). 서비스가 도는 중에도 된다 (SQLite WAL, busy_timeout 5초).
DB 경로는 config.yaml storage.url (환경변수 MOCHANG_DATABASE_URL 이 우선).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import delete, func, select  # noqa: E402

from backend import storage as S  # noqa: E402
from backend.llm.client import load_config  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("ids", nargs="*", help="지울 draft_id (앞부분만 적어도 됨)")
    ap.add_argument("--list", action="store_true", help="초안 목록만 보여준다")
    ap.add_argument("--yes", action="store_true", help="실제로 지운다 (없으면 미리보기)")
    args = ap.parse_args()

    st = S.Storage.from_config(load_config())
    st.backup = None                      # 이 스크립트는 서비스 DB 만 다룬다
    st.init()
    assert st.engine is not None
    d, g, r = S.drafts, S.generations, S.research

    with st.engine.connect() as conn:
        rows = conn.execute(
            select(d.c.draft_id, d.c.owner, d.c.created_at, d.c.idea,
                   select(func.count()).where(g.c.draft_id == d.c.draft_id).scalar_subquery().label("gens"))
            .order_by(d.c.created_at)).all()
    print(f"서비스 DB {st.url} — 초안 {len(rows)}건")
    if args.list or not args.ids:
        for row in rows:
            print(f"  {row.draft_id[:8]}  {str(row.created_at)[:16]}  {row.owner or '-':15}  생성 {row.gens:2d}  {str(row.idea)[:60]!r}")
        return 0

    targets = []
    for prefix in args.ids:
        hits = [row for row in rows if row.draft_id.startswith(prefix)]
        if len(hits) != 1:
            print(f"  ! {prefix}: {'없음' if not hits else f'{len(hits)}건이 겹침 — 더 길게 적어주세요'}")
            return 1
        targets.append(hits[0])
    for row in targets:
        print(f"  {'삭제' if args.yes else '미리보기'}  {row.draft_id}  {row.owner or '-'}  생성 {row.gens}  {str(row.idea)[:60]!r}")
    if not args.yes:
        print("실제로 지우려면 --yes 를 붙이세요.")
        return 0
    with st.engine.begin() as conn:
        for row in targets:
            n_g = conn.execute(delete(g).where(g.c.draft_id == row.draft_id)).rowcount
            n_r = conn.execute(delete(r).where(r.c.draft_id == row.draft_id)).rowcount
            conn.execute(delete(d).where(d.c.draft_id == row.draft_id))
            print(f"  지움 {row.draft_id[:8]}: generations {n_g}, research {n_r}")
    st.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
