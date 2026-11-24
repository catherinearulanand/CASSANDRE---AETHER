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
     "rule_key": "DEMO-ETD-COVERAGE", "rule_version": 2, "severity": "HIGH", "max_staleness_hours": 24,
     "scope": {"all": [{"eq": ["market_type", "ETD_DOMESTIC"]}, {"eq": ["venue", "DEMO_NSE"]}, {"eq": ["currency", "INR"]}]},
     "condition": {"gte": ["eligible_collateral_inr", "required_margin_inr"]},
     "required_inputs": ["required_margin_inr", "eligible_collateral_inr", "input_as_of"]},
    {"control_key": "CMP-COV-IFSC-USD", "family": "MARGIN_COVERAGE", "title": "IFSC ETD margin coverage (USD, per account)",
     "rule_key": "DEMO-IFSC-COVERAGE", "rule_version": 1, "severity": "HIGH", "max_staleness_hours": 24,
     "scope": {"all": [{"eq": ["market_type", "ETD_IFSC"]}, {"eq": ["venue", "DEMO_IFSC"]}, {"eq": ["currency", "USD"]}]},
     "condition": {"gte": ["eligible_collateral_usd", "required_margin_usd"]},
     "required_inputs": ["required_margin_usd", "eligible_collateral_usd", "input_as_of"]},
    {"control_key": "CMP-POSLIMIT-ETD", "family": "POSITION_LIMIT", "title": "Domestic ETD client position limit (fictional 100 lots)",
     "rule_key": "DEMO-ETD-POSLIMIT", "rule_version": 1, "severity": "MEDIUM", "max_staleness_hours": 24,
     "params": {"required_basis": "GROSS_FUTURES_EQUIV", "aggregation": "CLIENT", "unit": "lots"},
     "scope": {"all": [{"eq": ["market_type", "ETD_DOMESTIC"]}, {"eq": ["client_classification", "CLIENT"]}]},
     "condition": {"lte": ["position_lots", 100]},
     "required_inputs": ["position_lots", "position_basis", "client_classification", "input_as_of"]},
    {"control_key": "CMP-RECON-ETD", "family": "RECONCILIATION", "title": "Clearing margin file reconciliation",
     "rule_key": "DEMO-ETD-MARGIN", "rule_version": 1, "severity": "HIGH", "max_staleness_hours": 24,
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
]
UNSUPPORTED = [
    ("CMP-COLL-CONC", "COLLATERAL_CONCENTRATION", "Collateral eligibility and concentration",
     "No frozen contract exposes allocation constraint slacks to compliance in this build."),
    ("CMP-SETTLE-DEADLINE", "SETTLEMENT_DEADLINE", "Margin available as of settlement deadline",
     "Requires settled-vs-proposed collateral timeline from the collateral engine (not in contract v1)."),
    ("CMP-REPORT-SCHEMA", "REPORTING_COMPLETENESS", "Reporting schema completeness",
     "No approved reporting schema version exists; no regulatory submission is implemented."),
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
