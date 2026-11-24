"""V-A02..V-A05: threshold/MTA/rounding boundary goldens, return amounts, separate obligations. Expected values from
fixtures/expected/risk/hand_cases.json (hand-calculated)."""
import json
from decimal import Decimal
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.engines.risk import margin as mg

D = Decimal
G = json.loads((Path(__file__).resolve().parents[3] / "fixtures" / "expected" / "risk" / "hand_cases.json").read_text())["golden"]
H = "a" * 64


def terms(**kw):
    t = {"base_currency": "USD", "threshold_a": "10000000", "threshold_b": "10000000", "mta_a": "500000", "mta_b": "500000",
         "independent_amount_a": "0", "independent_amount_b": "0", "rounding_increment": "100000", "rounding_mode": "UP", "mta_semantics": "GTE"}
    t.update(kw)
    return t


ZERO = {"PARTY_A": "0", "PARTY_B": "0"}


def run(e, t=None, held=None):
    return mg.otc_waterfall(D(e), t or terms(), held or ZERO, "USD", H)


def test_g02_quiet_zero_transfer():
    g = G["G02"]
    w = run(g["exposure"])
    assert w["direction"] == "NONE" and w["rounded_amount"] == 0 and w["target"] == D(g["target"])


def test_g03_shock_call_rounded_up():
    g = G["G03"]
    w = run(g["exposure"])
    assert w["unrounded_amount"] == D(g["unrounded"]) and w["rounded_amount"] == D(g["rounded_up"]) and w["direction"] == "POST"
    assert w["obligor"] == "PARTY_B" and w["mta_triggered"]
    assert any("TRIGGERED" in s["value"] for s in w["steps"] if "MTA" in s["step"])


def test_g04_mta_boundary_gte():
    g = G["G04"]
    thr = D("10000000")
    below = run(thr + D(g["below_unrounded"]))
    assert below["direction"] == "NONE" and not below["mta_triggered"] and below["residual"] == D(g["below_unrounded"])
    at = run(thr + D(g["at_unrounded"]))
    assert at["direction"] == "POST" and at["rounded_amount"] == D("500000")
    gt = run(thr + D(g["at_unrounded"]), terms(mta_semantics="GT"))
    assert gt["direction"] == "NONE"  # exactly MTA does NOT trigger under GT


def test_g08_amendment_target_before_after():
    g = G["G08"]
    assert run(g["exposure"], terms(threshold_b=g["threshold_before"]))["target"] == D(g["target_before"])
    assert run(g["exposure"], terms(threshold_b=g["threshold_after"]))["target"] == D(g["target_after"])


def test_g25_return_of_excess_no_new_posting():
    g = G["G25"]
    t = terms(threshold_b="0", mta_b=g["mta"], rounding_increment="1", return_rounding_mode="DOWN")
    w = run(g["target"], t, {"PARTY_A": "0", "PARTY_B": g["held_same_direction"]})
    assert w["direction"] == "RETURN" and w["unrounded_amount"] == D(g["return_unrounded"]) and w["rounded_amount"] == 50


def test_return_rounding_assumption_is_explicit():
    t = terms(threshold_b="0", rounding_increment="1", mta_b="10")
    w = run("100", t, {"PARTY_A": "0", "PARTY_B": "150"})
    assert any("ASSUMPTION" in s["citation"] for s in w["steps"])


def test_g26_opposite_direction_and_im_are_separate_never_offset():
    # exposure favours A (B owes); A had previously posted 2,000,000 which must be returned SEPARATELY, not netted into the call.
    w = run("19469510.12", terms(), {"PARTY_A": "2000000", "PARTY_B": "0"})
    assert w["rounded_amount"] == D("9500000") and w["direction"] == "POST"  # call unchanged by opposite collateral
    assert w["opposite_return"] == D("2000000")


def test_zero_exposure_returns_all_held_separately():
    w = run("0", held={"PARTY_A": "1000000", "PARTY_B": "300000"})
    assert w["direction"] == "NONE" and w["opposite_return"] == D("1300000")


def test_negative_exposure_uses_party_a_terms():
    w = run("-19469510.12", terms(threshold_a="9000000"))
    assert w["obligor"] == "PARTY_A" and w["unrounded_amount"] == D("10469510.12")


def test_missing_and_invalid_terms_block_without_defaults():
    t = terms()
    del t["mta_b"]
    with pytest.raises(mg.TermsError, match="mta_b"):
        run("1", t)
    with pytest.raises(mg.TermsError):
        run("1", terms(rounding_increment="0"))
    with pytest.raises(mg.TermsError):
        run("1", terms(mta_a="-1"))
    with pytest.raises(mg.TermsError):
        run("1", terms(rounding_mode="CEIL"))
    with pytest.raises(mg.TermsError, match="currency"):
        mg.otc_waterfall(D(1), terms(base_currency="EUR"), ZERO, "USD", H)


def test_im_schedule_is_separate_illustration():
    im = mg.im_illustrative([(D("100"), D("1.5")), (D("-50"), D("4"))], [{"max_years": "2", "rate": "0.01"}, {"max_years": "10", "rate": "0.03"}])
    assert im == D("1") + D("1.5")


@settings(max_examples=150, deadline=None)
@given(e=st.integers(min_value=-5 * 10**9, max_value=5 * 10**9), thr=st.integers(0, 10**9))
def test_property_rounded_call_is_increment_multiple_and_not_below_unrounded(e, thr):
    w = run(D(e), terms(threshold_a=str(thr), threshold_b=str(thr)))
    inc = D("100000")
    assert w["rounded_amount"] % inc == 0
    if w["direction"] == "POST":
        assert w["rounded_amount"] >= w["unrounded_amount"] >= D("500000")
    else:
        assert w["rounded_amount"] == 0


@settings(max_examples=100, deadline=None)
@given(a=st.integers(0, 10**9), b=st.integers(0, 10**9))
def test_property_monotone_in_exposure(a, b):
    lo, hi = sorted((a, b))
    assert run(D(lo))["rounded_amount"] <= run(D(hi))["rounded_amount"]
