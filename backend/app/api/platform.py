"""Common endpoints: /session, /scenarios, /runs, /runs/{id}, /runs/{id}/events, /health/*, /capabilities."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, Header, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.auth.security import COOKIE, Principal, current_principal, get_db, login, logout, require_roles
from app.core import services
from app.core.config import REPO_ROOT, get_settings
from app.core.errors import NotFound, ValidationFailed
from app.core.idempotency import run_idempotent
from app.db.base import new_id, utcnow
from app.db.models_core import Run
from app.engines import ENGINE_KEYS, ENGINE_NAMES
from app.engines.evidence.models import EvEvent
from app.orchestration.runs import transition

router = APIRouter()


class LoginBody(BaseModel):
    tenant_id: str = Field(min_length=1, max_length=64)  # tenant *selection at login only*; afterwards from session
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=200)


def _principal_out(p: Principal) -> dict:
    s = get_settings()
    return {"user_id": p.user_id, "tenant_id": p.tenant_id, "username": p.username, "display_name": p.display_name,
            "roles": list(p.roles), "entitlements": list(p.entitlements), "csrf_token": p.csrf_token,
            "data_mode": s.data_mode, "source_mode": s.source_mode}


@router.post("/session/login", tags=["session"])
def session_login(body: LoginBody, request: Request, response: Response, db: Session = Depends(get_db)):
    ip = request.client.host if request.client else "unknown"
    token, p = login(db, body.tenant_id, body.username, body.password, ip)
    st = get_settings()
    response.set_cookie(COOKIE, token, httponly=True, samesite="lax", secure=st.cookie_secure,
                        max_age=st.session_ttl_seconds, path="/")
    return _principal_out(p)


@router.post("/session/logout", tags=["session"])
def session_logout(request: Request, response: Response, p: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    tok = request.cookies.get(COOKIE)
    if tok:
        logout(db, tok)
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}


@router.get("/session", tags=["session"])
def session_get(p: Principal = Depends(current_principal)):
    return _principal_out(p)


# ---------------------------------------------------------------- health / capabilities
@router.get("/health/live", tags=["health"])
def live():
    return {"status": "live"}  # no expensive work


@router.get("/health/ready", tags=["health"])
def ready(db: Session = Depends(get_db)):
    checks = {}
    try:
        db.execute(text("SELECT 1"))
        checks["database"] = "ok"
        from app.engines.evidence.models import EvChainHead
        db.execute(select(EvChainHead).limit(1)).all()
        checks["evidence_store"] = "ok"
    except Exception as exc:  # noqa: BLE001
        checks.setdefault("database", f"error: {type(exc).__name__}")
        checks.setdefault("evidence_store", "error")
    ok = all(v == "ok" for v in checks.values())
    if not ok:
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=503, content={"status": "not_ready", "checks": checks})
    return {"status": "ready", "checks": checks}


@router.get("/capabilities", tags=["platform"])
def capabilities(request: Request, p: Principal = Depends(current_principal)):
    """Honest capability report: which engines are mounted and which services are registered."""
    mounted = getattr(request.app.state, "engines_mounted", {})
    return {
        "data_mode": get_settings().data_mode,
        "source_mode": get_settings().source_mode,
        "engines": [{"key": k, "name": ENGINE_NAMES[k], "mounted": bool(mounted.get(k)),
                     "status": "AVAILABLE" if mounted.get(k) else "NOT_IMPLEMENTED"} for k in ENGINE_KEYS],
        "services": services.registered(),
        "external_transmission": "DISABLED",  # no trading / settlement / regulatory-report transmission paths
    }


# ---------------------------------------------------------------- scenarios & runs
def _scenarios() -> list[dict]:
    d = REPO_ROOT / "fixtures" / "scenarios"
    out = []
    if d.exists():
        for f in sorted(d.glob("*.json")):
            out.append(json.loads(f.read_text(encoding="utf-8")))
    return out


@router.get("/scenarios", tags=["scenarios"])
def list_scenarios(p: Principal = Depends(current_principal)):
    return {"items": _scenarios()}


class RunCreate(BaseModel):
    scenario_id: str
    parent_run_id: str | None = None


def _run_out(r: Run) -> dict:
    return {"run_id": r.run_id, "scenario_id": r.scenario_id, "scenario_revision": r.scenario_revision,
            "market": r.market, "mode": r.mode, "as_of": r.as_of.isoformat(), "seed": r.seed, "state": r.state,
            "block_reasons": r.block_reasons, "input_manifest": r.input_manifest, "output_manifest": r.output_manifest,
            "engine_versions": r.engine_versions, "correlation_id": r.correlation_id, "parent_run_id": r.parent_run_id,
            "revision": r.revision, "created_at": r.created_at.isoformat(), "updated_at": r.updated_at.isoformat()}


@router.post("/runs", status_code=201, tags=["runs"])
def create_run(body: RunCreate, db: Session = Depends(get_db),
               p: Principal = Depends(require_roles("maker", "compliance_officer", "tenant_admin")),
               idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
    sc = next((s for s in _scenarios() if s["scenario_id"] == body.scenario_id), None)
    if sc is None:
        raise ValidationFailed("Unknown scenario", field_errors={"scenario_id": "not found"})

    def make() -> dict:
        run = Run(run_id=new_id(), tenant_id=p.tenant_id, parent_run_id=body.parent_run_id, scenario_id=sc["scenario_id"],
                  scenario_revision=str(sc.get("revision", "1")), market=sc["market"], mode=get_settings().source_mode,
                  as_of=datetime.fromisoformat(sc["as_of"].replace("Z", "+00:00")), seed=int(sc.get("seed", 0)),
                  correlation_id=new_id(), created_by=p.user_id, input_manifest=sc.get("input_manifest", {}))
        db.add(run)
        db.flush()
        services.call("evidence.append", db, p.tenant_id, engine="platform", action="RUN_CREATED", actor_id=p.user_id,
                      actor_type="USER", actor_role=",".join(p.roles), entity_type="run", entity_id=run.run_id,
                      correlation_id=run.correlation_id, effective_at=run.as_of, source_mode=run.mode,
                      payload={"scenario_id": run.scenario_id, "market": run.market, "parent_run_id": body.parent_run_id})
        return _run_out(run)

    out, _ = run_idempotent(db, p.tenant_id, idempotency_key, "runs.create", body.model_dump(), make)
    return out


def _get_run(db: Session, p: Principal, run_id: str) -> Run:
    r = db.get(Run, run_id)
    if r is None or r.tenant_id != p.tenant_id:
        raise NotFound("Run not found")  # identical for 'absent' and 'other tenant'
    return r


@router.get("/runs", tags=["runs"])
def list_runs(limit: int = 50, cursor: str | None = None, state: str | None = None,
              db: Session = Depends(get_db), p: Principal = Depends(current_principal)):
    limit = max(1, min(limit, 200))
    q = select(Run).where(Run.tenant_id == p.tenant_id)
    if state:
        q = q.where(Run.state == state)
    if cursor:
        q = q.where(Run.created_at < datetime.fromisoformat(cursor))
    rows = db.execute(q.order_by(Run.created_at.desc(), Run.run_id).limit(limit + 1)).scalars().all()
    nxt = rows[limit - 1].created_at.isoformat() if len(rows) > limit else None
    return {"items": [_run_out(r) for r in rows[:limit]], "next_cursor": nxt}


@router.get("/runs/{run_id}", tags=["runs"])
def get_run(run_id: str, db: Session = Depends(get_db), p: Principal = Depends(current_principal)):
    return _run_out(_get_run(db, p, run_id))


@router.get("/runs/{run_id}/events", tags=["runs"])
def run_events(run_id: str, limit: int = 200, db: Session = Depends(get_db), p: Principal = Depends(current_principal)):
    r = _get_run(db, p, run_id)
    rows = db.execute(select(EvEvent).where(EvEvent.tenant_id == p.tenant_id, EvEvent.correlation_id == r.correlation_id)
                      .order_by(EvEvent.sequence).limit(max(1, min(limit, 1000)))).scalars().all()
    return {"items": [{"sequence": e.sequence, "event_id": e.event_id, "engine": e.engine, "action": e.action,
                       "actor_id": e.actor_id, "actor_type": e.actor_type, "entity_type": e.entity_type,
                       "entity_id": e.entity_id, "recorded_at": e.recorded_at.isoformat(),
                       "effective_at": e.effective_at.isoformat(), "event_hash": e.event_hash,
                       "prev_hash": e.prev_hash, "rationale": e.rationale} for e in rows]}


@router.post("/runs/{run_id}/cancel", tags=["runs"])
def cancel_run(run_id: str, db: Session = Depends(get_db),
               p: Principal = Depends(require_roles("maker", "compliance_officer", "tenant_admin"))):
    r = _get_run(db, p, run_id)
    transition(r, "CANCELLED")
    services.call("evidence.append", db, p.tenant_id, engine="platform", action="RUN_CANCELLED", actor_id=p.user_id,
                  actor_type="USER", entity_type="run", entity_id=r.run_id, entity_revision=r.revision,
                  correlation_id=r.correlation_id)
    return _run_out(r)
