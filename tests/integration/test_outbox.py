"""Transactional outbox + idempotent inbox: at-least-once delivery, idempotent effects, bounded backoff, DLQ and recovery."""
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select, update

from app.core import events
from app.core.events import MAX_ATTEMPTS, dispatch_once, emit, requeue_dead, subscribe
from app.db.base import SessionLocal, utcnow
from app.db.models_core import InboxRecord, OutboxEvent


def new_tenant():
    return "ob-" + uuid.uuid4().hex[:8]


def emit_one(s, tenant, etype="MarginComputed"):
    return emit(s, event_type=etype, tenant_id=tenant, correlation_id="c1", aggregate_id="agg1", actor_id="u", payload={"x": "1"})


@pytest.fixture(autouse=True)
def clean_handlers():
    saved = {k: list(v) for k, v in events._HANDLERS.items()}
    events._HANDLERS.clear()
    # Other engines' tests leave PENDING rows in the shared test DB; dispatch_once is oldest-first with a limit,
    # so drain unrelated rows to keep these tests about the outbox itself (test isolation, not product behaviour).
    with SessionLocal() as s:
        s.execute(update(OutboxEvent).where(OutboxEvent.status == "PENDING").values(status="DELIVERED"))
        s.commit()
    yield
    events._HANDLERS.clear()
    events._HANDLERS.update(saved)


def test_rejects_unknown_event_type_and_owner_catalogue_is_complete():
    with SessionLocal() as s, pytest.raises(ValueError):
        emit(s, event_type="Bogus", tenant_id="t", correlation_id="c", aggregate_id="a", actor_id="u")
    assert set(events.EVENT_OWNER) == set(events.EVENT_TYPES)


def test_outbox_row_is_part_of_callers_transaction():
    t = new_tenant()
    with SessionLocal() as s:
        emit_one(s, t)
        s.rollback()  # domain transaction failed => the event must vanish with it
    with SessionLocal() as s:
        assert s.execute(select(OutboxEvent).where(OutboxEvent.tenant_id == t)).first() is None


def test_delivery_is_idempotent_per_consumer_on_redelivery():
    t, calls = new_tenant(), []
    subscribe("MarginComputed", "c-a", lambda s, env: calls.append(("a", env["event_id"])))
    subscribe("MarginComputed", "c-b", lambda s, env: calls.append(("b", env["event_id"])))
    with SessionLocal() as s:
        env = emit_one(s, t)
        s.commit()
        assert dispatch_once(s)["delivered"] >= 1
        s.commit()
        row = s.get(OutboxEvent, env.event_id)
        row.status = "PENDING"  # simulate at-least-once redelivery of an already-processed event
        s.commit()
        dispatch_once(s)
        s.commit()
    mine = [c for c in calls if c[1] == env.event_id]
    assert sorted(c[0] for c in mine) == ["a", "b"]  # each consumer ran exactly once despite redelivery
    with SessionLocal() as s:
        assert len(s.execute(select(InboxRecord).where(InboxRecord.event_id == env.event_id)).all()) == 2


def test_failure_retries_with_backoff_then_dead_letters_and_can_be_requeued():
    t, state = new_tenant(), {"fail": True}

    def flaky(s, env):
        if state["fail"]:
            raise RuntimeError("downstream unavailable")

    subscribe("MarginComputed", "flaky", flaky)
    with SessionLocal() as s:
        env = emit_one(s, t)
        s.commit()
        for attempt in range(1, MAX_ATTEMPTS + 1):
            s.get(OutboxEvent, env.event_id).next_attempt_at = utcnow() - timedelta(seconds=1)  # make due
            s.commit()
            dispatch_once(s)
            s.commit()
            row = s.get(OutboxEvent, env.event_id)
            assert row.attempts == attempt and "downstream unavailable" in row.last_error
            if attempt < MAX_ATTEMPTS:
                assert row.status == "PENDING" and row.next_attempt_at > utcnow()  # backoff is in the future
        assert row.status == "DEAD"  # visible dead letter, not silently dropped
        assert not requeue_dead(s, "other-tenant", env.event_id)  # recovery is tenant-scoped
        state["fail"] = False
        assert requeue_dead(s, t, env.event_id)
        s.commit()
        dispatch_once(s)
        s.commit()
        assert s.get(OutboxEvent, env.event_id).status == "DELIVERED"


def test_failing_consumer_does_not_lose_effects_of_successful_one():
    t, ok_calls = new_tenant(), []
    subscribe("MarginComputed", "good", lambda s, env: ok_calls.append(env["event_id"]))
    subscribe("MarginComputed", "bad", lambda s, env: (_ for _ in ()).throw(RuntimeError("boom")))
    with SessionLocal() as s:
        env = emit_one(s, t)
        s.commit()
        dispatch_once(s)
        s.commit()
        assert env.event_id in ok_calls
        assert s.get(OutboxEvent, env.event_id).status == "PENDING"  # retried later; 'good' is guarded by the inbox
        assert s.get(InboxRecord, ("good", env.event_id)) is not None and s.get(InboxRecord, ("bad", env.event_id)) is None
