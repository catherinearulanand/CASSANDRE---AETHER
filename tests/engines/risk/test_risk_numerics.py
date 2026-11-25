"""Round 2 numerical hardening: negative-rate curves, missing fixings, malformed curves, expired instruments, FX orientation
(property tests) and an INDEPENDENT Decimal reconciliation of the float Black-Scholes analytics.
Expected values are derived here from closed-form formulas with Decimal arithmetic, never by calling the code under test."""
import math
import uuid
from datetime import date
from decimal import Decimal, localcontext

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.core import services
from app.core.canonical import content_hash
from app.core.errors import ValidationFailed
from app.db.base import SessionLocal
from app.engines.risk import etd
from app.engines.risk import fixtures as fx
from app.engines.risk import services as svc
from app.engines.risk.pricing import MarketData, PricingError, shock_payload, validate_snapshot_payload, value_fxf, value_irs

D = Decimal
VAL = date(2026, 8, 17)
PREC = 60


def dexp(x):
    with localcontext() as c:
        c.prec = PREC
        return D(x).exp()


def flat_curve(ccy, rate, tmax=3):
    """Continuously compounded flat zero rate (negative allowed): DF(t) = exp(-rate t); ln DF is linear so log-linear interpolation is exact."""
    pts = [["0", "1"]] + [[str(t), str(dexp(-D(rate) * t))] for t in range(1, tmax + 1)]
    return {"currency": ccy, "points": pts}


def md_of(curves=None, fx_=None, fixings=None, **extra):
    return MarketData({"curves": curves or {}, "fx": fx_ or {}, "fixings": fixings or {}, **extra})


def irs(**kw):
    t = {"trade_id": "T1", "master_id": "M", "type": "IRS", "pay_receive": "PAY_FIXED", "notional": "1000000", "currency": "USD", "fixed_rate": "0.02",
         "effective": "2026-08-17", "maturity": "2027-08-17", "freq_months": 12, "fixed_dc": "ACT/360", "float_dc": "ACT/360", "curve_disc": "C", "curve_proj": "C"}
    t.update(kw)
    return t


# ------------------------------------------------------------------------------------------------ negative rates
def test_negative_rate_curve_is_valid_and_prices_to_the_closed_form():
    snap = {"curves": {"C": flat_curve("USD", "-0.005")}}
    assert validate_snapshot_payload(snap) == []          # negative rates are legal: DF > 1 must NOT be rejected
    df1 = dexp("0.005")
    assert df1 > 1
    n, k = D(1_000_000), D("0.02")
    expected = n * (1 - df1) - n * k * (D(365) / D(360)) * df1   # PAY_FIXED: PV_float - PV_fixed, single annual coupon, disc = proj
    got = value_irs(irs(), md_of(snap["curves"]), VAL, "T1")
    assert abs(got - expected) < D("1e-25"), (got, expected)
    assert got < 0 and value_irs(irs(pay_receive="RECEIVE_FIXED"), md_of(snap["curves"]), VAL, "T1") == -got   # sign symmetry


def test_negative_fixed_rate_trade_is_accepted_and_priced():
    t = irs(fixed_rate="-0.0025")
    assert svc.validate_trade(t) == []
    df1 = dexp("0.005")
    expected = D(1_000_000) * (1 - df1) - D(1_000_000) * D("-0.0025") * (D(365) / D(360)) * df1
    assert abs(value_irs(t, md_of({"C": flat_curve("USD", "-0.005")}), VAL, "T1") - expected) < D("1e-25")


def test_fx_forward_with_negative_base_rate_hand_value():
    mat = date(2027, 2, 17)
    t_years = D((mat - VAL).days) / D(365)                # 184 days
    s, k, n = D("1.10"), D("1.12"), D(2_000_000)
    md = md_of({"EUR": flat_curve("EUR", "-0.005"), "USD": flat_curve("USD", "0.03")}, {"EURUSD": "1.10"})
    trade = {"base_ccy": "EUR", "quote_ccy": "USD", "notional_base": "2000000", "strike": "1.12", "maturity": "2027-02-17", "curve_base": "EUR", "curve_quote": "USD", "side": "BUY"}
    expected = n * (s * dexp(D("0.005") * t_years) - k * dexp(-D("0.03") * t_years))
    assert abs(value_fxf(trade, md, VAL) - expected) < D("1e-22")


def test_big_negative_parallel_shock_keeps_snapshot_valid():
    base = {"curves": {"C": flat_curve("USD", "0.01")}}
    shocked = shock_payload(base, {"curve_shift_bp": "-400"})   # rates become -3%
    assert validate_snapshot_payload(shocked) == [] and D(shocked["curves"]["C"]["points"][1][1]) > 1
    assert base["curves"]["C"]["points"][1][1] == flat_curve("USD", "0.01")["points"][1][1]  # baseline untouched


# ------------------------------------------------------------------------------------------------ malformed curves
BAD = {
    "non-increasing time": [["0", "1"], ["2", "0.95"], ["1", "0.97"]],
    "duplicate time": [["0", "1"], ["1", "0.97"], ["1", "0.96"]],
    "zero DF": [["0", "1"], ["1", "0"]],
    "negative DF": [["0", "1"], ["1", "-0.5"]],
    "DF at t=0 is not 1": [["0", "0.99"], ["1", "0.97"]],
    "single point": [["1", "0.97"]],
    "NaN DF": [["0", "1"], ["1", "NaN"]],
    "infinite DF": [["0", "1"], ["1", "Infinity"]],
    "not a number": [["0", "1"], ["1", "abc"]],
    "float DF (not a decimal string)": [["0", "1"], ["1", 0.97]],
    "negative time": [["-1", "1.01"], ["1", "0.97"]],
}


@pytest.mark.parametrize("name", sorted(BAD))
def test_malformed_curve_is_rejected_with_a_reason(name):
    errs = validate_snapshot_payload({"curves": {"C": {"currency": "USD", "points": BAD[name]}}})
    assert errs and any("curve C" in e for e in errs), (name, errs)


def test_curve_without_currency_and_fx_reciprocity_and_bad_marks_rejected():
    assert any("currency missing" in e for e in validate_snapshot_payload({"curves": {"C": {"points": [["0", "1"], ["1", "0.97"]]}}}))
    assert validate_snapshot_payload({"fx": {"EURUSD": "1.10", "USDEUR": "0.909090909"}}) == []                # within 1e-6
    assert any("reciprocal" in e for e in validate_snapshot_payload({"fx": {"EURUSD": "1.10", "USDEUR": "0.5"}}))
    assert any("positive" in e for e in validate_snapshot_payload({"fx": {"EURUSD": "-1.1"}}))
    assert validate_snapshot_payload({"marks": {"X": "0"}}) and validate_snapshot_payload({"vols": {"X": "-0.1"}})


def test_pin_snapshot_service_rejects_malformed_curve_and_pins_nothing():
    t = f"num-{uuid.uuid4().hex[:8]}"
    with SessionLocal() as s:
        with pytest.raises(ValidationFailed):
            svc.pin_snapshot(s, t, "SNAP-BAD", date_to_dt(), {"curves": {"C": {"currency": "USD", "points": BAD["non-increasing time"]}}})
        s.rollback()


def date_to_dt():
    from datetime import datetime, timezone
    return datetime(2026, 8, 17, 4, 0, tzinfo=timezone.utc)


# ------------------------------------------------------------------------------------------------ missing fixings
def test_missing_fixing_blocks_and_present_fixing_matches_hand_value():
    t = irs(effective="2026-08-17", maturity="2027-08-17", freq_months=12)
    val = date(2026, 9, 17)                                # inside the accrual period: the coupon has already reset
    md_missing = md_of({"C": flat_curve("USD", "0.03")})
    with pytest.raises(PricingError, match="missing fixing T1\\|2026-08-17"):
        value_irs(t, md_missing, val, "T1")
    wrong_key = md_of({"C": flat_curve("USD", "0.03")}, fixings={"T1|2026-08-18": "0.04"})   # fixing for a different date must not be used
    with pytest.raises(PricingError, match="missing fixing"):
        value_irs(t, wrong_key, val, "T1")
    md = md_of({"C": flat_curve("USD", "0.03")}, fixings={"T1|2026-08-17": "0.04"})
    days_pay = (date(2027, 8, 17) - val).days                 # 334
    df = dexp(-D("0.03") * D(days_pay) / D(365))
    acc = D((date(2027, 8, 17) - date(2026, 8, 17)).days) / D(360)
    n = D(1_000_000)
    expected = n * D("0.04") * acc * df - n * D("0.02") * acc * df      # PAY_FIXED: fixed-in-arrears float minus fixed leg
    assert abs(value_irs(t, md, val, "T1") - expected) < D("1e-22")


def test_paid_coupons_are_excluded_and_fixings_are_not_needed_for_them():
    t = irs(effective="2026-02-17", maturity="2027-02-17", freq_months=6)
    md = md_of({"C": flat_curve("USD", "0.03")})
    # val = 2026-08-17: first coupon paid (end <= val), second starts exactly on val (forward-looking): no fixing required anywhere
    pv = value_irs(t, md, VAL, "T1")
    days = (date(2027, 2, 17) - VAL).days
    df = dexp(-D("0.03") * D(days) / D(365))
    n = D(1_000_000)
    expected = n * (1 - df) / df * df - n * D("0.02") * (D(days) / D(360)) * df
    assert abs(pv - expected) < D("1e-22")


# ------------------------------------------------------------------------------------------------ expired instruments
@pytest.fixture
def tenant():
    t = f"num-{uuid.uuid4().hex[:10]}"
    with SessionLocal() as s:
        fx.seed_fixtures(s, t)
        s.commit()
    return t


TERMS = {"base_currency": "USD", "threshold_a": "10000000", "threshold_b": "10000000", "mta_a": "500000", "mta_b": "500000", "independent_amount_a": "0",
         "independent_amount_b": "0", "rounding_increment": "100000", "rounding_mode": "UP", "mta_semantics": "GTE"}


@pytest.fixture(autouse=True)
def terms_provider(monkeypatch):
    monkeypatch.setitem(services._REGISTRY, "contracts.resolve_terms", lambda s, t, ns, a, k, f=None: {"status": "RESOLVED", "terms_hash": content_hash(TERMS), "terms": dict(TERMS), "blocking_reasons": []})


def test_expired_trade_in_a_netting_set_blocks_the_whole_result_never_zero(tenant):
    expired = {"trade_id": "TRD-EXPIRED", "master_id": "MA-MER-NWD-001", "netting_set_id": "NS-MER-NWD-001", "type": "FXF", "side": "BUY", "base_ccy": "EUR",
               "quote_ccy": "USD", "notional_base": "1000000", "strike": "1.10", "maturity": "2026-08-10", "curve_base": "EUR-FLAT0", "curve_quote": "USD-FLAT0"}
    assert svc.validate_trade(expired) == []                    # expiry is valuation-date dependent: accepted at import, blocked at compute
    with SessionLocal() as s:
        svc.import_portfolio(s, tenant, trades=[expired])
        s.commit()
        r = svc.compute_margin(s, tenant, {"market_type": "OTC", "netting_set_id": "NS-MER-NWD-001", "as_of": "2026-08-17T04:00:00Z",
                                           "existing_vm_balance": {"PARTY_A": "0", "PARTY_B": "0"}}, "SNAP-SHOCK-001", content_hash(TERMS), None, "OTC_VM_SIMPLE_V1")
        s.commit()
    assert r["status"] == "BLOCKED" and any("expired/matured" in x and "TRD-EXPIRED" in x for x in r["blocking_reasons"])
    assert r["required_amount"] is None and r["exposure"] is None   # unknown is never aggregated as zero


def test_expired_and_invalid_option_inputs_raise_not_silently_priced():
    inst = {"instrument_id": "OPT-X", "type": "OPT", "currency": "INR", "multiplier": "25", "expiry": "2026-08-17", "strike": "24000", "option_type": "CALL", "underlying_id": "FUT-X"}
    md = md_of(marks={"FUT-X": "24000"}, vols={"OPT-X": "0.18"}, rates={"INR": "0.065"})
    with pytest.raises(PricingError, match="expired"):
        etd.position_value(inst, D(10), md, VAL, D("0.01"))
    with pytest.raises(PricingError, match="expired"):
        etd.position_pnl_between(inst, D(10), md, md, VAL)
    for bad in ((0, 100, 1, 0.05, 0, 0.2), (100, 0, 1, 0.05, 0, 0.2), (100, 100, 0, 0.05, 0, 0.2), (100, 100, 1, 0.05, 0, 0.0), (-5, 100, 1, 0.05, 0, 0.2)):
        with pytest.raises(PricingError):
            etd.bs_price_delta("CALL", *bad)
    with pytest.raises(PricingError, match="unsupported option type"):
        etd.bs_price_delta("STRADDLE", 100, 100, 1, 0.05, 0, 0.2)
    with pytest.raises(PricingError, match="missing"):
        etd.position_value(dict(inst, expiry="2026-12-31"), D(1), md_of(rates={"INR": "0.065"}), VAL, D("0.01"))   # no mark/vol: never defaulted


def test_get_positions_reports_expired_option_as_unknown_not_a_crash(tenant):
    inst = {"instrument_id": "OPT-NUM-EXPIRED", "type": "OPT", "venue": "DEMO_NSE", "currency": "INR", "multiplier": "25", "expiry": "2026-08-10",
            "settlement_type": "CASH", "strike": "24000", "option_type": "CALL", "exercise": "EUROPEAN", "underlying_id": "FUT-DEMO-NSE-IDX-SEP26"}
    p = {"account_id": "ACC-NUM-EXP", "instrument_id": "OPT-NUM-EXPIRED", "as_of": "2026-08-17T03:30:00Z", "signed_lots": "5", "settlement_price": "1.00"}
    with SessionLocal() as s:
        svc.import_portfolio(s, tenant, instruments=[inst], positions=[p])
        s.commit()
        g = svc.get_positions(s, tenant, "ACC-NUM-EXP", "2026-08-17T04:00:00Z")
    assert g["gross_lots"] == "5" and g["notional"] is None and g["delta_equivalent_lots"] is None and any("expired" in w for w in g["warnings"])


# ------------------------------------------------------------------------------------------------ FX orientation (properties)
cm = st.integers(min_value=50_000, max_value=200_000).map(lambda i: D(i) / 100_000)       # spot / strike in (0.5, 2.0), 5 dp
rt = st.integers(min_value=-200, max_value=800).map(lambda i: D(i) / 10_000)               # -2% .. +8%, may be negative
nn = st.integers(min_value=1, max_value=10**9)
TR = lambda side, base, quote, n, k, cb, cq: {"base_ccy": base, "quote_ccy": quote, "notional_base": str(n), "strike": str(k), "maturity": "2027-02-17",
                                              "curve_base": cb, "curve_quote": cq, "side": side}


def two_ccy(rb, rq):
    return {"EUR": flat_curve("EUR", rb), "USD": flat_curve("USD", rq)}


@settings(max_examples=120, deadline=None)
@given(s=cm, k=cm, n=nn, rb=rt, rq=rt)
def test_property_buy_sell_antisymmetry_and_closed_form(s, k, n, rb, rq):
    md = md_of(two_ccy(rb, rq), {"EURUSD": str(s)})
    buy, sell = value_fxf(TR("BUY", "EUR", "USD", n, k, "EUR", "USD"), md, VAL), value_fxf(TR("SELL", "EUR", "USD", n, k, "EUR", "USD"), md, VAL)
    assert buy == -sell
    t = D((date(2027, 2, 17) - VAL).days) / D(365)
    expected = D(n) * (s * dexp(-rb * t) - k * dexp(-rq * t))
    assert abs(buy - expected) <= D("1e-20") * max(D(1), abs(expected))


@settings(max_examples=120, deadline=None)
@given(s=cm, k=cm, n=nn, rb=rt, rq=rt)
def test_property_pv_is_invariant_to_the_quoted_fx_orientation(s, k, n, rb, rq):
    """Supplying USDEUR = 1/S instead of EURUSD = S must give the same PV (the engine inverts explicitly; no orientation guess)."""
    direct = md_of(two_ccy(rb, rq), {"EURUSD": str(s)})
    with localcontext() as c:
        c.prec = PREC
        inv = str(1 / s)
    reverse = md_of(two_ccy(rb, rq), {"USDEUR": inv})
    tr = TR("BUY", "EUR", "USD", n, k, "EUR", "USD")
    a, b = value_fxf(tr, direct, VAL), value_fxf(tr, reverse, VAL)
    assert abs(a - b) <= D("1e-25") * max(D(1), abs(a)) * D(n)


@settings(max_examples=120, deadline=None)
@given(s=cm, k=cm, n=nn, rb=rt, rq=rt)
def test_property_mirrored_contract_has_the_same_value(s, k, n, rb, rq):
    """BUY N EUR at K USD/EUR == SELL N*K USD at 1/K EUR/USD. Valued in EUR then converted at spot, the PV must equal the original USD PV."""
    md = md_of(two_ccy(rb, rq), {"EURUSD": str(s)})
    orig = value_fxf(TR("BUY", "EUR", "USD", n, k, "EUR", "USD"), md, VAL)
    with localcontext() as c:
        c.prec = PREC
        kinv, nk = 1 / k, D(n) * k
    mirrored_eur = value_fxf({"base_ccy": "USD", "quote_ccy": "EUR", "notional_base": str(nk), "strike": str(kinv), "maturity": "2027-02-17",
                              "curve_base": "USD", "curve_quote": "EUR", "side": "SELL"}, md, VAL)
    in_usd = md.convert(mirrored_eur, "EUR", "USD")
    assert abs(in_usd - orig) <= D("1e-20") * max(D(1), D(n) * s)


def test_fx_reciprocal_tolerance_boundary():
    ok = {"fx": {"EURUSD": "1.25", "USDEUR": "0.8"}}                                  # exact reciprocal
    assert validate_snapshot_payload(ok) == []
    just_inside = {"fx": {"EURUSD": "1.25", "USDEUR": "0.8000009"}}                  # |1.25*0.8000009 - 1| = 1.125e-6 > 1e-6 -> reject
    assert validate_snapshot_payload(just_inside)
    assert validate_snapshot_payload({"fx": {"EURUSD": "1.25", "USDEUR": "0.80000079"}}) == []   # 9.9e-7 < 1e-6 accepted
    with pytest.raises(PricingError, match="missing FX"):
        md_of(fx_={"EURUSD": "1.1"}).fx_rate("GBP", "USD")


# ------------------------------------------------------------------------------------------------ independent Decimal reconciliation of float BSM
PI = D("3.14159265358979323846264338327950288419716939937510582097494459230781640628620899")


def d_erf(z):
    with localcontext() as c:
        c.prec = 90
        z = D(z)
        z2, term, tot = z * z, z, z
        for n in range(1, 450):
            term = -term * z2 / n
            tot += term / (2 * n + 1)
        return 2 / PI.sqrt() * tot


def d_ncdf(x):
    with localcontext() as c:
        c.prec = 90
        return (1 + d_erf(D(x) / D(2).sqrt())) / 2


def d_bsm(kind, s, k, t, r, q, vol):
    with localcontext() as c:
        c.prec = 90
        s, k, t, r, q, vol = (D(x) for x in (s, k, t, r, q, vol))
        d1 = ((s / k).ln() + (r - q + vol * vol / 2) * t) / (vol * t.sqrt())
        d2 = d1 - vol * t.sqrt()
        dfq, dfr = (-q * t).exp(), (-r * t).exp()
        if kind == "CALL":
            return s * dfq * d_ncdf(d1) - k * dfr * d_ncdf(d2), dfq * d_ncdf(d1)
        return k * dfr * d_ncdf(-d2) - s * dfq * d_ncdf(-d1), -dfq * d_ncdf(-d1)


def test_decimal_erf_oracle_is_itself_correct():
    # independent sanity of the oracle: known constants (erf(1) = 0.8427007929497148693412206350826...)
    assert abs(d_erf(1) - D("0.84270079294971486934122063508260925929606699796630")) < D("1e-40")
    assert abs(d_ncdf(0) - D("0.5")) < D("1e-80") and abs(d_ncdf(1.96) - D("0.97500210485177")) < D("1e-12")


SP = st.integers(5_000, 40_000).map(lambda i: D(i) / 100)
KP = st.integers(5_000, 40_000).map(lambda i: D(i) / 100)
TP = st.integers(2, 730).map(lambda i: D(i) / 365)
RP = st.integers(-100, 1000).map(lambda i: D(i) / 10_000)
QP = st.integers(0, 500).map(lambda i: D(i) / 10_000)
VP = st.integers(500, 9000).map(lambda i: D(i) / 10_000)


@settings(max_examples=80, deadline=None)
@given(s=SP, k=KP, t=TP, r=RP, q=QP, vol=VP, kind=st.sampled_from(["CALL", "PUT"]))
def test_property_float_bsm_reconciles_to_independent_decimal_within_1e10(s, k, t, r, q, vol, kind):
    fp, fd = etd.bs_price_delta(kind, float(s), float(k), float(t), float(r), float(q), float(vol))
    dp, dd = d_bsm(kind, float(s), float(k), float(t), float(r), float(q), float(vol))   # oracle fed the exact same binary inputs
    assert abs(D(fp) - dp) <= D("1e-10") * max(D(1), D(float(s))), (kind, s, k, t, r, q, vol, fp, dp)
    assert abs(D(fd) - dd) <= D("1e-12")
    assert (0 <= fd <= 1) if kind == "CALL" else (-1 <= fd <= 0)


@settings(max_examples=60, deadline=None)
@given(s=SP, k=KP, t=TP, r=RP, q=QP, vol=VP)
def test_property_put_call_parity_and_monotonic_in_spot(s, k, t, r, q, vol):
    args = (float(k), float(t), float(r), float(q), float(vol))
    c, _ = etd.bs_price_delta("CALL", float(s), *args)
    p, _ = etd.bs_price_delta("PUT", float(s), *args)
    parity = D(float(s)) * dexp(-D(float(q)) * D(float(t))) - D(float(k)) * dexp(-D(float(r)) * D(float(t)))
    assert abs(D(c) - D(p) - parity) <= D("1e-8")
    c_up, _ = etd.bs_price_delta("CALL", float(s) * 1.01, *args)
    assert c_up >= c - 1e-12                                                     # call value never falls when spot rises


def test_option_position_pnl_reconciles_to_decimal_within_quantisation_and_futures_is_exact():
    inst = {"instrument_id": "OPT-N", "type": "OPT", "currency": "INR", "multiplier": "25", "expiry": "2026-09-29", "strike": "24000", "option_type": "CALL", "underlying_id": "FUT-N"}
    md = md_of(marks={"FUT-N": "24000"}, vols={"OPT-N": "0.18"}, rates={"INR": "0.065"})
    lots, shock = D(-20), D("-0.05")
    pnl, _ = etd.position_value(inst, lots, md, VAL, shock)
    t = D((date(2026, 9, 29) - VAL).days) / D(365)
    p0, _ = d_bsm("CALL", 24000, 24000, t, "0.065", 0, "0.18")
    p1, _ = d_bsm("CALL", D(24000) * (1 + shock), 24000, t, "0.065", 0, "0.18")
    exact = lots * 25 * (p1 - p0)
    assert abs(pnl - exact) <= abs(lots) * 25 * D("0.0051"), (pnl, exact)          # float path is quantised to 2dp per unit: bound = 0.005/unit
    fut = {"instrument_id": "FUT-N", "type": "FUT", "currency": "INR", "multiplier": "25"}
    fp, fdelta = etd.position_value(fut, D(40), md, VAL, D("0.03"))
    assert fp == D(40) * D(25) * D(24000) * D("0.03") == D("720000") and fdelta == 1.0      # futures path is pure Decimal: exact
    assert math.isfinite(float(pnl))
