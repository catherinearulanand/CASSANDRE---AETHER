"""Deterministic synthetic fixtures for the compliance engine. Idempotent; call from the lead's demo seeder."""
from __future__ import annotations

from app.engines.compliance.controls import seed_library


def seed_fixtures(session, tenant_id: str) -> dict:
    ctrls = seed_library(session, tenant_id)
    return {"controls": len(ctrls), "supported": sum(1 for c in ctrls if c.support_status == "SUPPORTED"),
            "unsupported": sum(1 for c in ctrls if c.support_status != "SUPPORTED"),
            "note": "Surveillance model is trained on demand via POST /compliance/surveillance/run (seed 42)."}
