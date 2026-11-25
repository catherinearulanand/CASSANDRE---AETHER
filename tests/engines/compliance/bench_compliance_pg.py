"""Compliance benchmark on PostgreSQL (NOT collected by pytest). Writes reports/benchmarks/compliance_pg.json.

    python tests/engines/compliance/bench_compliance_pg.py

Measures, with real persistence and evidence writes (no stubs):
  A. 2,000 position rows x 50 synthetic rules: one `evaluate` call evaluates ALL 50 rules over the same 2,000 rows
     (each rule = one sum_by over its own contract group). Counts APPLICABLE evaluations actually produced.
     cold  = first evaluation of fresh inputs (new as_of): evaluate + persist facts/trace + evidence + events + cases
     warm  = identical repeat of the same inputs (idempotent dedupe path: facts hashed, existing rows returned)
     n = 30 each; p50/p95/max in milliseconds.
  B. Event burst: 240 independent failing events from 8 concurrent workers; per-event detection latency =
     submit -> case committed and visible; plus throughput.  Position facts are caller-supplied (labelled), margin services not used.
The database is aether_t5 (this agent's own); machine load is sampled and recorded because other agents share the host.
"""
import json
import os
import statistics
import subprocess
import sys
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "backend"))
pw = (ROOT / "var" / "pg.pw").read_text().strip()
os.environ["AETHER_DATABASE_URL"] = f"postgresql+psycopg://aether:{pw}@127.0.0.1:5433/aether_t5"
os.environ["AETHER_AUTO_CREATE_SCHEMA"] = "true"

from sqlalchemy import func, select  # noqa: E402

import app.main  # noqa: E402,F401
from app.db.base import SessionLocal, create_all  # noqa: E402
from app.engines.compliance import engine as E  # noqa: E402
from app.engines.compliance.controls import register_control  # noqa: E402
from app.engines.compliance.models import CmpCase, CmpEvaluation  # noqa: E402

N_ROWS, N_RULES, N_RUNS, BURST, WORKERS = 2000, 50, 30, 240, 8
ASOF0 = datetime(2026, 8, 17, 4, 0, tzinfo=UTC)


def load_snapshot() -> dict:
    out = {"cpu_count": os.cpu_count()}
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command",
                            "(Get-CimInstance Win32_Processor | Measure-Object -Property LoadPercentage -Average).Average; "
                            "[math]::Round((Get-CimInstance Win32_OperatingSystem).FreePhysicalMemory/1024)"],
                           capture_output=True, text=True, timeout=30)
        a = r.stdout.split()
        out["cpu_load_percent"], out["free_mem_mb"] = float(a[0]), float(a[1])
    except Exception as e:  # noqa: BLE001
        out["load_error"] = str(e)[:100]
    return out


def pct(xs, q):
    s = sorted(xs)
    return s[min(len(s) - 1, int(round(q * (len(s) - 1))))]


def summary(ms):
    return {"n": len(ms), "p50_ms": round(statistics.median(ms), 1), "p95_ms": round(pct(ms, 0.95), 1), "max_ms": round(max(ms), 1),
            "mean_ms": round(statistics.mean(ms), 1)}


def main():
    create_all()
    tenant = "bench-a5-" + uuid.uuid4().hex[:8]
    with SessionLocal() as s:
        from app.engines.compliance.fixtures import seed_fixtures
        seed_fixtures(s, tenant)
        for i in range(N_RULES):
            register_control(s, tenant, {
                "control_key": f"BENCH-{i:02d}", "family": "POSITION_LIMIT", "title": f"bench rule {i}", "rule_key": f"BENCH-RULE-{i:02d}",
                "rule_version": 1, "authority_type": "SYNTHETIC_POLICY", "severity": "LOW", "frequency": "EVENT",
                "scope": {"eq": ["market_type", "ETD_DOMESTIC"]},
                "condition": {"lte": [{"sum_by": ["positions", "lots", "contract", f"C{i % 25}"]}, 100000]},
                "required_inputs": ["positions"]})
        s.commit()
    keys = [f"BENCH-{i:02d}" for i in range(N_RULES)]
    rows = [{"contract": f"C{j % 25}", "lots": str(1 + (j % 7))} for j in range(N_ROWS)]
    ctx_base = {"market_type": "ETD_DOMESTIC", "venue": "DEMO_NSE", "account_id": "ACC-BENCH-1", "currency": "INR", "control_keys": keys}
    result = {"database": "PostgreSQL 16 (Docker, 127.0.0.1:5433, aether_t5)", "rows": N_ROWS, "rules": N_RULES, "runs": N_RUNS,
              "load_before": load_snapshot()}

    cold, warm, applicable = [], [], []
    for i in range(N_RUNS):
        ctx = {**ctx_base, "as_of": (ASOF0 + timedelta(minutes=i)).isoformat()}
        with SessionLocal() as s:
            t0 = time.perf_counter()
            r = E.evaluate(s, tenant, ctx, f"SNAP-B{i}", "rsh", facts={"positions": rows, "input_as_of": ctx["as_of"]})
            s.commit()
            cold.append((time.perf_counter() - t0) * 1000)
        applicable.append(sum(1 for e in r["evaluations"] if e["status"] != "NOT_APPLICABLE"))
        assert r["new_evaluations"] == N_RULES, r["new_evaluations"]
        with SessionLocal() as s:
            t0 = time.perf_counter()
            r2 = E.evaluate(s, tenant, ctx, f"SNAP-B{i}", "rsh", facts={"positions": rows, "input_as_of": ctx["as_of"]})
            s.commit()
            warm.append((time.perf_counter() - t0) * 1000)
        assert r2["new_evaluations"] == 0
    with SessionLocal() as s:
        stored = s.execute(select(func.count()).select_from(CmpEvaluation).where(CmpEvaluation.tenant_id == tenant)).scalar_one()
        status_counts = dict(s.execute(select(CmpEvaluation.status, func.count()).where(CmpEvaluation.tenant_id == tenant).group_by(CmpEvaluation.status)).all())
    result["evaluate_2000_rows_x_50_rules"] = {
        "applicable_evaluations_per_call": sorted(set(applicable)), "total_applicable_evaluations": sum(applicable),
        "persisted_evaluations": stored, "status_counts": status_counts,
        "cold_new_inputs": summary(cold), "warm_identical_repeat": summary(warm),
        "per_rule_cold_p50_ms": round(statistics.median(cold) / N_RULES, 2),
        "definition": "cold = evaluate+persist (facts incl. 2,000 rows are stored with EACH of the 50 evaluations); warm = idempotent repeat of identical inputs",
    }

    # ---------------------------------------------------------------- B: event burst with detection latency
    lat, errs = [], []
    lock = threading.Lock()
    work = list(range(BURST))
    t_start = time.perf_counter()

    def worker():
        while True:
            with lock:
                if not work:
                    return
                k = work.pop()
            ctx = {"market_type": "ETD_DOMESTIC", "venue": "DEMO_NSE", "account_id": f"ACC-BURST-{k:04d}", "currency": "INR",
                   "as_of": ASOF0.isoformat(), "control_keys": ["CMP-POSLIMIT-ETD"]}
            facts = {"position_lots": "101", "position_basis": "GROSS_FUTURES_EQUIV", "client_classification": "CLIENT", "input_as_of": ASOF0.isoformat()}
            t0 = time.perf_counter()
            try:
                with SessionLocal() as s:
                    r = E.evaluate(s, tenant, ctx, f"SNAP-BURST-{k}", "rsh", facts=facts)
                    s.commit()
                with SessionLocal() as s2:  # detection = a committed case another session can see
                    seen = s2.execute(select(CmpCase.case_id).where(CmpCase.tenant_id == tenant, CmpCase.subject_id == ctx["account_id"])).first()
                if not seen or not r["cases"]:
                    raise AssertionError("case not visible after commit")
                with lock:
                    lat.append((time.perf_counter() - t0) * 1000)
            except Exception as e:  # noqa: BLE001
                with lock:
                    errs.append(f"{type(e).__name__}: {e}"[:160])

    load_mid = load_snapshot()
    th = [threading.Thread(target=worker) for _ in range(WORKERS)]
    [t.start() for t in th]
    [t.join() for t in th]
    wall = time.perf_counter() - t_start
    with SessionLocal() as s:
        n_cases = s.execute(select(func.count()).select_from(CmpCase).where(CmpCase.tenant_id == tenant, CmpCase.control_key == "CMP-POSLIMIT-ETD")).scalar_one()
    result["event_burst"] = {"events": BURST, "workers": WORKERS, "detected_cases": n_cases, "errors": len(errs), "error_samples": errs[:3],
                             "wall_seconds": round(wall, 2), "events_per_second": round(len(lat) / wall, 1),
                             "detection_latency": summary(lat) if lat else None,
                             "definition": "latency = submit evaluate -> case committed and visible from a second session"}
    result["load_during_burst_start"] = load_mid
    result["load_after"] = load_snapshot()
    result["targets"] = {"note": "no performance target was specified in the compliance spec section used here; numbers are reported as measured",
                         "misses": []}
    result["caveats"] = [
        "Shared development host: other agents run builds/tests concurrently; see load_* fields. Numbers are not a clean-room measurement.",
        "Facts (including the 2,000-row positions array) are persisted with every evaluation by design (replayability); storage grows linearly with rules x rows.",
        "Burst positions are caller-supplied (CALLER_SUPPLIED provenance); margin/collateral services are not part of this latency.",
        "Single Python process, SQLAlchemy sessions on a local Docker PostgreSQL; no network hop."]
    out = ROOT / "reports" / "benchmarks" / "compliance_pg.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=1))
    print(json.dumps({k: result[k] for k in ("evaluate_2000_rows_x_50_rules", "event_burst")}, indent=1)[:2500])


if __name__ == "__main__":
    main()
