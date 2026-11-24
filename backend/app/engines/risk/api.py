"""A3 VECTOR API (mounted at /api/v1/risk). Tenant and actor always come from the server session."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.security import Principal, current_principal, get_db, require_roles
from app.core import services
from app.core.errors import NotFound, Unavailable
from app.core.idempotency import run_idempotent
from app.engines.risk import services as svc
from app.engines.risk.models import (RiskClearingFile, RiskInstrument, RiskPosition, RiskResult, RiskSnapshot, RiskStressRun,
                                     RiskTrade)

router = APIRouter()
WRITERS = ("maker", "compliance_officer", "tenant_admin")


def _ctx(request: Request, p: Principal) -> dict:
    return {"actor_id": p.user_id, "actor_role": ",".join(p.roles), "correlation_id": request.state.correlation_id}


class PortfolioBody(BaseModel):
    instruments: list[dict] = Field(default_factory=list, max_length=5000)
    trades: list[dict] = Field(default_factory=list, max_length=50000)
    positions: list[dict] = Field(default_factory=list, max_length=100000)


@router.post("/trades/import", status_code=201)
def trades_import(body: PortfolioBody, request: Request, db: Session = Depends(get_db), p: Principal = Depends(require_roles(*WRITERS))):
    return svc.import_portfolio(db, p.tenant_id, instruments=body.instruments, trades=body.trades, positions=body.positions, ctx=_ctx(request, p))


class SnapshotBody(BaseModel):
    snapshot_id: str = Field(min_length=1, max_length=80)
    data_as_of: str
    source_mode: str = "REPLAY"
    max_age_seconds: int = Field(default=86400, ge=1, le=31 * 86400)
    payload: dict


@router.post("/snapshots", status_code=201)
def snapshots_pin(body: SnapshotBody, request: Request, db: Session = Depends(get_db), p: Principal = Depends(require_roles(*WRITERS))):
    return svc.pin_snapshot(db, p.tenant_id, body.snapshot_id, svc._ts(body.data_as_of, "data_as_of"), body.payload, source_mode=body.source_mode,
                            max_age_seconds=body.max_age_seconds, ctx=_ctx(request, p))


@router.get("/snapshots")
def snapshots_list(db: Session = Depends(get_db), p: Principal = Depends(current_principal)):
    rows = db.execute(select(RiskSnapshot).where(RiskSnapshot.tenant_id == p.tenant_id).order_by(RiskSnapshot.snapshot_id)).scalars().all()
    return {"items": [svc.snapshot_out(r) for r in rows]}


class ClearingBody(BaseModel):
    csv_text: str = Field(max_length=10_000_000)
    venue: str
    revision: int = Field(default=1, ge=1)
    declared_total: str | None = None
    expected_sha256: str | None = None


@router.post("/clearing/import", status_code=201)
def clearing_import(body: ClearingBody, request: Request, db: Session = Depends(get_db), p: Principal = Depends(require_roles(*WRITERS))):
    return svc.import_clearing_file(db, p.tenant_id, csv_text=body.csv_text, venue=body.venue, revision=body.revision,
                                    declared_total=body.declared_total, expected_sha256=body.expected_sha256, ctx=_ctx(request, p))


@router.get("/clearing")
def clearing_list(account_id: str | None = None, db: Session = Depends(get_db), p: Principal = Depends(current_principal)):
    q = select(RiskClearingFile).where(RiskClearingFile.tenant_id == p.tenant_id)
    if account_id:
        q = q.where(RiskClearingFile.account_id == account_id)
    rows = db.execute(q.order_by(RiskClearingFile.as_of.desc(), RiskClearingFile.file_id, RiskClearingFile.revision.desc()).limit(200)).scalars().all()
    return {"items": [svc.clearing_out(r) for r in rows]}


class CalcBody(BaseModel):
    context: dict
    snapshot_id: str | None = None
    terms_hash: str | None = None
    rule_set_hash: str | None = None
    methodology: str


@router.post("/calculate")
def calculate(body: CalcBody, request: Request, db: Session = Depends(get_db), p: Principal = Depends(require_roles(*WRITERS)),
              idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
    ctx = {k: v for k, v in body.context.items() if k not in svc.ACTOR_KEYS}  # actor/correlation are server-side only
    ctx.update(_ctx(request, p))

    def go():
        return svc.compute_margin(db, p.tenant_id, ctx, body.snapshot_id, body.terms_hash, body.rule_set_hash, body.methodology)

    out, _ = run_idempotent(db, p.tenant_id, idempotency_key, "risk.calculate", body.model_dump(), go)
    return out


class TermsBody(BaseModel):
    netting_set_id: str
    as_of: str


@router.post("/terms/resolve")
def terms_resolve(body: TermsBody, db: Session = Depends(get_db), p: Principal = Depends(require_roles(*WRITERS, "checker", "viewer", "auditor"))):
    """Thin wrapper over contracts.resolve_terms so the desk can show the approved terms hash (or the explicit blocking reason)."""
    as_of = svc._ts(body.as_of)
    try:
        r = services.call("contracts.resolve_terms", db, p.tenant_id, body.netting_set_id, as_of, as_of, None)
    except Unavailable as exc:
        return {"status": "BLOCKED", "terms_hash": None, "blocking_reasons": [exc.message], "terms": None}
    return {"status": r.get("status"), "terms_hash": r.get("terms_hash"), "blocking_reasons": r.get("blocking_reasons", []), "terms": r.get("terms")}


class ReconcileBody(BaseModel):
    file_id: str
    snapshot_id: str | None = None


@router.post("/reconcile")
def reconcile(body: ReconcileBody, request: Request, db: Session = Depends(get_db), p: Principal = Depends(require_roles(*WRITERS))):
    return svc.reconcile_clearing(db, p.tenant_id, body.file_id, body.snapshot_id, ctx=_ctx(request, p))


class StressBody(BaseModel):
    base_result_id: str
    shock_spec: dict


@router.post("/stress")
def stress(body: StressBody, request: Request, db: Session = Depends(get_db), p: Principal = Depends(require_roles(*WRITERS))):
    return svc.stress(db, p.tenant_id, body.base_result_id, body.shock_spec, ctx=_ctx(request, p))


@router.get("/results/{result_id}")
def result_get(result_id: str, db: Session = Depends(get_db), p: Principal = Depends(current_principal)):
    return svc.get_result(db, p.tenant_id, result_id)


@router.get("/results")
def results_list(market_type: str | None = None, limit: int = 50, db: Session = Depends(get_db), p: Principal = Depends(current_principal)):
    q = select(RiskResult).where(RiskResult.tenant_id == p.tenant_id)
    if market_type:
        q = q.where(RiskResult.market_type == market_type)
    rows = db.execute(q.order_by(RiskResult.created_at.desc(), RiskResult.result_id).limit(max(1, min(limit, 200)))).scalars().all()
    return {"items": [{"result_id": r.result_id, "market_type": r.market_type, "subject_id": r.subject_id, "status": r.status, "created_at": r.created_at.isoformat(),
                       "required_amount": r.payload.get("required_amount"), "direction": r.payload.get("direction"), "hypothetical": r.payload.get("hypothetical"),
                       "snapshot_id": r.payload.get("snapshot_id")} for r in rows]}


@router.get("/trades")
def trades_list(netting_set_id: str | None = None, db: Session = Depends(get_db), p: Principal = Depends(current_principal)):
    q = select(RiskTrade).where(RiskTrade.tenant_id == p.tenant_id)
    if netting_set_id:
        q = q.where(RiskTrade.netting_set_id == netting_set_id)
    rows = db.execute(q.order_by(RiskTrade.trade_id).limit(1000)).scalars().all()
    return {"items": [{"trade_id": r.trade_id, "master_id": r.master_id, "netting_set_id": r.netting_set_id, "type": r.trade_type, "maturity": r.maturity,
                       "terms": r.payload} for r in rows]}


@router.get("/positions")
def positions_list(account_id: str, db: Session = Depends(get_db), p: Principal = Depends(current_principal)):
    from app.db.base import utcnow
    rows = svc._positions(db, p.tenant_id, account_id, utcnow().replace(year=2100))
    insts = {r.instrument_id: r.payload for r in db.execute(select(RiskInstrument).where(RiskInstrument.tenant_id == p.tenant_id)).scalars()}
    return {"items": [{**r, "instrument": insts.get(r["instrument_id"])} for r in rows]}


@router.get("/stress/{stress_id}")
def stress_get(stress_id: str, db: Session = Depends(get_db), p: Principal = Depends(current_principal)):
    r = db.get(RiskStressRun, stress_id)
    if r is None or r.tenant_id != p.tenant_id:
        raise NotFound("Stress run not found")
    return r.payload


METHODOLOGY = {
    "method_version": "vector/1.0.0",
    "numerics": "Python decimal, 40-digit precision; floats only inside Black-Scholes scenario revaluation (math.erf), quantised to 2dp.",
    "methods": {
        "OTC_VM_SIMPLE_V1": {
            "summary": "Illustrative one-direction variation-margin waterfall under fictional directional CSA terms. NOT SIMM, NOT certified.",
            "formulas": ["IRS PV = signed discounted fixed/float cashflows (single-period float coupon = N*(DFp(s)/DFp(e)-1))",
                         "FX forward PV (quote ccy) = N*(S*DF_base - K*DF_quote); BUY=+1, SELL=-1; converted with pinned FX",
                         "target = max(0, |E| + IA - Threshold); delta = target - held; MTA tested on UNROUNDED |delta|; directional rounding"],
            "limitations": ["no business-day adjustment or holiday calendars", "single-period stubs unsupported", "no extrapolation beyond the last curve node",
                            "IM is a demonstrative notional-bucket schedule only", "regulatory applicability requires a pinned rule set (else UNKNOWN is flagged)"]},
        "ETD_IMPORTED_V1": {
            "summary": "Imported clearing-style margin is the SOURCE OBLIGATION (independent component sum, reconciled to the reported total). No SPAN replication.",
            "formulas": ["required = imported_total * rule multiplier (only when a rule/hypothetical multiplier is pinned)",
                         "internal estimate = worst-of(+/-shock) futures P&L and Black-Scholes European option revaluation; labelled INTERNAL, never the obligation"],
            "limitations": ["synthetic venues DEMO_NSE (INR) / DEMO_IFSC (USD); profiles independent", "CCP_OTC unsupported"]}},
    "conventions": {"curve_time": "ACT/365F", "interpolation": "log-linear in discount factor", "blocking": "unknown or stale inputs => BLOCKED, never zero"},
}


@router.get("/methodology")
def methodology(p: Principal = Depends(current_principal)):
    return METHODOLOGY
