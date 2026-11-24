"""G01 / R-A07 with the REAL risk (A3) and collateral (A4) engines and their own seeds (no test doubles).
Skipped (and reported as skipped) when those engines are not present in the checkout."""
import uuid

import pytest
from sqlalchemy import select

from app.core import services
from tests.engines.regulatory.helpers import EXPECTED, dt

G = EXPECTED["G01"]


def test_g01_end_to_end_with_real_risk_and_collateral_engines(env):
    import app.main  # noqa: F401  (mounts every engine so their services register)
    for name in ("risk.compute_margin", "collateral.coverage"):
        if not services.is_registered(name):
            pytest.skip(f"{name} not available in this checkout")
    try:
        from app.engines.collateral.fixtures import seed_fixtures as seed_col
        from app.engines.risk.fixtures import seed_fixtures as seed_risk
    except ImportError:
        pytest.skip("risk/collateral fixtures not available")
    from app.engines.regulatory import impact
    from app.engines.regulatory.fixtures import _principals, seed_fixtures
    from app.engines.regulatory.models import RegRule
    tenant = f"t-int-{uuid.uuid4().hex[:8]}"
    seed_fixtures(env.db, tenant)
    seed_risk(env.db, tenant)
    seed_col(env.db, tenant)
    env.db.flush()
    v2 = env.db.execute(select(RegRule).where(RegRule.tenant_id == tenant, RegRule.rule_key == "DEMO-ETD-MARGIN", RegRule.version == 2)).scalar_one().rule_id
    maker, _ = _principals(tenant)
    run = impact.run_impact(env.db, maker, {"as_of": dt("2026-08-17T04:00:00Z"), "hypothetical_rule_ids": [v2], "accounts": [
        {"account_id": "ACC-DEMO-NSE-001", "context": {"market_type": "ETD_DOMESTIC", "venue": "DEMO_NSE"}, "snapshot_id": "SNAP-ETD-001"}]})
    row = run.results["rows"][0]
    assert run.status == "COMPLETED", row
    assert (row["baseline_required"], row["hypothetical_required"], row["delta_required"]) == (G["baseline_required"], G["hypothetical_required"], G["delta_required"])
    assert (row["eligible_posted"], row["hypothetical_shortfall"], row["baseline_shortfall"]) == (G["eligible_posted"], G["hypothetical_shortfall"], G["baseline_shortfall"])
    assert run.results["official"] is False
