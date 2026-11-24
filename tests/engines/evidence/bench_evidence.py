"""Evidence benchmark. Run:  cd backend && python ../tests/engines/evidence/bench_evidence.py
Writes reports/benchmarks/evidence.json with measured p50/p95/p99, sample counts, failures and the environment.
Uses a fresh temporary SQLite DB/object store/key dir (never the live ones). Targets (spec 14) are reported against,
never assumed met."""
from __future__ import annotations

import json
import os
import platform
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
tmp = Path(tempfile.mkdtemp(prefix="ev-bench-"))
os.environ["AETHER_DATABASE_URL"] = f"sqlite:///{(tmp / 'b.db').as_posix()}"
os.environ["AETHER_OBJECT_STORE_DIR"] = str(tmp / "objects")
os.environ["AETHER_SIGNING_KEY_DIR"] = str(tmp / "keys")
sys.path.insert(0, str(ROOT / "backend"))

from app.db.base import SessionLocal, create_all  # noqa: E402
from app.engines.evidence import checkpoints as cps  # noqa: E402
from app.engines.evidence import objects  # noqa: E402
from app.engines.evidence import service as ev  # noqa: E402
from app.engines.evidence.bundle import build_bundle  # noqa: E402


def pct(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(q * (len(xs) - 1))))]


def summarize(ms):
    return {"n": len(ms), "p50_ms": round(pct(ms, .5), 3), "p95_ms": round(pct(ms, .95), 3), "p99_ms": round(pct(ms, .99), 3),
            "mean_ms": round(statistics.mean(ms), 3), "max_ms": round(max(ms), 3)}


def one_append(tenant, i):
    t0 = time.perf_counter()
    with SessionLocal() as s:
        ev.append(s, tenant, engine="platform", action="BENCH", actor_id="u", actor_type="USER", entity_type="x",
                  entity_id=str(i), correlation_id="bench", payload={"i": i, "amount": "123.45"})
        s.commit()
    return (time.perf_counter() - t0) * 1000


def main():
    create_all()
    res = {"environment": {"os": platform.platform(), "python": sys.version.split()[0], "cpu_count": os.cpu_count(),
                           "db": "SQLite (WAL, BEGIN IMMEDIATE) - NOT PostgreSQL", "network": "none"}}
    try:
        res["environment"]["commit"] = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, cwd=ROOT).stdout.strip()
    except OSError:
        res["environment"]["commit"] = "unknown"
    failures = 0
    for i in range(5):  # warmups
        one_append("bench-warm", i)
    lat = []
    for i in range(200):
        try:
            lat.append(one_append("bench-single", i))
        except Exception:  # noqa: BLE001
            failures += 1
    res["append_single_writer"] = {**summarize(lat), "failures": failures, "target": "p95 < 100 ms", "met": pct(lat, .95) < 100,
                                   "note": "one append per committed transaction, cold chain growing 0..200, warm process"}
    # contention: 4 concurrent writers, same tenant (serialised by the chain-head lock)
    clat, errs = [], []

    def worker(k):
        for i in range(40):
            try:
                clat.append(one_append("bench-contended", k * 100 + i))
            except Exception as e:  # noqa: BLE001
                errs.append(repr(e))
    th = [threading.Thread(target=worker, args=(k,)) for k in range(4)]
    t0 = time.perf_counter()
    [t.start() for t in th]
    [t.join() for t in th]
    wall = time.perf_counter() - t0
    res["append_contention_4_writers"] = {**summarize(clat), "failures": len(errs), "throughput_per_s": round(len(clat) / wall, 1),
                                          "chain_valid": None}
    with SessionLocal() as s:
        res["append_contention_4_writers"]["chain_valid"] = ev.verify_chain(s, "bench-contended")["valid"]
    # 10,000-event chain verification
    with SessionLocal() as s:
        for i in range(10_000):
            ev.append(s, "bench-10k", engine="platform", action="BENCH", actor_id="u", actor_type="USER", entity_type="x",
                      entity_id=str(i), correlation_id="b10k", payload={"i": i})
            if i % 1000 == 999:
                s.commit()
        s.commit()
        objs = [objects.store_object(s, "bench-10k", f"obj-{i}".encode() * 50, "SNAPSHOT") for i in range(100)]
        s.commit()
        times = []
        for rep in range(5):
            t0 = time.perf_counter()
            r = ev.verify_chain(s, "bench-10k")
            times.append((time.perf_counter() - t0) * 1000)
            assert r["valid"] and r["events"] == 10_000
        t0 = time.perf_counter()
        ov = objects.verify_objects(s, "bench-10k")
        obj_ms = (time.perf_counter() - t0) * 1000
        cps.create_checkpoint(s, "bench-10k")
        t0 = time.perf_counter()
        va = cps.verify_against_exported(s, "bench-10k")
        anch_ms = (time.perf_counter() - t0) * 1000
        s.commit()
        res["verify_chain_10000_events"] = {"cold_ms": round(times[0], 1), "warm_p50_ms": round(statistics.median(times[1:]), 1),
                                            "runs": len(times), "target": "< 5000 ms", "met": max(times) < 5000,
                                            "objects_100_hash_check_ms": round(obj_ms, 1), "objects_valid": ov["valid"],
                                            "anchored_verify_ms": round(anch_ms, 1), "anchored_valid": va["valid"],
                                            "note": "chain hashes recomputed for every event; object hashes reported separately"}
        # export latency for a 200-event run (with 10 vault objects referenced)
        for i in range(200):
            ev.append(s, "bench-10k", engine="risk", action="STEP", actor_id="u", actor_type="SERVICE", entity_type="x",
                      entity_id=f"r{i}", correlation_id="runX", input_hashes=[objs[i % 10]])
        s.commit()
        ex = []
        for _ in range(5):
            t0 = time.perf_counter()
            build_bundle(s, "bench-10k", "runX", "runX", "bench")
            s.commit()
            ex.append((time.perf_counter() - t0) * 1000)
        res["export_bundle_200_events_10k_chain"] = {"n": 5, "cold_ms": round(ex[0], 1), "warm_p50_ms": round(statistics.median(ex[1:]), 1),
                                                     "note": "includes checkpoint signing, 10,200-row skeleton, zip, vault write, evidence append"}
    out = ROOT / "reports" / "benchmarks" / "evidence.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(res, indent=2))
    print(json.dumps(res, indent=2))


if __name__ == "__main__":
    main()
