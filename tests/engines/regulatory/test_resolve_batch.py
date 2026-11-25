"""regulatory.resolve_rules_batch (additive v1.1) must return EXACTLY what N single resolve_rules calls return."""
import pytest
from sqlalchemy import select

from app.core import services
from app.core.errors import ValidationFailed
from app.engines.regulatory.fixtures import seed_fixtures
from app.engines.regulatory.models import RegRule
from app.engines.regulatory.resolve import resolve_rules, resolve_rules_batch
from tests.engines.regulatory.helpers import dt

ASOF = dt("2026-08-17T04:00:00Z")
CTXS = [
    {"market_type": "ETD_DOMESTIC", "venue": "DEMO_NSE", "currency": "INR", "account_id": "A1"},
    {"market_type": "ETD_DOMESTIC", "venue": "DEMO_NSE", "currency": "INR", "account_id": "A2", "entity_category": "CLEARING_MEMBER"},
    {"market_type": "ETD_IFSC", "venue": "DEMO_IFSC", "currency": "USD", "account_id": "A3"},
    {"market_type": "OTC", "currency": "USD", "account_id": "A4"},
    {"venue": "DEMO_NSE", "account_id": "A5"},   # market_type missing => scope UNKNOWN, never applicable
    {},
]


def strip(r):  # results are plain JSON-able dicts; compare structurally
    return r


def test_batch_equals_single_calls_official_and_hypothetical(env):
    seed_fixtures(env.db, env.tenant)
    v2 = env.db.execute(select(RegRule).where(RegRule.tenant_id == env.tenant, RegRule.rule_key == "DEMO-ETD-MARGIN", RegRule.version == 2)).scalar_one()
    for hyp in (None, [v2.rule_id]):
        single = [resolve_rules(env.db, env.tenant, c, ASOF, None, hyp) for c in CTXS]
        batch = resolve_rules_batch(env.db, env.tenant, CTXS, ASOF, None, hyp)
        assert batch == single
        assert [b["rule_set_hash"] for b in batch] == [s["rule_set_hash"] for s in single]
    # missing context is UNKNOWN in the batch too (never applicable)
    b = resolve_rules_batch(env.db, env.tenant, CTXS, ASOF)
    assert not any(e["rule_key"] == "DEMO-ETD-MARGIN" for e in b[4]["applicable"]) and b[4]["unknown"]


def test_batch_registered_as_service_bounded_and_preserves_order(env):
    seed_fixtures(env.db, env.tenant)
    out = services.call("regulatory.resolve_rules_batch", env.db, env.tenant, CTXS, ASOF, None)
    assert len(out) == len(CTXS) and out[0]["rule_set_hash"] == resolve_rules(env.db, env.tenant, CTXS[0], ASOF)["rule_set_hash"]
    with pytest.raises(ValidationFailed):
        resolve_rules_batch(env.db, env.tenant, [{}] * 10_001, ASOF)
