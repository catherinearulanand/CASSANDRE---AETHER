"""A2 COVENANT API (mounted at /api/v1/contracts). Tenant comes from the session; every mutation needs an
Idempotency-Key; reviews carry expected revisions (409 on conflict)."""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, Header, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.auth.security import Principal, current_principal, get_db
from app.core.errors import ValidationFailed
from app.core.idempotency import run_idempotent
from app.engines.contracts import services as S
from app.engines.contracts import terms as T

router = APIRouter()
IDEM = Header(default=None, alias="Idempotency-Key")


def _cid(request: Request) -> str:
    return getattr(request.state, "correlation_id", "n/a")


def _dt(s: str | None, name: str):
    if s is None:
        return None
    try:
        return T.parse_dt(s)
    except Exception:  # noqa: BLE001
        raise ValidationFailed(f"{name} must be ISO-8601", field_errors={name: "invalid datetime"}) from None


class DocIn(BaseModel):
    kind: str | None = None
    title: str | None = None
    doc_ref: str
    text: str
    effective_from: str
    missing_annexes: list[str] = Field(default_factory=list)


class PackageIn(BaseModel):
    master_id: str
    csa_id: str
    netting_set_id: str
    party_a_id: str
    party_b_id: str
    party_a_name: str
    party_b_name: str
    base_currency: str
    governing_law: str
    documents: list[DocIn]


class ExtractIn(BaseModel):
    mode: Literal["RULES", "REPLAY", "LIVE_MODEL"] = "RULES"
    doc_id: str | None = None


class ReviewIn(BaseModel):
    field_id: str
    action: Literal["CORRECT", "APPROVE", "REJECT", "MARK_UNRESOLVED"]
    reason: str | None = None
    expected_revision: int
    corrected_value: Any = None
    direction: Literal["A", "B"] | None = None


class FreezeProposeIn(BaseModel):
    reason: str | None = None


class FreezeIn(BaseModel):
    approval_id: str
    reason: str | None = None


class TradeIn(BaseModel):
    trade_id: str
    declared_master_id: str | None = None
    declared_netting_set_id: str | None = None


class NettingImportIn(BaseModel):
    trades: list[TradeIn] = Field(min_length=1, max_length=1000)


class FactsIn(BaseModel):
    facts: dict[str, dict[str, Any]]


def _idem(db, p, key, scope, payload, fn):
    out, _ = run_idempotent(db, p.tenant_id, key, scope, payload, fn)
    return out


# ------------------------------------------------------------------ literal routes first
@router.post("/packages", status_code=201)
def create_package(body: PackageIn, request: Request, db: Session = Depends(get_db), p: Principal = Depends(current_principal),
                   key: str | None = IDEM):
    return _idem(db, p, key, "contracts.create_package", body.model_dump(),
                 lambda: S.create_package(db, p, _cid(request), body.model_dump()))


@router.get("/packages")
def list_packages(status: str | None = None, currency: str | None = None, counterparty: str | None = None,
                  needs_review: bool | None = None, limit: int = 50, cursor: str | None = None,
                  db: Session = Depends(get_db), p: Principal = Depends(current_principal)):
    S._scope(p)
    return S.list_packages(db, p.tenant_id, status, currency, counterparty, needs_review, limit, cursor)


@router.post("/netting/import")
def netting_import(body: NettingImportIn, request: Request, db: Session = Depends(get_db),
                   p: Principal = Depends(current_principal), key: str | None = IDEM):
    def go():
        res = [S.register_trade(db, p, _cid(request), t.trade_id, t.declared_master_id, t.declared_netting_set_id) for t in body.trades]
        return {"items": res, "counts": {s: sum(1 for r in res if r["status"] == s) for s in ("MEMBER", "QUARANTINED", "REJECTED")}}
    return _idem(db, p, key, "contracts.netting_import", body.model_dump(), go)


@router.get("/netting/quarantine")
def quarantine(db: Session = Depends(get_db), p: Principal = Depends(current_principal)):
    S._scope(p)
    return S.quarantine_list(db, p.tenant_id)


@router.get("/netting/membership/{trade_id}")
def membership(trade_id: str, db: Session = Depends(get_db), p: Principal = Depends(current_principal)):
    S._scope(p)
    return S.netting_membership(db, p.tenant_id, trade_id)


@router.post("/facts", status_code=201)
def record_facts(body: FactsIn, request: Request, db: Session = Depends(get_db), p: Principal = Depends(current_principal),
                 key: str | None = IDEM):
    return _idem(db, p, key, "contracts.facts", body.model_dump(), lambda: S.record_facts(db, p, _cid(request), body.facts))


class ResolveRecordIn(BaseModel):
    netting_set_id: str
    as_of: str
    known_at: str | None = None
    fact_snapshot_id: str | None = None


@router.post("/resolve/record")
def resolve_record(body: ResolveRecordIn, request: Request, p: Principal = Depends(current_principal), db: Session = Depends(get_db),
                   idem: str | None = IDEM):
    """Audited resolution: records an evidence event with a replay spec (known_at is pinned so it stays reproducible)."""
    out, _ = run_idempotent(db, p.tenant_id, idem, "contracts.resolve.record", body.model_dump(), lambda: S.record_resolution(
        db, p, _cid(request), body.netting_set_id, _dt(body.as_of, "as_of"), _dt(body.known_at, "known_at"), body.fact_snapshot_id))
    return out


@router.get("/resolve")
def resolve(netting_set_id: str, as_of: str, known_at: str | None = None, fact_snapshot_id: str | None = None,
            db: Session = Depends(get_db), p: Principal = Depends(current_principal)):
    S._scope(p)
    return S.resolve_terms(db, p.tenant_id, netting_set_id, _dt(as_of, "as_of"), _dt(known_at, "known_at"), fact_snapshot_id)


@router.get("/terms-by-hash/{terms_hash}")
def terms_by_hash(terms_hash: str, db: Session = Depends(get_db), p: Principal = Depends(current_principal)):
    S._scope(p)
    from app.core.errors import NotFound
    r = S.terms_by_hash(db, p.tenant_id, terms_hash)
    if r is None:
        raise NotFound("Terms hash not found")
    return r


# ------------------------------------------------------------------ package-scoped routes
@router.get("/{package_id}")
def detail(package_id: str, db: Session = Depends(get_db), p: Principal = Depends(current_principal)):
    S._scope(p)
    return S.package_detail(db, p.tenant_id, package_id)


@router.post("/{package_id}/documents", status_code=201)
def add_document(package_id: str, body: DocIn, request: Request, db: Session = Depends(get_db),
                 p: Principal = Depends(current_principal), key: str | None = IDEM):
    return _idem(db, p, key, f"contracts.doc:{package_id}", body.model_dump(),
                 lambda: S.add_document(db, p, _cid(request), package_id, body.model_dump(), amendment=False))


@router.get("/{package_id}/documents/{doc_id}")
def document(package_id: str, doc_id: str, db: Session = Depends(get_db), p: Principal = Depends(current_principal)):
    S._scope(p)
    return S.document_view(db, p.tenant_id, package_id, doc_id)


@router.post("/{package_id}/extract")
def extract(package_id: str, body: ExtractIn, request: Request, db: Session = Depends(get_db),
            p: Principal = Depends(current_principal), key: str | None = IDEM):
    return _idem(db, p, key, f"contracts.extract:{package_id}", body.model_dump(),
                 lambda: S.extract(db, p, _cid(request), package_id, body.mode, body.doc_id))


@router.get("/{package_id}/terms")
def terms(package_id: str, db: Session = Depends(get_db), p: Principal = Depends(current_principal)):
    S._scope(p)
    return S.terms_view(db, p.tenant_id, package_id)


@router.post("/{package_id}/review")
def review(package_id: str, body: ReviewIn, request: Request, db: Session = Depends(get_db),
           p: Principal = Depends(current_principal), key: str | None = IDEM):
    return _idem(db, p, key, f"contracts.review:{package_id}", body.model_dump(),
                 lambda: S.review_field(db, p, _cid(request), package_id, body.field_id, body.action, body.reason,
                                        body.expected_revision, body.corrected_value, body.direction))


@router.post("/{package_id}/freeze/propose")
def freeze_propose(package_id: str, body: FreezeProposeIn, request: Request, db: Session = Depends(get_db),
                   p: Principal = Depends(current_principal), key: str | None = IDEM):
    return _idem(db, p, key, f"contracts.freeze_propose:{package_id}", body.model_dump(),
                 lambda: S.propose_freeze(db, p, _cid(request), package_id, body.reason))


@router.post("/{package_id}/freeze")
def freeze(package_id: str, body: FreezeIn, request: Request, db: Session = Depends(get_db),
           p: Principal = Depends(current_principal), key: str | None = IDEM):
    return _idem(db, p, key, f"contracts.freeze:{package_id}", body.model_dump(),
                 lambda: S.freeze(db, p, _cid(request), package_id, body.approval_id, body.reason))


@router.post("/{package_id}/amendments", status_code=201)
def amendments(package_id: str, body: DocIn, request: Request, db: Session = Depends(get_db),
               p: Principal = Depends(current_principal), key: str | None = IDEM):
    return _idem(db, p, key, f"contracts.amend:{package_id}", body.model_dump(),
                 lambda: S.add_document(db, p, _cid(request), package_id, body.model_dump(), amendment=True))


@router.get("/{package_id}/versions")
def versions(package_id: str, entries: bool = False, db: Session = Depends(get_db), p: Principal = Depends(current_principal)):
    S._scope(p)
    return S.list_versions(db, p.tenant_id, package_id, entries)


@router.get("/{package_id}/versions/compare")
def compare(package_id: str, a: str, b: str, as_of: str, db: Session = Depends(get_db), p: Principal = Depends(current_principal)):
    S._scope(p)
    return S.compare_versions(db, p.tenant_id, package_id, a, b, _dt(as_of, "as_of"))


@router.get("/{package_id}/netting")
def netting(package_id: str, db: Session = Depends(get_db), p: Principal = Depends(current_principal)):
    S._scope(p)
    return S.netting_map(db, p.tenant_id, package_id)
