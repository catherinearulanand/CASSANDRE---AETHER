"""A6 VERITAS HTTP API. There is deliberately NO generic append endpoint: evidence is only written by engine services
inside their own transactions (and by the export/replay actions below, which record their own events)."""
from __future__ import annotations

import io
import zipfile
from datetime import datetime

from fastapi import APIRouter, Depends, Header, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.security import Principal, current_principal, get_db, require_roles
from app.core.errors import NotFound, ValidationFailed
from app.core.events import emit
from app.core.idempotency import run_idempotent
from app.db.models_core import Run
from app.engines.evidence import keys, ops
from app.engines.evidence.bundle import build_bundle
from app.engines.evidence.checkpoints import create_checkpoint, exported_checkpoints, list_checkpoints, verify_against_exported
from app.engines.evidence.lineage import build_lineage
from app.engines.evidence.models import EvExport
from app.engines.evidence.objects import read_object
from app.engines.evidence.replay import replay_run
from app.engines.evidence.tamper import MODES, tamper_demo

router = APIRouter()
READ_ROLES = ("viewer", "maker", "checker", "compliance_officer", "auditor", "tenant_admin")
AUDIT_ROLES = ("compliance_officer", "auditor", "tenant_admin")
FULL_EXPORT_ROLES = {"compliance_officer", "auditor"}


def _run(db: Session, p: Principal, run_id: str) -> Run:
    r = db.get(Run, run_id)
    if r is None or r.tenant_id != p.tenant_id:
        raise NotFound("Run not found")
    return r


@router.get("/events")
def events(actor_id: str | None = None, action: str | None = None, entity_id: str | None = None, entity_type: str | None = None,
           engine: str | None = None, correlation_id: str | None = None, from_ts: datetime | None = None,
           to_ts: datetime | None = None, cursor: int | None = None, limit: int = 50, sequence: int | None = None,
           db: Session = Depends(get_db), p: Principal = Depends(require_roles(*READ_ROLES))):
    out = ops.search_events(db, p.tenant_id, roles=set(p.roles), actor_id=actor_id, action=action, entity_id=entity_id,
                            entity_type=entity_type, engine=engine, correlation_id=correlation_id, from_ts=from_ts,
                            to_ts=to_ts, cursor=cursor, limit=limit, sequence=sequence)
    ops.log_access(db, p.tenant_id, p.user_id, "SEARCH_EVENTS", {"filters": {k: v for k, v in dict(
        actor_id=actor_id, action=action, entity_id=entity_id, engine=engine, correlation_id=correlation_id).items() if v}})
    return out


@router.get("/runs/{run_id}/lineage")
def lineage(run_id: str, db: Session = Depends(get_db), p: Principal = Depends(require_roles(*READ_ROLES))):
    r = _run(db, p, run_id)
    ops.log_access(db, p.tenant_id, p.user_id, "VIEW_LINEAGE", {"run_id": run_id})
    return {"run_id": run_id, **build_lineage(db, p.tenant_id, r.correlation_id)}


@router.get("/entities/{entity_id}/lineage")
def entity_lineage(entity_id: str, db: Session = Depends(get_db), p: Principal = Depends(require_roles(*READ_ROLES))):
    """Deep link target /evidence?entity=<id>. Resolves an entity id (or a run id) to the decision graph(s) it belongs to.
    Tenant-scoped: an id that exists only in another tenant is indistinguishable from an unknown id."""
    from sqlalchemy import String, cast, or_
    from app.engines.evidence.models import EvEvent
    if len(entity_id) > 120:
        raise ValidationFailed("entity id too long", field_errors={"entity_id": "max 120 chars"})
    run = db.get(Run, entity_id)
    corrs: dict[str, dict] = {}
    if run is not None and run.tenant_id == p.tenant_id:
        corrs[run.correlation_id] = {"match": "RUN", "run_id": run.run_id}
    own = db.execute(select(EvEvent.correlation_id, EvEvent.sequence).where(EvEvent.tenant_id == p.tenant_id, EvEvent.entity_id == entity_id)
                     .order_by(EvEvent.sequence).limit(500)).all()
    for c, _seq in own:
        corrs.setdefault(c, {"match": "ENTITY"})
    # bounded fallback: entity recorded only as a parent inside another event's payload
    like = db.execute(select(EvEvent.correlation_id, EvEvent.payload).where(
        EvEvent.tenant_id == p.tenant_id, cast(EvEvent.payload, String).like(f"%{entity_id}%")).limit(500)).all()
    for c, payload in like:
        if any(isinstance(x, dict) and str(x.get("entity_id")) == entity_id for x in (payload or {}).get("parents", []) or []):
            corrs.setdefault(c, {"match": "PARENT"})
    ops.log_access(db, p.tenant_id, p.user_id, "RESOLVE_ENTITY", {"entity_id": entity_id, "correlations": len(corrs)})
    if not corrs:
        return {"entity_id": entity_id, "found": False, "decisions": [], "lineage": None,
                "message": "No evidence event in this tenant references this id (it may not exist, or evidence was never recorded)."}
    runs = {r.correlation_id: r.run_id for r in db.execute(select(Run).where(Run.tenant_id == p.tenant_id, Run.correlation_id.in_(list(corrs)))).scalars()}
    decisions = [{"correlation_id": c, "run_id": runs.get(c), **m} for c, m in corrs.items()]
    first = decisions[0]["correlation_id"]
    return {"entity_id": entity_id, "found": True, "decisions": decisions, "lineage": {"run_id": runs.get(first), **build_lineage(db, p.tenant_id, first)}}


class VerifyBody(BaseModel):
    anchor: str = Field(default="exported_checkpoints", pattern="^(exported_checkpoints|chain_only)$")
    mode: str = Field(default="full", pattern="^(full|incremental)$")  # incremental: only events after the newest trusted anchor


@router.post("/verify")
def verify(body: VerifyBody | None = None, db: Session = Depends(get_db), p: Principal = Depends(require_roles(*AUDIT_ROLES))):
    body = body or VerifyBody()
    from app.engines.evidence.service import verify_chain
    res = verify_against_exported(db, p.tenant_id, mode=body.mode) if body.anchor == "exported_checkpoints" else {
        **verify_chain(db, p.tenant_id), "anchor_status": "CHAIN_ONLY_NO_ANCHOR_TRUNCATION_NOT_DETECTABLE"}
    ops.log_access(db, p.tenant_id, p.user_id, "VERIFY_CHAIN", {"valid": res["valid"], "anchor": body.anchor})
    if not res["valid"]:
        emit(db, event_type="IntegrityFailure", tenant_id=p.tenant_id, correlation_id="integrity", aggregate_id=p.tenant_id,
             actor_id=p.user_id, actor_type="SERVICE", payload={"problems": res["problems"][:20]})
    return res


class ReplayBody(BaseModel):
    run_id: str


@router.post("/replay")
def replay(body: ReplayBody, db: Session = Depends(get_db), p: Principal = Depends(require_roles(*AUDIT_ROLES)),
           idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
    r = _run(db, p, body.run_id)
    out, _ = run_idempotent(db, p.tenant_id, idempotency_key, "evidence.replay", body.model_dump(),
                            lambda: replay_run(db, p.tenant_id, r.run_id, r.correlation_id, p.user_id))
    return out


class ExportBody(BaseModel):
    run_id: str
    redact: bool = False


@router.post("/export", status_code=201)
def export(body: ExportBody, db: Session = Depends(get_db), p: Principal = Depends(require_roles(*AUDIT_ROLES)),
           idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
    r = _run(db, p, body.run_id)
    # Export authorisation: only auditor/compliance officer may export content; admins always get the redacted form.
    redact = body.redact or not (set(p.roles) & FULL_EXPORT_ROLES)

    def make() -> dict:
        out = build_bundle(db, p.tenant_id, r.run_id, r.correlation_id, p.user_id, redact=redact)
        ops.log_access(db, p.tenant_id, p.user_id, "EXPORT", {"run_id": r.run_id, "redacted": redact})
        emit(db, event_type="EvidenceExported", tenant_id=p.tenant_id, correlation_id=r.correlation_id,
             aggregate_id=out["export_id"], actor_id=p.user_id, payload={"run_id": r.run_id, "redacted": redact})
        return out

    out, _ = run_idempotent(db, p.tenant_id, idempotency_key, "evidence.export", {"run_id": body.run_id, "redact": redact}, make)
    return out


def _export(db: Session, p: Principal, export_id: str) -> EvExport:
    e = db.get(EvExport, export_id)
    if e is None or e.tenant_id != p.tenant_id:
        raise NotFound("Export not found")
    return e


@router.get("/exports/{export_id}")
def export_detail(export_id: str, db: Session = Depends(get_db), p: Principal = Depends(require_roles(*AUDIT_ROLES))):
    e = _export(db, p, export_id)
    data = read_object(db, p.tenant_id, e.bundle_hash)
    z = zipfile.ZipFile(io.BytesIO(data))
    import json
    manifest = json.loads(z.read("manifest.json"))
    ops.log_access(db, p.tenant_id, p.user_id, "VIEW_EXPORT", {"export_id": export_id})
    return {"export_id": e.export_id, "run_id": e.run_id, "redacted": e.redacted, "events": e.event_count, "bundle_hash": e.bundle_hash,
            "created_at": e.created_at.isoformat(), "preview": {
                "files": manifest["files"], "missing_objects": manifest["missing_objects"], "chain": manifest["chain"],
                "redaction_note": manifest["redaction_note"], "scope": manifest["scope"], "limitations": manifest["limitations"]}}


@router.get("/exports")
def export_list(run_id: str | None = None, db: Session = Depends(get_db), p: Principal = Depends(require_roles(*AUDIT_ROLES))):
    q = select(EvExport).where(EvExport.tenant_id == p.tenant_id)
    if run_id:
        q = q.where(EvExport.run_id == run_id)
    rows = db.execute(q.order_by(EvExport.created_at.desc()).limit(100)).scalars().all()
    return {"items": [{"export_id": e.export_id, "run_id": e.run_id, "redacted": e.redacted, "events": e.event_count,
                       "created_at": e.created_at.isoformat(), "bundle_hash": e.bundle_hash} for e in rows]}


@router.get("/exports/{export_id}/download")
def export_download(export_id: str, db: Session = Depends(get_db), p: Principal = Depends(require_roles(*AUDIT_ROLES))):
    e = _export(db, p, export_id)
    data = read_object(db, p.tenant_id, e.bundle_hash)
    ops.log_access(db, p.tenant_id, p.user_id, "DOWNLOAD_EXPORT", {"export_id": export_id})
    return Response(data, media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="aether-evidence-{export_id[:8]}.zip"'})


class TamperBody(BaseModel):
    export_id: str
    mode: str


@router.post("/tamper-demo")
def tamper(body: TamperBody, db: Session = Depends(get_db), p: Principal = Depends(require_roles(*AUDIT_ROLES))):
    if body.mode not in MODES:
        raise ValidationFailed("unknown mode", field_errors={"mode": f"one of {', '.join(MODES)}"})
    return tamper_demo(db, p.tenant_id, body.export_id, body.mode)


@router.get("/checkpoints")
def checkpoints(db: Session = Depends(get_db), p: Principal = Depends(require_roles(*AUDIT_ROLES))):
    return {"items": list_checkpoints(db, p.tenant_id),
            "exported": [{k: c[k] for k in ("sequence", "head_hash", "key_id", "created_at", "signature_valid")}
                         for c in exported_checkpoints(p.tenant_id)][:20]}


class CheckpointBody(BaseModel):
    mode: str = Field(default="full", pattern="^(full|incremental)$")


@router.post("/checkpoints", status_code=201)
def make_checkpoint(body: CheckpointBody | None = None, db: Session = Depends(get_db), p: Principal = Depends(require_roles(*AUDIT_ROLES)),
                    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
    mode = (body or CheckpointBody()).mode
    out, _ = run_idempotent(db, p.tenant_id, idempotency_key, "evidence.checkpoint", {"mode": mode}, lambda: {
        k: v for k, v in create_checkpoint(db, p.tenant_id, mode=mode).items() if k != "exported_path"})
    return out


@router.get("/public-keys")
def public_keys(p: Principal = Depends(require_roles(*READ_ROLES))):
    return {"items": [{k: v for k, v in k_.items()} for k_ in keys.public_keys()],
            "warning": "Local file key, not an HSM. Distribute the public key to verifiers out-of-band."}


class RotateBody(BaseModel):
    reason: str = Field(min_length=5, max_length=500)


@router.post("/keys/rotate", status_code=201)
def rotate_key(body: RotateBody, db: Session = Depends(get_db), p: Principal = Depends(require_roles("tenant_admin")),
               idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
    """Audited signing-key rotation (tenant_admin). The key store is deployment-wide; see ops.rotate_signing_key."""
    out, _ = run_idempotent(db, p.tenant_id, idempotency_key, "evidence.rotate_key", body.model_dump(),
                            lambda: ops.rotate_signing_key(db, p.tenant_id, p.user_id, body.reason))
    return out


@router.get("/integrity")
def integrity(db: Session = Depends(get_db), p: Principal = Depends(require_roles(*AUDIT_ROLES))):
    return ops.integrity_dashboard(db, p.tenant_id)


@router.get("/retention")
def retention(db: Session = Depends(get_db), p: Principal = Depends(require_roles(*AUDIT_ROLES))):
    return ops.get_retention(db, p.tenant_id)


class RetentionBody(BaseModel):
    retention_days: int | None = None
    legal_hold: bool = False


@router.put("/retention/{category}")
def put_retention(category: str, body: RetentionBody, db: Session = Depends(get_db), p: Principal = Depends(require_roles("tenant_admin"))):
    return ops.set_retention(db, p.tenant_id, category, body.retention_days, body.legal_hold, p.user_id)


@router.get("/access-log")
def access_log(limit: int = Query(50, le=200), db: Session = Depends(get_db), p: Principal = Depends(require_roles(*AUDIT_ROLES))):
    from app.engines.evidence.models import EvAccessLog
    rows = db.execute(select(EvAccessLog).where(EvAccessLog.tenant_id == p.tenant_id).order_by(EvAccessLog.id.desc()).limit(limit)).scalars().all()
    return {"items": [{"actor_id": r.actor_id, "action": r.action, "detail": r.detail, "at": r.at.isoformat()} for r in rows]}
