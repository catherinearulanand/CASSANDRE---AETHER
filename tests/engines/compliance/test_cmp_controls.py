import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.engines.compliance import cases as C
from app.engines.compliance import engine as E
from app.engines.compliance.models import CmpCase, CmpControl, CmpEvaluation
from tests.engines.compliance.conftest import CTX, IFSC_CTX

EXP = json.loads((Path(__file__).resolve().parents[3] / "fixtures" / "expected" / "compliance" / "golden.json").read_text(encoding="utf-8"))
ASOF = datetime(2026, 8, 17, 4, 0, tzinfo=UTC)


def ctrl(db, tenant, key):
    return db.execute(select(CmpControl).where(CmpControl.tenant_id == tenant, CmpControl.control_key == key)).scalars().first()


def run(db, tenant, ctx=CTX, **kw):
    kw.setdefault("snapshot_id", "SNAP-ETD-001")
    kw.setdefault("rule_set_hash", "rsh-1")
    r = E.evaluate(db, tenant, ctx, kw.pop("snapshot_id"), kw.pop("rule_set_hash"), **kw)
    db.commit()
    return r


@pytest.mark.gold
def test_S_A01_all_six_statuses(db, tenant):
    c = ctrl(db, tenant, "CMP-COV-ETD-INR")
    good = {"market_type": "ETD_DOMESTIC", "venue": "DEMO_NSE", "currency": "INR", "required_margin_inr": "10", "eligible_collateral_inr": "10",
            "input_as_of": "2026-08-17T03:00:00Z"}
    assert E.evaluate_control(c, good, ASOF, [])["status"] == "PASS"
    assert E.evaluate_control(c, {**good, "eligible_collateral_inr": "9"}, ASOF, [])["status"] == "FAIL"
    assert E.evaluate_control(c, {k: v for k, v in good.items() if k != "eligible_collateral_inr"}, ASOF, [])["status"] == "UNKNOWN"
    assert E.evaluate_control(c, {**good, "venue": "DEMO_IFSC"}, ASOF, [])["status"] == "NOT_APPLICABLE"
    assert E.evaluate_control(c, good, ASOF, [], {"conflict": True})["status"] == "CONFLICT"
    bad = {**good, "required_margin_inr": "10", "eligible_collateral_inr": 1.5}  # float fact => bounded evaluator error
    assert E.evaluate_control(c, bad, ASOF, [])["status"] == "ERROR"


@pytest.mark.gold
def test_G01_shortfall_creates_exactly_one_case_with_trace(db, tenant, up):
    r = run(db, tenant, margin_result_id="MR-1")
    exp = EXP["G01_shortfall"]
    cov = [e for e in r["evaluations"] if e["control_key"] == "CMP-COV-ETD-INR"]
    assert len(cov) == 1 and len(r["evaluations"]) == 2                       # only the triggered family; IFSC control reports NOT_APPLICABLE
    assert [x["status"] for x in r["evaluations"] if x["control_key"] == "CMP-COV-IFSC-USD"] == ["NOT_APPLICABLE"]
    e = cov[0]
    assert e["status"] == exp["expected_status"] and e["gap"] == exp["shortfall_inr"] and e["unit"] == "INR"
    assert e["measured"] == exp["posted_inr"] and e["limit"] == exp["required_inr"]
    assert e["rule_key"] == "DEMO-ETD-COVERAGE" and e["rule_version"] == 1 and len(e["rule_hash"]) == 64
    assert e["fact_provenance"]["required_margin_inr"] == "SERVICE:risk.get_result"
    assert "posted only" in e["fact_provenance"]["eligible_collateral_inr"]
    assert any(t.get("op") == "gte" and t["result"] == "FALSE" for t in e["trace"]["condition"])
    assert len(r["cases"]) == exp["expected_cases"]
    n = db.execute(select(func.count()).select_from(CmpCase).where(CmpCase.tenant_id == tenant)).scalar_one()
    assert n == 1
    assert r["cases"][0]["due_at"] == "2026-08-18T04:00:00+00:00"          # HIGH => 24h SLA from explicit as_of


def test_S_A05_repeated_event_does_not_duplicate_case_or_evaluation(db, tenant, up):
    run(db, tenant, margin_result_id="MR-1")
    r2 = run(db, tenant, margin_result_id="MR-1")                         # exact same event redelivered
    assert r2["new_evaluations"] == 0
    assert db.execute(select(func.count()).select_from(CmpEvaluation).where(CmpEvaluation.tenant_id == tenant)).scalar_one() == 2      # coverage + IFSC NOT_APPLICABLE, once
    r3 = run(db, tenant, margin_result_id="MR-1", snapshot_id="SNAP-ETD-002")  # new snapshot, same failure
    assert r3["new_evaluations"] == 2
    cases = db.execute(select(CmpCase).where(CmpCase.tenant_id == tenant)).scalars().all()
    assert len(cases) == 1 and cases[0].occurrence_count == 2               # aggregated; both evaluations retained
    assert db.execute(select(func.count()).select_from(CmpEvaluation).where(CmpEvaluation.tenant_id == tenant)).scalar_one() == 4


@pytest.mark.gold
def test_G11_position_limit_boundaries_and_unknown_classification(db, tenant, up):
    for i, row in enumerate(EXP["G11_position_limit"]["rows"]):
        facts = {"position_lots": row["lots"], "position_basis": "GROSS_FUTURES_EQUIV", "input_as_of": "2026-08-17T03:00:00Z"}
        if row["classification"]:
            facts["client_classification"] = row["classification"]
        ctx = {**CTX, "account_id": f"ACC-G11-{i}"}
        r = run(db, tenant, ctx=ctx, snapshot_id=f"S{i}", facts=facts)
        e = [x for x in r["evaluations"] if x["control_key"] == "CMP-POSLIMIT-ETD"][0]
        assert e["status"] == row["expected"], row
        if "gap" in row:
            assert e["gap"] == row["gap"] and e["unit"] == "lots"


def test_position_limit_wrong_measure_basis_is_unknown_not_pass(db, tenant, up):
    facts = {"position_lots": "10", "position_basis": "NET_DELTA", "client_classification": "CLIENT", "input_as_of": "2026-08-17T03:00:00Z"}
    r = run(db, tenant, facts=facts)
    e = r["evaluations"][0]
    assert e["status"] == "UNKNOWN" and any("MEASURE_BASIS_MISMATCH" in x for x in e["reasons"])


def test_S_A02_stale_or_missing_input_cannot_pass(db, tenant, up):
    up.posted = {"amount": "99999999", "currency": "INR"}                 # coverage would pass if trusted
    up.input_as_of = "2026-08-15T00:00:00Z"                              # 52h old vs 24h limit
    e = run(db, tenant, margin_result_id="MR-1")["evaluations"][0]
    assert e["status"] == "UNKNOWN" and any(r.startswith("STALE_INPUT") for r in e["reasons"])
    up.input_as_of = None
    e2 = run(db, tenant, margin_result_id="MR-1", snapshot_id="S2")["evaluations"][0]
    assert e2["status"] == "UNKNOWN" and any(r.startswith("INPUT_AS_OF_MISSING") for r in e2["reasons"])
    up.input_as_of = "2026-08-17T05:00:00Z"                               # from the future
    e3 = run(db, tenant, margin_result_id="MR-1", snapshot_id="S3")["evaluations"][0]
    assert e3["status"] == "UNKNOWN"


def test_unavailable_upstream_services_yield_unknown_with_reason(db, tenant, up):
    up.risk_down = True
    e = run(db, tenant, margin_result_id="MR-1")["evaluations"][0]
    assert e["status"] == "UNKNOWN" and any("RISK_RESULT_UNAVAILABLE" in r for r in e["reasons"])
    # a blocked margin result is also not usable as input
    up.risk_down, up.status = False, "BLOCKED"
    e2 = run(db, tenant, margin_result_id="MR-2")["evaluations"][0]
    assert e2["status"] == "UNKNOWN" and any("MARGIN_RESULT_BLOCKED" in r for r in e2["reasons"])


def test_callers_cannot_supply_engine_owned_facts(db, tenant, up):
    from app.core.errors import ValidationFailed
    with pytest.raises(ValidationFailed):
        E.evaluate(db, tenant, CTX, "S", "h", facts={"eligible_collateral_inr": "99999999", "required_margin_inr": "1"})


@pytest.mark.gold
def test_G20_ifsc_usd_profile_independent_of_domestic_inr(db, tenant, up):
    g = EXP["G20_ifsc"]
    up.required, up.posted = {"amount": g["required_usd"], "currency": "USD"}, {"amount": g["posted_usd"], "currency": "USD"}
    r = run(db, tenant, ctx=IFSC_CTX, margin_result_id="MR-3")
    st = {e["control_key"]: e["status"] for e in r["evaluations"]}
    assert st == {"CMP-COV-ETD-INR": g["domestic_control_status"], "CMP-COV-IFSC-USD": g["expected_status"]} and not r["cases"]
    # the domestic INR control, forced onto the IFSC subject, is excluded: not applicable, never blended
    r2 = run(db, tenant, ctx={**IFSC_CTX, "control_keys": ["CMP-COV-ETD-INR"]}, margin_result_id="MR-3", snapshot_id="S9")
    assert r2["evaluations"][0]["status"] == g["domestic_control_status"]


def test_currency_mismatch_between_margin_and_coverage_is_unknown(db, tenant, up):
    up.posted = {"amount": "99999999", "currency": "USD"}                 # posted in USD vs INR requirement
    e = run(db, tenant, margin_result_id="MR-1")["evaluations"][0]
    assert e["status"] == "UNKNOWN" and any("COVERAGE_CURRENCY_MISMATCH" in r for r in e["reasons"])


def test_coverage_reports_unsupported_and_unevaluated_not_a_percentage(db, tenant, up):
    run(db, tenant, margin_result_id="MR-1")
    cov = C.coverage(db, tenant, None)
    c = cov["coverage"]
    assert c["registered"] == 9 and c["supported"] == 6 and c["unsupported"] == 3
    assert c["supported_and_evaluated"] == 2 and c["supported_not_evaluated"] == 4
    assert c["latest_status_counts"]["FAIL"] == 1 and "percentage" in c["note"].lower()
    assert not any("percent" in k for k in c)
    unsup = [x for x in cov["controls"] if x["support_status"] == "UNSUPPORTED"]
    assert len(unsup) == 3 and all(x["unsupported_reason"] for x in unsup)


def test_control_compile_gate_rejects_bad_rules(db, tenant):
    from app.core.errors import ValidationFailed
    from app.engines.compliance.controls import register_control
    base = {"control_key": "X-1", "family": "MARGIN_COVERAGE", "title": "t", "rule_key": "R", "authority_type": "SYNTHETIC_POLICY",
            "scope": {"eq": ["market_type", "OTC"]}}
    for cond in ({"lte": ["position_lots"]}, {"lte": ["position_lots", 1.5]}, {"lte": ["no_such_field", 1]}, {"exec": ["x"]}):
        with pytest.raises(ValidationFailed):
            register_control(db, tenant, {**base, "condition": cond})


def test_superseded_control_revision_is_effective_dated_history_preserved(db, tenant, up):
    from app.engines.compliance.controls import supersede_control
    new, old = supersede_control(db, tenant, "CMP-POSLIMIT-ETD", {"condition": {"lte": ["position_lots", 80]}}, datetime(2026, 9, 1, tzinfo=UTC))
    db.commit()
    assert new.rule_version == old.rule_version + 1 and old.effective_to == new.effective_from and new.rule_hash != old.rule_hash
    facts = {"position_lots": "90", "position_basis": "GROSS_FUTURES_EQUIV", "client_classification": "CLIENT", "input_as_of": "2026-08-17T03:00:00Z"}
    before = run(db, tenant, facts=facts)["evaluations"][0]
    assert before["status"] == "PASS" and before["rule_version"] == 1       # old revision still governs earlier as_of
    after = E.evaluate(db, tenant, {**CTX, "as_of": "2026-09-02T04:00:00Z"}, "S2", "h", facts={**facts, "input_as_of": "2026-09-02T03:00:00Z"})
    assert after["evaluations"][0]["status"] == "FAIL" and after["evaluations"][0]["rule_version"] == 2


def test_rule_currency_checked_against_regulatory_when_available(db, tenant, up):
    from app.core import services
    state = {"r": {"applicable": [{"rule_key": "DEMO-ETD-COVERAGE", "version": 3}], "unknown": [], "conflicts": []}}
    services.register("regulatory.resolve_rules", lambda s, t, c, a, k=None, **kw: state["r"])
    try:
        e = run(db, tenant, margin_result_id="MR-1")["evaluations"][0]
        assert e["status"] == "UNKNOWN" and any("CONTROL_STALE_VS_ACTIVE_RULE" in r for r in e["reasons"])   # stale control never looks current
        state["r"] = {"applicable": [], "unknown": [], "conflicts": [{"rule_key": "DEMO-ETD-COVERAGE"}]}
        e2 = run(db, tenant, margin_result_id="MR-1", snapshot_id="S2")["evaluations"][0]
        assert e2["status"] == "CONFLICT"
        state["r"] = {"applicable": [], "unknown": [{"rule_key": "DEMO-ETD-COVERAGE"}], "conflicts": []}
        assert run(db, tenant, margin_result_id="MR-1", snapshot_id="S3")["evaluations"][0]["status"] == "UNKNOWN"
    finally:
        services._REGISTRY.pop("regulatory.resolve_rules", None)


def test_unverified_rule_currency_is_recorded_when_regulatory_absent(db, tenant, up):
    e = run(db, tenant, margin_result_id="MR-1")["evaluations"][0]
    assert e["trace"]["rule_currency"].startswith("UNVERIFIED")


def test_context_requires_explicit_as_of(db, tenant):
    from app.core.errors import ValidationFailed
    with pytest.raises(ValidationFailed):
        E.evaluate(db, tenant, {k: v for k, v in CTX.items() if k != "as_of"}, "S", "h")


def test_input_problems_only_block_controls_that_depend_on_them(db, tenant, up):
    """Regression (found in live run): an unavailable risk/collateral lookup must not turn the position-limit control UNKNOWN."""
    up.risk_down = True
    facts = {"position_lots": "101", "position_basis": "GROSS_FUTURES_EQUIV", "client_classification": "CLIENT", "input_as_of": "2026-08-17T03:00:00Z"}
    r = run(db, tenant, margin_result_id="MR-X", facts=facts, ctx={**CTX, "full_sweep": True}) if False else E.evaluate(
        db, tenant, {**CTX, "full_sweep": True}, "S-RG", "h", margin_result_id="MR-X", facts=facts)
    db.commit()
    by = {e["control_key"]: e for e in r["evaluations"]}
    assert by["CMP-POSLIMIT-ETD"]["status"] == "FAIL" and by["CMP-POSLIMIT-ETD"]["gap"] == "1"
    assert by["CMP-COV-ETD-INR"]["status"] == "UNKNOWN" and any("RISK_RESULT_UNAVAILABLE" in x for x in by["CMP-COV-ETD-INR"]["reasons"])
    # without a margin result the collateral lookup is not even attempted
    r2 = E.evaluate(db, tenant, CTX, "S-RG2", "h", facts=facts)
    assert [e["control_key"] for e in r2["evaluations"]] == ["CMP-POSLIMIT-ETD"] and r2["evaluations"][0]["status"] == "FAIL"
