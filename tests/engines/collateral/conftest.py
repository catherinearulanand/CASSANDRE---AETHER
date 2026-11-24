"""Collateral engine test support. Fake upstream providers (risk.get_result / contracts.resolve_terms / ...) are injected
ONLY here, in tests; they are restored after each test. Production paths return explicit Unavailable when absent."""
from __future__ import annotations

import itertools
import uuid

import pytest

from app.auth.security import Principal
from app.core import services

_n = itertools.count(1)


def uid(prefix: str) -> str:
    return f"{prefix}-{next(_n)}-{uuid.uuid4().hex[:6]}"


class Upstream:
    """Mutable fake of the risk / contracts / regulatory services."""

    def __init__(self):
        self.margins: dict[str, dict] = {}
        self.terms: dict[str, dict] = {}
        self.rule_params: dict[str, dict] = {}

    def add_margin(self, **kw) -> str:
        rid = kw.pop("result_id", uid("MR"))
        m = {"result_id": rid, "status": "OK", "market_type": "ETD_DOMESTIC", "currency": "INR", "account_id": "ACC-X",
             "direction": "POST", "required_amount": {"amount": "100", "currency": "INR"}, "rounded_amount": "100",
             "result_hash": uuid.uuid4().hex + uuid.uuid4().hex, "terms_hash": None, "rule_set_hash": None,
             "blocking_reasons": [], **kw}
        self.margins[rid] = m
        return rid


@pytest.fixture
def up(app_client):
    u = Upstream()
    saved = {k: services._REGISTRY.get(k) for k in ("risk.get_result", "contracts.resolve_terms", "regulatory.rule_parameters")}
    services.register("risk.get_result", lambda s, t, rid: u.margins.get(rid))
    services.register("contracts.resolve_terms", lambda s, t, ns, as_of, known_at, fact=None: u.terms.get(ns, {"status": "BLOCKED", "blocking_reasons": ["no terms"], "terms_hash": None}))
    services.register("regulatory.rule_parameters", lambda s, t, h: u.rule_params.get(h, {}))
    yield u
    for k, v in saved.items():
        if v is None:
            services._REGISTRY.pop(k, None)
        else:
            services._REGISTRY[k] = v


@pytest.fixture
def db(app_client):
    from app.db.base import SessionLocal
    s = SessionLocal()
    try:
        yield s
    finally:
        s.rollback()
        s.close()


def principal(user: str, *roles: str, tenant: str = "tenant-a") -> Principal:
    return Principal(user_id=user, tenant_id=tenant, username=user, display_name=user, roles=tuple(roles),
                     entitlements=(), csrf_token="x")


MAKER = principal("u-maker", "maker")
CHECKER = principal("u-checker", "checker")
T = "tenant-a"
AS_OF = "2026-08-17T04:00:00Z"
DEADLINE = "2026-08-19T04:00:00Z"


def simple_policy(*entries: dict, **extra) -> dict:
    return {"schedule": list(entries), **extra}


def lot(lot_id, asset_class, units, price="1", rate="0", *, txn="0", ccy="INR", entitled=("ACC-X",), lag=0, **kw) -> dict:
    d = {"lot_id": lot_id, "owner_entity": "ENT-MERIDIAN", "custody_account": "CUST-T", "asset_class": asset_class,
         "currency": ccy, "units": units, "price": price, "annual_cost_rate": rate, "txn_cost_per_unit": txn,
         "settlement_lag_days": lag, "entitled_to": list(entitled)}
    d.update(kw)
    return d


def snapshot(db, sid, lots, policy, *, ccy="INR", market="ETD_DOMESTIC", wrong=(), tenant=T) -> dict:
    from app.engines.collateral import services as S
    out = S.import_inventory(db, tenant, actor_id="u-maker", role="maker", correlation_id="c-test", payload={
        "snapshot_id": sid, "as_of": AS_OF, "currency": ccy, "market_type": market, "policy": policy,
        "wrong_way_issuer_groups": list(wrong), "lots": lots})
    db.commit()
    return out
