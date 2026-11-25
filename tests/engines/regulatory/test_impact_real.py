"""R-A07 / G01 through the REAL risk, collateral and evidence services (no fakes): seeded demo tenant, hypothetical revision x1.25."""
import os
import sys
import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core import services
from app.db.base import SessionLocal
from app.engines.regulatory import impact
from app.engines.regulatory.models import RegRule
from tests.engines.regulatory.helpers import P, fake_services

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "backend", "scripts"))
from seed_demo import seed_engines  # noqa: E402

AS_OF = datetime(2026, 8, 17, 4, 0, tzinfo=UTC)
D = Decimal


@pytest.fixture(scope="module")
def tenant():
    t = "a1real-" + uuid.uuid4().hex[:8]
    seed_engines(t)
    return t


def body(db, tenant, **kw):
    v2 = db.execute(select(RegRule).where(RegRule.tenant_id == tenant, RegRule.rule_key == "DEMO-ETD-MARGIN", RegRule.version == 2)).scalar_one()
    acct = {"account_id": "ACC-DEMO-NSE-001", "snapshot_id": "SNAP-ETD-001", "terms_hash": None,
            "context": {"market_type": "ETD_DOMESTIC", "venue": "DEMO_NSE", "currency": "INR"}}
    return {"as_of": AS_OF, "hypothetical_rule_ids": [v2.rule_id], "accounts": [acct | kw.pop("acct", {})], **kw}


@pytest.mark.gold
def test_g01_impact_with_real_risk_and_collateral_and_feasibility(tenant):
    with SessionLocal() as s:
        run = impact.run_impact(s, P("maker-1", tenant, "maker"), body(
            s, tenant, include_feasibility=True,
            acct={"inventory_snapshot_id": "INV-ETD-001", "deadline": "2026-08-19T04:00:00Z", "horizon_days": 30}), corr="c-real")
        s.commit()
        row = run.results["rows"][0]
        assert run.status == "COMPLETED" and row["status"] == "COMPUTED" and row["currency"] == "INR", row
        # independent hand values (spec G01): 10,000,000 -> 12,500,000 (+25 lakh); posted 11,500,000 -> shortfall 10 lakh
        assert (row["baseline_required"], row["hypothetical_required"], row["delta_required"]) == ("10000000", "12500000", "2500000")
        assert row["eligible_posted"] == "11500000" and row["baseline_shortfall"] == "0" and row["hypothetical_shortfall"] == "1000000"
        f = row["feasibility"]
        assert f["status"] == "EVALUATED" and f["solver_status"] == "optimal" and D(f["required_coverage"]) == D("1000000")
        assert f["hypothetical"] is True and D(f["residual"]) == 0 and "cannot be approved" in f["note"]
        assert run.results["official"] is False and run.hypothetical
        assert run.results["liquidity_feasibility"].startswith("EVALUATED")


def test_feasibility_is_opt_in_and_never_faked(tenant):
    calls = []
    with SessionLocal() as s:
        real = services._REGISTRY["collateral.optimise"]
        with fake_services(**{"collateral.optimise": lambda *a, **k: calls.append(1) or real(*a, **k)}):
            off = impact.run_impact(s, P("maker-1", tenant, "maker"), body(s, tenant))
            assert calls == [] and "feasibility" not in off.results["rows"][0]
            assert off.results["liquidity_feasibility"].startswith("NOT_EVALUATED")
            miss = impact.run_impact(s, P("maker-1", tenant, "maker"), body(s, tenant, include_feasibility=True))
            assert calls == []  # inputs missing => explicit NOT_EVALUATED, optimiser not called
            assert miss.results["rows"][0]["feasibility"]["status"] == "NOT_EVALUATED"
            assert "inventory_snapshot_id" in miss.results["rows"][0]["feasibility"]["reason"]
        s.rollback()


def test_feasibility_unavailable_service_is_reported_not_hidden(tenant):
    with SessionLocal() as s, fake_services(**{"collateral.optimise": None}):
        run = impact.run_impact(s, P("maker-1", tenant, "maker"), body(
            s, tenant, include_feasibility=True, acct={"inventory_snapshot_id": "INV-ETD-001", "deadline": "2026-08-19T04:00:00Z", "horizon_days": 30}))
        f = run.results["rows"][0]["feasibility"]
        assert f["status"] == "NOT_EVALUATED" and "SERVICE_NOT_IMPLEMENTED" in f["reason"]
        s.rollback()


def test_replay_of_real_impact_run_matches_then_diverges_when_rule_changes(tenant):
    from sqlalchemy import text
    with SessionLocal() as s:
        impact.run_impact(s, P("maker-1", tenant, "maker"), body(s, tenant), corr="c-real-replay")
        s.commit()
        out = services.call("evidence.replay_run", s, tenant, "run-r", "c-real-replay", "auditor-1")
        assert out["status"] == "MATCH" and out["items"][0]["status"] == "MATCH", out
        # the hypothetical rule's parameter changes in the DB => re-resolved rule-set hash differs => DIVERGED
        v2 = s.execute(select(RegRule).where(RegRule.tenant_id == tenant, RegRule.rule_key == "DEMO-ETD-MARGIN", RegRule.version == 2)).scalar_one()
        s.execute(text("UPDATE reg_rules SET content_hash = 'tampered' WHERE rule_id = :i"), {"i": v2.rule_id})
        s.expire_all()
        out2 = services.call("evidence.replay_run", s, tenant, "run-r", "c-real-replay", "auditor-1")
        assert out2["status"] == "DIVERGED", out2
        s.rollback()
