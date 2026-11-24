"""Canonical serialisation and domain-separated SHA-256.

Rules (documented in docs/adr/0002-canonical-serialisation.md):
  * UTF-8; object keys sorted by Unicode code point; no insignificant whitespace.
  * Strings are NFC-normalised.
  * Decimal -> normalised plain string (no exponent). float is REJECTED (unstable representation).
  * datetime -> ISO-8601 UTC with 'Z' and microsecond precision; naive datetimes REJECTED.
  * Arrays keep caller order (callers must sort sets deterministically before hashing).
  * This is a strict subset compatible with RFC 8785 for the value types we emit (no floats).
"""
from __future__ import annotations

import hashlib
import json
import unicodedata
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import Enum
from uuid import UUID

from app.core.money import dstr


class CanonicalError(ValueError):
    pass


def _norm(v):
    if v is None or isinstance(v, bool):
        return v
    if isinstance(v, str):
        return unicodedata.normalize("NFC", v)
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        raise CanonicalError("float is not permitted in canonical records; use a decimal string")
    if isinstance(v, Decimal):
        if not v.is_finite():
            raise CanonicalError("non-finite Decimal")
        return dstr(v)
    if isinstance(v, datetime):
        if v.tzinfo is None:
            raise CanonicalError("naive datetime not permitted")
        return v.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, UUID):
        return str(v)
    if isinstance(v, Enum):
        return _norm(v.value)
    if isinstance(v, bytes):
        return "b64:" + __import__("base64").b64encode(v).decode()
    if isinstance(v, dict):
        out = {}
        for k, val in v.items():
            if not isinstance(k, str):
                raise CanonicalError(f"non-string key {k!r}")
            nk = unicodedata.normalize("NFC", k)
            if nk in out:
                raise CanonicalError(f"duplicate key after normalisation: {nk!r}")
            out[nk] = _norm(val)
        return out
    if isinstance(v, (list, tuple)):
        return [_norm(x) for x in v]
    if hasattr(v, "model_dump"):
        return _norm(v.model_dump(mode="python"))
    raise CanonicalError(f"unsupported type {type(v).__name__}")


def canonical_bytes(value) -> bytes:
    return json.dumps(_norm(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def domain_hash(domain: str, value) -> str:
    """sha256( domain || 0x00 || canonical(value) ) as lowercase hex."""
    h = hashlib.sha256()
    h.update(domain.encode("utf-8"))
    h.update(b"\x00")
    h.update(canonical_bytes(value))
    return h.hexdigest()


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def content_hash(value) -> str:
    """Business-content hash used for input pinning (snapshots, terms, rule sets)."""
    return domain_hash("aether.content.v1", value)
