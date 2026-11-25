"""Seeds a PRIVATE demo database for the risk browser flows without touching the shared .demo-credentials file.
Usage (from repo root):  AETHER_DATABASE_URL=<db url> AETHER_AUTO_CREATE_SCHEMA=true python tests/e2e/risk_seed.py [creds_out.json]
Writes {tenant: {user: password}} (random passwords, gitignored location recommended: var/) and seeds all engines' synthetic fixtures."""
import json
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "backend" / "scripts"))

from app.auth.security import hash_password  # noqa: E402
from app.db.base import SessionLocal, create_all  # noqa: E402
from app.db.models_core import Tenant, User  # noqa: E402
from seed_demo import seed_engines  # noqa: E402
from sqlalchemy import select  # noqa: E402

out = Path(sys.argv[1] if len(sys.argv) > 1 else ROOT / "var" / "a3-creds.json")
create_all()
creds: dict = {}
with SessionLocal() as s:
    for tid in ("tenant-a", "tenant-b"):
        if s.get(Tenant, tid) is None:
            s.add(Tenant(tenant_id=tid, name=f"{tid} (synthetic)"))
        for uname, roles in (("viewer", ["viewer"]), ("maker", ["maker"]), ("checker", ["checker"]), ("admin", ["tenant_admin"])):
            if s.execute(select(User).where(User.tenant_id == tid, User.username == uname)).scalar_one_or_none():
                continue
            pw = secrets.token_urlsafe(12)
            s.add(User(tenant_id=tid, username=uname, display_name=f"{uname} ({tid})", password_hash=hash_password(pw), roles=roles,
                       entitlements=["OTC", "ETD_DOMESTIC", "ETD_IFSC"]))
            creds.setdefault(tid, {})[uname] = pw
    s.commit()
if creds:
    out.parent.mkdir(parents=True, exist_ok=True)
    old = json.loads(out.read_text()) if out.exists() else {}
    for t, u in creds.items():
        old.setdefault(t, {}).update(u)
    out.write_text(json.dumps(old, indent=2))
res = seed_engines("tenant-a")
print("seeded:", {k: (v if not isinstance(v, dict) else sorted(v)[:3]) for k, v in res.items()}, "| creds:", out)
