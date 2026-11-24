"""V-A01: pricing vs INDEPENDENT hand/reference values (fixtures/expected/risk, never imported by production code)."""
import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from app.engines.risk.pricing import (MarketData, PricingError, schedule, shock_payload, validate_snapshot_payload, value_fxf, value_irs)

EXP = Path(__file__).resolve().parents[3] / "fixtures" / "expected" / "risk"
HAND = json.loads((EXP / "hand_cases.json").read_text())
VAL = date(2026, 8, 17)
D = Decimal


def test_irs_1y_hand_value():
    c = HAND["irs_1y_annual"]
    md = MarketData({"curves": c["curves"]})
    pv = value_irs(c["trade"], md, VAL, "T")
    assert abs(pv - D(c["pv_receive_fixed"])) < D(c["tolerance_abs"])
    pay = dict(c["trade"], pay_receive="PAY_FIXED")
    assert abs(value_irs(pay, md, VAL, "T") - D(c["pv_pay_fixed"])) < D(c["tolerance_abs"])


def test_irs_2y_semiannual_matches_independent_reference():
    ref = json.loads((EXP / "irs_2y_semi.json").read_text())
    snap = json.loads((Path(__file__).resolve().parents[3] / "fixtures" / "risk" / "snapshots.json").read_text())[0]["payload"]
    t = {"type": "IRS", "pay_receive": "PAY_FIXED", "notional": "50000000", "currency": "USD", "fixed_rate": "0.04", "effective": "2026-08-17",
         "maturity": "2028-08-17", "freq_months": 6, "fixed_dc": "ACT/360", "float_dc": "ACT/360", "curve_disc": "USD-SOFR-DISC", "curve_proj": "USD-SOFR-PROJ"}
    md = MarketData(snap)
    assert abs(value_irs(t, md, VAL, "T") - D(str(ref["pv_pay_fixed"]))) < D(str(ref["tolerance_abs"]))
    rf = dict(t, pay_receive="RECEIVE_FIXED")
    assert abs(value_irs(rf, md, VAL, "T") - D(str(ref["pv_receive_fixed"]))) < D(str(ref["tolerance_abs"]))


def test_fx_forward_buy_sell_hand_value():
    c = HAND["fx_forward_buy"]
    md = MarketData({"curves": c["curves"], "fx": c["fx"]})
    assert value_fxf(c["trade"], md, VAL) == D(c["pv_quote_buy"])
    assert value_fxf(dict(c["trade"], side="SELL"), md, VAL) == D(c["pv_quote_sell"])


def test_fx_orientation_and_reciprocal():
    c = HAND["fx_orientation"]
    md = MarketData({"fx": c["fx"]})
    assert md.fx_rate("EUR", "USD") == D("1.12")
    assert abs(md.convert(D("30400"), "USD", "EUR") - D(c["usd_to_eur_of_30400"])) < D(c["tolerance_abs"])
    assert md.convert(D("5"), "EUR", "EUR") == 5
    with pytest.raises(PricingError):
        md.fx_rate("EUR", "INR")  # missing pair is an error, never 1


def test_missing_fixing_never_silently_ignored():
    c = HAND["irs_1y_annual"]
    t = dict(c["trade"], effective="2026-05-17", maturity="2027-05-17")  # accrual in progress at 2026-08-17
    md = MarketData({"curves": c["curves"]})
    with pytest.raises(PricingError, match="missing fixing"):
        value_irs(t, md, VAL, "TRD")
    md2 = MarketData({"curves": c["curves"], "fixings": {"TRD|2026-05-17": "0.045"}})
    assert isinstance(value_irs(t, md2, VAL, "TRD"), Decimal)


def test_no_extrapolation_beyond_curve():
    c = HAND["irs_1y_annual"]
    t = dict(c["trade"], maturity="2028-08-17", freq_months=12)
    with pytest.raises(PricingError, match="beyond last curve node"):
        value_irs(t, MarketData({"curves": c["curves"]}), VAL, "T")


def test_snapshot_validation_rules():
    ok = {"curves": {"C": {"currency": "USD", "points": [["0", "1"], ["1", "1.01"]]}}, "fx": {"EURUSD": "1.1"}}
    assert validate_snapshot_payload(ok) == []  # negative rates (DF>1) are legal
    bad_grid = {"curves": {"C": {"currency": "USD", "points": [["0", "1"], ["1", "0.9"], ["1", "0.8"]]}}}
    assert any("strictly increasing" in e for e in validate_snapshot_payload(bad_grid))
    assert any("positive" in e for e in validate_snapshot_payload({"curves": {"C": {"currency": "USD", "points": [["0", "1"], ["1", "-0.1"]]}}}))
    assert any("reciprocal" in e for e in validate_snapshot_payload({"fx": {"EURUSD": "1.1", "USDEUR": "0.5"}}))
    assert validate_snapshot_payload({"fx": {"EURUSD": "NaN"}})
    assert validate_snapshot_payload({"marks": {"X": "-5"}})


def test_schedule_rejects_stub_and_non_positive():
    assert len(schedule(date(2026, 8, 17), date(2028, 8, 17), 6)) == 5
    with pytest.raises(PricingError):
        schedule(date(2026, 8, 17), date(2027, 1, 17), 4 * 2)  # stub
    with pytest.raises(PricingError):
        schedule(date(2026, 8, 17), date(2026, 8, 17), 6)


def test_shock_copy_does_not_mutate_baseline():
    base = {"curves": {"C": {"currency": "USD", "points": [["0", "1"], ["1", "0.96"]]}}, "fx": {"EURUSD": "1.125"}, "marks": {"M": "100"}}
    import copy
    before = copy.deepcopy(base)
    sh = shock_payload(base, {"curve_shift_bp": "100", "fx_shock_pct": {"EURUSD": "0.1"}, "price_shock_pct": "-0.05"})
    assert base == before
    assert D(sh["fx"]["EURUSD"]) == D("1.2375") and D(sh["marks"]["M"]) == D("95")
    assert D(sh["curves"]["C"]["points"][1][1]) < D("0.96")  # +100bp lowers DF
    with pytest.raises(PricingError):
        shock_payload(base, {"fx_shock_pct": {"GBPUSD": "0.1"}})
