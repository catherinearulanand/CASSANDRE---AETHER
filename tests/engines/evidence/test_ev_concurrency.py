"""Concurrency regression tests found/hardened on PostgreSQL (A6 round 2).

Each PostgreSQL-only test reproduces the bug DETERMINISTICALLY with the verbatim pre-fix code ("legacy_*") and shows the fixed
code survives the same interleaving: session A inserts and holds its transaction open, session B (thread) runs the same
check-then-insert, then A commits. On PostgreSQL the legacy B raises a unique violation; SQLite's BEGIN IMMEDIATE serialises the
transactions, which is exactly why these bugs were invisible there.
"""
import hashlib
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.db.base import SessionLocal
from app.engines.evidence import checkpoints as cps
from app.engines.evidence import objects, ops
from app.engines.evidence import service as ev
from app.engines.evidence.lineage import record_lineage
from app.engines.evidence.models import EvEvent, EvLineage, EvObject, EvRetention
from tests.engines.evidence.conftest import add_events

IS_PG = os.environ.get("AETHER_TEST_DATABASE_URL", "").startswith("postgresql")
pg_only = pytest.mark.skipif(not IS_PG, reason="deterministic race reproduction needs PostgreSQL (set AETHER_TEST_DATABASE_URL)")
ROOT = Path(__file__).resolve().parents[3]

WORKER = """
import sys, time
sys.path.insert(0, {backend!r})
from app.db.base import SessionLocal
from app.engines.evidence import service as ev
tenant, n, go = sys.argv[1], int(sys.argv[2]), float(sys.argv[3])
time.sleep(max(0.0, go - time.time()))          # all workers start together
with SessionLocal() as s:
    for i in range(n):
        ev.append(s, tenant, engine="platform", action="PROC", actor_id="p", actor_type="SERVICE", entity_type="x",
                  entity_id=f"{{sys.argv[4]}}-{{i}}", correlation_id="proc")
        s.commit()
"""


def _spawn(tenant, n, go, tag):
    env = {**os.environ}
    code = WORKER.format(backend=str(ROOT / "backend"))
    return subprocess.Popen([sys.executable, "-c", code, tenant, str(n), str(go), tag], env=env, cwd=ROOT,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


# ------------------------------------------------------------------ append: separate processes + threads
def test_8_writers_processes_and_threads_yield_contiguous_valid_anchored_chain(tenant):
    go = time.time() + 6.0  # interpreter + import start-up headroom
    procs = [_spawn(tenant, 25, go, f"p{k}") for k in range(4)]
    errs: list = []

    def tw(k):
        try:
            with SessionLocal() as s:
                for i in range(25):
                    ev.append(s, tenant, engine="platform", action="THR", actor_id="t", actor_type="SERVICE", entity_type="x",
                              entity_id=f"t{k}-{i}", correlation_id="thr")
                    s.commit()
        except Exception as e:  # noqa: BLE001
            errs.append(repr(e))

    th = [threading.Thread(target=tw, args=(k,)) for k in range(4)]
    time.sleep(max(0.0, go - time.time() - 0.5))
    [t.start() for t in th]
    outs = [p.communicate(timeout=240) for p in procs]
    [t.join() for t in th]
    assert not errs, errs
    assert all(p.returncode == 0 for p in procs), [o[1][-400:] for o in outs]
    with SessionLocal() as s:
        seqs = [r[0] for r in s.execute(select(EvEvent.sequence).where(EvEvent.tenant_id == tenant).order_by(EvEvent.sequence))]
        assert seqs == list(range(1, 201))                       # 8 writers x 25, no gaps, no duplicates
        assert ev.verify_chain(s, tenant)["valid"]
        cps.create_checkpoint(s, tenant, mode="full")
        s.commit()
        assert cps.verify_against_exported(s, tenant)["valid"]


def test_first_append_for_a_brand_new_tenant_from_8_processes_at_once(tenant):
    """The create-the-chain-head race (unique violation on PostgreSQL before the ON CONFLICT fix)."""
    go = time.time() + 6.0
    procs = [_spawn(tenant, 1, go, f"f{k}") for k in range(8)]
    outs = [p.communicate(timeout=240) for p in procs]
    assert all(p.returncode == 0 for p in procs), [o[1][-500:] for o in outs]
    with SessionLocal() as s:
        r = ev.verify_chain(s, tenant)
        assert r["valid"] and r["events"] == 8


# ------------------------------------------------------------------ verification / checkpoint under live appends
def test_verification_and_checkpoints_never_false_fail_while_appends_are_landing(tenant):
    with SessionLocal() as s:
        add_events(s, tenant, 5)
        cps.create_checkpoint(s, tenant, mode="full")
        s.commit()
    stop = threading.Event()
    errs: list = []

    def writer():
        try:
            with SessionLocal() as s:
                i = 0
                while not stop.is_set():
                    ev.append(s, tenant, engine="platform", action="LIVE", actor_id="w", actor_type="SERVICE", entity_type="x",
                              entity_id=f"w{i}", correlation_id="live")
                    s.commit()
                    i += 1
        except Exception as e:  # noqa: BLE001
            errs.append(repr(e))

    ws = [threading.Thread(target=writer) for _ in range(3)]
    [w.start() for w in ws]
    bad, checks = [], 0
    try:
        t_end = time.time() + 6
        while time.time() < t_end:
            with SessionLocal() as s:
                for fn in (lambda: ev.verify_chain(s, tenant),
                           lambda: cps.verify_against_exported(s, tenant, mode="incremental"),
                           lambda: cps.verify_against_exported(s, tenant, mode="full")):
                    r = fn()
                    checks += 1
                    if not r["valid"]:
                        bad.append(r["problems"][:2])
                cp = cps.create_checkpoint(s, tenant)           # incremental; must sign exactly what it verified
                s.commit()
                row = s.execute(select(EvEvent.event_hash).where(EvEvent.tenant_id == tenant, EvEvent.sequence == cp["sequence"])).scalar_one()
                assert row == cp["head_hash"]                    # signed head == the real event hash at that sequence
    finally:
        stop.set()
        [w.join() for w in ws]
    assert not errs, errs
    assert checks > 6 and not bad, (checks, bad[:3])
    with SessionLocal() as s:
        assert cps.verify_against_exported(s, tenant)["valid"]


# ------------------------------------------------------------------ object vault / retention / lineage
def test_same_bytes_stored_concurrently_registers_once_and_blob_exists(tenant):
    data = b"identical bytes stored by many writers" * 20
    h = hashlib.sha256(data).hexdigest()
    errs: list = []

    def put(_):
        try:
            with SessionLocal() as s:
                objects.store_object(s, tenant, data, "SNAPSHOT")
                s.commit()
        except Exception as e:  # noqa: BLE001
            errs.append(repr(e))

    th = [threading.Thread(target=put, args=(i,)) for i in range(8)]
    [t.start() for t in th]
    [t.join() for t in th]
    assert not errs, errs
    with SessionLocal() as s:
        assert len(s.execute(select(EvObject).where(EvObject.tenant_id == tenant, EvObject.content_hash == h)).scalars().all()) == 1
        assert objects.read_object(s, tenant, h) == data


def test_temp_object_restored_as_evidence_is_upgraded_and_never_purged(tenant):
    data = b"first stored as scratch, later needed as a source document"
    with SessionLocal() as s:
        h = objects.store_object(s, tenant, data, "TEMP")
        s.commit()
    with SessionLocal() as s:
        assert objects.store_object(s, tenant, data, "SOURCE_DOCUMENT") == h
        s.commit()
    with SessionLocal() as s:
        assert s.get(EvObject, (tenant, h)).classification == "SOURCE_DOCUMENT"
        assert objects.purge_temp_objects(s, tenant, older_than_days=0)["purged"] == 0
        s.commit()
        assert objects.read_object(s, tenant, h) == data


def test_purge_honours_a_legal_hold_recorded_in_the_database_and_records_evidence(tenant):
    with SessionLocal() as s:
        h = objects.store_object(s, tenant, b"temp scratch", "TEMP")
        ops.set_retention(s, tenant, "TEMP_OBJECTS", 30, True, "admin-1")
        s.commit()
    with SessionLocal() as s:
        assert objects.purge_temp_objects(s, tenant, older_than_days=0) == {"purged": 0, "reason": "LEGAL_HOLD"}
        assert objects.read_object(s, tenant, h) == b"temp scratch"
        ops.set_retention(s, tenant, "TEMP_OBJECTS", 30, False, "admin-1")
        s.commit()
        assert objects.purge_temp_objects(s, tenant, older_than_days=0)["purged"] == 1
        s.commit()
        rows = s.execute(select(EvEvent).where(EvEvent.tenant_id == tenant, EvEvent.action == "RETENTION_CONFIG_CHANGED")
                         .order_by(EvEvent.sequence)).scalars().all()
        assert [r.rationale for r in rows] == ["LEGAL HOLD SET", "LEGAL HOLD CLEARED"]
        assert rows[1].payload["previous"]["legal_hold"] is True and rows[1].payload["new"]["legal_hold"] is False
        assert ev.verify_chain(s, tenant)["valid"]


def test_unconfigured_category_stays_unconfigured_after_a_purge_placeholder(tenant):
    with SessionLocal() as s:
        objects.purge_temp_objects(s, tenant, older_than_days=0)   # creates the lock placeholder row
        s.commit()
        items = {i["category"]: i for i in ops.get_retention(s, tenant)["items"]}
        assert items["TEMP_OBJECTS"]["configured"] is False and items["TEMP_OBJECTS"]["retention_days"] is None


def test_concurrent_first_retention_writes_do_not_collide_and_are_all_audited(tenant):
    errs: list = []

    def put(i):
        try:
            with SessionLocal() as s:
                ops.set_retention(s, tenant, "SOURCE_DOCUMENTS", 100 + i, bool(i % 2), f"admin-{i}")
                s.commit()
        except Exception as e:  # noqa: BLE001
            errs.append(repr(e))

    th = [threading.Thread(target=put, args=(i,)) for i in range(6)]
    [t.start() for t in th]
    [t.join() for t in th]
    assert not errs, errs
    with SessionLocal() as s:
        assert len(s.execute(select(EvRetention).where(EvRetention.tenant_id == tenant, EvRetention.category == "SOURCE_DOCUMENTS")).scalars().all()) == 1
        n = s.execute(select(EvEvent).where(EvEvent.tenant_id == tenant, EvEvent.action == "RETENTION_CONFIG_CHANGED")).scalars().all()
        assert len(n) == 6                                          # last-writer-wins, but every change is in the chain
        assert ev.verify_chain(s, tenant)["valid"]


def test_concurrent_duplicate_lineage_edges_record_exactly_once(tenant):
    res: list = []
    errs: list = []

    def edge(_):
        try:
            with SessionLocal() as s:
                res.append(record_lineage(s, tenant, "c1", "reg_rule", "R1", "risk_result", "M1"))
                s.commit()
        except Exception as e:  # noqa: BLE001
            errs.append(repr(e))

    th = [threading.Thread(target=edge, args=(i,)) for i in range(8)]
    [t.start() for t in th]
    [t.join() for t in th]
    assert not errs, errs
    assert res.count(True) == 1 and res.count(False) == 7
    with SessionLocal() as s:
        assert len(s.execute(select(EvLineage).where(EvLineage.tenant_id == tenant)).scalars().all()) == 1


# ------------------------------------------------------------------ deterministic legacy reproduction (PostgreSQL)
def legacy_store_object(session, tenant_id, data, classification):
    """VERBATIM pre-fix logic: check-then-insert on the object registry."""
    h = hashlib.sha256(data).hexdigest()
    p = objects.object_path(tenant_id, h)
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    if session.get(EvObject, (tenant_id, h)) is None:
        session.add(EvObject(tenant_id=tenant_id, content_hash=h, classification=classification, size_bytes=len(data)))
        session.flush()
    return h


def legacy_record_lineage(session, tenant_id, correlation_id, parent_type, parent_id, child_type, child_id):
    """VERBATIM pre-fix logic: check-then-insert on the lineage edge."""
    exists = session.execute(select(EvLineage.id).where(
        EvLineage.tenant_id == tenant_id, EvLineage.parent_type == parent_type, EvLineage.parent_id == parent_id,
        EvLineage.child_type == child_type, EvLineage.child_id == child_id)).first()
    if exists:
        return False
    session.add(EvLineage(tenant_id=tenant_id, correlation_id=correlation_id, parent_type=parent_type, parent_id=parent_id,
                          child_type=child_type, child_id=child_id))
    session.flush()
    return True


def legacy_set_retention(session, tenant_id, category, retention_days, legal_hold, actor_id):
    """VERBATIM pre-fix logic: get-then-add on the retention row."""
    r = session.get(EvRetention, (tenant_id, category))
    if r is None:
        r = EvRetention(tenant_id=tenant_id, category=category)
        session.add(r)
    r.retention_days, r.legal_hold, r.updated_by = retention_days, legal_hold, actor_id
    session.flush()


def _interleave(first, second):
    """A runs `first` and keeps its transaction OPEN; B runs `second` in a thread; then A commits. Returns B's exception or None."""
    out: dict = {}
    a = SessionLocal()
    first(a)
    def run_b():
        with SessionLocal() as b:
            try:
                second(b)
                b.commit()
            except Exception as e:  # noqa: BLE001
                b.rollback()
                out["err"] = e
    t = threading.Thread(target=run_b)
    t.start()
    time.sleep(1.0)       # B is now blocked behind A's uncommitted insert (PostgreSQL) or serialised behind it (SQLite)
    a.commit()
    a.close()
    t.join(timeout=60)
    return out.get("err")


@pg_only
@pytest.mark.parametrize("name", ["object", "lineage", "retention"])
def test_legacy_check_then_insert_fails_on_postgres_and_fixed_code_survives(tenant, name):
    if name == "object":
        data = b"race-bytes-" + tenant.encode()
        legacy = lambda s: legacy_store_object(s, tenant, data, "SNAPSHOT")                      # noqa: E731
        fixed = lambda s: objects.store_object(s, tenant, data, "SNAPSHOT")                      # noqa: E731
        first_legacy, first_fixed = legacy, fixed
    elif name == "lineage":
        legacy = lambda s: legacy_record_lineage(s, tenant, "c", "reg_rule", "R", "risk_result", "M")  # noqa: E731
        fixed = lambda s: record_lineage(s, tenant, "c", "reg_rule", "R", "risk_result", "M")           # noqa: E731
        first_legacy, first_fixed = legacy, fixed
    else:
        legacy = lambda s: legacy_set_retention(s, tenant, "SOURCE_DOCUMENTS", 10, False, "u")   # noqa: E731
        fixed = lambda s: ops.set_retention(s, tenant, "SOURCE_DOCUMENTS", 10, False, "u")       # noqa: E731
        first_legacy, first_fixed = legacy, fixed
    err = _interleave(first_legacy, legacy)
    assert isinstance(err, IntegrityError), f"legacy {name} race did not reproduce on PostgreSQL: {err!r}"
    # fresh tenant for the fixed code so state from the legacy run cannot mask anything
    tenant2 = tenant + "-fx"
    if name == "object":
        data2 = b"race-bytes-" + tenant2.encode()
        first_fixed = lambda s: objects.store_object(s, tenant2, data2, "SNAPSHOT")              # noqa: E731
        fixed = first_fixed
    elif name == "lineage":
        first_fixed = fixed = lambda s: record_lineage(s, tenant2, "c", "reg_rule", "R", "risk_result", "M")  # noqa: E731
    else:
        first_fixed = fixed = lambda s: ops.set_retention(s, tenant2, "SOURCE_DOCUMENTS", 10, False, "u")      # noqa: E731
    assert _interleave(first_fixed, fixed) is None


@pg_only
def test_first_append_race_legacy_reproduces_and_fixed_survives():
    """Chain-head creation: A holds an uncommitted head INSERT; B's legacy get-then-add collides, the fixed append does not."""
    from app.engines.evidence.models import EvChainHead
    t1, t2 = "race-legacy-" + os.urandom(3).hex(), "race-fixed-" + os.urandom(3).hex()

    def legacy_append_head(s, t):
        if s.get(EvChainHead, t) is None:
            s.add(EvChainHead(tenant_id=t))
            s.flush()

    err = _interleave(lambda a: legacy_append_head(a, t1), lambda b: legacy_append_head(b, t1))
    assert isinstance(err, IntegrityError), f"legacy chain-head race did not reproduce: {err!r}"
    def fixed(s):
        ev.append(s, t2, engine="platform", action="X", actor_id="u", actor_type="USER", entity_type="x", entity_id="1", correlation_id="c")
    assert _interleave(fixed, fixed) is None
    with SessionLocal() as s:
        r = ev.verify_chain(s, t2)
        assert r["valid"] and r["events"] == 2
        s.execute(text("SELECT 1"))
