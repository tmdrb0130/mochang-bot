"""사용자가 입력한 아이디어와 생성된 신청서 초안을 DB 에 남긴다.

지금까지 초안은 브라우저 localStorage 에만 있었고 서버는 아무것도 남기지 않았다.
이 모듈은 요청이 끝날 때마다 (1) 아이디어·인테이크 답 = drafts, (2) 문항별 생성문 = generations 로 쌓는다.

  기본:  backend/.data/mochang.sqlite  (WAL 모드, gitignore)
  전환:  환경변수 MOCHANG_DATABASE_URL 또는 config.yaml storage.url 에 SQLAlchemy URL —
         예) postgresql+psycopg://user:pw@host/mochang  (드라이버만 설치하면 코드는 그대로)

원칙 (timing.py 와 같다): 모델을 호출하지 않고, 저장에 실패해도 서비스 동작에 영향을 주지 않는다.
쓰기는 스레드에서 돌려 이벤트 루프를 막지 않는다 (asyncio.to_thread).

신청서 한 벌을 묶는 키는 draft_id — 프론트가 crypto.randomUUID() 로 만들어 모든 요청에 실어 보낸다.
안 실려 오면(구 브라우저 캐시) track|idea 해시로 대신 묶는다 → 같은 아이디어 문항 9개가 한 초안이 된다.

읽는 법:  .venv/Scripts/python scripts/drafts_report.py
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import (Boolean, Column, DateTime, Integer, MetaData, String, Table, Text, create_engine, event,
                        select)
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

log = logging.getLogger("mochang.storage")

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_URL = "sqlite:///backend/.data/mochang.sqlite"
SCHEMA_VERSION = 1

# 프론트가 보내는 draft_id 형식 (UUID 등). 이 밖의 값은 무시하고 해시로 대체한다 — DB 키에 임의 문자열이 들어오지 않게.
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")

# drafts 에 그대로 옮기는 입력 필드 (요청 스키마 GenerateRequest/IntakeRequest 공통부)
_DRAFT_FIELDS = ("idea", "track", "is_business", "current_item", "team", "capability")
# generations 를 남기는 작업 종류 — 결과에 text 가 있는 것만
_TEXT_KINDS = ("generate", "extend")

metadata = MetaData()

drafts = Table(
    "drafts", metadata,
    Column("draft_id", String(64), primary_key=True),
    Column("idea", Text, nullable=False, default=""),
    Column("track", String(16), default="tech"),
    Column("is_business", Boolean, default=False),
    Column("current_item", Text, default=""),
    Column("team", Text, default=""),
    Column("capability", Text, default=""),
    Column("answers", Text, default="[]"),          # 인테이크 답변 JSON [{slot,label,answer,unknown}]
    Column("owner", String(64)),                    # 제출 클라이언트(IP). 인증이 없어 남용 추적용
    Column("model", String(160)),
    Column("request_count", Integer, default=0),    # 이 초안으로 들어온 요청 수 (인테이크·생성·이어쓰기·조사·검증)
    Column("created_at", DateTime, nullable=False),
    Column("updated_at", DateTime, nullable=False, index=True),
)

generations = Table(
    "generations", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("draft_id", String(64), nullable=False, index=True),
    Column("question_id", String(16), nullable=False),
    Column("kind", String(24), nullable=False),     # generate | extend
    Column("style", String(16)),
    Column("text", Text, nullable=False),
    Column("chars", Integer),
    Column("model", String(160)),
    Column("meta", Text),                           # 결과의 나머지(폴리시·refine 신호 등) JSON — 품질 분석용
    Column("created_at", DateTime, nullable=False, index=True),
)

schema_meta = Table(
    "schema_meta", metadata,
    Column("key", String(32), primary_key=True),
    Column("value", String(64)),
)


def draft_id_for(form: dict) -> str:
    """요청 폼에서 초안 키를 정한다. draft_id 가 형식에 맞으면 그것, 아니면 track|idea 해시(접두 'h')."""
    did = str(form.get("draft_id") or "").strip()
    if _ID_RE.match(did):
        return did
    raw = f"{form.get('track') or ''}|{(form.get('idea') or '').strip()}"
    return "h" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:23]


def _dumps(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False, sort_keys=True, default=str)


def _loads(s: str | None, default):
    if not s:
        return default
    try:
        return json.loads(s)
    except ValueError:
        return default


class Storage:
    def __init__(self, url: str = DEFAULT_URL, enabled: bool = True):
        self.url = url
        self.enabled = enabled
        self.engine: Engine | None = None

    @classmethod
    def from_config(cls, config: dict) -> "Storage":
        cfg = config.get("storage") or {}
        url = os.environ.get("MOCHANG_DATABASE_URL") or cfg.get("url") or DEFAULT_URL
        return cls(url=url, enabled=bool(cfg.get("enabled", True)))

    # ── 수명 ──
    @property
    def is_sqlite(self) -> bool:
        return self.url.startswith("sqlite")

    def init(self) -> None:
        """엔진 생성 + 테이블 생성. 실패하면 예외를 올린다 — 호출자(lifespan)가 잡아서 저장만 끈다."""
        if not self.enabled or self.engine is not None:
            return
        url = self.url
        if self.is_sqlite and ":memory:" not in url:
            # 상대 경로는 프로젝트 루트 기준 (uvicorn 을 어디서 띄우든 같은 파일을 쓰게)
            path = url[len("sqlite:///"):]
            p = Path(path)
            if not p.is_absolute():
                p = ROOT / p
            p.parent.mkdir(parents=True, exist_ok=True)
            url = "sqlite:///" + p.as_posix()
        if self.is_sqlite:
            engine = create_engine(url, connect_args={"check_same_thread": False, "timeout": 5})

            @event.listens_for(engine, "connect")
            def _pragmas(conn, _rec):
                # WAL: 읽기가 쓰기를 막지 않는다 (사람이 몰려 문항 9개씩 저장될 때 폴링·조회가 안 기다리게)
                # busy_timeout: 동시 쓰기가 겹치면 5초까지 기다렸다가 실패 (바로 'database is locked' 를 내지 않게)
                cur = conn.cursor()
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA synchronous=NORMAL")
                cur.execute("PRAGMA busy_timeout=5000")
                cur.close()
        else:
            engine = create_engine(url, pool_pre_ping=True)
        metadata.create_all(engine)
        with engine.begin() as conn:
            row = conn.execute(select(schema_meta.c.value).where(schema_meta.c.key == "version")).first()
            if row is None:
                conn.execute(schema_meta.insert().values(key="version", value=str(SCHEMA_VERSION)))
        self.engine = engine

    def close(self) -> None:
        if self.engine is not None:
            self.engine.dispose()
            self.engine = None

    # ── 쓰기 (동기; 스레드에서 부른다) ──
    def upsert_draft(self, form: dict, owner: str | None = None) -> str:
        """아이디어·입력·인테이크 답을 저장하고 draft_id 를 돌려준다. 있으면 최신 입력으로 갱신."""
        assert self.engine is not None
        did = draft_id_for(form)
        now = datetime.now()
        values = {k: form.get(k) for k in _DRAFT_FIELDS}
        values["idea"] = values.get("idea") or ""
        values["is_business"] = bool(values.get("is_business"))
        for k in ("current_item", "team", "capability"):
            values[k] = values.get(k) or ""
        values["answers"] = _dumps(form.get("answers") or [])
        values["model"] = form.get("model")
        values["updated_at"] = now
        if owner:
            values["owner"] = owner[:64]
        update = (drafts.update().where(drafts.c.draft_id == did)
                  .values(request_count=drafts.c.request_count + 1, **values))
        with self.engine.begin() as conn:
            if conn.execute(update).rowcount:
                return did
        try:
            with self.engine.begin() as conn:
                conn.execute(drafts.insert().values(draft_id=did, request_count=1, created_at=now, **values))
        except IntegrityError:
            # 같은 초안의 첫 요청 둘이 동시에 들어온 경우(문항 여러 개 동시 생성) — 다른 쪽이 먼저 넣었으니 갱신으로
            with self.engine.begin() as conn:
                conn.execute(update)
        return did

    def add_generation(self, draft_id: str, kind: str, form: dict, result: dict) -> int | None:
        """문항 생성/이어쓰기 결과 한 건. text 가 없으면 남기지 않는다."""
        assert self.engine is not None
        text = result.get("text") if isinstance(result, dict) else None
        if not text:
            return None
        meta = {k: v for k, v in result.items() if k not in ("text", "question_id", "style", "model")}
        with self.engine.begin() as conn:
            res = conn.execute(generations.insert().values(
                draft_id=draft_id,
                question_id=str(result.get("question_id") or form.get("question_id") or "")[:16],
                kind=kind[:24],
                style=str(result.get("style") or form.get("style") or "")[:16] or None,
                text=text,
                chars=len(text),
                model=str(result.get("model") or form.get("model") or "")[:160] or None,
                meta=_dumps(meta) if meta else None,
                created_at=datetime.now(),
            ))
            return res.inserted_primary_key[0] if res.inserted_primary_key else None

    def _record_sync(self, kind: str, form: dict, result: Any, owner: str | None) -> None:
        if not form.get("idea"):
            return
        did = self.upsert_draft(form, owner)
        if kind in _TEXT_KINDS and isinstance(result, dict):
            self.add_generation(did, kind, form, result)

    async def record(self, kind: str, form: dict, result: Any, owner: str | None = None) -> None:
        """요청 하나가 끝났을 때 부른다. 어떤 경우에도 예외를 올리지 않는다 — 저장 때문에 생성이 실패하면 안 된다."""
        if not self.enabled or self.engine is None:
            return
        try:
            await asyncio.to_thread(self._record_sync, kind, form, result, owner)
        except Exception as e:
            log.warning("저장 실패 (%s): %s", kind, str(e)[:200])

    # ── 읽기 ──
    def get_draft(self, draft_id: str) -> dict | None:
        """초안 + 문항별 생성 이력 (오래된 것부터). 없으면 None."""
        if self.engine is None:
            return None
        with self.engine.connect() as conn:
            d = conn.execute(select(drafts).where(drafts.c.draft_id == draft_id)).mappings().first()
            if d is None:
                return None
            gens = conn.execute(select(generations).where(generations.c.draft_id == draft_id)
                                .order_by(generations.c.id)).mappings().all()
        out = dict(d)
        out["answers"] = _loads(out.get("answers"), [])
        out["generations"] = [{**dict(g), "meta": _loads(g.get("meta"), None)} for g in gens]
        for row in [out, *out["generations"]]:
            for k in ("created_at", "updated_at"):
                if isinstance(row.get(k), datetime):
                    row[k] = row[k].isoformat(timespec="seconds")
        return out

    def list_drafts(self, limit: int = 50) -> list[dict]:
        """최근 초안 목록 (운영자 스크립트용). 본문은 idea 앞부분만."""
        if self.engine is None:
            return []
        with self.engine.connect() as conn:
            rows = conn.execute(select(drafts).order_by(drafts.c.updated_at.desc()).limit(limit)).mappings().all()
            out = []
            for r in rows:
                n = conn.execute(select(generations.c.id).where(generations.c.draft_id == r["draft_id"])).all()
                out.append({"draft_id": r["draft_id"], "idea": (r["idea"] or "")[:80], "track": r["track"],
                            "owner": r["owner"], "requests": r["request_count"], "generations": len(n),
                            "created_at": r["created_at"].isoformat(timespec="seconds"),
                            "updated_at": r["updated_at"].isoformat(timespec="seconds")})
            return out
