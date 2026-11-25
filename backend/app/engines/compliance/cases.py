"""Case lifecycle, remediation, recheck, waivers, ops queue, control coverage.

Lifecycle: OPEN -> ACKNOWLEDGED -> INVESTIGATING -> REMEDIATION_PROPOSED -> REVIEW_REQUIRED -> RESOLVED
           any pre-review state -> DISMISSED (two-person: maker requests, independent checker approves, reason mandatory).
Closure is only by a fresh deterministic re-evaluation (PASS) + independent checker. Waivers/acknowledgements never alter
the raw evaluation status.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from app.auth.security import Principal, require_distinct_checker
from app.core import services
from app.core.canonical import content_hash
from app.core.config import get_settings
from app.core.errors import Blocked, Conflict, Forbidden, NotFound, ValidationFailed
from app.core.events import emit
from app.db.base import lock_for_update, new_id, utcnow
from app.db.models_core import ApprovalRow
from app.engines.compliance import engine as E
from app.engines.compliance.controls import LEGAL_AUTHORITY, WAIVABLE_AUTHORITY
from app.engines.compliance.models import CmpCase, CmpCaseEvent, CmpControl, CmpEvaluation, CmpWaiver

TRANSITIONS = {
    "OPEN": {"ACKNOWLEDGED", "DISMISSED"},
    "ACKNOWLEDGED": {"INVESTIGATING", "DISMISSED"},
    "INVESTIGATING": {"REMEDIATION_PROPOSED", "DISMISSED"},
    "REMEDIATION_PROPOSED": {"REVIEW_REQUIRED", "DISMISSED"},
    "REVIEW_REQUIRED": {"RESOLVED", "INVESTIGATING"},
    "RESOLVED": set(), "DISMISSED": set(),
}
MAKER_ROLES = ("maker", "compliance_officer", "tenant_admin")
MAX_WAIVER_DAYS = 30


def actor_of(p: Principal) -> dict:
    return {"id": p.user_id, "type": "USER", "role": ",".join(p.roles)}


def get_case(session, tenant_id: str, case_id: str, lock: bool = False) -> CmpCase:
    q = select(CmpCase).where(CmpCase.tenant_id == tenant_id, CmpCase.case_id == case_id)
    if lock:
        # Row lock + refresh: the revision check that follows must see the committed row, not a stale read
        # (without this, PostgreSQL let 4 of 5 concurrent updates pass the optimistic check).
        q = lock_for_update(q).execution_options(populate_existing=True)
    c = session.execute(q).scalar_one_or_none()
    if c is None:
        raise NotFound("Case not found")
    return c


def _need(reason: str | None, field="reason", n=5) -> str:
    if not reason or len(reason.strip()) < n:
        raise ValidationFailed(f"{field} is mandatory (min {n} characters)", field_errors={field: "required"})
    return reason.strip()


def _bump(c: CmpCase):
    c.revision += 1
    c.updated_at = utcnow()


def _record(session, p, c, action, payload, *, event_type, frm=None, to=None, comment=None, as_of=None, outbox=None):
    E.add_case_event(session, c.tenant_id, c.case_id, event_type, p.user_id, frm=frm, to=to, comment=comment, data=payload, as_of=as_of)
    corr = (c.eval_context or {}).get("correlation_id") or c.case_id
    parents = [{"entity_type": "control_evaluation", "entity_id": c.last_evaluation_id}] if c.last_evaluation_id else []
    if payload.get("approval_id"):
        parents.append({"entity_type": "approval", "entity_id": payload["approval_id"]})
    E._ev(session, c.tenant_id, actor_of(p), action, "case", c.case_id, c.revision, corr, {**payload, "parents": parents},
          effective_at=as_of, rationale=comment)
    if outbox:
        emit(session, event_type=outbox, tenant_id=c.tenant_id, correlation_id=corr, aggregate_id=c.case_id,
             aggregate_revision=c.revision, actor_id=p.user_id, actor_type="USER", payload={"state": c.state}, effective_at=as_of)


def _actors(session, c: CmpCase, types: tuple[str, ...]) -> set[str]:
    return set(session.execute(select(CmpCaseEvent.actor_id).where(
        CmpCaseEvent.tenant_id == c.tenant_id, CmpCaseEvent.case_id == c.case_id, CmpCaseEvent.event_type.in_(types))).scalars())


def remedy_options(c: CmpCase) -> list[dict]:
    """Options and consequences only. None of these is an executable market action; nothing is transmitted."""
    fam = c.family
    ev_reasons: list[str] = []
    o: list[dict] = []
    if c.kind == "ANOMALY_INVESTIGATION":
        return [
            {"route": "REVIEW_SOURCE_EVENTS", "target_engine": None, "label": "Analyst review of the underlying synthetic events",
             "consequence": "Human review only. The score is a ranking signal, not a finding of wrongdoing."},
            {"route": "CLOSE_NO_ACTION", "target_engine": None, "label": "Record as reviewed, no action",
             "consequence": "Requires rationale and independent approval; does not alter any control outcome."}]
    if fam == "MARGIN_COVERAGE":
        o.append({"route": "ROUTE_COLLATERAL_OPTIMISE", "target_engine": "collateral", "service": "collateral.optimise",
                  "label": "Request a collateral recommendation for the pinned margin result",
                  "inputs": {"margin_result_id": c.margin_result_id},
                  "consequence": "ATLAS proposes a feasible allocation that needs maker-checker approval; nothing is posted or transmitted. "
                                 "A proposal does not count as posted collateral, so this control stays FAIL until simulated settlement is recorded and rechecked."})
        o.append({"route": "ROUTE_RULE_REVIEW", "target_engine": "regulatory", "label": "Confirm the margin rule revision in force",
                  "consequence": "Checks whether the requirement reflects the intended rule version; may change the requirement."})
    elif fam == "POSITION_LIMIT":
        o.append({"route": "ESCALATE_DESK", "target_engine": None, "label": "Escalate to the desk owner for position reduction",
                  "consequence": "AETHER does not block or place trades; reduction happens outside this system. Recheck uses fresh position data."})
        o.append({"route": "ROUTE_RISK_DATA", "target_engine": "risk", "label": "Verify the position data and measure basis",
                  "consequence": "May show the measure basis or classification was wrong."})
    elif fam == "RECONCILIATION":
        o.append({"route": "ROUTE_RISK_RECONCILE", "target_engine": "risk", "service": "risk.reconcile_clearing",
                  "label": "Re-import or re-reconcile the clearing file", "consequence": "VECTOR produces a new reconciliation result; the old one is retained."})
    elif fam == "AGREEMENT":
        o.append({"route": "ROUTE_TERMS_REVIEW", "target_engine": "contracts", "label": "Resolve unreviewed or expired agreement terms",
                  "consequence": "COVENANT review and freeze; margin results depending on these terms stay blocked meanwhile."})
    elif fam == "FRESHNESS":
        o.append({"route": "ROUTE_RULE_REFRESH", "target_engine": "regulatory", "label": "Refresh the policy source", "consequence": "New source revision requires review and approval."})
    last = c.last_status
    if last == "UNKNOWN":
        o.append({"route": "ROUTE_NEW_SNAPSHOT", "target_engine": "risk", "label": "Provide a fresh pinned snapshot / missing input",
                  "consequence": "A new run is created; the prior evaluation remains in history."})
    o.append({"route": "ROUTE_EVIDENCE_GAP", "target_engine": "evidence", "label": "Report missing evidence", "consequence": "Opens an evidence-integrity review; no financial effect."})
    return o


# --------------------------------------------------------------------------- transitions
def transition(session, p: Principal, case_id: str, to_state: str, reason: str | None, expected_revision: int, as_of: datetime | None = None) -> CmpCase:
    c = get_case(session, p.tenant_id, case_id, lock=True)
    if c.revision != expected_revision:
        raise Conflict(f"case changed (revision {c.revision}, expected {expected_revision}); reload", code="REVISION_CONFLICT")
    if to_state not in TRANSITIONS.get(c.state, set()) and not (to_state == "DISMISSED" and c.state in TRANSITIONS):
        raise Conflict(f"illegal transition {c.state} -> {to_state}", code="ILLEGAL_TRANSITION")
    frm = c.state
    if to_state == "REMEDIATION_PROPOSED":
        raise Conflict("use the remediate action (it records proposed/selected/rejected remedies)", code="USE_REMEDIATE")
    if to_state == "DISMISSED":
        if frm in ("RESOLVED", "DISMISSED", "REVIEW_REQUIRED"):
            raise Conflict(f"cannot dismiss from {frm}", code="ILLEGAL_TRANSITION")
        return _dismiss(session, p, c, _need(reason), as_of)
    if to_state == "RESOLVED":
        return _resolve(session, p, c, _need(reason), as_of)
    if not p.has(*MAKER_ROLES):
        raise Forbidden("Requires maker, compliance_officer or tenant_admin role")
    if to_state == "INVESTIGATING" and frm == "REVIEW_REQUIRED":
        _need(reason, "reason")
    c.state = to_state
    if to_state == "ACKNOWLEDGED":
        c.owner_id = c.owner_id or p.user_id
    if to_state == "REVIEW_REQUIRED":
        if not c.remediations:
            raise Blocked("no remediation has been proposed")
        c.review_entered_at = utcnow()
        c.last_recheck = None  # a recheck must happen AFTER submission for review
    _bump(c)
    _record(session, p, c, "CASE_TRANSITIONED", {"from": frm, "to": to_state},
            event_type="SUBMITTED_FOR_REVIEW" if to_state == "REVIEW_REQUIRED" else "STATE_CHANGE", frm=frm, to=to_state, comment=reason, as_of=as_of, outbox="CaseUpdated")
    return c


def _dismiss(session, p, c, reason, as_of):
    pend = c.pending_dismissal
    settings = get_settings()
    snap = content_hash({"case": c.case_id, "rev": c.revision, "state": c.state, "last_eval": c.last_evaluation_id})
    stale = bool(pend) and (pend["hash"] != snap or pend["revision"] != c.revision or
                            (utcnow() - datetime.fromisoformat(pend["requested_at"])).total_seconds() > settings.approval_ttl_seconds)
    if pend is None or stale:
        # (Re-)request: a stale/expired request can never be approved; a maker must request again against the current state.
        if not p.has(*MAKER_ROLES):
            if stale:
                raise Conflict("dismissal request is stale or expired (case changed since it was requested); a maker must request again", code="STALE_APPROVAL")
            raise Blocked("no dismissal has been requested; a maker must request it first", code="NO_DISMISSAL_REQUEST")
        c.pending_dismissal = {"requested_by": p.user_id, "reason": reason, "revision": c.revision, "hash": snap,
                               "requested_at": utcnow().isoformat()}
        c.maker_id = p.user_id
        _bump(c)
        c.pending_dismissal = {**c.pending_dismissal, "revision": c.revision,
                               "hash": content_hash({"case": c.case_id, "rev": c.revision, "state": c.state, "last_eval": c.last_evaluation_id})}
        _record(session, p, c, "CASE_DISMISSAL_REQUESTED", {"reason": reason, "replaced_stale": stale}, event_type="DISMISSAL_REQUESTED", comment=reason, as_of=as_of)
        return c
    if not p.has("checker", "compliance_officer"):
        raise Conflict("dismissal is awaiting independent checker approval", code="AWAITING_CHECKER")
    require_distinct_checker(pend["requested_by"], p)
    appr = ApprovalRow(approval_id=new_id(), tenant_id=p.tenant_id, action_type="DISMISS_CASE", target_type="case", target_id=c.case_id,
                       target_revision=c.revision, target_hash=snap, maker_id=pend["requested_by"], checker_id=p.user_id,
                       decision="APPROVED", reason=reason, decided_at=utcnow(), expires_at=utcnow() + timedelta(seconds=settings.approval_ttl_seconds))
    session.add(appr)
    frm = c.state
    c.state, c.pending_dismissal = "DISMISSED", None
    c.resolution = {"kind": "DISMISSED", "reason": reason, "requested_by": pend["requested_by"], "approved_by": p.user_id, "approval_id": appr.approval_id,
                    "bound_hash": snap, "note": "Dismissal does not change any raw control evaluation status"}
    _bump(c)
    _record(session, p, c, "CASE_DISMISSED", {"approval_id": appr.approval_id, "requested_by": pend["requested_by"]},
            event_type="STATE_CHANGE", frm=frm, to="DISMISSED", comment=reason, as_of=as_of, outbox="CaseResolved")
    return c


def _resolve(session, p, c, reason, as_of):
    if c.state != "REVIEW_REQUIRED":
        raise Conflict(f"cannot resolve from {c.state}", code="ILLEGAL_TRANSITION")
    makers = _actors(session, c, ("REMEDIATION_PROPOSED", "SUBMITTED_FOR_REVIEW"))
    if p.user_id in makers:
        raise Forbidden("The remediation author/submitter cannot resolve their own case", code="SELF_APPROVAL")
    if not p.has("checker", "compliance_officer"):
        raise Forbidden("Checker role required", code="CHECKER_ROLE_REQUIRED")
    lr = c.last_recheck
    if not lr:
        raise Blocked("resolution requires a fresh deterministic recheck after submission for review", code="RECHECK_REQUIRED")
    ev = session.execute(select(CmpEvaluation).where(CmpEvaluation.tenant_id == p.tenant_id, CmpEvaluation.evaluation_id == lr["evaluation_id"])).scalar_one_or_none()
    if ev is None or not ev.is_recheck or ev.case_id != c.case_id:
        raise Blocked("recheck evidence not found", code="RECHECK_REQUIRED")
    if ev.status != "PASS":
        raise Blocked(f"latest recheck is {ev.status}, not PASS: case cannot be closed", code="RECHECK_NOT_PASS")
    if c.review_entered_at and ev.created_at < c.review_entered_at:
        raise Blocked("recheck predates submission for review", code="RECHECK_STALE")
    frm = c.state
    c.state = "RESOLVED"
    c.resolution = {"kind": "RESOLVED", "reason": reason, "recheck_evaluation_id": ev.evaluation_id, "approved_by": p.user_id,
                    "verified_by": "fresh deterministic re-evaluation", "recheck_as_of": ev.as_of.isoformat()}
    _bump(c)
    _record(session, p, c, "CASE_RESOLVED", {"recheck_evaluation_id": ev.evaluation_id}, event_type="STATE_CHANGE", frm=frm, to="RESOLVED",
            comment=reason, as_of=as_of, outbox="CaseResolved")
    return c


def remediate(session, p: Principal, case_id: str, body: dict) -> CmpCase:
    if not p.has(*MAKER_ROLES):
        raise Forbidden("Requires maker, compliance_officer or tenant_admin role")
    c = get_case(session, p.tenant_id, case_id, lock=True)
    if c.revision != body.get("expected_revision"):
        raise Conflict("case changed; reload", code="REVISION_CONFLICT")
    if c.state != "INVESTIGATING":
        raise Conflict("remediation can only be proposed while INVESTIGATING", code="ILLEGAL_TRANSITION")
    opts = {o["route"]: o for o in remedy_options(c)}
    sel = body.get("selected")
    if sel not in opts:
        raise ValidationFailed("selected remedy is not an offered option", field_errors={"selected": "unknown route"})
    rationale = _need(body.get("rationale"), "rationale")
    rejected = []
    for r in body.get("rejected", []):
        if r.get("route") not in opts or r["route"] == sel:
            raise ValidationFailed("rejected remedies must be other offered routes", field_errors={"rejected": "invalid"})
        rejected.append({"route": r["route"], "reason": _need(r.get("reason"), "rejected.reason", 3)})
    deadline = E.ts(body["deadline"], "deadline") if body.get("deadline") else c.due_at
    rec = {"id": new_id(), "proposed": list(opts.values()), "selected": opts[sel], "rejected": rejected, "rationale": rationale,
           "deadline": deadline.isoformat(), "owner_id": body.get("owner_id") or p.user_id, "proposed_by": p.user_id,
           "recorded_at": utcnow().isoformat(), "follow_up_outcome": None,
           "note": "Option and consequence record only; no market action is executed or transmitted by AETHER"}
    c.remediations = [*c.remediations, rec]
    c.state, c.maker_id, c.owner_id = "REMEDIATION_PROPOSED", p.user_id, rec["owner_id"]
    _bump(c)
    _record(session, p, c, "CASE_REMEDIATION_PROPOSED", {"selected": sel, "rejected": [r["route"] for r in rejected], "deadline": rec["deadline"]},
            event_type="REMEDIATION_PROPOSED", frm="INVESTIGATING", to="REMEDIATION_PROPOSED", comment=rationale, outbox="CaseUpdated")
    return c


def add_comment(session, p: Principal, case_id: str, text: str) -> CmpCase:
    c = get_case(session, p.tenant_id, case_id)
    t = _need(text, "comment", 2)
    E.add_case_event(session, p.tenant_id, c.case_id, "COMMENT", p.user_id, comment=t)
    return c


def recheck(session, tenant_id: str, case_id: str, snapshot_id, *, as_of=None, facts=None, actor_id="system", actor_type="SYSTEM",
            actor_role=None, correlation_id=None) -> dict:
    """Fresh deterministic re-evaluation of the case's control for its subject. Never edits history or rules."""
    c = get_case(session, tenant_id, case_id, lock=True)
    if c.kind == "ANOMALY_INVESTIGATION":
        raise Blocked("anomaly cases have no deterministic control to recheck; close with review and independent approval", code="NO_RECHECK")
    ctx = dict(c.eval_context or {})
    if not as_of:
        raise ValidationFailed("as_of is required for recheck (no implicit clock)", field_errors={"as_of": "required"})
    rsh = ctx.get("rule_set_hash")
    ctx["as_of"] = E.ts(as_of).isoformat()
    ctx = E.normalise_context(ctx)
    ctx["rule_set_hash"] = rsh
    asof = E.ts(ctx["as_of"])
    cid = correlation_id or new_id()
    actor = {"id": actor_id, "type": actor_type, "role": actor_role}
    ctrls = E.active_controls(session, tenant_id, asof, only_key=c.control_key)
    if not ctrls:
        raise Blocked("no active revision of this control at the given as_of", code="CONTROL_NOT_ACTIVE")
    fct, prov, reasons = E.gather_facts(session, tenant_id, ctx, c.margin_result_id, c.allocation_id, facts, {c.family})
    ev, _ = E.run_control(session, tenant_id, ctrls[-1], ctx, fct, prov, reasons, asof, snapshot_id, ctx.get("rule_set_hash"),
                          c.margin_result_id, c.allocation_id, cid, actor, is_recheck=True)
    ev.case_id = c.case_id
    c.last_recheck = {"evaluation_id": ev.evaluation_id, "status": ev.status, "as_of": ev.as_of.isoformat(), "snapshot_id": snapshot_id}
    E.add_case_event(session, tenant_id, c.case_id, "RECHECK", actor_id, as_of=asof, data=c.last_recheck,
                     comment=f"Deterministic recheck: {ev.status}")
    _bump(c)
    session.flush()
    return E.evaluation_out(ev)


# --------------------------------------------------------------------------- waivers
def create_waiver(session, p: Principal, body: dict) -> CmpWaiver:
    if not p.has(*MAKER_ROLES):
        raise Forbidden("Requires maker, compliance_officer or tenant_admin role")
    ctrl = session.execute(select(CmpControl).where(CmpControl.tenant_id == p.tenant_id, CmpControl.control_key == body.get("control_key"))
                           .order_by(CmpControl.rule_version.desc())).scalars().first()
    if ctrl is None:
        raise ValidationFailed("unknown control", field_errors={"control_key": "not found"})
    if ctrl.authority_type in LEGAL_AUTHORITY or ctrl.authority_type not in WAIVABLE_AUTHORITY:
        raise Forbidden("A waiver cannot override a legal obligation; only internal/synthetic policy controls are waivable", code="LEGAL_OBLIGATION_NOT_WAIVABLE")
    subject = body.get("subject_id")
    if not subject or subject in ("*", "ALL"):
        raise ValidationFailed("a waiver must name one exact subject (no wildcards)", field_errors={"subject_id": "required, exact"})
    vf, vt = E.ts(body.get("valid_from"), "valid_from"), E.ts(body.get("valid_to"), "valid_to")
    if vt <= vf or vt - vf > timedelta(days=MAX_WAIVER_DAYS):
        raise ValidationFailed(f"waiver must be time-limited (0 < duration <= {MAX_WAIVER_DAYS} days)", field_errors={"valid_to": "invalid duration"})
    w = CmpWaiver(tenant_id=p.tenant_id, waiver_id=new_id(), control_key=ctrl.control_key, subject_id=subject, reason=_need(body.get("reason")),
                  valid_from=vf, valid_to=vt, requested_by=p.user_id, provenance="SYNTHETIC")
    session.add(w)
    session.flush()
    E._ev(session, p.tenant_id, actor_of(p), "WAIVER_REQUESTED", "waiver", w.waiver_id, 1, w.waiver_id,
          {"control_key": w.control_key, "subject_id": subject, "valid_from": vf.isoformat(), "valid_to": vt.isoformat()}, effective_at=vf, rationale=w.reason)
    return w


def approve_waiver(session, p: Principal, waiver_id: str, decision: str, reason: str) -> CmpWaiver:
    wq = select(CmpWaiver).where(CmpWaiver.tenant_id == p.tenant_id, CmpWaiver.waiver_id == waiver_id)
    w = session.execute(lock_for_update(wq).execution_options(populate_existing=True)).scalar_one_or_none()
    if w is None:
        raise NotFound("Waiver not found")
    if w.status != "PENDING":
        raise Conflict(f"waiver already {w.status}")
    require_distinct_checker(w.requested_by, p)
    _need(reason)
    snap = content_hash({"w": w.waiver_id, "c": w.control_key, "s": w.subject_id, "f": w.valid_from, "t": w.valid_to})
    appr = ApprovalRow(approval_id=new_id(), tenant_id=p.tenant_id, action_type="APPROVE_WAIVER", target_type="waiver", target_id=w.waiver_id,
                       target_revision=w.revision, target_hash=snap, maker_id=w.requested_by, checker_id=p.user_id,
                       decision="APPROVED" if decision == "APPROVE" else "REJECTED", reason=reason, decided_at=utcnow(),
                       expires_at=utcnow() + timedelta(seconds=get_settings().approval_ttl_seconds))
    session.add(appr)
    w.status, w.approved_by, w.approval_id = ("ACTIVE" if decision == "APPROVE" else "REJECTED"), p.user_id, appr.approval_id
    w.revision += 1
    E._ev(session, p.tenant_id, actor_of(p), "WAIVER_" + ("APPROVED" if decision == "APPROVE" else "REJECTED"), "waiver", w.waiver_id, w.revision,
          w.waiver_id, {"approval_id": appr.approval_id, "hash": snap}, rationale=reason)
    return w


def waiver_out(w: CmpWaiver) -> dict:
    return {"waiver_id": w.waiver_id, "control_key": w.control_key, "subject_id": w.subject_id, "reason": w.reason, "status": w.status,
            "valid_from": w.valid_from.isoformat(), "valid_to": w.valid_to.isoformat(), "requested_by": w.requested_by, "approved_by": w.approved_by,
            "scope_note": "Applies only to this control and subject within the interval; internal policy only; raw outcome unchanged"}


# --------------------------------------------------------------------------- reads
def coverage(session, tenant_id: str, as_of: datetime | None) -> dict:
    ctrls = session.execute(select(CmpControl).where(CmpControl.tenant_id == tenant_id, CmpControl.active.is_(True))).scalars().all()
    latest: dict[str, CmpControl] = {}
    for c in ctrls:
        if as_of and not (c.effective_from <= as_of and (c.effective_to is None or as_of < c.effective_to)):
            continue
        if c.control_key not in latest or c.rule_version > latest[c.control_key].rule_version:
            latest[c.control_key] = c
    evals = session.execute(select(CmpEvaluation).where(CmpEvaluation.tenant_id == tenant_id, CmpEvaluation.is_recheck.is_(False))
                            .order_by(CmpEvaluation.as_of, CmpEvaluation.created_at)).scalars().all()
    cur: dict[tuple, CmpEvaluation] = {}
    hyp: dict[tuple, CmpEvaluation] = {}
    for e in evals:
        # hypothetical (what-if / draft-rule) outcomes are NEVER official compliance status: counted separately (spec R-A09)
        (hyp if (e.trace or {}).get("hypothetical") else cur)[(e.control_key, e.subject_id)] = e
    counts = {k: 0 for k in ("PASS", "FAIL", "UNKNOWN", "NOT_APPLICABLE", "CONFLICT", "ERROR")}
    for e in cur.values():
        counts[e.status] += 1
    evaluated_keys = {k for k, _ in cur}
    supported = [c for c in latest.values() if c.support_status == "SUPPORTED"]
    unsupported = [c for c in latest.values() if c.support_status != "SUPPORTED"]
    items = []
    for c in sorted(latest.values(), key=lambda x: x.control_key):
        ev_c = [e for (k, _), e in cur.items() if k == c.control_key]
        items.append({"control_key": c.control_key, "title": c.title, "family": c.family, "rule_key": c.rule_key, "rule_version": c.rule_version,
                      "rule_hash": c.rule_hash, "authority_type": c.authority_type, "severity": c.severity, "frequency": c.frequency,
                      "max_staleness_hours": c.max_staleness_hours, "support_status": c.support_status, "unsupported_reason": c.unsupported_reason,
                      "required_inputs": c.required_inputs, "effective_from": c.effective_from.isoformat(),
                      "evaluated_subjects": len(ev_c), "latest_status_counts": {s: sum(1 for e in ev_c if e.status == s) for s in counts if any(e.status == s for e in ev_c)},
                      "last_evaluated_as_of": max((e.as_of for e in ev_c), default=None) and max(e.as_of for e in ev_c).isoformat()})
    last = max((e.as_of for e in cur.values()), default=None)
    return {"controls": items,
            "coverage": {"registered": len(latest), "supported": len(supported), "unsupported": len(unsupported),
                         "supported_and_evaluated": sum(1 for c in supported if c.control_key in evaluated_keys),
                         "supported_not_evaluated": sum(1 for c in supported if c.control_key not in evaluated_keys),
                         "subject_evaluations": len(cur), "latest_status_counts": counts, "unknown_count": counts["UNKNOWN"],
                         "hypothetical_evaluations": len(hyp), "hypothetical_status_counts": {k: sum(1 for e in hyp.values() if e.status == k) for k in counts if any(e.status == k for e in hyp.values())},
                         "note": "No generic compliance percentage is shown: the absence of alerts is not compliance. UNKNOWN is never PASS."},
            "freshness": {"latest_evaluation_as_of": last.isoformat() if last else None}}


def queue(session, tenant_id: str, as_of: datetime | None, user_id: str | None = None) -> dict:
    q = select(CmpCase).where(CmpCase.tenant_id == tenant_id, CmpCase.state.notin_(["RESOLVED", "DISMISSED"]))
    if user_id:
        q = q.where(CmpCase.owner_id == user_id)
    rows = session.execute(q.order_by(CmpCase.due_at, CmpCase.case_id)).scalars().all()
    out = []
    for c in rows:
        d = E.case_out(c)
        overdue = bool(as_of and c.due_at < as_of)
        d["sla"] = {"due_at": d["due_at"], "overdue": overdue, "as_of": as_of.isoformat() if as_of else None,
                    "hours_overdue": round((as_of - c.due_at).total_seconds() / 3600, 2) if overdue else 0,
                    "escalation": "ESCALATED" if overdue else "NONE"}
        out.append(d)
    return {"items": out, "as_of": as_of.isoformat() if as_of else None,
            "clock_note": "SLA is measured against the supplied business as_of, never the server clock"}


def latest_as_of(session, tenant_id: str) -> datetime | None:
    return session.execute(select(func.max(CmpEvaluation.as_of)).where(CmpEvaluation.tenant_id == tenant_id)).scalar_one()
