"""E02 evidence vault: immutable content-addressed objects.

Path = <object_store_dir>/<tenant>/<hash[:2]>/<hash>. Objects are write-once: an existing path is never
overwritten (identical bytes are an idempotent no-op; a hash can only map to its own bytes).
"""
from __future__ import annotations

import hashlib
import os
import re
import uuid
from pathlib import Path

from sqlalchemy import select

from app.core.config import get_settings
from app.core.errors import NotFound, Unavailable, ValidationFailed
from app.db.base import lock_for_update, utcnow
from app.engines.evidence._db import insert_ignore
from app.engines.evidence.models import EvObject

_TENANT_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
CLASSIFICATIONS = frozenset({"SOURCE_DOCUMENT", "SNAPSHOT", "RULE_REVISION", "TERMS_REVISION", "CALCULATION", "OPTIMISATION",
                             "CONTROL_TRACE", "REVIEW_RECORD", "EXPORT_BUNDLE", "TEMP"})


def _root(tenant_id: str) -> Path:
    if not _TENANT_RE.match(tenant_id):
        raise ValidationFailed("invalid tenant id for object path")
    return Path(get_settings().object_store_dir) / tenant_id


def object_path(tenant_id: str, content_hash: str) -> Path:
    if not _HASH_RE.match(content_hash):
        raise ValidationFailed("content hash must be 64 lowercase hex characters")
    return _root(tenant_id) / content_hash[:2] / content_hash


def store_object(session, tenant_id: str, data: bytes, classification: str) -> str:
    """Store bytes; returns sha256 hex. Raises Unavailable if the store cannot be written (callers block).

    Concurrency (found on PostgreSQL): the object row is created with INSERT .. ON CONFLICT DO NOTHING, then LOCKED, and only
    then is the blob written if absent. A concurrent purge of the same TEMP object therefore cannot unlink the blob between
    'row exists' and 'blob written'. If the same bytes are stored again with a non-TEMP classification, the row is UPGRADED so
    protected evidence is never left classified TEMP (and purgeable)."""
    if classification not in CLASSIFICATIONS:
        raise ValidationFailed(f"unknown classification {classification!r}", field_errors={"classification": "unsupported"})
    h = hashlib.sha256(data).hexdigest()
    p = object_path(tenant_id, h)
    for _attempt in (1, 2):  # second attempt only if a concurrent purge deleted the row while we waited for the lock
        insert_ignore(session, EvObject, tenant_id=tenant_id, content_hash=h, classification=classification,
                      size_bytes=len(data), created_at=utcnow())
        row = session.execute(lock_for_update(select(EvObject).where(EvObject.tenant_id == tenant_id, EvObject.content_hash == h))
                              .execution_options(populate_existing=True)).scalar_one_or_none()
        if row is not None:
            break
    else:
        raise Unavailable("object registry busy; retry", code="EVIDENCE_OBJECT_RETRY")
    if row.classification == "TEMP" and classification != "TEMP":
        row.classification = classification
    try:
        if not p.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.parent / f".tmp-{uuid.uuid4().hex}"
            tmp.write_bytes(data)
            os.replace(tmp, p)  # atomic publish
    except OSError as exc:
        raise Unavailable(f"evidence object store unavailable: {exc.strerror or exc}", code="EVIDENCE_STORAGE_UNAVAILABLE") from exc
    session.flush()
    return h


def read_object(session, tenant_id: str, content_hash: str) -> bytes:
    """Tenant-scoped read that re-hashes the bytes (a modified or missing blob is an error, never silently served)."""
    if session.get(EvObject, (tenant_id, content_hash)) is None:
        raise NotFound("object not found")
    p = object_path(tenant_id, content_hash)
    if not p.exists():
        raise Unavailable("object bytes missing from store", code="EVIDENCE_BLOB_MISSING")
    data = p.read_bytes()
    if hashlib.sha256(data).hexdigest() != content_hash:
        raise Unavailable("object bytes do not match content hash", code="EVIDENCE_BLOB_CORRUPT")
    return data


def verify_objects(session, tenant_id: str) -> dict:
    rows = session.execute(select(EvObject).where(EvObject.tenant_id == tenant_id)).scalars().all()
    problems = []
    for r in rows:
        p = object_path(tenant_id, r.content_hash)
        if not p.exists():
            problems.append({"content_hash": r.content_hash, "problem": "MISSING_BLOB"})
        elif hashlib.sha256(p.read_bytes()).hexdigest() != r.content_hash:
            problems.append({"content_hash": r.content_hash, "problem": "HASH_MISMATCH"})
    return {"objects": len(rows), "valid": not problems, "problems": problems}


def storage_probe() -> dict:
    """Cheap writability probe for the integrity dashboard / readiness."""
    root = Path(get_settings().object_store_dir)
    try:
        root.mkdir(parents=True, exist_ok=True)
        t = root / f".probe-{uuid.uuid4().hex}"
        t.write_bytes(b"x")
        t.unlink()
        return {"writable": True, "path": str(root)}
    except OSError as exc:
        return {"writable": False, "path": str(root), "error": exc.strerror or str(exc)}


def purge_temp_objects(session, tenant_id: str, *, older_than_days: int, legal_hold: bool = False) -> dict:
    """Controlled purge for NON-EVIDENCE temporary objects only. Protected audit material is never purged here.

    The TEMP_OBJECTS retention row is created if absent and LOCKED, so a concurrent legal-hold change is serialised with the
    purge (either the hold is visible here, or the purge completed first). Selected rows are locked before the blobs are
    unlinked so a concurrent store of the same bytes waits and then re-registers the object."""
    from datetime import timedelta

    from app.engines.evidence.models import EvRetention
    insert_ignore(session, EvRetention, tenant_id=tenant_id, category="TEMP_OBJECTS", legal_hold=False, updated_at=utcnow())
    ret = session.execute(lock_for_update(select(EvRetention).where(EvRetention.tenant_id == tenant_id, EvRetention.category == "TEMP_OBJECTS"))
                          .execution_options(populate_existing=True)).scalar_one()
    if legal_hold or ret.legal_hold:
        return {"purged": 0, "reason": "LEGAL_HOLD"}
    cutoff = utcnow() - timedelta(days=older_than_days)
    rows = session.execute(lock_for_update(select(EvObject).where(
        EvObject.tenant_id == tenant_id, EvObject.classification == "TEMP", EvObject.created_at < cutoff))
        .execution_options(populate_existing=True)).scalars().all()
    for r in rows:
        object_path(tenant_id, r.content_hash).unlink(missing_ok=True)
        session.delete(r)
    session.flush()
    return {"purged": len(rows)}
