"""COVENANT benchmark (real database, real workflow). Usage (own DB only):
    AETHER_DATABASE_URL=postgresql+psycopg://...aether_t2 python tests/engines/contracts/bench_contracts.py [N_PACKAGES] [OUT.json]
Seeds N agreements through create -> extract -> maker/checker review -> propose -> freeze (so every row carries real evidence),
then times: resolve_terms (cold + warm), netting_membership, registry listing (filters), replay.contracts via evidence replay,
and the seeding workflow itself. NOT a capacity claim: single machine, other workers may be running (load recorded)."""
import json
import os
import statistics
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT))

from app.db.base import SessionLocal, create_all, get_engine, import_all_models  # noqa: E402

import_all_models()
create_all()
from app.auth.security import Principal  # noqa: E402
from app.core import services  # noqa: E402
from app.engines.contracts import fixtures as F  # noqa: E402
from app.engines.contracts import services as S  # noqa: E402

N = int(sys.argv[1]) if len(sys.argv) > 1 else 100
OUT = Path(sys.argv[2]) if len(sys.argv) > 2 else ROOT / "reports" / "benchmarks" / "contracts_pg.json"
AS_OF = datetime(2026, 8, 17, 4, tzinfo=UTC)


def load_snapshot():
    try:
        cpu = subprocess.run(["powershell", "-NoProfile", "-Command", "(Get-CimInstance Win32_Processor | Measure-Object LoadPercentage -Average).Average"],
                             capture_output=True, text=True, timeout=20).stdout.strip()
        procs = subprocess.run(["powershell", "-NoProfile", "-Command",
                                "(Get-Process python,node,msedge -ErrorAction SilentlyContinue | Measure-Object).Count"],
                               capture_output=True, text=True, timeout=20).stdout.strip()
        return {"cpu_load_percent": float(cpu) if cpu else None, "python_node_msedge_processes": int(procs or 0), "logical_cpus": os.cpu_count()}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)[:100]}


def pct(xs, q):
    xs = sorted(xs)
    return round(xs[min(len(xs) - 1, int(q * len(xs)))] * 1000, 2)


def summarize(xs):
    return {"n": len(xs), "p50_ms": pct(xs, .5), "p95_ms": pct(xs, .95), "max_ms": round(max(xs) * 1000, 2), "mean_ms": round(statistics.mean(xs) * 1000, 2)}


tenant = f"bench-{uuid.uuid4().hex[:8]}"
maker = Principal("b-maker", tenant, "b-maker", "b-maker", ("maker",), (), "")
checker = Principal("b-checker", tenant, "b-checker", "b-checker", ("checker",), (), "")
body0 = F.package_body(F.SPECS[2])  # NS-DEMO-G25 text (cash-only CSA)
load_before = load_snapshot()
seed_t, nsets = [], []
t_all = time.perf_counter()
with SessionLocal() as s:
    for i in range(N):
        ns = f"NS-BENCH-{i:05d}"
        b = dict(body0, master_id=f"MA-BENCH-{i:05d}", csa_id=f"CSA-BENCH-{i:05d}", netting_set_id=ns)
        txt = b["documents"][0]["text"].replace("NS-DEMO-G25", ns).replace("MA-DEMO-G25", b["master_id"]).replace("CSA-DEMO-G25", b["csa_id"])
        b["documents"] = [dict(b["documents"][0], text=txt, doc_ref=b["csa_id"], title=b["csa_id"])]
        t0 = time.perf_counter()
        corr = uuid.uuid4().hex
        pkg = S.create_package(s, maker, corr, b)
        S.extract(s, maker, corr, pkg["package_id"], "RULES")
        F._approve_all(s, checker, corr, pkg["package_id"])
        F._freeze(s, maker, checker, corr, pkg["package_id"])
        s.commit()
        seed_t.append(time.perf_counter() - t0)
        nsets.append(ns)
        if i % 25 == 24:
            print(f"seeded {i + 1}/{N}", flush=True)
seed_total = time.perf_counter() - t_all

res = {}
with SessionLocal() as s:
    cold = []
    for ns in nsets:
        t0 = time.perf_counter()
        r = S.resolve_terms(s, tenant, ns, AS_OF, None)
        cold.append(time.perf_counter() - t0)
        assert r["status"] == "RESOLVED"
    s.commit()
    res["resolve_terms_cold_first_resolution"] = summarize(cold)
    warm = []
    for _ in range(3):
        for ns in nsets:
            t0 = time.perf_counter()
            S.resolve_terms(s, tenant, ns, AS_OF, None)
            warm.append(time.perf_counter() - t0)
    res["resolve_terms_warm"] = summarize(warm)
    t = []
    for i in range(300):
        t0 = time.perf_counter()
        S.netting_membership(s, tenant, f"UNKNOWN-{i}")
        t.append(time.perf_counter() - t0)
    res["netting_membership_lookup"] = summarize(t)
    t = []
    for kw in ({}, {"status": "FROZEN"}, {"currency": "USD"}, {"needs_review": False}):
        for _ in range(10):
            t0 = time.perf_counter()
            S.list_packages(s, tenant, **kw)
            t.append(time.perf_counter() - t0)
    res["registry_list_all_filters"] = summarize(t)
    t = []
    for ns in nsets[:30]:
        pkg = s.execute(__import__("sqlalchemy").select(S.ConPackage).where(S.ConPackage.tenant_id == tenant, S.ConPackage.netting_set_id == ns)).scalar_one()
        t0 = time.perf_counter()
        S.terms_view(s, tenant, pkg.package_id)
        t.append(time.perf_counter() - t0)
    res["terms_view_one_package"] = summarize(t)
    # audited resolutions + evidence replay
    corr = uuid.uuid4().hex
    t0 = time.perf_counter()
    for ns in nsets[:20]:
        S.record_resolution(s, checker, corr, ns, AS_OF, None, None)
    s.commit()
    res["record_resolution_20_audited"] = {"total_ms": round((time.perf_counter() - t0) * 1000, 1)}
    t0 = time.perf_counter()
    rp = services.call("evidence.replay_run", s, tenant, "bench-" + corr, corr, "u-auditor")
    res["evidence_replay_20_resolutions"] = {"ms": round((time.perf_counter() - t0) * 1000, 1), "status": rp["status"], "items": len(rp["items"])}
    s.commit()

report = {"database": get_engine().dialect.name, "agreements": N, "seed_workflow": {"total_s": round(seed_total, 1), **summarize(seed_t),
          "per_agreement_note": "create+extract+all field reviews+propose+freeze, each with evidence events"},
          "results": res, "machine_load_before": load_before, "machine_load_after": load_snapshot(),
          "caveat": "Single machine; Docker PostgreSQL 16 on localhost; other engineering workers share the CPU. Indicative only."}
OUT.parent.mkdir(parents=True, exist_ok=True)
OUT.write_text(json.dumps(report, indent=1))
print(json.dumps({k: v for k, v in report.items() if k != "results"}, indent=1)[:900])
for k, v in res.items():
    print(k, v)
