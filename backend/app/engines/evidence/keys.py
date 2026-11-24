"""Local Ed25519 signing keys for evidence checkpoints and bundle manifests.

LIMITS (disclosed, see docs/methodologies/evidence.md): this is a LOCAL FILE KEY. It is not an HSM, not
hardware-protected, and anyone with filesystem access to `signing_key_dir` can sign. It gives tamper-evidence only
when the verifier holds the public key and checkpoints are kept outside the DB write boundary.

Layout in settings.signing_key_dir:
  ev-<key_id>.pem   private key (PEM, unencrypted, chmod 600 where supported)
  ev-<key_id>.pub   public key (PEM) - retained forever for verifying historical checkpoints
  CURRENT           key_id of the signing key
Rotation: `rotate()` creates a new key and points CURRENT at it; old keys remain for verification only.
Recovery: restore the key directory from the separately protected backup; without the private key no NEW
checkpoints can be signed under the old key_id, but old signatures stay verifiable from the .pub files.
"""
from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from app.core.config import get_settings


def _dir() -> Path:
    p = Path(get_settings().signing_key_dir)
    p.mkdir(parents=True, exist_ok=True)
    return p


def key_id_of(pub: Ed25519PublicKey) -> str:
    raw = pub.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return hashlib.sha256(raw).hexdigest()[:16]


def pub_pem(pub: Ed25519PublicKey) -> str:
    return pub.public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()


def _generate() -> str:
    d = _dir()
    priv = Ed25519PrivateKey.generate()
    kid = key_id_of(priv.public_key())
    pem = priv.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    f = d / f"ev-{kid}.pem"
    f.write_bytes(pem)
    try:
        os.chmod(f, 0o600)
    except OSError:
        pass
    (d / f"ev-{kid}.pub").write_text(pub_pem(priv.public_key()))
    (d / "CURRENT").write_text(kid)
    return kid


def current_key_id() -> str:
    f = _dir() / "CURRENT"
    if f.exists():
        kid = f.read_text().strip()
        if (_dir() / f"ev-{kid}.pem").exists():
            return kid
    return _generate()


def rotate() -> str:
    return _generate()


def _private(kid: str) -> Ed25519PrivateKey:
    return serialization.load_pem_private_key((_dir() / f"ev-{kid}.pem").read_bytes(), password=None)  # type: ignore[return-value]


def sign(message: bytes) -> tuple[str, str]:
    """Returns (key_id, base64 signature)."""
    kid = current_key_id()
    return kid, base64.b64encode(_private(kid).sign(message)).decode()


def load_public(kid: str) -> Ed25519PublicKey | None:
    f = _dir() / f"ev-{kid}.pub"
    if not f.exists():
        return None
    return serialization.load_pem_public_key(f.read_bytes())  # type: ignore[return-value]


def verify(kid: str, message: bytes, sig_b64: str, pub: Ed25519PublicKey | None = None) -> bool:
    pub = pub or load_public(kid)
    if pub is None:
        return False
    try:
        pub.verify(base64.b64decode(sig_b64), message)
        return True
    except Exception:  # noqa: BLE001 - InvalidSignature / bad base64 => not verified
        return False


def public_keys() -> list[dict]:
    cur = current_key_id()
    out = []
    for f in sorted(_dir().glob("ev-*.pub")):
        kid = f.stem[3:]
        out.append({"key_id": kid, "current": kid == cur, "algorithm": "Ed25519", "public_key_pem": f.read_text(),
                    "protection": "LOCAL_FILE_KEY_NOT_HSM"})
    return out
