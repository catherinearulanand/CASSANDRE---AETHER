"""Seed two synthetic tenants and role users. Passwords are generated randomly and written to
.demo-credentials (gitignored). No production defaults exist."""
from __future__ import annotations
import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.auth.security import hash_password, new_password
from app.core.config import REPO_ROOT
from app.db.base import SessionLocal, create_all
from app.db.models_core import Tenant, User
from sqlalchemy import select

TENANTS = {"tenant-a": "Meridian Demo Bank (synthetic)", "tenant-b": "Northwind Demo Clearing (synthetic)"}
USERS = [("viewer", ["viewer"]), ("maker", ["maker"]), ("checker", ["checker"]),
         ("compliance", ["compliance_officer"]), ("auditor", ["auditor"]), ("admin", ["tenant_admin"])]

def main():
    create_all()
    creds = {}
    with SessionLocal() as s:
        for tid, name in TENANTS.items():
            if s.get(Tenant, tid) is None:
                s.add(Tenant(tenant_id=tid, name=name))
            for uname, roles in USERS:
                if s.execute(select(User).where(User.tenant_id == tid, User.username == uname)).scalar_one_or_none():
                    continue
                pw = new_password()
                s.add(User(tenant_id=tid, username=uname, display_name=f"{uname.title()} ({tid})", password_hash=hash_password(pw),
                           roles=roles, entitlements=["OTC", "ETD_DOMESTIC", "ETD_IFSC"]))
                creds.setdefault(tid, {})[uname] = pw
        s.commit()
    if creds:
        p = REPO_ROOT / ".demo-credentials"
        old = json.loads(p.read_text()) if p.exists() else {}
        for t, u in creds.items():
            old.setdefault(t, {}).update(u)
        p.write_text(json.dumps(old, indent=2))
        print(f"seeded; new credentials written to {p}")
    else:
        print("already seeded; nothing changed")

if __name__ == "__main__":
    main()
