"""E-A09 backup/restore preserves evidence; restore verifies anchors; corrupted backup refused."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.core.config import get_settings
from app.db.base import SessionLocal
from app.engines.evidence import checkpoints as cps
from app.engines.evidence import objects
from tests.engines.evidence.conftest import add_events

ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = ROOT / "backend" / "scripts"


def _run(script, *args):
    env = {**os.environ, "AETHER_OBJECT_STORE_DIR": get_settings().object_store_dir,
           "AETHER_SIGNING_KEY_DIR": get_settings().signing_key_dir}
    return subprocess.run([sys.executable, str(SCRIPTS / script), *args], capture_output=True, text=True, env=env, cwd=ROOT)


IS_PG = os.environ.get("AETHER_TEST_DATABASE_URL", "").startswith("postgresql")


@pytest.mark.skipif(IS_PG, reason="SQLite file backup path; the PostgreSQL path is covered by the *_pg tests below")
def test_backup_restore_roundtrip_and_corruption(tenant, tmp_path):
    with SessionLocal() as s:
        h = objects.store_object(s, tenant, b"source-document-bytes", "SOURCE_DOCUMENT")
        add_events(s, tenant, 5, input_hashes=[h])
        cps.create_checkpoint(s, tenant)
        s.commit()
    b = _run("backup.py", "--out", str(tmp_path / "bk"))
    assert b.returncode == 0, b.stdout + b.stderr
    m = json.loads((tmp_path / "bk" / "manifest.json").read_text())
    assert m["private_key_included"] is False and not any(f.endswith(".pem") for f in m["files"])
    r = _run("restore.py", str(tmp_path / "bk"), "--target", str(tmp_path / "rs"), "--tenant", tenant)
    assert r.returncode == 0, r.stdout + r.stderr
    rep = json.loads(r.stdout)
    assert rep["restored"] and rep["tenants"][tenant]["events"] == 5 and rep["tenants"][tenant]["anchor_status"] == "ANCHORED"
    assert rep["tenants"][tenant]["objects_valid"]
    # restoring over a non-empty target is refused (live evidence is never overwritten)
    assert _run("restore.py", str(tmp_path / "bk"), "--target", str(tmp_path / "rs")).returncode == 2
    # corrupt an object in the backup => refused before anything is declared usable
    blob = next((tmp_path / "bk" / "objects").rglob(h))
    blob.write_bytes(b"corrupt")
    bad = _run("restore.py", str(tmp_path / "bk"), "--target", str(tmp_path / "rs2"), "--tenant", tenant)
    assert bad.returncode == 1 and "BACKUP_FILE_HASH_MISMATCH" in bad.stdout


# ------------------------------------------------------------------ PostgreSQL path (pg_dump / pg_restore)
pg_only = pytest.mark.skipif(not IS_PG, reason="PostgreSQL backup/restore test: set AETHER_TEST_DATABASE_URL to a PostgreSQL URL")


@pytest.fixture
def pg_restore_db():
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url
    name = "restore_" + os.urandom(4).hex()
    yield name
    u = make_url(os.environ["AETHER_TEST_DATABASE_URL"]).set(database="postgres")
    e = create_engine(u, isolation_level="AUTOCOMMIT")
    with e.connect() as c:
        c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    e.dispose()


def _seed(tenant):
    with SessionLocal() as s:
        h = objects.store_object(s, tenant, b"source-document-bytes", "SOURCE_DOCUMENT")
        add_events(s, tenant, 5, input_hashes=[h])
        cps.create_checkpoint(s, tenant)
        s.commit()
    return h


@pg_only
def test_pg_backup_restore_into_new_database_verifies_and_refuses_unsafe_targets(tenant, tmp_path, pg_restore_db):
    h = _seed(tenant)
    b = _run("backup.py", "--out", str(tmp_path / "bk"))
    assert b.returncode == 0, b.stdout + b.stderr
    m = json.loads((tmp_path / "bk" / "manifest.json").read_text())
    assert m["database"]["engine"] == "postgresql" and m["database"]["file"] == "db/aether.pgdump"
    assert m["private_key_included"] is False and not any(f.endswith(".pem") for f in m["files"])
    pw = make_url_password()
    assert pw not in json.dumps(m) and pw not in b.stdout + b.stderr          # password never leaks into manifest/logs
    r = _run("restore.py", str(tmp_path / "bk"), "--target", str(tmp_path / "rs"), "--pg-restore-db", pg_restore_db, "--tenant", tenant)
    assert r.returncode == 0, r.stdout + r.stderr
    rep = json.loads(r.stdout)
    assert pw not in r.stdout + r.stderr
    assert rep["restored"] and rep["engine"] == "postgresql" and rep["database"].split("/")[-1] == pg_restore_db
    t = rep["tenants"][tenant]
    assert t["events"] == 5 and t["chain_valid"] and t["anchor_status"] == "ANCHORED" and t["objects_valid"]
    # isolation: the restore is a different database from the live one
    assert pg_restore_db not in os.environ["AETHER_TEST_DATABASE_URL"]
    # a database that already contains tables is refused (second restore into the same name), as is a non-empty directory
    again = _run("restore.py", str(tmp_path / "bk"), "--target", str(tmp_path / "rs-b"), "--pg-restore-db", pg_restore_db, "--tenant", tenant)
    assert again.returncode == 2 and "already contains" in again.stderr
    assert _run("restore.py", str(tmp_path / "bk"), "--target", str(tmp_path / "rs"), "--pg-restore-db", pg_restore_db).returncode == 2
    # never over the live database named in AETHER_DATABASE_URL
    live = os.environ["AETHER_TEST_DATABASE_URL"].rsplit("/", 1)[-1]
    over = _run("restore.py", str(tmp_path / "bk"), "--target", str(tmp_path / "rs-c"), "--pg-restore-db", live)
    assert over.returncode == 2
    # restore needs a new database name
    assert _run("restore.py", str(tmp_path / "bk"), "--target", str(tmp_path / "rs-d")).returncode == 2


@pg_only
def test_pg_corrupted_dump_refused_before_any_restore(tenant, tmp_path, pg_restore_db):
    _seed(tenant)
    assert _run("backup.py", "--out", str(tmp_path / "bk")).returncode == 0
    dump = tmp_path / "bk" / "db" / "aether.pgdump"
    raw = bytearray(dump.read_bytes())
    raw[len(raw) // 2] ^= 0xFF
    dump.write_bytes(bytes(raw))
    bad = _run("restore.py", str(tmp_path / "bk"), "--target", str(tmp_path / "rs"), "--pg-restore-db", pg_restore_db, "--tenant", tenant)
    assert bad.returncode == 1 and "BACKUP_FILE_HASH_MISMATCH" in bad.stdout
    # the new database must not even have been created
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url
    e = create_engine(make_url(os.environ["AETHER_TEST_DATABASE_URL"]).set(database="postgres"), isolation_level="AUTOCOMMIT")
    with e.connect() as c:
        assert c.execute(text("SELECT 1 FROM pg_database WHERE datname=:n"), {"n": pg_restore_db}).first() is None
    e.dispose()


@pg_only
def test_pg_backup_of_tampered_chain_restores_but_is_reported_unusable(tenant, tmp_path, pg_restore_db):
    """A chain modified BEFORE the backup is faithfully dumped; restore verification must catch it (exit 1)."""
    from sqlalchemy import text
    _seed(tenant)
    with SessionLocal() as s:
        s.execute(text("UPDATE ev_events SET action='TAMPERED' WHERE tenant_id=:t AND sequence=3"), {"t": tenant})
        s.commit()
    assert _run("backup.py", "--out", str(tmp_path / "bk")).returncode == 0
    r = _run("restore.py", str(tmp_path / "bk"), "--target", str(tmp_path / "rs"), "--pg-restore-db", pg_restore_db, "--tenant", tenant)
    assert r.returncode == 1
    rep = json.loads(r.stdout)
    assert rep["restored"] is False and any(p["problem"] == "RESTORE_VERIFICATION_FAILED" for p in rep["problems"])
    assert rep["tenants"][tenant]["chain_valid"] is False


def make_url_password() -> str:
    from sqlalchemy.engine import make_url
    return make_url(os.environ["AETHER_TEST_DATABASE_URL"]).password or "no-password"
