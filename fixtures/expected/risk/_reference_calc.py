"""INDEPENDENT reference for the 2y semi-annual IRS case. Plain floats + math.log/exp, different structure from
backend/app/engines/risk/pricing.py (which uses Decimal). Run once; output committed as irs_2y_semi.json.
Never imported by production code."""
import json
import math
from datetime import date
from pathlib import Path

VAL = date(2026, 8, 17)
DISC = [(0.0, 1.0), (0.5, 0.98), (1.0, 0.96), (2.0, 0.92), (5.0, 0.80)]
PROJ = [(0.0, 1.0), (0.5, 0.9775), (1.0, 0.955), (2.0, 0.915), (5.0, 0.79)]


def df(curve, t):
    for (t0, d0), (t1, d1) in zip(curve, curve[1:]):
        if t0 <= t <= t1:
            w = (t - t0) / (t1 - t0)
            return math.exp((1 - w) * math.log(d0) + w * math.log(d1))
    raise ValueError(t)


def months(d, m):
    y, mo = divmod(d.month - 1 + m, 12)
    return date(d.year + y, mo + 1, d.day)


dates = [months(VAL, 6 * k) for k in range(5)]
N, K = 50_000_000.0, 0.04
fixed = flt = 0.0
for s, e in zip(dates, dates[1:]):
    tau = (e - s).days / 360
    te, ts = (e - VAL).days / 365, (s - VAL).days / 365
    fixed += N * K * tau * df(DISC, te)
    flt += N * (df(PROJ, ts) / df(PROJ, te) - 1) * df(DISC, te)
out = {"case": "irs_2y_semi_pay_fixed", "valuation_date": "2026-08-17", "pv_pay_fixed": round(flt - fixed, 6),
       "pv_receive_fixed": round(fixed - flt, 6), "tolerance_abs": 0.001, "pv_fixed_leg": round(fixed, 6), "pv_float_leg": round(flt, 6),
       "method": "independent float reference (_reference_calc.py)"}
Path(__file__).with_name("irs_2y_semi.json").write_text(json.dumps(out, indent=2))
print(out)
