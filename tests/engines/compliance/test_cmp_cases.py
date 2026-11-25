from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from tests.engines.compliance.conftest import CTX, P

from app.core.errors import Blocked, Conflict, Forbidden, NotFound, ValidationFailed
from app.engines.compliance import cases as C
from app.engines.compliance import engine as E
from app.engines.compliance.models import CmpCase, CmpCaseEvent, CmpEvaluation

AS1, AS2 = "2026-08-17T06:00:00Z", "2026-08-17T07:00:00Z"


def open_case(db, tenant):
    r = E.evaluate(db, tenant, CTX, "SNAP-ETD-001", "rsh", margin_result_id="MR-1")
    db.commit()
    return [c for c in r["cases"]][0]["case_id"]


def tr(db, p, cid, to, reason=None):
    c = C.get_case(db, p.tenant_id, cid)
    out = C.transition(db, p, cid, to, reason, c.revision)
    db.commit()
    return out


def to_review(db, tenant, cid, maker):
    tr(db, maker, cid, "ACKNOWLEDGED")
    tr(db, maker, cid, "INVESTIGATING")
    c = C.get_case(db, tenant, cid)
    C.remediate(db, maker, cid, {"expected_revision": c.revision, "selected": "ROUTE_COLLATERAL_OPTIMISE",
                                 "rationale": "Request recommendation for pinned margin result",
                                 "rejected": [{"route": "ROUTE_RULE_REVIEW", "reason": "rule revision already confirmed"}]})
    db.commit()
    tr(db, maker, cid, "REVIEW_REQUIRED")


@pytest.mark.gold
def test_S_A06_closure_only_after_verified_recheck_and_independent_checker(db, tenant, up):
    cid = open_case(db, tenant)
    maker, checker = P(tenant, "u-maker", "maker"), P(tenant, "u-checker", "checker")
    to_review(db, tenant, cid, maker)
    # no recheck yet -> cannot close
    with pytest.raises(Blocked) as e:
        tr(db, checker, cid, "RESOLVED", "looks fine to me")
    assert e.value.code == "RECHECK_REQUIRED"
    # recheck against UNCHANGED reality still FAILS -> cannot close (a proposal is not posted collateral)
    r1 = C.recheck(db, tenant, cid, "SNAP-2", as_of=AS1)
    db.commit()
    assert r1["status"] == "FAIL" and r1["is_recheck"]
    with pytest.raises(Blocked) as e:
        tr(db, checker, cid, "RESOLVED", "closing anyway")
    assert e.value.code == "RECHECK_NOT_PASS"
    # collateral actually posted (as reported by the collateral engine) -> fresh recheck PASSES
    up.posted = {"amount": "12500000", "currency": "INR"}
    r2 = C.recheck(db, tenant, cid, "SNAP-3", as_of=AS2)
    db.commit()
    assert r2["status"] == "PASS"
    # the maker/author cannot close their own remediation
    with pytest.raises(Forbidden) as e:
        tr(db, P(tenant, "u-maker", "compliance_officer"), cid, "RESOLVED", "self close")
    assert e.value.code == "SELF_APPROVAL"
    done = tr(db, checker, cid, "RESOLVED", "Recheck PASS after posted collateral")
    assert done.state == "RESOLVED" and done.resolution["recheck_evaluation_id"] == r2["evaluation_id"]
    # raw history untouched: first evaluation is still FAIL
    first = db.execute(select(CmpEvaluation).where(CmpEvaluation.evaluation_id == done.first_evaluation_id)).scalar_one()
    assert first.status == "FAIL"


def test_PASS_observed_never_auto_closes_case(db, tenant, up):
    cid = open_case(db, tenant)
    up.posted = {"amount": "13000000", "currency": "INR"}
    E.evaluate(db, tenant, CTX, "SNAP-NEXT", "rsh", margin_result_id="MR-1")
    db.commit()
    c = C.get_case(db, tenant, cid)
    assert c.state == "OPEN"
    ev = db.execute(select(CmpCaseEvent).where(CmpCaseEvent.case_id == cid, CmpCaseEvent.event_type == "PASS_OBSERVED")).scalars().all()
    assert len(ev) == 1


def test_recurrence_reopens_resolved_case_not_duplicate(db, tenant, up):
    cid = open_case(db, tenant)
    maker, checker = P(tenant, "u-maker", "maker"), P(tenant, "u-checker", "checker")
    to_review(db, tenant, cid, maker)
    up.posted = {"amount": "12500000", "currency": "INR"}
    C.recheck(db, tenant, cid, "S-OK", as_of=AS1)
    db.commit()
    tr(db, checker, cid, "RESOLVED", "verified by recheck")
    up.posted = {"amount": "11000000", "currency": "INR"}
    E.evaluate(db, tenant, CTX, "S-AGAIN", "rsh", margin_result_id="MR-1")
    db.commit()
    rows = db.execute(select(CmpCase).where(CmpCase.tenant_id == tenant)).scalars().all()
    assert len(rows) == 1 and rows[0].state == "OPEN" and rows[0].reopen_count == 1


def test_lifecycle_guards_and_optimistic_revision(db, tenant, up):
    cid = open_case(db, tenant)
    maker = P(tenant, "u-maker", "maker")
    c = C.get_case(db, tenant, cid)
    with pytest.raises(Conflict) as e:
        C.transition(db, maker, cid, "ACKNOWLEDGED", None, c.revision + 5)
    assert e.value.code == "REVISION_CONFLICT"
    with pytest.raises(Conflict) as e:
        C.transition(db, maker, cid, "RESOLVED", "x" * 6, c.revision)      # cannot skip states
    assert e.value.code == "ILLEGAL_TRANSITION"
    with pytest.raises(Conflict) as e:
        C.transition(db, maker, cid, "REMEDIATION_PROPOSED", None, c.revision)
    with pytest.raises(Forbidden):
        C.transition(db, P(tenant, "u-viewer", "viewer"), cid, "ACKNOWLEDGED", None, c.revision)
    tr(db, maker, cid, "ACKNOWLEDGED")
    tr(db, maker, cid, "INVESTIGATING")
    c = C.get_case(db, tenant, cid)
    with pytest.raises(ValidationFailed):                                    # unknown remedy
        C.remediate(db, maker, cid, {"expected_revision": c.revision, "selected": "WIRE_MONEY", "rationale": "because"})
    with pytest.raises(ValidationFailed):                                    # rationale mandatory
        C.remediate(db, maker, cid, {"expected_revision": c.revision, "selected": "ROUTE_COLLATERAL_OPTIMISE", "rationale": ""})


def test_remediation_records_proposed_selected_rejected_and_routes_to_atlas(db, tenant, up):
    cid = open_case(db, tenant)
    maker = P(tenant, "u-maker", "maker")
    to_review(db, tenant, cid, maker)
    c = C.get_case(db, tenant, cid)
    rec = c.remediations[0]
    assert rec["selected"]["target_engine"] == "collateral" and rec["selected"]["inputs"] == {"margin_result_id": "MR-1"}
    assert "nothing is posted or transmitted" in rec["selected"]["consequence"]
    assert [r["route"] for r in rec["rejected"]] == ["ROUTE_RULE_REVIEW"] and rec["rationale"] and rec["deadline"] and rec["owner_id"]
    assert len(rec["proposed"]) >= 3 and "no market action" in rec["note"]


def test_dismissal_requires_reason_and_independent_checker_and_binds_current_state(db, tenant, up):
    cid = open_case(db, tenant)
    maker, checker, officer = P(tenant, "u-maker", "maker"), P(tenant, "u-checker", "checker"), P(tenant, "u-officer", "compliance_officer")
    with pytest.raises(ValidationFailed):
        tr(db, maker, cid, "DISMISSED", "")
    with pytest.raises(Blocked) as e:                                        # a checker alone cannot dismiss without a request
        tr(db, checker, cid, "DISMISSED", "no request exists")
    assert e.value.code == "NO_DISMISSAL_REQUEST"
    c = tr(db, officer, cid, "DISMISSED", "false alarm - duplicate feed")
    assert c.state == "OPEN" and c.pending_dismissal["requested_by"] == "u-officer"
    with pytest.raises(Forbidden) as e:                                      # requester cannot approve own request
        tr(db, officer, cid, "DISMISSED", "approving myself")
    assert e.value.code == "SELF_APPROVAL"
    # case changes after the request (new occurrence) -> approval is stale and refused
    E.evaluate(db, tenant, CTX, "SNAP-LATER", "rsh", margin_result_id="MR-1")
    db.commit()
    with pytest.raises(Conflict) as e:
        tr(db, checker, cid, "DISMISSED", "approve stale")
    assert e.value.code == "STALE_APPROVAL"
    tr(db, maker, cid, "DISMISSED", "re-request after the new occurrence")   # maker re-requests against current state
    done = tr(db, checker, cid, "DISMISSED", "independently reviewed; duplicate of upstream feed")
    assert done.state == "DISMISSED" and done.resolution["approved_by"] == "u-checker" and "does not change" in done.resolution["note"]
    # dismissal does not touch the raw evaluation outcome
    assert {e.status for e in db.execute(select(CmpEvaluation).where(CmpEvaluation.case_id == cid)).scalars()} == {"FAIL"}


def test_case_history_is_append_only_in_code_paths(db, tenant, up):
    cid = open_case(db, tenant)
    maker = P(tenant, "u-maker", "maker")
    tr(db, maker, cid, "ACKNOWLEDGED")
    C.add_comment(db, maker, cid, "investigating the margin call")
    db.commit()
    evs = db.execute(select(CmpCaseEvent).where(CmpCaseEvent.case_id == cid).order_by(CmpCaseEvent.id)).scalars().all()
    assert [e.event_type for e in evs] == ["OPENED", "STATE_CHANGE", "COMMENT"]
    assert not hasattr(C, "delete_comment") and not hasattr(C, "edit_comment")


def test_tenant_isolation_of_cases(db, tenant, up):
    cid = open_case(db, tenant)
    with pytest.raises(NotFound):
        C.get_case(db, "some-other-tenant", cid)
    with pytest.raises(NotFound):
        C.recheck(db, "some-other-tenant", cid, "S", as_of=AS1)


# ------------------------------------------------------------------ waivers
@pytest.mark.gold
def test_S_A07_waiver_scope_expiry_and_authority(db, tenant, up):
    maker, checker = P(tenant, "u-maker", "maker"), P(tenant, "u-checker", "checker")
    body = {"control_key": "CMP-COV-ETD-INR", "subject_id": CTX["account_id"], "reason": "temporary internal-policy exception",
            "valid_from": "2026-08-17T00:00:00Z", "valid_to": "2026-08-20T00:00:00Z"}
    with pytest.raises(ValidationFailed):
        C.create_waiver(db, maker, {**body, "subject_id": "*"})                               # no wildcards
    with pytest.raises(ValidationFailed):
        C.create_waiver(db, maker, {**body, "valid_to": "2026-12-31T00:00:00Z"})              # must be time-limited
    w = C.create_waiver(db, maker, body)
    db.commit()
    with pytest.raises(Forbidden):
        C.approve_waiver(db, maker, w.waiver_id, "APPROVE", "approve my own")                 # independent checker only
    C.approve_waiver(db, checker, w.waiver_id, "APPROVE", "reviewed; internal policy only")
    db.commit()
    r = E.evaluate(db, tenant, CTX, "S1", "rsh", margin_result_id="MR-1")
    e = r["evaluations"][0]
    assert e["status"] == "FAIL" and e["waiver_id"] == w.waiver_id                       # raw outcome UNCHANGED, waiver annotated
    assert r["cases"][0]["waiver_id"] == w.waiver_id
    # other subject: waiver does not apply
    other = E.evaluate(db, tenant, {**CTX, "account_id": "ACC-OTHER"}, "S2", "rsh", margin_result_id="MR-1")["evaluations"][0]
    assert other["status"] == "FAIL" and other["waiver_id"] is None
    # after expiry: waiver does not apply
    late = E.evaluate(db, tenant, {**CTX, "as_of": "2026-08-20T00:00:00Z"}, "S3", "rsh", margin_result_id="MR-1")
    up.input_as_of = None
    assert late["evaluations"][0]["waiver_id"] is None


def test_waiver_cannot_override_legal_obligation(db, tenant):
    from app.engines.compliance.controls import register_control
    register_control(db, tenant, {"control_key": "LEGAL-1", "family": "MARGIN_COVERAGE", "title": "statutory", "rule_key": "LAW-1",
                                  "authority_type": "STATUTORY", "scope": {"eq": ["market_type", "OTC"]},
                                  "condition": {"eq": ["terms_status", "FROZEN"]}})
    db.commit()
    with pytest.raises(Forbidden) as e:
        C.create_waiver(db, P(tenant, "u-maker", "maker"), {"control_key": "LEGAL-1", "subject_id": "NS-1", "reason": "please",
                                                           "valid_from": "2026-08-17T00:00:00Z", "valid_to": "2026-08-18T00:00:00Z"})
    assert e.value.code == "LEGAL_OBLIGATION_NOT_WAIVABLE"


# ------------------------------------------------------------------ queue / SLA
def test_sla_uses_explicit_business_clock_and_escalation(db, tenant, up):
    open_case(db, tenant)
    before = C.queue(db, tenant, datetime(2026, 8, 18, 3, 59, tzinfo=UTC))["items"][0]["sla"]
    after = C.queue(db, tenant, datetime(2026, 8, 18, 6, 0, tzinfo=UTC))["items"][0]["sla"]
    assert before["overdue"] is False and before["escalation"] == "NONE"
    assert after["overdue"] is True and after["hours_overdue"] == 2.0 and after["escalation"] == "ESCALATED"


def test_unknown_input_opens_a_data_gap_review_case_with_lower_severity(db, tenant, up):
    up.input_as_of = None
    r = E.evaluate(db, tenant, CTX, "S", "h", margin_result_id="MR-1")
    db.commit()
    c = r["cases"][0]
    assert c["kind"] == "DATA_GAP" and c["last_status"] == "UNKNOWN" and c["severity"] == "MEDIUM"
    assert any(o["route"] == "ROUTE_NEW_SNAPSHOT" for o in C.remedy_options(C.get_case(db, tenant, c["case_id"])))
    _ = CmpCase
