"""A3 VECTOR ETD internal stress proxy (NOT SPAN, NOT a regulatory margin; separate from the imported source obligation).

Futures P&L = signed_lots * multiplier * mark * price_shock.
European options: Black-Scholes-Merton scenario revaluation (float internally via math.erf; results quantised to 2dp Decimal;
error vs closed form bounded by double precision ~1e-12 relative). Inputs validated: S>0, K>0, vol>0, T>0.
"""
from __future__ import annotations

import math
from datetime import date
from decimal import ROUND_HALF_EVEN, Decimal

from app.engines.risk.pricing import MarketData, PricingError

Q2 = Decimal("0.01")


def _ncdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_price_delta(opt_type: str, s: float, k: float, t: float, r: float, q: float, vol: float) -> tuple[float, float]:
    if not (s > 0 and k > 0 and vol > 0 and t > 0):
        raise PricingError("Black-Scholes inputs invalid (S,K,vol,T must be positive)")
    d1 = (math.log(s / k) + (r - q + 0.5 * vol * vol) * t) / (vol * math.sqrt(t))
    d2 = d1 - vol * math.sqrt(t)
    df_r, df_q = math.exp(-r * t), math.exp(-q * t)
    if opt_type == "CALL":
        return s * df_q * _ncdf(d1) - k * df_r * _ncdf(d2), df_q * _ncdf(d1)
    if opt_type == "PUT":
        return k * df_r * _ncdf(-d2) - s * df_q * _ncdf(-d1), -df_q * _ncdf(-d1)
    raise PricingError(f"unsupported option type {opt_type!r}")


def _q2(x: float) -> Decimal:
    return Decimal(repr(x)).quantize(Q2, rounding=ROUND_HALF_EVEN)


def position_value(inst: dict, lots: Decimal, md: MarketData, val: date, shock: Decimal) -> tuple[Decimal, float]:
    """Returns (scenario P&L for `shock` fractional move in the underlying, delta per unit)."""
    mult = Decimal(inst["multiplier"])
    if inst["type"] == "FUT":
        mark = md.marks.get(inst["instrument_id"])
        if mark is None:
            raise PricingError(f"missing mark for {inst['instrument_id']}")
        return lots * mult * mark * shock, 1.0
    und = inst.get("underlying_id")
    s0 = md.marks.get(und)
    vol = md.vols.get(inst["instrument_id"])
    if s0 is None or vol is None:
        raise PricingError(f"missing underlying mark or vol for {inst['instrument_id']}")
    exp = date.fromisoformat(inst["expiry"])
    if exp <= val:
        raise PricingError(f"option {inst['instrument_id']} expired/at expiry; settlement handled by instrument settlement_type, not repriced")
    t = (exp - val).days / 365.0
    r = float(md.rates.get(inst["currency"], Decimal(0)))
    q = float(md.rates.get(inst["currency"] + "_DIV", Decimal(0)))
    k = float(inst["strike"])
    p0, d0 = bs_price_delta(inst["option_type"], float(s0), k, t, r, q, float(vol))
    p1, _ = bs_price_delta(inst["option_type"], float(s0) * (1 + float(shock)), k, t, r, q, float(vol))
    return lots * mult * _q2(p1 - p0), d0


def _opt_price(inst: dict, md: MarketData, val: date) -> float:
    und, vol = inst["underlying_id"], md.vols.get(inst["instrument_id"])
    s0 = md.marks.get(und)
    if s0 is None or vol is None:
        raise PricingError(f"missing underlying mark or vol for {inst['instrument_id']}")
    exp = date.fromisoformat(inst["expiry"])
    if exp <= val:
        raise PricingError(f"option {inst['instrument_id']} expired/at expiry")
    r = float(md.rates.get(inst["currency"], Decimal(0)))
    q = float(md.rates.get(inst["currency"] + "_DIV", Decimal(0)))
    return bs_price_delta(inst["option_type"], float(s0), float(inst["strike"]), (exp - val).days / 365.0, r, q, float(vol))[0]


def position_pnl_between(inst: dict, lots: Decimal, md0: MarketData, md1: MarketData, val: date) -> Decimal:
    """Scenario P&L between a baseline and a (shocked) market-data copy: futures by mark change; options by BSM revaluation."""
    mult = Decimal(inst["multiplier"])
    if inst["type"] == "FUT":
        k = inst["instrument_id"]
        if k not in md0.marks or k not in md1.marks:
            raise PricingError(f"missing mark for {k}")
        return lots * mult * (md1.marks[k] - md0.marks[k])
    return lots * mult * _q2(_opt_price(inst, md1, val) - _opt_price(inst, md0, val))


def internal_estimate(positions: list[dict], instruments: dict[str, dict], md: MarketData, val: date, shock_pct: Decimal) -> dict:
    """Worst-of(+shock,-shock) loss. Also raw metrics for A5 position-limit rules (gross/net lots, notional, delta-equivalent)."""
    pnl_dn = pnl_up = Decimal(0)
    gross = net = Decimal(0)
    notional = delta_eq = Decimal(0)
    by_inst = []
    for p in positions:
        inst = instruments[p["instrument_id"]]
        lots = Decimal(p["signed_lots"])
        dn, d0 = position_value(inst, lots, md, val, -shock_pct)
        up, _ = position_value(inst, lots, md, val, shock_pct)
        pnl_dn += dn; pnl_up += up
        gross += abs(lots); net += lots
        under = inst["instrument_id"] if inst["type"] == "FUT" else inst["underlying_id"]
        mk = md.marks.get(under, Decimal(0))
        notional += abs(lots) * Decimal(inst["multiplier"]) * mk
        delta_eq += lots * Decimal(repr(round(d0, 8)))
        by_inst.append({"instrument_id": inst["instrument_id"], "signed_lots": str(lots), "pnl_down": str(dn), "pnl_up": str(up)})
    worst = min(pnl_dn, pnl_up, Decimal(0))
    return {"label": "INTERNAL_ESTIMATE_NOT_SOURCE_OBLIGATION_NOT_SPAN", "shock_pct": str(shock_pct),
            "pnl_down": str(pnl_dn), "pnl_up": str(pnl_up), "estimated_loss": str(-worst),
            "metrics": {"gross_lots": str(gross), "net_lots": str(net), "notional": str(notional.quantize(Q2)),
                        "delta_equivalent_lots": str(delta_eq)},
            "by_instrument": by_inst}


def position_metrics(positions: list[dict], instruments: dict[str, dict], md: MarketData, val: date) -> dict:
    """Raw metrics for position-limit rules (contract v1.1): notional = sum |lots| x multiplier x underlying mark (instrument currency);
    delta-equivalent lots = sum lots x delta (futures 1.0; options Black-Scholes-Merton delta, float math quantised to 8dp)."""
    notional = delta_eq = Decimal(0)
    for p in positions:
        inst = instruments[p["instrument_id"]]
        lots = Decimal(p["signed_lots"])
        under = inst["instrument_id"] if inst["type"] == "FUT" else inst["underlying_id"]
        mk = md.marks.get(under)
        if mk is None:
            raise PricingError(f"missing mark for {under}")
        notional += abs(lots) * Decimal(inst["multiplier"]) * mk
        if inst["type"] == "FUT":
            d0 = 1.0
        else:
            _, d0 = position_value(inst, lots, md, val, Decimal(0))
        delta_eq += lots * Decimal(repr(round(d0, 8)))
    return {"notional": str(notional.quantize(Q2)), "delta_equivalent_lots": str(delta_eq)}
