"""KCI 이용 준수 사항 5항 — 이용 종료 시 KCI 자료를 로컬에서 지운다.

  .venv/Scripts/python -m scripts.purge_kci_cache            # 무엇이 지워질지 보여주기만 (기본)
  .venv/Scripts/python -m scripts.purge_kci_cache --apply    # 실제 삭제

지우는 곳 두 군데:
  1) 검색 결과 디스크 캐시 backend/.cache/research/*.json — KCI 항목만 빼고 다시 쓴다 (다른 소스는 남긴다)
  2) 조사 결과(facts) 캐시 — source_kind/url 이 KCI 인 사실과 페이지 항목 제거

벡터DB(backend/.vectorstore)에는 KCI 가 애초에 들어가지 않는다 (준수 2항, pipeline.NO_INDEX_SOURCE).
그래도 예전 데이터가 있을 수 있어 발견되면 경고만 낸다 — 벡터DB 삭제는 문서 단위 재색인이 필요해 사람이 판단한다.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CACHE_DIR = ROOT / "backend" / ".cache" / "research"
VECTOR_DIR = ROOT / "backend" / ".vectorstore"
KCI_MARKS = ("kci.go.kr", "\"kci\"")


def is_kci(row: dict) -> bool:
    """검색 결과·사실 한 건이 KCI 에서 온 것인지."""
    if not isinstance(row, dict):
        return False
    if str(row.get("source_type") or row.get("source_kind") or "").lower() == "kci":
        return True
    return "kci.go.kr" in str(row.get("url", ""))


def clean_payload(payload):
    """캐시 파일 한 개에서 KCI 항목을 뺀다. (새 내용, 지운 건수)"""
    removed = 0
    if isinstance(payload, list):
        out = []
        for item in payload:
            new, n = clean_payload(item)
            removed += n
            if isinstance(item, dict) and is_kci(item):
                removed += 1
                continue
            out.append(new)
        return out, removed
    if isinstance(payload, dict):
        out = {}
        for k, v in payload.items():
            if k in ("results", "facts", "pages") and isinstance(v, list):
                kept = [x for x in v if not is_kci(x)]
                removed += len(v) - len(kept)
                v = kept
            new, n = clean_payload(v) if isinstance(v, (dict, list)) else (v, 0)
            removed += n
            out[k] = new
        return out, removed
    return payload, 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="실제로 파일을 고친다 (기본은 미리보기)")
    a = ap.parse_args()

    files = sorted(CACHE_DIR.glob("*.json")) if CACHE_DIR.exists() else []
    touched = total = 0
    for path in files:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        cleaned, removed = clean_payload(payload)
        if not removed:
            continue
        touched += 1
        total += removed
        print(f"{'삭제' if a.apply else '삭제 예정'}: {path.name} 에서 {removed}건")
        if a.apply:
            path.write_text(json.dumps(cleaned, ensure_ascii=False), encoding="utf-8")

    print(f"캐시 파일 {len(files)}개 중 {touched}개에서 KCI 항목 {total}건 "
          f"{'삭제함' if a.apply else '삭제 예정 (--apply 로 실행)'}")

    if VECTOR_DIR.exists():
        blob = ""
        for name in ("docstore.json", "index_store.json"):
            f = VECTOR_DIR / name
            if f.exists():
                blob += f.read_text(encoding="utf-8", errors="ignore")
        if any(m in blob for m in KCI_MARKS):
            print("⚠️ 벡터DB 에 KCI 흔적이 있다 — 준수 2항 위반 가능. 사람이 확인해 재색인할 것 "
                  f"({VECTOR_DIR})")
        else:
            print("벡터DB: KCI 항목 없음 (준수 2항 정상)")


if __name__ == "__main__":
    main()
