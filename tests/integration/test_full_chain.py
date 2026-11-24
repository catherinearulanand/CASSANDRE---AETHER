"""Lead integration tests: REAL engines wired together via the service registry (no fakes), full demo seed.
Expected values are hand-derived in the spec golden catalogue (G01-G03), not produced by the code under test."""
import os
import sys
import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core import services
from app.db.base import SessionLocal
from app.engines.evidence import service as ev
from app.engines.evidence.models import EvEvent

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "backend", "scripts"))
from seed_demo import seed_engines  # noqa: E402

AS_OF = datetime(2026, 8, 17, 4, 0, tzinfo=UTC)
D = Decimal


@pytest.fixture(scope="module")
def tenant():
    t = "int-" + uuid.uuid4().hex[:8]
    seed_engines(t)
    return t


def known_now():
    return datetime.now(UTC)  # terms were frozen "now": bitemporal known_at must be >= freeze time


@pytest.mark.gold
def test_g02_g03_otc_chain_contracts_to_risk(tenant):
    with SessionLocal() as s:
        terms = services.call("contracts.resolve_terms", s, tenant, "NS-MER-NWD-001", AS_OF, known_now())
        assert terms["status"] == "RESOLVED" and terms["terms_hash"]
        base = {"market_type": "OTC", "netting_set_id": "NS-MER-NWD-001", "as_of": AS_OF.isoformat(),
                "known_at": known_now().isoformat(), "existing_vm_balance": {"PARTY_A": "0", "PARTY_B": "0"}}
        quiet = services.call("risk.compute_margin", s, tenant, base, "SNAP-QUIET-001", terms["terms_hash"], None, "OTC_VM_SIMPLE_V1")
        shock = services.call("risk.compute_margin", s, tenant, base, "SNAP-SHOCK-001", terms["terms_hash"], None, "OTC_VM_SIMPLE_V1")
        s.commit()
    assert quiet["status"] == "OK" and D(quiet["exposure"]["amount"]) == D("1500000") and D(quiet["rounded_amount"]["amount"]) == 0
    assert shock["status"] == "OK" and shock["direction"] == "POST"
    assert D(shock["exposure"]["amount"]) == D("19469510.12")          # G03 exposure
    assert D(shock["unrounded_amount"]["amount"]) == D("9469510.12")   # exposure - 10,000,000 threshold
    assert D(shock["rounded_amount"]["amount"]) == D("9500000")        # MTA 500k passed; UP to 100k increment


@pytest.mark.gold
def test_g21_stale_snapshot_blocks_material_result(tenant):
    with SessionLocal() as s:
        terms = services.call("contracts.resolve_terms", s, tenant, "NS-MER-NWD-001", AS_OF, known_now())
        ctx = {"market_type": "OTC", "netting_set_id": "NS-MER-NWD-001", "as_of": AS_OF.isoformat(),
               "known_at": known_now().isoformat(), "existing_vm_balance": {"PARTY_A": "0", "PARTY_B": "0"}}
        r = services.call("risk.compute_margin", s, tenant, ctx, "SNAP-STALE-001", terms["terms_hash"], None, "OTC_VM_SIMPLE_V1")
        s.commit()
    assert r["status"] == "BLOCKED" and r["blocking_reasons"]


@pytest.mark.gold
def test_g01_etd_chain_regulatory_risk_collateral_compliance(tenant):
    """Hypothetical rule revision (x1.25) -> INR 12,500,000 required; posted 11,500,000 -> shortfall 1,000,000 -> ONE case."""
    ctx = {"market_type": "ETD_DOMESTIC", "venue": "DEMO_NSE", "account_id": "ACC-DEMO-NSE-001", "currency": "INR",
           "as_of": AS_OF.isoformat()}
    with SessionLocal() as s:
        from app.engines.regulatory.models import RegRule
        v2 = s.execute(select(RegRule).where(RegRule.tenant_id == tenant, RegRule.rule_key == "DEMO-ETD-MARGIN",
                                             RegRule.version == 2)).scalar_one()
        base_rules = services.call("regulatory.resolve_rules", s, tenant, ctx, AS_OF, None)
        hyp_rules = services.call("regulatory.resolve_rules", s, tenant, ctx, AS_OF, None, [v2.rule_id])
        assert hyp_rules["hypothetical"] is True and base_rules["rule_set_hash"] != hyp_rules["rule_set_hash"]
        base = services.call("risk.compute_margin", s, tenant, ctx, "SNAP-ETD-001", None, base_rules["rule_set_hash"], "ETD_IMPORTED_V1")
        hyp = services.call("risk.compute_margin", s, tenant, ctx, "SNAP-ETD-001", None, hyp_rules["rule_set_hash"], "ETD_IMPORTED_V1")
        assert base["status"] == "OK" and hyp["status"] == "OK", (base["blocking_reasons"], hyp["blocking_reasons"])
        assert D(base["required_amount"]["amount"]) == D("10000000")
        assert D(hyp["required_amount"]["amount"]) == D("12500000") and hyp["hypothetical"] is True
        posted = services.call("collateral.coverage", s, tenant, "ACC-DEMO-NSE-001", AS_OF)
        assert D(posted["eligible_posted"]["amount"]) == D("11500000")
        shortfall = D(hyp["required_amount"]["amount"]) - D(posted["eligible_posted"]["amount"])
        assert shortfall == D("1000000")                                   # G01 independent hand value
        res = services.call("compliance.evaluate", s, tenant, ctx, "SNAP-ETD-001", hyp_rules["rule_set_hash"], hyp["result_id"])
        e = next(x for x in res["evaluations"] if x["control_key"] == "CMP-COV-ETD-INR")
        assert e["status"] == "FAIL"                                       # S-A04: shortfall is a deterministic FAIL, not UNKNOWN
        cond = e["trace"]["condition"][0]
        assert (cond["left"], cond["right"]) == ("11500000", "12500000")   # measured vs limit, in the trace
        assert len(res["cases"]) == 1 and res["cases"][0]["state"] == "OPEN"
        again = services.call("compliance.evaluate", s, tenant, ctx, "SNAP-ETD-001", hyp_rules["rule_set_hash"], hyp["result_id"])
        from app.engines.compliance.models import CmpCase
        n_cases = s.execute(select(CmpCase).where(CmpCase.tenant_id == tenant, CmpCase.subject_id == "ACC-DEMO-NSE-001",
                                                  CmpCase.control_key == "CMP-COV-ETD-INR")).scalars().all()
        assert len(n_cases) == 1 and not [c for c in again["cases"] if c["case_id"] != n_cases[0].case_id]  # S-A05: no duplicate
        # ATLAS: allocate collateral for the 1,000,000 INR gap. Hypothetical margin => proposal is flagged, never approvable.
        prop = services.call("collateral.optimise", s, tenant, hyp["result_id"], "INV-ETD-001", None, None,
                             "2026-08-19T04:00:00Z", 30, actor_id="int-test", actor_type="SERVICE")
        assert D(prop["required_coverage"]) == D("1000000") and prop["state"] in ("CHECKED", "PROPOSED")
        assert prop["result"]["verification"]["ok"]
        s.commit()


def test_every_engine_emitted_evidence_and_chain_is_valid(tenant):
    """E-A01 (partial, on the seeded flow): each of the five computing engines wrote evidence; the chain verifies."""
    with SessionLocal() as s:
        engines = {e for (e,) in s.execute(select(EvEvent.engine).where(EvEvent.tenant_id == tenant).distinct())}
        report = ev.verify_chain(s, tenant)
    assert {"regulatory", "contracts", "risk", "collateral", "compliance"} <= engines, engines
    assert report["valid"] and report["events"] > 0
