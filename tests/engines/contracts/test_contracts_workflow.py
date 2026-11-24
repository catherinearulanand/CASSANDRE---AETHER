"""Service-level workflow tests against the real DB: maker/checker (C-A07, G15), freeze enforcement, stale approvals,
concurrent freeze, effective-dated amendments + historical reproducibility (C-A06, C-A08, G08), blocking gates
(C-A02, C-A03, G14), cross-master (C-A05, G09), evidence + outbox atomicity."""
import threading
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.core import services
from app.core.errors import (Blocked, Conflict, Forbidden, NotFound, NotImplementedCapability, ValidationFailed)
from app.core.events import EVENT_OWNER
from app.db.base import SessionLocal
from app.db.models_core import ApprovalRow, OutboxEvent
from app.engines.contracts import fixtures as F
from app.engines.contracts import services as S
from app.engines.contracts.models import ConField, ConVersion
from app.engines.evidence.models import EvEvent
from tests.engines.contracts.conftest import GOLD, new_pkg, principal, spec_by_ns

AS_OF = datetime(2026, 8, 17, 4, tzinfo=UTC)


def fields_of(db, pid):
    return list(db.execute(select(ConField).where(ConField.package_id == pid)).scalars())


def approve_all(db, checker, pid):
    return F._approve_all(db, checker, "c", pid)


def frozen_pkg(db, tenant, maker, checker, ns="NS-DEMO-G25"):
    pkg = new_pkg(db, maker, ns)
    S.extract(db, maker, "c", pkg["package_id"], "RULES")
    approve_all(db, checker, pkg["package_id"])
    v = F._freeze(db, maker, checker, "c", pkg["package_id"])
    db.commit()
    return pkg, v


def events(db, tenant):
    return [e.action for e in db.execute(select(EvEvent).where(EvEvent.tenant_id == tenant).order_by(EvEvent.sequence)).scalars()]


# ---------------------------------------------------------------- maker / checker
def test_maker_role_cannot_approve_and_admin_cannot_approve(db, tenant, maker, admin):
    pkg = new_pkg(db, maker)
    S.extract(db, maker, "c", pkg["package_id"], "RULES")
    f = fields_of(db, pkg["package_id"])[0]
    for actor in (maker, admin):
        with pytest.raises(Forbidden):
            S.review_field(db, actor, "c", pkg["package_id"], f.field_id, "APPROVE", "looks right to me", f.revision)


def test_same_user_cannot_approve_own_extraction_or_correction_g15(db, tenant, maker):
    both = principal(tenant, "u-both", "maker", "checker")
    pkg = new_pkg(db, both)
    S.extract(db, both, "c", pkg["package_id"], "RULES")
    f = fields_of(db, pkg["package_id"])[0]
    with pytest.raises(Forbidden) as e:
        S.review_field(db, both, "c", pkg["package_id"], f.field_id, "APPROVE", "self approving this", f.revision)
    assert e.value.code == "SELF_APPROVAL"


def test_correction_preserves_original_and_corrector_cannot_approve(db, tenant, maker, maker2, checker):
    pkg = new_pkg(db, maker)
    pid = pkg["package_id"]
    S.extract(db, maker, "c", pid, "RULES")
    f = next(x for x in fields_of(db, pid) if x.field_key == "mta_a")
    out = S.review_field(db, maker2, "c", pid, f.field_id, "CORRECT", "units checked against clause", f.revision,
                         corrected_value={"amount": "25", "currency": "USD"})
    assert out["value"] == {"amount": "25", "currency": "USD"} and out["original_value"] == {"amount": "10", "currency": "USD"}
    assert out["maker_id"] == "u-maker2" and out["status"] == "PROPOSED"
    both = principal(tenant, "u-maker2", "maker", "checker")  # same identity as the corrector
    with pytest.raises(Forbidden):
        S.review_field(db, both, "c", pid, f.field_id, "APPROVE", "approving own correction", out["revision"])
    ok = S.review_field(db, checker, "c", pid, f.field_id, "APPROVE", "independent check done", out["revision"])
    assert ok["status"] == "APPROVED" and ok["reviewer_id"] == "u-checker"


def test_reason_mandatory_and_revision_conflict(db, tenant, maker, checker):
    pkg = new_pkg(db, maker)
    pid = pkg["package_id"]
    S.extract(db, maker, "c", pid, "RULES")
    f = fields_of(db, pid)[0]
    with pytest.raises(ValidationFailed):
        S.review_field(db, checker, "c", pid, f.field_id, "APPROVE", "", f.revision)
    with pytest.raises(Conflict) as e:
        S.review_field(db, checker, "c", pid, f.field_id, "APPROVE", "valid reason here", f.revision + 5)
    assert e.value.code == "REVISION_CONFLICT"


def test_invalid_correction_rejected_and_unresolved_cannot_be_approved(db, tenant, maker, checker):
    pkg = new_pkg(db, maker)
    pid = pkg["package_id"]
    S.extract(db, maker, "c", pid, "RULES")
    f = next(x for x in fields_of(db, pid) if x.field_key == "eligible_schedule")
    bad = [{"asset_class": "CASH", "currency": "USD", "issuer_class": "ANY", "valuation_pct": "90", "haircut_pct": "90",
            "fx_haircut_pct": "0", "concentration_cap_pct": None, "cap_denominator": None, "min_rating": None, "max_maturity_years": None}]
    with pytest.raises(ValidationFailed):  # C-A09 through the review path
        S.review_field(db, maker, "c", pid, f.field_id, "CORRECT", "attempt inconsistent schedule", f.revision, corrected_value=bad)
    g = next(x for x in fields_of(db, pid) if x.field_key == "mta_a")
    S.review_field(db, checker, "c", pid, g.field_id, "MARK_UNRESOLVED", "dispute with counterparty open", g.revision)
    g = db.get(ConField, g.field_id)
    with pytest.raises(Blocked):
        S.review_field(db, checker, "c", pid, g.field_id, "APPROVE", "approve anyway please", g.revision)


# ---------------------------------------------------------------- freeze
def test_freeze_requires_independent_checker_and_exact_hash(db, tenant, maker, checker):
    both = principal(tenant, "u-maker", "maker", "checker")  # same identity as maker
    pkg = new_pkg(db, maker)
    pid = pkg["package_id"]
    S.extract(db, maker, "c", pid, "RULES")
    approve_all(db, checker, pid)
    ap = S.propose_freeze(db, maker, "c", pid, "ready for independent freeze approval")
    with pytest.raises(Forbidden) as e:
        S.freeze(db, both, "c", pid, ap["approval_id"], "self freeze attempt here")
    assert e.value.code == "SELF_APPROVAL"
    v = S.freeze(db, checker, "c", pid, ap["approval_id"], "independent checker approves hash")
    assert v["version_no"] == 1 and v["bundle_hash"] == ap["target_hash"] and v["checker_id"] == "u-checker"
    row = db.get(ApprovalRow, ap["approval_id"])
    assert row.decision == "APPROVED" and row.target_hash == v["bundle_hash"]
    with pytest.raises(Conflict) as e2:  # single use
        S.freeze(db, checker, "c", pid, ap["approval_id"], "reusing approval again")
    assert e2.value.code == "APPROVAL_NOT_PENDING"


def test_freeze_blocked_until_every_field_is_dispositioned(db, tenant, maker, checker):
    pkg = new_pkg(db, maker)
    pid = pkg["package_id"]
    S.extract(db, maker, "c", pid, "RULES")
    with pytest.raises(Blocked) as e:
        S.propose_freeze(db, maker, "c", pid, "nothing reviewed yet at all")
    assert "UNRESOLVED_FIELDS" in str(e.value)


def test_stale_approval_after_content_change(db, tenant, maker, checker):
    pkg = new_pkg(db, maker)
    pid = pkg["package_id"]
    S.extract(db, maker, "c", pid, "RULES")
    approve_all(db, checker, pid)
    ap = S.propose_freeze(db, maker, "c", pid, "approval that will go stale")
    f = next(x for x in fields_of(db, pid) if x.field_key == "mta_a")
    S.review_field(db, checker, "c", pid, f.field_id, "MARK_UNRESOLVED", "new dispute raised after proposal", f.revision)
    with pytest.raises(Conflict):
        S.freeze(db, checker, "c", pid, ap["approval_id"], "should be rejected as stale")
    assert db.get(ApprovalRow, ap["approval_id"]).decision == "EXPIRED"


def test_expired_approval_rejected(db, tenant, maker, checker):
    pkg = new_pkg(db, maker)
    pid = pkg["package_id"]
    S.extract(db, maker, "c", pid, "RULES")
    approve_all(db, checker, pid)
    ap = S.propose_freeze(db, maker, "c", pid, "short lived approval")
    db.get(ApprovalRow, ap["approval_id"]).expires_at = datetime.now(UTC) - timedelta(seconds=1)
    db.flush()
    with pytest.raises(Conflict) as e:
        S.freeze(db, checker, "c", pid, ap["approval_id"], "too late to approve")
    assert e.value.code == "STALE_APPROVAL"


def test_frozen_terms_are_read_only_server_side(db, tenant, maker, checker):
    pkg, _ = frozen_pkg(db, tenant, maker, checker)
    pid = pkg["package_id"]
    f = fields_of(db, pid)[0]
    with pytest.raises(Conflict) as e:
        S.review_field(db, checker, "c", pid, f.field_id, "REJECT", "trying to edit frozen field", f.revision)
    assert e.value.code == "FIELD_FROZEN"
    with pytest.raises(Conflict) as e2:
        S.add_document(db, maker, "c", pid, {"doc_ref": "X", "text": "x", "effective_from": "2026-01-01T00:00:00Z"}, amendment=False)
    assert e2.value.code == "PACKAGE_FROZEN"
    with pytest.raises(Conflict) as e3:
        S.propose_freeze(db, maker, "c", pid, "nothing changed since freeze")
    assert e3.value.code == "NOTHING_TO_FREEZE"


def test_amendment_requires_frozen_base(db, tenant, maker):
    pkg = new_pkg(db, maker)
    with pytest.raises(Conflict) as e:
        S.add_document(db, maker, "c", pkg["package_id"], {"doc_ref": "A", "text": "x", "effective_from": "2026-01-01T00:00:00Z"}, amendment=True)
    assert e.value.code == "NO_FROZEN_TERMS_TO_AMEND"


def test_concurrent_freeze_only_one_succeeds_c06(db, tenant, maker, checker):
    pkg = new_pkg(db, maker)
    pid = pkg["package_id"]
    S.extract(db, maker, "c", pid, "RULES")
    approve_all(db, checker, pid)
    ap = S.propose_freeze(db, maker, "c", pid, "concurrent freeze race test")
    db.commit()
    results = []

    def go(uid):
        try:
            with SessionLocal() as s:
                S.freeze(s, principal(tenant, uid, "checker"), "c", pid, ap["approval_id"], "racing checker approval")
                s.commit()
                results.append("ok")
        except Conflict as e:
            results.append(e.code)

    ts = [threading.Thread(target=go, args=(f"chk{i}",)) for i in range(2)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sorted(results) == ["APPROVAL_NOT_PENDING", "ok"]
    n = db.execute(select(func.count()).select_from(ConVersion).where(ConVersion.package_id == pid)).scalar_one()
    assert n == 1


# ---------------------------------------------------------------- gates (C-A02, C-A03, G14) and gold resolutions
def test_missing_mta_blocks_freeze_and_resolution_c_a03(db, tenant, maker, checker, seeded):
    r = S.resolve_terms(db, tenant, "NS-DEMO-G14-NOMTA", AS_OF)
    assert r["status"] == "BLOCKED" and r["terms_hash"] is None and r["terms"] is None
    for expected in GOLD["blocked"]["NS-DEMO-G14-NOMTA"]:
        assert expected in r["blocking_reasons"]
    pid = db.execute(select(S.ConPackage.package_id).where(S.ConPackage.tenant_id == tenant,
                                                           S.ConPackage.netting_set_id == "NS-DEMO-G14-NOMTA")).scalar_one()
    with pytest.raises(Blocked) as e:
        S.propose_freeze(db, maker, "c", pid, "attempt freeze without MTA")
    assert "MISSING_REQUIRED_FIELD:mta_a" in str(e.value)


@pytest.mark.gold
def test_gold_resolutions_independent_expected_values(db, tenant, seeded):
    for ns, cases in GOLD["resolutions"].items():
        for c in cases:
            r = S.resolve_terms(db, tenant, ns, datetime.fromisoformat(c["as_of"].replace("Z", "+00:00")), None, c.get("fact_snapshot_id"))
            assert r["status"] == "RESOLVED", (ns, c, r["blocking_reasons"])
            t = r["terms"]
            for k_exp, k_terms in (("threshold_a", "threshold_a"), ("threshold_b", "threshold_b"), ("mta_a", "mta_a"),
                                   ("ia_a", "independent_amount_a"), ("rounding_increment", "rounding_increment"),
                                   ("rounding_mode", "rounding_mode"), ("mta_semantics", "mta_semantics")):
                if k_exp in c:
                    assert t[k_terms] == c[k_exp], (ns, c["as_of"], k_exp)


def test_unresolved_rating_trigger_blocks_and_future_fact_is_not_used_c_a02(db, tenant, seeded):
    ns = "NS-DEMO-G14-RATING"
    r = S.resolve_terms(db, tenant, ns, AS_OF)
    assert r["status"] == "BLOCKED" and any(b.startswith("UNRESOLVED_CONDITIONAL_FACT:threshold_b") for b in r["blocking_reasons"])
    assert r["required_facts"] == ["rating:ENT-OTHERCP"]
    r2 = S.resolve_terms(db, tenant, ns, AS_OF, None, "FACT-RATING-FUTURE")  # effective 2026-12-31 > as_of
    assert r2["status"] == "BLOCKED"
    r3 = S.resolve_terms(db, tenant, ns, AS_OF, None, "FACT-NOT-THERE")
    assert r3["blocking_reasons"] == ["FACT_SNAPSHOT_NOT_FOUND:FACT-NOT-THERE"]
    late = S.resolve_terms(db, tenant, ns, datetime(2027, 1, 5, tzinfo=UTC), None, "FACT-RATING-FUTURE")
    assert late["status"] == "RESOLVED" and late["terms"]["threshold_b"] == "0"  # once effective, the fact applies


def test_missing_annex_blocks_freeze(db, tenant, maker, checker):
    body = F.package_body(spec_by_ns("NS-DEMO-G25"))
    body["documents"][0]["missing_annexes"] = ["Annex 2 (page 7 missing from scan)"]
    pkg = S.create_package(db, maker, "c", body)
    S.extract(db, maker, "c", pkg["package_id"], "RULES")
    approve_all(db, checker, pkg["package_id"])
    with pytest.raises(Blocked) as e:
        S.propose_freeze(db, maker, "c", pkg["package_id"], "freeze with missing annex")
    assert "MISSING_ANNEX" in str(e.value)


# ---------------------------------------------------------------- amendments / history
def test_amendment_effective_date_and_historical_reproducibility_c_a06_c_a08(db, tenant, maker, checker):
    pkg, v1 = frozen_pkg(db, tenant, maker, checker)
    pid, ns = pkg["package_id"], "NS-DEMO-G25"
    q = datetime(2026, 8, 17, tzinfo=UTC)
    before = S.resolve_terms(db, tenant, ns, q)
    db.commit()
    assert before["terms"]["threshold_a"] == "0"
    known_after_v1 = datetime.now(UTC)
    amd = F.load_doc("amendment_001_csa_mer_nwd_001.txt").replace("Northwind Demo Clearing", "Orchid Demo Counterparty") \
        .replace("USD 8,000,000", "USD 5,000,000")
    S.add_document(db, maker, "c", pid, {"doc_ref": "AMD-G25-1", "text": amd, "effective_from": "2026-09-01T00:00:00Z"}, amendment=True)
    S.extract(db, maker, "c", pid, "RULES")
    approve_all(db, checker, pid)
    v2 = F._freeze(db, maker, checker, "c", pid)
    db.commit()
    assert v2["version_no"] == 2 and v2["supersedes_version_id"] == v1["version_id"]
    assert S.list_versions(db, tenant, pid)["items"][0]["status"] == "SUPERSEDED"
    # amended value applies only from its effective date
    assert S.resolve_terms(db, tenant, ns, datetime(2026, 8, 31, 23, 59, 59, tzinfo=UTC))["terms"]["threshold_a"] == "0"
    assert S.resolve_terms(db, tenant, ns, datetime(2026, 9, 1, tzinfo=UTC))["terms"]["threshold_a"] == "5000000"
    # historical replay: knowledge as of before v2 reproduces the pre-amendment hash exactly
    again = S.resolve_terms(db, tenant, ns, q, known_after_v1)
    assert again["terms_hash"] == before["terms_hash"] and again["version_no"] == 1
    assert S.resolve_terms(db, tenant, ns, datetime(2026, 9, 2, tzinfo=UTC), known_after_v1)["terms"]["threshold_a"] == "0"
    # terms are re-readable by hash for downstream engines
    assert S.terms_by_hash(db, tenant, before["terms_hash"])["terms"] == before["terms"]
    cmp_ = S.compare_versions(db, tenant, pid, v1["version_id"], v2["version_id"], datetime(2026, 9, 1, tzinfo=UTC))
    assert {c["key"] for c in cmp_["resolved_changes_at_as_of"]} == {"threshold_a", "threshold_b"}
    assert cmp_["affected_netting_sets"][0]["netting_set_id"] == ns


def test_amended_bundle_hash_differs_and_prior_hash_unchanged(db, tenant, maker, checker, seeded):
    vs = S.list_versions(db, tenant, db.execute(select(S.ConPackage.package_id).where(
        S.ConPackage.tenant_id == tenant, S.ConPackage.netting_set_id == "NS-MER-NWD-001")).scalar_one(), True)["items"]
    assert [v["version_no"] for v in vs] == [1, 2] and vs[0]["bundle_hash"] != vs[1]["bundle_hash"]
    assert vs[0]["status"] == "SUPERSEDED" and vs[1]["status"] == "FROZEN"


def test_bundle_hash_is_deterministic_across_tenants(db, maker, checker):
    a, b = f"ct-{id(maker)}-a", f"ct-{id(checker)}-b"
    hashes = []
    for t in (a, b):
        m, c = principal(t, "m", "maker"), principal(t, "c", "checker")
        _, v = frozen_pkg(db, t, m, c)
        hashes.append(v["bundle_hash"])
    assert hashes[0] == hashes[1]


# ---------------------------------------------------------------- netting (C-A05, G09)
def test_cross_master_trade_rejected_and_unknown_quarantined(db, tenant, seeded):
    by = {t["trade_id"]: t for t in seeded["trades"]}
    assert by["TRD-G09-001"]["status"] == "MEMBER" and by["TRD-G09-001"]["netting_set_id"] == "NS-MER-NWD-001"
    assert by["TRD-G09-002"]["status"] == "MEMBER" and by["TRD-G09-002"]["netting_set_id"] == "NS-MER-OTH-002"
    assert by["TRD-G09-X"]["status"] == "REJECTED" and "CROSS_MASTER" in by["TRD-G09-X"]["reason"]
    assert by["TRD-UNKNOWN-NS"]["status"] == "QUARANTINED"
    m = S.netting_membership(db, tenant, "TRD-G09-X")
    assert m["netting_set_id"] is None  # rejected trade joins no set
    assert S.netting_membership(db, tenant, "NEVER-SEEN")["netting_set_id"] is None
    assert S.netting_membership(db, tenant, "TRD-G09-001")["netting_set_id"] != S.netting_membership(db, tenant, "TRD-G09-002")["netting_set_id"]
    assert "approved close-out netting authorisation" in by["TRD-G09-001"]["reason"]


def test_netting_set_cannot_be_registered_under_two_masters(db, tenant, maker, seeded):
    body = F.package_body(spec_by_ns("NS-DEMO-G25"))
    body.update(master_id="MA-SOMEONE-ELSE", netting_set_id="NS-MER-NWD-001")
    with pytest.raises(Conflict) as e:
        S.create_package(db, maker, "c", body)
    assert e.value.code == "CROSS_MASTER_NETTING_SET"


def test_trade_mapping_requires_authorised_netting_and_is_not_silently_changed(db, tenant, maker):
    pkg = new_pkg(db, maker)  # extracted? no: no approved netting_authorization yet
    r = S.register_trade(db, maker, "c", "TRD-EARLY", "MA-DEMO-G25", "NS-DEMO-G25")
    assert r["status"] == "QUARANTINED" and "NETTING_NOT_AUTHORISED" in r["reason"]
    assert S.register_trade(db, maker, "c", "TRD-EARLY", "MA-DEMO-G25", "NS-DEMO-G25")["status"] == "QUARANTINED"  # idempotent
    with pytest.raises(Conflict):
        S.register_trade(db, maker, "c", "TRD-EARLY", "MA-OTHER", "NS-DEMO-G25")
    _ = pkg


# ---------------------------------------------------------------- package validation
def test_swapped_parties_in_document_header_rejected_at_creation(db, tenant, maker):
    body = F.package_body(spec_by_ns("NS-DEMO-G25"))
    body["party_a_name"], body["party_b_name"] = body["party_b_name"], body["party_a_name"]
    with pytest.raises(ValidationFailed) as e:
        S.create_package(db, maker, "c", body)
    assert "DOC_PARTY_MISMATCH" in str(e.value.field_errors)


def test_doc_date_mismatch_and_duplicate_document(db, tenant, maker):
    body = F.package_body(spec_by_ns("NS-DEMO-G25"))
    body["documents"][0]["effective_from"] = "2026-02-01T00:00:00Z"
    with pytest.raises(ValidationFailed) as e:
        S.create_package(db, maker, "c", body)
    assert "DOC_DATE_MISMATCH" in str(e.value.field_errors)
    body = F.package_body(spec_by_ns("NS-DEMO-G25"))
    body["documents"] = body["documents"] * 2
    with pytest.raises(ValidationFailed):
        S.create_package(db, maker, "c", body)


def test_live_model_not_implemented_and_replay_needs_labels(db, tenant, maker):
    pkg = new_pkg(db, maker)
    with pytest.raises(NotImplementedCapability):
        S.extract(db, maker, "c", pkg["package_id"], "LIVE_MODEL")
    with pytest.raises(ValidationFailed) as e:
        S.extract(db, maker, "c", pkg["package_id"], "REPLAY")  # no hand labels for the G25 doc
    assert e.value.code == "NO_REPLAY_FOR_DOCUMENT"


def test_extract_only_once_per_document(db, tenant, maker):
    pkg = new_pkg(db, maker)
    S.extract(db, maker, "c", pkg["package_id"], "RULES")
    with pytest.raises(Conflict) as e:
        S.extract(db, maker, "c", pkg["package_id"], "RULES")
    assert e.value.code == "NOTHING_TO_EXTRACT"


# ---------------------------------------------------------------- tenancy
def test_other_tenant_cannot_see_or_resolve(db, tenant, seeded):
    other = f"{tenant}-other"
    pid = db.execute(select(S.ConPackage.package_id).where(S.ConPackage.tenant_id == tenant)).scalars().first()
    with pytest.raises(NotFound):
        S.package_detail(db, other, pid)
    r = S.resolve_terms(db, other, "NS-MER-NWD-001", AS_OF)
    assert r["status"] == "BLOCKED" and r["blocking_reasons"] == ["NETTING_SET_UNKNOWN:NS-MER-NWD-001"]
    assert S.netting_membership(db, other, "TRD-G09-001")["netting_set_id"] is None
    assert S.list_packages(db, other)["items"] == []
    th = S.resolve_terms(db, tenant, "NS-MER-NWD-001", AS_OF)["terms_hash"]
    assert S.terms_by_hash(db, other, th) is None


# ---------------------------------------------------------------- evidence + events
def test_evidence_emitted_for_every_consequential_write_and_chain_valid(db, tenant, seeded):
    acts = set(events(db, tenant))
    for a in ("CONTRACT_PACKAGE_CREATED", "CONTRACT_EXTRACTED", "CONTRACT_FIELD_REVIEWED", "TERMS_FREEZE_PROPOSED", "TERMS_APPROVED",
              "TERMS_FROZEN", "AMENDMENT_ADDED", "NETTING_MAPPING_RECORDED", "FACT_SNAPSHOT_RECORDED"):
        assert a in acts, a
    assert services.call("evidence.verify_chain", db, tenant)["valid"] is True
    ev = db.execute(select(EvEvent).where(EvEvent.tenant_id == tenant, EvEvent.action == "TERMS_FROZEN")).scalars().first()
    assert ev.engine == "contracts" and ev.source_spans and ev.input_hashes and ev.output_hash


def test_failed_action_leaves_no_evidence_atomicity(db, tenant, maker, checker):
    pkg = new_pkg(db, maker)
    pid = pkg["package_id"]
    S.extract(db, maker, "c", pid, "RULES")
    db.commit()
    before = len(events(db, tenant))
    try:
        S.propose_freeze(db, maker, "c", pid, "unapproved fields will block this")
    except Blocked:
        db.rollback()
    assert len(events(db, tenant)) == before


def test_terms_events_emitted_by_owner_with_contract_shape(db, tenant, seeded):
    rows = db.execute(select(OutboxEvent).where(OutboxEvent.tenant_id == tenant,
                                                OutboxEvent.event_type.in_(["TermsApproved", "TermsFrozen"]))).scalars().all()
    assert {r.event_type for r in rows} == {"TermsApproved", "TermsFrozen"}
    assert EVENT_OWNER["TermsApproved"] == EVENT_OWNER["TermsFrozen"] == "contracts"
    for r in rows:
        e = r.envelope
        for k in ("event_id", "event_type", "schema_version", "tenant_id", "correlation_id", "aggregate_id", "aggregate_revision",
                  "occurred_at", "effective_at", "actor_id", "actor_type", "payload_hash", "payload"):
            assert k in e
        assert set(e["payload"]) == {"version_id", "bundle_hash", "netting_set_id"}


def test_seed_is_idempotent(db, tenant, seeded):
    n = len(events(db, tenant))
    again = F.seed_fixtures(db, tenant)
    assert set(again["packages"].values()) == {"EXISTS"}
    assert len(events(db, tenant)) == n
