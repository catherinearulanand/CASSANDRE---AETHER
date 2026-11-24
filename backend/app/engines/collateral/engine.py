"""Optimisation pipeline: assessments -> LP/MILP -> Decimal rounding + conservative repair -> independent verification
-> baseline. No DB access here (services.py loads/saves)."""
from __future__ import annotations

from datetime import datetime
from decimal import ROUND_CEILING, Decimal, localcontext

from app.core.money import dstr
from app.engines.collateral import optimiser as O
from app.engines.collateral import valuation as V
from app.engines.collateral import verifier
from app.engines.collateral.eligibility import Assessment

Q6 = Decimal("0.000001")


def _ceil6(x: Decimal) -> Decimal:
    return x.quantize(Q6, rounding=ROUND_CEILING)


def build_groups(el: list[Assessment], sources: list[dict]) -> tuple[list[O.Group], list[dict]]:
    """Cap groups from schedule entries (per source) and issuer-group caps. Returns (solver groups, meta rows)."""
    groups: list[O.Group] = []
    meta: list[dict] = []
    by_key: dict[str, list[int]] = {}
    for i, a in enumerate(el):
        for k in a.entry_keys:
            by_key.setdefault(k, []).append(i)
    for src in sources:
        name = src.get("source", "POLICY")
        for idx, e in enumerate(src.get("schedule") or []):
            if e.get("concentration_cap_pct") is None:
                continue
            key = f"{name}#{idx}"
            members = by_key.get(key, [])
            if not members:
                continue
            denom = e.get("cap_denominator") or "TOTAL_ALLOCATED"
            cap = V.D(e["concentration_cap_pct"]) / 100
            gname = f"CAP {key} {e['asset_class']} <= {dstr(cap * 100)}% of {denom}"
            groups.append(O.Group(gname, members, float(cap), denom))
            meta.append({"name": gname, "kind": "CAP_OBLIGATION" if denom == "OBLIGATION" else "CAP_TOTAL",
                         "cap": cap, "denom": denom, "members": members})
        if src.get("issuer_group_cap_pct") is not None:
            denom = src.get("issuer_cap_denominator") or "TOTAL_ALLOCATED"
            cap = V.D(src["issuer_group_cap_pct"]) / 100
            issuers: dict[str, list[int]] = {}
            for i, a in enumerate(el):
                if a.issuer_group:
                    issuers.setdefault(a.issuer_group, []).append(i)
            for g, members in sorted(issuers.items()):
                gname = f"CAP issuer-group {g} <= {dstr(cap * 100)}% of {denom}"
                groups.append(O.Group(gname, members, float(cap), denom))
                meta.append({"name": gname, "kind": "CAP_OBLIGATION" if denom == "OBLIGATION" else "CAP_TOTAL",
                             "cap": cap, "denom": denom, "members": members})
    return groups, meta


def _coverage(el: list[Assessment], q: dict[int, Decimal]) -> Decimal:
    with localcontext() as c:
        c.prec = V.PRECISION
        return sum((qty * el[i].e for i, qty in q.items()), Decimal(0))  # type: ignore[operator]


def round_and_repair(el: list[Assessment], x: list[float], required: Decimal, mode: str = "CEIL") -> tuple[dict[int, Decimal], bool]:
    """Round LP quantities to 6dp (whole blocks for indivisible lots) and clip to availability.
    mode CEIL   : float noise is first snapped to 9dp, then rounded UP to 6dp, so effective coverage is not below the LP's;
    mode NEAREST: half-even rounding. In either mode, if coverage is still short (e.g. a lot clipped at availability)
    the cheapest-per-coverage lot with headroom is topped up (a heuristic REPAIR -> optimality is then not claimed).
    Returns (quantities by index, repaired?)."""
    snap = Decimal("0.000000001")
    q: dict[int, Decimal] = {}
    for i, a in enumerate(el):
        if x[i] <= 1e-9:
            continue
        raw = Decimal(repr(x[i])).quantize(snap)
        if a.lot_size > 0:
            val = (raw / a.lot_size).to_integral_value() * a.lot_size
        elif mode == "CEIL":
            val = _ceil6(raw)
        else:
            val = V.q6(raw)
        val = min(val, a.available)
        if a.lot_size > 0:
            val = (val / a.lot_size).to_integral_value(rounding="ROUND_FLOOR") * a.lot_size
        if val > 0:
            q[i] = val
    repaired = False
    shortfall = required - _coverage(el, q)
    if shortfall > 0:
        order = sorted(range(len(el)), key=lambda i: (el[i].cost_unit / el[i].e, el[i].lot_id))
        for i in order:
            if shortfall <= 0:
                break
            a = el[i]
            head = a.available - q.get(i, Decimal(0))
            if head <= 0:
                continue
            if a.lot_size > 0:
                if head < a.lot_size:
                    continue
                add = a.lot_size
            else:
                add = min(head, _ceil6(shortfall / a.e))
            q[i] = q.get(i, Decimal(0)) + add
            repaired = True
            shortfall = required - _coverage(el, q)
    return q, repaired


def lots_for_verify(rows: list, assess_by_pk: dict[str, Assessment]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for r in rows:
        out[r["lot_id"]] = r
    return out


def overpost_allowance(el: list[Assessment], q: dict[int, Decimal], required: Decimal) -> Decimal:
    tol = required * Decimal("1e-7") + sum((el[i].e * Q6 for i in q), Decimal(0))
    blocks = [el[i].e * el[i].lot_size for i in q if el[i].lot_size > 0]
    return tol + (max(blocks) if blocks else Decimal(0))


def leg_dicts(el: list[Assessment], q: dict[int, Decimal], required: Decimal) -> list[dict]:
    cov_total = _coverage(el, q)
    legs = []
    for i in sorted(q, key=lambda k: el[k].lot_id):
        a, qty = el[i], q[i]
        cov = qty * a.e
        legs.append({
            "lot_id": a.lot_id, "asset_class": a.asset_class, "currency": a.currency, "quantity": dstr(qty),
            "price": dstr(a.price), "fx_rate": dstr(a.fx_rate), "gross_value": dstr(qty * a.price * a.fx_rate),
            "valuation_factor": dstr(a.valuation_factor), "fx_factor": dstr(a.fx_factor),
            "effective_coverage": dstr(cov), "cost_horizon": dstr(qty * a.cost_unit),
            "share_of_coverage": dstr(cov / cov_total) if cov_total else "0",
            "settlement_lag_days": a.settlement_lag_days})
    return legs


def run_verifier(legs: list[dict], raw_lots: dict[str, dict], params: dict) -> dict:
    ctx = {**params, "as_of": datetime.fromisoformat(params["as_of"]), "deadline": datetime.fromisoformat(params["deadline"])}
    return verifier.verify_legs([{"lot_id": l["lot_id"], "quantity": l["quantity"]} for l in legs], raw_lots, ctx)


def _greedy(el: list[Assessment], order: list[int], required: Decimal) -> dict[int, Decimal]:
    q: dict[int, Decimal] = {}
    rem = required
    for i in order:
        if rem <= 0:
            break
        a = el[i]
        if a.lot_size > 0:
            blocks = (rem / (a.e * a.lot_size)).to_integral_value(rounding=ROUND_CEILING)
            blocks = min(blocks, (a.available / a.lot_size).to_integral_value(rounding="ROUND_FLOOR"))
            take = blocks * a.lot_size
        else:
            take = min(a.available, _ceil6(rem / a.e))
        if take > 0:
            q[i] = take
            rem = required - _coverage(el, q)
    return q if rem <= 0 else {}


def baseline(el: list[Assessment], required: Decimal, raw_lots: dict[str, dict], params: dict, optimal_cost: Decimal) -> dict:
    """Independently defined, feasibility-checked baselines. B1 = cash only; B2 = cash first, then remaining
    eligible lots in lot_id order. Savings are reported ONLY against a baseline that passes the same verifier."""
    ordered = sorted(range(len(el)), key=lambda i: el[i].lot_id)
    cash = [i for i in ordered if el[i].asset_class == "CASH"]
    rest = [i for i in ordered if el[i].asset_class != "CASH"]
    tried = []
    for name, order in (("B1_CASH_ONLY", cash), ("B2_CASH_FIRST_THEN_LOT_ORDER", cash + rest)):
        q = _greedy(el, order, required)
        if not q:
            tried.append({"policy": name, "feasible": False, "reason": "insufficient eligible lots for this policy"})
            continue
        legs = leg_dicts(el, q, required)
        ver = run_verifier(legs, raw_lots, params)
        if not ver["ok"]:
            tried.append({"policy": name, "feasible": False, "reason": "fails verifier: " + "; ".join(f["name"] for f in ver["failures"][:3])})
            continue
        cost = sum((Decimal(l["cost_horizon"]) for l in legs), Decimal(0))
        sav = cost - optimal_cost
        return {"status": "AVAILABLE", "policy": name, "cost": dstr(cost), "legs": legs, "tried": tried,
                "savings_absolute": dstr(sav), "savings_pct": dstr(sav / cost * 100) if cost > 0 else None,
                "units": "target-currency horizon cost (NOT annualised)",
                "comparability": f"{name} is an independently defined heuristic over the same eligible set, "
                                 "verified by the same constraint verifier; it is not an institution's actual policy."}
    return {"status": "UNAVAILABLE", "policy": None, "tried": tried, "savings_absolute": None, "savings_pct": None,
            "comparability": "No feasible cash-first baseline exists; no savings are claimed."}


def solve_pipeline(el: list[Assessment], required: Decimal, groups: list[O.Group], meta: list[dict], raw_lots: dict[str, dict],
                   params: dict, *, time_limit: float = 10.0) -> dict:
    """el: ONLY eligible assessments (e and cost_unit present). Returns result dict (JSON friendly)."""
    items = [O.Item(a.lot_id, float(a.e), float(a.cost_unit), float(a.available), float(a.lot_size)) for a in el]
    sr = O.solve(items, float(required), groups, time_limit=time_limit)
    res: dict = {"solver_status": sr.status, "solver": sr.info, "runtime_s": round(sr.runtime_s, 6),
                 "solver_message": sr.message, "integer_lots": sr.integer_lots, "legs": [], "constraints": [],
                 "baseline": {"status": "NOT_COMPUTED"}, "diagnostics": {}, "repair_applied": False,
                 "objective": None, "residual": None, "mip_gap": sr.mip_gap, "dual_bound": sr.dual_bound,
                 "coverage_shadow_price": None, "verification": None}
    if sr.status == "infeasible":
        mc = O.max_coverage(items, float(required), groups)
        ach = mc.get("achievable")
        res["diagnostics"] = {
            "achievable_coverage": None if ach is None else dstr(V.q6(Decimal(repr(ach)))),
            "remaining": None if ach is None else dstr(V.q6(required - Decimal(repr(ach)))),
            "binding_constraints": mc.get("binding", []), "lots_at_availability_limit": mc.get("lots_at_availability_limit", []),
            "note": "Legal/policy limits are not relaxed; this is the most coverage obtainable under them."}
        res["residual"] = res["diagnostics"]["remaining"]
        return res
    if sr.status in ("timeout", "error") or not sr.x:
        return res
    q, repaired = round_and_repair(el, sr.x, required, "CEIL")
    legs = leg_dicts(el, q, required)
    cov = _coverage(el, q)
    p2 = {**params, "overpost_allowance": dstr(overpost_allowance(el, q, required))}
    ver = run_verifier(legs, raw_lots, p2)
    if not ver["ok"]:  # second attempt: nearest rounding + conservative repair, then full re-verification
        q, repaired = round_and_repair(el, sr.x, required, "NEAREST")
        legs = leg_dicts(el, q, required)
        cov = _coverage(el, q)
        p2 = {**params, "overpost_allowance": dstr(overpost_allowance(el, q, required))}
        ver = run_verifier(legs, raw_lots, p2)
    res["verification"] = ver
    res["params_overpost_allowance"] = p2["overpost_allowance"]
    cost = sum((Decimal(l["cost_horizon"]) for l in legs), Decimal(0))
    res["legs"], res["repair_applied"] = legs, repaired
    res["objective"] = dstr(cost)
    if sr.objective is not None:
        res["rounding"] = {"lp_objective": dstr(V.q6(Decimal(repr(sr.objective)))), "rounded_cost": dstr(cost),
                           "absolute_gap": dstr(V.q6(cost - Decimal(repr(sr.objective)))),
                           "note": "quantities rounded to 6 decimal places (UP-snapped); gap is the cost of rounding"}
    res["residual"] = dstr(max(Decimal(0), required - cov))
    res["coverage"] = dstr(cov)
    if repaired and res["solver_status"] == "optimal":
        res["solver_status"] = "feasible"  # never claim optimality after heuristic repair
        res["solver_message"] += " | repaired after rounding: optimality not claimed"
    if sr.shadow_price is not None and not sr.integer_lots:
        res["coverage_shadow_price"] = {"value": dstr(V.q6(Decimal(repr(sr.shadow_price)))),
                                        "unit": "target-currency horizon cost per 1 unit of effective coverage",
                                        "note": "dual of the coverage row of a continuous LP; other rows held fixed; "
                                                "not defined for MILP"}
    # constraint rows (Decimal, independent of solver floats)
    rows = [{"name": "COVERAGE >= required", "kind": "COVERAGE", "limit": dstr(required), "measured": dstr(cov),
             "slack": dstr(cov - required), "binding": (cov - required) <= required * Decimal("1e-9"), "unit": "effective coverage"}]
    for m in meta:
        got = sum((q.get(i, Decimal(0)) * el[i].e for i in m["members"]), Decimal(0))
        den = required if m["denom"] == "OBLIGATION" else cov
        lim = m["cap"] * den
        rows.append({"name": m["name"], "kind": m["kind"], "limit": dstr(lim), "measured": dstr(got), "slack": dstr(lim - got),
                     "binding": (lim - got) <= max(Decimal("0.000001"), lim * Decimal("1e-9")), "unit": "effective coverage"})
    for i, qty in sorted(q.items(), key=lambda kv: el[kv[0]].lot_id):
        a = el[i]
        rows.append({"name": f"AVAILABILITY {a.lot_id}", "kind": "AVAILABILITY", "limit": dstr(a.available), "measured": dstr(qty),
                     "slack": dstr(a.available - qty), "binding": qty >= a.available, "unit": "units"})
    res["constraints"] = rows
    if res["verification"]["ok"] is False:
        res["solver_message"] += " | INDEPENDENT VERIFIER FAILED: approval blocked"
    res["baseline"] = baseline(el, required, raw_lots, p2, cost)
    return res
