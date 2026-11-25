"""Deterministic control library definitions + registration/validation.

All executable rules here are SYNTHETIC_POLICY (fictional), never presented as statutory.
Controls that need inputs no frozen contract provides are registered as UNSUPPORTED (counted in coverage,
never silently skipped, never counted as passing).
"""
from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select

from app.core.canonical import content_hash
from app.core.errors import ValidationFailed
from app.engines.compliance import dsl
from app.engines.compliance.models import CmpControl

LEGAL_AUTHORITY = {"STATUTORY", "REGULATORY_DIRECTION", "EXCHANGE_RULE", "CLEARING_RULE"}
WAIVABLE_AUTHORITY = {"INTERNAL_POLICY", "SYNTHETIC_POLICY"}
EFFECTIVE0 = datetime(2026, 8, 17, tzinfo=UTC)
_MKT = {"in": ["market_type", ["OTC", "ETD_DOMESTIC", "ETD_IFSC"]]}

LIBRARY: list[dict] = [
    {"control_key": "CMP-COV-ETD-INR", "family": "MARGIN_COVERAGE", "title": "Domestic ETD margin coverage (INR, per account)",
     "rule_key": "DEMO-ETD-COVERAGE", "rule_version": 1, "severity": "HIGH", "max_staleness_hours": 24, "frequency": "DAILY",
     "scope": {"all": [{"eq": ["market_type", "ETD_DOMESTIC"]}, {"eq": ["venue", "DEMO_NSE"]}, {"eq": ["currency", "INR"]}]},
     "condition": {"gte": ["eligible_collateral_inr", "required_margin_inr"]},
     "required_inputs": ["required_margin_inr", "eligible_collateral_inr", "input_as_of"]},
    {"control_key": "CMP-COV-IFSC-USD", "family": "MARGIN_COVERAGE", "title": "IFSC ETD margin coverage (USD, per account)",
     "rule_key": "DEMO-IFSC-COVERAGE", "rule_version": 1, "severity": "HIGH", "max_staleness_hours": 24, "frequency": "DAILY",
     "scope": {"all": [{"eq": ["market_type", "ETD_IFSC"]}, {"eq": ["venue", "DEMO_IFSC"]}, {"eq": ["currency", "USD"]}]},
     "condition": {"gte": ["eligible_collateral_usd", "required_margin_usd"]},
     "required_inputs": ["required_margin_usd", "eligible_collateral_usd", "input_as_of"]},
    {"control_key": "CMP-POSLIMIT-ETD", "family": "POSITION_LIMIT", "title": "Domestic ETD client position limit (fictional 100 lots)",
     "rule_key": "DEMO-ETD-POSLIMIT", "rule_version": 1, "severity": "MEDIUM", "max_staleness_hours": 24, "frequency": "DAILY",
     "params": {"required_basis": "GROSS_FUTURES_EQUIV", "aggregation": "CLIENT", "unit": "lots"},
     "scope": {"all": [{"eq": ["market_type", "ETD_DOMESTIC"]}, {"eq": ["client_classification", "CLIENT"]}]},
     "condition": {"lte": ["position_lots", 100]},
     "required_inputs": ["position_lots", "position_basis", "client_classification", "input_as_of"]},
    {"control_key": "CMP-RECON-ETD", "family": "RECONCILIATION", "title": "Clearing margin file reconciliation",
     "rule_key": "DEMO-ETD-MARGIN", "rule_version": 1, "severity": "HIGH", "max_staleness_hours": 24, "frequency": "DAILY",
     "scope": {"eq": ["market_type", "ETD_DOMESTIC"]},
     "condition": {"eq": ["reconciliation_status", "MATCHED"]},
     "required_inputs": ["reconciliation_status", "input_as_of"]},
    {"control_key": "CMP-FRESH-SOURCE", "family": "FRESHNESS", "title": "Policy source freshness (fictional 720h)",
     "rule_key": "DEMO-SOURCE-FRESHNESS", "rule_version": 1, "severity": "LOW", "max_staleness_hours": None,
     "scope": _MKT, "condition": {"lte": ["source_age_hours", 720]}, "required_inputs": ["source_age_hours"]},
    {"control_key": "CMP-AGREEMENT-OTC", "family": "AGREEMENT", "title": "Bilateral agreement terms frozen and in force",
     "rule_key": "DEMO-OTC-VM", "rule_version": 1, "severity": "HIGH", "max_staleness_hours": None,
     "scope": {"eq": ["market_type", "OTC"]},
     "condition": {"all": [{"eq": ["terms_status", "FROZEN"]},
                           {"effective_on": ["agreement_effective_from", "agreement_effective_to"]}]},
     "required_inputs": ["terms_status", "agreement_effective_from"]},
    # ---- round 2: controls that were UNSUPPORTED in round 1 now have inputs via contract v1.1 (rule_version 2 supersedes the
    # unsupported v1 row of the same key). All are SYNTHETIC policy; none is presented as statutory.
    {"control_key": "CMP-COLL-CONC", "family": "COLLATERAL_CONCENTRATION", "title": "Collateral allocation within concentration caps (per proposal)",
     "rule_key": "DEMO-COLLATERAL-CONC", "rule_version": 2, "severity": "MEDIUM", "max_staleness_hours": None, "frequency": "EVENT",
     "scope": {"in": ["market_type", ["OTC", "ETD_DOMESTIC", "ETD_IFSC"]]},
     "condition": {"eq": ["concentration_breaches", 0]},
     "required_inputs": ["concentration_breaches"]},
    {"control_key": "CMP-SETTLE-DEADLINE", "family": "SETTLEMENT_DEADLINE", "title": "Posted collateral covers margin at the settlement deadline (INR)",
     "rule_key": "DEMO-ETD-SETTLEMENT", "rule_version": 2, "severity": "HIGH", "max_staleness_hours": 24, "frequency": "EVENT",
     "scope": {"all": [{"eq": ["market_type", "ETD_DOMESTIC"]}, {"eq": ["venue", "DEMO_NSE"]}, {"eq": ["currency", "INR"]}]},
     "condition": {"gte": ["deadline_collateral_inr", "required_margin_inr"]},
     "required_inputs": ["required_margin_inr", "deadline_collateral_inr", "input_as_of"]},
    {"control_key": "CMP-SETTLE-DEADLINE-USD", "family": "SETTLEMENT_DEADLINE", "title": "Posted collateral covers margin at the settlement deadline (IFSC USD)",
     "rule_key": "DEMO-IFSC-SETTLEMENT", "rule_version": 1, "severity": "HIGH", "max_staleness_hours": 24, "frequency": "EVENT",
     "scope": {"all": [{"eq": ["market_type", "ETD_IFSC"]}, {"eq": ["venue", "DEMO_IFSC"]}, {"eq": ["currency", "USD"]}]},
     "condition": {"gte": ["deadline_collateral_usd", "required_margin_usd"]},
     "required_inputs": ["required_margin_usd", "deadline_collateral_usd", "input_as_of"]},
    {"control_key": "CMP-REPORT-SCHEMA", "family": "REPORTING_COMPLETENESS",
     "title": "Synthetic margin-report record complete and well-formed (NOT an approved regulatory schema; never transmitted)",
     "rule_key": "DEMO-REPORT-SCHEMA", "rule_version": 2, "severity": "LOW", "max_staleness_hours": None, "frequency": "EVENT",
     "scope": _MKT, "condition": {"all": [{"eq": ["report_missing_fields", 0]}, {"eq": ["report_format_errors", 0]}]},
     "required_inputs": ["report_missing_fields", "report_format_errors"]},
]
# Controls still lacking inputs. Empty after round 2; kept so the mechanism (and its tests) remain.
UNSUPPORTED: list[tuple[str, str, str, str]] = [
]


def definition_hash(d: dict) -> str:
    return content_hash({"rule_key": d["rule_key"], "rule_version": d["rule_version"], "scope": d["scope"],
                         "condition": d["condition"], "params": d.get("params", {})})


def validate_definition(d: dict) -> None:
    """Compile-time gate (G29): rule must compile against the approved registry; thresholds must be valid."""
    for k in ("control_key", "family", "title", "rule_key", "scope", "condition", "authority_type"):
        if not d.get(k):
            raise ValidationFailed(f"missing {k}", field_errors={k: "required"})
    try:
        dsl.validate(d["scope"])
        dsl.validate(d["condition"])
    except dsl.DslError as exc:
        raise ValidationFailed(f"rule cannot compile: {exc}", field_errors={"condition": exc.code}) from exc
    if d["authority_type"] in ("SYNTHETIC_POLICY",) and d.get("claims_statutory"):
        raise ValidationFailed("synthetic rule must not be presented as statutory")


def register_control(session, tenant_id: str, d: dict, *, effective_from: datetime | None = None) -> CmpControl:
    d = {**d, "authority_type": d.get("authority_type", "SYNTHETIC_POLICY")}
    unsupported = d.get("support_status") == "UNSUPPORTED"
    if not unsupported:
        validate_definition(d)
    version = int(d.get("rule_version", 1))
    existing = session.execute(select(CmpControl).where(CmpControl.tenant_id == tenant_id, CmpControl.control_key == d["control_key"],
                                                        CmpControl.rule_version == version)).scalar_one_or_none()
    if existing:
        return existing
    c = CmpControl(
        tenant_id=tenant_id, control_key=d["control_key"], family=d["family"], title=d["title"], rule_key=d["rule_key"],
        rule_version=version, rule_hash=definition_hash({**d, "rule_version": version}) if not unsupported else "0" * 64,
        authority_type=d["authority_type"], scope=d.get("scope") or {}, condition=d.get("condition") or {},
        params=d.get("params", {}), required_inputs=d.get("required_inputs", []), frequency=d.get("frequency", "EVENT"),
        severity=d.get("severity", "HIGH"), max_staleness_hours=d.get("max_staleness_hours"),
        support_status="UNSUPPORTED" if unsupported else "SUPPORTED", unsupported_reason=d.get("unsupported_reason"),
        effective_from=effective_from or EFFECTIVE0, definition_hash=definition_hash({**d, "rule_version": version, "scope": d.get("scope") or {}, "condition": d.get("condition") or {}}),
        provenance="SYNTHETIC", source_ref=f"rule:{d['rule_key']}@{version}")
    session.add(c)
    session.flush()
    return c


def seed_library(session, tenant_id: str) -> list[CmpControl]:
    out = [register_control(session, tenant_id, {**d, "authority_type": "SYNTHETIC_POLICY"}) for d in LIBRARY]
    for key, fam, title, why in UNSUPPORTED:
        out.append(register_control(session, tenant_id, {
            "control_key": key, "family": fam, "title": title, "rule_key": "UNSUPPORTED", "rule_version": 1,
            "authority_type": "SYNTHETIC_POLICY", "support_status": "UNSUPPORTED", "unsupported_reason": why,
            "severity": "LOW"}))
    # Upgrade path: a tenant seeded in round 1 holds an UNSUPPORTED v1 row for keys that now have a SUPPORTED v2.
    # The old row is retired (active=False), never deleted: history stays queryable and nothing is rewritten.
    supported_keys = {c.control_key for c in out if c.support_status == "SUPPORTED" and c.rule_version > 1}
    for old in session.execute(select(CmpControl).where(CmpControl.tenant_id == tenant_id, CmpControl.support_status == "UNSUPPORTED",
                                                        CmpControl.active.is_(True))).scalars():
        if old.control_key in supported_keys:
            old.active = False
    session.flush()
    return out


def supersede_control(session, tenant_id: str, control_key: str, new_def: dict, effective_from: datetime) -> tuple[CmpControl, CmpControl]:
    """New immutable revision; the old one gets effective_to. History is never rewritten (G23)."""
    cur = session.execute(select(CmpControl).where(CmpControl.tenant_id == tenant_id, CmpControl.control_key == control_key,
                                                   CmpControl.effective_to.is_(None), CmpControl.active.is_(True))
                          .order_by(CmpControl.rule_version.desc())).scalars().first()
    if cur is None:
        raise ValidationFailed("no active control to supersede")
    if effective_from <= cur.effective_from:
        raise ValidationFailed("supersession must be effective after the current revision")
    d = {**new_def, "control_key": control_key, "family": cur.family, "title": new_def.get("title", cur.title),
         "rule_key": new_def.get("rule_key", cur.rule_key), "rule_version": cur.rule_version + 1,
         "authority_type": new_def.get("authority_type", cur.authority_type),
         "severity": new_def.get("severity", cur.severity), "max_staleness_hours": cur.max_staleness_hours,
         "scope": new_def.get("scope", cur.scope), "condition": new_def.get("condition", cur.condition),
         "params": new_def.get("params", cur.params), "required_inputs": cur.required_inputs}
    new = register_control(session, tenant_id, d, effective_from=effective_from)
    cur.effective_to = effective_from
    session.flush()
    return new, cur


def reevaluate_affected(session, tenant_id: str, control_key: str, as_of: datetime, actor: dict, *, correlation_id: str | None = None,
                        force: bool = False) -> dict:
    """G23: after a control supersession, re-evaluate ONLY the subjects that had a (non-recheck) evaluation under this
    control_key. Controls of other keys and subjects never evaluated for this control are not touched. Subjects whose
    latest outcome was NOT_APPLICABLE and whose scope is unchanged are skipped (their outcome cannot change).
    Inputs are the facts persisted with the subject's latest evaluation (no new data is invented). History is preserved:
    new evaluations are appended; old ones are never edited."""
    from app.engines.compliance import engine as E
    from app.engines.compliance.models import CmpEvaluation
    cid = correlation_id or E.new_id()
    new = E.active_controls(session, tenant_id, as_of, only_key=control_key)
    if not new:
        raise ValidationFailed("no active revision of the control at as_of", field_errors={"as_of": "no active revision"})
    new = new[-1]
    rows = session.execute(select(CmpEvaluation).where(CmpEvaluation.tenant_id == tenant_id, CmpEvaluation.control_key == control_key,
                                                       CmpEvaluation.is_recheck.is_(False))
                           .order_by(CmpEvaluation.as_of, CmpEvaluation.created_at)).scalars().all()
    latest: dict[str, CmpEvaluation] = {}
    for e in rows:
        latest[e.subject_id] = e
    touched, skipped, cases = [], [], []
    for subj, last in sorted(latest.items()):
        if last.rule_version == new.rule_version and not force:
            skipped.append({"subject_id": subj, "reason": "ALREADY_EVALUATED_UNDER_CURRENT_REVISION"})
            continue
        old_ctrl = session.execute(select(CmpControl).where(CmpControl.control_id == last.control_id)).scalar_one()
        if last.status == "NOT_APPLICABLE" and old_ctrl.scope == new.scope:
            skipped.append({"subject_id": subj, "reason": "NOT_APPLICABLE_AND_SCOPE_UNCHANGED"})
            continue
        old_facts = dict(last.facts)
        ctx = {"market_type": last.market_type, "venue": last.venue, "account_id": old_facts.get("account_id") or subj,
               "currency": old_facts.get("currency"), "jurisdiction": old_facts.get("jurisdiction"), "as_of": as_of.isoformat()}
        if last.snapshot_id and old_facts.get("settlement_deadline"):
            ctx["settlement_deadline"] = old_facts["settlement_deadline"]
        ctx = E.normalise_context(ctx)
        # Fresh inputs from the owning engines as of the effective date (stored facts may be stale and would only yield UNKNOWN).
        # Facts that only a caller could supply are carried over, still labelled CALLER_SUPPLIED; their input_as_of keeps them honest.
        carry = {k: v for k, v in old_facts.items() if (last.fact_provenance or {}).get(k) == "CALLER_SUPPLIED" and k in E.CALLER_ALLOWED}
        facts, prov, reasons = E.gather_facts(session, tenant_id, ctx, last.margin_result_id, last.allocation_id, carry or None, {new.family})
        ev, created = E.run_control(session, tenant_id, new, ctx, facts, prov, reasons, as_of, last.snapshot_id,
                                    last.rule_set_hash, last.margin_result_id, last.allocation_id, cid, actor)
        touched.append({"subject_id": subj, "evaluation_id": ev.evaluation_id, "status": ev.status, "created": created})
        if created:
            c = E.open_or_update_case(session, tenant_id, ev, actor, {**ctx, "snapshot_id": last.snapshot_id, "rule_set_hash": last.rule_set_hash,
                                                                      "correlation_id": cid})
            if c:
                cases.append(c.case_id)
    return {"control_key": control_key, "new_rule_version": new.rule_version, "re_evaluated": touched, "skipped": skipped,
            "cases_touched": sorted(set(cases)), "correlation_id": cid,
            "note": "Only subjects previously evaluated under this control were re-evaluated; other controls/subjects untouched; history retained."}
