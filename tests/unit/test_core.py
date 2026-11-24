from decimal import Decimal

import pytest

from app.core.canonical import CanonicalError, canonical_bytes, domain_hash
from app.core.money import Money, NumericError, Rate, round_to_increment, to_decimal


def test_floats_rejected():
    with pytest.raises(NumericError):
        to_decimal(1.5)
    with pytest.raises(CanonicalError):
        canonical_bytes({"a": 1.5})


def test_nan_inf_rejected():
    for bad in ("NaN", "Infinity", "-Infinity"):
        with pytest.raises(NumericError):
            to_decimal(bad)


def test_currency_allowlist_and_no_blending():
    with pytest.raises(ValueError):
        Money(amount="1", currency="XXX")
    with pytest.raises(NumericError):
        Money(amount="1", currency="USD") + Money(amount="1", currency="INR")


def test_canonical_stable_and_key_order_independent():
    a = {"b": Decimal("1.50"), "a": [1, "x"]}
    b = {"a": [1, "x"], "b": Decimal("1.5")}
    assert canonical_bytes(a) == canonical_bytes(b) == b'{"a":[1,"x"],"b":"1.5"}'


def test_domain_separation():
    assert domain_hash("d1", {"a": 1}) != domain_hash("d2", {"a": 1})


def test_rate_bps_explicit():
    assert Rate(fraction="0.08").to_bps().bps == Decimal("800")


@pytest.mark.gold
def test_g03_rounding_up_to_increment():
    # G03 independent hand expectation: 9,469,510.12 rounded UP to a 100,000 increment = 9,500,000
    assert round_to_increment(Decimal("9469510.12"), Decimal("100000"), "UP") == Decimal("9500000")
