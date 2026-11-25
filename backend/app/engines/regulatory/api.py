"""A1 REGULUS HTTP API (mounted at /api/v1/regulatory by the gateway). Tenant comes only from the session."""
from __future__ import annotations

from datetime import date, datetime

from fastapi import APIRouter, Depends, File, Form, Header, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth.security import Principal, current_principal, get_db, require_roles
from app.core.config import get_settings
from app.core.errors import NotFound, ValidationFailed
from app.core.idempotency import run_idempotent
from app.db.base import new_id, utcnow
from app.db.models_core import ApprovalRow
from app.engines.regulatory import dsl, impact, registry, resolve
from app.engines.regulatory.models import RegCandidate, RegImpactRun, RegRule, RegRuleTest, RegSource, RegSpan

router = APIRouter()
IDEM = Header(default=None, alias="Idempotency-Key")
MAKER = require_roles("maker")
CHECKER = require_roles("checker", "compliance_officer")
ANALYST = require_roles("maker", "checker", "compliance_officer", "tenant_admin")


def _iso(d):
    return d.isoformat() if d else None


def source_out(s: RegSource, text: bool = False) -> dict:
    o = {"source_id": s.source_id, "authority_type": s.authority_type, "doc_kind": s.doc_kind, "title": s.title, "issuer": s.issuer,
         "doc_number": s.doc_number, "official_uri": s.official_uri, "published_at": _iso(s.published_at), "effective_date": _iso(s.effective_date),
         "retrieved_at": _iso(s.retrieved_at), "classification": s.classification, "verification": s.verification, "kind": s.kind,
         "sha256": s.sha256, "text_sha256": s.text_sha256, "page_count": s.page_count, "unreadable_pages": s.unreadable_pages,
         "parser_version": s.parser_version, "risk_flags": s.risk_flags, "provenance": s.provenance, "created_at": _iso(s.created_at)}
    if text:
        o["text"] = s.text
    return o


def span_out(p: RegSpan) -> dict:
    return {"span_id": p.span_id, "source_id": p.source_id, "page": p.page, "start": p.start, "end": p.end, "byte_start": p.byte_start,
            "byte_end": p.byte_end, "quote": p.quote, "method": p.method}


def cand_out(c: RegCandidate) -> dict:
    return {"candidate_id": c.candidate_id, "source_id": c.source_id, "mode": c.mode, "fields": c.fields, "span_ids": c.span_ids,
            "issues": c.issues, "grounded": c.grounded, "confidence_category": c.confidence_category, "status": c.status,
            "note": "confidence_category describes the extraction method, not accuracy"}


def effective_state(r: RegRule, now: datetime) -> str:
    return "ACTIVE" if r.status == "SCHEDULED" and r.effective_from <= now else r.status


def rule_out(r: RegRule, detail: bool = False, db: Session | None = None) -> dict:
    o = {"rule_id": r.rule_id, "rule_key": r.rule_key, "version": r.version, "status": r.status, "effective_state": effective_state(r, utcnow()),
         "authority_type": r.authority_type, "title": r.title, "markets": r.markets, "effective_from": _iso(r.effective_from),
         "effective_to": _iso(r.effective_to), "recorded_at": _iso(r.created_at), "activated_at": _iso(r.activated_at),
         "withdrawn_at": _iso(r.withdrawn_at), "supersedes_rule_id": r.supersedes_rule_id, "superseded_by_rule_id": r.superseded_by_rule_id,
         "content_hash": r.content_hash, "approved_hash": r.approved_hash, "maker_id": r.maker_id, "revision": r.revision,
         "compile_ok": bool(r.compile_report.get("ok")), "conflict_group": r.conflict_group, "precedence_rank": r.precedence_rank,
         "provenance": r.provenance, "span_ids": r.span_ids}
    if detail:
        o |= {"scope": r.scope, "condition": r.condition, "parameters": r.parameters, "exceptions": r.exceptions, "rationale": r.rationale,
              "compile_report": r.compile_report, "review_notes": r.review_notes, "additive_stricter": r.additive_stricter}
        if db is not None:
            o["tests"] = [{"test_id": t.test_id, "vector": t.vector, "passed": t.passed, "last_result": t.last_result}
                          for t in db.execute(select(RegRuleTest).where(RegRuleTest.rule_id == r.rule_id)).scalars()]
            o["spans"] = [span_out(p) for p in (db.get(RegSpan, s) for s in r.span_ids) if p]
            o["approvals"] = [{"approval_id": a.approval_id, "decision": a.decision, "maker_id": a.maker_id, "checker_id": a.checker_id,
                               "target_hash": a.target_hash, "target_revision": a.target_revision, "expires_at": _iso(a.expires_at),
                               "decided_at": _iso(a.decided_at), "reason": a.reason}
                              for a in db.execute(select(ApprovalRow).where(ApprovalRow.tenant_id == r.tenant_id, ApprovalRow.target_id == r.rule_id)).scalars()]
    return o


# ------------------------------------------------------------------ documents
@router.post("/documents", status_code=201)
async def upload_document(file: UploadFile = File(...), authority_type: str = Form(...), title: str = Form(...), doc_kind: str = Form("NOTICE"),
                          issuer: str | None = Form(None), doc_number: str | None = Form(None), official_uri: str | None = Form(None),
                          published_at: date | None = Form(None), effective_date: date | None = Form(None),
                          db: Session = Depends(get_db), p: Principal = Depends(MAKER), idempotency_key: str | None = IDEM):
    data = await file.read(get_settings().max_upload_bytes + 1)
    meta = {"authority_type": authority_type, "title": title, "doc_kind": doc_kind, "issuer": issuer, "doc_number": doc_number,
            "official_uri": official_uri, "published_at": published_at, "effective_date": effective_date}

    def make():
        src, dup = registry.ingest_document(db, p, data, file.content_type, meta)
        return source_out(src) | {"deduplicated": dup}

    from app.core.canonical import sha256_hex
    out, _ = run_idempotent(db, p.tenant_id, idempotency_key, "reg.documents.upload",
                            {"sha": sha256_hex(data), **{k: str(v) for k, v in meta.items()}}, make)
    return out


@router.get("/documents")
def list_documents(authority_type: str | None = None, limit: int = 50, db: Session = Depends(get_db), p: Principal = Depends(current_principal)):
    q = select(RegSource).where(RegSource.tenant_id == p.tenant_id)
    if authority_type:
        q = q.where(RegSource.authority_type == authority_type)
    rows = db.execute(q.order_by(RegSource.created_at.desc(), RegSource.source_id).limit(max(1, min(limit, 200)))).scalars().all()
    return {"items": [source_out(s) for s in rows]}


@router.get("/documents/{source_id}")
def get_document(source_id: str, db: Session = Depends(get_db), p: Principal = Depends(current_principal)):
    return source_out(registry.get_source(db, p.tenant_id, source_id), text=True)


@router.get("/documents/{source_id}/spans")
def document_spans(source_id: str, db: Session = Depends(get_db), p: Principal = Depends(current_principal)):
    registry.get_source(db, p.tenant_id, source_id)
    rows = db.execute(select(RegSpan).where(RegSpan.tenant_id == p.tenant_id, RegSpan.source_id == source_id).order_by(RegSpan.start)).scalars().all()
    return {"items": [span_out(s) for s in rows]}


class SpanBody(BaseModel):
    start: int
    end: int


@router.post("/documents/{source_id}/spans", status_code=201)
def create_span(source_id: str, body: SpanBody, db: Session = Depends(get_db), p: Principal = Depends(MAKER)):
    src = registry.get_source(db, p.tenant_id, source_id)
    return span_out(registry.make_span(db, p, src, body.start, body.end, "MANUAL"))


class ExtractBody(BaseModel):
    mode: str = Field(default="REPLAY")
    candidates: list[dict] | None = None


@router.post("/documents/{source_id}/extract", status_code=201)
def extract(source_id: str, body: ExtractBody, db: Session = Depends(get_db), p: Principal = Depends(MAKER), idempotency_key: str | None = IDEM):
    def make():
        cs = registry.extract(db, p, source_id, body.mode, body.candidates)
        return {"mode": body.mode, "candidates": [cand_out(c) for c in cs]}
    out, _ = run_idempotent(db, p.tenant_id, idempotency_key, f"reg.extract.{source_id}", body.model_dump(), make)
    return out


@router.get("/documents/{source_id}/candidates")
def list_candidates(source_id: str, db: Session = Depends(get_db), p: Principal = Depends(current_principal)):
    registry.get_source(db, p.tenant_id, source_id)
    rows = db.execute(select(RegCandidate).where(RegCandidate.tenant_id == p.tenant_id, RegCandidate.source_id == source_id)
                      .order_by(RegCandidate.created_at, RegCandidate.candidate_id)).scalars().all()
    return {"items": [cand_out(c) for c in rows]}


# ------------------------------------------------------------------ rules
class RuleBody(BaseModel):
    rule_key: str
    title: str = ""
    authority_type: str
    scope: dict
    condition: dict
    parameters: dict = {}
    exceptions: list = []
    span_ids: list[str] = []
    conflict_group: str | None = None
    precedence_rank: int | None = None
    additive_stricter: bool = False
    effective_from: datetime
    effective_to: datetime | None = None
    rationale: str = ""
    candidate_id: str | None = None
    test_vectors: list[dict] = []


class RulePatch(BaseModel):
    title: str | None = None
    scope: dict | None = None
    condition: dict | None = None
    parameters: dict | None = None
    exceptions: list | None = None
    span_ids: list[str] | None = None
    conflict_group: str | None = None
    precedence_rank: int | None = None
    additive_stricter: bool | None = None
    effective_from: datetime | None = None
    effective_to: datetime | None = None
    rationale: str = ""
    test_vectors: list[dict] | None = None
    expected_revision: int | None = None


@router.get("/field-registry")
def field_registry(p: Principal = Depends(current_principal)):
    return {"fields": dsl.FIELD_REGISTRY, "row_fields": dsl.ROW_REGISTRY, "units": sorted(dsl.UNITS),
            "operators": sorted(dsl.BOOL_OPS | dsl.VALUE_OPS), "limits": {"depth": dsl.MAX_DEPTH, "nodes": dsl.MAX_NODES, "steps": dsl.MAX_STEPS}}


class CompileBody(BaseModel):
    scope: dict
    condition: dict
    parameters: dict = {}
    test_vectors: list[dict] = []


@router.post("/compile")
def compile_dry_run(body: CompileBody, p: Principal = Depends(ANALYST)):
    rep = dsl.compile_rule(body.scope, body.condition, body.parameters)
    tests = dsl.run_test_vectors(body.scope, body.condition, body.parameters, body.test_vectors) if rep["ok"] else []
    return {"compile": rep, "tests": tests}


@router.get("/rules")
def list_rules(status: str | None = None, authority_type: str | None = None, rule_key: str | None = None, market: str | None = None,
               limit: int = 100, db: Session = Depends(get_db), p: Principal = Depends(current_principal)):
    q = select(RegRule).where(RegRule.tenant_id == p.tenant_id)
    if status:
        q = q.where(RegRule.status == status)
    if authority_type:
        q = q.where(RegRule.authority_type == authority_type)
    if rule_key:
        q = q.where(RegRule.rule_key == rule_key)
    rows = db.execute(q.order_by(RegRule.rule_key, RegRule.version.desc()).limit(max(1, min(limit, 500)))).scalars().all()
    if market:
        rows = [r for r in rows if market in (r.markets or [])]
    return {"items": [rule_out(r) for r in rows]}


@router.post("/rules", status_code=201)
def create_rule(body: RuleBody, db: Session = Depends(get_db), p: Principal = Depends(MAKER), idempotency_key: str | None = IDEM):
    def make():
        return rule_out(registry.create_rule(db, p, body.model_dump()), True, db)
    out, _ = run_idempotent(db, p.tenant_id, idempotency_key, "reg.rules.create", body.model_dump(mode="json"), make)
    return out


@router.get("/rules/{rule_id}")
def get_rule(rule_id: str, db: Session = Depends(get_db), p: Principal = Depends(current_principal)):
    return rule_out(registry.get_rule(db, p.tenant_id, rule_id), True, db)


@router.patch("/rules/{rule_id}")
def patch_rule(rule_id: str, body: RulePatch, db: Session = Depends(get_db), p: Principal = Depends(MAKER)):
    ch = {k: v for k, v in body.model_dump(exclude_unset=True).items() if k != "expected_revision"}
    return rule_out(registry.update_draft(db, p, rule_id, ch, body.expected_revision), True, db)


class ReviseBody(BaseModel):
    rationale: str
    effective_from: datetime | None = None


@router.post("/rules/{rule_id}/revise", status_code=201)
def revise_rule(rule_id: str, body: ReviseBody, db: Session = Depends(get_db), p: Principal = Depends(MAKER)):
    return rule_out(registry.revise(db, p, rule_id, body.rationale, body.effective_from), True, db)


class ReviewBody(BaseModel):
    decision: str  # SUBMIT | APPROVE | REJECT
    reason: str = ""
    expected_revision: int | None = None


@router.post("/rules/{rule_id}/review")
def review_rule(rule_id: str, body: ReviewBody, db: Session = Depends(get_db), p: Principal = Depends(current_principal),
                idempotency_key: str | None = IDEM):
    if body.decision not in ("SUBMIT", "APPROVE", "REJECT"):
        raise ValidationFailed("decision must be SUBMIT | APPROVE | REJECT", field_errors={"decision": "invalid"})

    def make():
        if body.decision == "SUBMIT":
            if not p.has("maker"):
                from app.core.errors import Forbidden
                raise Forbidden("Maker role required to submit")
            r = registry.submit(db, p, rule_id, body.reason, body.expected_revision)
        else:  # denials for APPROVE/REJECT are recorded inside the registry before the 403 is raised
            r = registry.review(db, p, rule_id, body.decision, body.reason, body.expected_revision)
        return rule_out(r, True, db)
    out, _ = run_idempotent(db, p.tenant_id, idempotency_key, f"reg.review.{rule_id}", body.model_dump(), make)
    return out


class ActivateBody(BaseModel):
    reason: str
    expected_revision: int | None = None
    as_of: datetime | None = None


@router.post("/rules/{rule_id}/activate")
def activate_rule(rule_id: str, body: ActivateBody, db: Session = Depends(get_db), p: Principal = Depends(CHECKER), idempotency_key: str | None = IDEM):
    def make():
        return rule_out(registry.activate(db, p, rule_id, body.expected_revision, body.reason, body.as_of), True, db)
    out, _ = run_idempotent(db, p.tenant_id, idempotency_key, f"reg.activate.{rule_id}", body.model_dump(mode="json"), make)
    return out


class WithdrawBody(BaseModel):
    reason: str
    expected_revision: int | None = None


@router.post("/rules/{rule_id}/withdraw")
def withdraw_rule(rule_id: str, body: WithdrawBody, db: Session = Depends(get_db), p: Principal = Depends(CHECKER)):
    return rule_out(registry.withdraw(db, p, rule_id, body.reason, body.expected_revision), True, db)


class AdvanceBody(BaseModel):
    now: datetime


@router.post("/rules/advance")
def advance(body: AdvanceBody, db: Session = Depends(get_db), p: Principal = Depends(CHECKER)):
    return {"became_effective": registry.advance_states(db, p, body.now)}


@router.get("/rules/{rule_id}/diff")
def rule_diff(rule_id: str, against: str, db: Session = Depends(get_db), p: Principal = Depends(current_principal)):
    a, b = registry.get_rule(db, p.tenant_id, against), registry.get_rule(db, p.tenant_id, rule_id)
    if a.rule_key != b.rule_key:
        raise ValidationFailed("diff is defined between versions of the same rule_key", field_errors={"against": "different rule_key"})
    old, new = sorted([a, b], key=lambda r: r.version)
    return {"from": rule_out(old), "to": rule_out(new), "diff": registry.diff(old, new)}


# ------------------------------------------------------------------ resolve / impact / summary
class ResolveBody(BaseModel):
    context: dict
    as_of: datetime
    known_at: datetime | None = None
    hypothetical_rule_ids: list[str] | None = None


@router.post("/resolve")
def resolve_endpoint(body: ResolveBody, db: Session = Depends(get_db), p: Principal = Depends(current_principal)):
    return resolve.resolve_rules(db, p.tenant_id, body.context, body.as_of, body.known_at, body.hypothetical_rule_ids)


class ImpactBody(BaseModel):
    as_of: datetime
    known_at: datetime | None = None
    hypothetical_rule_ids: list[str]
    accounts: list[dict]
    include_feasibility: bool = False  # opt-in: ask ATLAS whether the extra requirement is coverable (creates HYPOTHETICAL proposals)


def impact_out(r: RegImpactRun) -> dict:
    return {"impact_id": r.impact_id, "status": r.status, "hypothetical": r.hypothetical, "official": False, "created_at": _iso(r.created_at),
            "banner": "HYPOTHETICAL COUNTERFACTUAL - not an operational compliance status", "params": r.params, "results": r.results}


@router.post("/impact", status_code=201)
def run_impact(body: ImpactBody, db: Session = Depends(get_db), p: Principal = Depends(ANALYST), idempotency_key: str | None = IDEM):
    def make():
        return impact_out(impact.run_impact(db, p, body.model_dump()))
    out, _ = run_idempotent(db, p.tenant_id, idempotency_key, "reg.impact", body.model_dump(mode="json"), make)
    return out


@router.get("/impact")
def list_impact(db: Session = Depends(get_db), p: Principal = Depends(current_principal)):
    rows = db.execute(select(RegImpactRun).where(RegImpactRun.tenant_id == p.tenant_id).order_by(RegImpactRun.created_at.desc()).limit(50)).scalars().all()
    return {"items": [{"impact_id": r.impact_id, "status": r.status, "hypothetical": True, "official": False, "created_at": _iso(r.created_at),
                       "counts": r.results.get("counts")} for r in rows]}


@router.get("/impact/{impact_id}")
def get_impact(impact_id: str, db: Session = Depends(get_db), p: Principal = Depends(current_principal)):
    return impact_out(impact.get_impact(db, p.tenant_id, impact_id))


@router.get("/summary")
def summary(db: Session = Depends(get_db), p: Principal = Depends(current_principal)):
    now = utcnow()
    counts = dict(db.execute(select(RegRule.status, func.count()).where(RegRule.tenant_id == p.tenant_id).group_by(RegRule.status)).all())
    pending = db.execute(select(func.count()).select_from(ApprovalRow).where(
        ApprovalRow.tenant_id == p.tenant_id, ApprovalRow.target_type == "reg_rule", ApprovalRow.decision == "PENDING")).scalar_one()
    sched = db.execute(select(RegRule).where(RegRule.tenant_id == p.tenant_id, RegRule.status == "SCHEDULED").order_by(RegRule.effective_from)).scalars().all()
    srcs = db.execute(select(RegSource.authority_type, func.count(), func.max(RegSource.retrieved_at)).where(RegSource.tenant_id == p.tenant_id)
                      .group_by(RegSource.authority_type)).all()
    last = db.execute(select(RegImpactRun).where(RegImpactRun.tenant_id == p.tenant_id).order_by(RegImpactRun.created_at.desc())).scalars().first()
    return {"rule_counts": counts, "pending_approvals": pending,
            "upcoming_changes": [{"rule_id": r.rule_id, "rule_key": r.rule_key, "version": r.version, "effective_from": _iso(r.effective_from)} for r in sched],
            "source_freshness": [{"authority_type": a, "documents": n, "latest_retrieved_at": _iso(m)} for a, n, m in srcs],
            "latest_impact": None if last is None else {"impact_id": last.impact_id, "status": last.status, "counts": last.results.get("counts"), "hypothetical": True},
            "affected_accounts": None if last is None else last.results.get("counts", {}).get("affected"),
            "as_of_server_time": _iso(now), "data_mode": "SYNTHETIC"}
