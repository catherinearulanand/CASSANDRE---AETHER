"""A4 ATLAS benchmark. Writes reports/benchmarks/collateral.json.

  python scripts/bench_collateral.py [--lots 1000] [--reps 30] [--warmups 5] [--cold-runs 5]

Measures, on DEMO-tier data (1,000 eligible continuous lots, deterministic seed):
  * pipeline  = eligibility already done; LP/HiGHS solve + Decimal rounding + INDEPENDENT verifier + baseline
  * solver    = HiGHS solve only
  * service   = full collateral.optimise (DB read, eligibility, pipeline, persistence, evidence) on a temp SQLite DB
Cold = first call in a FRESH process (n = --cold-runs subprocesses); warm = in-process repetitions after warmups.
Targets are engineering targets (p95 < 2 s), not achieved claims: this file records what was actually measured.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import random
import statistics
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
AS_OF = datetime(2026, 8, 17, 4, tzinfo=timezone.utc)
POLICY = {"schedule": [
    {"asset_class": "CASH", "currency": "INR", "valuation_pct": "100", "haircut_pct": "0"},
    {"asset_class": "GSEC", "currency": "INR", "valuation_pct": "90", "haircut_pct": "10", "min_rating": "AA"},
    {"asset_class": "CORP", "currency": "INR", "valuation_pct": "80", "haircut_pct": "20", "min_rating": "A-",
     "max_maturity_years": "5", "concentration_cap_pct": "25", "cap_denominator": "TOTAL_ALLOCATED"}],
    "issuer_group_cap_pct": "10", "issuer_cap_denominator": "TOTAL_ALLOCATED"}


def gen_lots(n: int, seed: int = 42) -> list[dict]:
    rng = random.Random(seed)
    lots = []
    for i in range(n):
        cls = ("CASH", "GSEC", "GSEC", "CORP")[i % 4]
        lots.append({
            "lot_id": f"L{i:05d}", "owner_entity": "ENT-MERIDIAN", "custody_account": "CUST", "asset_class": cls, "currency": "INR",
            "units": str(rng.randint(500, 20000)), "price": "1" if cls == "CASH" else str(rng.randint(95, 105)),
            "annual_cost_rate": f"{rng.uniform(0.03, 0.11):.4f}", "txn_cost_per_unit": "0", "settlement_lag_days": rng.randint(0, 2),
            "entitled_to": ["ACC-BENCH"], "rating": rng.choice(["AAA", "AA", "AA+"]) if cls != "CORP" else rng.choice(["AA", "A", "A+"]),
            "maturity_date": f"{2027 + rng.randint(0, 3)}-06-30", "issuer_group": f"IG{rng.randint(1, 120):03d}"})
    return lots


def pct(xs: list[float], q: float) -> float:
    xs = sorted(xs)
    k = (len(xs) - 1) * q
    f, c = int(k), min(int(k) + 1, len(xs) - 1)
    return xs[f] + (xs[c] - xs[f]) * (k - f)


def summ(xs: list[float]) -> dict:
    return {"n": len(xs), "p50_s": round(pct(xs, .5), 4), "p95_s": round(pct(xs, .95), 4), "p99_s": round(pct(xs, .99), 4),
            "mean_s": round(statistics.mean(xs), 4), "min_s": round(min(xs), 4), "max_s": round(max(xs), 4)}


def build_pipeline_inputs(n: int):
    from types import SimpleNamespace as NS
    from app.engines.collateral import engine as E
    from app.engines.collateral import valuation as V
    from app.engines.collateral.eligibility import assess_lot
    rows = []
    for l in gen_lots(n):
        rows.append(NS(id=l["lot_id"], lot_id=l["lot_id"], asset_class=l["asset_class"], currency="INR", issuer_class=None,
                       issuer_group=l["issuer_group"], units_u=V.to_u(Decimal(l["units"])), encumbered_u=0, reserved_u=0, lot_size_u=0,
                       price=l["price"], fx_to_target=None, rating=l["rating"], maturity_date=l["maturity_date"],
                       annual_cost_rate=l["annual_cost_rate"], txn_cost_per_unit="0", settlement_lag_days=l["settlement_lag_days"],
                       entitled_to=["ACC-BENCH"]))
    deadline = AS_OF + timedelta(days=2)
    src = [{"source": "CLEARING_POLICY", **POLICY}]
    asm = [assess_lot(r, obligation_id="ACC-BENCH", target_ccy="INR", sources=src, wrong_way_groups=[], as_of=AS_OF, deadline=deadline,
                      horizon_days=30) for r in rows]
    el = [a for a in asm if a.eligible]
    raw = {r.lot_id: {"lot_id": r.lot_id, "asset_class": r.asset_class, "currency": "INR", "issuer_class": None, "issuer_group": r.issuer_group,
                      "price": r.price, "fx_to_target": None, "rating": r.rating, "maturity_date": r.maturity_date,
                      "settlement_lag_days": r.settlement_lag_days, "entitled_to": ["ACC-BENCH"],
                      "available": str(V.from_u(r.units_u)), "lot_size": "0"} for r in rows}
    total = sum((a.available * a.e for a in el), Decimal(0))
    required = (total * Decimal("0.04")).quantize(Decimal("1"))
    params = {"sources": src, "wrong_way_issuer_groups": [], "obligation_id": "ACC-BENCH", "currency": "INR", "as_of": AS_OF.isoformat(),
              "deadline": deadline.isoformat(), "required": str(required), "overpost_allowance": "0", "stress": None}
    groups, meta = E.build_groups(el, src)
    return E, el, required, groups, meta, raw, params


def one_pipeline(n: int) -> tuple[float, dict]:
    E, el, required, groups, meta, raw, params = build_pipeline_inputs(n)
    t = time.perf_counter()
    res = E.solve_pipeline(el, required, groups, meta, raw, params)
    return time.perf_counter() - t, res


def cold_once(n: int, db_url: str | None = None, required: str | None = None, snapshot: str | None = None) -> None:
    t0 = time.perf_counter()
    dt, res = one_pipeline(n)
    out = {"cold_total_incl_setup_s": time.perf_counter() - t0, "pipeline_s": dt, "status": res["solver_status"],
           "verifier_ok": res["verification"]["ok"] if res.get("verification") else None}
    if db_url:  # cold SERVICE call: fresh process, first optimise() incl. connection pool + imports (on the real database)
        os.environ["AETHER_DATABASE_URL"] = db_url
        from app.core import services
        from app.db.base import SessionLocal
        import app.engines.evidence  # noqa: F401
        import app.engines.collateral.services as S
        services.register("risk.get_result", lambda s, t, rid: _margin(rid, required))
        t = time.perf_counter()
        with SessionLocal() as s:
            o = S.optimise(s, "bench", "MR", snapshot, None, None, AS_OF + timedelta(days=2), 30, actor_id="bench")
            s.commit()
        out.update(service_cold_s=time.perf_counter() - t, service_state=o["state"], service_solver=o["solver_status"])
    print(json.dumps(out))


def _margin(rid, required):
    return {"result_id": rid, "status": "OK", "market_type": "ETD_DOMESTIC", "currency": "INR", "account_id": "ACC-BENCH",
            "required_amount": {"amount": str(required), "currency": "INR"}, "result_hash": "0" * 64, "direction": "POST"}


def load_sample(seconds: float = 3.0) -> dict:
    import psutil
    c = psutil.cpu_percent(interval=seconds, percpu=True)
    top = sorted(((p.info["cpu_percent"] or 0, p.info["name"]) for p in psutil.process_iter(["name", "cpu_percent"])), reverse=True)[:5]
    return {"cpu_percent_total": round(sum(c) / len(c), 1), "cpu_percent_max_core": max(c), "top_processes_cpu": top,
            "mem_used_percent": psutil.virtual_memory().percent}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lots", type=int, default=1000)
    ap.add_argument("--reps", type=int, default=30)
    ap.add_argument("--warmups", type=int, default=5)
    ap.add_argument("--cold-runs", type=int, default=5)
    ap.add_argument("--cold-once", action="store_true")
    ap.add_argument("--db-url", default=None, help="PostgreSQL URL (service path on a real server); default temp SQLite")
    ap.add_argument("--out", default=None, help="report path (default reports/benchmarks/collateral.json)")
    ap.add_argument("--label", default=None, help="e.g. SCALE")
    ap.add_argument("--required", default=None)
    ap.add_argument("--snapshot", default=None)
    a = ap.parse_args()
    if a.cold_once:
        return cold_once(a.lots, a.db_url, a.required, a.snapshot)

    load_before = load_sample()
    pg_snapshot = None
    import_s = None
    req_for_cold = None
    if a.db_url:  # import ONCE on the real DB; cold subprocesses and warm loop then reuse the snapshot
        os.environ["AETHER_DATABASE_URL"] = a.db_url
        import app.engines.evidence  # noqa: F401
        import app.engines.collateral.services as S0
        from app.db.base import SessionLocal as SL0, create_all as create0
        create0()
        pg_snapshot = f"BENCH-{a.lots}-{int(time.time())}"
        t_imp = time.perf_counter()
        with SL0() as s0:
            S0.import_inventory(s0, "bench", actor_id="bench", role=None, correlation_id="b", payload={
                "snapshot_id": pg_snapshot, "as_of": AS_OF.isoformat(), "currency": "INR", "market_type": "ETD_DOMESTIC",
                "policy": POLICY, "lots": gen_lots(a.lots)})
            s0.commit()
        import_s = time.perf_counter() - t_imp
        req_for_cold = str(build_pipeline_inputs(a.lots)[2])
    cold = []
    for _ in range(a.cold_runs):
        cmd = [sys.executable, __file__, "--cold-once", "--lots", str(a.lots)]
        if a.db_url:
            cmd += ["--db-url", a.db_url, "--required", req_for_cold, "--snapshot", pg_snapshot]
        out = subprocess.run(cmd, capture_output=True, text=True, check=True)
        cold.append(json.loads(out.stdout.strip().splitlines()[-1]))

    from app.engines.collateral import optimiser as O
    E, el, required, groups, meta, raw, params = build_pipeline_inputs(a.lots)
    items = [O.Item(x.lot_id, float(x.e), float(x.cost_unit), float(x.available), 0.0) for x in el]
    for _ in range(a.warmups):
        E.solve_pipeline(el, required, groups, meta, raw, params)
    pipe, solv, fails = [], [], 0
    last = None
    for _ in range(a.reps):
        t = time.perf_counter()
        last = E.solve_pipeline(el, required, groups, meta, raw, params)
        pipe.append(time.perf_counter() - t)
        fails += 0 if last["solver_status"] in ("optimal", "feasible") and last["verification"]["ok"] else 1
        t = time.perf_counter()
        O.solve(items, float(required), groups)
        solv.append(time.perf_counter() - t)

    # full service path on a temp DB
    tmp = tempfile.mkdtemp(prefix="aether-bench-")
    if not a.db_url:
        os.environ["AETHER_DATABASE_URL"] = f"sqlite:///{Path(tmp, 'b.db').as_posix()}"
    from app.core import services
    from app.db.base import SessionLocal, create_all
    import app.engines.evidence  # noqa: F401  (registers evidence.append)
    import app.engines.collateral.services as S
    create_all()
    snap_id = pg_snapshot or "BENCH"
    if not a.db_url:
        lots = gen_lots(a.lots)
        with SessionLocal() as s:
            S.import_inventory(s, "bench", actor_id="bench", role=None, correlation_id="b", payload={
                "snapshot_id": "BENCH", "as_of": AS_OF.isoformat(), "currency": "INR", "market_type": "ETD_DOMESTIC", "policy": POLICY, "lots": lots})
            s.commit()
    services.register("risk.get_result", lambda s, t, rid: {"result_id": rid, "status": "OK", "market_type": "ETD_DOMESTIC", "currency": "INR",
                                                           "account_id": "ACC-BENCH", "required_amount": {"amount": str(required), "currency": "INR"},
                                                           "result_hash": "0" * 64, "direction": "POST"})
    svc, svc_status = [], set()
    for i in range(a.warmups + a.reps):
        with SessionLocal() as s:
            t = time.perf_counter()
            out = S.optimise(s, "bench", "MR", snap_id, None, None, AS_OF + timedelta(days=2), 30, actor_id="bench")
            s.commit()
            dt = time.perf_counter() - t
        if i >= a.warmups:
            svc.append(dt)
            svc_status.add((out["solver_status"], out["state"]))

    load_after = load_sample()
    db_info = "sqlite (WAL)"
    if a.db_url:
        from sqlalchemy import text as _t
        with SessionLocal() as s:
            db_info = s.execute(_t("select version()")).scalar_one().split(",")[0] + " (Docker, 127.0.0.1:5433, local loopback)"
    try:
        commit = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=ROOT).stdout.strip() or "unknown"
    except Exception:  # noqa: BLE001
        commit = "unknown"
    import numpy, scipy
    try:
        import psutil
        ram = round(psutil.virtual_memory().total / 2**30, 1)
    except Exception:  # noqa: BLE001
        ram = None
    report = {
        "engine": "ATLAS collateral", "target": "p95 < 2 s for 1,000 eligible continuous lots (solver + independent feasibility)",
        "environment": {"os": platform.platform(), "cpu": platform.processor(), "cpu_count": os.cpu_count(), "ram_gb": ram,
                        "python": sys.version.split()[0], "scipy": scipy.__version__, "numpy": numpy.__version__,
                        "solver": last["solver"] if last else None, "db": db_info, "network": "none (loopback)" if a.db_url else "none",
                        "machine_load_before": load_before, "machine_load_after": load_after,
                        "commit": commit, "uncommitted_changes_possible": True},
        "data": {"tier": a.label or "DEMO-like single pool", "lots_total": a.lots, "eligible_lots": len(el), "seed": 42,
                 "required_coverage": str(required), "caps": len(groups), "generator": "scripts/bench_collateral.py gen_lots"},
        "method": {"warmups": a.warmups, "measured_reps": a.reps, "cold": f"{a.cold_runs} fresh subprocesses, first call each",
                   "concurrency": 1},
        "results": {
            "pipeline_warm (solve+round+verify+baseline)": summ(pipe),
            "solver_only_warm": summ(solv),
            "service_optimise_end_to_end_warm (DB+eligibility+pipeline+persist+evidence)": summ(svc),
            "pipeline_cold_s": [round(c["pipeline_s"], 4) for c in cold],
            "cold_incl_import_and_setup_s": [round(c["cold_total_incl_setup_s"], 4) for c in cold],
            "pipeline_cold": summ([c["pipeline_s"] for c in cold]),
            **({"service_cold_first_call_fresh_process": summ([c["service_cold_s"] for c in cold]),
                "service_cold_outcomes": sorted({(c["service_state"], c["service_solver"]) for c in cold}),
                "inventory_import_once_s": round(import_s, 3)} if a.db_url else {}),
        },
        "failures": {"pipeline_failures_or_unverified": fails, "service_outcomes": sorted(map(list, svc_status))},
        "target_met": {"pipeline_warm_p95_lt_2s": pct(pipe, .95) < 2.0, "service_warm_p95_lt_2s": pct(svc, .95) < 2.0},
        "notes": ["Engineering target, not an SLA; numbers apply to this machine/run only.",
                  "Failed or slow measurements are retained in the aggregates, not discarded."],
    }
    out_path = Path(a.out) if a.out else ROOT / "reports" / "benchmarks" / "collateral.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2))
    print(json.dumps(report["results"], indent=2))
    print("target_met:", report["target_met"], "-> wrote", out_path)


if __name__ == "__main__":
    main()
