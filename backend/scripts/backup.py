"""Backup the evidence-bearing state: database + content-addressed object store + exported checkpoints + PUBLIC keys.

    python scripts/backup.py --out <dir>

* SQLite: consistent copy through the sqlite3 backup API.
* PostgreSQL: custom-format `pg_dump` (local client, else `docker exec <container>`; see backup_pg.py) written to db/aether.pgdump.
  The password travels only in the PGPASSWORD environment variable, never argv/logs/manifest.
* The PRIVATE signing key (ev-*.pem) is deliberately NOT included: it must be protected and backed up separately.
* Encryption of the backup is NOT performed here; encrypt the output directory with your institutional tooling.
* Output layout (restore root): db/aether.db | db/aether.pgdump, objects/, checkpoints/, keys/ (public only), manifest.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
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
    ap.add_argument("--pg-container", default=os.environ.get("AETHER_PG_CONTAINER", "aether-pg"),
                    help="docker container running PostgreSQL, used only when no local pg_dump is on PATH")
    a = ap.parse_args(argv)
    st = get_settings()
    out = Path(a.out)
    if out.exists() and any(out.iterdir()):
        print(f"refusing to write into non-empty {out}", file=sys.stderr)
        return 2
    (out / "db").mkdir(parents=True, exist_ok=True)
    db_info: dict
    if st.database_url.startswith("sqlite:///"):
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
        db_info = {"engine": "sqlite", "file": "db/aether.db"}
    elif st.database_url.startswith("postgresql"):
        import backup_pg as pg
        try:
            pg.dump(st.database_url, a.pg_container, out / "db" / "aether.pgdump")
            revision = pg.alembic_revision(st.database_url)
            from sqlalchemy import create_engine, text
            e = create_engine(st.database_url)
            with e.connect() as c:
                tables = {r[0] for r in c.execute(text("SELECT table_name FROM information_schema.tables WHERE table_schema='public'"))}
            e.dispose()
        except pg.PgError as exc:
            print(f"PostgreSQL backup failed: {exc}", file=sys.stderr)
            return 1
        db_info = {"engine": "postgresql", "file": "db/aether.pgdump", "format": "pg_dump custom (-Fc)",
                   "source_database": pg.parse(st.database_url).database}
    else:
        print("unsupported database URL (SQLite and PostgreSQL only)", file=sys.stderr)
        return 2
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
                "database": db_info, "private_key_included": False, "encrypted": False, "files": files}
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True))
    print(json.dumps({"backup": str(out), "files": len(files), "migration_revision": manifest["migration_revision"]}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
