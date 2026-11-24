"""A3 VECTOR synthetic fixture seeding. Reads the committed artefacts under fixtures/risk (sha256-listed in manifest.json).
Idempotent: re-running with unchanged fixtures changes nothing."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from app.core.config import REPO_ROOT
from app.engines.risk import services as svc

FIX = REPO_ROOT / "fixtures" / "risk"
SEED_CTX = {"actor_id": "SYSTEM", "correlation_id": "seed-risk-fixtures", "source_mode": "REPLAY"}


def verify_manifest() -> None:
    man = json.loads((FIX / "manifest.json").read_text(encoding="utf-8"))
    for f in man["files"]:
        got = hashlib.sha256((FIX / f["path"]).read_bytes()).hexdigest()
        if got != f["sha256"]:
            raise RuntimeError(f"fixture {f['path']} hash mismatch (manifest {f['sha256'][:12]} vs actual {got[:12]})")


def seed_fixtures(session, tenant_id: str, *, include_bad_total: bool = True) -> dict:
    verify_manifest()
    out = {"snapshots": 0, "clearing_files": 0}
    for s in json.loads((FIX / "snapshots.json").read_text(encoding="utf-8")):
        svc.pin_snapshot(session, tenant_id, s["snapshot_id"], datetime.fromisoformat(s["data_as_of"].replace("Z", "+00:00")).astimezone(timezone.utc),
                         s["payload"], max_age_seconds=s["max_age_seconds"], ctx=SEED_CTX)
        out["snapshots"] += 1
    pf = json.loads((FIX / "portfolio.json").read_text(encoding="utf-8"))
    svc.import_portfolio(session, tenant_id, instruments=pf["instruments"], trades=pf["trades"], positions=pf["positions"], ctx=SEED_CTX)
    files = [("clearing_nse_001.csv", "DEMO_NSE"), ("clearing_ifsc_001.csv", "DEMO_IFSC")]
    if include_bad_total:
        files.append(("clearing_nse_badtotal.csv", "DEMO_NSE"))
    for name, venue in files:
        svc.import_clearing_file(session, tenant_id, csv_text=(FIX / name).read_text(encoding="utf-8"), venue=venue, ctx=SEED_CTX)
        out["clearing_files"] += 1
    return out
