"""Authoritative numeric types. Decimal strings only; floats are rejected at the boundary.

Money   : decimal string + ISO-4217 code            e.g. {"amount": "9500000.00", "currency": "USD"}
Rate    : decimal FRACTION (0.08 == 8%)
BasisPoints : separate type; conversion to/from Rate is explicit.
"""
from __future__ import annotations

from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal, InvalidOperation, localcontext
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, field_serializer, field_validator

PRECISION = 40  # documented working precision for authoritative Decimal arithmetic

# Allowlist (synthetic build). Extend deliberately, never accept free-form codes.
SUPPORTED_CURRENCIES = frozenset({"INR", "USD", "EUR", "GBP", "JPY", "SGD", "AED", "CHF"})

ROUNDING = {"UP": ROUND_CEILING, "DOWN": ROUND_FLOOR, "HALF_UP": ROUND_HALF_UP, "HALF_EVEN": ROUND_HALF_EVEN}


class NumericError(ValueError):
    pass


def to_decimal(value: Any, *, field: str = "value", allow_negative: bool = True) -> Decimal:
    """Strict conversion: str/int/Decimal accepted; float, bool, NaN, Inf rejected."""
    if isinstance(value, bool) or isinstance(value, float):
        raise NumericError(f"{field}: floats/bools are not accepted for authoritative numbers; pass a decimal string")
    if isinstance(value, Decimal):
        d = value
    elif isinstance(value, (str, int)):
        try:
            d = Decimal(str(value).strip())
        except InvalidOperation as exc:
            raise NumericError(f"{field}: not a decimal: {value!r}") from exc
    else:
        raise NumericError(f"{field}: unsupported type {type(value).__name__}")
    if not d.is_finite():
        raise NumericError(f"{field}: NaN/Infinity rejected")
    if not allow_negative and d < 0:
        raise NumericError(f"{field}: negative value not permitted")
    return d


def dstr(d: Decimal) -> str:
    """Canonical plain-notation decimal string (no exponent, no trailing-zero ambiguity: normalised)."""
    if d == 0:
        return "0"
    s = format(d.normalize(), "f")
    return s


def round_to_increment(amount: Decimal, increment: Decimal, mode: str) -> Decimal:
    """Round `amount` to a multiple of `increment` using a named agreement-specific mode."""
    if increment <= 0:
        raise NumericError("rounding increment must be positive")
    if mode not in ROUNDING:
        raise NumericError(f"unknown rounding mode {mode!r}")
    with localcontext() as ctx:
        ctx.prec = PRECISION
        q = (amount / increment).to_integral_value(rounding=ROUNDING[mode])
        return q * increment


class Money(BaseModel):
    model_config = ConfigDict(frozen=True)
    amount: Decimal
    currency: str

    @field_validator("amount", mode="before")
    @classmethod
    def _amt(cls, v):
        return to_decimal(v, field="amount")

    @field_validator("currency")
    @classmethod
    def _ccy(cls, v: str):
        if v not in SUPPORTED_CURRENCIES:
            raise ValueError(f"unsupported currency {v!r}")
        return v

    @field_serializer("amount")
    def _ser(self, v: Decimal):
        return dstr(v)

    def _same(self, o: "Money"):
        if self.currency != o.currency:
            raise NumericError(f"currency mismatch {self.currency} vs {o.currency}: no implicit blending")

    def __add__(self, o: "Money") -> "Money":
        self._same(o)
        return Money(amount=self.amount + o.amount, currency=self.currency)

    def __sub__(self, o: "Money") -> "Money":
        self._same(o)
        return Money(amount=self.amount - o.amount, currency=self.currency)

    def __neg__(self) -> "Money":
        return Money(amount=-self.amount, currency=self.currency)


class Rate(BaseModel):
    """Decimal fraction. 0.08 means 8 %."""
    model_config = ConfigDict(frozen=True)
    fraction: Decimal

    @field_validator("fraction", mode="before")
    @classmethod
    def _f(cls, v):
        return to_decimal(v, field="fraction")

    def to_bps(self) -> "BasisPoints":
        return BasisPoints(bps=self.fraction * 10000)


class BasisPoints(BaseModel):
    model_config = ConfigDict(frozen=True)
    bps: Decimal

    @field_validator("bps", mode="before")
    @classmethod
    def _b(cls, v):
        return to_decimal(v, field="bps")

    def to_rate(self) -> Rate:
        return Rate(fraction=self.bps / 10000)


class Quantity(BaseModel):
    model_config = ConfigDict(frozen=True)
    value: Decimal
    unit: str

    @field_validator("value", mode="before")
    @classmethod
    def _v(cls, v):
        return to_decimal(v, field="value")


DecimalStr = Annotated[Decimal, "decimal-string"]
