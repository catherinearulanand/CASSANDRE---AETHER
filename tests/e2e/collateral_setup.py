"""Prepare a PRIVATE demo database for the collateral browser flows (does NOT touch .demo-credentials).

Usage (AETHER_DATABASE_URL must point at YOUR database; AETHER_AUTO_CREATE_SCHEMA=true for dev servers):
    python tests/e2e/collateral_setup.py
Writes var/a4-creds.json (random passwords, gitignored) and var/a4-ids.json (real risk-engine result ids).
Everything is computed by the real engines through the service registry; nothing is hard-coded.
"""
import json
import secrets
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "backend" / "scripts"))

from sqlalchemy import select  # noqa: E402

from app.auth.security import hash_password  # noqa: E402
from app.core import services  # noqa: E402
from app.db.base import SessionLocal, create_all  # noqa: E402
from app.db.models_core import Tenant, User  # noqa: E402

TENANT = "tenant-a"
OTHER = "tenant-b"
ROLES = {"viewer": ["viewer"], "maker": ["maker"], "checker": ["checker"], "compliance": ["compliance_officer"]}
AS_OF = datetime(2026, 8, 17, 4, 0, tzinfo=UTC)


def main():
    create_all()
    import app.main  # noqa: F401  (registers every engine's services)
    from seed_demo import seed_engines

    creds_path = ROOT / "var" / "a4-creds.json"
    creds = json.loads(creds_path.read_text()) if creds_path.exists() else {}
    with SessionLocal() as s:
        for t in (TENANT, OTHER):
            if s.get(Tenant, t) is None:
                s.add(Tenant(tenant_id=t, name=t + " (synthetic)"))
            for u, roles in ROLES.items():
                if s.execute(select(User).where(User.tenant_id == t, User.username == u)).scalar_one_or_none():
                    continue
                pw = secrets.token_urlsafe(12)
                s.add(User(tenant_id=t, username=u, display_name=f"{u} ({t})", password_hash=hash_password(pw), roles=roles,
                           entitlements=["OTC", "ETD_DOMESTIC", "ETD_IFSC"]))
                creds.setdefault(t, {})[u] = pw
        s.commit()
    creds_path.write_text(json.dumps(creds, indent=1))
    seed_engines(TENANT)

    now = datetime.now(UTC)
    ids = {}
    with SessionLocal() as s:
        terms = services.call("contracts.resolve_terms", s, TENANT, "NS-MER-NWD-001", AS_OF, now)
        assert terms["status"] == "RESOLVED", terms
        otc = {"market_type": "OTC", "netting_set_id": "NS-MER-NWD-001", "as_of": AS_OF.isoformat(), "known_at": now.isoformat(),
               "existing_vm_balance": {"PARTY_A": "0", "PARTY_B": "0"}}
        shock = services.call("risk.compute_margin", s, TENANT, otc, "SNAP-SHOCK-001", terms["terms_hash"], None, "OTC_VM_SIMPLE_V1")
        quiet = services.call("risk.compute_margin", s, TENANT, otc, "SNAP-QUIET-001", terms["terms_hash"], None, "OTC_VM_SIMPLE_V1")
        assert shock["status"] == "OK" and shock["rounded_amount"]["amount"] == "9500000", shock
        ids.update(otc_post=shock["result_id"], otc_zero=quiet["result_id"], otc_terms_hash=terms["terms_hash"])

        from app.engines.regulatory.models import RegRule
        ctx = {"market_type": "ETD_DOMESTIC", "venue": "DEMO_NSE", "account_id": "ACC-DEMO-NSE-001", "currency": "INR", "as_of": AS_OF.isoformat()}
        v2 = s.execute(select(RegRule).where(RegRule.tenant_id == TENANT, RegRule.rule_key == "DEMO-ETD-MARGIN", RegRule.version == 2)).scalar_one()
        base_rules = services.call("regulatory.resolve_rules", s, TENANT, ctx, AS_OF, None)
        hyp_rules = services.call("regulatory.resolve_rules", s, TENANT, ctx, AS_OF, None, [v2.rule_id])
        base = services.call("risk.compute_margin", s, TENANT, ctx, "SNAP-ETD-001", None, base_rules["rule_set_hash"], "ETD_IMPORTED_V1")
        hyp = services.call("risk.compute_margin", s, TENANT, ctx, "SNAP-ETD-001", None, hyp_rules["rule_set_hash"], "ETD_IMPORTED_V1")
        assert base["status"] == "OK" and hyp["status"] == "OK" and hyp["hypothetical"] is True, (base, hyp)
        ids.update(etd_base=base["result_id"], etd_hyp=hyp["result_id"], etd_hyp_rules=hyp_rules["rule_set_hash"])
        s.commit()
    ids.update(otc_snapshot="INV-OTC-001", etd_snapshot="INV-ETD-001", deadline="2026-08-19T04:00")
    (ROOT / "var" / "a4-ids.json").write_text(json.dumps(ids, indent=1))
    print("ready:", {k: (v[:8] + "…" if isinstance(v, str) and len(v) > 20 else v) for k, v in ids.items()})


if __name__ == "__main__":
    main()
