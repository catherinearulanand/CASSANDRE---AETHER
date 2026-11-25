"""Signing-key rotation procedure (A6 round 2): audited rotation, old checkpoints/bundles keep verifying with the OLD public key,
new ones need the NEW key, a lost old .pub makes old anchors fail loudly (recovery = restore the public key), and the
offline verifier never accepts a bundle under the wrong trusted key. Local file key, not an HSM ceremony."""
import hashlib
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import select

from app.core.errors import ValidationFailed
from app.db.base import SessionLocal
from app.engines.evidence import checkpoints as cps
from app.engines.evidence import keys, ops
from app.engines.evidence.bundle import build_bundle
from app.engines.evidence.models import EvEvent
from app.engines.evidence.objects import read_object
from tests.engines.evidence.conftest import add_events

ROOT = Path(__file__).resolve().parents[3]


def _pub(tmp_path: Path, kid: str, name: str) -> Path:
    k = next(x for x in keys.public_keys() if x["key_id"] == kid)
    f = tmp_path / name
    f.write_text(k["public_key_pem"])
    return f


def _cli(bundle: Path, pub: Path):
    return subprocess.run([sys.executable, "-m", "tools.verify_bundle", str(bundle), "--trusted-key", str(pub)],
                          cwd=ROOT, capture_output=True, text=True, env={"PYTHONPATH": str(ROOT), "SYSTEMROOT": "C:\\Windows"})


def _export(tenant, run_id, corr, tmp_path, name):
    with SessionLocal() as s:
        out = build_bundle(s, tenant, run_id, corr, "auditor-1")
        s.commit()
        data = read_object(s, tenant, out["bundle_hash"])
    f = tmp_path / name
    f.write_bytes(data)
    return f


def test_rotation_procedure_end_to_end(tenant, tmp_path):
    with SessionLocal() as s:
        add_events(s, tenant, 3, correlation="run-a")
        s.commit()
    k1 = keys.current_key_id()
    b1 = _export(tenant, "run-1", "run-a", tmp_path, "b1.zip")                       # signed by K1
    with SessionLocal() as s:
        res = ops.rotate_signing_key(s, tenant, "admin-1", "scheduled annual rotation")  # audited
        s.commit()
        add_events(s, tenant, 3, correlation="run-b")
        s.commit()
    k2 = res["current_key_id"]
    assert k2 != k1 and res["previous_key_id"] == k1 and keys.current_key_id() == k2
    b2 = _export(tenant, "run-2", "run-b", tmp_path, "b2.zip")                       # signed by K2
    pub1, pub2 = _pub(tmp_path, k1, "k1.pub"), _pub(tmp_path, k2, "k2.pub")
    # offline verifier: each bundle needs the key that signed it; the wrong trusted key is NEVER accepted
    assert _cli(b1, pub1).returncode == 0 and _cli(b2, pub2).returncode == 0
    assert _cli(b1, pub2).returncode == 1 and _cli(b2, pub1).returncode == 1
    # platform view: anchors under BOTH keys verify; the chain has an audited rotation event
    with SessionLocal() as s:
        r = cps.verify_against_exported(s, tenant, mode="full")
        assert r["valid"] and r["anchors_checked"] >= 2
        ev = s.execute(select(EvEvent).where(EvEvent.tenant_id == tenant, EvEvent.action == "SIGNING_KEY_ROTATED")).scalar_one()
        assert ev.payload["previous_key_id"] == k1 and ev.payload["new_key_id"] == k2 and ev.rationale == "scheduled annual rotation"
        assert ev.payload["new_public_key_sha256"] == hashlib.sha256(pub2.read_bytes()).hexdigest()
        assert "BEGIN" not in str(ev.payload)                                        # no key material in the chain
    # recovery: losing the OLD public key makes old anchors fail loudly (never silently trusted); restoring it heals
    old_pub = Path(keys._dir()) / f"ev-{k1}.pub"
    saved = old_pub.read_text()
    old_pub.unlink()
    try:
        with SessionLocal() as s:
            bad = cps.verify_against_exported(s, tenant)
            assert not bad["valid"] and any(p["problem"] == "ANCHOR_SIGNATURE_INVALID" for p in bad["problems"])
    finally:
        old_pub.write_text(saved)
    with SessionLocal() as s:
        assert cps.verify_against_exported(s, tenant)["valid"]


def test_rotation_requires_a_reason(tenant):
    with SessionLocal() as s:
        with pytest.raises(ValidationFailed):
            ops.rotate_signing_key(s, tenant, "admin-1", " ")


def test_rotation_over_http_is_admin_only_idempotent_and_audited(app_client, audit_users):
    from tests.conftest import login
    admin = login(app_client, "tenant-a", "admin")
    auditor = login(app_client, "tenant-a", "auditor")
    h = {"Idempotency-Key": "rot-1"}
    assert auditor.post("/api/v1/evidence/keys/rotate", json={"reason": "no permission"}, headers=h).status_code == 403
    r1 = admin.post("/api/v1/evidence/keys/rotate", json={"reason": "scheduled rotation"}, headers=h)
    assert r1.status_code == 201, r1.text
    r2 = admin.post("/api/v1/evidence/keys/rotate", json={"reason": "scheduled rotation"}, headers=h)
    assert r2.json() == r1.json()                                                    # replay, not a second rotation
    assert admin.post("/api/v1/evidence/keys/rotate", json={"reason": "different payload"}, headers=h).status_code == 409
    assert keys.current_key_id() == r1.json()["current_key_id"]
