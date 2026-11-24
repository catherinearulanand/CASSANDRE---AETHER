"""Evaluation engine: fact gathering from other engines (via the service registry only), per-control deterministic
evaluation, persistence, evidence, case opening. UNKNOWN never passes. Nothing here changes a rule or a margin."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select

from app.core import services
from app.core.canonical import CanonicalError, canonical_bytes, content_hash
from app.core.errors import ApiError, ValidationFailed
from app.core.events import emit
from app.core.money import dstr, to_decimal
from app.db.base import new_id
from app.engines.compliance import dsl
from app.engines.compliance.controls import WAIVABLE_AUTHORITY
from app.engines.compliance.models import CmpCase, CmpCaseEvent, CmpControl, CmpEvaluation, CmpWaiver

SLA_HOURS = {"CRITICAL": 4, "HIGH": 24, "MEDIUM": 72, "LOW": 120}
SEV_ORDER = ["LOW", "MEDIUM", "HIGH", "CRITICAL"]
CASE_STATUSES = {"FAIL", "CONFLICT", "ERROR", "UNKNOWN"}
SERVICE_KEYS = {"required_margin_inr", "eligible_collateral_inr", "required_margin_usd", "eligible_collateral_usd"}
CONTEXT_KEYS = {"market_type", "venue", "jurisdiction", "account_id", "currency", "as_of"}
CALLER_ALLOWED = {"position_lots", "position_basis", "client_classification", "terms_status", "agreement_effective_from",
                  "agreement_effective_to", "source_age_hours", "source_retrieved_at", "reconciliation_status",
                  "positions", "input_as_of"}


def ts(v, name="as_of") -> datetime:
    try:
        return dsl.parse_ts(v)
    except Exception as exc:  # noqa: BLE001
        raise ValidationFailed(f"{name} must be an ISO-8601 timestamp with timezone", field_errors={name: "invalid"}) from exc


def severity_for(base: str, status: str) -> str:
    i = SEV_ORDER.index(base)
    if status == "UNKNOWN":
        i = min(i, 1)  # missing data is a review item, capped at MEDIUM
    elif status == "ERROR":
        i = min(i, 1)
    return SEV_ORDER[i]


def normalise_context(context: dict) -> dict:
    if not context.get("as_of"):
        raise ValidationFailed("context.as_of is required (no implicit clock)", field_errors={"as_of": "required"})
    ctx = {k: context.get(k) for k in ("market_type", "venue", "jurisdiction", "account_id", "currency", "netting_set_id")}
    ctx["as_of"] = ts(context["as_of"]).isoformat()
    if not ctx["market_type"]:
        raise ValidationFailed("context.market_type is required", field_errors={"market_type": "required"})
    ctx["subject_id"] = ctx.get("account_id") or ctx.get("netting_set_id")
    if not ctx["subject_id"]:
        raise ValidationFailed("context.account_id or netting_set_id is required", field_errors={"account_id": "required"})
    return ctx


RISK_FIELDS = ("required_margin_inr", "required_margin_usd")
COV_FIELDS = ("eligible_collateral_inr", "eligible_collateral_usd")


def _r(msg: str, affects) -> dict:
    return {"msg": msg, "affects": list(affects)}


def _relevant(reasons, c: CmpControl) -> list[str]:
    """An input problem only blocks controls that depend on that input (an unavailable collateral lookup must not
    make an unrelated position-limit control UNKNOWN). Plain-string reasons apply to every control."""
    need = set(c.required_inputs or [])
    return [r if isinstance(r, str) else r["msg"] for r in reasons if isinstance(r, str) or need & set(r["affects"])]


def gather_facts(session, tenant_id: str, ctx: dict, margin_result_id, allocation_id, caller_facts: dict | None):
    """-> (facts, provenance, input_reasons). Missing/unavailable inputs become reasons, never zeros."""
    as_of = ts(ctx["as_of"])
    facts: dict = {k: ctx[k] for k in ("market_type", "venue", "jurisdiction", "account_id", "currency") if ctx.get(k)}
    prov = {k: "CONTEXT" for k in facts}
    reasons: list[str] = []
    ccy = (ctx.get("currency") or "").upper() or None
    if margin_result_id:
        try:
            res = services.call("risk.get_result", session, tenant_id, margin_result_id)
            if res.get("status") != "OK":
                reasons.append(_r(f"MARGIN_RESULT_{res.get('status', 'UNKNOWN')}", RISK_FIELDS))
            else:
                req = res["required_amount"]
                ccy = req["currency"]
                if ccy in ("INR", "USD"):
                    facts[f"required_margin_{ccy.lower()}"] = req["amount"]
                    prov[f"required_margin_{ccy.lower()}"] = "SERVICE:risk.get_result"
                else:
                    reasons.append(_r(f"MARGIN_CURRENCY_UNSUPPORTED_{ccy}", RISK_FIELDS))
                for k in ("reconciliation_status",):
                    if res.get(k):
                        facts[k], prov[k] = res[k], "SERVICE:risk.get_result"
                if res.get("as_of") or res.get("snapshot_as_of"):
                    facts["input_as_of"] = res.get("as_of") or res.get("snapshot_as_of")
                    prov["input_as_of"] = "SERVICE:risk.get_result"
        except ApiError as exc:
            reasons.append(_r(f"RISK_RESULT_UNAVAILABLE:{exc.code}", RISK_FIELDS))
    if (margin_result_id or allocation_id) and ctx.get("account_id") and ccy in ("INR", "USD"):
        try:
            cov = services.call("collateral.coverage", session, tenant_id, ctx["account_id"], as_of)
            ep = cov["eligible_posted"]
            if ep["currency"] != ccy:
                reasons.append(_r(f"COVERAGE_CURRENCY_MISMATCH:{ep['currency']}!={ccy}", COV_FIELDS))
            else:
                facts[f"eligible_collateral_{ccy.lower()}"] = ep["amount"]
                prov[f"eligible_collateral_{ccy.lower()}"] = "SERVICE:collateral.coverage (posted only)"
        except ApiError as exc:
            reasons.append(_r(f"COVERAGE_UNAVAILABLE:{exc.code}", COV_FIELDS))
    if caller_facts:
        bad = sorted(set(caller_facts) - CALLER_ALLOWED)
        if bad:
            raise ValidationFailed("facts not accepted from callers (must come from their owning engine): " + ", ".join(bad),
                                   field_errors={b: "not caller-suppliable" for b in bad})
        for k, v in caller_facts.items():
            try:
                canonical_bytes(v)
            except CanonicalError as exc:
                raise ValidationFailed(f"fact {k}: {exc}", field_errors={k: "floats not accepted; use decimal strings"}) from exc
            if k not in facts:
                facts[k], prov[k] = v, "CALLER_SUPPLIED"
    if "source_retrieved_at" in facts and "source_age_hours" not in facts:
        age = (as_of - ts(facts["source_retrieved_at"], "source_retrieved_at")).total_seconds() / 3600
        facts["source_age_hours"] = dstr(Decimal(str(round(age, 6))))
        prov["source_age_hours"] = "DERIVED(as_of - source_retrieved_at)"
    facts.pop("source_retrieved_at", None)
    return facts, prov, reasons


def _gap(trace: list[dict]):
    for t in trace:
        if t.get("result") == dsl.FALSE and t["op"] in ("gte", "gt", "lte", "lt") and t.get("left") is not None:
            try:
                l, r = to_decimal(t["left"]), to_decimal(t["right"])
            except Exception:  # noqa: BLE001
                continue
            return t["left"], t["right"], dstr(abs(l - r)), t.get("unit")
    for t in trace:
        if t.get("result") == dsl.FALSE and t["op"] == "eq":
            return t.get("left"), t.get("right"), None, t.get("unit")
    return None, None, None, None


def evaluate_control(c: CmpControl, facts: dict, as_of: datetime, input_reasons: list[str], rule_state: dict | None = None) -> dict:
    """Pure deterministic evaluation of one control. -> dict(status, reasons, trace, measured, limit, gap, unit)"""
    out = {"status": "UNKNOWN", "reasons": [], "trace": {"scope": [], "condition": []}, "measured": None,
           "limit": None, "gap": None, "unit": None}
    rs = rule_state or {}
    try:
        if rs.get("conflict"):
            out.update(status="CONFLICT", reasons=["RULE_CONFLICT: incompatible applicable mandates; no model-selected precedence"])
            return out
        scope, st = dsl.evaluate_tri(c.scope, facts, as_of)
        out["trace"]["scope"] = st
        if scope == dsl.FALSE:
            out.update(status="NOT_APPLICABLE", reasons=["scope predicate FALSE"])
            return out
        if scope == dsl.UNKNOWN:
            out.update(status="UNKNOWN", reasons=["scope cannot be decided: required context missing (e.g. entity/client classification)"])
            return out
        if rs.get("unknown"):
            out.update(status="UNKNOWN", reasons=["RULE_APPLICABILITY_UNKNOWN per regulatory resolution"])
            return out
        if rs.get("stale_control"):
            out.update(status="UNKNOWN", reasons=["CONTROL_STALE_VS_ACTIVE_RULE: control pins an older rule revision than the active one"])
            return out
        reasons = _relevant(input_reasons, c)
        want = c.params.get("required_basis")
        if want and facts.get("position_basis") != want:
            reasons.append(f"MEASURE_BASIS_MISMATCH: rule measures {want}, input provides {facts.get('position_basis')!r}")
        if c.max_staleness_hours is not None:
            ia = facts.get("input_as_of")
            if ia is None:
                reasons.append("INPUT_AS_OF_MISSING: cannot establish data freshness (fail closed)")
            else:
                age = as_of - ts(ia, "input_as_of")
                if age > timedelta(hours=c.max_staleness_hours):
                    reasons.append(f"STALE_INPUT: input is {age.total_seconds() / 3600:.2f}h old, limit {c.max_staleness_hours}h")
                if age < timedelta(0):
                    reasons.append("INPUT_FROM_FUTURE: input timestamp is after the evaluation as_of")
        if reasons:
            out.update(status="UNKNOWN", reasons=reasons)
            return out
        cond, ct = dsl.evaluate_tri(c.condition, facts, as_of)
        out["trace"]["condition"] = ct
        if cond == dsl.TRUE:
            out.update(status="PASS", reasons=["condition TRUE"])
        elif cond == dsl.FALSE:
            m, l, g, u = _gap(ct)
            out.update(status="FAIL", reasons=["condition FALSE"], measured=m, limit=l, gap=g, unit=u)
        else:
            out.update(status="UNKNOWN", reasons=["condition UNKNOWN: required input missing"])
    except dsl.DslError as exc:
        out.update(status="ERROR", reasons=[f"{exc.code}: {exc.message}"])
    return out


def _rule_state(session, tenant_id, c: CmpControl, ctx: dict, as_of: datetime, rule_set_hash) -> tuple[dict, str]:
    """Rule currency check against A1 when available. Unavailable => explicitly UNVERIFIED (never silently 'current')."""
    if not services.is_registered("regulatory.resolve_rules"):
        return {}, "UNVERIFIED (regulatory service not available)"
    try:
        r = services.call("regulatory.resolve_rules", session, tenant_id, ctx, as_of, None)
    except ApiError as exc:
        return {}, f"UNVERIFIED ({exc.code})"
    key = c.rule_key
    if any(x.get("rule_key") == key for x in r.get("conflicts", [])):
        return {"conflict": True}, "CONFLICT"
    if any(x.get("rule_key") == key for x in r.get("unknown", [])):
        return {"unknown": True}, "UNKNOWN"
    app = next((x for x in r.get("applicable", []) if x.get("rule_key") == key), None)
    if app and int(app.get("version", c.rule_version)) != c.rule_version:
        return {"stale_control": True}, f"STALE (active v{app['version']} vs control v{c.rule_version})"
    return {}, "VERIFIED" if app else "NOT_LISTED_AS_APPLICABLE"


def active_waiver(session, tenant_id, control_key, subject_id, as_of):
    return session.execute(select(CmpWaiver).where(
        CmpWaiver.tenant_id == tenant_id, CmpWaiver.control_key == control_key, CmpWaiver.subject_id == subject_id,
        CmpWaiver.status == "ACTIVE", CmpWaiver.valid_from <= as_of, CmpWaiver.valid_to > as_of)).scalars().first()


def _ev(session, tenant_id, actor, action, entity_type, entity_id, rev, cid, payload, effective_at=None, rationale=None, inputs=None):
    services.call("evidence.append", session, tenant_id, engine="compliance", action=action, actor_id=actor["id"],
                  actor_type=actor["type"], actor_role=actor.get("role"), entity_type=entity_type, entity_id=entity_id,
                  entity_revision=rev, correlation_id=cid, effective_at=effective_at, payload=payload, rationale=rationale,
                  input_hashes=inputs or [])


def evaluation_out(e: CmpEvaluation) -> dict:
    return {"evaluation_id": e.evaluation_id, "control_key": e.control_key, "rule_key": e.rule_key, "rule_version": e.rule_version,
            "rule_hash": e.rule_hash, "subject_id": e.subject_id, "market_type": e.market_type, "venue": e.venue,
            "status": e.status, "severity": e.severity, "reasons": e.reasons, "trace": e.trace, "measured": e.measured,
            "limit": e.limit_value, "gap": e.gap, "unit": e.unit, "as_of": e.as_of.isoformat(), "snapshot_id": e.snapshot_id,
            "rule_set_hash": e.rule_set_hash, "margin_result_id": e.margin_result_id, "allocation_id": e.allocation_id,
            "facts": e.facts, "fact_provenance": e.fact_provenance, "waiver_id": e.waiver_id, "case_id": e.case_id,
            "is_recheck": e.is_recheck, "created_at": e.created_at.isoformat(), "correlation_id": e.correlation_id}


def case_out(c: CmpCase) -> dict:
    return {"case_id": c.case_id, "kind": c.kind, "control_key": c.control_key, "rule_version": c.rule_version, "family": c.family,
            "subject_id": c.subject_id, "market_type": c.market_type, "title": c.title, "state": c.state, "severity": c.severity,
            "last_status": c.last_status, "owner_id": c.owner_id, "maker_id": c.maker_id, "opened_as_of": c.opened_as_of.isoformat(),
            "last_as_of": c.last_as_of.isoformat(), "due_at": c.due_at.isoformat(), "occurrence_count": c.occurrence_count,
            "reopen_count": c.reopen_count, "margin_result_id": c.margin_result_id, "allocation_id": c.allocation_id,
            "first_evaluation_id": c.first_evaluation_id, "last_evaluation_id": c.last_evaluation_id, "remediations": c.remediations,
            "pending_dismissal": c.pending_dismissal, "last_recheck": c.last_recheck, "waiver_id": c.waiver_id,
            "resolution": c.resolution, "watchlist": c.watchlist, "revision": c.revision, "provenance": c.provenance}


def add_case_event(session, tenant_id, case_id, event_type, actor_id, *, frm=None, to=None, comment=None, data=None, as_of=None):
    session.add(CmpCaseEvent(tenant_id=tenant_id, case_id=case_id, event_type=event_type, actor_id=actor_id, from_state=frm,
                             to_state=to, comment=comment, data=data or {}, as_of=as_of))


def open_or_update_case(session, tenant_id: str, ev: CmpEvaluation, actor: dict, eval_context: dict | None = None) -> CmpCase | None:
    """One case per (control, subject, rule revision); recurrences aggregate, individual evaluations are retained."""
    if ev.status not in CASE_STATUSES:
        # a PASS never auto-closes a case (no closure on narrative or absence of alerts); it is only noted.
        open_case = session.execute(select(CmpCase).where(
            CmpCase.tenant_id == tenant_id, CmpCase.occurrence_key == _okey(ev), CmpCase.state.notin_(["RESOLVED", "DISMISSED"]))).scalar_one_or_none()
        if open_case and ev.status == "PASS":
            add_case_event(session, tenant_id, open_case.case_id, "PASS_OBSERVED", actor["id"], as_of=ev.as_of,
                           comment="Later evaluation PASSED; closure still requires remediation, recheck and independent review",
                           data={"evaluation_id": ev.evaluation_id})
        return None
    key = _okey(ev)
    c = session.execute(select(CmpCase).where(CmpCase.tenant_id == tenant_id, CmpCase.occurrence_key == key)).scalar_one_or_none()
    sev = ev.severity
    wv = active_waiver(session, tenant_id, ev.control_key, ev.subject_id, ev.as_of)
    if c is None:
        ctrl = session.execute(select(CmpControl).where(CmpControl.control_id == ev.control_id)).scalar_one()
        c = CmpCase(tenant_id=tenant_id, case_id=new_id(), occurrence_key=key,
                    kind="CONTROL_BREACH" if ev.status in ("FAIL", "CONFLICT") else "DATA_GAP", control_key=ev.control_key,
                    rule_version=ev.rule_version, family=ctrl.family, subject_id=ev.subject_id, market_type=ev.market_type,
                    title=f"{ev.control_key} {ev.status} for {ev.subject_id}", state="OPEN", severity=sev, last_status=ev.status,
                    opened_as_of=ev.as_of, last_as_of=ev.as_of, due_at=ev.as_of + timedelta(hours=SLA_HOURS[sev]),
                    first_evaluation_id=ev.evaluation_id, last_evaluation_id=ev.evaluation_id, margin_result_id=ev.margin_result_id,
                    allocation_id=ev.allocation_id, eval_context=eval_context or {}, waiver_id=wv.waiver_id if wv else None,
                    provenance="SYNTHETIC", source_ref=f"control:{ev.control_key}")
        session.add(c)
        session.flush()
        add_case_event(session, tenant_id, c.case_id, "OPENED", actor["id"], to="OPEN", as_of=ev.as_of,
                       data={"evaluation_id": ev.evaluation_id, "status": ev.status, "gap": ev.gap, "unit": ev.unit})
        _ev(session, tenant_id, actor, "CASE_OPENED", "case", c.case_id, c.revision, ev.correlation_id,
            {"control_key": ev.control_key, "subject_id": ev.subject_id, "status": ev.status, "gap": ev.gap, "unit": ev.unit,
             "evaluation_id": ev.evaluation_id}, effective_at=ev.as_of, inputs=[ev.facts_hash])
        emit(session, event_type="CaseOpened", tenant_id=tenant_id, correlation_id=ev.correlation_id, aggregate_id=c.case_id,
             actor_id=actor["id"], actor_type=actor["type"], payload={"control_key": ev.control_key, "subject_id": ev.subject_id},
             effective_at=ev.as_of)
    else:
        c.occurrence_count += 1
        c.last_evaluation_id, c.last_status, c.last_as_of = ev.evaluation_id, ev.status, ev.as_of
        if ev.margin_result_id:
            c.margin_result_id = ev.margin_result_id
        if ev.allocation_id:
            c.allocation_id = ev.allocation_id
        c.waiver_id = wv.waiver_id if wv else None
        if SEV_ORDER.index(sev) > SEV_ORDER.index(c.severity):
            c.severity = sev
        if c.state in ("RESOLVED", "DISMISSED"):
            prev = c.state
            c.state, c.reopen_count, c.due_at = "OPEN", c.reopen_count + 1, ev.as_of + timedelta(hours=SLA_HOURS[c.severity])
            c.resolution, c.pending_dismissal, c.last_recheck = None, None, None
            add_case_event(session, tenant_id, c.case_id, "REOPENED", actor["id"], frm=prev, to="OPEN", as_of=ev.as_of,
                           comment="Failure recurred", data={"evaluation_id": ev.evaluation_id})
        else:
            add_case_event(session, tenant_id, c.case_id, "OCCURRENCE", actor["id"], as_of=ev.as_of,
                           data={"evaluation_id": ev.evaluation_id, "count": c.occurrence_count})
        c.revision += 1
        c.updated_at = datetime.now(UTC)
        _ev(session, tenant_id, actor, "CASE_UPDATED", "case", c.case_id, c.revision, ev.correlation_id,
            {"occurrence_count": c.occurrence_count, "state": c.state, "evaluation_id": ev.evaluation_id}, effective_at=ev.as_of)
        emit(session, event_type="CaseUpdated", tenant_id=tenant_id, correlation_id=ev.correlation_id, aggregate_id=c.case_id,
             aggregate_revision=c.revision, actor_id=actor["id"], actor_type=actor["type"], payload={"state": c.state}, effective_at=ev.as_of)
    ev.case_id = c.case_id
    session.flush()
    return c


def _okey(ev: CmpEvaluation) -> str:
    return f"{ev.control_key}|{ev.subject_id}|r{ev.rule_version}"


def run_control(session, tenant_id, c: CmpControl, ctx, facts, prov, input_reasons, as_of, snapshot_id, rule_set_hash,
                margin_result_id, allocation_id, cid, actor, *, is_recheck=False) -> tuple[CmpEvaluation, bool]:
    """Evaluate + persist (idempotent on eval_key). Returns (evaluation, created)."""
    rstate, rcur = _rule_state(session, tenant_id, c, {k: ctx.get(k) for k in ("market_type", "venue", "jurisdiction", "account_id", "currency")}, as_of, rule_set_hash)
    res = evaluate_control(c, facts, as_of, input_reasons, rstate)
    facts_hash = content_hash(facts)
    ekey = content_hash({"c": c.control_key, "v": c.rule_version, "s": ctx["subject_id"], "snap": snapshot_id, "as_of": as_of,
                         "f": facts_hash, "m": margin_result_id, "a": allocation_id, "rs": rule_set_hash, "rc": is_recheck})
    existing = session.execute(select(CmpEvaluation).where(CmpEvaluation.tenant_id == tenant_id, CmpEvaluation.eval_key == ekey)).scalar_one_or_none()
    if existing:
        return existing, False
    wv = active_waiver(session, tenant_id, c.control_key, ctx["subject_id"], as_of) if res["status"] in CASE_STATUSES else None
    ev = CmpEvaluation(
        tenant_id=tenant_id, evaluation_id=new_id(), eval_key=ekey, control_id=c.control_id, control_key=c.control_key,
        rule_key=c.rule_key, rule_version=c.rule_version, rule_hash=c.rule_hash, subject_type="ACCOUNT" if ctx.get("account_id") else "NETTING_SET",
        subject_id=ctx["subject_id"], market_type=ctx["market_type"], venue=ctx.get("venue"), status=res["status"],
        severity=severity_for(c.severity, res["status"]), reasons=res["reasons"],
        trace={**res["trace"], "rule_currency": rcur, "control_authority": c.authority_type, "rule_hash": c.rule_hash},
        measured=res["measured"], limit_value=res["limit"], gap=res["gap"], unit=res["unit"], as_of=as_of, snapshot_id=snapshot_id,
        rule_set_hash=rule_set_hash, margin_result_id=margin_result_id, allocation_id=allocation_id, facts=facts,
        fact_provenance=prov, facts_hash=facts_hash, waiver_id=wv.waiver_id if wv else None, is_recheck=is_recheck,
        correlation_id=cid, provenance="SYNTHETIC", source_ref=f"control:{c.control_key}")
    session.add(ev)
    session.flush()
    _ev(session, tenant_id, actor, "CONTROL_EVALUATED", "evaluation", ev.evaluation_id, 1, cid,
        {"control_key": c.control_key, "rule_version": c.rule_version, "subject_id": ev.subject_id, "status": ev.status,
         "gap": ev.gap, "unit": ev.unit, "waiver_id": ev.waiver_id, "recheck": is_recheck}, effective_at=as_of, inputs=[facts_hash, c.rule_hash])
    emit(session, event_type="ControlEvaluated", tenant_id=tenant_id, correlation_id=cid, aggregate_id=ev.evaluation_id,
         actor_id=actor["id"], actor_type=actor["type"], payload={"control_key": c.control_key, "status": ev.status}, effective_at=as_of)
    return ev, True


def triggered_families(facts: dict, margin_result_id, allocation_id) -> set[str]:
    fam = set()
    if margin_result_id or allocation_id:
        fam.add("MARGIN_COVERAGE")
    if "reconciliation_status" in facts:
        fam.add("RECONCILIATION")
    if "position_lots" in facts or "client_classification" in facts:
        fam.add("POSITION_LIMIT")
    if "source_age_hours" in facts:
        fam.add("FRESHNESS")
    if "terms_status" in facts:
        fam.add("AGREEMENT")
    return fam


def active_controls(session, tenant_id: str, as_of: datetime, only_key: str | None = None):
    q = select(CmpControl).where(CmpControl.tenant_id == tenant_id, CmpControl.active.is_(True), CmpControl.effective_from <= as_of)
    rows = session.execute(q.order_by(CmpControl.control_key, CmpControl.rule_version)).scalars().all()
    out = [c for c in rows if (c.effective_to is None or as_of < c.effective_to) and (only_key is None or c.control_key == only_key)]
    return out


def evaluate(session, tenant_id: str, context: dict, snapshot_id, rule_set_hash, margin_result_id=None, allocation_id=None, *,
             actor_id: str = "system", actor_type: str = "SYSTEM", actor_role: str | None = None, correlation_id: str | None = None,
             facts: dict | None = None) -> dict:
    ctx = normalise_context(context)
    as_of = ts(ctx["as_of"])
    cid = correlation_id or new_id()
    actor = {"id": actor_id, "type": actor_type, "role": actor_role}
    fct, prov, reasons = gather_facts(session, tenant_id, ctx, margin_result_id, allocation_id, facts)
    evals, cases, created_n = [], [], 0
    keys = context.get("control_keys")
    triggered = triggered_families(fct, margin_result_id, allocation_id)
    for c in active_controls(session, tenant_id, as_of):
        if c.support_status != "SUPPORTED":
            continue  # reported as UNSUPPORTED in coverage; never evaluated, never PASS
        if keys is not None:
            if c.control_key not in keys:
                continue
        elif not context.get("full_sweep") and c.family not in triggered:
            continue  # event-triggered: only controls whose inputs arrived are evaluated; the rest stay visibly 'unevaluated'
        ev, created = run_control(session, tenant_id, c, ctx, fct, prov, reasons, as_of, snapshot_id, rule_set_hash,
                                  margin_result_id, allocation_id, cid, actor)
        evals.append(ev)
        if created:
            created_n += 1
            case = open_or_update_case(session, tenant_id, ev, actor, {**ctx, "snapshot_id": snapshot_id, "rule_set_hash": rule_set_hash, "correlation_id": cid})
            if case:
                cases.append(case)
    return {"evaluations": [evaluation_out(e) for e in evals], "cases": [case_out(c) for c in {c.case_id: c for c in cases}.values()],
            "new_evaluations": created_n, "correlation_id": cid}
