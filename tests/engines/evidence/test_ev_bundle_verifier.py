"""E-A07 offline verifier, E-A08 redaction/CSV safety, G17 tampered copied bundle."""
import io
import json
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from app.db.base import SessionLocal
from app.engines.evidence import keys, objects
from app.engines.evidence.bundle import build_bundle, csv_escape
from app.engines.evidence.tamper import tamper_demo
from tests.engines.evidence.conftest import add_events

ROOT = Path(__file__).resolve().parents[3]


def _pubfile(tmp_path, kid=None) -> Path:
    k = next(x for x in keys.public_keys() if (x["key_id"] == kid if kid else x["current"]))
    f = tmp_path / "trusted.pub"
    f.write_text(k["public_key_pem"])
    return f


def _cli(bundle: Path, pub: Path) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "tools.verify_bundle", str(bundle), "--trusted-key", str(pub)],
                          cwd=ROOT, capture_output=True, text=True, env={"PYTHONPATH": str(ROOT), "SYSTEMROOT": "C:\\Windows"})


@pytest.fixture
def built(tenant, ev_dirs):
    with SessionLocal() as s:
        h = objects.store_object(s, tenant, b'{"curve":"synthetic"}', "SNAPSHOT")
        add_events(s, tenant, 3, correlation="other")
        add_events(s, tenant, 4, correlation="run-corr", input_hashes=[h], rationale="=HYPERLINK(\"http://x\")",
                   source_spans=[{"document_id": "DOC-1", "start": 0, "end": 10}], payload={"amount": "9500000.00", "currency": "USD"})
        add_events(s, tenant, 2, correlation="other2")
        s.commit()
        out = build_bundle(s, tenant, "run-1", "run-corr", "auditor-1")
        s.commit()
        from app.engines.evidence.objects import read_object
        data = read_object(s, tenant, out["bundle_hash"])
        return tenant, out, data


def test_valid_bundle_verifies_via_cli_exit_0(built, ev_dirs):
    _, out, data = built
    b = ev_dirs / "b.zip"
    b.write_bytes(data)
    r = _cli(b, _pubfile(ev_dirs))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "VERIFIED" in r.stdout and out["events"] == 4 and out["missing_objects"] == []


def test_bundle_contains_object_and_signed_manifest(built):
    _, _, data = built
    z = zipfile.ZipFile(io.BytesIO(data))
    names = z.namelist()
    assert {"manifest.json", "manifest.sig.json", "checkpoint.json", "chain_skeleton.json", "events.json", "events.csv",
            "summary.json", "lineage.json", "replay.json"} <= set(names)
    assert any(n.startswith("objects/") for n in names)
    m = json.loads(z.read("manifest.json"))
    assert m["scope"] == "INTERNAL_EVIDENCE_PACK_NOT_A_REGULATORY_SUBMISSION" and m["redacted"] is False


def test_modified_event_fails_cli_nonzero(built, ev_dirs):
    _, _, data = built
    zin = zipfile.ZipFile(io.BytesIO(data))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zo:
        for n in zin.namelist():
            raw = zin.read(n)
            if n == "events.json":
                raw = raw.replace(b'"TEST"', b'"TAMPERED"', 1)
            zo.writestr(n, raw)
    b = ev_dirs / "t.zip"
    b.write_bytes(out.getvalue())
    r = _cli(b, _pubfile(ev_dirs))
    assert r.returncode == 1 and "FILE_HASH_MISMATCH events.json" in r.stdout


def test_tamper_with_rehashed_manifest_fails_signature_and_event_hash(built, ev_dirs):
    """Attacker edits an event AND updates the manifest file hash: signature + recomputed event hash still fail."""
    import hashlib
    _, _, data = built
    zin = zipfile.ZipFile(io.BytesIO(data))
    files = {n: zin.read(n) for n in zin.namelist()}
    ev = json.loads(files["events.json"])
    ev[0]["actor_id"] = "mallory"
    files["events.json"] = json.dumps(ev, sort_keys=True, indent=2, ensure_ascii=False).encode()
    m = json.loads(files["manifest.json"])
    m["files"]["events.json"] = {"sha256": hashlib.sha256(files["events.json"]).hexdigest(), "size": len(files["events.json"])}
    files["manifest.json"] = json.dumps(m, sort_keys=True, indent=2).encode()
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as zo:
        for n, raw in files.items():
            zo.writestr(n, raw)
    b = ev_dirs / "t2.zip"
    b.write_bytes(out.getvalue())
    r = _cli(b, _pubfile(ev_dirs))
    assert r.returncode == 1 and "MANIFEST_SIGNATURE_INVALID" in r.stdout and "EVENT_HASH_MISMATCH" in r.stdout


def test_wrong_trusted_key_rejected(built, ev_dirs):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    _, _, data = built
    b = ev_dirs / "b.zip"
    b.write_bytes(data)
    other = ev_dirs / "other.pub"
    other.write_bytes(Ed25519PrivateKey.generate().public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    assert _cli(b, other).returncode == 1


def test_path_traversal_and_zip_bomb_rejected(ev_dirs):
    keys.current_key_id()
    pub = _pubfile(ev_dirs)
    evil = ev_dirs / "evil.zip"
    with zipfile.ZipFile(evil, "w") as z:
        z.writestr("../../escape.txt", "x")
    r = _cli(evil, pub)
    assert r.returncode == 1 and "unsafe path" in r.stdout
    bomb = ev_dirs / "bomb.zip"
    with zipfile.ZipFile(bomb, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", b"\0" * (30 * 2**20))
    r = _cli(bomb, pub)
    assert r.returncode == 1 and "zip bomb" in r.stdout
    assert not (ev_dirs.parent / "escape.txt").exists() and not (ev_dirs / "escape.txt").exists()


def test_redacted_bundle_verifies_but_is_marked_and_has_no_content(tenant, ev_dirs):
    with SessionLocal() as s:
        add_events(s, tenant, 3, correlation="rc", rationale="secret reasoning", payload={"k": "v"})
        s.commit()
        out = build_bundle(s, tenant, "run-r", "rc", "admin-1", redact=True)
        s.commit()
        from app.engines.evidence.objects import read_object
        data = read_object(s, tenant, out["bundle_hash"])
    z = zipfile.ZipFile(io.BytesIO(data))
    assert b"secret reasoning" not in z.read("events.json") and b"secret reasoning" not in z.read("events.csv")
    m = json.loads(z.read("manifest.json"))
    assert m["redacted"] and "does NOT contain the entire original chain" in m["redaction_note"]
    b = ev_dirs / "r.zip"
    b.write_bytes(data)
    r = _cli(b, _pubfile(ev_dirs))
    assert r.returncode == 0 and "redacted_events=3" in r.stdout


def test_csv_formula_injection_escaped(built):
    _, _, data = built
    csvtxt = zipfile.ZipFile(io.BytesIO(data)).read("events.csv").decode()
    assert "'=HYPERLINK" in csvtxt and ",=HYPERLINK" not in csvtxt
    for v in ("=1+1", "+1", "-1", "@SUM(A1)", "\tx"):
        assert csv_escape(v).startswith("'")
    assert csv_escape("normal") == "normal"


def test_tamper_demo_uses_copy_only(built, tenant):
    t, out, data = built
    with SessionLocal() as s:
        before = s.execute(__import__("sqlalchemy").text("SELECT COUNT(*), MAX(event_hash) FROM ev_events WHERE tenant_id=:t"), {"t": t}).one()
        ok = tamper_demo(s, t, out["export_id"], "none")
        bad = {m: tamper_demo(s, t, out["export_id"], m) for m in ("modify_event", "drop_event", "truncate_chain_skeleton", "wrong_trusted_key")}
        after = s.execute(__import__("sqlalchemy").text("SELECT COUNT(*), MAX(event_hash) FROM ev_events WHERE tenant_id=:t"), {"t": t}).one()
    assert ok["verified"] is True
    assert all(v["verified"] is False and v["problems"] for v in bad.values())
    assert tuple(before) == tuple(after)
