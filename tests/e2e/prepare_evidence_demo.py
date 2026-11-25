"""Prepare a DEMO database for the Evidence browser tests (A6).

    AETHER_DATABASE_URL=<db> AETHER_AUTO_CREATE_SCHEMA=true python tests/e2e/prepare_evidence_demo.py

Creates tenants tenant-a / tenant-b, users with RANDOM passwords written to var/a6-creds.json (gitignored; it does NOT touch the shared
.demo-credentials), seeds every engine's fixtures for tenant-a, and records three runs with real engine calls under their own
correlation ids: G01 (ETD shortfall + case + proposal), G03 (OTC rate shock) and one run with only RUN_CREATED (lineage must show
explicit missing links). Finishes with a full signed checkpoint. Prints the ids as JSON."""
import json
import secrets
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "backend" / "scripts"))

from sqlalchemy import select  # noqa: E402

from app.auth.security import hash_password  # noqa: E402
from app.core import services  # noqa: E402
from app.db.base import SessionLocal, create_all  # noqa: E402
from app.db.models_core import Run, Tenant, User  # noqa: E402

AS_OF = datetime(2026, 8, 17, 4, 0, tzinfo=UTC)
USERS = [("viewer", ["viewer"]), ("maker", ["maker"]), ("checker", ["checker"]), ("auditor", ["auditor"]),
         ("compliance", ["compliance_officer"]), ("admin", ["tenant_admin"])]


def _run(s, tenant, scenario, market, corr, only_created=False):
    rid = str(uuid.uuid4())
    s.add(Run(run_id=rid, tenant_id=tenant, scenario_id=scenario, market=market, as_of=AS_OF, correlation_id=corr, created_by="demo-maker"))
    s.flush()
    services.call("evidence.append", s, tenant, engine="platform", action="RUN_CREATED", actor_id="demo-maker", actor_type="USER",
                  actor_role="maker", entity_type="run", entity_id=rid, correlation_id=corr, effective_at=AS_OF,
                  payload={"scenario_id": scenario, "market": market})
    return rid


def main():
    create_all()
    import app.main  # noqa: F401  (mounts engines, registers services)
    from seed_demo import seed_engines
    creds = {}
    with SessionLocal() as s:
        for t in ("tenant-a", "tenant-b"):
            if s.get(Tenant, t) is None:
                s.add(Tenant(tenant_id=t, name=t))
            for u, roles in USERS:
                if s.execute(select(User).where(User.tenant_id == t, User.username == u)).scalar_one_or_none():
                    continue
                pw = secrets.token_urlsafe(12)
                s.add(User(tenant_id=t, username=u, display_name=f"{u} ({t})", password_hash=hash_password(pw), roles=roles, entitlements=[]))
                creds.setdefault(t, {})[u] = pw
        s.commit()
    p = ROOT / "var" / "a6-creds.json"
    p.parent.mkdir(exist_ok=True)
    old = json.loads(p.read_text()) if p.exists() else {}
    for t, u in creds.items():
        old.setdefault(t, {}).update(u)
    p.write_text(json.dumps(old, indent=2))
    seed_engines("tenant-a")
    out = {}
    now = datetime.now(UTC)
    with SessionLocal() as s:
        from app.engines.regulatory.models import RegRule
        corr1 = "corr-g01-" + uuid.uuid4().hex[:6]
        out["g01_run"] = _run(s, "tenant-a", "ETD-DOM-G01", "ETD_DOMESTIC", corr1)
        v2 = s.execute(select(RegRule).where(RegRule.tenant_id == "tenant-a", RegRule.rule_key == "DEMO-ETD-MARGIN", RegRule.version == 2)).scalar_one()
        ctx = {"market_type": "ETD_DOMESTIC", "venue": "DEMO_NSE", "account_id": "ACC-DEMO-NSE-001", "currency": "INR",
               "as_of": AS_OF.isoformat(), "correlation_id": corr1}
        rules = services.call("regulatory.resolve_rules", s, "tenant-a", ctx, AS_OF, None, [v2.rule_id])
        margin = services.call("risk.compute_margin", s, "tenant-a", ctx, "SNAP-ETD-001", None, rules["rule_set_hash"], "ETD_IMPORTED_V1")
        services.call("compliance.evaluate", s, "tenant-a", ctx, "SNAP-ETD-001", rules["rule_set_hash"], margin["result_id"], correlation_id=corr1)
        prop = services.call("collateral.optimise", s, "tenant-a", margin["result_id"], "INV-ETD-001", None, None, "2026-08-19T04:00:00Z", 30,
                             actor_id="demo-maker", actor_type="SERVICE", correlation_id=corr1)
        out.update(g01_margin_result=margin["result_id"], g01_proposal=prop["proposal_id"])
        corr3 = "corr-g03-" + uuid.uuid4().hex[:6]
        out["g03_run"] = _run(s, "tenant-a", "OTC-SHOCK-G03", "OTC", corr3)
        terms = services.call("contracts.resolve_terms", s, "tenant-a", "NS-MER-NWD-001", AS_OF, now)
        m3 = services.call("risk.compute_margin", s, "tenant-a", {
            "market_type": "OTC", "netting_set_id": "NS-MER-NWD-001", "as_of": AS_OF.isoformat(), "known_at": now.isoformat(),
            "existing_vm_balance": {"PARTY_A": "0", "PARTY_B": "0"}, "correlation_id": corr3},
            "SNAP-SHOCK-001", terms["terms_hash"], None, "OTC_VM_SIMPLE_V1")
        out["g03_margin_result"] = m3["result_id"]
        out["bare_run"] = _run(s, "tenant-a", "ETD-IFSC-G20", "ETD_IFSC", "corr-bare-" + uuid.uuid4().hex[:6])
        # tenant-b has its own run so cross-tenant checks have something real to probe
        out["tenantb_run"] = _run(s, "tenant-b", "ETD-DOM-G01", "ETD_DOMESTIC", "corr-b-" + uuid.uuid4().hex[:6])
        for i in range(4):  # a few more events so a later truncation drill on tenant-b has a tail to remove
            services.call("evidence.append", s, "tenant-b", engine="platform", action="DEMO_EVENT", actor_id="demo", actor_type="SERVICE",
                          entity_type="demo", entity_id=f"d{i}", correlation_id="corr-b-demo")
        s.commit()
        from app.engines.evidence.checkpoints import create_checkpoint
        create_checkpoint(s, "tenant-a", mode="full")
        create_checkpoint(s, "tenant-b", mode="full")
        s.commit()
    print(json.dumps(out))


if __name__ == "__main__":
    main()
