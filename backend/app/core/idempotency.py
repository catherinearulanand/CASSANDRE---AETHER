"""Idempotent mutations: same key + same payload => stored response replayed; same key + different payload => 409."""
from __future__ import annotations

from collections.abc import Callable

from app.core.canonical import domain_hash
from app.core.errors import Conflict, ValidationFailed
from app.db.models_core import IdempotencyRecord


def run_idempotent(db, tenant_id: str, key: str | None, scope: str, payload: dict, fn: Callable[[], dict]) -> tuple[dict, bool]:
    """Returns (response, replayed). `key` is required for material mutations."""
    if not key:
        raise ValidationFailed("Idempotency-Key header is required", field_errors={"Idempotency-Key": "required"})
    rh = domain_hash("aether.idem.v1", payload)
    rec = db.get(IdempotencyRecord, (tenant_id, key, scope))
    if rec is not None:
        if rec.request_hash != rh:
            raise Conflict("Idempotency-Key reused with a different payload", code="IDEMPOTENCY_MISMATCH")
        return rec.response, True
    out = fn()
    db.add(IdempotencyRecord(tenant_id=tenant_id, key=key, scope=scope, request_hash=rh, response=out))
    db.flush()
    return out, False
