import itertools

import pytest

from app.engines.collateral import optimiser as O
from app.engines.collateral.optimiser import Group, Item, max_coverage, solve


@pytest.mark.gold
def test_g05_tiny_lp_matches_hand_solution():
    # required 100; A avail 60 cost 1; B avail 100 cost 3 -> optimum A60 + B40 = 180 (vs B100 = 300)
    r = solve([Item("A", 1.0, 1.0, 60.0), Item("B", 1.0, 3.0, 100.0)], 100.0, [])
    assert r.status == "optimal"
    assert [round(v, 6) for v in r.x] == [60.0, 40.0]
    assert round(r.objective, 6) == 180.0
    assert round(300.0 / 180.0, 6) == round(1.6666667, 6)  # 40 % lower than all-B (300 -> 180)


def test_shadow_price_continuous_lp_matches_perturbation():
    items = [Item("A", 1.0, 1.0, 60.0), Item("B", 1.0, 3.0, 100.0)]
    base = solve(items, 100.0, [])
    up = solve(items, 101.0, [])
    assert base.shadow_price == pytest.approx(3.0, abs=1e-6)
    assert up.objective - base.objective == pytest.approx(base.shadow_price, abs=1e-6)


def test_no_dual_reported_for_milp():
    r = solve([Item("A", 1.0, 1.0, 60.0, lot_size=7.0), Item("B", 1.0, 3.0, 100.0)], 100.0, [])
    assert r.status == "optimal" and r.integer_lots and r.shadow_price is None
    assert r.x[0] % 7 == pytest.approx(0, abs=1e-9)  # whole blocks of 7


@pytest.mark.gold
def test_g07_infeasible_reports_remaining_and_achievable():
    items = [Item("A", 1.0, 1.0, 50.0), Item("B", 1.0, 3.0, 30.0)]
    r = solve(items, 100.0, [])
    assert r.status == "infeasible"
    mc = max_coverage(items, 100.0, [])
    assert mc["achievable"] == pytest.approx(80.0) and 100 - mc["achievable"] == pytest.approx(20.0)


def test_cap_fraction_of_total_linearised():
    # B limited to 30 % of total allocated coverage; A avail 80 -> B <= 0.3*(A+B) => B <= 34.28 when A=80; need 100
    items = [Item("A", 1.0, 1.0, 80.0), Item("B", 1.0, 0.5, 100.0)]
    r = solve(items, 100.0, [Group("capB", [1], 0.30, "TOTAL_ALLOCATED")])
    assert r.status == "optimal"
    total = sum(r.x)
    assert r.x[1] <= 0.30 * total + 1e-7
    assert r.x[1] == pytest.approx(30.0, abs=1e-6) and r.x[0] == pytest.approx(70.0, abs=1e-6)


def test_cap_fixed_obligation_denominator():
    items = [Item("A", 1.0, 1.0, 80.0), Item("B", 1.0, 0.5, 100.0)]
    r = solve(items, 100.0, [Group("capB", [1], 0.25, "OBLIGATION")])
    assert r.x[1] == pytest.approx(25.0, abs=1e-6) and r.x[0] == pytest.approx(75.0, abs=1e-6)


def test_cap_that_makes_problem_infeasible_is_not_relaxed():
    r = solve([Item("A", 1.0, 1.0, 60.0), Item("B", 1.0, 3.0, 100.0)], 100.0, [Group("capB", [1], 0.30, "TOTAL_ALLOCATED")])
    assert r.status == "infeasible"


def test_fx_inversion_effect_via_effective_factor():
    # lot with e=0.5 (e.g. 0.5 target per unit): need 100 -> 200 units
    r = solve([Item("X", 0.5, 1.0, 1000.0)], 100.0, [])
    assert r.x[0] == pytest.approx(200.0)


def test_deterministic_tie_break_lot_order():
    items = [Item("L1", 1.0, 2.0, 100.0), Item("L2", 1.0, 2.0, 100.0), Item("L3", 1.0, 2.0, 100.0)]
    runs = [solve(items, 100.0, []).x for _ in range(5)]
    assert all(r == runs[0] for r in runs)
    assert runs[0][0] == pytest.approx(100.0)  # secondary objective prefers lowest rank (lot_id order)


def test_negative_cost_is_bounded_by_overpost_limit():
    r = solve([Item("N", 1.0, -1.0, 1e6), Item("P", 1.0, 1.0, 1e6)], 100.0, [])
    assert r.status == "optimal"
    assert sum(r.x) <= 100.0 * (1 + 1e-6)  # does not run off to availability


def test_zero_required_is_rejected_by_solver_zero_call_handled_upstream():
    assert solve([Item("A", 1.0, 1.0, 1.0)], 0.0, []).status == "error"


def test_no_eligible_lots_infeasible():
    assert solve([], 10.0, []).status == "infeasible"


def test_status_mapping():
    class R:  # noqa: D401
        def __init__(self, s, x=None):
            self.status, self.x = s, x
    assert O._status(R(0), False) == "optimal"
    assert O._status(R(1), False) == "timeout"
    assert O._status(R(1, [1.0]), True) == "feasible"  # MILP incumbent at time limit: NOT claimed optimal
    assert O._status(R(2), False) == "infeasible"
    assert O._status(R(4), False) == "error"


def test_solver_version_recorded():
    info = solve([Item("A", 1.0, 1.0, 10.0)], 5.0, []).info
    assert info["solver"] == "HiGHS" and info["highs_version"] and info["scipy_version"]


@pytest.mark.parametrize("seed", range(6))
def test_lp_vs_exhaustive_grid_small_integer_instances(seed):
    import random
    rng = random.Random(seed)
    n = 3
    items = [Item(f"L{i}", 1.0, float(rng.randint(1, 9)), float(rng.randint(2, 6))) for i in range(n)]
    required = float(rng.randint(3, int(sum(i.avail for i in items))))
    r = solve(items, required, [])
    assert r.status == "optimal"
    best = None
    ranges = [range(0, int(it.avail) + 1) for it in items]
    for combo in itertools.product(*ranges):  # exhaustive integer grid (e == 1, integer data => LP optimum integral)
        if sum(combo) >= required and sum(combo) <= required:
            c = sum(k * it.cost for k, it in zip(combo, items))
            best = c if best is None else min(best, c)
    assert best is not None
    assert r.objective == pytest.approx(best, abs=1e-6)
