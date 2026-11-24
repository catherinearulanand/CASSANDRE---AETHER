"""INDEPENDENT feasibility verifier (Decimal only).

Deliberately does NOT import optimiser.py, eligibility.py or valuation.py: it re-derives eligibility, effective
coverage and every cap from raw lot rows + the frozen policy, so an error in the solver path cannot also hide here.
Any failed check blocks approval.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal, localcontext

_RATINGS = ["AAA", "AA+", "AA", "AA-", "A+", "A", "A-", "BBB+", "BBB", "BBB-", "BB+", "BB", "BB-",
            "B+", "B", "B-", "CCC+", "CCC", "CCC-", "CC", "C", "D"]
_SCALE = Decimal(10) ** 6


def _lot_effective(lot: dict, sources: list[dict], target: str, add_pct=None) -> tuple[Decimal | None, list[str], list[str]]:
    """returns (effective per unit, problems, group keys)."""
    probs: list[str] = []
    groups: list[str] = []
    price = Decimal(lot["price"]) if lot.get("price") not in (None, "") else None
    if price is None or price <= 0:
        return None, ["PRICE_UNKNOWN"], groups
    if lot["currency"] == target:
        fx = Decimal(1)
    else:
        if lot.get("fx_to_target") in (None, ""):
            return None, ["FX_UNKNOWN"], groups
        fx = Decimal(lot["fx_to_target"])
    factors: list[Decimal] = []
    fxf: list[Decimal] = []
    for src in sources:
        sched = src.get("schedule") or []
        if not sched:
            continue
        hit = None
        for i, e in enumerate(sched):
            if (e["asset_class"] == lot["asset_class"] and (not e.get("currency") or e["currency"] == lot["currency"])
                    and (not e.get("issuer_class") or e["issuer_class"] == lot.get("issuer_class"))):
                hit = (i, e)
                break
        if hit is None:
            probs.append(f"NOT_IN_SCHEDULE:{src.get('source')}")
            continue
        i, e = hit
        groups.append(f"{src.get('source')}#{i}")
        v, h = e.get("valuation_pct"), e.get("haircut_pct")
        if v is None and h is None:
            probs.append("NO_VALUATION")
            continue
        if v is not None and h is not None and Decimal(str(v)) + Decimal(str(h)) != 100:
            probs.append("INCONSISTENT_HAIRCUT")
            continue
        factors.append(Decimal(str(v)) / 100 if v is not None else (100 - Decimal(str(h))) / 100)
        if lot["currency"] == target:
            fxf.append(Decimal(1))
        elif e.get("fx_haircut_pct") is None:
            probs.append("FX_HAIRCUT_MISSING")
        else:
            fxf.append((100 - Decimal(str(e["fx_haircut_pct"]))) / 100)
        if e.get("min_rating"):
            r = (lot.get("rating") or "").upper()
            if r not in _RATINGS:
                probs.append("RATING_UNKNOWN")
            elif _RATINGS.index(r) > _RATINGS.index(e["min_rating"].upper()):
                probs.append("RATING_BELOW_MIN")
        if e.get("max_maturity_years") is not None:
            if not lot.get("maturity_date"):
                probs.append("MATURITY_UNKNOWN")
            else:
                days = (date.fromisoformat(lot["maturity_date"]) - date.fromisoformat(lot["_as_of_date"])).days
                if days > int(Decimal(str(e["max_maturity_years"])) * 365):
                    probs.append("MATURITY_TOO_LONG")
    if not factors or not fxf or probs:
        return None, probs or ["NO_POLICY"], groups
    with localcontext() as c:
        c.prec = 40
        vf = min(factors)
        if add_pct and lot["asset_class"] != "CASH":
            vf = max(Decimal(0), vf - Decimal(str(add_pct)) / 100)
        return price * fx * vf * min(fxf), probs, groups


def verify_legs(legs: list[dict], lots: dict[str, dict], ctx: dict) -> dict:
    """legs: [{lot_id, quantity(str)}]; lots: lot_id -> raw lot dict with 'available'(str units) ;
    ctx: {required, currency, sources, wrong_way_issuer_groups, obligation_id, as_of, deadline,
          overpost_allowance, stress(optional: haircut_add_pct, unavailable_lot_ids)}"""
    checks: list[dict] = []

    def chk(name, ok, detail=""):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    required = Decimal(ctx["required"])
    target = ctx["currency"]
    as_of = ctx["as_of"]
    deadline = ctx["deadline"]
    seen: set[str] = set()
    cov_total = Decimal(0)
    cov_by_group: dict[str, Decimal] = {}
    cov_by_issuer: dict[str, Decimal] = {}
    detail_legs: list[dict] = []
    stress = ctx.get("stress") or {}
    for leg in legs:
        lid = leg["lot_id"]
        q = Decimal(leg["quantity"])
        lot = lots.get(lid)
        if lot is None:
            chk(f"leg {lid}: lot exists", False, "unknown lot")
            continue
        lot = dict(lot, _as_of_date=as_of.date().isoformat())
        chk(f"leg {lid}: unique", lid not in seen)
        seen.add(lid)
        chk(f"leg {lid}: quantity > 0", q > 0, str(q))
        chk(f"leg {lid}: quantity precision <= 6dp", q == q.quantize(Decimal("0.000001")))
        avail = Decimal(lot["available"])
        chk(f"leg {lid}: inventory (qty <= available)", q <= avail, f"{q} <= {avail}")
        ls = Decimal(lot.get("lot_size") or 0)
        if ls > 0:
            chk(f"leg {lid}: whole multiple of lot size", (q / ls) == (q / ls).to_integral_value(), f"lot_size {ls}")
        chk(f"leg {lid}: entitlement", ctx["obligation_id"] in (lot.get("entitled_to") or []))
        chk(f"leg {lid}: not wrong-way", not (lot.get("issuer_group") and lot["issuer_group"] in ctx["wrong_way_issuer_groups"]))
        chk(f"leg {lid}: settles by deadline", as_of + timedelta(days=int(lot.get("settlement_lag_days", 0))) <= deadline)
        chk(f"leg {lid}: not stress-unavailable", lid not in set(stress.get("unavailable_lot_ids", [])))
        e, probs, groups = _lot_effective(lot, ctx["sources"], target, stress.get("haircut_add_pct"))
        chk(f"leg {lid}: eligible under every policy source", e is not None, ",".join(probs))
        if e is None:
            continue
        cov = q * e
        cov_total += cov
        for g in groups:
            cov_by_group[g] = cov_by_group.get(g, Decimal(0)) + cov
        if lot.get("issuer_group"):
            cov_by_issuer[lot["issuer_group"]] = cov_by_issuer.get(lot["issuer_group"], Decimal(0)) + cov
        detail_legs.append({"lot_id": lid, "effective_per_unit": str(e), "coverage": str(cov)})
    chk("coverage >= required", cov_total >= required, f"{cov_total} >= {required}")
    allow = Decimal(ctx["overpost_allowance"])
    chk("over-posting within allowance", cov_total <= required + allow, f"{cov_total - required} <= {allow}")
    cap_results: list[dict] = []
    for src in ctx["sources"]:
        for i, e in enumerate(src.get("schedule") or []):
            if e.get("concentration_cap_pct") is None:
                continue
            key = f"{src.get('source')}#{i}"
            den = required if (e.get("cap_denominator") or "TOTAL_ALLOCATED") == "OBLIGATION" else cov_total
            lim = Decimal(str(e["concentration_cap_pct"])) / 100 * den
            got = cov_by_group.get(key, Decimal(0))
            ok = got <= lim
            chk(f"cap {key} ({e['asset_class']}) <= {e['concentration_cap_pct']}% of {e.get('cap_denominator') or 'TOTAL_ALLOCATED'}", ok, f"{got} <= {lim}")
            cap_results.append({"name": key, "limit": str(lim), "measured": str(got)})
        if src.get("issuer_group_cap_pct") is not None:
            den = required if (src.get("issuer_cap_denominator") or "TOTAL_ALLOCATED") == "OBLIGATION" else cov_total
            lim = Decimal(str(src["issuer_group_cap_pct"])) / 100 * den
            for g, got in sorted(cov_by_issuer.items()):
                chk(f"issuer-group cap {g} <= {src['issuer_group_cap_pct']}%", got <= lim, f"{got} <= {lim}")
    return {"ok": all(c["ok"] for c in checks), "checks": checks, "coverage": str(cov_total),
            "required": str(required), "legs": detail_legs, "caps": cap_results,
            "failures": [c for c in checks if not c["ok"]]}
