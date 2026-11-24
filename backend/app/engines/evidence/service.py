"""A6 evidence append interface (lead-supplied foundation; A6 extends).

Contract (frozen v1):
    append(session, tenant_id, *, engine, action, actor_id, actor_type, entity_type, entity_id, ...) -> EvEvent
The caller passes ITS OWN open transaction `session`: the evidence row and the domain change commit or
roll back together (atomic). There is deliberately no public HTTP append endpoint.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import select

from app.core import services
from app.core.canonical import domain_hash
from app.core.errors import Conflict
from app.db.base import lock_for_update, new_id, utcnow
from app.engines.evidence.models import EvChainHead, EvEvent

GENESIS = "0" * 64
HASH_DOMAIN = "aether.evidence.event.v1"
SERVICE_VERSION = "aether-evidence/1.0"

_HASHED_FIELDS = (
    "event_id", "engine", "action", "actor_id", "actor_type", "actor_role", "service_version",
    "correlation_id", "causation_id", "entity_type", "entity_id", "entity_revision",
    "effective_at", "recorded_at", "source_mode", "input_hashes", "source_spans",
    "output_hash", "rationale", "payload",
)


def compute_event_hash(tenant_id: str, sequence: int, prev_hash: str, fields: dict) -> str:
    rec = {"chain_id": tenant_id, "sequence": sequence, "prev_hash": prev_hash}
    rec.update({k: fields[k] for k in _HASHED_FIELDS})
    return domain_hash(HASH_DOMAIN, rec)


def _fields_of(ev: EvEvent) -> dict:
    return {k: getattr(ev, k) for k in _HASHED_FIELDS}


def append(
    session,
    tenant_id: str,
    *,
    engine: str,
    action: str,
    actor_id: str,
    actor_type: str,
    entity_type: str,
    entity_id: str,
    correlation_id: str,
    entity_revision: int = 1,
    actor_role: str | None = None,
    causation_id: str | None = None,
    effective_at: datetime | None = None,
    source_mode: str = "REPLAY",
    input_hashes: list[str] | None = None,
    source_spans: list[dict] | None = None,
    output_hash: str | None = None,
    rationale: str | None = None,
    payload: dict | None = None,
    event_id: str | None = None,
    service_version: str = SERVICE_VERSION,
) -> EvEvent:
    now = utcnow()
    head = session.get(EvChainHead, tenant_id)
    if head is None:
        session.add(EvChainHead(tenant_id=tenant_id))
        session.flush()
    # Lock the per-tenant chain head so concurrent appends serialise (no forks, no duplicate sequence).
    head = session.execute(lock_for_update(select(EvChainHead).where(EvChainHead.tenant_id == tenant_id))).scalar_one()
    eid = event_id or new_id()
    if session.execute(select(EvEvent.sequence).where(EvEvent.tenant_id == tenant_id, EvEvent.event_id == eid)).first():
        raise Conflict(f"duplicate evidence event_id {eid}")
    seq = head.sequence + 1
    fields = dict(
        event_id=eid, engine=engine, action=action, actor_id=actor_id, actor_type=actor_type,
        actor_role=actor_role, service_version=service_version, correlation_id=correlation_id,
        causation_id=causation_id, entity_type=entity_type, entity_id=entity_id,
        entity_revision=entity_revision, effective_at=effective_at or now, recorded_at=now,
        source_mode=source_mode, input_hashes=sorted(input_hashes or []), source_spans=source_spans or [],
        output_hash=output_hash, rationale=rationale, payload=payload or {},
    )
    h = compute_event_hash(tenant_id, seq, head.head_hash, fields)  # also validates canonical types (no floats)
    ev = EvEvent(tenant_id=tenant_id, sequence=seq, prev_hash=head.head_hash, event_hash=h, **fields)
    session.add(ev)
    head.sequence, head.head_hash, head.updated_at = seq, h, now
    session.flush()
    return ev


def verify_chain(session, tenant_id: str, checkpoint: dict | None = None) -> dict:
    """Recompute every hash. If a trusted checkpoint {sequence, head_hash} is supplied, also detect
    truncation / tail rewrite (a bare hash chain cannot detect a privileged truncation by itself)."""
    rows = session.execute(select(EvEvent).where(EvEvent.tenant_id == tenant_id).order_by(EvEvent.sequence)).scalars().all()
    problems: list[dict] = []
    prev, expected_seq = GENESIS, 1
    for ev in rows:
        if ev.sequence != expected_seq:
            problems.append({"sequence": ev.sequence, "problem": "SEQUENCE_GAP_OR_REORDER", "expected": expected_seq})
            expected_seq = ev.sequence
        if ev.prev_hash != prev:
            problems.append({"sequence": ev.sequence, "problem": "PREV_HASH_MISMATCH"})
        if compute_event_hash(tenant_id, ev.sequence, ev.prev_hash, _fields_of(ev)) != ev.event_hash:
            problems.append({"sequence": ev.sequence, "problem": "EVENT_HASH_MISMATCH"})
        prev, expected_seq = ev.event_hash, expected_seq + 1
    head = session.get(EvChainHead, tenant_id)
    if head is not None and (head.sequence != (rows[-1].sequence if rows else 0) or head.head_hash != (rows[-1].event_hash if rows else GENESIS)):
        problems.append({"problem": "HEAD_MISMATCH", "detail": "stored chain head differs from last event"})
    if checkpoint is not None:
        seq, hh = int(checkpoint["sequence"]), checkpoint["head_hash"]
        match = next((e for e in rows if e.sequence == seq), None)
        if seq > 0 and (match is None or match.event_hash != hh):
            problems.append({"problem": "CHECKPOINT_NOT_REPRODUCED", "detail": f"trusted checkpoint at seq {seq} not found/mismatched (truncation or rewrite)"})
    return {"tenant_id": tenant_id, "events": len(rows), "valid": not problems, "problems": problems,
            "head_sequence": rows[-1].sequence if rows else 0, "head_hash": rows[-1].event_hash if rows else GENESIS,
            "checkpoint_checked": checkpoint is not None}


services.register("evidence.append", append)
services.register("evidence.verify_chain", verify_chain)
