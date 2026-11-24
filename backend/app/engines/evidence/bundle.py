"""E04 audit packs: signed, verifiable evidence bundles (zip). INTERNAL evidence pack, not a regulatory submission.

Contents: manifest.json (+ manifest.sig.json), checkpoint.json, chain_skeleton.json (hash-only prefix proving chain
membership), events.json, events.csv (formula-injection escaped), summary.json, lineage.json, replay.json, objects/<sha256>.
Verified offline by tools/verify_bundle (independent implementation).
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import zipfile

from sqlalchemy import select

from app.core.canonical import _norm, canonical_bytes
from app.db.base import new_id, utcnow
from app.engines.evidence import keys
from app.engines.evidence.checkpoints import create_checkpoint
from app.engines.evidence.lineage import build_lineage
from app.engines.evidence.models import EvEvent, EvExport, EvObject, EvReplay
from app.engines.evidence.objects import read_object, store_object
from app.engines.evidence.service import _HASHED_FIELDS, append

MAX_OBJECT_BYTES, MAX_OBJECTS = 5 * 2**20, 200
KEEP_WHEN_REDACTED = ("event_id", "engine", "action", "actor_type", "correlation_id", "entity_type", "entity_id",
                      "entity_revision", "recorded_at", "effective_at", "source_mode", "service_version")
LIMITATIONS = [
    "Synthetic data only; this is an INTERNAL evidence pack, not a regulatory submission.",
    "Integrity is proven relative to the supplied trusted public key; a local file key is not an HSM and gives no non-repudiation.",
    "A hash chain alone cannot detect truncation; the signed checkpoint bounds the proven prefix. Events after the checkpoint are not covered.",
    "Redacted bundles omit event content; they prove chain membership only and do not contain the entire original chain.",
]


def csv_escape(v) -> str:
    """Neutralise spreadsheet formula injection: a leading = + - @ TAB CR gets a single-quote prefix."""
    s = "" if v is None else (v if isinstance(v, str) else json.dumps(v, sort_keys=True, ensure_ascii=False))
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s


def _event_record(ev: EvEvent, tenant_id: str, redact: bool) -> dict:
    full = {k: _norm(getattr(ev, k)) for k in _HASHED_FIELDS}
    rec = {"tenant_id": tenant_id, "sequence": ev.sequence, "prev_hash": ev.prev_hash, "event_hash": ev.event_hash, **full}
    if not redact:
        return rec
    return {"tenant_id": tenant_id, "sequence": ev.sequence, "prev_hash": ev.prev_hash, "event_hash": ev.event_hash,
            "redacted": True, **{k: rec[k] for k in KEEP_WHEN_REDACTED}}


def _csv(events: list[dict]) -> bytes:
    cols = ["sequence", "event_id", "engine", "action", "actor_id", "actor_type", "entity_type", "entity_id",
            "recorded_at", "effective_at", "correlation_id", "source_mode", "rationale", "event_hash", "redacted"]
    buf = io.StringIO(newline="")
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(cols)
    for e in events:
        w.writerow([csv_escape(e.get(c)) if c != "sequence" else e["sequence"] for c in cols])
    return buf.getvalue().encode("utf-8")


def _json(v) -> bytes:
    return json.dumps(v, sort_keys=True, indent=2, ensure_ascii=False).encode("utf-8")


def build_bundle(session, tenant_id: str, run_id: str, correlation_id: str, actor_id: str, *, redact: bool = False) -> dict:
    cp = create_checkpoint(session, tenant_id)  # anchors the chain head; refuses to sign a tampered chain
    seq_limit = cp["sequence"]
    skeleton = [[r.sequence, r.prev_hash, r.event_hash] for r in session.execute(
        select(EvEvent.sequence, EvEvent.prev_hash, EvEvent.event_hash).where(
            EvEvent.tenant_id == tenant_id, EvEvent.sequence <= seq_limit).order_by(EvEvent.sequence))]
    evs = session.execute(select(EvEvent).where(EvEvent.tenant_id == tenant_id, EvEvent.correlation_id == correlation_id,
                                                EvEvent.sequence <= seq_limit).order_by(EvEvent.sequence)).scalars().all()
    events = [_event_record(e, tenant_id, redact) for e in evs]

    files: dict[str, bytes] = {
        "checkpoint.json": _json({k: cp[k] for k in ("tenant_id", "sequence", "head_hash", "key_id", "created_at", "algorithm", "signature")}),
        "chain_skeleton.json": _json(skeleton),
        "events.json": _json(events),
        "events.csv": _csv(events),
    }
    missing_objects, objects = [], {}
    if not redact:
        wanted = []
        for e in evs:
            wanted += list(e.input_hashes or []) + ([e.output_hash] if e.output_hash else [])
        for h in sorted(set(wanted)):
            if session.get(EvObject, (tenant_id, h)) is None:
                continue  # a hash that is not a vault object (e.g. a business hash) is not an "object"
            if len(objects) >= MAX_OBJECTS:
                missing_objects.append({"content_hash": h, "reason": "BUNDLE_OBJECT_LIMIT"})
                continue
            try:
                data = read_object(session, tenant_id, h)
            except Exception as exc:  # noqa: BLE001 - missing blob is REPORTED
                missing_objects.append({"content_hash": h, "reason": getattr(exc, "code", type(exc).__name__)})
                continue
            if len(data) > MAX_OBJECT_BYTES:
                missing_objects.append({"content_hash": h, "reason": "OBJECT_TOO_LARGE"})
                continue
            objects[f"objects/{h}"] = data
    files.update(objects)

    sources = {}
    for e in evs:
        for sp in (e.source_spans or []) if not redact else []:
            sources[json.dumps(sp, sort_keys=True, default=str)] = sp
    by_engine: dict[str, int] = {}
    for e in evs:
        by_engine[e.engine] = by_engine.get(e.engine, 0) + 1
    lineage = build_lineage(session, tenant_id, correlation_id)
    rp = session.execute(select(EvReplay).where(EvReplay.tenant_id == tenant_id, EvReplay.run_id == run_id)
                         .order_by(EvReplay.created_at.desc()).limit(1)).scalar_one_or_none()
    files["summary.json"] = _json({
        "run_id": run_id, "correlation_id": correlation_id, "events": len(evs), "events_by_engine": by_engine,
        "methodology": "Hash-chained evidence events (SHA-256, domain-separated canonical JSON) + Ed25519 signed checkpoint.",
        "limitations": LIMITATIONS, "governing_sources": [] if redact else list(sources.values()),
        "lineage_complete": lineage["complete"], "lineage_missing": lineage["missing"],
        "content": "REDACTED" if redact else "FULL"})
    files["lineage.json"] = _json(lineage)
    files["replay.json"] = _json({"replay_id": rp.replay_id, "status": rp.status, "result": rp.result} if rp else None)

    manifest = {
        "format": "aether-evidence-bundle/1", "tenant_id": tenant_id, "run_id": run_id, "correlation_id": correlation_id,
        "generated_at": utcnow().strftime("%Y-%m-%dT%H:%M:%S.%fZ"), "redacted": redact,
        "redaction_note": ("REDACTED export: event content omitted; proves chain membership only; does NOT contain the entire "
                           "original chain or object content.") if redact else None,
        "scope": "INTERNAL_EVIDENCE_PACK_NOT_A_REGULATORY_SUBMISSION", "data_mode": "SYNTHETIC",
        "event_count": len(events), "missing_objects": missing_objects,
        "chain": {"checkpoint_sequence": cp["sequence"], "checkpoint_head_hash": cp["head_hash"], "key_id": cp["key_id"]},
        "files": {n: {"sha256": hashlib.sha256(b).hexdigest(), "size": len(b)} for n, b in sorted(files.items())},
        "limitations": LIMITATIONS,
    }
    kid, sig = keys.sign(canonical_bytes(manifest))
    files["manifest.json"] = _json(manifest)
    files["manifest.sig.json"] = _json({"key_id": kid, "algorithm": "Ed25519", "signature": sig})

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name in sorted(files):
            zi = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            zi.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(zi, files[name])
    data = buf.getvalue()
    bundle_hash = store_object(session, tenant_id, data, "EXPORT_BUNDLE")
    export_id = new_id()
    session.add(EvExport(export_id=export_id, tenant_id=tenant_id, run_id=run_id, actor_id=actor_id, redacted=redact,
                         bundle_hash=bundle_hash, manifest_hash=hashlib.sha256(files["manifest.json"]).hexdigest(),
                         checkpoint_id=cp["checkpoint_id"], event_count=len(events)))
    session.flush()
    append(session, tenant_id, engine="evidence", action="EVIDENCE_EXPORTED", actor_id=actor_id, actor_type="USER",
           entity_type="export", entity_id=export_id, correlation_id=correlation_id, output_hash=bundle_hash,
           payload={"run_id": run_id, "redacted": redact, "events": len(events), "checkpoint_sequence": cp["sequence"]})
    return {"export_id": export_id, "run_id": run_id, "redacted": redact, "bundle_hash": bundle_hash, "events": len(events),
            "checkpoint_sequence": cp["sequence"], "key_id": kid, "missing_objects": missing_objects, "size_bytes": len(data),
            "lineage_complete": lineage["complete"]}
