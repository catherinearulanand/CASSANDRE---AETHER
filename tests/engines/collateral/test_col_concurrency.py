"""Concurrency / lost-update proofs for ATLAS (round 2).

Every contender is a real thread with its OWN SQLAlchemy session = its own DB connection (no shared session).
On PostgreSQL these race for real; on SQLite writers are serialised by BEGIN IMMEDIATE so the same assertions hold trivially.

"TEETH" tests (PostgreSQL only) switch a guard OFF and assert that the very invariant checked by the regression test is
then violated - i.e. each regression test provably FAILS without its lock / constraint.
"""
import threading
from datetime import datetime, timezone

import pytest
from sqlalchemy import func, select, update

from app.core.errors import ApiError
from app.db.base import SessionLocal, is_sqlite
from app.db.models_core import ApprovalRow
from app.engines.collateral import services as S
from app.engines.collateral.models import ColInstructionDraft, ColLot, ColProposal, ColReservation, ColSnapshot
from tests.engines.collateral.conftest import DEADLINE, T, lot, principal, snapshot, uid
from tests.engines.collateral.test_col_services import approve, new_checked, run, toy_policy

U = 10**6  # 6dp fixed-point units used by the engine


def race(n, fn, *, barrier=True):
    """Run fn(session, i) in n threads (own session each). Returns list of labels: 'ok:<payload>' or error code."""
    out: list = [None] * n
    bar = threading.Barrier(n) if barrier else None

    def worker(i):
        s = SessionLocal()
        try:
            if bar:
                bar.wait(timeout=10)
            r = fn(s, i)
            s.commit()
            out[i] = ("ok", r)
        except ApiError as e:
            s.rollback()
            out[i] = (e.code, None)
        except Exception as e:  # noqa: BLE001 - an unexpected exception IS a failure (e.g. IntegrityError => would be a 500)
            s.rollback()
            out[i] = (f"ERR {type(e).__name__}", str(e)[:200])
        finally:
            s.close()

    th = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    [t.start() for t in th]
    [t.join() for t in th]
    return out


def codes(res):
    return [c for c, _ in res]


def lot_row(db, sid):
    r = db.execute(select(ColLot).where(ColLot.snapshot_id == sid).order_by(ColLot.lot_id)).scalars().first()
    db.refresh(r)
    return r


def reserved_invariant(db, sid):
    """lot.reserved_u must equal the sum of ACTIVE reservations and never exceed units (no double pledge)."""
    db.expire_all()
    for r in db.execute(select(ColLot).where(ColLot.snapshot_id == sid)).scalars():
        active = db.execute(select(func.coalesce(func.sum(ColReservation.quantity_u), 0)).where(
            ColReservation.lot_pk == r.id, ColReservation.status == "ACTIVE")).scalar_one()
        assert r.reserved_u == int(active), (r.lot_id, r.reserved_u, active)
        assert r.reserved_u + r.encumbered_u <= r.units_u, (r.lot_id, r.reserved_u, r.units_u)


# ---------------------------------------------------------------------------- regression tests
def test_concurrent_approvals_of_same_proposal_exactly_one_wins(db, up):
    out, sid, _ = new_checked(db, up)
    pid, rev = out["proposal_id"], out["revision"]
    res = race(5, lambda s, i: S.approve_proposal(s, T, principal(f"chk-{i}", "checker"), pid, reason="independent review",
                                                  expected_revision=rev)["approval"]["approval_id"])
    assert codes(res).count("ok") == 1, codes(res)
    assert all(c in ("ok", "REVISION_CONFLICT", "ILLEGAL_TRANSITION") for c in codes(res)), codes(res)
    n = db.execute(select(func.count()).select_from(ApprovalRow).where(ApprovalRow.target_id == pid)).scalar_one()
    assert n == 1


def test_concurrent_reserve_same_approval_reserves_once(db, up):
    # 30 of 100 units: if two requests both reserved, the unique reservation constraint / double count would show.
    out, sid, _ = new_checked(db, up, units="100", required="30")
    aid = approve(db, out["proposal_id"])["approval"]["approval_id"]
    res = race(5, lambda s, i: S.reserve(s, T, out["proposal_id"], aid, actor_id=f"u{i}")["state"])
    assert not [c for c in codes(res) if c.startswith("ERR")], res          # no IntegrityError / 500
    assert all(c == "ok" for c in codes(res)), codes(res)                   # winners + idempotent retries
    assert lot_row(db, sid).reserved_u == 30 * U
    n = db.execute(select(func.count()).select_from(ColReservation).where(ColReservation.proposal_id == out["proposal_id"])).scalar_one()
    assert n == 1
    reserved_invariant(db, sid)


@pytest.mark.gold
def test_g13_six_contenders_exactly_three_fit(db, up):
    """6 separately-approved proposals each want 30 of 100 units: exactly 3 can win; reserved never exceeds units."""
    sid = uid("SNAP")
    snapshot(db, sid, [lot("L1", "GSEC", "100", txn="1")], toy_policy())
    mid = up.add_margin(required_amount={"amount": "30", "currency": "INR"}, rounded_amount="30")
    pids = [run(db, mid, sid)["proposal_id"] for _ in range(6)]
    aids = [approve(db, pid)["approval"]["approval_id"] for pid in pids]
    res = race(6, lambda s, i: S.reserve(s, T, pids[i], aids[i], actor_id="u")["state"])
    assert not [c for c in codes(res) if c.startswith("ERR")], res
    assert codes(res).count("ok") == 3, codes(res)
    assert set(codes(res)) - {"ok"} <= {"RESERVATION_CONFLICT", "STALE_INVENTORY"}, codes(res)
    assert lot_row(db, sid).reserved_u == 90 * U
    reserved_invariant(db, sid)


def test_concurrent_cancel_vs_reserve_stays_consistent(db, up, monkeypatch):
    """Both contenders READ the APPROVED proposal before either writes (rendezvous at the revision check). With the row lock the
    second one waits (the rendezvous times out harmlessly) and then sees the committed state; without it both 'succeed' and the
    outcomes contradict each other. Asserted: a successful cancel always ends CANCELLED with nothing reserved; a successful
    reserve whose cancel did not succeed ends RESERVED; reservation counters always match."""
    orig_check = S._check_rev
    for _ in range(3):
        out, sid, _ = new_checked(db, up, units="100", required="40")
        pid = out["proposal_id"]
        aid = approve(db, pid)["approval"]["approval_id"]
        bar = threading.Barrier(2)

        def check_with_rendezvous(p, expected, bar=bar):
            try:
                bar.wait(timeout=1.5)
            except threading.BrokenBarrierError:
                pass
            return orig_check(p, expected)

        monkeypatch.setattr(S, "_check_rev", check_with_rendezvous)

        def act(s, i, pid=pid, aid=aid):
            if i == 0:
                return S.reserve(s, T, pid, aid, actor_id="u")["state"]
            return S.cancel(s, T, pid, actor_id="u", reason="cancel race")["state"]

        res = race(2, act)
        monkeypatch.setattr(S, "_check_rev", orig_check)
        assert not [c for c in codes(res) if c.startswith("ERR")], res
        db.expire_all()
        st = db.get(ColProposal, pid).state
        reserved_invariant(db, sid)
        if codes(res)[1] == "ok":          # cancel reported success => the proposal MUST be cancelled and fully released
            assert st == "CANCELLED" and lot_row(db, sid).reserved_u == 0, (st, res)
        else:                              # cancel lost => reserve must be the (only) outcome
            assert st == "RESERVED" and codes(res)[0] == "ok", (st, res)


def test_concurrent_draft_instruction_creates_one_draft(db, up):
    out, sid, _ = new_checked(db, up, units="100", required="40")
    aid = approve(db, out["proposal_id"])["approval"]["approval_id"]
    S.reserve(db, T, out["proposal_id"], aid, actor_id="u")
    db.commit()
    res = race(4, lambda s, i: S.draft_instruction(s, T, out["proposal_id"], actor_id=f"u{i}")["content_hash"])
    assert not [c for c in codes(res) if c.startswith("ERR")], res          # PK collision would surface as IntegrityError
    assert len({v for c, v in res if c == "ok"}) == 1
    assert db.execute(select(func.count()).select_from(ColInstructionDraft).where(
        ColInstructionDraft.proposal_id == out["proposal_id"])).scalar_one() == 1


def test_concurrent_simulated_settlement_posts_once(db, up):
    acc = uid("ACC-SIM")
    sid = uid("SNAP")
    snapshot(db, sid, [lot("L1", "GSEC", "100", txn="1", entitled=(acc,))], toy_policy())
    mid = up.add_margin(account_id=acc, required_amount={"amount": "40", "currency": "INR"})
    out = run(db, mid, sid)
    aid = approve(db, out["proposal_id"])["approval"]["approval_id"]
    S.reserve(db, T, out["proposal_id"], aid, actor_id="u")
    S.draft_instruction(db, T, out["proposal_id"], actor_id="u")
    db.commit()
    res = race(4, lambda s, i: S.simulate_settlement(s, T, out["proposal_id"], actor_id=f"u{i}")["state"])
    assert not [c for c in codes(res) if c.startswith("ERR")], res
    assert codes(res).count("ok") == 1 and set(codes(res)) - {"ok"} <= {"ILLEGAL_TRANSITION"}, codes(res)
    r = lot_row(db, sid)
    assert r.posted_u == 40 * U and r.units_u == 60 * U and r.reserved_u == 0   # moved exactly once


def test_concurrent_identical_inventory_import_is_idempotent(db):
    sid = uid("SNAP")
    payload = {"snapshot_id": sid, "as_of": "2026-08-17T04:00:00Z", "currency": "INR", "market_type": "ETD_DOMESTIC",
               "policy": toy_policy(), "lots": [lot("L1", "GSEC", "100", txn="1")]}
    res = race(5, lambda s, i: S.import_inventory(s, T, actor_id="u", role="maker", payload=dict(payload),
                                                  correlation_id=f"c{i}")["replayed"])
    assert not [c for c in codes(res) if c.startswith("ERR")], res          # loser must not 500 on the PK
    assert codes(res).count("ok") == 5 and sorted(v for _, v in res).count(False) == 1, res
    assert db.execute(select(func.count()).select_from(ColSnapshot).where(ColSnapshot.snapshot_id == sid)).scalar_one() == 1
    assert db.execute(select(func.count()).select_from(ColLot).where(ColLot.snapshot_id == sid)).scalar_one() == 1


def test_concurrent_conflicting_inventory_import_one_winner(db):
    sid = uid("SNAP")

    def imp(s, i):
        return S.import_inventory(s, T, actor_id="u", role="maker", correlation_id=f"c{i}", payload={
            "snapshot_id": sid, "as_of": "2026-08-17T04:00:00Z", "currency": "INR", "market_type": "ETD_DOMESTIC",
            "policy": toy_policy(), "lots": [lot("L1", "GSEC", str(100 + i), txn="1")]})["snapshot_id"]

    res = race(4, imp)
    assert not [c for c in codes(res) if c.startswith("ERR")], res
    assert codes(res).count("ok") == 1 and set(codes(res)) - {"ok"} == {"SNAPSHOT_CONTENT_MISMATCH"}, codes(res)


# ---------------------------------------------------------------------------- TEETH (PostgreSQL only)
pg_only = pytest.mark.skipif(is_sqlite(), reason="SQLite serialises writers (BEGIN IMMEDIATE): these races exist only on PostgreSQL")


@pg_only
def test_teeth_guard_removed_double_pledge_happens(db, up, monkeypatch):
    """Disable the conditional-UPDATE guard AND the pre-check verifier: two 80-unit reservations on a 100-unit lot BOTH win.
    This is exactly the violation test_g13_* / reserved_invariant would flag, so those tests fail without the guard."""
    sid = uid("SNAP")
    snapshot(db, sid, [lot("L1", "GSEC", "100", txn="1")], toy_policy())
    mid = up.add_margin(required_amount={"amount": "80", "currency": "INR"}, rounded_amount="80")
    pids = [run(db, mid, sid)["proposal_id"] for _ in range(2)]
    aids = [approve(db, pid)["approval"]["approval_id"] for pid in pids]

    def unguarded(session, tenant_id, lot_pk, q):  # no availability predicate
        session.execute(update(ColLot).where(ColLot.id == lot_pk).values(reserved_u=ColLot.reserved_u + q))
        return True

    monkeypatch.setattr(S, "_reserve_lot_units", unguarded)
    monkeypatch.setattr(S, "_run_verify", lambda *a, **k: {"ok": True, "checks": [], "failures": [], "coverage": "0", "required": "0"})
    res = race(2, lambda s, i: S.reserve(s, T, pids[i], aids[i], actor_id="u")["state"])
    assert codes(res) == ["ok", "ok"], res
    assert lot_row(db, sid).reserved_u == 160 * U > 100 * U   # double pledge: the invariant the real tests assert is broken


@pg_only
def test_teeth_proposal_lock_removed_duplicate_approvals(db, up, monkeypatch):
    """Without the proposal row lock all contenders pass the revision check on the same stale row (lost update)."""
    out, sid, _ = new_checked(db, up)
    pid, rev = out["proposal_id"], out["revision"]
    orig_get, orig_check = S._get, S._check_rev
    bar = threading.Barrier(4)

    def unlocked_get(session, tenant_id, proposal_id, lock=False):
        return orig_get(session, tenant_id, proposal_id, False)

    def check_with_rendezvous(p, expected):  # all contenders have READ the row before any of them writes
        try:
            bar.wait(timeout=5)
        except threading.BrokenBarrierError:
            pass
        return orig_check(p, expected)

    monkeypatch.setattr(S, "_get", unlocked_get)
    monkeypatch.setattr(S, "_check_rev", check_with_rendezvous)
    res = race(4, lambda s, i: S.approve_proposal(s, T, principal(f"chk-{i}", "checker"), pid, reason="independent review",
                                                  expected_revision=rev)["approval"]["approval_id"])
    assert codes(res).count("ok") > 1, res                     # lost update reproduced => the real test above has teeth
    n = db.execute(select(func.count()).select_from(ApprovalRow).where(ApprovalRow.target_id == pid)).scalar_one()
    assert n > 1
