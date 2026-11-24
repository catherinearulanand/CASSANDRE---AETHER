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


@pytest.mark.skipif(not os.environ.get("AETHER_TEST_DATABASE_URL", "sqlite").startswith("sqlite"),
                    reason="backup.py/restore.py copy the SQLite file only; PostgreSQL backup (pg_dump + object store) is NOT implemented")
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
