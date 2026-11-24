from datetime import datetime, timedelta, timezone
from decimal import Decimal

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from app.engines.collateral import verifier
from app.engines.collateral.verifier import verify_legs

AS_OF = datetime(2026, 8, 17, 4, tzinfo=timezone.utc)
SRC = [{"source": "CLEARING_POLICY", "schedule": [
    {"asset_class": "CASH", "currency": "INR", "valuation_pct": "100"},
    {"asset_class": "GSEC", "currency": "INR", "valuation_pct": "90", "haircut_pct": "10", "min_rating": "AA"},
    {"asset_class": "CORP", "currency": "INR", "valuation_pct": "80", "min_rating": "A-", "concentration_cap_pct": "25",
     "cap_denominator": "TOTAL_ALLOCATED"}]}]


def mk(lot_id, cls, price="100", avail="1000", **kw):
    d = {"lot_id": lot_id, "asset_class": cls, "currency": "INR", "price": price, "available": avail, "entitled_to": ["ACC"],
         "rating": "AAA", "settlement_lag_days": 0, "issuer_group": kw.pop("issuer_group", None)}
    d.update(kw)
    return d


def ctx(required, lots_wrong=(), **kw):
    return {"required": required, "currency": "INR", "sources": SRC, "wrong_way_issuer_groups": list(lots_wrong),
            "obligation_id": "ACC", "as_of": AS_OF, "deadline": AS_OF + timedelta(days=2), "overpost_allowance": "0.01", **kw}


def names(res):
    return {f["name"] for f in res["failures"]}


def test_passes_when_everything_holds():
    lots = {"G": mk("G", "GSEC")}
    r = verify_legs([{"lot_id": "G", "quantity": "10"}], lots, ctx("900"))
    assert r["ok"] and Decimal(r["coverage"]) == Decimal("900")  # 10 * 100 * 0.9


def test_coverage_shortfall_blocks():
    r = verify_legs([{"lot_id": "G", "quantity": "10"}], {"G": mk("G", "GSEC")}, ctx("900.01"))
    assert not r["ok"] and "coverage >= required" in names(r)


def test_inventory_exceeded_blocks():
    r = verify_legs([{"lot_id": "G", "quantity": "11"}], {"G": mk("G", "GSEC", avail="10")}, ctx("900"))
    assert any("inventory" in n for n in names(r))


def test_wrong_way_and_entitlement_and_schedule_and_rating():
    lots = {"W": mk("W", "GSEC", issuer_group="IG-N"), "E": mk("E", "GSEC", entitled_to=["OTHER"]),
            "X": mk("X", "GOLDETF"), "R": mk("R", "GSEC", rating="BBB")}
    for lid, key in (("W", "not wrong-way"), ("E", "entitlement"), ("X", "eligible under every policy source"), ("R", "eligible under every policy source")):
        r = verify_legs([{"lot_id": lid, "quantity": "1"}], lots, ctx("0.0001", lots_wrong=["IG-N"]))
        assert any(key in n for n in names(r)), (lid, names(r))


def test_settlement_after_deadline_blocks():
    r = verify_legs([{"lot_id": "G", "quantity": "1"}], {"G": mk("G", "GSEC", settlement_lag_days=3)}, ctx("90"))
    assert any("settles by deadline" in n for n in names(r))


def test_concentration_cap_total_allocated_edge():
    lots = {"C": mk("C", "CORP"), "S": mk("S", "GSEC")}
    # CORP coverage 80*x; GSEC 90*y ; cap 25% of total: 80x <= 0.25*(80x+90y)
    ok = verify_legs([{"lot_id": "C", "quantity": "10"}, {"lot_id": "S", "quantity": "26.666667"}], lots, ctx("3200", overpost_allowance="1"))
    assert any(n.startswith("cap CLEARING_POLICY#2") for n in [c["name"] for c in ok["checks"] if c["ok"]]) or ok["ok"]
    bad = verify_legs([{"lot_id": "C", "quantity": "10"}, {"lot_id": "S", "quantity": "10"}], lots, ctx("1700", overpost_allowance="1"))
    assert any(n.startswith("cap CLEARING_POLICY#2") for n in names(bad))


def test_overposting_beyond_allowance_blocks():
    r = verify_legs([{"lot_id": "G", "quantity": "10"}], {"G": mk("G", "GSEC")}, ctx("500", overpost_allowance="1"))
    assert "over-posting within allowance" in names(r)


def test_duplicate_zero_and_precision_and_lot_size():
    lots = {"G": mk("G", "GSEC", lot_size="5")}
    r = verify_legs([{"lot_id": "G", "quantity": "0"}, {"lot_id": "G", "quantity": "7.1234567"}], lots, ctx("1"))
    n = names(r)
    assert any("unique" in x for x in n) and any("quantity > 0" in x for x in n)
    assert any("precision" in x for x in n) and any("lot size" in x for x in n)


def test_inconsistent_haircut_schedule_rejects_leg():
    bad_src = [{"source": "X", "schedule": [{"asset_class": "GSEC", "currency": "INR", "valuation_pct": "94", "haircut_pct": "94"}]}]
    r = verify_legs([{"lot_id": "G", "quantity": "1"}], {"G": mk("G", "GSEC")}, {**ctx("1"), "sources": bad_src})
    assert not r["ok"]


def test_cross_currency_fx_haircut_applied_once():
    src = [{"source": "P", "schedule": [{"asset_class": "GSEC", "currency": "USD", "valuation_pct": "94", "fx_haircut_pct": "8"}]}]
    lot = mk("U", "GSEC", price="100", avail="100", currency="USD", fx_to_target="1")
    r = verify_legs([{"lot_id": "U", "quantity": "100"}], {"U": lot}, {**ctx("8648"), "sources": src})
    assert r["ok"] and Decimal(r["coverage"]) == Decimal("8648.00")  # G06 86.48 per unit


@settings(max_examples=40, suppress_health_check=[HealthCheck.too_slow], deadline=None)
@given(req=st.integers(min_value=1, max_value=12000), cash=st.integers(0, 5000), gs=st.integers(0, 200), cp=st.integers(0, 200),
       rc=st.floats(0.01, 0.2), rg=st.floats(0.01, 0.2), rp=st.floats(0.01, 0.2))
def test_property_pipeline_feasible_implies_verifier_ok(req, cash, gs, cp, rc, rg, rp):
    """Whenever the pipeline claims a feasible allocation, the INDEPENDENT verifier agrees (zero false-feasible)."""
    from types import SimpleNamespace as NS
    from app.engines.collateral import engine as E
    from app.engines.collateral.eligibility import assess_lot
    lots = []
    for lid, cls, units, price, rate, grp in (("CA", "CASH", cash, "1", rc, None), ("GS", "GSEC", gs, "100", rg, "IG-S"),
                                              ("CP", "CORP", cp, "100", rp, "IG-C")):
        lots.append(NS(id=lid, lot_id=lid, asset_class=cls, currency="INR", issuer_class=None, issuer_group=grp,
                       units_u=units * 10**6, encumbered_u=0, reserved_u=0, lot_size_u=0, price=price, fx_to_target=None,
                       rating="AAA", maturity_date="2028-01-01", annual_cost_rate=str(round(rate, 6)), txn_cost_per_unit="0",
                       settlement_lag_days=0, entitled_to=["ACC"]))
    asm = [assess_lot(l, obligation_id="ACC", target_ccy="INR", sources=SRC, wrong_way_groups=[], as_of=AS_OF,
                      deadline=AS_OF + timedelta(days=2), horizon_days=30) for l in lots]
    el = [a for a in asm if a.eligible]
    raw = {l.lot_id: {"lot_id": l.lot_id, "asset_class": l.asset_class, "currency": "INR", "issuer_class": None,
                      "issuer_group": l.issuer_group, "price": l.price, "fx_to_target": None, "rating": "AAA",
                      "maturity_date": l.maturity_date, "settlement_lag_days": 0, "entitled_to": ["ACC"],
                      "available": str(Decimal(l.units_u) / 10**6), "lot_size": "0"} for l in lots}
    params = {"sources": SRC, "wrong_way_issuer_groups": [], "obligation_id": "ACC", "currency": "INR", "as_of": AS_OF.isoformat(),
              "deadline": (AS_OF + timedelta(days=2)).isoformat(), "required": str(req), "overpost_allowance": "0", "stress": None}
    groups, meta = E.build_groups(el, SRC)
    res = E.solve_pipeline(el, Decimal(req), groups, meta, raw, params)
    if res["solver_status"] in ("optimal", "feasible"):
        assert res["verification"]["ok"], res["verification"]["failures"]
        assert Decimal(res["coverage"]) >= Decimal(req)
