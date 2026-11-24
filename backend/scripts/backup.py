"""Backup the evidence-bearing state: database + content-addressed object store + exported checkpoints + PUBLIC keys.

    python scripts/backup.py --out <dir>

* SQLite only (consistent copy through the sqlite3 backup API). For PostgreSQL use `pg_dump` plus this script's
  object/checkpoint copy; the PostgreSQL path has NOT been exercised on this machine (no PostgreSQL available).
* The PRIVATE signing key (ev-*.pem) is deliberately NOT included: it must be protected and backed up separately.
* Encryption of the backup is NOT performed here; encrypt the output directory with your institutional tooling.
* Output layout (restore root): db/aether.db, objects/, checkpoints/, keys/ (public only), manifest.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.core.config import get_settings  # noqa: E402


def _sha(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    st = get_settings()
    if not st.database_url.startswith("sqlite:///"):
        print("backup.py supports SQLite only; use pg_dump for PostgreSQL (not exercised here).", file=sys.stderr)
        return 2
    out = Path(a.out)
    if out.exists() and any(out.iterdir()):
        print(f"refusing to write into non-empty {out}", file=sys.stderr)
        return 2
    (out / "db").mkdir(parents=True, exist_ok=True)
    src_db = Path(st.database_url.split("sqlite:///", 1)[1])
    src = sqlite3.connect(src_db)
    dst = sqlite3.connect(out / "db" / "aether.db")
    with dst:
        src.backup(dst)
    tables = {r[0] for r in dst.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    revision = None
    if "alembic_version" in tables:
        row = dst.execute("SELECT version_num FROM alembic_version").fetchone()
        revision = row[0] if row else None
    dst.close()
    src.close()
    obj, cps = Path(st.object_store_dir), Path(st.signing_key_dir).parent / "checkpoints"
    if obj.exists():
        shutil.copytree(obj, out / "objects", ignore=shutil.ignore_patterns(".tmp-*", ".probe-*"))
    if cps.exists():
        shutil.copytree(cps, out / "checkpoints")
    keys_dir = Path(st.signing_key_dir)
    (out / "keys").mkdir(exist_ok=True)
    for pub in keys_dir.glob("ev-*.pub") if keys_dir.exists() else []:
        shutil.copy2(pub, out / "keys" / pub.name)
    files = {str(p.relative_to(out)).replace("\\", "/"): {"sha256": _sha(p), "size": p.stat().st_size}
             for p in sorted(out.rglob("*")) if p.is_file()}
    manifest = {"format": "aether-backup/1", "created_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "schema_tables": sorted(tables), "migration_revision": revision or "none-recorded (no alembic_version table; create_all baseline)",
                "private_key_included": False, "encrypted": False, "files": files}
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True))
    print(json.dumps({"backup": str(out), "files": len(files), "migration_revision": manifest["migration_revision"]}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
