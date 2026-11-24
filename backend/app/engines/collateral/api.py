"""A4 ATLAS HTTP API (mounted by the gateway at /api/v1/collateral). Tenant comes from the session only."""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.auth.security import Principal, current_principal, get_db, require_roles
from app.core.idempotency import run_idempotent
from app.engines.collateral import services as S

router = APIRouter()
MAKERS = ("maker", "tenant_admin", "compliance_officer")
CHECKERS = ("checker", "compliance_officer")
Idem = Header(default=None, alias="Idempotency-Key")


def _cid(request: Request) -> str:
    return getattr(request.state, "correlation_id", None) or "n/a"


class InventoryImport(BaseModel):
    snapshot_id: str = Field(min_length=1, max_length=80)
    as_of: str
    currency: str
    market_type: str
    policy: dict = Field(default_factory=dict)
    wrong_way_issuer_groups: list[str] = Field(default_factory=list)
    source_ref: str | None = None
    lots: list[dict] | None = None
    csv: str | None = None


class OptimiseBody(BaseModel):
    margin_result_id: str
    inventory_snapshot_id: str
    rule_set_hash: str | None = None
    terms_hash: str | None = None
    deadline: datetime
    horizon_days: int = Field(gt=0, le=3650)


class StressBody(OptimiseBody):
    params: dict = Field(default_factory=dict)


class ReasonBody(BaseModel):
    reason: str = ""
    expected_revision: int | None = None


class ReserveBody(BaseModel):
    approval_id: str
    expected_revision: int | None = None


class RevBody(BaseModel):
    expected_revision: int | None = None


@router.post("/inventory/import", status_code=201)
def import_inventory(body: InventoryImport, request: Request, db: Session = Depends(get_db),
                     p: Principal = Depends(require_roles(*MAKERS)), idempotency_key: str | None = Idem):
    payload = body.model_dump()
    out, _ = run_idempotent(db, p.tenant_id, idempotency_key, "collateral.inventory.import", payload,
                            lambda: S.import_inventory(db, p.tenant_id, actor_id=p.user_id, role=",".join(p.roles),
                                                       payload=payload, correlation_id=_cid(request)))
    return out


@router.get("/inventory")
def list_inventory(db: Session = Depends(get_db), p: Principal = Depends(current_principal)):
    return {"items": S.list_snapshots(db, p.tenant_id)}


@router.get("/inventory/{snapshot_id}")
def get_inventory(snapshot_id: str, db: Session = Depends(get_db), p: Principal = Depends(current_principal)):
    return S.snapshot_lots(db, p.tenant_id, snapshot_id)


@router.post("/optimise", status_code=201)
def optimise(body: OptimiseBody, request: Request, db: Session = Depends(get_db),
             p: Principal = Depends(require_roles(*MAKERS)), idempotency_key: str | None = Idem):
    payload = body.model_dump(mode="json")
    out, _ = run_idempotent(db, p.tenant_id, idempotency_key, "collateral.optimise", payload, lambda: S.optimise(
        db, p.tenant_id, body.margin_result_id, body.inventory_snapshot_id, body.rule_set_hash, body.terms_hash, body.deadline,
        body.horizon_days, actor_id=p.user_id, actor_type="USER", actor_role=",".join(p.roles), correlation_id=_cid(request)))
    return out


@router.post("/stress")
def stress(body: StressBody, request: Request, db: Session = Depends(get_db),
           p: Principal = Depends(require_roles(*MAKERS, "checker")), idempotency_key: str | None = Idem):
    payload = body.model_dump(mode="json")
    out, _ = run_idempotent(db, p.tenant_id, idempotency_key, "collateral.stress", payload, lambda: S.stress(
        db, p.tenant_id, body.margin_result_id, body.inventory_snapshot_id, body.rule_set_hash, body.terms_hash, body.deadline,
        body.horizon_days, body.params, actor_id=p.user_id, actor_role=",".join(p.roles), correlation_id=_cid(request)))
    return out


@router.get("/desk")
def desk(margin_result_id: str, inventory_snapshot_id: str, deadline: datetime, horizon_days: int = 30,
         rule_set_hash: str | None = None, terms_hash: str | None = None, db: Session = Depends(get_db),
         p: Principal = Depends(current_principal)):
    return S.desk(db, p.tenant_id, margin_result_id, inventory_snapshot_id, rule_set_hash, terms_hash, deadline, horizon_days)


@router.get("/coverage")
def coverage(account_id: str, as_of: datetime, db: Session = Depends(get_db), p: Principal = Depends(current_principal)):
    return S.posted_coverage(db, p.tenant_id, account_id, as_of)


@router.get("/proposals")
def list_proposals(limit: int = 50, state: str | None = None, db: Session = Depends(get_db),
                   p: Principal = Depends(current_principal)):
    return {"items": S.list_proposals(db, p.tenant_id, limit, state)}


@router.get("/proposals/{proposal_id}")
def get_proposal(proposal_id: str, db: Session = Depends(get_db), p: Principal = Depends(current_principal)):
    return S.get_proposal(db, p.tenant_id, proposal_id)


@router.post("/proposals/{proposal_id}/verify")
def verify(proposal_id: str, db: Session = Depends(get_db), p: Principal = Depends(require_roles(*MAKERS, *CHECKERS))):
    return S.verify(db, p.tenant_id, proposal_id, actor_id=p.user_id, actor_type="USER", actor_role=",".join(p.roles))


@router.post("/proposals/{proposal_id}/approve")
def approve(proposal_id: str, body: ReasonBody, db: Session = Depends(get_db),
            p: Principal = Depends(require_roles(*CHECKERS)), idempotency_key: str | None = Idem):
    out, _ = run_idempotent(db, p.tenant_id, idempotency_key, f"collateral.approve.{proposal_id}", body.model_dump(),
                            lambda: S.approve_proposal(db, p.tenant_id, p, proposal_id, reason=body.reason,
                                                       expected_revision=body.expected_revision))
    return out


@router.post("/proposals/{proposal_id}/reserve")
def reserve(proposal_id: str, body: ReserveBody, db: Session = Depends(get_db),
            p: Principal = Depends(require_roles(*MAKERS, *CHECKERS)), idempotency_key: str | None = Idem):
    out, _ = run_idempotent(db, p.tenant_id, idempotency_key, f"collateral.reserve.{proposal_id}", body.model_dump(),
                            lambda: S.reserve(db, p.tenant_id, proposal_id, body.approval_id, actor_id=p.user_id,
                                              actor_type="USER", actor_role=",".join(p.roles),
                                              expected_revision=body.expected_revision))
    return out


@router.post("/proposals/{proposal_id}/cancel")
def cancel(proposal_id: str, body: ReasonBody, db: Session = Depends(get_db),
           p: Principal = Depends(require_roles(*MAKERS, *CHECKERS)), idempotency_key: str | None = Idem):
    out, _ = run_idempotent(db, p.tenant_id, idempotency_key, f"collateral.cancel.{proposal_id}", body.model_dump(),
                            lambda: S.cancel(db, p.tenant_id, proposal_id, actor_id=p.user_id, actor_role=",".join(p.roles),
                                             reason=body.reason, expected_revision=body.expected_revision))
    return out


@router.post("/proposals/{proposal_id}/draft")
def draft(proposal_id: str, body: RevBody, db: Session = Depends(get_db),
          p: Principal = Depends(require_roles(*MAKERS, *CHECKERS)), idempotency_key: str | None = Idem):
    out, _ = run_idempotent(db, p.tenant_id, idempotency_key, f"collateral.draft.{proposal_id}", body.model_dump(),
                            lambda: S.draft_instruction(db, p.tenant_id, proposal_id, actor_id=p.user_id,
                                                        actor_role=",".join(p.roles), expected_revision=body.expected_revision))
    return out


@router.get("/proposals/{proposal_id}/instruction")
def instruction(proposal_id: str, db: Session = Depends(get_db), p: Principal = Depends(current_principal)):
    return S.get_instruction(db, p.tenant_id, proposal_id)


@router.post("/proposals/{proposal_id}/simulate-settlement")
def simulate_settlement(proposal_id: str, body: RevBody, db: Session = Depends(get_db),
                        p: Principal = Depends(require_roles(*MAKERS, *CHECKERS)), idempotency_key: str | None = Idem):
    out, _ = run_idempotent(db, p.tenant_id, idempotency_key, f"collateral.simsettle.{proposal_id}", body.model_dump(),
                            lambda: S.simulate_settlement(db, p.tenant_id, proposal_id, actor_id=p.user_id,
                                                          actor_role=",".join(p.roles), expected_revision=body.expected_revision))
    return out
