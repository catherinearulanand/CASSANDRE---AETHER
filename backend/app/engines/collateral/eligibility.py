"""Eligibility intersection + per-lot assessment.

A lot is usable for an obligation only if ALL hold:
  * explicit entitlement (lot.entitled_to contains the obligation id) - default deny, no pooling
  * issuer group is not on the explicit wrong-way list (relationship data, never name similarity)
  * every active policy source (CONTRACT schedule, CLEARING_POLICY schedule, REGULATORY overlay) has a matching entry
  * price present and > 0, FX present when cross-currency, rating/maturity known when the entry restricts them
  * settles by the deadline (as_of + settlement_lag_days <= deadline; calendar days, P0)
  * has available units (units - encumbered - reserved > 0)
Unknown price/rating/FX/entitlement EXCLUDES the lot with a reason code; nothing is defaulted.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal

from app.core.money import NumericError
from app.engines.collateral import valuation as V

RATING_ORDER = ["AAA", "AA+", "AA", "AA-", "A+", "A", "A-", "BBB+", "BBB", "BBB-", "BB+", "BB", "BB-",
                "B+", "B", "B-", "CCC+", "CCC", "CCC-", "CC", "C", "D"]
CAP_DENOMS = ("OBLIGATION", "TOTAL_ALLOCATED")


@dataclass
class Assessment:
    lot_pk: str
    lot_id: str
    asset_class: str
    currency: str
    issuer_group: str | None
    eligible: bool
    reasons: list[str] = field(default_factory=list)
    available: Decimal = Decimal(0)
    price: Decimal | None = None
    fx_rate: Decimal | None = None
    valuation_factor: Decimal | None = None
    fx_factor: Decimal | None = None
    e: Decimal | None = None  # effective coverage per unit
    cost_unit: Decimal | None = None  # horizon cost per unit
    lot_size: Decimal = Decimal(0)
    entry_keys: list[str] = field(default_factory=list)  # "<SOURCE>#<index>" groups used for caps
    settlement_lag_days: int = 0


def rating_rank(r: str | None) -> int | None:
    if r is None:
        return None
    r = r.strip().upper()
    return RATING_ORDER.index(r) if r in RATING_ORDER else None


def validate_source(src: dict) -> list[str]:
    """Static validation of a policy source; returns error strings (empty = valid)."""
    errs: list[str] = []
    for i, e in enumerate(src.get("schedule") or []):
        try:
            V.valuation_factor(e)
            if e.get("fx_haircut_pct") is not None:
                V.pct_to_factor(V.D(e["fx_haircut_pct"]), "fx_haircut_pct")
            if e.get("concentration_cap_pct") is not None:
                V.pct_to_factor(V.D(e["concentration_cap_pct"]), "concentration_cap_pct")
                if (e.get("cap_denominator") or "TOTAL_ALLOCATED") not in CAP_DENOMS:
                    errs.append(f"{src.get('source')}#{i}: cap_denominator must be one of {CAP_DENOMS}")
            if e.get("min_rating") and rating_rank(e["min_rating"]) is None:
                errs.append(f"{src.get('source')}#{i}: unknown min_rating {e['min_rating']}")
        except NumericError as exc:
            errs.append(f"{src.get('source')}#{i}: {exc}")
        if not e.get("asset_class"):
            errs.append(f"{src.get('source')}#{i}: asset_class required")
    return errs


def match_entry(schedule: list[dict], lot) -> tuple[int, dict] | None:
    """First entry (file order) whose asset_class / currency / issuer_class constraints match the lot."""
    for i, e in enumerate(schedule):
        if e.get("asset_class") != lot.asset_class:
            continue
        if e.get("currency") and e["currency"] != lot.currency:
            continue
        ic = e.get("issuer_class")
        if ic and ic != "ANY" and ic != lot.issuer_class:  # "ANY" = explicit wildcard (COVENANT vocabulary); unknown lot class never matches a specific class
            continue
        return i, e
    return None


def available_units(lot) -> Decimal:
    return V.from_u(max(0, lot.units_u - lot.encumbered_u - lot.reserved_u))


def assess_lot(lot, *, obligation_id: str, target_ccy: str, sources: list[dict], wrong_way_groups: list[str],
               as_of: datetime, deadline: datetime, horizon_days: int, extra_available: int = 0,
               stress: dict | None = None, mode: str = "OPTIMISE") -> Assessment:
    stress = stress or {}
    a = Assessment(lot_pk=lot.id, lot_id=lot.lot_id, asset_class=lot.asset_class, currency=lot.currency,
                   issuer_group=lot.issuer_group, eligible=True, lot_size=V.from_u(lot.lot_size_u),
                   settlement_lag_days=lot.settlement_lag_days)
    avail_u = max(0, lot.units_u - lot.encumbered_u - lot.reserved_u + extra_available)
    a.available = V.from_u(avail_u)
    if lot.id in set(stress.get("unavailable_lot_ids", [])) or lot.lot_id in set(stress.get("unavailable_lot_ids", [])):
        a.reasons.append("STRESS_UNAVAILABLE")
    posted = mode == "POSTED"  # valuing collateral ALREADY posted: no entitlement/availability/settlement/cost checks
    if not posted and obligation_id not in (lot.entitled_to or []):
        a.reasons.append("NO_ENTITLEMENT")
    if lot.issuer_group and lot.issuer_group in set(wrong_way_groups):
        a.reasons.append("WRONG_WAY_RISK")
    if not posted and avail_u <= 0:
        a.reasons.append("NO_AVAILABLE_UNITS")
    if not posted and as_of + timedelta(days=lot.settlement_lag_days) > deadline:
        a.reasons.append("SETTLEMENT_AFTER_DEADLINE")
    # price / fx
    try:
        a.price = V.D(lot.price, "price") if lot.price is not None else None
    except NumericError:
        a.price = None
    if a.price is None or a.price <= 0:
        a.reasons.append("UNKNOWN_PRICE")
    if lot.currency == target_ccy:
        a.fx_rate = Decimal(1)
    else:
        try:
            a.fx_rate = V.D(lot.fx_to_target, "fx_to_target") if lot.fx_to_target is not None else None
        except NumericError:
            a.fx_rate = None
        if a.fx_rate is None or a.fx_rate <= 0:
            a.reasons.append("MISSING_FX")
    # policy intersection
    vfs: list[Decimal] = []
    fxs: list[Decimal] = []
    matrating = rating_rank(lot.rating)
    mat = date.fromisoformat(lot.maturity_date) if lot.maturity_date else None
    for src in sources:
        sched = src.get("schedule") or []
        if not sched:
            continue
        m = match_entry(sched, lot)
        name = src.get("source", "POLICY")
        if m is None:
            a.reasons.append(f"NO_SCHEDULE_MATCH:{name}")
            continue
        idx, e = m
        try:
            vf = V.valuation_factor(e)
            xf = V.fx_factor(e, lot.currency, target_ccy)
        except NumericError as exc:
            a.reasons.append(str(exc).split(":")[0] + f":{name}")
            continue
        vfs.append(vf)
        fxs.append(xf)
        a.entry_keys.append(f"{name}#{idx}")
        if e.get("min_rating"):
            if matrating is None:
                a.reasons.append("UNKNOWN_RATING")
            elif matrating > RATING_ORDER.index(e["min_rating"].upper()):
                a.reasons.append("BELOW_MIN_RATING")
        if e.get("max_maturity_years") is not None:
            if mat is None:
                a.reasons.append("UNKNOWN_MATURITY")
            elif (mat - as_of.date()).days > int(Decimal(str(e["max_maturity_years"])) * 365):
                a.reasons.append("MATURITY_EXCEEDS_LIMIT")
    if not vfs and not any(src.get("schedule") for src in sources):
        a.reasons.append("NO_POLICY_SOURCE")
    if a.price and a.price > 0 and a.fx_rate and a.fx_rate > 0 and vfs and not a.reasons:
        vf = min(vfs)
        add = stress.get("haircut_add_pct")
        if add and lot.asset_class != "CASH":
            vf = max(Decimal(0), vf - V.D(add, "haircut_add_pct") / V.HUNDRED)
        a.valuation_factor, a.fx_factor = vf, min(fxs)
        a.e = V.effective_per_unit(a.price, a.fx_rate, a.valuation_factor, a.fx_factor)
        rate = V.D(lot.annual_cost_rate, "annual_cost_rate") if lot.annual_cost_rate is not None else None
        if posted:
            pass
        elif rate is None:
            a.reasons.append("UNKNOWN_COST")  # no documented cost assumption => cannot optimise this lot
        else:
            bump = stress.get("cost_add_bps")
            if bump:
                rate = rate + V.D(bump, "cost_add_bps") / Decimal(10000)
            a.cost_unit = V.cost_per_unit(a.price, a.fx_rate, rate, horizon_days, V.D(lot.txn_cost_per_unit, "txn_cost"))
        if a.e is not None and a.e <= 0:
            a.reasons.append("ZERO_COVERAGE")
    a.eligible = not a.reasons
    return a
