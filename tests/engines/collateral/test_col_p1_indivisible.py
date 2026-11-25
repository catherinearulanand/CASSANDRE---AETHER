"""P1: indivisible (whole-block) lots via MILP. Optimality is checked against exhaustive enumeration of the integer grid; a
repaired or time-limited result must never be labelled optimal; every allocation passes the independent verifier.

Model definition used by the enumeration (docs/methodologies/collateral.md): coverage must lie in [R, R + A] where A is the
over-posting allowance = the largest single block's effective coverage among blocked lots (whole blocks cannot hit R exactly).
"""
import itertools
import random
from decimal import Decimal

import pytest

from app.engines.collateral.optimiser import Item, solve
from tests.engines.collateral.conftest import lot, snapshot, uid
from tests.engines.collateral.test_col_services import run, toy_policy

D = Decimal


def brute_force(items, required):
    """items: (e, cost_per_unit, avail, size). Exhaustive over whole blocks; returns min cost or None."""
    allow = max(e * size for e, c, av, size in items if size > 0 and av >= size)
    ranges = [range(int(av // size) + 1) for e, c, av, size in items]
    best = None
    for combo in itertools.product(*ranges):
        cov = sum(n * size * e for n, (e, c, av, size) in zip(combo, items))
        if required - 1e-9 <= cov <= required + allow + 1e-9:
            cost = sum(n * size * c for n, (e, c, av, size) in zip(combo, items))
            best = cost if best is None or cost < best else best
    return best


@pytest.mark.parametrize("seed", range(12))
def test_milp_matches_exhaustive_enumeration(seed):
    rng = random.Random(seed)
    n = rng.randint(2, 3)
    raw = [(1.0, float(rng.randint(1, 9)), float(rng.randint(30, 90)), float(rng.choice([5, 7, 10, 12]))) for _ in range(n)]
    required = float(rng.randint(40, 120))
    items = [Item(f"L{i}", e, c, av, size) for i, (e, c, av, size) in enumerate(raw)]
    want = brute_force(raw, required)
    r = solve(items, required, [])
    if want is None:
        assert r.status == "infeasible", (seed, r.status)
        return
    assert r.status == "optimal" and r.integer_lots and r.shadow_price is None
    assert r.objective == pytest.approx(want, rel=1e-6, abs=1e-6), (seed, r.objective, want)
    for x, (e, c, av, size) in zip(r.x, raw):
        assert abs(x / size - round(x / size)) < 1e-6 and x <= av + 1e-9      # whole blocks, within availability


def test_service_hand_value_two_blocked_lots(db, up):
    """Hand-derived: A (size 7, 60 avail, 1/unit), B (size 10, 100 avail, 3/unit), R=100 -> A x8=56, B x5=50, coverage 106, cost 206."""
    sid = uid("SNAP")
    snapshot(db, sid, [lot("A", "GSEC", "60", txn="1", lot_size="7"), lot("B", "GSEC", "100", txn="3", lot_size="10")], toy_policy())
    out = run(db, up.add_margin(), sid)
    legs = {l["lot_id"]: D(l["quantity"]) for l in out["legs"]}
    assert legs == {"A": D(56), "B": D(50)}, legs
    assert out["solver_status"] == "optimal" and D(out["objective"]) == D(206) and out["state"] == "CHECKED"
    assert D(out["result"]["coverage"]) == D(106) and out["result"]["verification"]["ok"]
    assert out["result"]["coverage_shadow_price"] is None and out["result"]["integer_lots"] is True


def test_blocks_larger_than_availability_are_infeasible_not_fractionalised(db, up):
    sid = uid("SNAP")
    snapshot(db, sid, [lot("A", "GSEC", "6", txn="1", lot_size="7")], toy_policy())   # not even one 7-unit block exists
    out = run(db, up.add_margin(required_amount={"amount": "5", "currency": "INR"}, rounded_amount="5"), sid)
    assert out["state"] in ("INFEASIBLE", "NOT_REQUIRED") or not out["legs"]
    assert out["state"] != "CHECKED"


@pytest.mark.parametrize("seed", range(6))
def test_repair_never_claims_optimal_and_checked_always_verified(db, up, seed):
    rng = random.Random(100 + seed)
    sid = uid("SNAP")
    lots = [lot(f"M{i}", "GSEC", str(rng.randint(40, 90)), txn=str(rng.randint(1, 5)), lot_size=str(rng.choice([3, 7, 11])))
            for i in range(rng.randint(2, 4))] + [lot("C", "CASH", "500", txn="6")]
    snapshot(db, sid, lots, toy_policy())
    out = run(db, up.add_margin(required_amount={"amount": str(rng.randint(60, 200)), "currency": "INR"}), sid)
    if out["state"] == "CHECKED":
        assert out["result"]["verification"]["ok"]
        assert D(out["result"]["coverage"]) >= D(out["required_coverage"])
    if out["result"].get("repair_applied"):
        assert out["solver_status"] != "optimal"          # optimality never claimed after a heuristic repair
    for l in out["legs"]:
        size = D(next(x for x in lots if x["lot_id"] == l["lot_id"]).get("lot_size", "0"))
        if size > 0:
            assert D(l["quantity"]) % size == 0
