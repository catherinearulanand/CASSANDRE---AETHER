from decimal import Decimal

import pytest

from app.core.money import NumericError
from app.engines.collateral import valuation as V

D = Decimal


@pytest.mark.gold
def test_g06_haircut_fx_effective_value_no_double_haircut():
    # G06 independent hand calc: 100 * 0.94 * 0.92 = 86.48
    e = V.effective_per_unit(D("100"), D(1), D("0.94"), D("0.92"))
    assert e == D("86.48")


def test_valuation_vs_haircut_conventions_are_one_discount_not_two():
    assert V.valuation_factor({"valuation_pct": "94"}) == D("0.94")
    assert V.valuation_factor({"haircut_pct": "6"}) == D("0.94")
    assert V.valuation_factor({"valuation_pct": "94", "haircut_pct": "6"}) == D("0.94")
    with pytest.raises(NumericError, match="INCONSISTENT_HAIRCUT"):
        V.valuation_factor({"valuation_pct": "94", "haircut_pct": "94"})  # would double-apply


def test_missing_valuation_and_out_of_range_rejected():
    with pytest.raises(NumericError, match="INVALID_VALUATION"):
        V.valuation_factor({})
    with pytest.raises(NumericError):
        V.valuation_factor({"valuation_pct": "101"})
    with pytest.raises(NumericError):
        V.valuation_factor({"haircut_pct": "-1"})


def test_fx_factor_only_cross_currency_and_never_defaulted():
    assert V.fx_factor({}, "INR", "INR") == 1
    assert V.fx_factor({"fx_haircut_pct": "8"}, "USD", "INR") == D("0.92")
    with pytest.raises(NumericError, match="MISSING_FX_HAIRCUT"):
        V.fx_factor({}, "USD", "INR")


def test_cost_per_unit_horizon_not_annualised():
    # 100 price * 7% p.a. * 30/365 + 0.5 txn = 0.575342465... + 0.5
    c = V.cost_per_unit(D("100"), D(1), D("0.07"), 30, D("0.5"))
    assert abs(c - D("1.07534246575342465753")) < D("1e-18")  # 210/365 + 0.5, hand-derived
    assert V.cost_per_unit(D("100"), D(1), D("0.07"), 365, D(0)) == D("7")  # one year == annual cost


def test_floats_rejected():
    with pytest.raises(NumericError):
        V.D(1.5)


def test_micro_unit_conversion_exact():
    assert V.to_u(D("11111.111111")) == 11111111111
    assert V.from_u(11111111111) == D("11111.111111")
