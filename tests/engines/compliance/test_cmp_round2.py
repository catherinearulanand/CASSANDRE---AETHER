"""Round 2: contract v1.1 consumption, new controls, scheduling/missed runs, G23, evidence parents + replay (names prefixed test_cmp_)."""
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.core import services
from app.core.events import dispatch_once, emit
from app.db.models_core import OutboxEvent
from app.engines.compliance import cases as C
from app.engines.compliance import controls as K
from app.engines.compliance import engine as E
from app.engines.compliance import schedule as SCH
from app.engines.compliance.models import CmpCase, CmpEvaluation, CmpJob
from app.engines.evidence.models import EvEvent
from tests.engines.compliance.conftest import CTX, P

ASOF = datetime(2026, 8, 17, 4, 0, tzinfo=UTC)
FRESH = "2026-08-17T03:00:00Z"


def ev(db, tenant, ctx=CTX, snap="SNAP-R2", rsh="rsh-r2", **kw):
    r = E.evaluate(db, tenant, ctx, snap, rsh, **kw)
    db.commit()
    return r


def by(r):
    return {e["control_key"]: e for e in r["evaluations"]}


# ------------------------------------------------------------------ (1) contract v1.1 consumption
POS_OK = {"gross_lots": "101", "net_lots": "101", "delta_equivalent_lots": None, "client_class": "CLIENT", "as_of": FRESH}


def test_position_limit_uses_service_data_and_service_wins_over_caller_facts(db, tenant, up):
    up.positions = dict(POS_OK)
    # caller tries to understate the position; the owning engine's value is used (G11 limit 100: 101 FAILS)
    r = ev(db, tenant, {**CTX, "families": ["POSITION_LIMIT"]}, facts={"position_lots": "5", "position_basis": "GROSS_FUTURES_EQUIV",
                                                                       "client_classification": "CLIENT", "input_as_of": FRESH})
    e = by(r)["CMP-POSLIMIT-ETD"]
    assert e["status"] == "FAIL" and e["measured"] == "101" and e["gap"] == "1"
    assert e["fact_provenance"]["position_lots"].startswith("SERVICE:risk.get_positions")
    assert e["fact_provenance"]["client_classification"] == "SERVICE:risk.get_positions"
    assert e["facts"]["position_lots"] == "101"


def test_position_limit_boundary_100_passes_and_unknown_classification_is_unknown_not_not_applicable(db, tenant, up):
    up.positions = {**POS_OK, "gross_lots": "100"}
    assert by(ev(db, tenant, {**CTX, "families": ["POSITION_LIMIT"]}))["CMP-POSLIMIT-ETD"]["status"] == "PASS"
    # the risk engine reports client_class "UNKNOWN" (it does not own classification): UNKNOWN, never a value / never NOT_APPLICABLE
    up.positions = {**POS_OK, "client_class": "UNKNOWN"}
    e = by(ev(db, tenant, {**CTX, "families": ["POSITION_LIMIT"]}, snap="S-U"))["CMP-POSLIMIT-ETD"]
    assert e["status"] == "UNKNOWN" and "client_classification" not in e["facts"]
    # an explicitly labelled caller fallback may supply the classification
    e2 = by(ev(db, tenant, {**CTX, "families": ["POSITION_LIMIT"]}, snap="S-U2", facts={"client_classification": "CLIENT"}))["CMP-POSLIMIT-ETD"]
    assert e2["status"] == "FAIL" and e2["fact_provenance"]["client_classification"] == "CALLER_SUPPLIED"   # 101 > 100 once classified


def test_position_service_unavailable_or_no_positions_is_unknown(db, tenant, up):
    e = by(ev(db, tenant, {**CTX, "families": ["POSITION_LIMIT"]}))["CMP-POSLIMIT-ETD"]       # up.positions None => unavailable
    assert e["status"] == "UNKNOWN" and any("POSITIONS_UNAVAILABLE" in x for x in e["reasons"])
    up.positions = {"gross_lots": None, "net_lots": None, "client_class": "UNKNOWN", "as_of": None, "available": False}
    e2 = by(ev(db, tenant, {**CTX, "families": ["POSITION_LIMIT"]}, snap="S2"))["CMP-POSLIMIT-ETD"]
    assert e2["status"] == "UNKNOWN" and any("POSITIONS_MEASURE_MISSING" in x for x in e2["reasons"])


@pytest.mark.parametrize("status,expect", [("OK", "PASS"), ("EXCEPTION", "FAIL"), ("UNKNOWN", "UNKNOWN")])
def test_reconciliation_status_mapping(db, tenant, up, status, expect):
    up.recon = {"status": status, "difference": "100000", "as_of": FRESH}
    e = by(ev(db, tenant, {**CTX, "families": ["RECONCILIATION"]}))["CMP-RECON-ETD"]
    assert e["status"] == expect
    if expect == "FAIL":
        assert e["measured"] == "EXCEPTION" and e["limit"] == "MATCHED"


def test_input_as_of_is_oldest_input_and_prefers_snapshot_as_of(db, tenant, up):
    up.extra = {"snapshot_as_of": "2026-08-15T03:00:00Z"}                    # data is 49h old although the calculation as_of is fresh
    e = by(ev(db, tenant, margin_result_id="MR-1"))["CMP-COV-ETD-INR"]
    assert e["status"] == "UNKNOWN" and any(x.startswith("STALE_INPUT") for x in e["reasons"])
    up.extra = {}
    up.positions, up.recon = dict(POS_OK), {"status": "OK", "as_of": "2026-08-16T01:00:00Z"}  # 27h old reconciliation file
    r = ev(db, tenant, {**CTX, "families": ["POSITION_LIMIT", "RECONCILIATION"]}, snap="S-OLD")
    assert by(r)["CMP-RECON-ETD"]["status"] == "UNKNOWN" and by(r)["CMP-RECON-ETD"]["facts"]["input_as_of"] == "2026-08-16T01:00:00Z"


# ------------------------------------------------------------------ (2) concentration, settlement deadline, report schema
def test_collateral_concentration_pass_fail_and_unavailable(db, tenant, up):
    up.slacks = [{"name": "cap_gsec", "limit": "50", "used": "40", "slack": "10", "binding": False},
                 {"name": "cap_corp", "limit": "20", "used": "20", "slack": "0", "binding": True}]
    e = by(ev(db, tenant, allocation_id="PROP-1", margin_result_id="MR-1"))["CMP-COLL-CONC"]
    assert e["status"] == "PASS" and e["facts"]["concentration_constraints_checked"] == 2        # zero slack is within the cap
    up.slacks = [*up.slacks, {"name": "cap_cash", "limit": "10", "used": "12", "slack": "-2", "binding": True}]
    r = ev(db, tenant, allocation_id="PROP-2", margin_result_id="MR-1", snap="S-C2")
    e2 = by(r)["CMP-COLL-CONC"]
    assert e2["status"] == "FAIL" and e2["measured"] == "1" and e2["limit"] == "0" and any(c["control_key"] == "CMP-COLL-CONC" for c in r["cases"])
    up.slacks = None
    e3 = by(ev(db, tenant, allocation_id="PROP-3", margin_result_id="MR-1", snap="S-C3"))["CMP-COLL-CONC"]
    assert e3["status"] == "UNKNOWN" and any("CONSTRAINT_SLACKS_UNAVAILABLE" in x for x in e3["reasons"])


def test_settlement_deadline_uses_posted_collateral_as_of_the_deadline(db, tenant, up):
    deadline = "2026-08-19T04:00:00+00:00"
    up.posted_at = lambda t: "12500000" if t >= datetime(2026, 8, 19, tzinfo=UTC) else "11500000"   # settlement lands before the deadline
    r = ev(db, tenant, {**CTX, "settlement_deadline": deadline}, margin_result_id="MR-1")
    s = by(r)["CMP-SETTLE-DEADLINE"]
    assert s["status"] == "PASS" and s["facts"]["deadline_collateral_inr"] == "12500000"
    assert by(r)["CMP-COV-ETD-INR"]["status"] == "FAIL"                           # as_of coverage still FAILS: no early credit
    up.posted_at = lambda t: "11500000"                                          # never settles in time
    r2 = ev(db, tenant, {**CTX, "settlement_deadline": deadline}, margin_result_id="MR-1", snap="S-D2")
    s2 = by(r2)["CMP-SETTLE-DEADLINE"]
    assert s2["status"] == "FAIL" and s2["gap"] == "1000000" and s2["fact_provenance"]["deadline_collateral_inr"].endswith("as of settlement_deadline)")
    assert any(c["control_key"] == "CMP-SETTLE-DEADLINE" for c in r2["cases"])


def test_settlement_deadline_missing_input_is_unknown(db, tenant, up):
    e = by(ev(db, tenant, {**CTX, "families": ["SETTLEMENT_DEADLINE"]}, margin_result_id="MR-1"))["CMP-SETTLE-DEADLINE"]
    assert e["status"] == "UNKNOWN" and "SETTLEMENT_DEADLINE_NOT_PROVIDED" in e["reasons"]


GOOD_REPORT = {"report_id": "R-1", "account_id": "ACC-DEMO-NSE-001", "as_of": "2026-08-17T04:00:00Z", "currency": "INR",
               "required_margin": "12500000", "posted_collateral": "11500000"}


def test_report_schema_completeness_and_format_validation_only(db, tenant, up):
    e = by(ev(db, tenant, facts={"report_record": GOOD_REPORT}))["CMP-REPORT-SCHEMA"]
    assert e["status"] == "PASS" and e["fact_provenance"]["report_missing_fields"].startswith("CALLER_SUPPLIED record validated against DEMO-MARGIN-REPORT-v1")
    assert "NOT an approved regulatory format" in e["fact_provenance"]["report_missing_fields"]
    bad = {k: v for k, v in GOOD_REPORT.items() if k != "report_id"} | {"currency": "ZZZ", "required_margin": "-5", "as_of": "yesterday"}
    r = ev(db, tenant, facts={"report_record": bad}, snap="S-BAD")
    e2 = by(r)["CMP-REPORT-SCHEMA"]
    assert e2["status"] == "FAIL" and e2["facts"]["report_missing_fields"] == 1 and e2["facts"]["report_format_errors"] == 3
    assert "MISSING:report_id" in e2["facts"]["report_findings"] and r["cases"]


# ------------------------------------------------------------------ (3) scheduled evaluation, missed runs, G23
def seed_subject(db, tenant, up):
    up.positions, up.recon = dict(POS_OK) | {"gross_lots": "90"}, {"status": "OK", "as_of": FRESH}
    return ev(db, tenant, {**CTX, "families": ["POSITION_LIMIT", "RECONCILIATION"]}, snap="S-SEED")


def test_scheduled_sweep_runs_daily_controls_and_records_watermark(db, tenant, up):
    seed_subject(db, tenant, up)
    up.extra = {"snapshot_as_of": FRESH}
    out = SCH.run_sweep(db, tenant, ASOF, [{"context": {k: CTX[k] for k in ("market_type", "venue", "account_id", "currency")}, "snapshot_id": "S-SW",
                                            "rule_set_hash": "h", "margin_result_id": "MR-1"}])
    db.commit()
    assert out["status"] == "DONE" and out["slot"] == "2026-08-17" and out["watermark_as_of"] == ASOF.isoformat()
    ev_keys = {e.control_key for e in db.execute(select(CmpEvaluation).where(CmpEvaluation.tenant_id == tenant,
                                                                              CmpEvaluation.snapshot_id == "S-SW")).scalars()}
    assert ev_keys == {"CMP-COV-ETD-INR", "CMP-COV-IFSC-USD", "CMP-POSLIMIT-ETD", "CMP-RECON-ETD"}   # only frequency DAILY, supported controls
    assert out["status_counts"]["PASS"] >= 2 and "FAIL" in out["status_counts"]                      # coverage still FAILS (G01 numbers)
    st = SCH.schedule_status(db, tenant, ASOF + timedelta(hours=11))                                 # 15:00Z the same day
    assert st["completed_slots"] == ["2026-08-17"] and st["missed_slots"] == [] and st["stale"] is False


def test_missed_runs_detected_reported_and_recorded_once(db, tenant, up):
    seed_subject(db, tenant, up)
    SCH.run_sweep(db, tenant, ASOF, None)
    db.commit()
    now = datetime(2026, 8, 20, 18, tzinfo=UTC)
    st = SCH.schedule_status(db, tenant, now)
    assert st["missed_slots"] == ["2026-08-18", "2026-08-19", "2026-08-20"] and st["stale"] is True
    assert "not compliance" in st["note"]
    first = SCH.schedule_status(db, tenant, now, record=True)
    db.commit()
    assert first["newly_recorded_missed"] == ["2026-08-18", "2026-08-19", "2026-08-20"]
    again = SCH.schedule_status(db, tenant, now, record=True)
    assert again["newly_recorded_missed"] == []                                              # once per slot
    acts = [e.action for e in db.execute(select(EvEvent).where(EvEvent.tenant_id == tenant, EvEvent.action == "MISSED_RUN_DETECTED")).scalars()]
    assert len(acts) == 3
    # a sweep for a missed slot clears it
    SCH.run_sweep(db, tenant, datetime(2026, 8, 18, 15, tzinfo=UTC), None)
    assert "2026-08-18" not in SCH.schedule_status(db, tenant, now)["missed_slots"]


def test_sweep_over_stale_inputs_never_passes(db, tenant, up):
    seed_subject(db, tenant, up)
    out = SCH.run_sweep(db, tenant, datetime(2026, 8, 19, 15, tzinfo=UTC), None)        # inputs are ~60h old by now
    assert out["status_counts"].get("PASS", 0) == 0 and out["status_counts"].get("UNKNOWN", 0) >= 1


def test_G23_supersession_reevaluates_only_affected_subjects_and_preserves_history(db, tenant, up):
    up.positions = {**POS_OK, "gross_lots": "100"}
    up.recon = {"status": "OK", "as_of": FRESH}
    a = {**CTX, "families": ["POSITION_LIMIT"], "account_id": "ACC-A"}
    b = {**CTX, "families": ["POSITION_LIMIT"], "account_id": "ACC-B"}
    ev(db, tenant, a, snap="S-A")
    ev(db, tenant, b, snap="S-B")
    ev(db, tenant, {**CTX, "families": ["RECONCILIATION"], "account_id": "ACC-A"}, snap="S-R")         # a DIFFERENT control: must stay untouched
    n_before = {k: db.execute(select(CmpEvaluation).where(CmpEvaluation.tenant_id == tenant, CmpEvaluation.control_key == k)).scalars().all()
                for k in ("CMP-POSLIMIT-ETD", "CMP-RECON-ETD")}
    eff = datetime(2026, 8, 20, tzinfo=UTC)
    up.positions = {**POS_OK, "gross_lots": "100", "as_of": "2026-08-20T00:00:00Z"}                # fresh data as of the effective date
    new, old = K.supersede_control(db, tenant, "CMP-POSLIMIT-ETD", {"condition": {"lte": ["position_lots", 99]}}, eff)   # limit tightened to 99
    out = K.reevaluate_affected(db, tenant, "CMP-POSLIMIT-ETD", eff, E_actor())
    db.commit()
    assert new.rule_version == old.rule_version + 1 and old.effective_to == eff and new.rule_hash != old.rule_hash
    assert sorted(x["subject_id"] for x in out["re_evaluated"]) == ["ACC-A", "ACC-B"] and all(x["status"] == "FAIL" for x in out["re_evaluated"])  # 100 > 99
    after = {k: db.execute(select(CmpEvaluation).where(CmpEvaluation.tenant_id == tenant, CmpEvaluation.control_key == k)).scalars().all()
             for k in ("CMP-POSLIMIT-ETD", "CMP-RECON-ETD")}
    assert len(after["CMP-RECON-ETD"]) == len(n_before["CMP-RECON-ETD"])                      # unaffected control untouched
    assert len(after["CMP-POSLIMIT-ETD"]) == len(n_before["CMP-POSLIMIT-ETD"]) + 2            # appended, nothing rewritten
    olds = [e for e in after["CMP-POSLIMIT-ETD"] if e.rule_version == old.rule_version]
    assert len(olds) == 2 and all(e.status == "PASS" for e in olds)                           # history preserved (100 <= 100 passed)
    # a subject never evaluated for this control is NOT touched
    assert "ACC-NEW" not in {e.subject_id for e in after["CMP-POSLIMIT-ETD"]}


def E_actor():
    return {"id": "sys", "type": "SYSTEM", "role": None}


def test_rule_activation_event_reevaluates_only_controls_bound_to_that_rule_key(db, tenant, up):
    up.positions = {**POS_OK, "gross_lots": "50"}
    ev(db, tenant, {**CTX, "families": ["POSITION_LIMIT"]}, snap="S-P")
    n = len(db.execute(select(CmpEvaluation).where(CmpEvaluation.tenant_id == tenant)).scalars().all())
    emit(db, event_type="RuleActivated", tenant_id=tenant, correlation_id="c-r", aggregate_id="rule-1", actor_id="u",
         effective_at=datetime(2026, 8, 18, tzinfo=UTC), payload={"rule_key": "DEMO-ETD-POSLIMIT", "version": 2, "status": "ACTIVE"})
    emit(db, event_type="RuleActivated", tenant_id=tenant, correlation_id="c-r2", aggregate_id="rule-9", actor_id="u",
         effective_at=datetime(2026, 8, 18, tzinfo=UTC), payload={"rule_key": "DEMO-SOMETHING-ELSE", "version": 2, "status": "ACTIVE"})
    db.commit()
    # dispatch_once serves the oldest PENDING rows of ALL tenants (earlier tests leave many undelivered): loop until OUR events are delivered
    for _ in range(400):
        dispatch_once(db, limit=500)
        db.commit()
        if not db.execute(select(OutboxEvent).where(OutboxEvent.tenant_id == tenant, OutboxEvent.event_type == "RuleActivated",
                                                    OutboxEvent.status == "PENDING")).first():
            break
    rows = db.execute(select(CmpEvaluation).where(CmpEvaluation.tenant_id == tenant)).scalars().all()
    assert len(rows) == n + 1
    new = [r for r in rows if r.as_of == datetime(2026, 8, 18, tzinfo=UTC)]
    assert [r.control_key for r in new] == ["CMP-POSLIMIT-ETD"]
    sent = db.execute(select(OutboxEvent).where(OutboxEvent.tenant_id == tenant, OutboxEvent.event_type == "RuleActivated")).scalars().all()
    assert all(o.status == "DELIVERED" for o in sent)


# ------------------------------------------------------------------ (4) evidence contract: parents + replay
def test_evidence_parents_and_replay_hash_match_and_detect_drift(db, tenant, up):
    r = ev(db, tenant, margin_result_id="MR-1", allocation_id="PROP-7")
    evid = by(r)["CMP-COV-ETD-INR"]["evaluation_id"]
    rows = db.execute(select(EvEvent).where(EvEvent.tenant_id == tenant, EvEvent.entity_id == evid)).scalars().all()
    assert len(rows) == 1 and rows[0].entity_type == "control_evaluation" and rows[0].action == "CONTROL_EVALUATED"
    parents = {(p["entity_type"], p["entity_id"]) for p in rows[0].payload["parents"]}
    assert parents == {("risk_result", "MR-1"), ("allocation_proposal", "PROP-7")}
    spec = rows[0].payload["replay"]
    assert spec["replayer"] == "replay.compliance" and spec["engine_version"] and spec["method_version"] and len(spec["business_hash"]) == 64
    assert spec["business_hash"] == rows[0].output_hash and "evaluation_id" in spec["inputs"]
    case = next(c for c in db.execute(select(EvEvent).where(EvEvent.tenant_id == tenant, EvEvent.action == "CASE_OPENED")).scalars()
                if c.payload["evaluation_id"] == evid)
    assert case.payload["parents"] == [{"entity_type": "control_evaluation", "entity_id": evid}]
    # read-only replay through VERITAS: MATCH
    rep = services.call("evidence.replay_run", db, tenant, "run-x", rows[0].correlation_id, "auditor-1")
    items = [i for i in rep["items"] if i["engine"] == "compliance"]
    assert items and all(i["status"] == "MATCH" for i in items), rep
    # drift: the persisted fact is altered (simulated tamper) => the replayed outcome no longer matches the recorded hash
    e = db.get(CmpEvaluation, evid)
    e.facts = {**e.facts, "eligible_collateral_inr": "99999999"}
    db.flush()
    rep2 = services.call("evidence.replay_run", db, tenant, "run-x2", rows[0].correlation_id, "auditor-1")
    assert any(i["status"] == "DIVERGED" for i in rep2["items"] if i["engine"] == "compliance"), rep2
    db.rollback()


def test_replay_reports_not_replayable_without_the_replayer(db, tenant, up):
    r = ev(db, tenant, margin_result_id="MR-1")
    cid = r["correlation_id"]
    saved = services._REGISTRY.pop("replay.compliance")
    try:
        rep = services.call("evidence.replay_run", db, tenant, "run-y", cid, "auditor-1")
    finally:
        services.register("replay.compliance", saved)
    assert any(i["status"] == "NOT_REPLAYABLE" for i in rep["items"] if i["engine"] == "compliance"), rep
    db.rollback()


# ------------------------------------------------------------------ hypothetical results are never official compliance status (R-A09 / I05)
def test_hypothetical_margin_result_opens_a_flagged_separate_case_and_never_counts_as_official(db, tenant, up):
    up.extra = {"hypothetical": True, "account_id": "ACC-DEMO-NSE-001"}
    r = ev(db, tenant, margin_result_id="MR-HYP")
    e = by(r)["CMP-COV-ETD-INR"]
    assert e["status"] == "FAIL" and e["hypothetical"] is True and e["official"] is False and e["gap"] == "1000000"
    assert r["cases"][0]["hypothetical"] is True and r["cases"][0]["title"].startswith("[HYPOTHETICAL] ")
    cov = C.coverage(db, tenant, None)["coverage"]
    assert cov["subject_evaluations"] == 0 and cov["latest_status_counts"]["FAIL"] == 0           # no official status published
    assert cov["hypothetical_evaluations"] == 2 and cov["hypothetical_status_counts"] == {"FAIL": 1, "NOT_APPLICABLE": 1}   # ETD FAIL + IFSC not applicable
    hyp_ev = db.execute(select(EvEvent).where(EvEvent.tenant_id == tenant, EvEvent.entity_id == e["evaluation_id"])).scalar_one()
    assert hyp_ev.payload["hypothetical"] is True
    # an OFFICIAL failure of the same control/subject/revision opens its OWN case: the two never share a case
    up.extra = {"account_id": "ACC-DEMO-NSE-001"}
    r2 = ev(db, tenant, margin_result_id="MR-OFF", snap="S-OFF")
    assert r2["cases"][0]["hypothetical"] is False and r2["cases"][0]["case_id"] != r["cases"][0]["case_id"]
    assert C.coverage(db, tenant, None)["coverage"]["subject_evaluations"] == 2                  # official ETD FAIL + IFSC not applicable


def test_margin_result_of_another_subject_is_rejected_not_silently_used(db, tenant, up):
    up.extra = {"account_id": "ACC-SOMEONE-ELSE"}
    e = by(ev(db, tenant, margin_result_id="MR-X"))["CMP-COV-ETD-INR"]
    assert e["status"] == "UNKNOWN" and any("MARGIN_RESULT_SUBJECT_MISMATCH" in x for x in e["reasons"])
    assert "required_margin_inr" not in e["facts"]
