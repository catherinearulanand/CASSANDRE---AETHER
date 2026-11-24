"""Pure Decimal valuation and cost functions (no DB).

Effective coverage per unit (target currency):
    e_i = price_i * FX_i * valuation_factor_i * fx_factor_i
valuation_factor comes from ONE convention per schedule entry: valuation_pct (e.g. 94) XOR (100 - haircut_pct).
If an entry supplies both they must agree (sum to 100) - otherwise the entry is rejected, so a 6 % haircut is never
applied on top of a 94 % valuation. Percent fields are PERCENT POINTS (94 means 94 %), not fractions.
fx_factor = (100 - fx_haircut_pct)/100 only when the lot currency differs from the obligation currency.
Where several policy sources apply the MOST CONSERVATIVE (minimum) factor is used - discounts are never multiplied.
"""
from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Decimal, localcontext

from app.core.money import PRECISION, NumericError, to_decimal

HUNDRED = Decimal(100)
QTY_DP = Decimal("0.000001")  # quantity precision (micro-units)
SCALE = 10**6


def D(v, field: str = "value") -> Decimal:
    return to_decimal(v, field=field)


def pct_to_factor(p: Decimal, field: str) -> Decimal:
    if p < 0 or p > 100:
        raise NumericError(f"{field}: percent must be within 0..100, got {p}")
    return p / HUNDRED


def valuation_factor(entry: dict) -> Decimal:
    """Returns the valuation factor of a schedule entry or raises NumericError (INCONSISTENT_HAIRCUT / INVALID_VALUATION)."""
    v, h = entry.get("valuation_pct"), entry.get("haircut_pct")
    if v is None and h is None:
        raise NumericError("INVALID_VALUATION: neither valuation_pct nor haircut_pct supplied")
    vf = pct_to_factor(D(v, "valuation_pct"), "valuation_pct") if v is not None else None
    hf = pct_to_factor(D(h, "haircut_pct"), "haircut_pct") if h is not None else None
    if vf is not None and hf is not None and vf + hf != 1:
        raise NumericError(f"INCONSISTENT_HAIRCUT: valuation_pct + haircut_pct must equal 100 (got {vf*100}+{hf*100})")
    return vf if vf is not None else 1 - hf  # type: ignore[operator]


def fx_factor(entry: dict, lot_ccy: str, target_ccy: str) -> Decimal:
    if lot_ccy == target_ccy:
        return Decimal(1)
    h = entry.get("fx_haircut_pct")
    if h is None:
        raise NumericError("MISSING_FX_HAIRCUT: cross-currency lot but schedule has no fx_haircut_pct")
    return 1 - pct_to_factor(D(h, "fx_haircut_pct"), "fx_haircut_pct")


def effective_per_unit(price: Decimal, fx_rate: Decimal, val_factor: Decimal, fx_fac: Decimal) -> Decimal:
    """e = price * FX * valuation_factor * fx_factor  (G06: 100 * 1 * 0.94 * 0.92 = 86.48)."""
    with localcontext() as c:
        c.prec = PRECISION
        return price * fx_rate * val_factor * fx_fac


def cost_per_unit(price: Decimal, fx_rate: Decimal, annual_rate: Decimal, horizon_days: int, txn_cost: Decimal) -> Decimal:
    """Horizon funding cost per unit in target currency:
       gross_value_per_unit * annual_cost_rate * horizon_days/365 + transaction cost per unit.
       annual_rate is a decimal FRACTION p.a. (0.07 = 7 %); horizon cost is NOT annualised."""
    with localcontext() as c:
        c.prec = PRECISION
        return price * fx_rate * annual_rate * Decimal(horizon_days) / Decimal(365) + txn_cost


def to_u(q: Decimal) -> int:
    with localcontext() as c:
        c.prec = PRECISION
        return int((q * SCALE).to_integral_value(rounding=ROUND_HALF_EVEN))


def from_u(u: int) -> Decimal:
    return Decimal(u) / SCALE


def q6(x: Decimal) -> Decimal:
    return x.quantize(QTY_DP, rounding=ROUND_HALF_EVEN)
