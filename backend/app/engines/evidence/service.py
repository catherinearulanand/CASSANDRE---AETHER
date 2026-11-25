"""A6 evidence append interface (lead-supplied foundation; A6 extends).

Contract (frozen v1):
    append(session, tenant_id, *, engine, action, actor_id, actor_type, entity_type, entity_id, ...) -> EvEvent
The caller passes ITS OWN open transaction `session`: the evidence row and the domain change commit or
roll back together (atomic). There is deliberately no public HTTP append endpoint.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select

from app.core import services
from app.core.canonical import domain_hash
from app.core.errors import Conflict, Unavailable
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


_STORAGE_OUTAGE = False


def set_storage_outage(flag: bool) -> None:
    """FAULT INJECTION for tests/runbook drills only (not reachable over HTTP). While set, append() refuses, so the
    caller's transaction (which includes the domain change) fails: no material state transition without evidence (G24)."""
    global _STORAGE_OUTAGE
    _STORAGE_OUTAGE = bool(flag)


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
    if _STORAGE_OUTAGE:
        raise Unavailable("Evidence service unavailable: material action blocked until evidence can be recorded",
                          code="EVIDENCE_UNAVAILABLE")
    now = utcnow()
    if session.get(EvChainHead, tenant_id) is None:
        # Atomic create-if-absent: two concurrent first appends for a new tenant must not both INSERT
        # (PostgreSQL raised a unique violation here; SQLite's serialised writer had masked it).
        if session.get_bind().dialect.name == "postgresql":
            from sqlalchemy.dialects.postgresql import insert as pg_insert
            session.execute(pg_insert(EvChainHead).values(tenant_id=tenant_id, sequence=0, head_hash=GENESIS, updated_at=now)
                            .on_conflict_do_nothing(index_elements=["tenant_id"]))
        else:
            session.add(EvChainHead(tenant_id=tenant_id))
            session.flush()
    # Lock the per-tenant chain head so concurrent appends serialise (no forks, no duplicate sequence).
    # populate_existing: after waiting on the lock we must see the committed head, not a stale cached row.
    head = session.execute(lock_for_update(select(EvChainHead).where(EvChainHead.tenant_id == tenant_id))
                           .execution_options(populate_existing=True)).scalar_one()
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


def verify_chain(session, tenant_id: str, checkpoint: dict | None = None, checkpoints: list[dict] | None = None,
                 start_after: dict | None = None, chunk: int = 5000) -> dict:
    """Recompute event hashes in ONE streaming pass (bounded memory), checking links, sequence, head and trusted anchors.

    mode FULL (default): every event from genesis.
    mode INCREMENTAL (`start_after={"sequence": S, "head_hash": H}`, a TRUSTED signed anchor): the event at S must exist with
    event_hash == H, then only events with sequence > S are recomputed. TRUST ASSUMPTION (reported in the result): events
    <= S are NOT re-hashed in this pass, so row-content modification of events <= S since the verification that produced the
    anchor is detected only by a FULL pass. Anchors are always point-checked (truncation/rewrite below any anchor is caught).
    A bare hash chain cannot detect privileged tail truncation; only trusted anchors can."""
    base_seq, base_hash = (int(start_after["sequence"]), start_after["head_hash"]) if start_after else (0, GENESIS)
    anchors = ([checkpoint] if checkpoint is not None else []) + list(checkpoints or [])
    want = {int(cp["sequence"]): cp["head_hash"] for cp in anchors if int(cp["sequence"]) > 0}
    anchor_seen: dict[int, str] = {}
    problems: list[dict] = []
    prev, expected_seq = base_hash, base_seq + 1
    examined, last_seq, last_hash = 0, base_seq if start_after else 0, base_hash if start_after else GENESIS
    # CONSISTENT SNAPSHOT under concurrent appends: take the committed head FIRST and verify only events up to it.
    # (An append commits its event and the head together, so every event <= head.sequence is already visible.)
    # Verifying "all rows" then comparing with a LATER head read produced false HEAD_MISMATCH failures on PostgreSQL.
    head0 = session.execute(select(EvChainHead).where(EvChainHead.tenant_id == tenant_id)
                            .execution_options(populate_existing=True)).scalar_one_or_none()
    upper = head0.sequence if head0 is not None else None
    q = select(EvEvent).where(EvEvent.tenant_id == tenant_id)
    if upper is not None:
        q = q.where(EvEvent.sequence <= upper)
    if start_after:
        q = q.where(EvEvent.sequence >= base_seq)
    stream = session.execute(q.order_by(EvEvent.sequence).execution_options(yield_per=chunk))
    first = True
    for ev in stream.scalars():
        if start_after and first:
            first = False
            if ev.sequence != base_seq or ev.event_hash != base_hash:
                problems.append({"problem": "INCREMENTAL_BASE_NOT_REPRODUCED", "anchor_sequence": base_seq,
                                 "detail": "the trusted anchor used as the incremental base is not reproduced by the stored chain"})
                # fall through: still examine what follows so the report is useful
                prev, expected_seq = ev.event_hash if ev.sequence == base_seq else base_hash, base_seq + 1
            if ev.sequence in want:
                anchor_seen[ev.sequence] = ev.event_hash
            if ev.sequence == base_seq:
                last_seq, last_hash = ev.sequence, ev.event_hash
                continue
        examined += 1
        if ev.sequence != expected_seq:
            problems.append({"sequence": ev.sequence, "problem": "SEQUENCE_GAP_OR_REORDER", "expected": expected_seq})
            expected_seq = ev.sequence
        if ev.prev_hash != prev:
            problems.append({"sequence": ev.sequence, "problem": "PREV_HASH_MISMATCH"})
        if compute_event_hash(tenant_id, ev.sequence, ev.prev_hash, _fields_of(ev)) != ev.event_hash:
            problems.append({"sequence": ev.sequence, "problem": "EVENT_HASH_MISMATCH"})
        if ev.sequence in want:
            anchor_seen[ev.sequence] = ev.event_hash
        prev, expected_seq = ev.event_hash, expected_seq + 1
        last_seq, last_hash = ev.sequence, ev.event_hash
        session.expunge(ev)  # keep the identity map small on very long chains
    if start_after and first:  # no rows at all >= base
        problems.append({"problem": "INCREMENTAL_BASE_NOT_REPRODUCED", "anchor_sequence": base_seq,
                         "detail": "no events at or after the trusted anchor (chain truncated below it)"})
    if head0 is not None:
        if head0.sequence != last_seq or head0.head_hash != last_hash:
            problems.append({"problem": "HEAD_MISMATCH", "detail": "stored chain head differs from last event"})
    # events recorded BEYOND the (re-read) head are a rewrite/forgery, not a concurrent append (appends move the head atomically)
    # ORDER MATTERS: read the max event sequence FIRST, then the head. An append commits event+head together, so a head read
    # AFTER the max read is always >= it for legitimate appends (the reverse order raced into a false mismatch on PostgreSQL).
    max_seq = session.execute(select(func.max(EvEvent.sequence)).where(EvEvent.tenant_id == tenant_id)).scalar_one()
    head1 = session.execute(select(EvChainHead).where(EvChainHead.tenant_id == tenant_id)
                            .execution_options(populate_existing=True)).scalar_one_or_none()
    if max_seq is not None and max_seq > (head1.sequence if head1 is not None else 0):
        problems.append({"problem": "HEAD_MISMATCH", "detail": "events exist beyond the stored chain head"})
    # Anchors not met in the streamed range (those strictly below an incremental base) are point-checked.
    for seq, hh in want.items():
        if seq in anchor_seen:
            got = anchor_seen[seq]
        else:
            row = session.execute(select(EvEvent.event_hash).where(EvEvent.tenant_id == tenant_id, EvEvent.sequence == seq)).first()
            got = row[0] if row else None
        if got != hh:
            problems.append({"problem": "CHECKPOINT_NOT_REPRODUCED", "anchor_sequence": seq,
                             "detail": f"trusted checkpoint at seq {seq} not found/mismatched (truncation or rewrite)"})
    total = last_seq
    out = {"tenant_id": tenant_id, "events": (examined if not start_after else total), "valid": not problems, "problems": problems,
           "head_sequence": last_seq, "head_hash": last_hash, "checkpoint_checked": bool(anchors),
           "mode": "INCREMENTAL" if start_after else "FULL", "events_recomputed": examined}
    if start_after:
        out.update(verified_from_sequence=base_seq,
                   trust_assumption=f"events 1..{base_seq} were NOT re-hashed in this pass (trusted from the anchor's earlier verification); run a FULL verification to re-check them")
    return out


def check_expected_actions(session, tenant_id: str, correlation_id: str, expected: dict[str, list[str]]) -> dict:
    """E-A01 helper for the lead: given {engine: [actions]} returns the (engine, action) pairs missing for a correlation."""
    rows = session.execute(select(EvEvent.engine, EvEvent.action).where(
        EvEvent.tenant_id == tenant_id, EvEvent.correlation_id == correlation_id)).all()
    have = {(e, a) for e, a in rows}
    missing = [{"engine": e, "action": a} for e, acts in expected.items() for a in acts if (e, a) not in have]
    return {"complete": not missing, "missing": missing, "events_seen": len(rows)}


services.register("evidence.append", append)
services.register("evidence.check_expected_actions", check_expected_actions)
services.register("evidence.verify_chain", verify_chain)
