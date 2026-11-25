"""A3 VECTOR PostgreSQL benchmark (spec section 14). Synthetic data only; no network.

Run (PostgreSQL URL via environment so no credential is ever on a command line or in a file):
    cd backend
    AETHER_DATABASE_URL=postgresql+psycopg://USER:PASS@127.0.0.1:5433/aether_bench3 python -m app.engines.risk.benchmark_pg [--cold-runs 30] [--reps 50]
The database MUST be a dedicated, throw-away database whose name contains 'bench' (the script drops and recreates all tables in it).

Measures, each reported with n, p50/p95/p99/min/max (no cherry-picking; every repetition is kept in the JSON):
  OTC margin, 500 vanilla trades (250 IRS + 250 FX forwards)   cold = first compute in a fresh process (cold caches, new connection pool)
                                                               warm = same process after warm-ups, inputs varied so the result cache is NOT hit
                                                               pricing-only = value_set() with no DB
  ETD clearing file 2,000 component rows                       import (parse + validate + persist + evidence) and reconcile, end to end
  ETD positions 2,000 rows                                     atomic import_portfolio (validate + persist)
  OTC margin, 5,000 trades (SCALE tier, reported separately)
Scope: service level (includes DB reads/writes and the evidence append, excludes HTTP). Terms come from a fixed in-process provider, so the
contracts engine is excluded from the measurement. Contention: the machine was shared (see `contention`); a CPU calibration loop is timed
at start and end so interference is quantified instead of hidden.
Targets are engineering targets from the spec, not achieved claims: OTC 500 trades p95 < 2 s; ETD 2,000 rows < 5 s.
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import platform
import re
import subprocess
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
OUT = REPO / "reports" / "benchmarks" / "risk_pg.json"
AS_OF = "2026-08-17T04:00:00Z"
TERMS = {"base_currency": "USD", "threshold_a": "10000000", "threshold_b": "10000000", "mta_a": "500000", "mta_b": "500000", "independent_amount_a": "0",
         "independent_amount_b": "0", "rounding_increment": "100000", "rounding_mode": "UP", "mta_semantics": "GTE"}
TENANT = "bench-risk"
N_POS = 2000


def gen_trades(ns: str, n: int) -> list[dict]:
    half = n // 2
    out = []
    for i in range(half):
        out.append({"trade_id": f"{ns}-IRS-{i:05d}", "master_id": "MA-BENCH", "netting_set_id": ns, "type": "IRS", "pay_receive": "PAY_FIXED" if i % 2 else "RECEIVE_FIXED",
                    "notional": str(1_000_000 + 10_000 * i), "currency": "USD", "fixed_rate": f"0.0{3 + i % 3}{i % 7}", "effective": "2026-08-17",
                    "maturity": f"{2028 + i % 3}-08-17", "freq_months": 6, "fixed_dc": "ACT/360", "float_dc": "ACT/360", "curve_disc": "USD-SOFR-DISC", "curve_proj": "USD-SOFR-PROJ"})
    for i in range(n - half):
        out.append({"trade_id": f"{ns}-FXF-{i:05d}", "master_id": "MA-BENCH", "netting_set_id": ns, "type": "FXF", "side": "BUY" if i % 2 else "SELL", "base_ccy": "EUR", "quote_ccy": "USD",
                    "notional_base": str(500_000 + 5_000 * i), "strike": f"1.{10 + i % 9}", "maturity": f"2026-{10 + i % 3}-17", "curve_base": "EUR-FLAT0", "curve_quote": "USD-FLAT0"})
    return out


def pct(xs, p):
    xs = sorted(xs)
    k = (len(xs) - 1) * p / 100
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def summ(xs):
    return {"n": len(xs), "p50_ms": round(pct(xs, 50), 2), "p95_ms": round(pct(xs, 95), 2), "p99_ms": round(pct(xs, 99), 2),
            "min_ms": round(min(xs), 2), "max_ms": round(max(xs), 2), "samples_ms": [round(x, 2) for x in xs]}


def calib_ms() -> float:
    """Fixed pure-Python CPU workload (min of 5): its slowdown vs the first measurement quantifies machine contention."""
    best = 1e9
    for _ in range(5):
        t0 = time.perf_counter()
        s = 0
        for i in range(1_500_000):
            s += i * i
        best = min(best, (time.perf_counter() - t0) * 1000)
    return best


def ram_gb() -> float | None:
    try:
        class MS(ctypes.Structure):
            _fields_ = [("l", ctypes.c_ulong), ("m", ctypes.c_ulong), ("tp", ctypes.c_ulonglong), ("ap", ctypes.c_ulonglong), ("a", ctypes.c_ulonglong),
                        ("b", ctypes.c_ulonglong), ("c", ctypes.c_ulonglong), ("d", ctypes.c_ulonglong), ("e", ctypes.c_ulonglong)]
        m = MS(); m.l = ctypes.sizeof(MS)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
        return round(m.tp / 2 ** 30, 1)
    except Exception:  # noqa: BLE001
        return None


def init():
    """Imports + in-process terms provider (the contracts engine is excluded from the measurement)."""
    from app.core import services
    from app.core.canonical import content_hash
    from app.db.base import SessionLocal
    from app.engines.risk import services as svc
    import app.engines.evidence  # noqa: F401  registers evidence.append
    th = content_hash(TERMS)
    services.register("contracts.resolve_terms", lambda s, t, ns, a, k, f=None: {"status": "RESOLVED", "terms_hash": th, "terms": TERMS, "blocking_reasons": []})
    return SessionLocal, svc, th


def otc_once(SessionLocal, svc, th, ns: str, n: int, salt: int) -> float:
    ctx = {"market_type": "OTC", "netting_set_id": ns, "as_of": AS_OF, "existing_vm_balance": {"PARTY_A": "0", "PARTY_B": str(salt)}}
    t0 = time.perf_counter()
    with SessionLocal() as s:
        r = svc.compute_margin(s, TENANT, ctx, "SNAP-QUIET-001", th, None, "OTC_VM_SIMPLE_V1")
        s.commit()
    dt = (time.perf_counter() - t0) * 1000
    if r["status"] != "OK" or len(r["trade_valuations"]) != n:
        raise RuntimeError(f"benchmark compute failed: {r['status']} {r['blocking_reasons'][:2]}")
    return dt


def clearing_csv(fid: str, n: int) -> str:
    rows = ["file_id,account_id,component,amount,currency,aggregation_kind,as_of"]
    total = 0
    for k in range(n - 1):
        amt = 1000 + (k * 7) % 313
        total += amt
        rows.append(f"{fid},ACC-DEMO-NSE-001,COMP-{k:05d},{amt},INR,ADDITIVE,2026-08-17T03:00:00Z")
    rows.append(f"{fid},ACC-DEMO-NSE-001,TOTAL_REPORTED,{total},INR,REPORTED_TOTAL,2026-08-17T03:00:00Z")
    return "\n".join(rows)  # n rows: n-1 additive components + 1 reported total


def etd_once(SessionLocal, svc, tag: str, n: int = 2000) -> tuple[float, float]:
    text = clearing_csv(f"CLR-BENCH-{tag}", n)  # file generation is harness work and is NOT timed
    t0 = time.perf_counter()
    with SessionLocal() as s:
        f = svc.import_clearing_file(s, TENANT, csv_text=text, venue="DEMO_NSE", ctx={"actor_id": "bench"})
        s.commit()
    t1 = time.perf_counter()
    with SessionLocal() as s:
        r = svc.reconcile_clearing(s, TENANT, f["file_id"], None, ctx={"actor_id": "bench"})
        s.commit()
    t2 = time.perf_counter()
    if f["status"] != "RECONCILED" or r["status"] != "RECONCILED" or len(f["rows"]) != n:
        raise RuntimeError(f"ETD benchmark failed: {f['status']} {r['status']} rows={len(f['rows'])}")
    return (t1 - t0) * 1000, (t2 - t1) * 1000


def positions_once(SessionLocal, svc, rev: int) -> float:
    pos = [{"account_id": "ACC-BENCH-NSE", "instrument_id": f"BENCH-FUT-{k:05d}", "as_of": "2026-08-17T03:30:00Z", "revision": rev,
            "signed_lots": str(((k * 13) % 41) - 20), "settlement_price": f"{24000 + k % 500}.00"} for k in range(N_POS)]
    t0 = time.perf_counter()
    with SessionLocal() as s:
        r = svc.import_portfolio(s, TENANT, positions=pos, ctx={"actor_id": "bench"})
        s.commit()
    dt = (time.perf_counter() - t0) * 1000
    if r["added"]["positions"] != N_POS:
        raise RuntimeError(f"positions import added {r['added']}")
    return dt


def child(mode: str, ns: str, n: int):
    SL, svc, th = init()
    salt = os.getpid() * 1000 + int(time.time()) % 1000
    if mode == "otc":
        print(json.dumps({"cold_ms": otc_once(SL, svc, th, ns, n, salt)}))
    else:
        a, b = etd_once(SL, svc, f"C{salt}")
        print(json.dumps({"cold_import_ms": a, "cold_reconcile_ms": b, "cold_total_ms": a + b}))


def run_children(args: list[str], runs: int) -> list[dict]:
    out = []
    for _ in range(runs):
        r = subprocess.run([sys.executable, "-m", "app.engines.risk.benchmark_pg", *args], capture_output=True, text=True, cwd=REPO / "backend", env=os.environ.copy())
        if r.returncode != 0:
            raise SystemExit(r.stderr[-1500:])
        out.append(json.loads(r.stdout.strip().splitlines()[-1]))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cold-runs", type=int, default=30)
    ap.add_argument("--reps", type=int, default=50)
    ap.add_argument("--warmups", type=int, default=5)
    ap.add_argument("--scale-trades", type=int, default=5000)
    ap.add_argument("--scale-reps", type=int, default=10)
    ap.add_argument("--scale-cold-runs", type=int, default=5)
    ap.add_argument("--child", default=None)
    ap.add_argument("--ns", default="NS-BENCH")
    ap.add_argument("--n", type=int, default=500)
    a = ap.parse_args()
    url = os.environ.get("AETHER_DATABASE_URL", "")
    if not url.startswith("postgresql") or not re.search(r"bench", url.rsplit("/", 1)[-1]):
        raise SystemExit("AETHER_DATABASE_URL must point at a dedicated PostgreSQL database whose name contains 'bench'")
    if a.child:
        return child(a.child, a.ns, a.n)

    from sqlalchemy import select, text
    from app.db.base import Base, SessionLocal, get_engine, import_all_models
    from app.engines.risk import fixtures as fx
    from app.engines.risk.models import RiskSnapshot, RiskTrade
    from app.engines.risk.pricing import MarketData
    import_all_models()
    Base.metadata.drop_all(get_engine())
    Base.metadata.create_all(get_engine())
    SL, svc, th = init()
    with SL() as s:
        fx.seed_fixtures(s, TENANT, include_bad_total=False)
        svc.import_portfolio(s, TENANT, trades=gen_trades("NS-BENCH", 500) + gen_trades("NS-SCALE", a.scale_trades),
                             instruments=[{"instrument_id": f"BENCH-FUT-{k:05d}", "type": "FUT", "venue": "DEMO_NSE", "currency": "INR", "multiplier": "25", "expiry": "2026-09-29", "settlement_type": "CASH"}
                                          for k in range(N_POS)])
        s.commit()
        pg = s.execute(text("select version()")).scalar_one()
        shared = s.execute(text("show shared_buffers")).scalar_one()
    c0 = calib_ms()

    rep: dict = {"benchmark": "risk_engine_postgresql", "measured_at": datetime.now(timezone.utc).isoformat(), "tier": "DEMO (500 trades, 2,000 rows) + SCALE (reported separately)",
                 "environment": {"os": platform.platform(), "cpu": platform.processor(), "cpu_count": os.cpu_count(), "ram_gb": ram_gb(), "python": sys.version.split()[0],
                                 "postgres": pg.split(",")[0], "postgres_shared_buffers": shared, "postgres_runs_in": "Docker Desktop container on the same machine (loopback)",
                                 "driver": __import__("psycopg").__version__, "network_mode": "offline loopback only", "numerics": "python decimal prec=40",
                                 "scope": "service level: DB reads/writes + evidence append included, HTTP excluded; contracts engine excluded (fixed in-process terms provider)"},
                 "method": {"warmups": a.warmups, "warm_repetitions": a.reps, "cold_runs_fresh_processes": a.cold_runs, "scale_cold_runs": a.scale_cold_runs, "scale_repetitions": a.scale_reps,
                            "cold_definition": "first compute in a fresh Python process (imports done before timing; new connection pool; no result cache)",
                            "warm_definition": "same process after warm-ups; existing_vm_balance varied each call so the result cache is never hit"}}

    # ---- OTC 500
    cold = [c["cold_ms"] for c in run_children(["--child", "otc", "--ns", "NS-BENCH", "--n", "500"], a.cold_runs)]
    for i in range(a.warmups):
        otc_once(SL, svc, th, "NS-BENCH", 500, 900_000 + i)
    warm = [otc_once(SL, svc, th, "NS-BENCH", 500, 1_000_000 + i) for i in range(a.reps)]
    with SL() as s:
        snap = s.execute(select(RiskSnapshot).where(RiskSnapshot.snapshot_id == "SNAP-QUIET-001")).scalar_one()
        tr = svc._trade_dicts(s.execute(select(RiskTrade).where(RiskTrade.netting_set_id == "NS-BENCH")).scalars().all())
    pricing = []
    for _ in range(a.reps):
        t0 = time.perf_counter()
        vals, errs = svc.value_set(tr, MarketData(snap.payload), date(2026, 8, 17), "USD")
        pricing.append((time.perf_counter() - t0) * 1000)
        assert not errs and len(vals) == 500
    rep["otc_margin_500_trades"] = {"cold_end_to_end_ms": summ(cold), "warm_end_to_end_ms": summ(warm), "warm_pricing_only_ms": summ(pricing),
                                    "target": {"p95_ms_lt": 2000, "met_warm_p95": pct(warm, 95) < 2000, "met_cold_p95": pct(cold, 95) < 2000}}
    c1 = calib_ms()

    # ---- ETD 2,000 rows
    ecold = run_children(["--child", "etd"], max(5, a.cold_runs // 3))
    for i in range(a.warmups):
        etd_once(SL, svc, f"W{i}")
    imp, rec = [], []
    for i in range(max(20, a.reps // 2)):
        x, y = etd_once(SL, svc, f"R{i}")
        imp.append(x); rec.append(y)
    tot = [x + y for x, y in zip(imp, rec)]
    posn = []
    for i in range(1, 11):
        posn.append(positions_once(SL, svc, 10_000 + i))
    rep["etd_clearing_file_2000_rows"] = {"cold_import_ms": summ([c["cold_import_ms"] for c in ecold]), "cold_reconcile_ms": summ([c["cold_reconcile_ms"] for c in ecold]),
                                          "cold_total_ms": summ([c["cold_total_ms"] for c in ecold]), "warm_import_ms": summ(imp), "warm_reconcile_ms": summ(rec), "warm_total_ms": summ(tot),
                                          "target": {"total_ms_lt": 5000, "met_warm_p95": pct(tot, 95) < 5000, "met_cold_p95": pct([c['cold_total_ms'] for c in ecold], 95) < 5000},
                                          "rows": 2000, "includes": "parse + strict validation + persist + evidence + independent reconcile; CSV text generation excluded"}
    rep["etd_positions_import_2000_rows"] = {"warm_import_ms": summ(posn), "rows": N_POS, "target": {"total_ms_lt": 5000, "met_p95": pct(posn, 95) < 5000},
                                             "includes": "atomic validation + persist of 2,000 position rows (instruments already imported) + evidence"}
    c2 = calib_ms()

    # ---- SCALE
    n = a.scale_trades
    scold = [c["cold_ms"] for c in run_children(["--child", "otc", "--ns", "NS-SCALE", "--n", str(n)], a.scale_cold_runs)]
    for i in range(2):
        otc_once(SL, svc, th, "NS-SCALE", n, 2_000_000 + i)
    swarm = [otc_once(SL, svc, th, "NS-SCALE", n, 3_000_000 + i) for i in range(a.scale_reps)]
    rep[f"otc_margin_{n}_trades_SCALE"] = {"cold_end_to_end_ms": summ(scold), "warm_end_to_end_ms": summ(swarm),
                                           "target": "none specified for this tier; reported for scaling behaviour only (spec: do not claim beyond what is measured)"}
    c3 = calib_ms()
    rep["contention"] = {"calibration_ms_start": round(c0, 1), "after_otc_ms": round(c1, 1), "after_etd_ms": round(c2, 1), "end_ms": round(c3, 1),
                         "max_slowdown_vs_start": round(max(c1, c2, c3) / c0, 2),
                         "note": "other engineering workers were running tests/servers on this machine and on the same PostgreSQL container; a ratio well above 1.0 means the timings are pessimistic/noisy"}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(rep, indent=1), encoding="utf-8")
    brief = {k: {kk: (vv["p50_ms"], vv["p95_ms"]) if isinstance(vv, dict) and "p50_ms" in vv else vv for kk, vv in v.items() if kk != "includes"} for k, v in rep.items() if isinstance(v, dict) and k.startswith(("otc", "etd"))}
    print(json.dumps({"contention": rep["contention"], "summary(p50,p95 ms)": brief}, indent=1)[:6000])


if __name__ == "__main__":
    main()
