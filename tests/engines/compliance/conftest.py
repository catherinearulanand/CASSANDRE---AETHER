"""Fixtures for SENTINEL tests. Fakes of risk/collateral/regulatory services are injected ONLY here (unit/integration
tests); no production code path contains stub results."""
import uuid

import pytest

from app.auth.security import Principal
from app.core import services
from app.db.base import SessionLocal

CTX = {"market_type": "ETD_DOMESTIC", "venue": "DEMO_NSE", "account_id": "ACC-DEMO-NSE-001", "currency": "INR",
       "as_of": "2026-08-17T04:00:00Z"}
IFSC_CTX = {"market_type": "ETD_IFSC", "venue": "DEMO_IFSC", "account_id": "ACC-DEMO-IFSC-001", "currency": "USD",
            "as_of": "2026-08-17T04:00:00Z"}


class FakeUpstream:
    """Mutable fake of risk.get_result / collateral.coverage used to drive deterministic scenarios."""
    def __init__(self):
        self.required = {"amount": "12500000", "currency": "INR"}
        self.posted = {"amount": "11500000", "currency": "INR"}
        self.input_as_of = "2026-08-17T03:00:00Z"
        self.status = "OK"
        self.risk_down = False
        self.extra = {}

    def get_result(self, session, tenant_id, result_id):
        if self.risk_down:
            from app.core.errors import Unavailable
            raise Unavailable("risk down")
        return {"result_id": result_id, "status": self.status, "required_amount": self.required, "as_of": self.input_as_of, **self.extra}

    def coverage(self, session, tenant_id, account_id, as_of):
        return {"eligible_posted": self.posted, "as_of": as_of.isoformat()}


@pytest.fixture
def up():
    f = FakeUpstream()
    saved = {k: services._REGISTRY.get(k) for k in ("risk.get_result", "collateral.coverage", "regulatory.resolve_rules")}
    services.register("risk.get_result", f.get_result)
    services.register("collateral.coverage", f.coverage)
    services._REGISTRY.pop("regulatory.resolve_rules", None)
    yield f
    for k, v in saved.items():
        if v is None:
            services._REGISTRY.pop(k, None)
        else:
            services.register(k, v)


@pytest.fixture
def db():
    s = SessionLocal()
    yield s
    s.rollback()
    s.close()


@pytest.fixture
def tenant(db):
    from app.engines.compliance.fixtures import seed_fixtures
    t = "cmp-" + uuid.uuid4().hex[:10]
    seed_fixtures(db, t)
    db.commit()
    return t


def P(tenant, uid, *roles):
    return Principal(user_id=uid, tenant_id=tenant, username=uid, display_name=uid, roles=tuple(roles), entitlements=(), csrf_token="x")
