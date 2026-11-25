"""Signed chain-head checkpoints, exported OUTSIDE the DB write boundary.

A hash chain alone cannot detect a privileged rewrite/truncation. A checkpoint {tenant, sequence, head_hash, key_id,
created_at} signed with Ed25519 and written to <signing_key_dir>/../checkpoints/<tenant>/ gives an independent anchor:
`verify_against_exported` checks the signature with the key store and the chain against the anchored head.
"""
from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import select

from app.core.canonical import canonical_bytes
from app.core.config import get_settings
from app.core.errors import ValidationFailed
from app.db.base import new_id, utcnow
from app.engines.evidence import keys
from app.engines.evidence.models import EvChainHead, EvCheckpoint
from app.engines.evidence.service import verify_chain

CP_DOMAIN = "aether.evidence.checkpoint.v1"


def checkpoint_dir(tenant_id: str) -> Path:
    from app.engines.evidence.objects import _TENANT_RE
    if not _TENANT_RE.match(tenant_id):
        raise ValidationFailed("invalid tenant id")
    return Path(get_settings().signing_key_dir).parent / "checkpoints" / tenant_id


def signing_message(tenant_id: str, sequence: int, head_hash: str, key_id: str, created_at: str) -> bytes:
    return canonical_bytes({"domain": CP_DOMAIN, "tenant_id": tenant_id, "sequence": sequence, "head_hash": head_hash,
                            "key_id": key_id, "created_at": created_at})


def latest_trusted_checkpoint(tenant_id: str) -> dict | None:
    """Newest exported checkpoint whose signature verifies (scans file names newest-first; stops at the first valid one)."""
    d = checkpoint_dir(tenant_id)
    for f in sorted(d.glob("cp-*.json"), reverse=True) if d.exists() else []:
        try:
            cp = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        try:
            if verify_checkpoint_signature(cp):
                return cp
        except (KeyError, ValueError, TypeError):
            continue
    return None


def create_checkpoint(session, tenant_id: str, *, require_valid_chain: bool = True, mode: str = "incremental") -> dict:
    """Sign the current head. Refuses to anchor a chain that does not verify (would bless a tampered state).

    mode="incremental" (default): verify only the events since the newest trusted exported checkpoint (cost proportional to
    NEW events), point-checking that anchor. Falls back to a FULL pass when no trusted checkpoint exists.
    mode="full": recompute every event (use for explicit audit actions and periodically).
    The incremental pass does not re-hash events at/below the previous anchor (see verify_chain's trust assumption)."""
    if mode not in ("incremental", "full"):
        raise ValidationFailed("mode must be 'incremental' or 'full'", field_errors={"mode": "incremental|full"})
    if require_valid_chain:
        base = latest_trusted_checkpoint(tenant_id) if mode == "incremental" else None
        v = verify_chain(session, tenant_id, start_after={"sequence": base["sequence"], "head_hash": base["head_hash"]}
                         if base and int(base["sequence"]) > 0 else None)
        if not v["valid"]:
            raise ValidationFailed("chain does not verify; refusing to sign a checkpoint over it",
                                   field_errors={"chain": f"{len(v['problems'])} problem(s)"})
    if require_valid_chain:
        seq, hh = v["head_sequence"], v["head_hash"]  # sign EXACTLY the head that was verified (appends may have landed since)
    else:
        head = session.execute(select(EvChainHead).where(EvChainHead.tenant_id == tenant_id)
                               .execution_options(populate_existing=True)).scalar_one_or_none()
        seq, hh = (head.sequence, head.head_hash) if head else (0, "0" * 64)
    now = utcnow()
    created = now.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    kid = keys.current_key_id()
    kid, sig = keys.sign(signing_message(tenant_id, seq, hh, kid, created))
    cp = {"tenant_id": tenant_id, "sequence": seq, "head_hash": hh, "key_id": kid, "created_at": created,
          "algorithm": "Ed25519", "signature": sig}
    cid = new_id()
    d = checkpoint_dir(tenant_id)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"cp-{seq:010d}-{cid[:8]}.json"
    path.write_text(json.dumps(cp, sort_keys=True, indent=2))
    session.add(EvCheckpoint(checkpoint_id=cid, tenant_id=tenant_id, sequence=seq, head_hash=hh, key_id=kid,
                             signature=sig, created_at=now, exported_path=str(path)))
    session.flush()
    return {"checkpoint_id": cid, **cp, "exported_path": str(path),
            "verification": {"mode": v["mode"] if require_valid_chain else "SKIPPED",
                             "events_recomputed": v["events_recomputed"] if require_valid_chain else 0}}


def verify_checkpoint_signature(cp: dict, pub=None) -> bool:
    msg = signing_message(cp["tenant_id"], int(cp["sequence"]), cp["head_hash"], cp["key_id"], cp["created_at"])
    return keys.verify(cp["key_id"], msg, cp["signature"], pub)


def list_checkpoints(session, tenant_id: str, limit: int = 50) -> list[dict]:
    rows = session.execute(select(EvCheckpoint).where(EvCheckpoint.tenant_id == tenant_id)
                           .order_by(EvCheckpoint.sequence.desc(), EvCheckpoint.created_at.desc()).limit(limit)).scalars().all()
    return [{"checkpoint_id": r.checkpoint_id, "sequence": r.sequence, "head_hash": r.head_hash, "key_id": r.key_id,
             "created_at": r.created_at.isoformat(), "exported": bool(r.exported_path)} for r in rows]


def exported_checkpoints(tenant_id: str) -> list[dict]:
    """Read anchors from the exported directory (NOT the DB), newest first, signature-verified."""
    d = checkpoint_dir(tenant_id)
    out = []
    for f in sorted(d.glob("cp-*.json"), reverse=True) if d.exists() else []:
        try:
            cp = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        cp["signature_valid"] = verify_checkpoint_signature(cp)
        out.append(cp)
    out.sort(key=lambda c: (c["sequence"], c["created_at"]), reverse=True)
    return out


def verify_against_exported(session, tenant_id: str, mode: str = "full") -> dict:
    """Verify the chain against the strongest trusted anchor: the newest exported checkpoint with a valid signature.
    Also checks every other exported anchor (a truncation below ANY signed anchor is detected).

    mode="full" (default): recompute every event. mode="incremental": recompute only events after the newest valid anchor
    (all anchors are still point-checked); the result carries `trust_assumption` and `mode`."""
    if mode not in ("full", "incremental"):
        raise ValidationFailed("mode must be 'full' or 'incremental'", field_errors={"mode": "full|incremental"})
    cps = [c for c in exported_checkpoints(tenant_id)]
    bad_sigs = [c for c in cps if not c["signature_valid"]]
    good = [c for c in cps if c["signature_valid"]]
    if not good:
        r = verify_chain(session, tenant_id)
        r.update(checkpoint_checked=False, anchor_status="NO_TRUSTED_ANCHOR",
                 invalid_signature_anchors=len(bad_sigs))
        return r
    start = {"sequence": good[0]["sequence"], "head_hash": good[0]["head_hash"]} if mode == "incremental" and int(good[0]["sequence"]) > 0 else None
    base = verify_chain(session, tenant_id, checkpoints=[{"sequence": c["sequence"], "head_hash": c["head_hash"]} for c in good],
                        start_after=start)
    problems = list(base["problems"])
    head = session.get(EvChainHead, tenant_id)
    for c in good:
        if head is not None and head.sequence < c["sequence"]:
            problems.append({"problem": "CHAIN_SHORTER_THAN_ANCHOR", "anchor_sequence": c["sequence"], "chain_head": head.sequence})
    for c in bad_sigs:
        problems.append({"problem": "ANCHOR_SIGNATURE_INVALID", "anchor_sequence": c["sequence"]})
    seen, uniq = set(), []
    for p in problems:
        k = json.dumps(p, sort_keys=True)
        if k not in seen:
            seen.add(k)
            uniq.append(p)
    return {**base, "valid": not uniq, "problems": uniq, "checkpoint_checked": True, "anchor_status": "ANCHORED",
            "anchor_sequence": good[0]["sequence"], "anchors_checked": len(good), "invalid_signature_anchors": len(bad_sigs)}
