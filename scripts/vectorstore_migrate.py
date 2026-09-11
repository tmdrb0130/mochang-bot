"""벡터 저장소 이관 — LlamaIndex 기본 JSON(SimpleVectorStore) → Chroma (2026-09-11).

    .venv/Scripts/python scripts/vectorstore_migrate.py --dry-run          # 셈만 하고 안 쓴다
    .venv/Scripts/python scripts/vectorstore_migrate.py --limit 200        # 앞 200청크만 (연습)
    .venv/Scripts/python scripts/vectorstore_migrate.py                    # 전체 이관
    .venv/Scripts/python scripts/vectorstore_migrate.py --reset            # 대상 컬렉션을 비우고 다시

왜: 기본 저장소는 벡터를 **JSON 텍스트**로 들고 있어 열 때 전체를 파싱하고(실측 143초, 계속 증가)
쓸 때 362MB 를 통째로 다시 쓴다(실측 69.7초). Chroma 는 SQLite+HNSW 라 열기는 즉시, 쓰기는 증분이다.

**재임베딩하지 않는다.** 기존 JSON 에 벡터가 그대로 있으므로 읽어서 넣기만 한다 (모델 호출 0, Ollama 불필요).

넣는 방법: Chroma 에 직접 쓰지 않고 **LlamaIndex 어댑터(`ChromaVectorStore.add`)를 거친다.**
어댑터가 `_node_content` 같은 자기 형식 메타를 함께 써야 조회 때 노드를 복원할 수 있다.
직접 쓰면 색인은 되지만 조회에서 노드 복원이 깨진다.

원본은 건드리지 않는다 — 대상 폴더에만 쓴다. 되돌리려면 config 의 `vectorstore.backend` 를 simple 로.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

SRC_DEFAULT = "backend/.vectorstore"
DST_DEFAULT = "backend/.vectorstore-chroma"
COLLECTION = "research"


def load_source(src: Path) -> tuple[dict, dict]:
    """(node_id → 노드 __data__, node_id → 임베딩). 둘 다 있는 것만 이관 대상이다."""
    ds_path, vs_path = src / "docstore.json", src / "default__vector_store.json"
    for p in (ds_path, vs_path):
        if not p.exists():
            raise SystemExit(f"원본이 없습니다: {p}")

    print(f"  docstore.json 읽는 중 ({ds_path.stat().st_size / 1048576:.0f} MB)…", flush=True)
    docstore = json.loads(ds_path.read_text(encoding="utf-8"))
    nodes = {k: v.get("__data__") or {} for k, v in (docstore.get("docstore/data") or {}).items()}

    print(f"  default__vector_store.json 읽는 중 ({vs_path.stat().st_size / 1048576:.0f} MB, 1~2분)…", flush=True)
    vectors = json.loads(vs_path.read_text(encoding="utf-8")).get("embedding_dict") or {}
    return nodes, vectors


def clean_meta(meta: dict) -> dict:
    """Chroma 메타는 str/int/float/bool 만 받는다. None·중첩은 버리거나 문자열로."""
    out = {}
    for k, v in (meta or {}).items():
        if v is None:
            continue
        out[k] = v if isinstance(v, (str, int, float, bool)) else json.dumps(v, ensure_ascii=False)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=SRC_DEFAULT)
    ap.add_argument("--dst", default=DST_DEFAULT)
    ap.add_argument("--batch", type=int, default=1000)
    ap.add_argument("--limit", type=int, default=0, help="앞에서 N개만 (연습용)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--reset", action="store_true", help="대상 컬렉션을 비우고 다시 넣는다")
    args = ap.parse_args()

    src, dst = Path(args.src), Path(args.dst)
    t0 = time.monotonic()
    nodes, vectors = load_source(src)

    ids = [nid for nid in nodes if nid in vectors]
    skipped = len(nodes) - len(ids)
    urls = {(nodes[n].get("metadata") or {}).get("url") for n in ids}
    urls.discard(None)
    print(f"\n  노드 {len(nodes):,} · 벡터 {len(vectors):,} → 이관 대상 {len(ids):,} (벡터 없는 노드 {skipped} 건 제외)")
    print(f"  고유 URL {len(urls):,} · 차원 {len(vectors[ids[0]]) if ids else 0}")
    if args.limit:
        ids = ids[: args.limit]
        print(f"  --limit {args.limit} → {len(ids):,} 건만 넣습니다")

    if args.dry_run:
        print(f"\n  [dry-run] 아무것도 쓰지 않았습니다. ({time.monotonic() - t0:.1f}초)")
        return 0

    import chromadb
    from chromadb.config import Settings
    from llama_index.core.schema import TextNode
    from llama_index.vector_stores.chroma import ChromaVectorStore

    dst.mkdir(parents=True, exist_ok=True)
    # 학생 아이디어가 도는 서버다 — 외부 전송(텔레메트리)은 끈다.
    client = chromadb.PersistentClient(path=str(dst), settings=Settings(anonymized_telemetry=False))
    if args.reset:
        try:
            client.delete_collection(COLLECTION)
            print("  기존 컬렉션 삭제")
        except Exception:
            pass
    coll = client.get_or_create_collection(COLLECTION, metadata={"hnsw:space": "cosine"})
    store = ChromaVectorStore(chroma_collection=coll)

    have = set()
    if coll.count():
        have = set(coll.get(include=[])["ids"])
        print(f"  대상에 이미 {len(have):,} 건 있음 — 겹치는 것은 건너뜁니다")

    added = 0
    batch: list[TextNode] = []
    for i, nid in enumerate(ids, 1):
        if nid in have:
            continue
        data = nodes[nid]
        text = data.get("text") or ""
        if not text.strip():
            continue
        batch.append(TextNode(id_=nid, text=text,
                              metadata=clean_meta(data.get("metadata")),
                              embedding=list(vectors[nid])))
        if len(batch) >= args.batch:
            store.add(batch)
            added += len(batch)
            batch = []
            print(f"    {added:,}/{len(ids):,} ({time.monotonic() - t0:.0f}초)", flush=True)
    if batch:
        store.add(batch)
        added += len(batch)

    print(f"\n  이관 완료: {added:,} 건 · 컬렉션 총 {coll.count():,} 건 · {time.monotonic() - t0:.1f}초")
    print(f"  대상: {dst}  ({sum(p.stat().st_size for p in dst.rglob('*') if p.is_file()) / 1048576:.0f} MB)")
    print("  원본은 그대로 두었습니다 — 검증 뒤에 지우세요.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
