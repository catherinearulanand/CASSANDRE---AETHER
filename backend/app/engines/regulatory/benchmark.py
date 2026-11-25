"""resolve_rules benchmark: 50 rules x 2,000 account contexts (spec section 14: rule evaluation target p95 < 3 s for 2,000 rows / 50 rules).

    cd backend; AETHER_DATABASE_URL=postgresql+psycopg://... python -m app.engines.regulatory.benchmark [--reps 30] [--out ../reports/benchmarks/regulatory.json]

Measures, on a freshly created tenant, with 5 warmups and N timed repetitions of the FULL 2,000-context sweep:
  per_call   : the public service (one resolve_rules call per context)        <- the frozen contract path
  batch      : regulatory.resolve_rules_batch (rule set prepared once)        <- additive service
  cold       : the very first sweep in a fresh process (separate from the warm series)
Also reports the number of (rule x context) scope evaluations performed (the unit the spec asks to count), the DB, and the hardware.
Nothing is cached across repetitions except what the database/driver itself caches; failed or slow runs are NOT discarded.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import statistics
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

from app.auth.security import Principal
from app.db.base import SessionLocal, create_all, get_engine
import app.engines.evidence  # noqa: F401  (registers evidence.* services)
import app.engines.regulatory  # noqa: F401
from app.engines.regulatory import registry
from app.engines.regulatory.resolve import resolve_rules, resolve_rules_batch

N_RULES, N_CONTEXTS = 50, 2000
AS_OF = datetime(2026, 8, 17, 4, 0, tzinfo=UTC)
VENUES = ["DEMO_NSE", "DEMO_IFSC", "DEMO_BSE"]
MARKETS = ["ETD_DOMESTIC", "ETD_IFSC", "OTC"]
CATS = ["MEMBER_BROKER", "CLEARING_MEMBER", "CLIENT"]


def P(uid, tenant, role):
    return Principal(uid, tenant, uid, uid, (role,), (), "")


def build_tenant() -> str:
    """50 ACTIVE synthetic rules: scope on market/venue/entity_category/currency; simple conditions + parameters. Full lifecycle via the registry."""
    tenant = "bench-" + uuid.uuid4().hex[:8]
    maker, checker = P("maker-b", tenant, "maker"), P("checker-b", tenant, "checker")
    text = ("Benchmark source. The member shall hold 100 lots. The broker shall not exceed 7 lots. "
            "Coverage must be at least 100 per cent of margin.\n")
    with SessionLocal() as db:
        src, _ = registry.ingest_document(db, maker, text.encode(), None, {"authority_type": "SYNTHETIC", "title": "bench"})
        i = text.index("The broker shall not exceed 7 lots")
        span = registry.make_span(db, maker, src, i, i + len("The broker shall not exceed 7 lots"), "MANUAL")
        for n in range(N_RULES):
            m, v, c = MARKETS[n % 3], VENUES[n % 3], CATS[(n // 3) % 3]
            scope = {"all": [{"eq": ["market_type", m]}, {"eq": ["venue", v]}] + ([{"eq": ["entity_category", c]}] if n % 2 else [])}
            body = {"rule_key": f"BENCH-{n:03d}", "authority_type": "SYNTHETIC_POLICY", "scope": scope,
                    "condition": {"exists": "required_margin_inr"}, "parameters": {"limit": {"value": str(10 + n), "unit": "LOTS"}},
                    "effective_from": datetime(2026, 1, 1, tzinfo=UTC), "span_ids": [span.span_id], "rationale": "benchmark rule",
                    "test_vectors": [{"name": "in-scope", "context": {"market_type": m, "venue": v, "entity_category": c, "required_margin_inr": "1"},
                                      "expect_scope": "TRUE", "expect_condition": "TRUE"}]}
            r = registry.create_rule(db, maker, body)
            registry.submit(db, maker, r.rule_id, "bench", None)
            registry.review(db, checker, r.rule_id, "APPROVE", "bench", None)
            registry.activate(db, checker, r.rule_id, None, "bench", as_of=AS_OF)
        db.commit()
    return tenant


def contexts() -> list[dict]:
    out = []
    for k in range(N_CONTEXTS):
        ctx = {"market_type": MARKETS[k % 3], "venue": VENUES[(k // 3) % 3], "jurisdiction": "IN", "instrument_class": "INDEX_FUTURE",
               "account_id": f"ACC-{k:05d}", "currency": "INR" if k % 3 != 1 else "USD"}
        if k % 7:                      # ~1/7 of contexts lack entity_category => rules scoped on it are UNKNOWN, never applicable
            ctx["entity_category"] = CATS[k % 3]
        out.append(ctx)
    return out


def pct(xs, q):
    s = sorted(xs)
    return s[min(len(s) - 1, int(round(q * (len(s) - 1))))]


def summarise(xs):
    return {"n": len(xs), "p50_s": round(statistics.median(xs), 4), "p95_s": round(pct(xs, 0.95), 4), "p99_s": round(pct(xs, 0.99), 4),
            "min_s": round(min(xs), 4), "max_s": round(max(xs), 4), "mean_s": round(statistics.fmean(xs), 4), "all_s": [round(x, 4) for x in xs]}


def sweep_per_call(tenant, ctxs):
    with SessionLocal() as db:
        t = time.perf_counter()
        n_app = 0
        for c in ctxs:
            r = resolve_rules(db, tenant, c, AS_OF, None)
            n_app += len(r["applicable"])
        db.commit()
        return time.perf_counter() - t, n_app


def sweep_batch(tenant, ctxs):
    with SessionLocal() as db:
        t = time.perf_counter()
        res = resolve_rules_batch(db, tenant, ctxs, AS_OF, None)
        n_app = sum(len(r["applicable"]) for r in res)
        db.commit()
        return time.perf_counter() - t, n_app


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--reps", type=int, default=30)
    ap.add_argument("--warmups", type=int, default=5)
    ap.add_argument("--out", default=str(Path(__file__).resolve().parents[4] / "reports" / "benchmarks" / "regulatory.json"))
    a = ap.parse_args(argv)
    create_all() if get_engine().dialect.name == "sqlite" or os.environ.get("AETHER_AUTO_CREATE_SCHEMA") == "true" else None
    t0 = time.perf_counter()
    tenant = build_tenant()
    setup_s = time.perf_counter() - t0
    ctxs = contexts()
    n_evals = N_RULES * N_CONTEXTS
    out = {"benchmark": "regulatory.resolve_rules", "rules": N_RULES, "contexts": N_CONTEXTS, "scope_evaluations_per_sweep": n_evals,
           "target": "p95 < 3 s per sweep of 2,000 contexts x 50 rules (spec section 14, 'Rule evaluation')", "setup_s": round(setup_s, 2),
           "env": {"db": get_engine().dialect.name, "os": platform.platform(), "python": sys.version.split()[0], "cpus": os.cpu_count(),
                   "commit": os.popen("git rev-parse --short HEAD 2>nul || git rev-parse --short HEAD").read().strip() or "uncommitted"},
           "as_of": AS_OF.isoformat(), "warmups": a.warmups, "reps": a.reps}
    for name, fn in (("per_call", sweep_per_call), ("batch", sweep_batch)):
        cold, napp = fn(tenant, ctxs)   # first sweep of this path in this process = cold
        for _ in range(a.warmups):
            fn(tenant, ctxs)
        xs = [fn(tenant, ctxs)[0] for _ in range(a.reps)]
        s = summarise(xs)
        s["cold_first_sweep_s"] = round(cold, 4)
        s["applicable_rule_hits_per_sweep"] = napp
        s["contexts_per_second_p50"] = round(N_CONTEXTS / s["p50_s"], 1)
        s["meets_target_p95_lt_3s"] = s["p95_s"] < 3.0
        out[name] = s
        print(f"{name}: p50 {s['p50_s']}s p95 {s['p95_s']}s (cold {s['cold_first_sweep_s']}s, n={s['n']}) target<3s: {s['meets_target_p95_lt_3s']}")
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(out, indent=1))
    print("written", a.out)


if __name__ == "__main__":
    main()
