"""Restore a backup into an ISOLATED target directory and verify it BEFORE declaring it usable.

    python scripts/restore.py <backup_dir> --target <empty dir> [--tenant T ...]

Checks: (1) every file's SHA-256 against manifest.json, (2) every tenant hash chain, (3) each chain against ALL signed
exported checkpoints with the retained public keys (detects truncation), (4) every vault object hash.
Exit code 0 only if all pass. Never touches the live database or live object store. Use the restored tree by pointing
AETHER_DATABASE_URL / AETHER_OBJECT_STORE_DIR / AETHER_SIGNING_KEY_DIR at <target>/db/aether.db, <target>/objects, <target>/keys.
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
    a = ap.parse_args(argv)
    src, tgt = Path(a.backup), Path(a.target)
    if tgt.exists() and any(tgt.iterdir()):
        print(f"refusing to restore into non-empty {tgt}", file=sys.stderr)
        return 2
    man = json.loads((src / "manifest.json").read_text())
    problems = []
    for name, meta in man["files"].items():
        f = src / name
        if not f.exists():
            problems.append({"problem": "BACKUP_FILE_MISSING", "file": name})
        elif _sha(f) != meta["sha256"]:
            problems.append({"problem": "BACKUP_FILE_HASH_MISMATCH", "file": name})
    if problems:
        print(json.dumps({"restored": False, "problems": problems}, indent=2))
        return 1
    shutil.copytree(src, tgt, dirs_exist_ok=True)
    os.environ["AETHER_DATABASE_URL"] = f"sqlite:///{(tgt / 'db' / 'aether.db').as_posix()}"
    os.environ["AETHER_OBJECT_STORE_DIR"] = str(tgt / "objects")
    os.environ["AETHER_SIGNING_KEY_DIR"] = str(tgt / "keys")
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
    print(json.dumps({"restored": ok, "target": str(tgt), "migration_revision": man.get("migration_revision"),
                      "tenants": report, "problems": problems}, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
