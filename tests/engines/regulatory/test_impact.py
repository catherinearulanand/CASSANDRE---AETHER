from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core import services
from app.core.errors import Unavailable, ValidationFailed
from app.engines.evidence.models import EvEvent
from app.engines.regulatory import impact
from app.engines.regulatory.fixtures import seed_fixtures
from app.engines.regulatory.models import RegRule
from app.engines.regulatory.resolve import resolve_rules
from tests.engines.regulatory.helpers import EXPECTED, dt, fake_services

G = EXPECTED["G01"]
CTX = {"market_type": "ETD_DOMESTIC", "venue": "DEMO_NSE"}
ASOF = "2026-08-17T04:00:00Z"


def fake_risk(db, tenant, context, snapshot_id, terms_hash, rule_set_hash, methodology):
    """TEST DOUBLE for A3: required = base margin (from the pinned snapshot) x multiplier read from the pinned rule set.
    The multiplication lives here, in the test, never in A1."""
    assert context["as_of"] and context["account_id"] == "ACC-DEMO-NSE-001"  # explicit clock + identity are passed to A3
    mult = Decimal(services.call("regulatory.rule_parameters", db, tenant, rule_set_hash)["margin_multiplier"])  # flat view used by risk
    return {"status": "OK", "result_id": f"R-{rule_set_hash[:6]}", "result_hash": rule_set_hash,
            "required_amount": {"amount": str(Decimal(G["base_margin_inr"]) * mult), "currency": "INR"}}


def coverage(db, tenant, account_id, as_of):
    return {"eligible_posted": {"amount": G["eligible_posted"], "currency": "INR"}, "as_of": as_of.isoformat()}


def v2_id(env):
    return env.db.execute(select(RegRule).where(RegRule.tenant_id == env.tenant, RegRule.rule_key == "DEMO-ETD-MARGIN", RegRule.version == 2)).scalar_one().rule_id


def body(env, **kw):
    return {"as_of": dt(ASOF), "hypothetical_rule_ids": [v2_id(env)], "accounts": [
        {"account_id": "ACC-DEMO-NSE-001", "context": CTX, "snapshot_id": "SNAP-ETD-001", "terms_hash": None}], **kw}


def test_r_a07_g01_plus_25_lakh_and_10_lakh_shortfall_via_risk_and_collateral(env):
    seed_fixtures(env.db, env.tenant)
    called = []
    with fake_services(**{"risk.compute_margin": fake_risk, "collateral.coverage": coverage,
                          "compliance.evaluate": lambda *a, **k: called.append(1)}):
        run = impact.run_impact(env.db, env.maker, body(env))
    row = run.results["rows"][0]
    assert run.status == "COMPLETED" and row["status"] == "COMPUTED" and row["currency"] == "INR"
    assert row["baseline_required"] == G["baseline_required"] and row["hypothetical_required"] == G["hypothetical_required"]
    assert row["delta_required"] == G["delta_required"] == EXPECTED["R-A07"]["delta_required"]
    assert row["baseline_shortfall"] == G["baseline_shortfall"] and row["hypothetical_shortfall"] == G["hypothetical_shortfall"]
    assert row["eligible_posted"] == G["eligible_posted"]
    assert run.results["aggregate_by_currency"]["INR"]["delta_required"] == G["delta_required"]
    assert called == []  # A5 evaluation is not invoked for a counterfactual


def test_r_a09_hypothetical_never_official(env):
    seed_fixtures(env.db, env.tenant)
    with fake_services(**{"risk.compute_margin": fake_risk, "collateral.coverage": coverage}):
        run = impact.run_impact(env.db, env.maker, body(env))
    assert run.hypothetical and run.results["official"] is False and run.results["publishes_compliance_status"] is False
    assert all(r["official"] is False for r in run.results["rows"])
    hyp = resolve_rules(env.db, env.tenant, CTX, dt(ASOF), None, [v2_id(env)])
    assert hyp["hypothetical"] and hyp["official"] is False and any(e["hypothetical"] for e in hyp["applicable"])
    official = resolve_rules(env.db, env.tenant, CTX, dt(ASOF))
    assert official["official"] is True and not any(e["hypothetical"] for e in official["applicable"])
    assert official["rule_set_hash"] != hyp["rule_set_hash"]
    ps = services.call("regulatory.rule_parameters", env.db, env.tenant, hyp["rule_set_hash"])
    assert ps["official"] is False and ps["parameters"]["DEMO-ETD-MARGIN"]["margin_multiplier"]["value"] == "1.25"
    assert ps["margin_multiplier"] == "1.25"  # flat convenience view for risk
    assert services.call("regulatory.rule_parameters", env.db, env.tenant, official["rule_set_hash"])["margin_multiplier"] == "1"
    ev = env.db.execute(select(EvEvent).where(EvEvent.tenant_id == env.tenant, EvEvent.action == "IMPACT_RUN_COMPLETED")).scalar_one()
    assert "HYPOTHETICAL" in ev.rationale


def test_impact_blocks_explicitly_when_risk_engine_unavailable_and_never_fakes(env):
    seed_fixtures(env.db, env.tenant)
    with fake_services(**{"risk.compute_margin": None, "collateral.coverage": None}):
        with pytest.raises(Unavailable):
            services.call("risk.compute_margin")  # sanity: really absent
        run = impact.run_impact(env.db, env.maker, body(env))
    row = run.results["rows"][0]
    assert run.status == "BLOCKED" and row["status"] == "BLOCKED" and "SERVICE_NOT_IMPLEMENTED" in row["reason"]
    assert "hypothetical_required" not in row and run.results["aggregate_by_currency"] == {}


def test_shortfall_not_computed_when_collateral_unavailable_but_margin_delta_is(env):
    seed_fixtures(env.db, env.tenant)
    with fake_services(**{"risk.compute_margin": fake_risk, "collateral.coverage": None}):
        run = impact.run_impact(env.db, env.maker, body(env))
    row = run.results["rows"][0]
    assert row["delta_required"] == G["delta_required"] and row["hypothetical_shortfall"] is None and "SERVICE_NOT_IMPLEMENTED" in row["coverage_note"]


def test_unaffected_accounts_are_not_recomputed_and_unknowns_are_counted(env):
    seed_fixtures(env.db, env.tenant)
    calls = []

    def counting(*a):
        calls.append(a[2].get("venue"))
        return fake_risk(*a)
    b = body(env)
    b["accounts"].append({"account_id": "ACC-DEMO-IFSC-001", "context": {"market_type": "ETD_IFSC", "venue": "DEMO_IFSC"}, "snapshot_id": "SNAP-IFSC-001"})
    with fake_services(**{"risk.compute_margin": counting, "collateral.coverage": coverage}):
        run = impact.run_impact(env.db, env.maker, b)
    rows = {r["account_id"]: r for r in run.results["rows"]}
    assert rows["ACC-DEMO-IFSC-001"]["status"] == "UNAFFECTED" and rows["ACC-DEMO-NSE-001"]["status"] == "COMPUTED"
    assert calls == ["DEMO_NSE", "DEMO_NSE"]  # baseline + counterfactual for the affected account only
    c = run.results["counts"]
    assert (c["accounts"], c["affected"], c["unaffected"], c["blocked"]) == (2, 1, 1, 0)


def test_non_ok_risk_result_and_currency_mismatch_block_and_unknown_never_zero(env):
    seed_fixtures(env.db, env.tenant)
    bad = lambda *a: {"status": "BLOCKED", "blocking_reasons": ["stale snapshot"]}  # noqa: E731
    with fake_services(**{"risk.compute_margin": bad, "collateral.coverage": coverage}):
        run = impact.run_impact(env.db, env.maker, body(env))
    assert run.results["rows"][0]["status"] == "BLOCKED" and "stale snapshot" in run.results["rows"][0]["reason"]

    def mixed(db, t, c, s, th, rsh, m):
        r = fake_risk(db, t, c, s, th, rsh, m)
        if rsh != resolve_rules(db, t, c, dt(ASOF))["rule_set_hash"]:
            r["required_amount"]["currency"] = "USD"
        return r
    with fake_services(**{"risk.compute_margin": mixed, "collateral.coverage": coverage}):
        run2 = impact.run_impact(env.db, env.maker, body(env))
    assert "CURRENCY_MISMATCH" in run2.results["rows"][0]["reason"] and run2.results["aggregate_by_currency"] == {}


def test_impact_needs_explicit_counterfactual_and_valid_inputs(env):
    seed_fixtures(env.db, env.tenant)
    with pytest.raises(ValidationFailed):
        impact.run_impact(env.db, env.maker, {"as_of": dt(ASOF), "hypothetical_rule_ids": [], "accounts": [{"context": CTX}]})
    with pytest.raises(ValidationFailed):
        impact.run_impact(env.db, env.maker, {"as_of": dt(ASOF), "hypothetical_rule_ids": [v2_id(env)], "accounts": []})
