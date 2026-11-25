"""The bulk import rewrite (one existence query per chunk instead of per row) must keep the exact semantics:
identical content is idempotent, changed content is a 409-class Conflict, duplicates inside a batch are handled,
batches larger than the 500-row chunk work, and a rejected batch commits nothing (atomic)."""
import uuid

import pytest
from sqlalchemy import func, select

from app.core.errors import Conflict, ValidationFailed
from app.db.base import SessionLocal
from app.engines.risk import services as svc
from app.engines.risk.models import RiskInstrument, RiskPosition, RiskTrade

N = 1234  # > 2 chunks of 500


def inst(k):
    return {"instrument_id": f"BULK-FUT-{k:05d}", "type": "FUT", "venue": "DEMO_NSE", "currency": "INR", "multiplier": "25", "expiry": "2026-09-29", "settlement_type": "CASH"}


def pos(k, rev=1, lots=None):
    return {"account_id": "ACC-BULK", "instrument_id": f"BULK-FUT-{k:05d}", "as_of": "2026-08-17T03:30:00Z", "revision": rev,
            "signed_lots": str(lots if lots is not None else (k % 17) - 8), "settlement_price": "24000.00"}


def trade(k):
    return {"trade_id": f"BULK-T-{k:05d}", "master_id": "MA-BULK", "netting_set_id": "NS-BULK", "type": "FXF", "side": "BUY", "base_ccy": "EUR", "quote_ccy": "USD",
            "notional_base": str(100_000 + k), "strike": "1.10", "maturity": "2026-12-17", "curve_base": "EUR-FLAT0", "curve_quote": "USD-FLAT0"}


@pytest.fixture
def tenant():
    return f"bulk-{uuid.uuid4().hex[:10]}"


def counts(t):
    with SessionLocal() as s:
        return tuple(s.execute(select(func.count()).select_from(m).where(m.tenant_id == t)).scalar_one() for m in (RiskInstrument, RiskPosition, RiskTrade))


def test_large_batch_imports_everything_and_is_idempotent(tenant):
    with SessionLocal() as s:
        r = svc.import_portfolio(s, tenant, instruments=[inst(k) for k in range(N)], positions=[pos(k) for k in range(N)], trades=[trade(k) for k in range(N)])
        s.commit()
    assert r["added"] == {"trades": N, "instruments": N, "positions": N} and counts(tenant) == (N, N, N)
    with SessionLocal() as s:  # identical retry: nothing added, nothing duplicated, no new evidence event
        again = svc.import_portfolio(s, tenant, instruments=[inst(k) for k in range(N)], positions=[pos(k) for k in range(N)], trades=[trade(k) for k in range(N)])
        s.commit()
    assert again["added"] == {"trades": 0, "instruments": 0, "positions": 0} and counts(tenant) == (N, N, N)


def test_changed_content_is_a_conflict_and_commits_nothing(tenant):
    with SessionLocal() as s:
        svc.import_portfolio(s, tenant, instruments=[inst(k) for k in range(700)], positions=[pos(k) for k in range(700)], trades=[trade(k) for k in range(700)])
        s.commit()
    for kw in ({"positions": [pos(650, lots=99)]}, {"instruments": [dict(inst(650), multiplier="50")]}, {"trades": [dict(trade(650), strike="1.99")]}):
        with SessionLocal() as s:
            with pytest.raises(Conflict):
                svc.import_portfolio(s, tenant, **kw)
            s.rollback()
    assert counts(tenant) == (700, 700, 700)
    # a NEW revision of the same position is allowed (revised file => new version)
    with SessionLocal() as s:
        svc.import_portfolio(s, tenant, positions=[pos(650, rev=2, lots=99)])
        s.commit()
    assert counts(tenant)[1] == 701


def test_duplicates_inside_one_batch(tenant):
    with SessionLocal() as s:
        r = svc.import_portfolio(s, tenant, instruments=[inst(1), inst(1)], positions=[pos(1), pos(1)])  # identical duplicate: collapses
        s.commit()
    assert r["added"]["instruments"] == 1 and r["added"]["positions"] == 1
    with SessionLocal() as s:
        with pytest.raises(Conflict):
            svc.import_portfolio(s, tenant, positions=[pos(2, lots=1), pos(2, lots=2)])  # conflicting duplicate in one batch
        s.rollback()
        with pytest.raises(Conflict):
            svc.import_portfolio(s, tenant, instruments=[inst(3), dict(inst(3), multiplier="75")])
        s.rollback()
        with pytest.raises(ValidationFailed):
            svc.import_portfolio(s, tenant, trades=[trade(4), trade(4)])  # duplicate trade_id is a validation error (unchanged behaviour)


def test_one_bad_row_rejects_the_whole_large_batch(tenant):
    bad = [pos(k) for k in range(600)] + [dict(pos(601), signed_lots="not-a-number")]
    with SessionLocal() as s:
        with pytest.raises(ValidationFailed):
            svc.import_portfolio(s, tenant, positions=bad)
        s.rollback()
    assert counts(tenant) == (0, 0, 0)  # atomic: nothing from the good 600 rows was committed
