"""Seed the full synthetic demo for one tenant: users (seed.py), then every engine's fixtures in dependency order.
Idempotent. Usage: python scripts/seed_demo.py [tenant-id]"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db.base import SessionLocal, create_all  # noqa: E402
from app.engines.collateral import fixtures as col  # noqa: E402
from app.engines.compliance import fixtures as cmp_  # noqa: E402
from app.engines.contracts import fixtures as con  # noqa: E402
from app.engines.regulatory import fixtures as reg  # noqa: E402
from app.engines.risk import fixtures as risk  # noqa: E402


def seed_engines(tenant_id: str) -> dict:
    """Order matters: rules -> terms -> market/trades -> inventory/posted collateral -> controls."""
    out = {}
    for name, fn in (("regulatory", reg.seed_fixtures), ("contracts", con.seed_fixtures), ("risk", risk.seed_fixtures),
                     ("collateral", col.seed_fixtures), ("compliance", cmp_.seed_fixtures)):
        with SessionLocal() as s:
            out[name] = fn(s, tenant_id)
            s.commit()
    return out


if __name__ == "__main__":
    import seed  # users + tenants (writes .demo-credentials)

    create_all()
    seed.main()
    t = sys.argv[1] if len(sys.argv) > 1 else "tenant-a"
    res = seed_engines(t)
    print({k: (v if isinstance(v, dict) else f"{len(v)} items") for k, v in res.items()})
