"""/api/v1/compliance/* routes. Tenant comes from the session only; foreign ids return 404."""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.security import Principal, current_principal, get_db, require_roles
from app.core.errors import NotFound, ValidationFailed
from app.core.idempotency import run_idempotent
from app.engines.compliance import cases as C
from app.engines.compliance import controls as K
from app.engines.compliance import engine as E
from app.engines.compliance import schedule as SCH
from app.engines.compliance import surv_service as SV
from app.engines.compliance.models import CmpCase, CmpCaseEvent, CmpControl, CmpEvaluation, CmpJob, CmpWaiver

router = APIRouter()
WRITE = ("maker", "compliance_officer", "tenant_admin")
IDEM = Header(default=None, alias="Idempotency-Key")


class EvaluateBody(BaseModel):
    context: dict
    snapshot_id: str | None = None
    rule_set_hash: str | None = None
    margin_result_id: str | None = None
    allocation_id: str | None = None
    facts: dict | None = None


class TransitionBody(BaseModel):
    to_state: str
    reason: str | None = None
    expected_revision: int
    as_of: str | None = None


class RemediateBody(BaseModel):
    expected_revision: int
    selected: str
    rationale: str
    rejected: list[dict] = Field(default_factory=list)
    deadline: str | None = None
    owner_id: str | None = None


class RecheckBody(BaseModel):
    snapshot_id: str
    as_of: str
    facts: dict | None = None


class CommentBody(BaseModel):
    text: str


class WaiverBody(BaseModel):
    control_key: str
    subject_id: str
    reason: str
    valid_from: str
    valid_to: str


class WaiverDecision(BaseModel):
    decision: str = Field(pattern="^(APPROVE|REJECT)$")
    reason: str


class ControlBody(BaseModel):
    control_key: str
    family: str
    title: str
    rule_key: str
    rule_version: int = 1
    authority_type: str = "SYNTHETIC_POLICY"
    scope: dict
    condition: dict
    severity: str = Field(default="HIGH", pattern="^(LOW|MEDIUM|HIGH|CRITICAL)$")
    max_staleness_hours: int | None = None
    params: dict = Field(default_factory=dict)
    required_inputs: list[str] = Field(default_factory=list)


class SurvBody(BaseModel):
    seed: int = 42


class SweepBody(BaseModel):
    as_of: str
    population: list[dict] | None = None


class SupersedeBody(BaseModel):
    effective_from: str
    reason: str
    condition: dict | None = None
    scope: dict | None = None
    params: dict | None = None
    severity: str | None = Field(default=None, pattern="^(LOW|MEDIUM|HIGH|CRITICAL)$")
    rule_key: str | None = None


class ModelStatusBody(BaseModel):
    status: str = Field(pattern="^(ACTIVE|DISABLED)$")


def _asof(v: str | None) -> datetime | None:
    return E.ts(v) if v else None


# ------------------------------------------------------------------ controls
@router.get("/controls")
def list_controls(as_of: str | None = None, db: Session = Depends(get_db), p: Principal = Depends(current_principal)):
    return C.coverage(db, p.tenant_id, _asof(as_of))


@router.post("/controls", status_code=201)
def register_control(body: ControlBody, db: Session = Depends(get_db), p: Principal = Depends(require_roles(*WRITE)),
                     idempotency_key: str | None = IDEM):
    def make():
        c = K.register_control(db, p.tenant_id, body.model_dump())
        E._ev(db, p.tenant_id, C.actor_of(p), "CONTROL_REGISTERED", "control", c.control_id, c.rule_version, c.control_id,
              {"control_key": c.control_key, "rule_hash": c.rule_hash, "authority_type": c.authority_type}, effective_at=c.effective_from)
        return {"control_id": c.control_id, "control_key": c.control_key, "rule_version": c.rule_version, "rule_hash": c.rule_hash,
                "support_status": c.support_status}
    return run_idempotent(db, p.tenant_id, idempotency_key, "compliance.control", body.model_dump(), make)[0]


@router.post("/controls/seed-library", status_code=201)
def seed_library(db: Session = Depends(get_db), p: Principal = Depends(require_roles("tenant_admin", "compliance_officer"))):
    from app.engines.compliance.fixtures import seed_fixtures
    return seed_fixtures(db, p.tenant_id)


# ------------------------------------------------------------------ evaluations
@router.post("/evaluate")
def evaluate(body: EvaluateBody, db: Session = Depends(get_db), p: Principal = Depends(require_roles(*WRITE)),
             idempotency_key: str | None = IDEM):
    def make():
        return E.evaluate(db, p.tenant_id, body.context, body.snapshot_id, body.rule_set_hash, body.margin_result_id, body.allocation_id,
                          actor_id=p.user_id, actor_type="USER", actor_role=",".join(p.roles), facts=body.facts)
    return run_idempotent(db, p.tenant_id, idempotency_key, "compliance.evaluate", body.model_dump(), make)[0]


@router.get("/evaluations/{evaluation_id}")
def get_evaluation(evaluation_id: str, db: Session = Depends(get_db), p: Principal = Depends(current_principal)):
    e = db.execute(select(CmpEvaluation).where(CmpEvaluation.tenant_id == p.tenant_id, CmpEvaluation.evaluation_id == evaluation_id)).scalar_one_or_none()
    if e is None:
        raise NotFound("Evaluation not found")
    return E.evaluation_out(e)


# ------------------------------------------------------------------ cases
@router.get("/cases")
def list_cases(state: str | None = None, severity: str | None = None, market: str | None = None, kind: str | None = None,
               owner_id: str | None = None, limit: int = 50, cursor: str | None = None,
               db: Session = Depends(get_db), p: Principal = Depends(current_principal)):
    limit = max(1, min(limit, 200))
    q = select(CmpCase).where(CmpCase.tenant_id == p.tenant_id)
    for col, v in ((CmpCase.state, state), (CmpCase.severity, severity), (CmpCase.market_type, market), (CmpCase.kind, kind), (CmpCase.owner_id, owner_id)):
        if v:
            q = q.where(col == v)
    if cursor:
        q = q.where(CmpCase.created_at < datetime.fromisoformat(cursor))
    rows = db.execute(q.order_by(CmpCase.created_at.desc(), CmpCase.case_id).limit(limit + 1)).scalars().all()
    return {"items": [E.case_out(c) for c in rows[:limit]], "next_cursor": rows[limit - 1].created_at.isoformat() if len(rows) > limit else None}


@router.get("/cases/{case_id}")
def get_case(case_id: str, db: Session = Depends(get_db), p: Principal = Depends(current_principal)):
    c = C.get_case(db, p.tenant_id, case_id)
    evs = db.execute(select(CmpCaseEvent).where(CmpCaseEvent.tenant_id == p.tenant_id, CmpCaseEvent.case_id == case_id).order_by(CmpCaseEvent.id)).scalars().all()
    evals = db.execute(select(CmpEvaluation).where(CmpEvaluation.tenant_id == p.tenant_id, CmpEvaluation.case_id == case_id)
                       .order_by(CmpEvaluation.as_of.desc(), CmpEvaluation.created_at.desc()).limit(50)).scalars().all()
    ctrl = db.execute(select(CmpControl).where(CmpControl.tenant_id == p.tenant_id, CmpControl.control_key == c.control_key,
                                               CmpControl.rule_version == c.rule_version)).scalar_one_or_none()
    return {**E.case_out(c),
            "events": [{"id": e.id, "event_type": e.event_type, "actor_id": e.actor_id, "from_state": e.from_state, "to_state": e.to_state,
                        "comment": e.comment, "data": e.data, "as_of": e.as_of.isoformat() if e.as_of else None, "recorded_at": e.recorded_at.isoformat()} for e in evs],
            "evaluations": [E.evaluation_out(e) for e in evals],
            "control": {"control_key": ctrl.control_key, "title": ctrl.title, "rule_key": ctrl.rule_key, "rule_version": ctrl.rule_version,
                        "rule_hash": ctrl.rule_hash, "authority_type": ctrl.authority_type, "scope": ctrl.scope, "condition": ctrl.condition} if ctrl else None,
            "remedy_options": C.remedy_options(c),
            "anomaly_disclaimer": SV.DISCLAIMER if c.kind == "ANOMALY_INVESTIGATION" else None,
            "separation_note": ("Statistical anomaly: a ranking signal for human review. Deterministic control outcomes are unaffected."
                                if c.kind == "ANOMALY_INVESTIGATION" else "Deterministic control outcome (rule-based).")}


@router.post("/cases/{case_id}/transition")
def case_transition(case_id: str, body: TransitionBody, db: Session = Depends(get_db), p: Principal = Depends(current_principal),
                    idempotency_key: str | None = IDEM):
    def make():
        return E.case_out(C.transition(db, p, case_id, body.to_state, body.reason, body.expected_revision, _asof(body.as_of)))
    return run_idempotent(db, p.tenant_id, idempotency_key, f"compliance.transition.{case_id}", body.model_dump(), make)[0]


@router.post("/cases/{case_id}/remediate")
def case_remediate(case_id: str, body: RemediateBody, db: Session = Depends(get_db), p: Principal = Depends(require_roles(*WRITE)),
                   idempotency_key: str | None = IDEM):
    def make():
        return E.case_out(C.remediate(db, p, case_id, body.model_dump()))
    return run_idempotent(db, p.tenant_id, idempotency_key, f"compliance.remediate.{case_id}", body.model_dump(), make)[0]


@router.post("/cases/{case_id}/recheck")
def case_recheck(case_id: str, body: RecheckBody, db: Session = Depends(get_db), p: Principal = Depends(require_roles(*WRITE, "checker")),
                 idempotency_key: str | None = IDEM):
    def make():
        return C.recheck(db, p.tenant_id, case_id, body.snapshot_id, as_of=body.as_of, facts=body.facts, actor_id=p.user_id,
                         actor_type="USER", actor_role=",".join(p.roles))
    return run_idempotent(db, p.tenant_id, idempotency_key, f"compliance.recheck.{case_id}", body.model_dump(), make)[0]


@router.post("/cases/{case_id}/comments", status_code=201)
def case_comment(case_id: str, body: CommentBody, db: Session = Depends(get_db), p: Principal = Depends(require_roles(*WRITE, "checker", "auditor"))):
    C.add_comment(db, p, case_id, body.text)
    return {"ok": True}


# ------------------------------------------------------------------ waivers
@router.get("/waivers")
def list_waivers(db: Session = Depends(get_db), p: Principal = Depends(current_principal)):
    rows = db.execute(select(CmpWaiver).where(CmpWaiver.tenant_id == p.tenant_id).order_by(CmpWaiver.created_at.desc())).scalars().all()
    return {"items": [C.waiver_out(w) for w in rows]}


@router.post("/waivers", status_code=201)
def create_waiver(body: WaiverBody, db: Session = Depends(get_db), p: Principal = Depends(require_roles(*WRITE)), idempotency_key: str | None = IDEM):
    return run_idempotent(db, p.tenant_id, idempotency_key, "compliance.waiver", body.model_dump(),
                          lambda: C.waiver_out(C.create_waiver(db, p, body.model_dump())))[0]


@router.post("/waivers/{waiver_id}/decision")
def waiver_decision(waiver_id: str, body: WaiverDecision, db: Session = Depends(get_db),
                    p: Principal = Depends(require_roles("checker", "compliance_officer")), idempotency_key: str | None = IDEM):
    return run_idempotent(db, p.tenant_id, idempotency_key, f"compliance.waiver.{waiver_id}", body.model_dump(),
                          lambda: C.waiver_out(C.approve_waiver(db, p, waiver_id, body.decision, body.reason)))[0]


# ------------------------------------------------------------------ queue & surveillance
@router.get("/queue")
def ops_queue(as_of: str | None = None, mine: bool = False, db: Session = Depends(get_db), p: Principal = Depends(current_principal)):
    a = _asof(as_of) or C.latest_as_of(db, p.tenant_id)
    return C.queue(db, p.tenant_id, a, p.user_id if mine else None)


@router.get("/surveillance")
def surveillance(limit: int = 50, db: Session = Depends(get_db), p: Principal = Depends(current_principal)):
    return SV.surveillance_view(db, p.tenant_id, limit)


@router.post("/surveillance/run", status_code=201)
def surveillance_run(body: SurvBody, db: Session = Depends(get_db), p: Principal = Depends(require_roles("compliance_officer", "tenant_admin")),
                     idempotency_key: str | None = IDEM):
    return run_idempotent(db, p.tenant_id, idempotency_key, "compliance.surveillance", body.model_dump(),
                          lambda: SV.train_and_persist(db, p.tenant_id, C.actor_of(p), body.seed))[0]


@router.get("/models/{model_id}")
def get_model(model_id: str, db: Session = Depends(get_db), p: Principal = Depends(current_principal)):
    return SV.model_out(SV.get_model(db, p.tenant_id, model_id))


@router.post("/models/{model_id}/status")
def model_status(model_id: str, body: ModelStatusBody, db: Session = Depends(get_db), p: Principal = Depends(require_roles("compliance_officer", "tenant_admin"))):
    return SV.model_out(SV.set_model_status(db, p.tenant_id, model_id, body.status, C.actor_of(p)))


# ------------------------------------------------------------------ control supersession (G23) and scheduled evaluation (S01)
@router.post("/controls/{control_key}/supersede", status_code=201)
def supersede_control(control_key: str, body: SupersedeBody, db: Session = Depends(get_db),
                      p: Principal = Depends(require_roles("compliance_officer", "tenant_admin")), idempotency_key: str | None = IDEM):
    """New immutable control revision effective from `effective_from`; only subjects previously evaluated by THIS control are
    re-evaluated (others untouched); history preserved."""
    def make():
        eff = E.ts(body.effective_from, "effective_from")
        new_def = {k: v for k, v in body.model_dump().items() if k in ("condition", "scope", "params", "severity", "rule_key") and v is not None}
        if "condition" in new_def or "scope" in new_def:
            probe = {"control_key": control_key, "family": "X", "title": "x", "rule_key": "x", "authority_type": "SYNTHETIC_POLICY",
                     "scope": new_def.get("scope", {"eq": ["market_type", "OTC"]}), "condition": new_def.get("condition", {"eq": ["market_type", "OTC"]})}
            K.validate_definition(probe)
        new, old = K.supersede_control(db, p.tenant_id, control_key, new_def, eff)
        actor = C.actor_of(p)
        E._ev(db, p.tenant_id, actor, "CONTROL_SUPERSEDED", "control", new.control_id, new.rule_version, new.control_id,
              {"control_key": control_key, "old_version": old.rule_version, "new_version": new.rule_version, "new_rule_hash": new.rule_hash,
               "old_rule_hash": old.rule_hash, "effective_from": eff.isoformat()}, effective_at=eff, rationale=body.reason)
        re = K.reevaluate_affected(db, p.tenant_id, control_key, eff, actor)
        return {"control_key": control_key, "old_version": old.rule_version, "new_version": new.rule_version, "rule_hash": new.rule_hash,
                "effective_from": eff.isoformat(), "re_evaluation": re}
    return run_idempotent(db, p.tenant_id, idempotency_key, f"compliance.supersede.{control_key}", body.model_dump(), make)[0]


@router.post("/schedule/run", status_code=201)
def schedule_run(body: SweepBody, db: Session = Depends(get_db), p: Principal = Depends(require_roles(*WRITE)), idempotency_key: str | None = IDEM):
    return run_idempotent(db, p.tenant_id, idempotency_key, "compliance.sweep", body.model_dump(),
                          lambda: SCH.run_sweep(db, p.tenant_id, E.ts(body.as_of), body.population, C.actor_of(p)))[0]


@router.get("/schedule")
def schedule_get(as_of: str, db: Session = Depends(get_db), p: Principal = Depends(current_principal)):
    """Read-only: watermark + missed slots against the supplied business as_of."""
    st = SCH.schedule_status(db, p.tenant_id, E.ts(as_of))
    jobs = db.execute(select(CmpJob).where(CmpJob.tenant_id == p.tenant_id, CmpJob.kind.in_([SCH.SWEEP, SCH.MISSED]))
                      .order_by(CmpJob.started_at.desc()).limit(30)).scalars().all()
    return {**st, "jobs": [{"job_id": j.job_id, "kind": j.kind, "status": j.status, "slot": j.detail.get("slot"),
                            "watermark_as_of": j.watermark_as_of.isoformat() if j.watermark_as_of else None,
                            "subjects": j.detail.get("subjects"), "evaluations": j.detail.get("evaluations"),
                            "status_counts": j.detail.get("status_counts"), "error": j.detail.get("error")} for j in jobs]}


@router.post("/schedule/check", status_code=201)
def schedule_check(body: SweepBody, db: Session = Depends(get_db), p: Principal = Depends(require_roles(*WRITE)), idempotency_key: str | None = IDEM):
    """Detect missed slots and record each newly missed one as evidence (once)."""
    return run_idempotent(db, p.tenant_id, idempotency_key, "compliance.sched-check", body.model_dump(),
                          lambda: SCH.schedule_status(db, p.tenant_id, E.ts(body.as_of), record=True, actor=C.actor_of(p)))[0]
