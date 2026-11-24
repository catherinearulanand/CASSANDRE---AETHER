"""A3 VECTOR pure pricing (no DB, no I/O). All authoritative arithmetic is Decimal at PREC=40 digits.

Conventions (documented in docs/methodologies/risk.md):
  * Curve time t = (date - valuation_date).days / 365   (ACT/365F year fraction).
  * Discount factors interpolated log-linearly in t with DF(0)=1. NO extrapolation beyond the last node (error).
  * Schedules: anchored month-stepping from the effective date; NO business-day adjustment / holiday calendar in P0.
  * IRS PV = signed discounted cashflows. Future floating coupon = N * (DFp(s)/DFp(e) - 1) discounted on the
    discount curve; reset-in-progress coupons REQUIRE a fixing from the snapshot (missing fixing => error, never ignored).
  * FX forward (buy N base at K quote/base): PV_quote = N * (S*DF_base - K*DF_quote); BUY=+1, SELL=-1.
"""
from __future__ import annotations

import calendar
from bisect import bisect_left
from datetime import date
from decimal import Decimal, localcontext
from typing import Any

PREC = 40
ONE = Decimal(1)
ZERO = Decimal(0)
METHOD_VERSION = "vector/1.0.0"


class PricingError(ValueError):
    pass


def dec(x: Any, field: str = "value") -> Decimal:
    from app.core.money import NumericError, to_decimal
    try:
        return to_decimal(x, field=field)
    except NumericError as exc:
        raise PricingError(str(exc)) from exc


def add_months(d: date, m: int) -> date:
    y = d.year + (d.month - 1 + m) // 12
    mo = (d.month - 1 + m) % 12 + 1
    return date(y, mo, min(d.day, calendar.monthrange(y, mo)[1]))


def schedule(effective: date, maturity: date, freq_months: int) -> list[date]:
    if freq_months <= 0:
        raise PricingError("payment frequency must be positive months")
    if maturity <= effective:
        raise PricingError("maturity must be after effective date")
    out, k = [effective], 1
    while True:
        d = add_months(effective, k * freq_months)
        if d > maturity:
            raise PricingError("irregular schedule: maturity is not an integer number of periods (stub periods unsupported in P0)")
        out.append(d)
        if d == maturity:
            return out
        k += 1


def accrual(s: date, e: date, dc: str) -> Decimal:
    if dc == "ACT/360":
        return Decimal((e - s).days) / Decimal(360)
    if dc == "ACT/365F":
        return Decimal((e - s).days) / Decimal(365)
    if dc == "30/360":
        d1, d2 = min(s.day, 30), e.day
        if d2 == 31 and d1 == 30:
            d2 = 30
        return Decimal(360 * (e.year - s.year) + 30 * (e.month - s.month) + (d2 - d1)) / Decimal(360)
    raise PricingError(f"unsupported day count {dc!r}")


class Curve:
    def __init__(self, curve_id: str, points: list[list[str]]):
        self.curve_id = curve_id
        with localcontext() as c:
            c.prec = PREC
            self.t = [dec(p[0], f"{curve_id}.t") for p in points]
            self.df = [dec(p[1], f"{curve_id}.df") for p in points]
            self._ln = [x.ln() for x in self.df]
        self._cache: dict[Decimal, Decimal] = {}

    def df_at(self, t: Decimal) -> Decimal:
        hit = self._cache.get(t)
        if hit is not None:
            return hit
        if t < 0:
            raise PricingError(f"{self.curve_id}: negative time {t}")
        if t == 0:
            return ONE
        i = bisect_left(self.t, t)
        if i < len(self.t) and self.t[i] == t:
            v = self.df[i]
        elif i >= len(self.t):
            raise PricingError(f"{self.curve_id}: t={t} beyond last curve node {self.t[-1]} (no extrapolation)")
        else:
            with localcontext() as c:
                c.prec = PREC
                if i == 0:
                    t0, l0 = ZERO, ZERO
                else:
                    t0, l0 = self.t[i - 1], self._ln[i - 1]
                w = (t - t0) / (self.t[i] - t0)
                v = (l0 + w * (self._ln[i] - l0)).exp()
        self._cache[t] = v
        return v


class MarketData:
    def __init__(self, payload: dict):
        self.payload = payload
        self.curves = {cid: Curve(cid, c["points"]) for cid, c in payload.get("curves", {}).items()}
        self.curve_ccy = {cid: c.get("currency") for cid, c in payload.get("curves", {}).items()}
        self.fx = {k: dec(v, f"fx.{k}") for k, v in payload.get("fx", {}).items()}
        self.marks = {k: dec(v, f"marks.{k}") for k, v in payload.get("marks", {}).items()}
        self.vols = {k: dec(v, f"vols.{k}") for k, v in payload.get("vols", {}).items()}
        self.rates = {k: dec(v, f"rates.{k}") for k, v in payload.get("rates", {}).items()}
        self.fixings = {k: dec(v, f"fixings.{k}") for k, v in payload.get("fixings", {}).items()}

    def curve(self, cid: str) -> Curve:
        if cid not in self.curves:
            raise PricingError(f"missing curve {cid!r}")
        return self.curves[cid]

    def fx_rate(self, base: str, quote: str) -> Decimal:
        """quote-per-base. Orientation explicit: uses direct pair, else reciprocal of the reverse pair."""
        if base == quote:
            return ONE
        if base + quote in self.fx:
            return self.fx[base + quote]
        if quote + base in self.fx:
            with localcontext() as c:
                c.prec = PREC
                return ONE / self.fx[quote + base]
        raise PricingError(f"missing FX rate {base}/{quote}")

    def convert(self, amount: Decimal, frm: str, to: str) -> Decimal:
        if frm == to:
            return amount
        with localcontext() as c:
            c.prec = PREC
            return amount * self.fx_rate(frm, to)


def validate_snapshot_payload(p: dict) -> list[str]:
    """V01 validation. Negative rates are legal; NaN/Inf, non-monotonic grids, non-positive DF, FX non-reciprocity are not."""
    errs: list[str] = []
    if not isinstance(p, dict):
        return ["snapshot payload must be an object"]
    for cid, c in (p.get("curves") or {}).items():
        pts = c.get("points") or []
        if len(pts) < 2:
            errs.append(f"curve {cid}: needs >=2 points")
            continue
        try:
            ts = [dec(x[0]) for x in pts]
            dfs = [dec(x[1]) for x in pts]
        except PricingError as exc:
            errs.append(f"curve {cid}: {exc}")
            continue
        if any(b <= a for a, b in zip(ts, ts[1:])):
            errs.append(f"curve {cid}: time grid not strictly increasing")
        if ts[0] < 0:
            errs.append(f"curve {cid}: negative time")
        if any(d <= 0 for d in dfs):
            errs.append(f"curve {cid}: discount factors must be positive")
        if ts[0] == 0 and dfs[0] != 1:
            errs.append(f"curve {cid}: DF at t=0 must be 1")
        if not c.get("currency"):
            errs.append(f"curve {cid}: currency missing")
    fx: dict[str, Decimal] = {}
    for k, v in (p.get("fx") or {}).items():
        try:
            d = dec(v)
        except PricingError as exc:
            errs.append(f"fx {k}: {exc}")
            continue
        if len(k) != 6 or d <= 0:
            errs.append(f"fx {k}: pair must be 6 letters BASEQUOTE with positive rate")
        fx[k] = d
    for k, d in fx.items():
        r = k[3:] + k[:3]
        if r in fx and abs(d * fx[r] - 1) > Decimal("1e-6"):
            errs.append(f"fx {k}/{r}: reciprocal tolerance exceeded")
    for sect in ("marks", "vols", "rates", "fixings"):
        for k, v in (p.get(sect) or {}).items():
            try:
                d = dec(v)
            except PricingError as exc:
                errs.append(f"{sect}.{k}: {exc}")
                continue
            if sect == "marks" and d <= 0:
                errs.append(f"marks.{k}: must be positive")
            if sect == "vols" and d <= 0:
                errs.append(f"vols.{k}: must be positive")
    return errs


def shock_payload(p: dict, spec: dict) -> dict:
    """Deterministic shocks on a COPY of the snapshot payload (baseline never mutated).
    spec: curve_shift_bp (parallel zero-rate bump, applied to all curves or `curve_ids`), fx_shock_pct {pair: decimal fraction},
    price_shock_pct (all marks, fraction), vol_shock_abs (additive)."""
    import copy
    q = copy.deepcopy(p)
    with localcontext() as c:
        c.prec = PREC
        bp = dec(spec.get("curve_shift_bp", "0"), "curve_shift_bp")
        if bp != 0:
            shift = bp / Decimal(10000)
            only = spec.get("curve_ids")
            for cid, cv in q.get("curves", {}).items():
                if only and cid not in only:
                    continue
                for pt in cv["points"]:
                    t, df = dec(pt[0]), dec(pt[1])
                    pt[1] = str(df * (-shift * t).exp())
        for pair, s in (spec.get("fx_shock_pct") or {}).items():
            if pair not in q.get("fx", {}):
                raise PricingError(f"fx shock for unknown pair {pair}")
            q["fx"][pair] = str(dec(q["fx"][pair]) * (ONE + dec(s, "fx_shock_pct")))
        ps = spec.get("price_shock_pct")
        if ps is not None:
            for k in q.get("marks", {}):
                q["marks"][k] = str(dec(q["marks"][k]) * (ONE + dec(ps, "price_shock_pct")))
        vs = spec.get("vol_shock_abs")
        if vs is not None:
            for k in q.get("vols", {}):
                nv = dec(q["vols"][k]) + dec(vs, "vol_shock_abs")
                if nv <= 0:
                    raise PricingError(f"vol shock makes vols.{k} non-positive")
                q["vols"][k] = str(nv)
    return q


def _parse_date(s: str, field: str) -> date:
    try:
        return date.fromisoformat(s)
    except (TypeError, ValueError) as exc:
        raise PricingError(f"{field}: invalid ISO date {s!r}") from exc


def value_irs(trade: dict, md: MarketData, val: date, trade_id: str) -> Decimal:
    """PV in trade currency from OUR perspective. PAY_FIXED => PV_float - PV_fixed."""
    with localcontext() as c:
        c.prec = PREC
        n = dec(trade["notional"], "notional")
        k = dec(trade["fixed_rate"], "fixed_rate")
        eff, mat = _parse_date(trade["effective"], "effective"), _parse_date(trade["maturity"], "maturity")
        disc, proj = md.curve(trade["curve_disc"]), md.curve(trade["curve_proj"])
        dates = schedule(eff, mat, int(trade["freq_months"]))
        pv_fixed = pv_float = ZERO
        for s, e in zip(dates, dates[1:]):
            if e <= val:
                continue  # paid
            t_pay = Decimal((e - val).days) / Decimal(365)
            dfd = disc.df_at(t_pay)
            pv_fixed += n * k * accrual(s, e, trade["fixed_dc"]) * dfd
            if s >= val:
                ts = Decimal((s - val).days) / Decimal(365)
                pv_float += n * (proj.df_at(ts) / proj.df_at(t_pay) - ONE) * dfd
            else:
                key = f"{trade_id}|{s.isoformat()}"
                if key not in md.fixings:
                    raise PricingError(f"missing fixing {key} for reset-in-progress coupon")
                pv_float += n * md.fixings[key] * accrual(s, e, trade["float_dc"]) * dfd
        pv = pv_float - pv_fixed if trade["pay_receive"] == "PAY_FIXED" else pv_fixed - pv_float
        return pv


def value_fxf(trade: dict, md: MarketData, val: date) -> Decimal:
    """PV in QUOTE currency. BUY base: N*(S*DF_base - K*DF_quote)."""
    with localcontext() as c:
        c.prec = PREC
        n, k = dec(trade["notional_base"], "notional_base"), dec(trade["strike"], "strike")
        mat = _parse_date(trade["maturity"], "maturity")
        t = Decimal((mat - val).days) / Decimal(365)
        s = md.fx_rate(trade["base_ccy"], trade["quote_ccy"])
        pv = n * (s * md.curve(trade["curve_base"]).df_at(t) - k * md.curve(trade["curve_quote"]).df_at(t))
        return pv if trade["side"] == "BUY" else -pv


def residual_years(trade: dict, val: date) -> Decimal:
    mat = _parse_date(trade["maturity"], "maturity")
    return Decimal((mat - val).days) / Decimal(365)
