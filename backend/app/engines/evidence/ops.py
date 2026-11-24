"""E05 search, access log, retention config, integrity dashboard data."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select

from app.core.errors import ValidationFailed
from app.db.base import utcnow
from app.db.models_core import OutboxEvent
from app.engines.evidence import keys
from app.engines.evidence.checkpoints import verify_against_exported
from app.engines.evidence.models import EvAccessLog, EvChainHead, EvCheckpoint, EvEvent, EvRetention
from app.engines.evidence.objects import storage_probe, verify_objects

FULL_DETAIL_ROLES = {"auditor", "compliance_officer", "tenant_admin"}
RETENTION_CATEGORIES = ["EVIDENCE_EVENTS", "SOURCE_DOCUMENTS", "CALCULATIONS", "EXPORT_BUNDLES", "TEMP_OBJECTS", "ACCESS_LOG"]
SORTABLE = {"sequence"}


def log_access(session, tenant_id: str, actor_id: str, action: str, detail: dict | None = None) -> None:
    session.add(EvAccessLog(tenant_id=tenant_id, actor_id=actor_id, action=action, detail=detail or {}))


def _out(e: EvEvent, full: bool) -> dict:
    d = {"sequence": e.sequence, "event_id": e.event_id, "engine": e.engine, "action": e.action, "actor_type": e.actor_type,
         "entity_type": e.entity_type, "entity_id": e.entity_id, "entity_revision": e.entity_revision,
         "correlation_id": e.correlation_id, "causation_id": e.causation_id, "recorded_at": e.recorded_at.isoformat(),
         "effective_at": e.effective_at.isoformat(), "source_mode": e.source_mode, "service_version": e.service_version,
         "event_hash": e.event_hash, "prev_hash": e.prev_hash, "detail_redacted": not full}
    if full:
        d.update(actor_id=e.actor_id, actor_role=e.actor_role, rationale=e.rationale, input_hashes=e.input_hashes,
                 output_hash=e.output_hash, source_spans=e.source_spans, payload=e.payload)
    return d


def search_events(session, tenant_id: str, *, roles: set[str], actor_id: str | None = None, action: str | None = None,
                  entity_id: str | None = None, entity_type: str | None = None, engine: str | None = None,
                  correlation_id: str | None = None, from_ts: datetime | None = None, to_ts: datetime | None = None,
                  cursor: int | None = None, limit: int = 50) -> dict:
    limit = max(1, min(limit, 200))
    q = select(EvEvent).where(EvEvent.tenant_id == tenant_id)
    for col, val in ((EvEvent.actor_id, actor_id), (EvEvent.action, action), (EvEvent.entity_id, entity_id),
                     (EvEvent.entity_type, entity_type), (EvEvent.engine, engine), (EvEvent.correlation_id, correlation_id)):
        if val:
            q = q.where(col == val)
    if from_ts:
        q = q.where(EvEvent.recorded_at >= from_ts)
    if to_ts:
        q = q.where(EvEvent.recorded_at <= to_ts)
    if cursor:
        q = q.where(EvEvent.sequence < cursor)
    rows = session.execute(q.order_by(EvEvent.sequence.desc()).limit(limit + 1)).scalars().all()
    full = bool(roles & FULL_DETAIL_ROLES)
    return {"items": [_out(e, full) for e in rows[:limit]], "next_cursor": rows[limit - 1].sequence if len(rows) > limit else None,
            "detail_level": "FULL" if full else "METADATA_ONLY"}


def get_retention(session, tenant_id: str) -> dict:
    rows = {r.category: r for r in session.execute(select(EvRetention).where(EvRetention.tenant_id == tenant_id)).scalars()}
    items = [{"category": c, "retention_days": rows[c].retention_days if c in rows else None,
              "legal_hold": rows[c].legal_hold if c in rows else False, "configured": c in rows} for c in RETENTION_CATEGORIES]
    return {"items": items, "note": ("No statutory retention period is assumed. Unconfigured categories have no period. "
                                     "Protected audit material (EVIDENCE_EVENTS, signed anchors) is never deleted through this "
                                     "application; deletion needs an approved institutional policy and archived anchors.")}


def set_retention(session, tenant_id: str, category: str, retention_days: int | None, legal_hold: bool, actor_id: str) -> dict:
    if category not in RETENTION_CATEGORIES:
        raise ValidationFailed("unknown retention category", field_errors={"category": "unsupported"})
    if retention_days is not None and retention_days < 1:
        raise ValidationFailed("retention_days must be >= 1 or null", field_errors={"retention_days": "invalid"})
    r = session.get(EvRetention, (tenant_id, category))
    if r is None:
        r = EvRetention(tenant_id=tenant_id, category=category)
        session.add(r)
    r.retention_days, r.legal_hold, r.updated_by, r.updated_at = retention_days, legal_hold, actor_id, utcnow()
    session.flush()
    return {"category": category, "retention_days": retention_days, "legal_hold": legal_hold}


def integrity_dashboard(session, tenant_id: str) -> dict:
    chain = verify_against_exported(session, tenant_id)
    objs = verify_objects(session, tenant_id)
    store = storage_probe()
    head = session.get(EvChainHead, tenant_id)
    last_cp = session.execute(select(EvCheckpoint).where(EvCheckpoint.tenant_id == tenant_id)
                              .order_by(EvCheckpoint.sequence.desc(), EvCheckpoint.created_at.desc()).limit(1)).scalar_one_or_none()
    head_seq = head.sequence if head else 0
    pend = dict(session.execute(select(OutboxEvent.status, func.count()).where(OutboxEvent.tenant_id == tenant_id)
                                .group_by(OutboxEvent.status)).all())
    problems = []
    if not chain["valid"]:
        problems.append("CHAIN_INVALID")
    if chain.get("anchor_status") == "NO_TRUSTED_ANCHOR":
        problems.append("NO_TRUSTED_ANCHOR")
    if not objs["valid"]:
        problems.append("OBJECT_STORE_INVALID")
    if not store["writable"]:
        problems.append("STORAGE_NOT_WRITABLE")
    if pend.get("DEAD", 0):
        problems.append("OUTBOX_DEAD_LETTERS")
    status = "FAILED" if {"CHAIN_INVALID", "OBJECT_STORE_INVALID", "STORAGE_NOT_WRITABLE"} & set(problems) else ("DEGRADED" if problems else "OK")
    return {"status": status, "problems": problems, "chain": chain, "objects": objs, "storage": store,
            "head_sequence": head_seq, "events_since_last_checkpoint": head_seq - (last_cp.sequence if last_cp else 0),
            "last_checkpoint": {"sequence": last_cp.sequence, "created_at": last_cp.created_at.isoformat(), "key_id": last_cp.key_id} if last_cp else None,
            "outbox": {"pending": pend.get("PENDING", 0), "dead": pend.get("DEAD", 0), "delivered": pend.get("DELIVERED", 0)},
            "signing": {"current_key_id": keys.current_key_id(), "protection": "LOCAL_FILE_KEY_NOT_HSM"}}
