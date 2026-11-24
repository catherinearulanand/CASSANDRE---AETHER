"""Schema/migration guard. PostgreSQL deployments must be migrated with Alembic; the app refuses to start on a
mismatch (spec 16: 'startup detects schema mismatch and fails visibly'). SQLite local/test runs keep create_all."""
from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text

from app.db.base import get_engine

BACKEND_DIR = Path(__file__).resolve().parents[2]


def head_revisions() -> set[str]:
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "migrations"))
    return set(ScriptDirectory.from_config(cfg).get_heads())


def current_revisions() -> set[str]:
    with get_engine().connect() as c:
        try:
            return {r[0] for r in c.execute(text("SELECT version_num FROM alembic_version"))}
        except Exception:  # table absent => never migrated
            return set()


def assert_schema_current() -> None:
    cur, head = current_revisions(), head_revisions()
    if cur != head:
        raise RuntimeError(f"Database schema revision {sorted(cur) or 'NONE'} != migration head {sorted(head)}. "
                           f"Run `alembic upgrade head` (cd backend) before starting the API.")
