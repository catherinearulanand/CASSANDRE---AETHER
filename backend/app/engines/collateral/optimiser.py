"""LP / MILP core (floating point, HiGHS via scipy). Pure: numbers in, numbers out. No DB, no Decimal.

Problem (per authorised pool), scaled so that the obligation R == 1:
    variables z_i      continuous lot: z_i = coverage share w_i (so x_i = w_i * R / e_i units)
                       indivisible lot: z_i = integer number of lot_size blocks
    minimise           sum_i cost_i z_i             cost_i = c_i * unit_i   (c_i = horizon cost per unit)
    s.t.               sum a_i z_i >= 1             coverage   (a_i = coverage share per unit of z)
                       sum a_i z_i <= 1 + allow     bounded over-posting (also keeps negative costs bounded)
                       CAP_TOTAL  : sum_{i in g} a_i z_i - cap * sum_i a_i z_i <= 0   (fraction of total allocated coverage)
                       CAP_OBLIG  : sum_{i in g} a_i z_i <= cap                       (fraction of the obligation)
                       0 <= z_i <= ub_i             availability
Tie-break: after the primary optimum z*, minimise sum rank_i a_i z_i subject to cost <= z* (rank = position in the
lot_id-sorted input) - a deterministic secondary objective so equal-cost optima are reproducible.
Status vocabulary: optimal | feasible (non-optimal incumbent) | infeasible | timeout | error.
Shadow price: only for a purely continuous LP; reported per unit of effective coverage, target-currency horizon cost.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

import numpy as np
import scipy
from scipy.optimize import Bounds, LinearConstraint, linprog, milp
from scipy.sparse import csr_matrix

TOL = 1e-12  # relative slack of the primary objective during the lexicographic tie-break
DUST = 1e-7  # coverage shares below this fraction of the obligation are solver-tolerance dust


def solver_info() -> dict:
    try:
        import highspy
        hv = highspy.Highs().version()
    except Exception:  # noqa: BLE001
        hv = "unknown"
    return {"solver": "HiGHS", "highs_version": hv, "scipy_version": scipy.__version__,
            "numpy_version": np.__version__, "feasibility_tolerance": 1e-7, "tie_break_tolerance": TOL}


@dataclass
class Item:
    key: str
    e: float  # effective coverage per unit (>0)
    cost: float  # horizon cost per unit (may be negative)
    avail: float  # units
    lot_size: float = 0.0  # 0 => divisible


@dataclass
class Group:
    name: str
    members: list[int]
    cap: float  # fraction 0..1
    denom: str  # TOTAL_ALLOCATED | OBLIGATION


@dataclass
class SolveResult:
    status: str
    x: list[float] = field(default_factory=list)  # units per item
    objective: float | None = None
    shadow_price: float | None = None  # coverage constraint, continuous LP only
    mip_gap: float | None = None
    dual_bound: float | None = None
    message: str = ""
    runtime_s: float = 0.0
    integer_lots: bool = False
    info: dict = field(default_factory=dict)


def _build(items: list[Item], required: float, groups: list[Group]):
    n = len(items)
    integer = np.array([1 if it.lot_size > 0 else 0 for it in items])
    a = np.empty(n)
    unit = np.empty(n)
    ub = np.empty(n)
    for i, it in enumerate(items):
        if it.lot_size > 0:
            unit[i] = it.lot_size
            a[i] = it.e * it.lot_size / required
            ub[i] = float(np.floor(it.avail / it.lot_size + 1e-12))
        else:
            unit[i] = required / it.e
            a[i] = 1.0
            ub[i] = it.avail * it.e / required
    cost = np.array([it.cost for it in items]) * unit
    # over-posting allowance: negligible for divisible pools, one block of the biggest used lot for indivisible ones
    steps = [a[i] for i in range(n) if integer[i] and ub[i] > 0]
    allow = max(steps) if steps else 1e-9
    rows, lo, hi = [], [], []
    rows.append(a.copy()); lo.append(1.0); hi.append(1.0 + allow)  # coverage window (R scaled to 1)
    names = ["COVERAGE"]
    for g in groups:
        r = np.zeros(n)
        if g.denom == "TOTAL_ALLOCATED":
            r[:] = -g.cap * a
            for i in g.members:
                r[i] += a[i]
            rows.append(r); lo.append(-np.inf); hi.append(0.0)
        else:
            for i in g.members:
                r[i] = a[i]
            rows.append(r); lo.append(-np.inf); hi.append(g.cap)
        names.append(g.name)
    return a, unit, ub, cost, integer, np.array(rows), np.array(lo), np.array(hi), names, allow


def _solve_once(c, A, lo, hi, ub, integer, time_limit):
    n = len(c)
    if integer.any():
        res = milp(c=c, constraints=LinearConstraint(csr_matrix(A), lo, hi), integrality=integer,
                   bounds=Bounds(np.zeros(n), ub), options={"time_limit": time_limit, "mip_rel_gap": 0.0})
        return res, True
    # linprog form: A_ub x <= b_ub
    A_ub, b_ub = [], []
    for r, l, h in zip(A, lo, hi):
        if np.isfinite(h):
            A_ub.append(r); b_ub.append(h)
        if np.isfinite(l):
            A_ub.append(-r); b_ub.append(-l)
    res = linprog(c, A_ub=csr_matrix(np.array(A_ub)), b_ub=np.array(b_ub), bounds=list(zip(np.zeros(n), ub)),
                  method="highs", options={"time_limit": time_limit, "presolve": True})
    return res, False


def _status(res, is_milp: bool) -> str:
    s = res.status
    if s == 0:
        return "optimal"
    if s == 1:
        return "feasible" if (is_milp and getattr(res, "x", None) is not None) else "timeout"
    if s == 2:
        return "infeasible"
    return "error"


def solve(items: list[Item], required: float, groups: list[Group], *, time_limit: float = 10.0) -> SolveResult:
    t0 = time.perf_counter()
    if required <= 0:
        return SolveResult("error", message="required must be > 0 (zero call is handled before the solver)")
    if not items:
        return SolveResult("infeasible", message="no eligible lots", x=[])
    for it in items:
        if not (it.e > 0 and np.isfinite(it.e) and np.isfinite(it.cost) and np.isfinite(it.avail)):
            return SolveResult("error", message=f"invalid numeric input for lot {it.key}")
    a, unit, ub, cost, integer, A, lo, hi, names, allow = _build(items, required, groups)
    res, is_milp = _solve_once(cost, A, lo, hi, ub, integer, time_limit)
    st = _status(res, is_milp)
    out = SolveResult(st, integer_lots=bool(integer.any()), message=str(getattr(res, "message", "")),
                      info=solver_info())
    if st in ("infeasible", "timeout", "error") or getattr(res, "x", None) is None:
        out.runtime_s = time.perf_counter() - t0
        return out
    z = np.array(res.x)
    primary = float(cost @ z)
    # ---- deterministic tie-break (secondary objective under the primary optimum) ----
    rank = np.arange(len(items), dtype=float) + 1.0
    A2 = np.vstack([A, cost.reshape(1, -1)])
    lo2 = np.append(lo, -np.inf)
    hi2 = np.append(hi, primary + TOL * max(1.0, abs(primary)))
    res2, _ = _solve_once(rank * a, A2, lo2, hi2, ub, integer, max(1.0, time_limit / 2))
    if getattr(res2, "x", None) is not None and res2.status in (0, 1):
        z = np.array(res2.x)
    z = np.clip(z, 0.0, ub)
    # Dust removal: tie-break slack can leave shares < 1e-7 of the obligation on otherwise unused continuous lots.
    # Fold them into the largest continuous lot (coverage preserved, cost change negligible, cap rows re-verified later).
    cont = [i for i in range(len(z)) if not integer[i]]
    dust = [i for i in cont if 0 < z[i] * a[i] < DUST]
    if dust and len(dust) < len(cont):
        big = max(cont, key=lambda i: (z[i] * a[i], -i))
        moved = sum(z[i] * a[i] for i in dust)
        if big not in dust and z[big] + moved / a[big] <= ub[big] + 1e-12:
            z[big] += moved / a[big]
            for i in dust:
                z[i] = 0.0
    if integer.any():
        z = np.where(integer == 1, np.round(z), z)
    x = z * unit
    out.x = [float(v) for v in x]
    out.objective = float(sum(it.cost * xi for it, xi in zip(items, x)))
    if is_milp:
        out.mip_gap = float(getattr(res, "mip_gap", 0.0) or 0.0)
        out.dual_bound = float(getattr(res, "mip_dual_bound", np.nan)) if hasattr(res, "mip_dual_bound") else None
    else:
        # coverage row is the first row; its '>= 1' side was converted to  -row <= -1  (second inequality entry)
        try:
            marg = res.ineqlin.marginals  # d objective / d rhs of each A_ub row
            # row order: for each constraint: [upper (if finite)], [lower (if finite)]
            idx = 0
            idx_lower = None
            for r_i, (l, h) in enumerate(zip(lo, hi)):
                if np.isfinite(h):
                    idx += 1
                if np.isfinite(l):
                    if r_i == 0:
                        idx_lower = idx
                    idx += 1
            if idx_lower is not None:
                out.shadow_price = float(-marg[idx_lower] / required)  # cost per unit of effective coverage
        except Exception:  # noqa: BLE001 - dual unavailable => not reported (never invented)
            out.shadow_price = None
    out.runtime_s = time.perf_counter() - t0
    return out


def max_coverage(items: list[Item], required: float, groups: list[Group], *, time_limit: float = 5.0) -> dict:
    """Auxiliary LP for infeasible cases: the most effective coverage obtainable under availability + caps
    (legal limits are NOT relaxed). Returns achievable coverage (same unit as `required`) and binding constraints."""
    if not items:
        return {"achievable": 0.0, "binding": [], "status": "no_eligible_lots"}
    a, unit, ub, cost, integer, A, lo, hi, names, allow = _build(items, required, groups)
    n = len(items)
    A_ub = [r for r in A[1:]]
    b_ub = [h for h in hi[1:]]
    # no coverage-window rows; maximise sum a z  (== minimise -sum a z)
    res = linprog(-a, A_ub=csr_matrix(np.array(A_ub)) if A_ub else None, b_ub=np.array(b_ub) if A_ub else None,
                  bounds=list(zip(np.zeros(n), ub)), method="highs", options={"time_limit": time_limit})
    if res.status != 0:
        return {"achievable": None, "binding": [], "status": "error"}
    z = res.x
    binding = []
    for nm, r, h in zip(names[1:], A[1:], hi[1:]):
        if abs(float(r @ z) - h) <= 1e-7:
            binding.append(nm)
    at_ub = [items[i].key for i in range(n) if abs(z[i] - ub[i]) <= 1e-9]
    return {"achievable": float(a @ z) * required, "binding": binding, "lots_at_availability_limit": at_ub, "status": "ok"}
