"""테스트 초안 삭제 도구 (2026-09-03). **is_test=1 표시가 있는 초안만** 지운다 — 실사용 초안은 id 를 콕 집어도, 어떤 옵션으로도 지워지지 않는다.

    .venv/Scripts/python scripts/drafts_delete.py --list                # 서비스 DB 초안 목록 (T = 테스트 표시)
    .venv/Scripts/python scripts/drafts_delete.py --list --backup       # 백업 DB 목록
    .venv/Scripts/python scripts/drafts_delete.py --tests               # 테스트 표시 초안 미리보기
    .venv/Scripts/python scripts/drafts_delete.py --tests --yes         # 테스트 표시 초안 전부 삭제 (drafts + generations + research)
    .venv/Scripts/python scripts/drafts_delete.py 72d5e24c --yes        # 특정 초안 — 테스트 표시가 있을 때만 지워진다

테스트 표시는 요청 헤더 X-Mochang-Test(사이트 주소 ?test=1, scripts/load_test.py, scripts/foreign_e2e.py)로 들어온 초안에만 붙고,
그런 초안은 서비스 DB 에 아예 들어오지 않는다(백업 DB 에만). 따라서 보통은 --backup 과 함께 쓴다.
서비스가 도는 중에도 된다 (SQLite WAL, busy_timeout 5초). DB 경로는 config.yaml storage (환경변수가 우선).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import storage as S  # noqa: E402
from backend.llm.client import load_config  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("ids", nargs="*", help="지울 draft_id (앞부분만 적어도 됨). 테스트 표시가 있는 것만 지워진다")
    ap.add_argument("--list", action="store_true", help="초안 목록만")
    ap.add_argument("--tests", action="store_true", help="테스트 표시가 있는 초안 전부")
    ap.add_argument("--backup", action="store_true", help="서비스 DB 대신 백업 DB 를 다룬다")
    ap.add_argument("--yes", action="store_true", help="실제로 지운다 (없으면 미리보기)")
    args = ap.parse_args()

    st = S.Storage.from_config(load_config())
    if args.backup:
        if st.backup is None:
            print("config.yaml storage.backup_url 이 없습니다."); return 1
        st = st.backup
    st.backup = None                      # 한 DB 만 다룬다 (미러링 없음)
    st.init()
    rows = sorted(st.list_drafts(limit=10**9), key=lambda r: r['created_at'])
    print(f"{'백업' if args.backup else '서비스'} DB {st.url} — 초안 {len(rows)}건 (테스트 표시 {sum(1 for r in rows if r['is_test'])}건)")

    def show(r, mark=""):
        print(f"  {mark:4}{r['draft_id'][:8]}  {'T' if r['is_test'] else ' '}  {str(r['created_at'])[:16]}  {(r['owner'] or '-'):15}  생성 {r['generations']:2d}  {str(r['idea'])[:55]!r}")

    if args.list or not (args.ids or args.tests):
        for r in rows:
            show(r)
        return 0

    targets: list[dict] = []
    if args.tests:
        targets = [r for r in rows if r["is_test"]]
    for prefix in args.ids:
        hits = [r for r in rows if r["draft_id"].startswith(prefix)]
        if len(hits) != 1:
            print(f"  ! {prefix}: {'없음' if not hits else f'{len(hits)}건이 겹침 — 더 길게 적어주세요'}"); return 1
        targets.append(hits[0])
    if not targets:
        print("대상 없음."); return 0
    for r in targets:
        show(r, "삭제" if (args.yes and r["is_test"]) else ("거부" if not r["is_test"] else "예정"))
    real = [r for r in targets if not r["is_test"]]
    if real:
        print(f"  ! {len(real)}건은 실사용 초안(테스트 표시 없음)이라 지우지 않습니다. 이 도구로는 지울 수 없습니다.")
    if not args.yes:
        print("실제로 지우려면 --yes"); return 0
    deleted = st.delete_drafts([r["draft_id"] for r in targets if r["is_test"]])
    print(f"지움 {len(deleted)}건: {', '.join(d[:8] for d in deleted) or '-'}")
    st.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
