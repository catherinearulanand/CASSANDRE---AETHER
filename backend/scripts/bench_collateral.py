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


def cold_once(n: int) -> None:
    t0 = time.perf_counter()
    dt, res = one_pipeline(n)
    print(json.dumps({"cold_total_incl_setup_s": time.perf_counter() - t0, "pipeline_s": dt, "status": res["solver_status"],
                      "verifier_ok": res["verification"]["ok"] if res.get("verification") else None}))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lots", type=int, default=1000)
    ap.add_argument("--reps", type=int, default=30)
    ap.add_argument("--warmups", type=int, default=5)
    ap.add_argument("--cold-runs", type=int, default=5)
    ap.add_argument("--cold-once", action="store_true")
    a = ap.parse_args()
    if a.cold_once:
        return cold_once(a.lots)

    cold = []
    for _ in range(a.cold_runs):
        out = subprocess.run([sys.executable, __file__, "--cold-once", "--lots", str(a.lots)], capture_output=True, text=True, check=True)
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
    os.environ["AETHER_DATABASE_URL"] = f"sqlite:///{Path(tmp, 'b.db').as_posix()}"
    from app.core import services
    from app.db.base import SessionLocal, create_all
    import app.engines.evidence  # noqa: F401  (registers evidence.append)
    import app.engines.collateral.services as S
    create_all()
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
            out = S.optimise(s, "bench", "MR", "BENCH", None, None, AS_OF + timedelta(days=2), 30, actor_id="bench")
            s.commit()
            dt = time.perf_counter() - t
        if i >= a.warmups:
            svc.append(dt)
            svc_status.add((out["solver_status"], out["state"]))

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
                        "solver": last["solver"] if last else None, "db": "sqlite (WAL)", "network": "none",
                        "commit": commit, "uncommitted_changes_possible": True},
        "data": {"tier": "DEMO-like single pool", "lots_total": a.lots, "eligible_lots": len(el), "seed": 42,
                 "required_coverage": str(required), "caps": len(groups), "generator": "scripts/bench_collateral.py gen_lots"},
        "method": {"warmups": a.warmups, "measured_reps": a.reps, "cold": f"{a.cold_runs} fresh subprocesses, first call each",
                   "concurrency": 1},
        "results": {
            "pipeline_warm (solve+round+verify+baseline)": summ(pipe),
            "solver_only_warm": summ(solv),
            "service_optimise_end_to_end_warm (DB+eligibility+pipeline+persist+evidence)": summ(svc),
            "pipeline_cold_s": [round(c["pipeline_s"], 4) for c in cold],
            "cold_incl_import_and_setup_s": [round(c["cold_total_incl_setup_s"], 4) for c in cold],
        },
        "failures": {"pipeline_failures_or_unverified": fails, "service_outcomes": sorted(map(list, svc_status))},
        "target_met": {"pipeline_warm_p95_lt_2s": pct(pipe, .95) < 2.0, "service_warm_p95_lt_2s": pct(svc, .95) < 2.0},
        "notes": ["Engineering target, not an SLA; numbers apply to this machine/run only.",
                  "Failed or slow measurements are retained in the aggregates, not discarded."],
    }
    out_path = ROOT / "reports" / "benchmarks" / "collateral.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2))
    print(json.dumps(report["results"], indent=2))
    print("target_met:", report["target_met"], "-> wrote", out_path)


if __name__ == "__main__":
    main()
