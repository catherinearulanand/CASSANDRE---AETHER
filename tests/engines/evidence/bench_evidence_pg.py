"""Evidence benchmark on PostgreSQL. Run (PowerShell/bash):
    AETHER_DATABASE_URL=postgresql+psycopg://aether:<pw>@127.0.0.1:5433/<dedicated db> AETHER_AUTO_CREATE_SCHEMA=true \
        python tests/engines/evidence/bench_evidence_pg.py
Writes reports/benchmarks/evidence_pg.json. Never discards a measurement; targets are reported against, never assumed.
The 10,000-event chain is built with the REAL append() path; the 100,000-event chain is built with a bulk loader that uses
the same compute_event_hash (so verification is real), because 100k single-transaction appends would dominate the run.
The machine was SHARED with other concurrently running test jobs: treat numbers as indicative, see `noise_note`."""
from __future__ import annotations

import ctypes
import json
import os
import platform
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import tracemalloc
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
URL = os.environ.get("AETHER_DATABASE_URL", "")
if not URL.startswith("postgresql"):
    sys.exit("set AETHER_DATABASE_URL to a PostgreSQL URL (dedicated benchmark database)")
tmp = Path(tempfile.mkdtemp(prefix="ev-bench-pg-"))
os.environ["AETHER_OBJECT_STORE_DIR"] = str(tmp / "objects")
os.environ["AETHER_SIGNING_KEY_DIR"] = str(tmp / "keys")
sys.path.insert(0, str(ROOT / "backend"))

from sqlalchemy import insert, text  # noqa: E402

from app.db.base import SessionLocal, create_all  # noqa: E402
from app.engines.evidence import checkpoints as cps  # noqa: E402
from app.engines.evidence import service as ev  # noqa: E402
from app.engines.evidence.bundle import build_bundle  # noqa: E402
from app.engines.evidence.models import EvChainHead, EvEvent  # noqa: E402

TARGETS = {"append_p95_ms": 100, "verify_10k_s": 5}


def pct(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(q * (len(xs) - 1))))]


def summarize(ms):
    return {"n": len(ms), "p50_ms": round(pct(ms, .5), 3), "p95_ms": round(pct(ms, .95), 3), "p99_ms": round(pct(ms, .99), 3),
            "mean_ms": round(statistics.mean(ms), 3), "max_ms": round(max(ms), 3)}


def peak_rss_mb():
    """Peak working set of this process (Windows via ctypes, else resource)."""
    try:
        if os.name == "nt":
            class PMC(ctypes.Structure):
                _fields_ = [("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong), ("PeakWorkingSetSize", ctypes.c_size_t),
                            ("WorkingSetSize", ctypes.c_size_t), ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaPagedPoolUsage", ctypes.c_size_t), ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaNonPagedPoolUsage", ctypes.c_size_t), ("PagefileUsage", ctypes.c_size_t),
                            ("PeakPagefileUsage", ctypes.c_size_t)]
            pmc = PMC()
            pmc.cb = ctypes.sizeof(PMC)
            ctypes.windll.psapi.GetProcessMemoryInfo(ctypes.windll.kernel32.GetCurrentProcess(), ctypes.byref(pmc), pmc.cb)
            return round(pmc.PeakWorkingSetSize / 1e6, 1)
        import resource
        return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)
    except Exception:  # noqa: BLE001
        return None


def one_append(tenant, i):
    t0 = time.perf_counter()
    with SessionLocal() as s:
        ev.append(s, tenant, engine="platform", action="BENCH", actor_id="u", actor_type="USER", entity_type="x",
                  entity_id=str(i), correlation_id="bench", payload={"i": i, "amount": "123.45"})
        s.commit()
    return (time.perf_counter() - t0) * 1000


def bulk_build(tenant, n, corr_for=None, chunk=5000):
    """Chain of n events with the real hash function, inserted with executemany. corr_for: {sequence: correlation_id}."""
    base = datetime(2026, 1, 1, tzinfo=UTC)
    prev, rows = ev.GENESIS, []
    with SessionLocal() as s:
        for seq in range(1, n + 1):
            ts = base + timedelta(seconds=seq)
            f = dict(event_id=f"b-{tenant}-{seq}", engine="platform", action="BULK", actor_id="u", actor_type="SERVICE", actor_role=None,
                     service_version="bench", correlation_id=(corr_for or {}).get(seq, "bulk"), causation_id=None, entity_type="x",
                     entity_id=str(seq), entity_revision=1, effective_at=ts, recorded_at=ts, source_mode="REPLAY", input_hashes=[],
                     source_spans=[], output_hash=None, rationale=None, payload={"i": seq})
            h = ev.compute_event_hash(tenant, seq, prev, f)
            rows.append({"tenant_id": tenant, "sequence": seq, "prev_hash": prev, "event_hash": h, **f})
            prev = h
            if len(rows) == chunk:
                s.execute(insert(EvEvent), rows)
                rows = []
        if rows:
            s.execute(insert(EvEvent), rows)
        s.add(EvChainHead(tenant_id=tenant, sequence=n, head_hash=prev))
        s.commit()


def _verify_once(tenant, mode):
    with SessionLocal() as s:
        if mode == "chain_full":
            return ev.verify_chain(s, tenant)
        return cps.verify_against_exported(s, tenant, mode="incremental" if mode == "anchored_incremental" else "full")


def timed_verify(tenant, mode, reps):
    """Wall-clock reps WITHOUT any tracing (tracemalloc inflates time several-fold), then ONE extra traced rep for memory only."""
    out = []
    for _ in range(reps):
        t0 = time.perf_counter()
        r = _verify_once(tenant, mode)
        out.append(time.perf_counter() - t0)
        assert r["valid"], r["problems"][:3]
    tracemalloc.start()
    _verify_once(tenant, mode)
    peak_py = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    return {"reps": reps, "seconds": [round(x, 3) for x in out], "p50_s": round(statistics.median(out), 3),
            "max_s": round(max(out), 3), "events_recomputed": r.get("events_recomputed"),
            "python_heap_peak_mb_separate_traced_rep": round(peak_py / 1e6, 1)}


WORKER = """
import sys, time, json
sys.path.insert(0, {backend!r})
from app.db.base import SessionLocal
from app.engines.evidence import service as ev
tenant, n, go, tag = sys.argv[1], int(sys.argv[2]), float(sys.argv[3]), sys.argv[4]
time.sleep(max(0.0, go - time.time()))
lat = []
with SessionLocal() as s:
    for i in range(n):
        t0 = time.perf_counter()
        ev.append(s, tenant, engine="platform", action="BENCHP", actor_id="p", actor_type="SERVICE", entity_type="x",
                  entity_id=tag + str(i), correlation_id="procs", payload={{"i": i}})
        s.commit()
        lat.append((time.perf_counter() - t0) * 1000)
print(json.dumps(lat))
"""


def main():
    create_all()
    with SessionLocal() as s:
        pgv = s.execute(text("select version()")).scalar_one()
    res = {"environment": {"os": platform.platform(), "python": sys.version.split()[0], "cpu_count": os.cpu_count(),
                           "db": pgv.split(",")[0], "db_location": "Docker container on the same host, loopback", "network": "loopback only",
                           "commit": "not recorded (shared working tree, uncommitted at measurement time)",
                           "noise_note": "Shared developer machine; other workers were running test suites concurrently. Numbers are indicative, not a quiet-machine baseline.",
                           "measured_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")},
           "targets": TARGETS}
    # ---- append, single writer (fresh process, warm after 20 warmups)
    for i in range(20):
        one_append("bpg-warm", i)
    lat = [one_append("bpg-single", i) for i in range(300)]
    res["append_single_writer"] = {**summarize(lat), "warmups": 20, "met_target_p95_lt_100ms": pct(lat, .95) < 100,
                                   "note": "one evidence append per committed transaction, real network round trips to PostgreSQL"}
    # ---- append, contended: threads
    for nthreads in (4, 8):
        clat, errs = [], []

        def worker(k, n=nthreads):
            try:
                for i in range(40):
                    clat.append(one_append(f"bpg-thr{n}", k * 1000 + i))
            except Exception as e:  # noqa: BLE001
                errs.append(repr(e))
        th = [threading.Thread(target=worker, args=(k,)) for k in range(nthreads)]
        t0 = time.perf_counter()
        [t.start() for t in th]
        [t.join() for t in th]
        wall = time.perf_counter() - t0
        with SessionLocal() as s:
            valid = ev.verify_chain(s, f"bpg-thr{nthreads}")["valid"]
        res[f"append_contended_{nthreads}_threads"] = {**summarize(clat), "failures": len(errs), "throughput_per_s": round(len(clat) / wall, 1),
                                                       "chain_valid": valid, "note": "same tenant: serialised by the chain-head row lock (by design)"}
    # ---- append, contended: separate processes
    go = time.time() + 6
    code = WORKER.format(backend=str(ROOT / "backend"))
    procs = [subprocess.Popen([sys.executable, "-c", code, "bpg-procs", "40", str(go), f"p{k}"], stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True) for k in range(4)]
    plat, perr = [], []
    t0 = None
    for p in procs:
        out, err = p.communicate(timeout=300)
        if p.returncode == 0:
            plat += json.loads(out.strip().splitlines()[-1])
        else:
            perr.append(err[-300:])
    with SessionLocal() as s:
        valid = ev.verify_chain(s, "bpg-procs")["valid"]
    res["append_contended_4_processes"] = {**summarize(plat), "failures": len(perr), "errors": perr, "chain_valid": valid,
                                           "note": "4 OS processes x 40 appends each, same tenant, per-append latency measured inside the worker"}
    # ---- 10,000 events built with real append(), verification
    with SessionLocal() as s:
        for i in range(10_000):
            ev.append(s, "bpg-10k", engine="platform", action="BENCH", actor_id="u", actor_type="USER", entity_type="x",
                      entity_id=str(i), correlation_id="b10k", payload={"i": i})
            if i % 1000 == 999:
                s.commit()
        s.commit()
    v10 = timed_verify("bpg-10k", "chain_full", 5)
    v10["target_s"] = TARGETS["verify_10k_s"]
    v10["met_target"] = v10["max_s"] < TARGETS["verify_10k_s"]
    res["verify_10k_chain_full"] = v10
    with SessionLocal() as s:
        cps.create_checkpoint(s, "bpg-10k", mode="full")
        s.commit()
        for i in range(100):
            ev.append(s, "bpg-10k", engine="platform", action="BENCH2", actor_id="u", actor_type="USER", entity_type="x", entity_id=f"n{i}", correlation_id="b10k")
        s.commit()
    res["verify_10k_anchored_full"] = timed_verify("bpg-10k", "anchored_full", 3)
    res["verify_10k_anchored_incremental_100_new_events"] = timed_verify("bpg-10k", "anchored_incremental", 5)
    # ---- 100,000 events (bulk-built with the real hash function)
    t0 = time.perf_counter()
    bulk_build("bpg-100k", 100_000, corr_for={50_001 + i: "bundle-corr" for i in range(200)})
    res["build_100k_bulk_seconds"] = round(time.perf_counter() - t0, 1)
    res["verify_100k_chain_full"] = timed_verify("bpg-100k", "chain_full", 3)
    with SessionLocal() as s:
        t0 = time.perf_counter()
        c = cps.create_checkpoint(s, "bpg-100k", mode="full")
        s.commit()
        res["checkpoint_100k_first_full_seconds"] = round(time.perf_counter() - t0, 2)
        assert c["verification"]["events_recomputed"] == 100_000
        bulk_more = [ev.append(s, "bpg-100k", engine="platform", action="NEW", actor_id="u", actor_type="USER", entity_type="x",
                               entity_id=f"new{i}", correlation_id="after") for i in range(100)]
        s.commit()
    res["verify_100k_anchored_full"] = timed_verify("bpg-100k", "anchored_full", 3)
    res["verify_100k_anchored_incremental_100_new_events"] = timed_verify("bpg-100k", "anchored_incremental", 5)
    # ---- export of 200 events out of a 100,000+ chain (checkpoint is incremental)
    exp = []
    for k in range(3):
        with SessionLocal() as s:
            t0 = time.perf_counter()
            out = build_bundle(s, "bpg-100k", "bench-run", "bundle-corr", "bench-actor", redact=False)
            s.commit()
            exp.append(time.perf_counter() - t0)
    res["export_200_events_from_100k_chain"] = {"reps": 3, "seconds": [round(x, 3) for x in exp], "p50_s": round(statistics.median(exp), 3),
                                                "bundle_events": out["events"],
                                                "note": "includes incremental checkpoint, signing, skeleton of ALL chain rows up to the checkpoint, zip, vault store"}
    res["process_peak_working_set_mb"] = peak_rss_mb()
    out_path = ROOT / "reports" / "benchmarks" / "evidence_pg.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(res, indent=2))
    print(json.dumps({k: res[k] for k in res if k not in ("environment",)}, indent=1)[:6000])


if __name__ == "__main__":
    main()
