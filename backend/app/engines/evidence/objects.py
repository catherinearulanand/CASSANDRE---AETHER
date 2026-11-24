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
    """Store bytes; returns sha256 hex. Raises Unavailable if the store cannot be written (callers block)."""
    if classification not in CLASSIFICATIONS:
        raise ValidationFailed(f"unknown classification {classification!r}", field_errors={"classification": "unsupported"})
    h = hashlib.sha256(data).hexdigest()
    p = object_path(tenant_id, h)
    try:
        if not p.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.parent / f".tmp-{uuid.uuid4().hex}"
            tmp.write_bytes(data)
            os.replace(tmp, p)  # atomic publish
    except OSError as exc:
        raise Unavailable(f"evidence object store unavailable: {exc.strerror or exc}", code="EVIDENCE_STORAGE_UNAVAILABLE") from exc
    if session.get(EvObject, (tenant_id, h)) is None:
        session.add(EvObject(tenant_id=tenant_id, content_hash=h, classification=classification, size_bytes=len(data)))
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
    """Controlled purge for NON-EVIDENCE temporary objects only. Protected audit material is never purged here."""
    from datetime import timedelta

    from app.db.base import utcnow
    if legal_hold:
        return {"purged": 0, "reason": "LEGAL_HOLD"}
    cutoff = utcnow() - timedelta(days=older_than_days)
    rows = session.execute(select(EvObject).where(EvObject.tenant_id == tenant_id, EvObject.classification == "TEMP",
                                                  EvObject.created_at < cutoff)).scalars().all()
    for r in rows:
        object_path(tenant_id, r.content_hash).unlink(missing_ok=True)
        session.delete(r)
    session.flush()
    return {"purged": len(rows)}
