"""Replay-and-integrity-lab tamper demonstration. Operates ONLY on an in-memory COPY of an export bundle:
operational evidence, the object store and checkpoints are never mutated."""
from __future__ import annotations

import importlib.util
import io
import json
import zipfile
from functools import lru_cache

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.core.config import REPO_ROOT
from app.core.errors import ValidationFailed
from app.engines.evidence import keys
from app.engines.evidence.models import EvExport
from app.engines.evidence.objects import read_object

MODES = ("none", "modify_event", "drop_event", "truncate_chain_skeleton", "wrong_trusted_key")


@lru_cache
def load_verifier():
    """Load the standalone verifier module by file path (single implementation shared with the CLI)."""
    f = REPO_ROOT / "tools" / "verify_bundle" / "verify.py"
    spec = importlib.util.spec_from_file_location("aether_verify_bundle_standalone", f)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _rewrite(data: bytes, edit) -> bytes:
    zin = zipfile.ZipFile(io.BytesIO(data))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zout:
        for n in zin.namelist():
            zout.writestr(n, edit(n, zin.read(n)))
    return out.getvalue()


def tamper_demo(session, tenant_id: str, export_id: str, mode: str) -> dict:
    if mode not in MODES:
        raise ValidationFailed("unknown tamper mode", field_errors={"mode": f"one of {MODES}"})
    ex = session.get(EvExport, export_id)
    if ex is None or ex.tenant_id != tenant_id:
        from app.core.errors import NotFound
        raise NotFound("Export not found")
    data = bytearray(read_object(session, tenant_id, ex.bundle_hash))  # copy
    data = bytes(data)

    def edit(name: str, raw: bytes) -> bytes:
        if mode == "modify_event" and name == "events.json":
            ev = json.loads(raw)
            if ev:
                ev[0]["action"] = "TAMPERED_ACTION" if not ev[0].get("redacted") else ev[0]["action"] + "X"
            return json.dumps(ev, sort_keys=True, indent=2, ensure_ascii=False).encode()
        if mode == "drop_event" and name == "events.json":
            return json.dumps(json.loads(raw)[1:], sort_keys=True, indent=2, ensure_ascii=False).encode()
        if mode == "truncate_chain_skeleton" and name == "chain_skeleton.json":
            return json.dumps(json.loads(raw)[:-1], sort_keys=True, indent=2).encode()
        return raw

    tampered = _rewrite(data, edit) if mode != "none" else data
    v = load_verifier()
    if mode == "wrong_trusted_key":
        pub = Ed25519PrivateKey.generate().public_key()
        trusted = pub.public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    else:
        trusted = keys.public_keys()[0]["public_key_pem"].encode()
        cur = next(k for k in keys.public_keys() if k["current"])
        trusted = cur["public_key_pem"].encode()
        # a bundle may have been signed by a rotated key: use the key id recorded in the manifest
        kid = json.loads(zipfile.ZipFile(io.BytesIO(data)).read("manifest.sig.json"))["key_id"]
        match = next((k for k in keys.public_keys() if k["key_id"] == kid), None)
        if match:
            trusted = match["public_key_pem"].encode()
    try:
        res = v.verify_bundle(tampered, trusted)
    except v.BundleRejected as exc:
        res = {"verified": False, "problems": [f"REJECTED {exc}"], "events": 0}
    return {"export_id": export_id, "mode": mode, "verified": res["verified"], "problems": res["problems"],
            "operational_evidence_modified": False, "note": "Verification ran on an in-memory copy; the original bundle and chain are untouched."}
