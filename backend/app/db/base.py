"""Database plumbing: engine, session, UTC datetime type, declarative base, tenant-scoped helper.

SQLite (local default): WAL + BEGIN IMMEDIATE so writers serialise and never silently interleave.
PostgreSQL (compose): chain-head / inventory rows are additionally locked with SELECT ... FOR UPDATE.
"""
from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import JSON, DateTime, String, TypeDecorator, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from app.core.config import get_settings


class UTCDateTime(TypeDecorator):
    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime rejected; use timezone-aware UTC")
        return value.astimezone(UTC)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class Base(DeclarativeBase):
    type_annotation_map = {dict: JSON, list: JSON, datetime: UTCDateTime}


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_id() -> str:
    return str(uuid.uuid4())


class CommonColumns:
    """Common fields required on all records (spec section 4)."""
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    schema_version: Mapped[str] = mapped_column(String(16), default="1")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    source_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    provenance: Mapped[str] = mapped_column(String(24), default="SYNTHETIC")


_engine = None
_SessionLocal = None


def _make_engine(url: str):
    kw: dict = {"future": True}
    if url.startswith("sqlite"):
        path = url.split("sqlite:///")[-1]
        if path and path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        kw["connect_args"] = {"check_same_thread": False, "timeout": 30}
        kw["isolation_level"] = None  # we emit BEGIN ourselves
    else:
        kw["pool_size"] = 10
        kw["max_overflow"] = 10
        kw["pool_pre_ping"] = True
    eng = create_engine(url, **kw)
    if url.startswith("sqlite"):
        @event.listens_for(eng, "connect")
        def _pragmas(dbapi_conn, _):
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA busy_timeout=30000")
            cur.close()

        @event.listens_for(eng, "begin")
        def _begin(conn):
            conn.exec_driver_sql("BEGIN IMMEDIATE")
    return eng


def get_engine():
    global _engine, _SessionLocal
    if _engine is None:
        _engine = _make_engine(get_settings().database_url)
        _SessionLocal = sessionmaker(_engine, expire_on_commit=False, class_=Session)
    return _engine


def reset_engine(url: str | None = None):
    """Test helper: dispose and optionally re-point (env var must be set by caller for url)."""
    global _engine, _SessionLocal
    if _engine is not None:
        _engine.dispose()
    _engine = _SessionLocal = None
    if url:
        os.environ["AETHER_DATABASE_URL"] = url
        get_settings.cache_clear()


def SessionLocal() -> Session:
    get_engine()
    return _SessionLocal()  # type: ignore[misc]


def is_sqlite() -> bool:
    return get_engine().dialect.name == "sqlite"


def lock_for_update(query):
    """Row lock on PostgreSQL; no-op on SQLite (BEGIN IMMEDIATE already serialises writers)."""
    return query if is_sqlite() else query.with_for_update()


def import_all_models() -> None:
    """Import every engine's models so Base.metadata is complete (missing engines are skipped)."""
    import importlib

    import app.db.models_core  # noqa: F401
    for key in ("regulatory", "contracts", "risk", "collateral", "compliance", "evidence"):
        try:
            importlib.import_module(f"app.engines.{key}.models")
        except ModuleNotFoundError as exc:
            if exc.name not in (f"app.engines.{key}", f"app.engines.{key}.models"):
                raise


def create_all() -> None:
    import_all_models()
    Base.metadata.create_all(get_engine())
