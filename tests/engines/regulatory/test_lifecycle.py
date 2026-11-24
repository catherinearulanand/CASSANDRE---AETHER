from datetime import timedelta

import pytest
from sqlalchemy import select, text

from app.core.errors import Blocked, Conflict, Forbidden, ValidationFailed
from app.db.base import utcnow
from app.db.models_core import ApprovalRow
from app.engines.evidence import service as ev
from app.engines.evidence.models import EvEvent
from app.engines.regulatory import registry
from app.engines.regulatory.models import RegRule
from app.engines.regulatory.resolve import resolve_rules
from tests.engines.regulatory.helpers import CTX, EXPECTED, dt


def actions(env):
    return [e.action for e in env.db.execute(select(EvEvent).where(EvEvent.tenant_id == env.tenant).order_by(EvEvent.sequence)).scalars()]


def keys(res, bucket="applicable"):
    return [(e["rule_key"], e["version"]) for e in res[bucket]]


# ---------------------------------------------------------------- R-A03 / G15
def test_r_a03_g15_author_cannot_approve_denial_is_recorded_and_checker_approves_exact_revision(env):
    r = env.draft()
    registry.submit(env.db, env.maker, r.rule_id, "ready", None)
    with pytest.raises(Forbidden) as e:
        registry.review(env.db, env.maker, r.rule_id, "APPROVE", "self", None)
    assert e.value.code == "SELF_APPROVAL"
    assert env.db.get(RegRule, r.rule_id).status == "REVIEW_REQUIRED"
    assert "APPROVAL_DENIED" in actions(env)  # denial is evidence, committed before the 403
    with pytest.raises(Forbidden) as e2:  # a non-checker role cannot approve either
        registry.review(env.db, env.maker.__class__("x", env.tenant, "x", "x", ("viewer",), (), ""), r.rule_id, "APPROVE", "no", None)
    assert e2.value.code == "CHECKER_ROLE_REQUIRED"
    approved = registry.review(env.db, env.checker, r.rule_id, "APPROVE", "independent review ok", None)
    assert approved.status == "APPROVED" and approved.approved_hash == approved.content_hash
    ap = env.db.execute(select(ApprovalRow).where(ApprovalRow.target_id == r.rule_id)).scalars().one()
    assert ap.maker_id == "maker-1" and ap.checker_id == "checker-1" and ap.decision == "APPROVED" and ap.target_hash == r.content_hash


def test_stale_expected_revision_is_a_conflict(env):
    r = env.draft()
    with pytest.raises(Conflict) as e:
        registry.submit(env.db, env.maker, r.rule_id, "ready", expected_revision=99)
    assert e.value.code == "REVISION_CONFLICT"


def test_reason_and_rationale_are_mandatory(env):
    with pytest.raises(ValidationFailed):
        env.draft(rationale="  ")
    r = env.draft()
    with pytest.raises(ValidationFailed):
        registry.submit(env.db, env.maker, r.rule_id, " ", None)


# ---------------------------------------------------------------- R-A02 no unsupported rule activated
def test_r_a02_rule_without_span_cannot_be_submitted(env):
    r = env.draft(span_ids=[])
    with pytest.raises(Blocked) as e:
        registry.submit(env.db, env.maker, r.rule_id, "go", None)
    assert e.value.code == "SOURCE_BASIS_INVALID" and "NO_SOURCE_SPAN" in e.value.message


def test_r_a02_draft_consultation_source_is_not_a_binding_basis(env):
    cons = env.source(b"Consultation: the member shall not exceed 3 lots.", "DRAFT_CONSULTATION", "c")
    sp = env.span_for(cons, "the member shall not exceed 3 lots")
    r = env.draft(span_ids=[sp.span_id])
    with pytest.raises(Blocked) as e:
        registry.submit(env.db, env.maker, r.rule_id, "go", None)
    assert "SOURCE_NOT_BINDING" in e.value.message


def test_r_a02_real_regulatory_claim_needs_authoritative_source(env):
    r = env.draft(authority_type="REGULATORY_VERIFIED")
    with pytest.raises(Blocked) as e:
        registry.submit(env.db, env.maker, r.rule_id, "go", None)
    assert "SOURCE_AUTHORITY_MISMATCH" in e.value.message


def test_r_a02_tampered_source_text_blocks_activation_even_after_approval(env):
    r = env.approved()
    env.db.execute(text("UPDATE reg_sources SET text = replace(text, 'broker', 'BROKER') WHERE source_id = :s"), {"s": env.src.source_id})
    env.db.expire_all()
    with pytest.raises(Blocked) as e:
        registry.activate(env.db, env.checker, r.rule_id, None, "go")
    assert e.value.code == "SOURCE_BASIS_INVALID" and "QUOTE_MISMATCH_TEXT" in e.value.message
    assert env.db.get(RegRule, r.rule_id).status == "APPROVED"  # never activated


def test_g29_g14_uncompilable_or_untested_rule_cannot_enter_review(env):
    bad = env.draft(key="BAD", params={"limit": {"value": "100"}}, condition={"lte": ["net_position_lots", {"param": "limit"}]})
    assert bad.compile_report["ok"] is False
    with pytest.raises(Blocked) as e:
        registry.submit(env.db, env.maker, bad.rule_id, "go", None)
    assert e.value.code == "COMPILE_FAILED" and "unit" in e.value.message
    notests = env.draft(key="NT", test_vectors=[])
    with pytest.raises(Blocked) as e2:
        registry.submit(env.db, env.maker, notests.rule_id, "go", None)
    assert e2.value.code == "NO_TEST_VECTORS"
    failing = env.draft(key="FT", test_vectors=[{"name": "x", "context": CTX | {"required_margin_inr": "1"}, "expect_scope": "FALSE"}])
    with pytest.raises(Blocked) as e3:
        registry.submit(env.db, env.maker, failing.rule_id, "go", None)
    assert e3.value.code == "TESTS_FAILED"


def test_approved_content_is_immutable_and_hash_binding_blocks_activation(env):
    r = env.approved()
    with pytest.raises(Conflict):
        registry.update_draft(env.db, env.maker, r.rule_id, {"rationale": "sneak", "parameters": {"mult": {"value": "9", "unit": "RATIO"}}}, None)
    env.db.execute(text("UPDATE reg_rules SET parameters = :p WHERE rule_id = :r"), {"p": '{"mult": {"value": "9", "unit": "RATIO"}}', "r": r.rule_id})
    env.db.expire_all()
    with pytest.raises(Blocked) as e:
        registry.activate(env.db, env.checker, r.rule_id, None, "go")
    assert e.value.code == "APPROVAL_HASH_MISMATCH"


def test_rejection_returns_revisable_draft_and_never_edits_approved_content(env):
    r = env.draft()
    registry.submit(env.db, env.maker, r.rule_id, "ready", None)
    rej = registry.review(env.db, env.checker, r.rule_id, "REJECT", "threshold unit unclear", None)
    assert rej.status == "DRAFT" and rej.approved_hash is None
    with pytest.raises(Forbidden):  # only the author edits
        registry.update_draft(env.db, env.checker, r.rule_id, {"rationale": "x"}, None)
    ed = registry.update_draft(env.db, env.maker, r.rule_id, {"rationale": "clarified unit", "parameters": {"mult": {"value": "1.10", "unit": "RATIO"}}}, None)
    assert ed.content_hash != r.content_hash or ed.parameters["mult"]["value"] == "1.1"
    registry.submit(env.db, env.maker, r.rule_id, "resubmit", None)
    assert env.db.get(RegRule, r.rule_id).status == "REVIEW_REQUIRED"


def test_expired_review_window_returns_draft(env):
    r = env.draft()
    registry.submit(env.db, env.maker, r.rule_id, "ready", None)
    ap = env.db.execute(select(ApprovalRow).where(ApprovalRow.target_id == r.rule_id)).scalars().one()
    ap.expires_at = utcnow() - timedelta(seconds=1)
    env.db.flush()
    with pytest.raises(Conflict) as e:
        registry.review(env.db, env.checker, r.rule_id, "APPROVE", "late", None)
    assert e.value.code == "APPROVAL_EXPIRED" and env.db.get(RegRule, r.rule_id).status == "DRAFT"


def test_expired_approval_cannot_activate(env):
    r = env.approved()
    ap = env.db.execute(select(ApprovalRow).where(ApprovalRow.target_id == r.rule_id)).scalars().one()
    ap.expires_at = utcnow() - timedelta(seconds=1)
    env.db.flush()
    with pytest.raises(Conflict) as e:
        registry.activate(env.db, env.checker, r.rule_id, None, "go")
    assert e.value.code == "APPROVAL_EXPIRED"


def test_unapproved_rule_cannot_activate(env):
    r = env.draft()
    with pytest.raises(Conflict) as e:
        registry.activate(env.db, env.checker, r.rule_id, None, "go")
    assert e.value.code == "NOT_APPROVED"


# ---------------------------------------------------------------- R-A04 / bitemporal
def test_r_a04_amendment_effective_at_exact_boundary_and_historical_replay_unchanged(env):
    exp = EXPECTED["R-A04"]
    env.active(key="AMD", eff="2026-03-01T00:00:00Z", recorded="2026-01-15T00:00:00Z", params={"t": {"value": "10", "unit": "RATIO"}})
    env.active(key="AMD", eff="2026-06-01T00:00:00Z", recorded="2026-01-15T00:00:00Z", params={"t": {"value": "8", "unit": "RATIO"}})
    # v3 recorded LATER (2026-05-20) but with an earlier effective date: invisible to a replay known at 2026-05-01
    env.active(key="AMD", eff="2026-07-01T00:00:00Z", recorded="2026-05-20T00:00:00Z", params={"t": {"value": "5", "unit": "RATIO"}})
    ver = lambda a, k=None: resolve_rules(env.db, env.tenant, CTX, dt(a), dt(k) if k else None)["applicable"][0]["version"]  # noqa: E731
    assert ver("2026-05-31T23:59:59Z", "2026-05-01T00:00:00Z") == exp["before_boundary"]
    assert ver("2026-06-01T00:00:00Z", "2026-05-01T00:00:00Z") == exp["at_boundary"]
    assert resolve_rules(env.db, env.tenant, CTX, dt("2026-02-28T23:59:59Z"), None)["applicable"] == []  # before v1 takes effect
    assert ver("2026-07-01T00:00:00Z", "2026-05-01T00:00:00Z") == 2      # v3 unknown at that recording time
    assert ver("2026-07-01T00:00:00Z", None) == 3                         # known now
    h1 = resolve_rules(env.db, env.tenant, CTX, dt("2026-05-31T00:00:00Z"), dt("2026-05-01T00:00:00Z"))["rule_set_hash"]
    env.active(key="AMD", eff="2026-08-01T00:00:00Z", recorded="2026-06-02T00:00:00Z")
    assert resolve_rules(env.db, env.tenant, CTX, dt("2026-05-31T00:00:00Z"), dt("2026-05-01T00:00:00Z"))["rule_set_hash"] == h1  # history intact


def test_future_effective_rule_is_scheduled_not_active_and_advance_is_explicit(env):
    r = env.approved(key="FUT", eff="2027-01-01T00:00:00Z")
    s = registry.activate(env.db, env.checker, r.rule_id, None, "schedule", as_of=dt("2026-08-17T00:00:00Z"), recorded_at=dt("2026-08-17T00:00:00Z"))
    assert s.status == "SCHEDULED"
    assert registry.advance_states(env.db, env.checker, dt("2026-12-31T23:59:59Z")) == []
    assert registry.advance_states(env.db, env.checker, dt("2027-01-01T00:00:00Z")) == [r.rule_id]
    assert env.db.get(RegRule, r.rule_id).status == "ACTIVE" and "RULE_BECAME_EFFECTIVE" in actions(env)


def test_overlapping_revision_is_rejected(env):
    env.active(key="OV", eff="2026-03-01T00:00:00Z")
    r2 = env.approved(key="OV", eff="2026-02-01T00:00:00Z")  # would take effect BEFORE the version it supersedes
    with pytest.raises(Conflict) as e:
        registry.activate(env.db, env.checker, r2.rule_id, None, "go")
    assert e.value.code == "OVERLAPPING_REVISION"


def test_supersession_marks_predecessor_when_effective(env):
    v1 = env.active(key="SUP", eff="2026-01-01T00:00:00Z")
    v2 = env.active(key="SUP", eff="2026-02-01T00:00:00Z", as_of="2026-08-01T00:00:00Z")
    env.db.refresh(v1)
    assert v1.status == "SUPERSEDED" and v1.superseded_by_rule_id == v2.rule_id and v2.supersedes_rule_id == v1.rule_id
    assert "RULE_SUPERSEDED" in actions(env)


def test_withdraw_is_a_recorded_revision_not_a_rewrite(env):
    r = env.active(key="WD", eff="2026-01-01T00:00:00Z", recorded="2026-01-02T00:00:00Z")
    registry.withdraw(env.db, env.checker, r.rule_id, "emergency rollback", None, recorded_at=dt("2026-06-01T00:00:00Z"))
    assert keys(resolve_rules(env.db, env.tenant, CTX, dt("2026-07-01T00:00:00Z"), dt("2026-05-01T00:00:00Z"))) == [("WD", 1)]  # as known then
    assert keys(resolve_rules(env.db, env.tenant, CTX, dt("2026-07-01T00:00:00Z"), dt("2026-06-02T00:00:00Z"))) == []
    clone = registry.revise(env.db, env.maker, r.rule_id, "restore previous content as new version", None)
    assert clone.version == 2 and clone.status == "DRAFT" and clone.content_hash != r.content_hash  # version is part of the hashed content


# ---------------------------------------------------------------- R-A05 / R-A06 / G20 resolution
def test_r_a05_contradictory_equal_rank_rules_block_and_neither_applies(env):
    env.active(key="C1", conflict_group="G", params={"cap": {"value": "100", "unit": "LOTS"}})
    env.active(key="C2", conflict_group="G", params={"cap": {"value": "80", "unit": "LOTS"}})
    res = resolve_rules(env.db, env.tenant, CTX, dt("2026-08-01T00:00:00Z"))
    assert [c["conflict_group"] for c in res["conflicts"]] == ["G"] and res["applicable"] == []


def test_precedence_by_approved_rank_and_additive_stricter_coexist(env):
    env.active(key="P1", conflict_group="H", precedence_rank=1, params={"cap": {"value": "100", "unit": "LOTS"}})
    env.active(key="P2", conflict_group="H", precedence_rank=2, params={"cap": {"value": "80", "unit": "LOTS"}})
    res = resolve_rules(env.db, env.tenant, CTX, dt("2026-08-01T00:00:00Z"))
    assert keys(res) == [("P1", 1)] and res["excluded"][0]["reason"] == "LOWER_PRECEDENCE_BY_APPROVED_RANK" and res["conflicts"] == []
    env.active(key="S1", conflict_group="I", additive_stricter=True, params={"cap": {"value": "100", "unit": "LOTS"}})
    env.active(key="S2", conflict_group="I", additive_stricter=True, params={"cap": {"value": "80", "unit": "LOTS"}})
    res2 = resolve_rules(env.db, env.tenant, CTX, dt("2026-08-01T00:00:00Z"))
    assert {"S1", "S2"} <= {k for k, _ in keys(res2)} and res2["conflicts"] == []


def test_r_a06_missing_entity_category_is_unknown_not_applicable(env):
    scope = {"all": [{"eq": ["market_type", "ETD_DOMESTIC"]}, {"in": ["entity_category", ["TRADING_MEMBER"]]}]}
    env.active(key="EC", scope=scope, test_vectors=[
        {"name": "member", "context": CTX | {"entity_category": "TRADING_MEMBER", "required_margin_inr": "1"}, "expect_scope": "TRUE", "expect_condition": "TRUE"},
        {"name": "category missing is UNKNOWN", "context": CTX | {"required_margin_inr": "1"}, "expect_scope": "UNKNOWN", "expect_condition": "TRUE"}])
    unk = resolve_rules(env.db, env.tenant, CTX, dt("2026-08-01T00:00:00Z"))
    assert unk["applicable"] == [] and keys(unk, "unknown") == [("EC", 1)] and unk["unknown"][0]["missing_context"] == ["entity_category"]
    ok = resolve_rules(env.db, env.tenant, CTX | {"entity_category": "TRADING_MEMBER"}, dt("2026-08-01T00:00:00Z"))
    assert keys(ok) == [("EC", 1)]
    other = resolve_rules(env.db, env.tenant, CTX | {"entity_category": "CLIENT"}, dt("2026-08-01T00:00:00Z"))
    assert keys(other, "excluded") == [("EC", 1)]


def test_g20_ifsc_profile_is_independent_of_domestic_policy_via_seed(env):
    from app.engines.regulatory.fixtures import seed_fixtures
    seed_fixtures(env.db, env.tenant)
    asof = dt("2026-08-17T04:00:00Z")
    dom = resolve_rules(env.db, env.tenant, {"market_type": "ETD_DOMESTIC", "venue": "DEMO_NSE"}, asof)
    ifs = resolve_rules(env.db, env.tenant, {"market_type": "ETD_IFSC", "venue": "DEMO_IFSC"}, asof)
    assert {k for k, _ in keys(dom)} == {"DEMO-ETD-COVERAGE", "DEMO-ETD-MARGIN", "DEMO-ETD-POSLIMIT"}
    assert keys(ifs) == [("DEMO-IFSC-COVERAGE", 1)]
    assert "DEMO-ETD-COVERAGE" in {k for k, _ in keys(ifs, "excluded")}  # domestic rule excluded, no currency blending
    # seeded history: v2 margin revision is REVIEW_REQUIRED so it is not in force; v1 governs
    assert ("DEMO-ETD-MARGIN", 1) in keys(dom)
    # resolved rules expose a param-inlined AST for engines with their own evaluator
    lim = next(e for e in dom["applicable"] if e["rule_key"] == "DEMO-ETD-POSLIMIT")
    assert {"const": "100", "unit": "LOTS"} in lim["resolved_dsl"]["condition"]["all"][1]["lte"]
    assert seed_fixtures(env.db, env.tenant)["seeded"] is False  # idempotent


def test_resolve_rejects_naive_datetimes(env):
    from datetime import datetime
    with pytest.raises(ValidationFailed):
        resolve_rules(env.db, env.tenant, CTX, datetime(2026, 8, 1))


# ---------------------------------------------------------------- diff & evidence
def test_diff_reports_added_removed_modified_and_spans(env):
    a = env.active(key="DF", eff="2026-01-01T00:00:00Z", params={"m": {"value": "1.00", "unit": "RATIO"}})
    b = env.approved(key="DF", eff="2026-09-01T00:00:00Z", params={"m": {"value": "1.25", "unit": "RATIO"}, "n": {"value": "3", "unit": "COUNT"}},
                     condition={"gte": ["eligible_collateral_inr", "required_margin_inr"]},
                     test_vectors=[{"name": "cov", "context": CTX | {"eligible_collateral_inr": "2", "required_margin_inr": "1"}}])
    d = registry.diff(a, b)
    assert {"path": "parameters.m.value", "from": "1", "to": "1.25"} in d["modified"]
    assert any(x["path"] == "parameters.n.value" for x in d["added"]) and any(x["path"].startswith("condition.exists") for x in d["removed"])
    assert "eligible_collateral_inr" in d["affected_fields"] and not d["identical"]


def test_every_consequential_write_emitted_valid_chained_evidence(env):
    r = env.draft(key="EVD")
    registry.submit(env.db, env.maker, r.rule_id, "go", None)
    registry.review(env.db, env.checker, r.rule_id, "APPROVE", "ok", None)
    registry.activate(env.db, env.checker, r.rule_id, None, "go", as_of=dt("2026-08-01T00:00:00Z"))
    env.db.flush()
    assert actions(env) == ["SOURCE_INGESTED", "RULE_DRAFTED", "RULE_SUBMITTED", "RULE_APPROVED", "RULE_ACTIVATED"]
    assert ev.verify_chain(env.db, env.tenant)["valid"]
