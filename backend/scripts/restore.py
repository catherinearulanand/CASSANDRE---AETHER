"""Restore a backup into an ISOLATED target and verify it BEFORE declaring it usable.

SQLite backup:      python scripts/restore.py <backup_dir> --target <empty dir> [--tenant T ...]
PostgreSQL backup:  AETHER_DATABASE_URL=<server admin url> python scripts/restore.py <backup_dir> --target <empty dir> \
                        --pg-restore-db <new_database_name> [--pg-container aether-pg] [--tenant T ...]

Checks: (1) every backup file's SHA-256 against manifest.json (nothing is restored if any differ), (2) restore into a NEW
database / empty directory (a database that already contains tables, or a non-empty directory, is REFUSED),
(3) the restored alembic revision equals the revision recorded in the manifest, (4) every tenant hash chain,
(5) each chain against ALL signed exported checkpoints with the retained public keys (detects truncation),
(6) every vault object hash. Exit code 0 only if all pass. The live database / object store are never written.
For PostgreSQL the URL in AETHER_DATABASE_URL only supplies host/credentials; its database name is never modified, and
the password is passed to pg_restore only through the PGPASSWORD environment variable.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))


def _sha(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("backup")
    ap.add_argument("--target", required=True)
    ap.add_argument("--tenant", action="append", help="verify only these tenants (default: all)")
    ap.add_argument("--pg-restore-db", help="name of the NEW database to restore a PostgreSQL backup into")
    ap.add_argument("--pg-container", default=os.environ.get("AETHER_PG_CONTAINER", "aether-pg"))
    a = ap.parse_args(argv)
    src, tgt = Path(a.backup), Path(a.target)
    if tgt.exists() and any(tgt.iterdir()):
        print(f"refusing to restore into non-empty {tgt}", file=sys.stderr)
        return 2
    man = json.loads((src / "manifest.json").read_text())
    db_info = man.get("database") or {"engine": "sqlite", "file": "db/aether.db"}
    problems: list[dict] = []
    for name, meta in man["files"].items():
        f = src / name
        if not f.exists():
            problems.append({"problem": "BACKUP_FILE_MISSING", "file": name})
        elif _sha(f) != meta["sha256"]:
            problems.append({"problem": "BACKUP_FILE_HASH_MISMATCH", "file": name})
    if problems:
        print(json.dumps({"restored": False, "problems": problems}, indent=2))
        return 1

    os.environ["AETHER_OBJECT_STORE_DIR"] = str(tgt / "objects")
    os.environ["AETHER_SIGNING_KEY_DIR"] = str(tgt / "keys")
    restored_url_display: str
    if db_info["engine"] == "postgresql":
        import backup_pg as pg
        if not a.pg_restore_db:
            print("a PostgreSQL backup needs --pg-restore-db <new database name>", file=sys.stderr)
            return 2
        server = os.environ.get("AETHER_DATABASE_URL", "")
        try:
            u = pg.parse(server).set(database=a.pg_restore_db)
        except pg.PgError:
            print("AETHER_DATABASE_URL must be a PostgreSQL URL (server + credentials) for restoring", file=sys.stderr)
            return 2
        if u.database == pg.parse(server).database:
            print("refusing to restore over the database named in AETHER_DATABASE_URL; choose a NEW name", file=sys.stderr)
            return 2
        existing = pg.database_table_count(u)
        if existing:
            print(f"refusing to restore into database {u.database!r}: it already contains {existing} table(s)", file=sys.stderr)
            return 2
        shutil.copytree(src, tgt, dirs_exist_ok=True)
        try:
            if existing is None:
                pg.create_database(u)
            pg.restore_into(u.render_as_string(hide_password=False), a.pg_container, tgt / db_info["file"])
        except pg.PgError as exc:
            print(json.dumps({"restored": False, "problems": [{"problem": "PG_RESTORE_FAILED", "detail": str(exc)[:400]}]}, indent=2))
            return 1
        full_url = u.render_as_string(hide_password=False)
        os.environ["AETHER_DATABASE_URL"] = full_url
        restored_url_display = pg.redact(u)
        revision = pg.alembic_revision(full_url)
    else:
        shutil.copytree(src, tgt, dirs_exist_ok=True)
        os.environ["AETHER_DATABASE_URL"] = f"sqlite:///{(tgt / 'db' / 'aether.db').as_posix()}"
        restored_url_display = f"sqlite:///{(tgt / 'db' / 'aether.db').as_posix()}"
        import sqlite3
        c = sqlite3.connect(tgt / "db" / "aether.db")
        try:
            row = c.execute("SELECT version_num FROM alembic_version").fetchone()
            revision = row[0] if row else None
        except sqlite3.OperationalError:
            revision = None
        c.close()

    recorded = man.get("migration_revision")
    if recorded and not str(recorded).startswith("none-recorded") and revision != recorded:
        problems.append({"problem": "MIGRATION_REVISION_MISMATCH", "restored": revision, "manifest": recorded})
    elif (not recorded or str(recorded).startswith("none-recorded")) and revision is not None:
        problems.append({"problem": "MIGRATION_REVISION_MISMATCH", "restored": revision, "manifest": recorded})
    warnings = []
    try:
        from app.db.migrations import head_revisions
        if revision is not None and revision not in head_revisions():
            warnings.append(f"restored schema revision {revision} is behind code head {sorted(head_revisions())}: run alembic upgrade head before serving")
    except Exception:  # noqa: BLE001 - informational only
        pass

    from sqlalchemy import select

    from app.db.base import SessionLocal
    from app.engines.evidence.checkpoints import verify_against_exported
    from app.engines.evidence.models import EvChainHead
    from app.engines.evidence.objects import verify_objects

    report = {}
    with SessionLocal() as s:
        tenants = a.tenant or [r[0] for r in s.execute(select(EvChainHead.tenant_id)).all()]
        for t in tenants:
            chain = verify_against_exported(s, t)
            objs = verify_objects(s, t)
            report[t] = {"events": chain["events"], "chain_valid": chain["valid"], "anchor_status": chain["anchor_status"],
                         "objects": objs["objects"], "objects_valid": objs["valid"],
                         "problems": chain["problems"] + objs["problems"]}
            if not chain["valid"] or not objs["valid"]:
                problems.append({"tenant": t, "problem": "RESTORE_VERIFICATION_FAILED"})
            if chain["anchor_status"] != "ANCHORED":
                problems.append({"tenant": t, "problem": "NO_TRUSTED_ANCHOR_RESTORED"})
    ok = not problems
    print(json.dumps({"restored": ok, "engine": db_info["engine"], "target": str(tgt), "database": restored_url_display,
                      "migration_revision": revision, "manifest_revision": recorded, "warnings": warnings,
                      "tenants": report, "problems": problems}, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
