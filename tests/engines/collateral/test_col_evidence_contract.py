"""Round 2: collateral.constraint_slacks, lineage payloads, replay.collateral via VERITAS' evidence.replay_run (CONTRACTS v1.1)."""
import json
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import func, select, text

from app.core import services
from app.core.errors import NotFound
from app.engines.collateral import services as S
from app.engines.collateral.models import ColLot, ColProposal, ColReservation
from app.engines.evidence.lineage import build_lineage
from app.engines.evidence.models import EvEvent
from tests.engines.collateral.conftest import CHECKER, DEADLINE, T, lot, snapshot, uid
from tests.engines.collateral.test_col_services import approve, toy_policy

EXP = json.loads((Path(__file__).resolve().parents[3] / "fixtures" / "expected" / "collateral" / "golden.json").read_text())
D = Decimal


def optimise(db, up, lots, *, cid=None, required="100", policy=None, **kw):
    sid = uid("SNAP")
    snapshot(db, sid, lots, policy or toy_policy())
    mid = up.add_margin(required_amount={"amount": required, "currency": "INR"}, rounded_amount=required)
    cid = cid or uid("COR")
    out = S.optimise(db, T, mid, sid, None, None, DEADLINE, 30, actor_id="u-maker", actor_type="USER", actor_role="maker",
                     correlation_id=cid, **kw)
    db.commit()
    return out, sid, mid, cid


def replay(db, cid, actor="u-auditor"):
    out = services.call("evidence.replay_run", db, T, "RUN-" + cid, cid, actor)
    db.commit()
    return out


# ------------------------------------------------------------------ constraint_slacks
@pytest.mark.gold
def test_constraint_slacks_g05_binding_and_slack_values(db, up):
    """G05 hand values: required 100; A avail 60 fully used (binding, slack 0); B avail 100 uses 40 (slack 60); coverage met exactly."""
    out, *_ = optimise(db, up, [lot("A", "GSEC", "60", txn="1"), lot("B", "CASH", "100", txn="3")])
    rows = services.call("collateral.constraint_slacks", db, T, out["proposal_id"])
    assert isinstance(rows, list) and rows
    by = {r["name"]: r for r in rows}
    assert set(rows[0]) >= {"name", "limit", "used", "slack", "binding"}
    a = next(r for n, r in by.items() if "AVAILABILITY A" in n)
    b = next(r for n, r in by.items() if "AVAILABILITY B" in n)
    assert (D(a["limit"]), D(a["used"]), D(a["slack"]), a["binding"]) == (D("60"), D("60"), D("0"), True)
    assert (D(b["limit"]), D(b["used"]), D(b["slack"]), b["binding"]) == (D("100"), D("40"), D("60"), False)
    cov = next(r for r in rows if r["kind"] == "COVERAGE")
    assert D(cov["used"]) >= D(cov["limit"]) == D("100")
    # identical to what the proposal API exposes (single source of truth)
    api = {c["name"]: c for c in S.get_proposal(db, T, out["proposal_id"])["constraints"]}
    assert {n: (r["limit"], r["used"], r["slack"], r["binding"]) for n, r in by.items()} == \
           {n: (c["limit"], c["measured"], c["slack"], c["binding"]) for n, c in api.items()}


def test_constraint_slacks_tenant_isolation_and_infeasible_case(db, up):
    out, *_ = optimise(db, up, [lot("A", "GSEC", "60", txn="1")], required="100")   # G07-style: 60 < 100
    assert out["solver_status"] == "infeasible"
    assert services.call("collateral.constraint_slacks", db, T, out["proposal_id"]) == []   # no constraints invented for infeasible
    with pytest.raises(NotFound):
        services.call("collateral.constraint_slacks", db, "tenant-b", out["proposal_id"])   # 404 semantics, no leakage
    with pytest.raises(NotFound):
        services.call("collateral.constraint_slacks", db, T, "no-such-proposal")


# ------------------------------------------------------------------ lineage payloads
def test_events_carry_lineage_parents_in_veritas_format_and_approval_node(db, up):
    out, sid, mid, cid = optimise(db, up, [lot("A", "GSEC", "60", txn="1"), lot("B", "CASH", "100", txn="3")])
    ap = approve(db, out["proposal_id"])["approval"]["approval_id"]
    ev = db.execute(select(EvEvent).where(EvEvent.tenant_id == T, EvEvent.correlation_id == cid).order_by(EvEvent.sequence)).scalars().all()
    created = next(e for e in ev if e.action == "COLLATERAL_PROPOSAL_CREATED")
    assert created.entity_type == "allocation_proposal"
    parents = {(p["entity_type"], p["entity_id"]) for p in created.payload["parents"]}
    assert ("risk_result", mid) in parents and ("inventory_snapshot", sid) in parents
    assert all(set(p) == {"entity_type", "entity_id"} for p in created.payload["parents"])
    rec = next(e for e in ev if e.action == "COLLATERAL_APPROVAL_RECORDED")
    assert (rec.entity_type, rec.entity_id) == ("approval", ap)
    assert rec.payload["parents"] == [{"entity_type": "allocation_proposal", "entity_id": out["proposal_id"]}]
    g = build_lineage(db, T, cid)
    nodes = {n["node_id"]: n for n in g["nodes"]}
    prop, apn = f"allocation_proposal:{out['proposal_id']}", f"approval:{ap}"
    assert nodes[prop]["stage"] == "PROPOSAL" and nodes[prop]["recorded"]
    assert nodes[apn]["stage"] == "APPROVAL" and nodes[apn]["recorded"]
    assert nodes[f"inventory_snapshot:{sid}"]["stage"] == "SNAPSHOT"
    edges = {(e["from"], e["to"]) for e in g["edges"]}
    assert (f"risk_result:{mid}", prop) in edges and (f"inventory_snapshot:{sid}", prop) in edges and (prop, apn) in edges
    # missing links are REPORTED, not hidden (the margin result is recorded by VECTOR, not ATLAS; the fake upstream records nothing)
    assert any(m["kind"] == "PARENT_NOT_RECORDED" and m["node_id"] == f"risk_result:{mid}" for m in g["missing"])


# ------------------------------------------------------------------ replay.collateral
def test_replay_match_and_pinned_availability_after_reservation(db, up):
    out, sid, mid, cid = optimise(db, up, [lot("A", "GSEC", "60", txn="1"), lot("B", "CASH", "100", txn="3")])
    spec = next(e for e in db.execute(select(EvEvent).where(EvEvent.tenant_id == T, EvEvent.correlation_id == cid)).scalars()
                if e.action == "COLLATERAL_PROPOSAL_CREATED").payload["replay"]
    assert spec["replayer"] == "replay.collateral" and spec["business_hash"] == out["proposal_hash"]
    assert spec["method_version"].startswith("collateral-lp/1 highspy/") and spec["tolerance"] == S.LP_TOLERANCE
    r = replay(db, cid)
    assert r["status"] == "MATCH" and r["items"][0]["engine"] == "collateral", r
    # reserve (inventory availability changes) -> replay still reproduces the ORIGINAL computation from pinned availability
    aid = approve(db, out["proposal_id"])["approval"]["approval_id"]
    S.reserve(db, T, out["proposal_id"], aid, actor_id="u-maker")
    db.commit()
    assert replay(db, cid)["status"] == "MATCH"


def test_replay_is_read_only_no_reservations_no_new_collateral_rows(db, up):
    out, sid, mid, cid = optimise(db, up, [lot("A", "GSEC", "100", txn="1")], required="40")
    approve(db, out["proposal_id"])
    db.commit()

    def counts():
        db.expire_all()
        return (db.execute(select(func.count()).select_from(ColReservation)).scalar_one(),
                db.execute(select(func.count()).select_from(ColProposal)).scalar_one(),
                db.execute(select(func.coalesce(func.sum(ColLot.reserved_u), 0)).where(ColLot.snapshot_id == sid)).scalar_one(),
                db.execute(select(func.count()).select_from(EvEvent).where(EvEvent.tenant_id == T, EvEvent.engine == "collateral")).scalar_one())

    before = counts()
    r = replay(db, cid)
    assert r["status"] == "MATCH" and r["read_only"] is True
    assert counts() == before          # replay created no reservation, no proposal, no collateral evidence, moved no units


def test_replay_diverged_when_persisted_inventory_was_altered(db, up):
    out, sid, mid, cid = optimise(db, up, [lot("A", "GSEC", "60", txn="1"), lot("B", "CASH", "100", txn="3")])
    assert replay(db, cid)["status"] == "MATCH"
    db.execute(text("UPDATE col_inventory SET txn_cost_per_unit = '5' WHERE snapshot_id = :s AND lot_id = 'A'"), {"s": sid})
    db.commit()                        # silent drift of a persisted input (cost of A rises above B)
    r = replay(db, cid)
    assert r["status"] == "DIVERGED", r
    it = r["items"][0]
    assert it["status"] == "DIVERGED" and it["first_divergence"] in ("objective", "business_hash")


def test_replay_not_replayable_on_method_version_change_and_changed_terms(db, up, monkeypatch):
    out, sid, mid, cid = optimise(db, up, [lot("A", "GSEC", "60", txn="1"), lot("B", "CASH", "100", txn="3")])
    monkeypatch.setattr(S, "METHOD_VERSION", "collateral-lp/1 highspy/0.0.0-other")   # a different solver library is NOT silently used
    r = replay(db, cid)
    assert r["status"] == "NOT_REPLAYABLE" and r["items"][0]["reason"] == "VERSION_MISMATCH"
    assert r["items"][0]["recorded"]["method_version"].startswith("collateral-lp/1 highspy/")


def test_replay_not_replayable_when_upstream_margin_result_unavailable(db, up):
    out, sid, mid, cid = optimise(db, up, [lot("A", "GSEC", "60", txn="1"), lot("B", "CASH", "100", txn="3")])
    del up.margins[mid]                # upstream result gone => must say so, never guess
    r = replay(db, cid)
    assert r["status"] == "NOT_REPLAYABLE" and "REPLAYER_ERROR" in r["items"][0]["reason"]


def test_replay_zero_call_and_stress_records(db, up):
    out, sid, mid, cid = optimise(db, up, [lot("A", "GSEC", "60", txn="1")], required="0")
    assert out["state"] == "NOT_REQUIRED" and replay(db, cid)["status"] == "MATCH"
    sid2 = uid("SNAP")
    snapshot(db, sid2, [lot("A", "GSEC", "60", txn="1"), lot("B", "CASH", "100", txn="3")], toy_policy())
    mid2 = up.add_margin(required_amount={"amount": "100", "currency": "INR"}, rounded_amount="100")
    cid2 = uid("COR")
    st = S.stress(db, T, mid2, sid2, None, None, DEADLINE, 30, {"cost_add_bps": "50"}, actor_id="u", correlation_id=cid2)
    db.commit()
    assert st["hypothetical"] is True
    # a HYPOTHETICAL stress record is replayable too (stress inputs are persisted); replay never makes it official/approvable
    r = replay(db, cid2)
    assert r["status"] == "MATCH" and r["items"][0]["engine"] == "collateral", r
    assert S.get_proposal(db, T, st["stressed"]["proposal_id"])["hypothetical"] is True
