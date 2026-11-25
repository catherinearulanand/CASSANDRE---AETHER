"""Provision a private demo database for the COVENANT browser flows (does NOT touch .demo-credentials or other workers' DBs).
Usage (env AETHER_DATABASE_URL must point at YOUR database, AETHER_AUTO_CREATE_SCHEMA=true):
    python tests/e2e/contracts_setup.py   -> creates tenants/users, seeds COVENANT fixtures, writes var/a2.creds.json (gitignored)"""
import json
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from sqlalchemy import select  # noqa: E402

from app.auth.security import hash_password  # noqa: E402
from app.db.base import SessionLocal, create_all  # noqa: E402
from app.db.models_core import Tenant, User  # noqa: E402
from app.engines.contracts import fixtures as F  # noqa: E402

USERS = [("viewer", ["viewer"]), ("maker", ["maker"]), ("maker2", ["maker"]), ("checker", ["checker"]),
         ("dual", ["maker", "checker"]), ("admin", ["tenant_admin"])]

create_all()
creds = {}
with SessionLocal() as s:
    for t in ("tenant-a", "tenant-b"):
        if s.get(Tenant, t) is None:
            s.add(Tenant(tenant_id=t, name=t))
        for u, roles in USERS:
            if s.execute(select(User).where(User.tenant_id == t, User.username == u)).scalar_one_or_none():
                continue
            pw = secrets.token_urlsafe(12)
            s.add(User(tenant_id=t, username=u, display_name=f"{u} ({t})", password_hash=hash_password(pw), roles=roles,
                       entitlements=["OTC"]))
            creds.setdefault(t, {})[u] = pw
    s.commit()
with SessionLocal() as s:
    out = F.seed_fixtures(s, "tenant-a")
    s.commit()
f = ROOT / "var" / "a2.creds.json"
old = json.loads(f.read_text()) if f.exists() else {}
for t, u in creds.items():
    old.setdefault(t, {}).update(u)
f.parent.mkdir(exist_ok=True)
f.write_text(json.dumps(old, indent=1))
print("provisioned; new users:", {t: sorted(u) for t, u in creds.items()}, "| packages:", sorted(out["packages"]))
