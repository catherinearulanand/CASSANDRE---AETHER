"""Independent offline verifier for AETHER evidence bundles.

Deliberately imports NOTHING from the platform (`app.*`): it re-implements canonical serialisation and the event hash
so a bundle can be checked on a machine that only has this folder + `cryptography` + the trusted PUBLIC key.

What a pass proves: the bundle files are exactly what the key holder signed; included events recompute to their
hashes; each event belongs to the signed hash-chain prefix up to the signed checkpoint.
What it does NOT prove: that the platform operator did not hold the signing key; completeness of the original chain
beyond the checkpoint; legal admissibility. Redacted events cannot be recomputed (flagged; membership checked only).
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import re
import unicodedata
import zipfile

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

EVENT_DOMAIN = "aether.evidence.event.v1"
CP_DOMAIN = "aether.evidence.checkpoint.v1"
GENESIS = "0" * 64
HASHED_FIELDS = ("event_id", "engine", "action", "actor_id", "actor_type", "actor_role", "service_version",
                 "correlation_id", "causation_id", "entity_type", "entity_id", "entity_revision", "effective_at",
                 "recorded_at", "source_mode", "input_hashes", "source_spans", "output_hash", "rationale", "payload")
MAX_ENTRIES, MAX_TOTAL, MAX_ONE, MAX_RATIO = 5000, 256 * 2**20, 64 * 2**20, 200
_SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*$")


class BundleRejected(Exception):
    """Structurally unsafe or unreadable bundle (path traversal, zip bomb, missing manifest...)."""


def _norm(v):
    if isinstance(v, str):
        return unicodedata.normalize("NFC", v)
    if isinstance(v, float):
        raise BundleRejected("float in canonical record")
    if isinstance(v, dict):
        return {unicodedata.normalize("NFC", k): _norm(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_norm(x) for x in v]
    return v


def canonical(v) -> bytes:
    return json.dumps(_norm(v), sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def domain_hash(domain: str, v) -> str:
    return hashlib.sha256(domain.encode() + b"\x00" + canonical(v)).hexdigest()


def event_hash(tenant: str, ev: dict) -> str:
    rec = {"chain_id": tenant, "sequence": ev["sequence"], "prev_hash": ev["prev_hash"]}
    rec.update({k: ev[k] for k in HASHED_FIELDS})
    return domain_hash(EVENT_DOMAIN, rec)


def load_public_key(raw: bytes) -> Ed25519PublicKey:
    txt = raw.strip()
    try:
        if b"BEGIN PUBLIC KEY" in txt:
            k = serialization.load_pem_public_key(txt)
            if isinstance(k, Ed25519PublicKey):
                return k
            raise ValueError("not an Ed25519 key")
        return Ed25519PublicKey.from_public_bytes(bytes.fromhex(txt.decode()))
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"unreadable trusted key: {exc}") from exc


def key_id_of(pub: Ed25519PublicKey) -> str:
    raw = pub.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return hashlib.sha256(raw).hexdigest()[:16]


def _verify_sig(pub: Ed25519PublicKey, message: bytes, sig_b64: str) -> bool:
    try:
        pub.verify(base64.b64decode(sig_b64, validate=True), message)
        return True
    except Exception:  # noqa: BLE001
        return False


def _safe_read(zf: zipfile.ZipFile) -> dict[str, bytes]:
    infos = zf.infolist()
    if len(infos) > MAX_ENTRIES:
        raise BundleRejected(f"too many entries ({len(infos)})")
    total, out = 0, {}
    for i in infos:
        n = i.filename
        if i.is_dir():
            continue
        if n.startswith("/") or "\\" in n or ".." in n.split("/") or ":" in n or not _SAFE_NAME.match(n):
            raise BundleRejected(f"unsafe path in bundle: {n!r}")
        if n in out:
            raise BundleRejected(f"duplicate entry: {n!r}")
        if i.file_size > MAX_ONE:
            raise BundleRejected(f"entry too large: {n!r}")
        if i.compress_size and i.file_size / max(i.compress_size, 1) > MAX_RATIO:
            raise BundleRejected(f"suspicious compression ratio (zip bomb?): {n!r}")
        total += i.file_size
        if total > MAX_TOTAL:
            raise BundleRejected("bundle exceeds total size limit")
        with zf.open(i) as fh:  # bounded read: never trust the declared size
            data = fh.read(MAX_ONE + 1)
        if len(data) != i.file_size or len(data) > MAX_ONE:
            raise BundleRejected(f"declared size mismatch (zip bomb?): {n!r}")
        out[n] = data
    return out


def verify_bundle(source, trusted_key: bytes) -> dict:
    """source: path or bytes. Returns {verified, problems[], ...}. Raises BundleRejected for unsafe structure."""
    pub = load_public_key(trusted_key)
    try:
        zf = zipfile.ZipFile(io.BytesIO(source) if isinstance(source, (bytes, bytearray)) else source)
    except zipfile.BadZipFile as exc:
        raise BundleRejected(f"not a valid zip: {exc}") from exc
    with zf:
        files = _safe_read(zf)
    problems: list[str] = []
    for req in ("manifest.json", "manifest.sig.json", "checkpoint.json", "chain_skeleton.json", "events.json"):
        if req not in files:
            raise BundleRejected(f"missing required file {req}")
    try:
        manifest = json.loads(files["manifest.json"])
        sigdoc = json.loads(files["manifest.sig.json"])
        cp = json.loads(files["checkpoint.json"])
        skeleton = json.loads(files["chain_skeleton.json"])
        events = json.loads(files["events.json"])
    except ValueError as exc:
        raise BundleRejected(f"malformed JSON: {exc}") from exc

    kid = key_id_of(pub)
    if sigdoc.get("key_id") != kid:
        problems.append(f"KEY_ID_MISMATCH manifest {sigdoc.get('key_id')} != trusted {kid}")
    if not _verify_sig(pub, canonical(manifest), sigdoc.get("signature", "")):
        problems.append("MANIFEST_SIGNATURE_INVALID")

    listed = manifest.get("files", {})
    for name, meta in listed.items():
        if name not in files:
            problems.append(f"MISSING_FILE {name}")
        elif hashlib.sha256(files[name]).hexdigest() != meta.get("sha256") or len(files[name]) != meta.get("size"):
            problems.append(f"FILE_HASH_MISMATCH {name}")
    for name in files:
        if name not in listed and name not in ("manifest.json", "manifest.sig.json"):
            problems.append(f"UNLISTED_FILE {name}")
        if name.startswith("objects/") and name.split("/", 1)[1] != hashlib.sha256(files[name]).hexdigest():
            problems.append(f"OBJECT_NAME_HASH_MISMATCH {name}")

    tenant = manifest.get("tenant_id")
    cp_msg = canonical({"domain": CP_DOMAIN, "tenant_id": cp.get("tenant_id"), "sequence": cp.get("sequence"),
                        "head_hash": cp.get("head_hash"), "key_id": cp.get("key_id"), "created_at": cp.get("created_at")})
    if cp.get("key_id") != kid or not _verify_sig(pub, cp_msg, cp.get("signature", "")):
        problems.append("CHECKPOINT_SIGNATURE_INVALID")
    if cp.get("tenant_id") != tenant:
        problems.append("CHECKPOINT_TENANT_MISMATCH")
    chain = manifest.get("chain", {})
    if chain.get("checkpoint_sequence") != cp.get("sequence") or chain.get("checkpoint_head_hash") != cp.get("head_hash"):
        problems.append("MANIFEST_CHECKPOINT_MISMATCH")

    # chain skeleton: complete prefix 1..N, linked from genesis, ending at the signed head
    prev, by_seq = GENESIS, {}
    for idx, row in enumerate(skeleton, start=1):
        seq, p, h = row
        if seq != idx:
            problems.append(f"SKELETON_SEQUENCE_GAP at {idx}")
            break
        if p != prev:
            problems.append(f"SKELETON_LINK_BROKEN at {seq}")
        by_seq[seq] = (p, h)
        prev = h
    if len(skeleton) != cp.get("sequence"):
        problems.append(f"SKELETON_LENGTH {len(skeleton)} != checkpoint sequence {cp.get('sequence')}")
    elif skeleton and skeleton[-1][2] != cp.get("head_hash"):
        problems.append("SKELETON_HEAD_NOT_CHECKPOINT_HEAD")

    redacted, seen = 0, set()
    for ev in events:
        seq = ev.get("sequence")
        if seq in seen:
            problems.append(f"DUPLICATE_EVENT sequence {seq}")
        seen.add(seq)
        if seq not in by_seq:
            problems.append(f"EVENT_NOT_IN_CHAIN sequence {seq}")
            continue
        if (ev.get("prev_hash"), ev.get("event_hash")) != by_seq[seq]:
            problems.append(f"EVENT_CHAIN_MISMATCH sequence {seq}")
            continue
        if ev.get("redacted"):
            redacted += 1
            continue
        try:
            if event_hash(tenant, ev) != ev["event_hash"]:
                problems.append(f"EVENT_HASH_MISMATCH sequence {seq}")
        except KeyError as exc:
            problems.append(f"EVENT_FIELD_MISSING sequence {seq}: {exc}")
    ordered = [e.get("sequence") for e in events]
    if ordered != sorted(ordered):
        problems.append("EVENTS_OUT_OF_ORDER")
    return {"verified": not problems, "problems": problems, "events": len(events), "redacted_events": redacted,
            "checkpoint_sequence": cp.get("sequence"), "key_id": kid, "tenant_id": tenant,
            "redacted_bundle": bool(manifest.get("redacted")),
            "scope_note": "Proves signed integrity of this bundle only; not a regulatory submission."}
