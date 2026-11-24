"""A3 VECTOR benchmark: OTC margin for 500 vanilla trades (250 IRS + 250 FX forwards), DEMO tier, synthetic.

Run:  cd backend && python -m app.engines.risk.benchmark [--reps 30] [--cold-runs 5]
Writes reports/benchmarks/risk.json. Reports every measurement summary (no cherry-picking): p50/p95/p99, n, failures.
  cold  = FIRST compute in a fresh Python process (imports done, empty result cache + fresh curve caches), repeated in N subprocesses
  warm  = subsequent computes in the same process with inputs varied so the result cache is NOT hit
  compute-only = value_set() pricing of 500 trades; end-to-end = compute_margin incl. DB reads, persistence and evidence append
Target (engineering target, not an achieved claim): p95 < 2 s for 500 trades.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
import tempfile
import time
from datetime import date, datetime, timezone
from pathlib import Path

N_IRS = N_FXF = 250


def _setup(db_path: str):
    os.environ["AETHER_DATABASE_URL"] = f"sqlite:///{Path(db_path).as_posix()}"
    from app.core import services
    from app.core.canonical import content_hash
    from app.db.base import SessionLocal, create_all
    from app.engines.risk import fixtures as fx
    from app.engines.risk import services as svc
    import app.engines.evidence  # noqa: F401  registers evidence.append
    create_all()
    terms = {"base_currency": "USD", "threshold_a": "10000000", "threshold_b": "10000000", "mta_a": "500000", "mta_b": "500000", "independent_amount_a": "0",
             "independent_amount_b": "0", "rounding_increment": "100000", "rounding_mode": "UP", "mta_semantics": "GTE"}
    th = content_hash(terms)
    services.register("contracts.resolve_terms", lambda s, t, ns, a, k, f=None: {"status": "RESOLVED", "terms_hash": th, "terms": terms, "blocking_reasons": []})
    tenant = "bench-risk"
    with SessionLocal() as s:
        fx.seed_fixtures(s, tenant, include_bad_total=False)
        trades = []
        for i in range(N_IRS):
            trades.append({"trade_id": f"B-IRS-{i:04d}", "master_id": "MA-BENCH", "netting_set_id": "NS-BENCH", "type": "IRS", "pay_receive": "PAY_FIXED" if i % 2 else "RECEIVE_FIXED",
                           "notional": str(1_000_000 + 10_000 * i), "currency": "USD", "fixed_rate": f"0.0{3 + i % 3}{i % 7}", "effective": "2026-08-17",
                           "maturity": f"{2028 + i % 3}-08-17", "freq_months": 6, "fixed_dc": "ACT/360", "float_dc": "ACT/360", "curve_disc": "USD-SOFR-DISC", "curve_proj": "USD-SOFR-PROJ"})
        for i in range(N_FXF):
            trades.append({"trade_id": f"B-FXF-{i:04d}", "master_id": "MA-BENCH", "netting_set_id": "NS-BENCH", "type": "FXF", "side": "BUY" if i % 2 else "SELL", "base_ccy": "EUR", "quote_ccy": "USD",
                           "notional_base": str(500_000 + 5_000 * i), "strike": f"1.{10 + i % 9}", "maturity": f"2026-{10 + i % 3}-17", "curve_base": "EUR-FLAT0", "curve_quote": "USD-FLAT0"})
        svc.import_portfolio(s, tenant, trades=trades)
        s.commit()
    return tenant, th, SessionLocal, svc


def _one(tenant, th, SessionLocal, svc, i: int) -> float:
    ctx = {"market_type": "OTC", "netting_set_id": "NS-BENCH", "as_of": "2026-08-17T04:00:00Z", "existing_vm_balance": {"PARTY_A": "0", "PARTY_B": str(i)}}
    t0 = time.perf_counter()
    with SessionLocal() as s:
        r = svc.compute_margin(s, tenant, ctx, "SNAP-QUIET-001", th, None, "OTC_VM_SIMPLE_V1")
        s.commit()
    dt = (time.perf_counter() - t0) * 1000
    if r["status"] != "OK" or len(r["trade_valuations"]) != 500:
        raise RuntimeError(f"benchmark compute failed: {r['status']} {r['blocking_reasons'][:2]}")
    return dt


def _pct(xs, p):
    xs = sorted(xs)
    k = (len(xs) - 1) * p / 100
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def _summ(xs):
    return {"n": len(xs), "p50_ms": round(_pct(xs, 50), 2), "p95_ms": round(_pct(xs, 95), 2), "p99_ms": round(_pct(xs, 99), 2), "min_ms": round(min(xs), 2), "max_ms": round(max(xs), 2)}


def child(db: str):
    tenant, th, SL, svc = _setup(db)
    print(json.dumps({"cold_ms": _one(tenant, th, SL, svc, 0)}))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reps", type=int, default=30)
    ap.add_argument("--warmups", type=int, default=5)
    ap.add_argument("--cold-runs", type=int, default=5)
    ap.add_argument("--child", default=None)
    a = ap.parse_args()
    if a.child:
        return child(a.child)
    cold = []
    for _ in range(a.cold_runs):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            out = subprocess.run([sys.executable, "-m", "app.engines.risk.benchmark", "--child", str(Path(d, "b.db"))], capture_output=True, text=True, cwd=Path(__file__).resolve().parents[3])
            if out.returncode != 0:
                raise SystemExit(out.stderr[-2000:])
            cold.append(json.loads(out.stdout.strip().splitlines()[-1])["cold_ms"])
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        tenant, th, SL, svc = _setup(str(Path(d, "w.db")))
        from decimal import Decimal
        from sqlalchemy import select
        from app.engines.risk.models import RiskSnapshot, RiskTrade
        from app.engines.risk.pricing import MarketData
        for i in range(1, a.warmups + 1):
            _one(tenant, th, SL, svc, i)
        warm = [_one(tenant, th, SL, svc, 100 + i) for i in range(a.reps)]
        with SL() as s:
            snap = s.execute(select(RiskSnapshot).where(RiskSnapshot.snapshot_id == "SNAP-QUIET-001")).scalar_one()
            rows = s.execute(select(RiskTrade).where(RiskTrade.netting_set_id == "NS-BENCH")).scalars().all()
            tr = svc._trade_dicts(rows)
        comp = []
        for _ in range(a.reps):
            t0 = time.perf_counter()
            vals, errs = svc.value_set(tr, MarketData(snap.payload), date(2026, 8, 17), "USD")
            comp.append((time.perf_counter() - t0) * 1000)
            assert not errs and len(vals) == 500
    rep = {"benchmark": "otc_margin_500_vanilla_trades", "tier": "DEMO (synthetic)", "trades": {"IRS": N_IRS, "FXF": N_FXF},
           "environment": {"os": platform.platform(), "cpu": platform.processor(), "cpu_count": os.cpu_count(), "python": sys.version.split()[0],
                           "database": "SQLite (WAL, BEGIN IMMEDIATE) local file; PostgreSQL NOT measured",
                           "network_mode": "offline (no network calls)", "numerics": "python decimal prec=40"},
           "method": {"warmups": a.warmups, "repetitions": a.reps, "cold_runs_fresh_processes": a.cold_runs},
           "measured_at": datetime.now(timezone.utc).isoformat(),
           "cold_end_to_end_ms": _summ(cold), "warm_end_to_end_ms": _summ(warm), "warm_compute_only_pricing_ms": _summ(comp),
           "target": {"p95_ms_lt": 2000, "scope": "engineering target (spec section 14), not an achieved claim"}}
    rep["target"]["met_warm_p95"] = rep["warm_end_to_end_ms"]["p95_ms"] < 2000
    rep["target"]["met_cold_p95"] = rep["cold_end_to_end_ms"]["p95_ms"] < 2000
    out = Path(__file__).resolve().parents[4] / "reports" / "benchmarks" / "risk.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rep, indent=2), encoding="utf-8")
    print(json.dumps(rep, indent=2))


if __name__ == "__main__":
    main()
