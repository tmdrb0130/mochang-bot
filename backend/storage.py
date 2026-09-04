"""사용자가 입력한 아이디어와 생성된 신청서 초안을 DB 에 남긴다.

지금까지 초안은 브라우저 localStorage 에만 있었고 서버는 아무것도 남기지 않았다.
이 모듈은 요청이 끝날 때마다 (1) 아이디어·인테이크 답 = drafts, (2) 문항별 생성문 = generations,
(3) 조사 결과(검색어·페이지·사실 목록) = research 로 쌓는다 (2026-09-03: 디스크 캐시 7일이 지나도 같은 근거로 다시 만들 수 있게).

  기본:  backend/.data/mochang.sqlite  (WAL 모드, gitignore)
  전환:  환경변수 MOCHANG_DATABASE_URL 또는 config.yaml storage.url 에 SQLAlchemy URL —
         예) postgresql+psycopg://user:pw@host/mochang  (드라이버만 설치하면 코드는 그대로)

원칙 (timing.py 와 같다): 모델을 호출하지 않고, 저장에 실패해도 서비스 동작에 영향을 주지 않는다.
쓰기는 스레드에서 돌려 이벤트 루프를 막지 않는다 (asyncio.to_thread).

신청서 한 벌을 묶는 키는 draft_id — 프론트가 crypto.randomUUID() 로 만들어 모든 요청에 실어 보낸다.
안 실려 오면(구 브라우저 캐시) track|idea 해시로 대신 묶는다 → 같은 아이디어 문항 9개가 한 초안이 된다.

읽는 법:  .venv/Scripts/python scripts/drafts_report.py

서비스용 / 백업용 두 DB (2026-09-03 사용자 결정): 백업(storage.backup_url)에는 테스트를 포함해 **전부** 남기고,
서비스 DB(url)에는 실제 사용자가 브라우저에서 입력한 것만 남긴다. 구분은 요청 헤더 `X-Mochang-Test`(부하 테스트·E2E 스크립트가 붙임) —
record(..., test=True) 면 서비스 DB 를 건너뛴다. 쓰기 메서드(upsert_draft·add_generation·add_research)가 백업에 미러링하므로
마무리 작업자처럼 record 를 안 거치는 쓰기도 백업에 남는다. 백업 쓰기 실패는 서비스 쓰기에 영향을 주지 않는다.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import secrets
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
SCHEMA_VERSION = 4      # 2: research 테이블 (2026-09-03). 3: drafts.is_test (2026-09-03). 4: drafts.share_token (2026-09-04). 열 추가는 init 이 ALTER TABLE 로.

# 프론트가 보내는 draft_id 형식 (UUID 등). 이 밖의 값은 무시하고 해시로 대체한다 — DB 키에 임의 문자열이 들어오지 않게.
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
# 공유 링크 토큰 형식 (secrets.token_urlsafe(18) = 24자). DB 키(draft_id)와 별개라 링크에 키가 드러나지 않는다.
SHARE_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")

# drafts 에 그대로 옮기는 입력 필드 (요청 스키마 GenerateRequest/IntakeRequest 공통부)
_DRAFT_FIELDS = ("idea", "track", "is_business", "current_item", "team", "capability")
# generations 를 남기는 작업 종류 — 결과에 text 가 있는 것만
_TEXT_KINDS = ("generate", "extend")
# research 를 남기는 작업 종류. idea_research 는 인테이크 안의 아이디어 공통 조사(question_id 는 IDEA_QUESTION 으로 저장).
_RESEARCH_KINDS = ("research", "idea_research")
IDEA_QUESTION = "idea"

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
    # 테스트로 만들어진 초안 (요청 헤더 X-Mochang-Test). 서비스 DB 에는 들어오지 않고 백업 DB 에서만 1 이다.
    # 삭제 도구(delete_drafts)는 이 표시가 있는 행만 지운다 — 실사용 행은 어떤 옵션으로도 지우지 않는다 (2026-09-03 사용자 결정).
    Column("is_test", Boolean, nullable=False, default=False, server_default="0"),
    # 공유 링크용 토큰 (2026-09-04). 다른 PC·폰에서 이어 보는 링크에 DB 키(draft_id) 대신 이걸 쓴다.
    # 처음 "링크 만들기" 를 눌렀을 때 만들어지고(share_token 메서드), 그 뒤로는 같은 값. 없는 초안은 NULL.
    Column("share_token", String(64), index=True),
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

research = Table(
    "research", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("draft_id", String(64), nullable=False, index=True),
    Column("question_id", String(16), nullable=False),   # q1… 또는 IDEA_QUESTION("idea": 인테이크의 아이디어 공통 조사)
    Column("cache_key", String(80)),                      # rag/pipeline._cache_key — 디스크 캐시와 대조용
    Column("queries", Text),                              # 검색어 목록 JSON
    Column("pages", Text),                                # 본 페이지 [{url,title}] JSON
    Column("facts", Text, nullable=False),                # 사실 목록 JSON — 생성 때 [웹 참고자료] 로 주입된 그것
    Column("facts_count", Integer),
    Column("backend", String(32)),                        # 검색 백엔드(ddgs·vane·vectorstore 등)
    Column("cached", Boolean),                            # 디스크 캐시 적중이었는지
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
    def __init__(self, url: str = DEFAULT_URL, enabled: bool = True, backup: "Storage | None" = None):
        self.url = url
        self.enabled = enabled
        self.engine: Engine | None = None
        self.backup = backup            # 전부 남기는 백업 저장소 (없으면 None). 백업 자신은 backup 이 None 이다.

    @classmethod
    def from_config(cls, config: dict) -> "Storage":
        cfg = config.get("storage") or {}
        url = os.environ.get("MOCHANG_DATABASE_URL") or cfg.get("url") or DEFAULT_URL
        enabled = bool(cfg.get("enabled", True))
        # 백업 DB: 환경변수가 우선(빈 문자열이면 끔), 없으면 config. 같은 파일을 가리키면 이중 저장이라 백업을 끈다.
        backup_url = os.environ.get("MOCHANG_BACKUP_DATABASE_URL")
        if backup_url is None:
            backup_url = str(cfg.get("backup_url") or "")
        backup = cls(url=backup_url, enabled=enabled) if backup_url and backup_url != url else None
        return cls(url=url, enabled=enabled, backup=backup)

    def _mirror(self, what: str, fn, *args) -> None:
        """백업 저장소에 같은 쓰기를 한 번 더. 실패해도 서비스 쓰기는 이미 끝났으므로 경고만 남긴다."""
        b = self.backup
        if b is None or not b.enabled or b.engine is None:
            return
        try:
            fn(*args)
        except Exception as e:
            log.warning("백업 저장 실패 (%s): %s", what, str(e)[:200])

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
            # v3 이행: 기존 DB 에 is_test 열이 없으면 붙인다 (create_all 은 새 테이블만 만든다). 기존 행은 전부 0 = 실사용.
            cols = {r[1] for r in conn.exec_driver_sql("PRAGMA table_info(drafts)").fetchall()} if self.is_sqlite else None
            if cols is not None and "is_test" not in cols:
                conn.exec_driver_sql("ALTER TABLE drafts ADD COLUMN is_test BOOLEAN NOT NULL DEFAULT 0")
            # v4 이행: 공유 링크 토큰 열. 기존 행은 NULL — 학생이 링크를 만들 때 채워진다.
            if cols is not None and "share_token" not in cols:
                conn.exec_driver_sql("ALTER TABLE drafts ADD COLUMN share_token VARCHAR(64)")
                conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_drafts_share_token ON drafts (share_token)")
            row = conn.execute(select(schema_meta.c.value).where(schema_meta.c.key == "version")).first()
            if row is None:
                conn.execute(schema_meta.insert().values(key="version", value=str(SCHEMA_VERSION)))
            elif str(row[0]) != str(SCHEMA_VERSION):
                conn.execute(schema_meta.update().where(schema_meta.c.key == "version").values(value=str(SCHEMA_VERSION)))
        self.engine = engine
        if self.backup is not None:
            try:
                self.backup.init()
            except Exception as e:          # 백업을 못 열어도 서비스 저장은 계속한다
                log.warning("백업 저장소를 열 수 없어 백업을 끕니다 (%s): %s", self.backup.url, e)
                self.backup = None

    def close(self) -> None:
        if self.engine is not None:
            self.engine.dispose()
            self.engine = None
        if self.backup is not None:
            self.backup.close()

    # ── 쓰기 (동기; 스레드에서 부른다) ──
    def upsert_draft(self, form: dict, owner: str | None = None, test: bool = False) -> str:
        """아이디어·입력·인테이크 답을 저장하고 draft_id 를 돌려준다. 있으면 최신 입력으로 갱신. 백업에도 같은 행.
        test=True 는 새 행에만 is_test=1 을 찍는다 — 이미 실사용으로 들어온 초안은 테스트로 바뀌지 않는다."""
        did = self._upsert_draft(form, owner, test)
        if self.backup:
            self._mirror("upsert_draft", self.backup._upsert_draft, form, owner, test)
        return did

    def _upsert_draft(self, form: dict, owner: str | None = None, test: bool = False) -> str:
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
                conn.execute(drafts.insert().values(draft_id=did, request_count=1, created_at=now, is_test=bool(test), **values))
        except IntegrityError:
            # 같은 초안의 첫 요청 둘이 동시에 들어온 경우(문항 여러 개 동시 생성) — 다른 쪽이 먼저 넣었으니 갱신으로
            with self.engine.begin() as conn:
                conn.execute(update)
        return did

    def add_generation(self, draft_id: str, kind: str, form: dict, result: dict) -> int | None:
        """문항 생성문 한 건. 백업에도 같은 행 (마무리 작업자가 record 없이 직접 부르는 경로 포함)."""
        rid = self._add_generation(draft_id, kind, form, result)
        if self.backup:
            self._mirror("add_generation", self.backup._add_generation, draft_id, kind, form, result)
        return rid

    def _add_generation(self, draft_id: str, kind: str, form: dict, result: dict) -> int | None:
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

    def add_research(self, draft_id: str, question_id: str, result: dict) -> int | None:
        rid = self._add_research(draft_id, question_id, result)
        if self.backup:
            self._mirror("add_research", self.backup._add_research, draft_id, question_id, result)
        return rid

    def _add_research(self, draft_id: str, question_id: str, result: dict) -> int | None:
        """조사 결과 한 건 (문항 조사 또는 아이디어 공통 조사). facts 목록이 없으면(조사 실패) 남기지 않는다.
        빈 목록은 남긴다 — "조사했지만 인용할 사실이 없었다" 도 정보다."""
        assert self.engine is not None
        if not isinstance(result, dict) or not isinstance(result.get("facts"), list) or result.get("error"):
            return None
        facts = result["facts"]
        pages = [{"url": p.get("url", ""), "title": p.get("title", "")} for p in (result.get("pages") or []) if isinstance(p, dict)]
        with self.engine.begin() as conn:
            res = conn.execute(research.insert().values(
                draft_id=draft_id,
                question_id=str(question_id or "")[:16],
                cache_key=str(result.get("cache_key") or "")[:80] or None,
                queries=_dumps(result.get("queries") or []),
                pages=_dumps(pages),
                facts=_dumps(facts),
                facts_count=len(facts),
                backend=str(result.get("backend") or "")[:32] or None,
                cached=bool(result.get("cached")),
                created_at=datetime.now(),
            ))
            return res.inserted_primary_key[0] if res.inserted_primary_key else None

    def _record_sync(self, kind: str, form: dict, result: Any, owner: str | None, test: bool = False) -> None:
        if not form.get("idea"):
            return
        did = self.upsert_draft(form, owner, test)
        if kind in _TEXT_KINDS and isinstance(result, dict):
            self.add_generation(did, kind, form, result)
        elif kind in _RESEARCH_KINDS and isinstance(result, dict):
            qid = IDEA_QUESTION if kind == "idea_research" else str(form.get("question_id") or "")
            if qid:
                self.add_research(did, qid, result)

    async def record(self, kind: str, form: dict, result: Any, owner: str | None = None, test: bool = False) -> None:
        """요청 하나가 끝났을 때 부른다. 어떤 경우에도 예외를 올리지 않는다 — 저장 때문에 생성이 실패하면 안 된다.
        test=True(요청 헤더 X-Mochang-Test) 면 서비스 DB 는 건너뛰고 백업에만 남긴다."""
        if not self.enabled or self.engine is None:
            return
        try:
            if test:
                if self.backup is not None and self.backup.engine is not None:
                    await asyncio.to_thread(self.backup._record_sync, kind, form, result, owner, True)
                return
            await asyncio.to_thread(self._record_sync, kind, form, result, owner)     # 쓰기 메서드가 백업에 미러링한다
        except Exception as e:
            log.warning("저장 실패 (%s): %s", kind, str(e)[:200])

    # ── 삭제 (테스트 초안만) ──
    def delete_drafts(self, draft_ids: list[str] | None = None) -> list[str]:
        """is_test=1 인 초안(과 그 generations·research)만 지운다. draft_ids 를 주면 그중 테스트 표시가 있는 것만,
        안 주면 테스트 표시가 있는 전부. **실사용 행(is_test=0)은 어떤 인자로도 지우지 않는다.** 돌려주는 값: 지운 draft_id."""
        if self.engine is None:
            return []
        from sqlalchemy import delete
        cond = drafts.c.is_test.is_(True)
        if draft_ids is not None:
            if not draft_ids:
                return []
            cond = cond & drafts.c.draft_id.in_(list(draft_ids))
        with self.engine.begin() as conn:
            ids = [r[0] for r in conn.execute(select(drafts.c.draft_id).where(cond)).all()]
            if not ids:
                return []
            conn.execute(delete(generations).where(generations.c.draft_id.in_(ids)))
            conn.execute(delete(research).where(research.c.draft_id.in_(ids)))
            conn.execute(delete(drafts).where(drafts.c.draft_id.in_(ids) & drafts.c.is_test.is_(True)))
        return ids

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
        out.pop("share_token", None)             # 공유 토큰은 /drafts/{id}/share 로만 준다 (응답 모양 유지)
        out["answers"] = _loads(out.get("answers"), [])
        out["generations"] = [{**dict(g), "meta": _loads(g.get("meta"), None)} for g in gens]
        for row in [out, *out["generations"]]:
            for k in ("created_at", "updated_at"):
                if isinstance(row.get(k), datetime):
                    row[k] = row[k].isoformat(timespec="seconds")
        return out

    # ── 공유 링크 (2026-09-04) ──
    def share_token(self, draft_id: str) -> str | None:
        """이 초안의 공유 토큰. 없으면 새로 만들어 저장하고(백업에도 같은 값) 돌려준다. 초안이 없으면 None.
        토큰은 draft_id 와 무관한 난수라 링크에 DB 키가 드러나지 않는다."""
        if self.engine is None:
            return None
        with self.engine.begin() as conn:
            row = conn.execute(select(drafts.c.share_token).where(drafts.c.draft_id == draft_id)).first()
            if row is None:
                return None
            if row[0]:
                return str(row[0])
            token = secrets.token_urlsafe(18)
            # 동시에 두 탭이 눌러도 먼저 넣은 값이 남는다 (share_token IS NULL 조건).
            n = conn.execute(drafts.update().where(drafts.c.draft_id == draft_id, drafts.c.share_token.is_(None))
                             .values(share_token=token)).rowcount
            if not n:
                token = str(conn.execute(select(drafts.c.share_token).where(drafts.c.draft_id == draft_id)).scalar() or token)
        if self.backup:
            self._mirror("share_token", self.backup._set_share_token, draft_id, token)
        return token

    def _set_share_token(self, draft_id: str, token: str) -> None:
        assert self.engine is not None
        with self.engine.begin() as conn:
            conn.execute(drafts.update().where(drafts.c.draft_id == draft_id, drafts.c.share_token.is_(None)).values(share_token=token))

    def draft_id_for_share(self, token: str) -> str | None:
        """공유 토큰 → draft_id. 형식이 다르거나 없으면 None."""
        if self.engine is None or not SHARE_TOKEN_RE.match(token or ""):
            return None
        with self.engine.connect() as conn:
            row = conn.execute(select(drafts.c.draft_id).where(drafts.c.share_token == token)).first()
        return str(row[0]) if row else None

    def get_research(self, draft_id: str, question_id: str) -> list[dict] | None:
        """이 초안·문항의 가장 최근 조사 사실 목록. 없으면 None (빈 목록과 구분)."""
        if self.engine is None:
            return None
        with self.engine.connect() as conn:
            row = conn.execute(select(research.c.facts).where(research.c.draft_id == draft_id, research.c.question_id == question_id)
                               .order_by(research.c.id.desc()).limit(1)).first()
        return _loads(row[0], []) if row else None

    def list_unfinished(self, idle_seconds: int, lookback_days: int = 3, now: datetime | None = None) -> list[dict]:
        """마무리 작업자(backend/finisher.py)용: 생성을 시작했지만(generate 1건 이상) 마지막 요청 뒤 idle_seconds 넘게 조용한 초안.
        너무 오래된 초안(lookback_days 이전)은 보지 않는다. 각 항목: 초안 입력 + done = {(question_id, style)} + styles."""
        if self.engine is None:
            return []
        now = now or datetime.now()
        cutoff = now.timestamp() - idle_seconds
        since = datetime.fromtimestamp(now.timestamp() - lookback_days * 86400)
        out = []
        with self.engine.connect() as conn:
            rows = conn.execute(select(drafts).where(drafts.c.updated_at >= since).order_by(drafts.c.updated_at.desc())).mappings().all()
            for d in rows:
                if d["updated_at"].timestamp() > cutoff:
                    continue                                   # 아직 활동 중
                gens = conn.execute(select(generations.c.question_id, generations.c.style)
                                    .where(generations.c.draft_id == d["draft_id"], generations.c.kind == "generate")).all()
                if not gens:
                    continue                                   # 생성을 시작한 적이 없다 (카드 단계에서 나감) — 대상 아님
                done = {(q, s or "") for q, s in gens}
                styles = sorted({s for _, s in done if s}) or ["logic"]
                out.append({**{k: d[k] for k in _DRAFT_FIELDS}, "draft_id": d["draft_id"], "model": d["model"],
                            "answers": _loads(d["answers"], []), "updated_at": d["updated_at"], "done": done, "styles": styles})
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
                            "is_test": bool(r["is_test"]),          # 테스트 표시 (2026-09-03) — 삭제 도구가 이것만 지운다
                            "created_at": r["created_at"].isoformat(timespec="seconds"),
                            "updated_at": r["updated_at"].isoformat(timespec="seconds")})
            return out
