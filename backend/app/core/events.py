"""Event envelope + transactional outbox + idempotent inbox consumers (at-least-once, bounded backoff, DLQ).

Not exactly-once: effects are idempotent via core_inbox unique (consumer,event_id).
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta

from pydantic import BaseModel
from sqlalchemy import select

from app.core.canonical import domain_hash
from app.db.base import new_id, utcnow
from app.db.models_core import InboxRecord, OutboxEvent

EVENT_TYPES = frozenset({
    "DocumentIngested", "RuleProposed", "RuleApproved", "RuleActivated", "RuleSuperseded",
    "TermsApproved", "TermsFrozen", "SnapshotPinned", "MarginComputed", "MarginReconciled",
    "AllocationProposed", "ControlEvaluated", "CaseOpened", "CaseUpdated", "CaseResolved",
    "ApprovalRecorded", "ReservationCreated", "InstructionDrafted", "EvidenceExported",
    "ReplayCompleted", "IntegrityFailure",
})
# Single owner per event type (spec 4.3 requires ownership + schema contract test).
EVENT_OWNER = {
    "DocumentIngested": "regulatory", "RuleProposed": "regulatory", "RuleApproved": "regulatory",
    "RuleActivated": "regulatory", "RuleSuperseded": "regulatory",
    "TermsApproved": "contracts", "TermsFrozen": "contracts",
    "SnapshotPinned": "risk", "MarginComputed": "risk", "MarginReconciled": "risk",
    "AllocationProposed": "collateral", "ReservationCreated": "collateral", "InstructionDrafted": "collateral",
    "ControlEvaluated": "compliance", "CaseOpened": "compliance", "CaseUpdated": "compliance", "CaseResolved": "compliance",
    "ApprovalRecorded": "platform", "EvidenceExported": "evidence", "ReplayCompleted": "evidence", "IntegrityFailure": "evidence",
}
MAX_ATTEMPTS = 6
BASE_BACKOFF_S = 2


class EventEnvelope(BaseModel):
    event_id: str
    event_type: str
    schema_version: str = "1"
    tenant_id: str
    correlation_id: str
    causation_id: str | None = None
    aggregate_id: str
    aggregate_revision: int = 1
    occurred_at: datetime
    effective_at: datetime
    actor_id: str
    actor_type: str
    idempotency_key: str | None = None
    source_mode: str = "REPLAY"
    payload_ref: str | None = None
    payload_hash: str


def emit(session, *, event_type: str, tenant_id: str, correlation_id: str, aggregate_id: str, actor_id: str,
         actor_type: str = "USER", payload: dict | None = None, aggregate_revision: int = 1,
         causation_id: str | None = None, effective_at: datetime | None = None, source_mode: str = "REPLAY",
         idempotency_key: str | None = None) -> EventEnvelope:
    """Write an outbox row in the CALLER'S transaction."""
    if event_type not in EVENT_TYPES:
        raise ValueError(f"unknown event type {event_type}")
    now = utcnow()
    env = EventEnvelope(
        event_id=new_id(), event_type=event_type, tenant_id=tenant_id, correlation_id=correlation_id,
        causation_id=causation_id, aggregate_id=aggregate_id, aggregate_revision=aggregate_revision,
        occurred_at=now, effective_at=effective_at or now, actor_id=actor_id, actor_type=actor_type,
        idempotency_key=idempotency_key, source_mode=source_mode,
        payload_hash=domain_hash("aether.payload.v1", payload or {}),
    )
    body = env.model_dump(mode="json")
    body["payload"] = payload or {}
    session.add(OutboxEvent(event_id=env.event_id, tenant_id=tenant_id, event_type=event_type, envelope=body))
    return env


_HANDLERS: dict[str, list[tuple[str, Callable]]] = {}


def subscribe(event_type: str, consumer: str, fn: Callable) -> None:
    """fn(session, envelope_dict). Must be idempotent; inbox guards redelivery."""
    _HANDLERS.setdefault(event_type, []).append((consumer, fn))


def dispatch_once(session, limit: int = 100) -> dict:
    """Deliver due outbox rows. Out-of-order handling is the consumer's job via aggregate_revision;
    a consumer that lacks a predecessor should raise to defer (retried with backoff, then DEAD)."""
    now = utcnow()
    rows = session.execute(
        select(OutboxEvent).where(OutboxEvent.status == "PENDING", OutboxEvent.next_attempt_at <= now)
        .order_by(OutboxEvent.created_at, OutboxEvent.event_id).limit(limit)
    ).scalars().all()
    delivered = failed = dead = 0
    for row in rows:
        try:
            for consumer, fn in _HANDLERS.get(row.event_type, []):
                if session.get(InboxRecord, (consumer, row.event_id)):
                    continue
                with session.begin_nested():
                    fn(session, row.envelope)
                    session.add(InboxRecord(consumer=consumer, event_id=row.event_id))
            row.status = "DELIVERED"
            delivered += 1
        except Exception as exc:  # noqa: BLE001 - recorded, never silent
            row.attempts += 1
            row.last_error = f"{type(exc).__name__}: {exc}"[:1000]
            if row.attempts >= MAX_ATTEMPTS:
                row.status = "DEAD"
                dead += 1
            else:
                row.next_attempt_at = now + timedelta(seconds=BASE_BACKOFF_S * 2 ** row.attempts)
                failed += 1
    session.flush()
    return {"delivered": delivered, "retrying": failed, "dead": dead}


def requeue_dead(session, tenant_id: str, event_id: str) -> bool:
    row = session.get(OutboxEvent, event_id)
    if row is None or row.tenant_id != tenant_id or row.status != "DEAD":
        return False
    row.status, row.attempts, row.next_attempt_at = "PENDING", 0, utcnow()
    return True
