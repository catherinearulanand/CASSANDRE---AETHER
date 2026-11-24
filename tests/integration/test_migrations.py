"""Alembic: upgrade a brand-new PostgreSQL database from empty to head, assert no model/migration drift, and that
the API refuses to start on an unmigrated database. PostgreSQL only (skipped on SQLite with a stated reason)."""
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

URL = os.environ.get("AETHER_TEST_DATABASE_URL", "")
BACKEND = Path(__file__).resolve().parents[2] / "backend"
pytestmark = pytest.mark.skipif(not URL.startswith("postgresql"), reason="migration test needs PostgreSQL (set AETHER_TEST_DATABASE_URL)")


@pytest.fixture
def fresh_db():
    name = "mig_" + uuid.uuid4().hex[:8]
    admin = create_engine(URL, isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f'CREATE DATABASE "{name}"'))
    url = make_url(URL).set(database=name).render_as_string(hide_password=False)
    yield url
    with admin.connect() as c:
        c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))


def run(args, url, code=None):
    env = {**os.environ, "AETHER_DATABASE_URL": url, "AETHER_AUTO_CREATE_SCHEMA": "false"}
    return subprocess.run([sys.executable, *args], cwd=BACKEND, env=env, capture_output=True, text=True, timeout=300)


def test_upgrade_from_empty_has_no_drift_and_downgrade_roundtrips(fresh_db):
    up = run(["-m", "alembic", "upgrade", "head"], fresh_db)
    assert up.returncode == 0, up.stderr
    chk = run(["-m", "alembic", "check"], fresh_db)
    assert chk.returncode == 0 and "No new upgrade operations" in (chk.stdout + chk.stderr), chk.stdout + chk.stderr
    assert run(["-m", "alembic", "downgrade", "base"], fresh_db).returncode == 0
    assert run(["-m", "alembic", "upgrade", "head"], fresh_db).returncode == 0


def test_api_refuses_to_start_on_unmigrated_database(fresh_db):
    r = run(["-c", "import app.main"], fresh_db)
    assert r.returncode != 0 and "alembic upgrade head" in r.stderr


def test_api_starts_on_migrated_database(fresh_db):
    assert run(["-m", "alembic", "upgrade", "head"], fresh_db).returncode == 0
    assert run(["-c", "import app.main"], fresh_db).returncode == 0
