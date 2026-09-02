"""저장된 아이디어·초안(backend/storage.py)을 읽어 보여준다. 모델을 호출하지 않는다. 읽기만 한다.

    .venv/Scripts/python scripts/drafts_report.py                 # 최근 초안 50건 목록
    .venv/Scripts/python scripts/drafts_report.py --last 200
    .venv/Scripts/python scripts/drafts_report.py --id <draft_id>  # 초안 하나: 입력 + 문항별 생성문
    .venv/Scripts/python scripts/drafts_report.py --dump out.jsonl # 전부 JSONL 로 내보내기 (분석용)

DB 위치는 config.yaml storage.url (환경변수 MOCHANG_DATABASE_URL 이 우선).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

from backend.llm.client import load_config    # noqa: E402
from backend.storage import Storage           # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--last", type=int, default=50, help="목록 건수")
    ap.add_argument("--id", help="초안 하나를 자세히")
    ap.add_argument("--dump", help="모든 초안을 이 JSONL 파일로")
    args = ap.parse_args()

    st = Storage.from_config(load_config())
    st.enabled = True
    st.init()

    if args.id:
        row = st.get_draft(args.id)
        if row is None:
            print("없음:", args.id)
            return 1
        gens = row.pop("generations")
        print(json.dumps(row, ensure_ascii=False, indent=2))
        for g in gens:
            print(f"\n── [{g['created_at']}] {g['question_id']} ({g['kind']}, {g['chars']}자, {g['model']}) ──")
            print(g["text"])
        return 0

    if args.dump:
        n = 0
        with open(args.dump, "w", encoding="utf-8") as f:
            for d in st.list_drafts(limit=10**9):
                f.write(json.dumps(st.get_draft(d["draft_id"]), ensure_ascii=False) + "\n")
                n += 1
        print(f"{n}건 → {args.dump}")
        return 0

    rows = st.list_drafts(limit=args.last)
    print(f"{'갱신':19} {'draft_id':26} {'트랙':5} {'요청':>4} {'문항':>4}  아이디어")
    for r in rows:
        print(f"{r['updated_at']:19} {r['draft_id']:26} {r['track'] or '':5} {r['requests']:>4} {r['generations']:>4}  {r['idea']}")
    print(f"\n{len(rows)}건 (DB: {st.url})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
