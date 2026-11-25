"""A4 ATLAS service layer. Public contract (CONTRACTS.md v1) is registered at the bottom:
collateral.optimise / verify / reserve / coverage. Everything here recommends; nothing moves assets or transmits.

Every consequential write appends an evidence event in the SAME transaction (services.call('evidence.append')).
If evidence is unavailable the call raises and the whole transaction rolls back (no material transition without evidence).
"""
from __future__ import annotations

import csv
import io
from datetime import date, datetime, timedelta
from importlib import metadata as _md
from decimal import Decimal

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from app.auth.security import Principal, require_distinct_checker
from app.core import services
from app.core.canonical import canonical_bytes, content_hash
from app.core.config import get_settings
from app.core.errors import Blocked, Conflict, Forbidden, NotFound, Unavailable, ValidationFailed
from app.core.events import emit
from app.core.money import SUPPORTED_CURRENCIES, NumericError, dstr
from app.db.base import lock_for_update, new_id, utcnow
from app.db.models_core import ApprovalRow
from app.engines.collateral import engine as E
from app.engines.collateral import optimiser as O
from app.engines.collateral import valuation as V
from app.engines.collateral.eligibility import RATING_ORDER, assess_lot, available_units, validate_source
from app.engines.collateral.models import (ColConstraint, ColInstructionDraft, ColLeg, ColLot, ColProposal,
                                           ColReservation, ColSnapshot)

SERVICE_VERSION = "aether-collateral/1.1"
try:
    _HIGHS = _md.version("highspy")
except _md.PackageNotFoundError:  # pragma: no cover
    _HIGHS = "unknown"
# Method version pins the solver library: a replay on a different HiGHS is NOT_REPLAYABLE (never silently re-run).
METHOD_VERSION = f"collateral-lp/1 highspy/{_HIGHS}"
LP_TOLERANCE = "0.000001"  # declared numerical tolerance for LP objective/coverage when legs differ at platform noise level
PROPOSAL_TYPE, SNAPSHOT_TYPE = "allocation_proposal", "inventory_snapshot"  # lineage stages PROPOSAL / SNAPSHOT (VERITAS)
WATERMARK = "SYNTHETIC DRAFT - NOT TRANSMITTED - NO ASSET MOVEMENT"
OPEN_STATES = ("PROPOSED", "CHECKED", "APPROVED", "RESERVED")
MARKETS = ("OTC", "ETD_DOMESTIC", "ETD_IFSC")


# ------------------------------------------------------------------ evidence helper
def _evidence(session, tenant_id: str, action: str, *, actor_id: str, actor_type: str = "USER", role: str | None = None,
              entity_type: str, entity_id: str, revision: int, correlation_id: str, payload: dict,
              input_hashes: list[str] | None = None, output_hash: str | None = None, rationale: str | None = None,
              effective_at: datetime | None = None):
    return services.call("evidence.append", session, tenant_id, engine="collateral", action=action, actor_id=actor_id,
                         actor_type=actor_type, actor_role=role, entity_type=entity_type, entity_id=entity_id,
                         entity_revision=revision, correlation_id=correlation_id, effective_at=effective_at,
                         input_hashes=[h for h in (input_hashes or []) if h], output_hash=output_hash,
                         rationale=rationale, payload=payload, service_version=SERVICE_VERSION)


# ------------------------------------------------------------------ inventory import
_LOT_FIELDS = ("lot_id", "owner_entity", "custody_account", "asset_class", "currency", "units")


def _parse_csv(text: str) -> list[dict]:
    rows = list(csv.DictReader(io.StringIO(text), strict=True))
    for r in rows:
        for k in ("entitled_to",):
            if isinstance(r.get(k), str):
                r[k] = [x for x in r[k].split(";") if x]
        for k in list(r):
            if r[k] == "":
                r[k] = None
        if r.get("reuse_permission") is not None:
            r["reuse_permission"] = str(r["reuse_permission"]).lower() in ("1", "true", "yes")
    return rows


def _validate_lot(i: int, raw: dict, errs: dict[str, str]) -> dict | None:
    p = f"lots[{i}]"
    n0 = len(errs)

    def need(k):
        if raw.get(k) in (None, ""):
            errs[f"{p}.{k}"] = "required"

    for k in _LOT_FIELDS:
        need(k)
    out: dict = {}
    try:
        for k in ("units", "encumbered_units", "reserved_units", "posted_units", "lot_size"):
            v = raw.get(k)
            d = V.D(v if v not in (None, "") else "0", k)
            if d < 0:
                raise NumericError(f"{k}: negative not allowed")
            if d != d.quantize(V.QTY_DP):
                raise NumericError(f"{k}: more than 6 decimal places")
            out[k] = d
        if out["encumbered_units"] + out["reserved_units"] > out["units"]:
            raise NumericError("encumbered + reserved exceeds units")
        if raw.get("price") not in (None, ""):
            pr = V.D(raw["price"], "price")
            if pr <= 0:
                raise NumericError("price must be > 0 (omit if unknown)")
            out["price"] = dstr(pr)
        if raw.get("fx_to_target") not in (None, ""):
            fx = V.D(raw["fx_to_target"], "fx_to_target")
            if fx <= 0:
                raise NumericError("fx_to_target must be > 0")
            out["fx_to_target"] = dstr(fx)
        if raw.get("annual_cost_rate") not in (None, ""):
            out["annual_cost_rate"] = dstr(V.D(raw["annual_cost_rate"], "annual_cost_rate"))  # fraction p.a.; may be negative
        out["txn_cost_per_unit"] = dstr(V.D(raw.get("txn_cost_per_unit") or "0", "txn_cost_per_unit"))
    except NumericError as exc:
        errs[p] = str(exc)
    for ccy in ("currency",):
        if raw.get(ccy) and raw[ccy] not in SUPPORTED_CURRENCIES:
            errs[f"{p}.{ccy}"] = f"unsupported currency {raw[ccy]!r}"
    if raw.get("rating") and raw["rating"].upper() not in RATING_ORDER:
        errs[f"{p}.rating"] = f"unknown rating {raw['rating']!r}"
    if raw.get("maturity_date"):
        try:
            date.fromisoformat(raw["maturity_date"])
        except ValueError:
            errs[f"{p}.maturity_date"] = "must be ISO date"
    ent = raw.get("entitled_to") or []
    if not isinstance(ent, list) or not all(isinstance(x, str) for x in ent):
        errs[f"{p}.entitled_to"] = "must be a list of obligation ids"
    try:
        out["settlement_lag_days"] = int(raw.get("settlement_lag_days") or 0)
        if out["settlement_lag_days"] < 0:
            raise ValueError
    except (TypeError, ValueError):
        errs[f"{p}.settlement_lag_days"] = "must be a non-negative integer"
    if len(errs) > n0:
        return None
    out.update({k: raw.get(k) for k in ("lot_id", "owner_entity", "custody_account", "custodian", "issuer", "issuer_group",
                                        "issuer_class", "asset_class", "currency", "rating", "maturity_date",
                                        "posted_account_id")})
    out["rating"] = out["rating"].upper() if out.get("rating") else None
    out["entitled_to"] = sorted(ent)
    out["reuse_permission"] = bool(raw.get("reuse_permission", False))
    return out


def _lot_canonical(l: dict) -> dict:
    return {k: (dstr(v) if isinstance(v, Decimal) else v) for k, v in sorted(l.items())}


def import_inventory(session, tenant_id: str, *, actor_id: str, role: str | None, payload: dict, correlation_id: str) -> dict:
    errs: dict[str, str] = {}
    sid = payload.get("snapshot_id")
    if not sid:
        errs["snapshot_id"] = "required"
    ccy = payload.get("currency")
    if ccy not in SUPPORTED_CURRENCIES:
        errs["currency"] = "unsupported or missing currency"
    market = payload.get("market_type")
    if market not in MARKETS:
        errs["market_type"] = f"must be one of {MARKETS} (CCP_OTC is unsupported)"
    try:
        as_of = datetime.fromisoformat(str(payload.get("as_of")).replace("Z", "+00:00"))
        if as_of.tzinfo is None:
            raise ValueError
    except ValueError:
        errs["as_of"] = "must be a timezone-aware ISO-8601 timestamp"
        as_of = None  # type: ignore[assignment]
    lots_raw = payload.get("lots")
    if payload.get("csv"):
        try:
            lots_raw = _parse_csv(payload["csv"])
        except csv.Error as exc:
            errs["csv"] = f"malformed CSV: {exc}"
    if not lots_raw:
        errs["lots"] = "at least one lot is required"
    policy = payload.get("policy") or {}
    wrong = payload.get("wrong_way_issuer_groups") or []
    for src in ([policy] if policy.get("schedule") else []):
        for e in validate_source({"source": "CLEARING_POLICY", **src}):
            errs[f"policy: {e}"] = "invalid schedule entry"
    try:
        canonical_bytes({"policy": policy, "wrong": wrong})  # rejects floats/non-canonical values
    except Exception as exc:  # noqa: BLE001
        errs["policy"] = f"policy must contain decimal strings, not floats: {exc}"
    lots: list[dict] = []
    seen: set[str] = set()
    for i, raw in enumerate(lots_raw or []):
        v = _validate_lot(i, dict(raw), errs)
        if v is not None:
            if v["lot_id"] in seen:
                errs[f"lots[{i}].lot_id"] = f"duplicate lot_id {v['lot_id']}"
            seen.add(v["lot_id"])
            lots.append(v)
    if errs:  # atomic reject: nothing is written (no silent partial import)
        raise ValidationFailed(f"Inventory import rejected ({len(errs)} problem(s)); nothing was committed",
                               field_errors=dict(list(errs.items())[:50]))
    h = content_hash({"as_of": as_of, "currency": ccy, "market_type": market, "policy": policy, "wrong": sorted(wrong),
                      "lots": [_lot_canonical(l) for l in sorted(lots, key=lambda x: x["lot_id"])]})
    existing = session.get(ColSnapshot, (tenant_id, sid))
    if existing is not None:
        if existing.content_hash != h:
            raise Conflict(f"snapshot {sid} already exists with different content; a revised file needs a new snapshot_id",
                           code="SNAPSHOT_CONTENT_MISMATCH")
        return {**snapshot_out(existing), "replayed": True}
    snap = ColSnapshot(tenant_id=tenant_id, snapshot_id=sid, as_of=as_of, currency=ccy, market_type=market,
                       content_hash=h, policy=policy, wrong_way_issuer_groups=sorted(wrong), lot_count=len(lots),
                       source_ref=payload.get("source_ref"), created_by=actor_id)
    try:
        with session.begin_nested():  # concurrent first import of the same snapshot_id: loser re-reads instead of a 500
            session.add(snap)
            session.flush()
    except IntegrityError:
        existing = session.get(ColSnapshot, (tenant_id, sid), populate_existing=True)
        if existing is not None and existing.content_hash == h:
            return {**snapshot_out(existing), "replayed": True}
        raise Conflict(f"snapshot {sid} was created concurrently with different content", code="SNAPSHOT_CONTENT_MISMATCH") from None
    for l in lots:
        session.add(ColLot(
            tenant_id=tenant_id, snapshot_id=sid, lot_id=l["lot_id"], owner_entity=l["owner_entity"],
            custody_account=l["custody_account"], custodian=l.get("custodian"), issuer=l.get("issuer"),
            issuer_group=l.get("issuer_group"), issuer_class=l.get("issuer_class"), asset_class=l["asset_class"],
            currency=l["currency"], units_u=V.to_u(l["units"]), encumbered_u=V.to_u(l["encumbered_units"]),
            reserved_u=V.to_u(l["reserved_units"]), lot_size_u=V.to_u(l["lot_size"]), price=l.get("price"),
            fx_to_target=l.get("fx_to_target"), rating=l["rating"], maturity_date=l.get("maturity_date"),
            annual_cost_rate=l.get("annual_cost_rate"), txn_cost_per_unit=l["txn_cost_per_unit"],
            settlement_lag_days=l["settlement_lag_days"], reuse_permission=l["reuse_permission"],
            entitled_to=l["entitled_to"], posted_account_id=l.get("posted_account_id"),
            posted_u=V.to_u(l["posted_units"])))
    session.flush()
    _evidence(session, tenant_id, "COLLATERAL_INVENTORY_IMPORTED", actor_id=actor_id, role=role, entity_type=SNAPSHOT_TYPE,
              entity_id=sid, revision=1, correlation_id=correlation_id, effective_at=as_of, output_hash=h,
              payload={"lots": len(lots), "currency": ccy, "market_type": market}, input_hashes=[h])
    return {**snapshot_out(snap), "replayed": False}


def snapshot_out(s: ColSnapshot) -> dict:
    return {"snapshot_id": s.snapshot_id, "as_of": s.as_of.isoformat(), "currency": s.currency, "market_type": s.market_type,
            "content_hash": s.content_hash, "lot_count": s.lot_count, "provenance": s.provenance,
            "has_policy": bool((s.policy or {}).get("schedule")), "wrong_way_issuer_groups": s.wrong_way_issuer_groups}


# ------------------------------------------------------------------ context
def _get_snapshot(session, tenant_id: str, snapshot_id: str) -> ColSnapshot:
    s = session.get(ColSnapshot, (tenant_id, snapshot_id))
    if s is None:
        raise NotFound("Inventory snapshot not found")
    return s


def _lots(session, tenant_id: str, snapshot_id: str) -> list[ColLot]:
    return list(session.execute(select(ColLot).where(ColLot.tenant_id == tenant_id, ColLot.snapshot_id == snapshot_id)
                                .order_by(ColLot.lot_id).execution_options(populate_existing=True)).scalars())


def _tz(dt: datetime | str) -> datetime:
    if isinstance(dt, str):
        dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValidationFailed("deadline must be timezone-aware", field_errors={"deadline": "naive datetime"})
    return dt


# COVENANT (contracts) and ATLAS (inventory) name asset classes / cap denominators differently. Until CONTRACTS.md freezes one
# vocabulary, ATLAS maps ONLY these exact aliases (same economic meaning) and RECORDS each mapping as a visible warning.
# Anything else is left untouched and is rejected/excluded by validation (no silent coercion).
ASSET_CLASS_ALIASES = {"GOVERNMENT_BOND": "GSEC", "CORPORATE_BOND": "CORP"}
CAP_DENOM_ALIASES = {"TOTAL_EFFECTIVE_ALLOCATED": "TOTAL_ALLOCATED"}  # both: fraction of TOTAL effective allocated coverage


def _normalise_contract_schedule(schedule: list[dict]) -> tuple[list[dict], list[str]]:
    out, notes = [], []
    for e in schedule:
        e = dict(e)
        ac = e.get("asset_class")
        if ac in ASSET_CLASS_ALIASES:
            notes.append(f"VOCABULARY_NORMALISED: contract asset_class {ac} -> inventory class {ASSET_CLASS_ALIASES[ac]}")
            e["asset_class"] = ASSET_CLASS_ALIASES[ac]
        cd = e.get("cap_denominator")
        if cd in CAP_DENOM_ALIASES:
            notes.append(f"VOCABULARY_NORMALISED: contract cap_denominator {cd} -> {CAP_DENOM_ALIASES[cd]}")
            e["cap_denominator"] = CAP_DENOM_ALIASES[cd]
        out.append(e)
    return out, sorted(set(notes))


def _terms_source(session, tenant_id: str, ns_id: str, as_of: datetime, terms_hash: str | None) -> tuple[dict, str]:
    t = services.call("contracts.resolve_terms", session, tenant_id, ns_id, as_of, None)
    if t.get("status") != "RESOLVED" or not t.get("terms_hash"):
        raise Blocked("Contract terms are not resolved: " + "; ".join(t.get("blocking_reasons") or ["unknown"]),
                      code="TERMS_BLOCKED")
    if terms_hash and t["terms_hash"] != terms_hash:
        raise Blocked("Approved terms changed since the margin result was computed (stale terms hash)", code="STALE_TERMS")
    schedule, notes = _normalise_contract_schedule((t.get("terms") or {}).get("eligible_schedule") or [])
    src = {"source": "CONTRACT", "schedule": schedule, "vocab_notes": notes}
    if not src["schedule"]:
        raise Blocked("Contract terms contain no eligibility schedule", code="NO_ELIGIBILITY_SCHEDULE")
    return src, t["terms_hash"]


def _regulatory_source(session, tenant_id: str, rule_set_hash: str | None) -> dict | None:
    if not rule_set_hash:
        return None
    params = services.call("regulatory.rule_parameters", session, tenant_id, rule_set_hash)  # Unavailable => block
    p = (params or {}).get("parameters", params or {})
    classes = p.get("eligible_asset_classes")
    src: dict = {"source": "REGULATORY", "schedule": []}
    if classes:
        src["schedule"] = [{"asset_class": c, "valuation_pct": "100", "fx_haircut_pct": "0"} for c in sorted(classes)]
    if p.get("issuer_group_cap_pct") is not None:
        src["issuer_group_cap_pct"] = str(p["issuer_group_cap_pct"])
        src["issuer_cap_denominator"] = p.get("issuer_cap_denominator", "TOTAL_ALLOCATED")
    return src if (src["schedule"] or "issuer_group_cap_pct" in src) else None


def posted_coverage(session, tenant_id: str, account_id: str, as_of: datetime) -> dict:
    """Effective coverage already POSTED to `account_id` (never proposed/reserved collateral)."""
    as_of = _tz(as_of)
    snaps = list(session.execute(select(ColSnapshot).where(ColSnapshot.tenant_id == tenant_id, ColSnapshot.as_of <= as_of)
                                 .order_by(ColSnapshot.as_of.desc(), ColSnapshot.snapshot_id)).scalars())
    for s in snaps:
        rows = [l for l in _lots(session, tenant_id, s.snapshot_id) if l.posted_account_id == account_id and l.posted_u > 0]
        if not rows:
            continue
        sources = [{"source": "CLEARING_POLICY", **s.policy}] if (s.policy or {}).get("schedule") else []
        if not sources:
            raise Unavailable("posted collateral exists but the snapshot has no valuation policy", code="NO_VALUATION_POLICY")
        total, excluded, incl = Decimal(0), [], []
        for l in rows:
            a = assess_lot(l, obligation_id=account_id, target_ccy=s.currency, sources=sources,
                           wrong_way_groups=s.wrong_way_issuer_groups, as_of=s.as_of, deadline=s.as_of, horizon_days=1, mode="POSTED")
            if a.e is None or a.reasons:
                excluded.append({"lot_id": l.lot_id, "reasons": a.reasons})  # unknown value is NOT counted as zero silently
                continue
            cov = V.from_u(l.posted_u) * a.e
            total += cov
            incl.append({"lot_id": l.lot_id, "units": dstr(V.from_u(l.posted_u)), "effective_coverage": dstr(cov)})
        return {"eligible_posted": {"amount": dstr(total), "currency": s.currency}, "as_of": as_of.isoformat(),
                "snapshot_id": s.snapshot_id, "included": incl, "excluded": excluded,
                "basis": "posted units valued under the snapshot posting policy; includes SIMULATED settlements (labelled)"}
    raise Unavailable("No posted-collateral records for this account at the given as_of", code="NO_POSTED_COLLATERAL_DATA")


def _money_amount(v, field: str, expected_ccy: str | None) -> Decimal:
    """Upstream amounts arrive either as a bare decimal string or as a Money object {"amount","currency"} (VECTOR's shape).
    Currency, when present, must equal the result's currency: no implicit blending."""
    if isinstance(v, dict):
        ccy = v.get("currency")
        if ccy and expected_ccy and ccy != expected_ccy:
            raise NumericError(f"{field}: currency {ccy} differs from result currency {expected_ccy}")
        v = v.get("amount")
    return V.D(v, field)


def _context(session, tenant_id: str, margin_result_id: str, snapshot_id: str, rule_set_hash: str | None,
             terms_hash: str | None, deadline, horizon_days: int) -> dict:
    if not isinstance(horizon_days, int) or horizon_days <= 0:
        raise ValidationFailed("horizon_days must be a positive integer", field_errors={"horizon_days": "invalid"})
    deadline = _tz(deadline)
    snap = _get_snapshot(session, tenant_id, snapshot_id)
    m = services.call("risk.get_result", session, tenant_id, margin_result_id)
    if not m:
        raise NotFound("Margin result not found")
    market = m.get("market_type")
    if m.get("status") == "UNSUPPORTED" or market == "CCP_OTC":
        raise Blocked("Margin result is for an unsupported market (e.g. centrally cleared OTC); no allocation is produced",
                      code="UNSUPPORTED_MARKET")
    if m.get("status") != "OK":
        raise Blocked("Margin result is not usable: " + "; ".join(m.get("blocking_reasons") or [str(m.get("status"))]),
                      code="MARGIN_BLOCKED")
    if market not in MARKETS:
        raise Blocked(f"Unknown market type {market!r}", code="UNSUPPORTED_MARKET")
    if snap.market_type != market:
        raise Blocked(f"Inventory snapshot is for {snap.market_type}, margin result for {market}: markets are never blended",
                      code="MARKET_MISMATCH")
    if m.get("currency") != snap.currency:
        raise Blocked(f"Currency mismatch: margin {m.get('currency')} vs inventory pool {snap.currency}; no currency blending",
                      code="CURRENCY_MISMATCH")
    if m.get("terms_hash") and terms_hash and m["terms_hash"] != terms_hash:
        raise Blocked("terms_hash differs from the margin result's terms_hash", code="STALE_TERMS")
    if m.get("rule_set_hash") and rule_set_hash and m["rule_set_hash"] != rule_set_hash:
        raise Blocked("rule_set_hash differs from the margin result's rule_set_hash", code="STALE_RULES")
    obligation = m.get("netting_set_id") or m.get("account_id")
    if not obligation:
        raise Blocked("Margin result carries no netting_set_id/account_id", code="MARGIN_BLOCKED")
    warnings: list[str] = []
    sources: list[dict] = []
    used_terms = None
    if market == "OTC":
        src, used_terms = _terms_source(session, tenant_id, obligation, snap.as_of, terms_hash or m.get("terms_hash"))
        warnings.extend(src.pop("vocab_notes", []))
        sources.append(src)
    if (snap.policy or {}).get("schedule"):
        sources.append({"source": "CLEARING_POLICY", **snap.policy})
    elif market != "OTC":
        raise Blocked("ETD inventory snapshot has no clearing/posting policy schedule", code="NO_ELIGIBILITY_POLICY")
    reg = _regulatory_source(session, tenant_id, rule_set_hash or m.get("rule_set_hash"))
    if reg:
        sources.append(reg)
    elif not (rule_set_hash or m.get("rule_set_hash")):
        warnings.append("NO_REGULATORY_OVERLAY: no rule_set_hash supplied; regulatory eligibility constraints not applied")
    for s in sources:
        bad = validate_source(s)
        if bad:
            raise Blocked("Invalid eligibility schedule: " + "; ".join(bad[:3]), code="INVALID_ELIGIBILITY_SCHEDULE")
    # required effective coverage still to be posted
    posted = None
    try:
        if market == "OTC":
            amt = Decimal(0)
            if m.get("direction") == "POST":
                amt = _money_amount(m.get("rounded_amount") or m.get("required_amount") or "0", "rounded_amount", m.get("currency"))
            need = amt
        else:
            total = _money_amount(m.get("required_amount") or "0", "required_amount", m.get("currency"))
    except NumericError as exc:  # never a 500: an unparseable upstream amount blocks with a reason
        raise Blocked(f"Margin result amount is not usable: {exc}", code="MARGIN_BLOCKED") from exc
    if market != "OTC":
        try:
            posted = posted_coverage(session, tenant_id, obligation, snap.as_of)
            have = Decimal(posted["eligible_posted"]["amount"])
        except Unavailable:
            have = Decimal(0)
            warnings.append("NO_POSTED_COLLATERAL_RECORDS: treated as no collateral posted (explicitly recorded)")
        need = max(Decimal(0), total - have)
    if need < 0:
        raise Blocked("Negative required amount", code="MARGIN_BLOCKED")
    return {"margin": m, "snap": snap, "market": market, "obligation": obligation, "hypothetical": bool(m.get("hypothetical")), "currency": snap.currency,
            "sources": sources, "need": need, "posted": posted, "warnings": warnings, "terms_hash": used_terms or terms_hash,
            "rule_set_hash": rule_set_hash or m.get("rule_set_hash"), "deadline": deadline, "horizon_days": horizon_days,
            "as_of": snap.as_of}


def _candidates(rows: list[ColLot]) -> list[ColLot]:
    """Records that only describe collateral ALREADY POSTED (no free units) are not candidates and not 'exclusions'."""
    return [l for l in rows if not (l.units_u == 0 and l.posted_u > 0)]


def _raw_lot(l: ColLot, extra_avail_u: int = 0) -> dict:
    return {"lot_id": l.lot_id, "asset_class": l.asset_class, "currency": l.currency, "issuer_class": l.issuer_class,
            "issuer_group": l.issuer_group, "price": l.price, "fx_to_target": l.fx_to_target, "rating": l.rating,
            "maturity_date": l.maturity_date, "settlement_lag_days": l.settlement_lag_days, "entitled_to": l.entitled_to,
            "available": dstr(V.from_u(max(0, l.units_u - l.encumbered_u - l.reserved_u + extra_avail_u))),
            "lot_size": dstr(V.from_u(l.lot_size_u))}


def _avail_u(l: ColLot) -> int:
    return max(0, l.units_u - l.encumbered_u - l.reserved_u)


def _evaluate(ctx: dict, rows: list[ColLot], need: Decimal, stress: dict | None = None, time_limit: float = 10.0,
              availability: dict | None = None) -> dict:
    """`availability` ({lot_id: decimal-string}) pins the availability the optimisation saw, so a REPLAY after later
    reservations/settlements recomputes from the same persisted inputs instead of the current inventory."""
    rows = _candidates(rows)
    extra = {l.lot_id: (V.to_u(Decimal(availability[l.lot_id])) - _avail_u(l)) if availability and l.lot_id in availability else 0
             for l in rows}
    assessed = [assess_lot(l, obligation_id=ctx["obligation"], target_ccy=ctx["currency"], sources=ctx["sources"],
                           wrong_way_groups=ctx["snap"].wrong_way_issuer_groups, as_of=ctx["as_of"], deadline=ctx["deadline"],
                           horizon_days=ctx["horizon_days"], stress=stress, extra_available=extra[l.lot_id]) for l in rows]
    el = [a for a in assessed if a.eligible]
    exclusions = [{"lot_id": a.lot_id, "reasons": sorted(set(a.reasons))} for a in assessed if not a.eligible]
    params = {"sources": ctx["sources"], "wrong_way_issuer_groups": ctx["snap"].wrong_way_issuer_groups,
              "obligation_id": ctx["obligation"], "currency": ctx["currency"], "as_of": ctx["as_of"].isoformat(),
              "deadline": ctx["deadline"].isoformat(), "required": dstr(need), "overpost_allowance": "0",
              "stress": stress or None, "horizon_days": ctx["horizon_days"],
              "availability": {l.lot_id: dstr(V.from_u(max(0, _avail_u(l) + extra[l.lot_id]))) for l in rows}}
    raw = {l.lot_id: _raw_lot(l, extra[l.lot_id]) for l in rows}
    groups, meta = E.build_groups(el, ctx["sources"])
    res = E.solve_pipeline(el, need, groups, meta, raw, params, time_limit=time_limit)
    params["overpost_allowance"] = res.get("params_overpost_allowance", "0")
    res["exclusions"] = exclusions
    res["eligible_lots"] = len(el)
    res["assessed"] = assessed
    return {"res": res, "params": params, "el": el}


# ------------------------------------------------------------------ proposals
def _proposal_hash(p: dict) -> str:
    return content_hash(p)


def _business_hash(ctx: dict, need: Decimal, kind: str, stress: dict | None, legs: list[dict]) -> str:
    """Canonical business hash of a proposal: inputs' hashes + required coverage + horizon + deadline + stress + rounded legs.
    Excludes proposal ids, timestamps, audit sequence. Used for proposal_hash AND for replay comparison."""
    return _proposal_hash({"margin_result_hash": ctx["margin"].get("result_hash"), "inventory_hash": ctx["snap"].content_hash,
                           "terms_hash": ctx["terms_hash"], "rule_set_hash": ctx["rule_set_hash"], "required": need,
                           "horizon_days": ctx["horizon_days"], "deadline": ctx["deadline"], "kind": kind, "stress": stress,
                           "legs": [{k: l[k] for k in ("lot_id", "quantity", "effective_coverage", "cost_horizon")} for l in legs]})


def _persist(session, tenant_id: str, ctx: dict, ev: dict, *, kind: str, state: str, actor_id: str, correlation_id: str,
             need: Decimal, hypothetical: bool, now: datetime) -> ColProposal:
    res = ev["res"]
    pid = new_id()
    legs = res.get("legs", [])
    h = _business_hash(ctx, need, kind, ev["params"].get("stress"), legs)
    stored = {k: v for k, v in res.items() if k not in ("assessed", "legs", "constraints", "baseline")}
    p = ColProposal(
        proposal_id=pid, tenant_id=tenant_id, kind=kind, state=state, solver_status=res["solver_status"],
        hypothetical=hypothetical, market_type=ctx["market"], obligation_id=ctx["obligation"],
        margin_result_id=ctx["margin"]["result_id"], margin_result_hash=ctx["margin"].get("result_hash"),
        inventory_snapshot_id=ctx["snap"].snapshot_id, inventory_hash=ctx["snap"].content_hash,
        terms_hash=ctx["terms_hash"], rule_set_hash=ctx["rule_set_hash"], deadline=ctx["deadline"], as_of=ctx["as_of"],
        horizon_days=ctx["horizon_days"], currency=ctx["currency"], required_coverage=dstr(need),
        objective=res.get("objective"), residual=res.get("residual"), baseline=res.get("baseline") or {},
        result={**stored, "warnings": ctx["warnings"], "posted_coverage": ctx["posted"]}, params=ev["params"],
        proposal_hash=h, created_by=actor_id, correlation_id=correlation_id, created_at=now, updated_at=now,
        expires_at=now + timedelta(seconds=get_settings().approval_ttl_seconds))
    session.add(p)
    for l in legs:
        session.add(ColLeg(tenant_id=tenant_id, proposal_id=pid, **l))
    for c in res.get("constraints", []):
        session.add(ColConstraint(tenant_id=tenant_id, proposal_id=pid, name=c["name"], kind=c["kind"], limit_value=c["limit"],
                                  measured_value=c["measured"], slack=c["slack"], binding=c["binding"], unit=c["unit"]))
    session.flush()
    return p


def _parents(ctx: dict, inventory_snapshot_id: str) -> list[dict]:
    """Lineage edges in VERITAS' format {entity_type, entity_id}: margin result, inventory snapshot, rule set, terms."""
    out = [{"entity_type": "margin_result", "entity_id": ctx["margin"]["result_id"]},
           {"entity_type": SNAPSHOT_TYPE, "entity_id": inventory_snapshot_id}]
    if ctx.get("rule_set_hash"):
        out.append({"entity_type": "rule_set", "entity_id": ctx["rule_set_hash"]})
    if ctx.get("terms_hash"):
        out.append({"entity_type": "terms", "entity_id": ctx["terms_hash"]})
    return out


def _replay_spec(p: ColProposal, ctx: dict) -> dict:
    """payload.replay (CONTRACTS v1.1). business_hash == proposal_hash: it covers margin/inventory/terms/rule hashes, the
    required coverage, horizon, deadline, stress and the rounded legs - and excludes ids, timestamps and audit sequence."""
    return {"replayer": "replay.collateral", "engine_version": SERVICE_VERSION, "method_version": METHOD_VERSION,
            "business_hash": p.proposal_hash,
            "inputs": {"proposal_id": p.proposal_id, "kind": p.kind, "margin_result_id": p.margin_result_id,
                       "inventory_snapshot_id": p.inventory_snapshot_id, "rule_set_hash": p.rule_set_hash,
                       "terms_hash": p.terms_hash, "deadline": p.deadline.isoformat(), "horizon_days": p.horizon_days,
                       "required": p.required_coverage, "stress": (p.params or {}).get("stress"),
                       "availability": (p.params or {}).get("availability")},
            "values": {"objective": p.objective or "0", "residual": p.residual or "0"},
            "tolerance": LP_TOLERANCE}


def optimise(session, tenant_id: str, margin_result_id: str, inventory_snapshot_id: str, rule_set_hash: str | None,
             terms_hash: str | None, deadline, horizon_days: int, *, actor_id: str = "system", actor_type: str = "SERVICE",
             actor_role: str | None = None, correlation_id: str | None = None, now: datetime | None = None,
             time_limit: float = 10.0) -> dict:
    cid, now = correlation_id or new_id(), now or utcnow()
    ctx = _context(session, tenant_id, margin_result_id, inventory_snapshot_id, rule_set_hash, terms_hash, deadline, horizon_days)
    need = ctx["need"]
    rows = _lots(session, tenant_id, inventory_snapshot_id)
    hashes = [ctx["margin"].get("result_hash"), ctx["snap"].content_hash, ctx["terms_hash"], ctx["rule_set_hash"]]
    if need <= 0:  # G? zero call: no allocation, no draft, not approvable
        ev = {"res": {"solver_status": "NOT_REQUIRED", "legs": [], "constraints": [], "baseline": {"status": "NOT_COMPUTED"},
                      "objective": "0", "residual": "0", "exclusions": [], "solver_message": "no additional collateral required"},
              "params": {"required": "0", "stress": None, "sources": ctx["sources"], "currency": ctx["currency"],
                         "as_of": ctx["as_of"].isoformat(), "deadline": ctx["deadline"].isoformat()}}
        p = _persist(session, tenant_id, ctx, ev, kind="PROPOSAL", state="NOT_REQUIRED", actor_id=actor_id,
                     correlation_id=cid, need=need, hypothetical=ctx["hypothetical"], now=now)
    else:
        ev = _evaluate(ctx, rows, need, time_limit=time_limit)
        st = ev["res"]["solver_status"]
        state = {"infeasible": "INFEASIBLE", "timeout": "FAILED", "error": "FAILED"}.get(st, "PROPOSED")
        p = _persist(session, tenant_id, ctx, ev, kind="PROPOSAL", state=state, actor_id=actor_id, correlation_id=cid,
                     need=need, hypothetical=ctx["hypothetical"], now=now)
    _evidence(session, tenant_id, "COLLATERAL_PROPOSAL_CREATED", actor_id=actor_id, actor_type=actor_type, role=actor_role,
              entity_type=PROPOSAL_TYPE, entity_id=p.proposal_id, revision=p.revision, correlation_id=cid,
              effective_at=ctx["as_of"], input_hashes=hashes, output_hash=p.proposal_hash,
              payload={"state": p.state, "solver_status": p.solver_status, "required_coverage": p.required_coverage,
                       "objective": p.objective, "legs": [l["lot_id"] for l in ev["res"].get("legs", [])],
                       "market": ctx["market"], "obligation_id": ctx["obligation"],
                       "parents": _parents(ctx, inventory_snapshot_id),
                       "replay": _replay_spec(p, ctx)})
    emit(session, event_type="AllocationProposed", tenant_id=tenant_id, correlation_id=cid, aggregate_id=p.proposal_id,
         actor_id=actor_id, actor_type=actor_type, aggregate_revision=p.revision, effective_at=ctx["as_of"],
         payload={"proposal_hash": p.proposal_hash, "state": p.state, "solver_status": p.solver_status})
    if p.state == "PROPOSED":
        p = _verify_and_check(session, p, actor_id=actor_id, actor_type=actor_type, role=actor_role, now=now)
    return proposal_out(session, p)


def _legs(session, p: ColProposal) -> list[ColLeg]:
    return list(session.execute(select(ColLeg).where(ColLeg.proposal_id == p.proposal_id).order_by(ColLeg.lot_id)).scalars())


def _own_reserved(session, p: ColProposal) -> dict[str, int]:
    rs = session.execute(select(ColReservation).where(ColReservation.proposal_id == p.proposal_id,
                                                      ColReservation.status == "ACTIVE")).scalars()
    return {r.lot_pk: r.quantity_u for r in rs}


def _run_verify(session, p: ColProposal, *, own_reserved_counts: bool) -> dict:
    legs = _legs(session, p)
    own = _own_reserved(session, p) if own_reserved_counts else {}
    lots = {l.lot_id: l for l in _lots(session, p.tenant_id, p.inventory_snapshot_id)}
    raw = {lid: _raw_lot(l, own.get(l.id, 0)) for lid, l in lots.items()}
    return E.run_verifier([{"lot_id": l.lot_id, "quantity": l.quantity} for l in legs], raw, p.params)


def _verify_and_check(session, p: ColProposal, *, actor_id, actor_type="USER", role=None, now) -> ColProposal:
    ver = _run_verify(session, p, own_reserved_counts=p.state in ("RESERVED", "DRAFTED"))
    p.result = {**p.result, "verification": ver}
    if ver["ok"] and p.state == "PROPOSED":
        p.state, p.revision, p.updated_at = "CHECKED", p.revision + 1, now
        _evidence(session, p.tenant_id, "COLLATERAL_PROPOSAL_CHECKED", actor_id=actor_id, actor_type=actor_type, role=role,
                  entity_type=PROPOSAL_TYPE, entity_id=p.proposal_id, revision=p.revision, correlation_id=p.correlation_id,
                  payload={"verifier_ok": True, "checks": len(ver["checks"]), "coverage": ver["coverage"],
                           "parents": [{"entity_type": PROPOSAL_TYPE, "entity_id": p.proposal_id}]},
                  output_hash=p.proposal_hash)
    elif not ver["ok"]:
        _evidence(session, p.tenant_id, "COLLATERAL_VERIFICATION_FAILED", actor_id=actor_id, actor_type=actor_type, role=role,
                  entity_type=PROPOSAL_TYPE, entity_id=p.proposal_id, revision=p.revision, correlation_id=p.correlation_id,
                  payload={"failures": [f["name"] for f in ver["failures"]][:20]})
    session.flush()
    return p


def _release(session, p: ColProposal, now: datetime) -> None:
    for r in session.execute(select(ColReservation).where(ColReservation.proposal_id == p.proposal_id,
                                                          ColReservation.status == "ACTIVE")).scalars():
        session.execute(update(ColLot).where(ColLot.id == r.lot_pk).values(reserved_u=ColLot.reserved_u - r.quantity_u,
                                                                         version=ColLot.version + 1)
                        .execution_options(synchronize_session=False))
        r.status, r.released_at = "RELEASED", now


def _get(session, tenant_id: str, proposal_id: str, lock: bool = False) -> ColProposal:
    """lock=True: row lock + refresh BEFORE any revision/state check (lost-update safe on PostgreSQL; found by the PG run)."""
    if lock:
        q = lock_for_update(select(ColProposal).where(ColProposal.proposal_id == proposal_id, ColProposal.tenant_id == tenant_id)) \
            .execution_options(populate_existing=True)
        p = session.execute(q).scalar_one_or_none()
    else:
        p = session.get(ColProposal, proposal_id)
    if p is None or p.tenant_id != tenant_id:
        raise NotFound("Proposal not found")  # identical for absent and other-tenant
    return p


def _maybe_expire(session, p: ColProposal, now: datetime, actor_id: str = "system") -> ColProposal:
    if p.state in OPEN_STATES and now > p.expires_at:
        p = _get(session, p.tenant_id, p.proposal_id, lock=True)  # re-read under lock: another request may have settled it
        if not (p.state in OPEN_STATES and now > p.expires_at):
            return p
        _release(session, p, now)
        p.state, p.revision, p.updated_at = "EXPIRED", p.revision + 1, now
        _evidence(session, p.tenant_id, "COLLATERAL_PROPOSAL_EXPIRED", actor_id=actor_id, actor_type="SYSTEM",
                  entity_type=PROPOSAL_TYPE, entity_id=p.proposal_id, revision=p.revision,
                  correlation_id=p.correlation_id, payload={"expired_at": p.expires_at.isoformat()})
        session.flush()
    return p


def get_proposal(session, tenant_id: str, proposal_id: str, now: datetime | None = None) -> dict:
    p = _maybe_expire(session, _get(session, tenant_id, proposal_id), now or utcnow())
    return proposal_out(session, p)


def list_proposals(session, tenant_id: str, limit: int = 50, state: str | None = None) -> list[dict]:
    q = select(ColProposal).where(ColProposal.tenant_id == tenant_id)
    if state:
        q = q.where(ColProposal.state == state)
    rows = session.execute(q.order_by(ColProposal.created_at.desc(), ColProposal.proposal_id).limit(max(1, min(limit, 200)))).scalars()
    return [proposal_out(session, p, brief=True) for p in rows]


def proposal_out(session, p: ColProposal, brief: bool = False) -> dict:
    out = {
        "proposal_id": p.proposal_id, "kind": p.kind, "state": p.state, "solver_status": p.solver_status,
        "hypothetical": p.hypothetical, "market_type": p.market_type, "obligation_id": p.obligation_id,
        "currency": p.currency, "required_coverage": p.required_coverage, "objective": p.objective, "residual": p.residual,
        "revision": p.revision, "proposal_hash": p.proposal_hash, "margin_result_id": p.margin_result_id,
        "inventory_snapshot_id": p.inventory_snapshot_id, "created_at": p.created_at.isoformat(),
        "expires_at": p.expires_at.isoformat(), "approval_id": p.approval_id, "created_by": p.created_by,
        "horizon_days": p.horizon_days, "deadline": p.deadline.isoformat(),
        "transmission": "NOT_TRANSMITTED", "simulated_settlement": p.simulated_settlement,
        "correlation_id": p.correlation_id, "maker_id": p.created_by,
    }
    if brief:
        return out
    legs = _legs(session, p)
    cons = list(session.execute(select(ColConstraint).where(ColConstraint.proposal_id == p.proposal_id)
                                .order_by(ColConstraint.id)).scalars())
    out.update({
        "margin_result_hash": p.margin_result_hash, "inventory_hash": p.inventory_hash, "terms_hash": p.terms_hash,
        "rule_set_hash": p.rule_set_hash, "as_of": p.as_of.isoformat(), "baseline": p.baseline, "result": p.result,
        "legs": [{c: getattr(l, c) for c in ("lot_id", "asset_class", "currency", "quantity", "price", "fx_rate", "gross_value",
                                              "valuation_factor", "fx_factor", "effective_coverage", "cost_horizon",
                                              "share_of_coverage", "settlement_lag_days")} for l in legs],
        "constraints": [{"name": c.name, "kind": c.kind, "limit": c.limit_value, "measured": c.measured_value, "slack": c.slack,
                         "binding": c.binding, "unit": c.unit} for c in cons],
        "cost_units": "target-currency horizon cost (not annualised); effective coverage in target currency",
    })
    return out


def verify(session, tenant_id: str, proposal_id: str, *, actor_id: str = "system", actor_type: str = "SERVICE",
           actor_role: str | None = None, now: datetime | None = None) -> dict:
    now = now or utcnow()
    p = _maybe_expire(session, _get(session, tenant_id, proposal_id, lock=True), now)
    if p.state in ("INFEASIBLE", "NOT_REQUIRED", "FAILED"):
        return {"proposal_id": p.proposal_id, "ok": False, "state": p.state, "failures": [{"name": f"state {p.state}: nothing to verify"}]}
    state_before = p.state
    p = _verify_and_check(session, p, actor_id=actor_id, actor_type=actor_type, role=actor_role, now=now)
    ver = p.result["verification"]
    if not ver["ok"] and state_before in ("CHECKED", "APPROVED"):
        # approval/check is invalidated: inputs (inventory availability) no longer satisfy the constraints
        _release(session, p, now)
        p.state, p.revision, p.updated_at = "EXPIRED", p.revision + 1, now
        p.result = {**p.result, "expiry_reason": "INVALIDATED: verification failed against current inventory"}
        session.flush()
    return {"proposal_id": p.proposal_id, "ok": ver["ok"], "state": p.state, "checks": ver["checks"], "failures": ver["failures"],
            "coverage": ver["coverage"], "required": ver["required"], "revision": p.revision}


def _check_rev(p: ColProposal, expected_revision: int | None):
    if expected_revision is not None and expected_revision != p.revision:
        raise Conflict(f"Proposal revision is {p.revision}, request expected {expected_revision}; reload", code="REVISION_CONFLICT")


def approve_proposal(session, tenant_id: str, principal: Principal, proposal_id: str, *, reason: str,
                     expected_revision: int | None = None, now: datetime | None = None) -> dict:
    now = now or utcnow()
    if not reason or not reason.strip():
        raise ValidationFailed("A reason is mandatory", field_errors={"reason": "required"})
    p = _maybe_expire(session, _get(session, tenant_id, proposal_id, lock=True), now)
    _check_rev(p, expected_revision)
    if p.kind != "PROPOSAL" or p.hypothetical:
        raise Forbidden("Stress runs and proposals derived from a HYPOTHETICAL margin result can never be approved or reserved",
                        code="NOT_APPROVABLE")
    if p.state != "CHECKED":
        raise Conflict(f"Only a CHECKED proposal can be approved (state is {p.state})", code="ILLEGAL_TRANSITION")
    require_distinct_checker(p.created_by, principal)
    ver = _run_verify(session, p, own_reserved_counts=False)
    if not ver["ok"]:
        raise Conflict("Independent verifier failed; approval blocked: " + "; ".join(f["name"] for f in ver["failures"][:3]),
                       code="VERIFICATION_FAILED")
    p.state, p.revision, p.updated_at = "APPROVED", p.revision + 1, now
    ap = ApprovalRow(tenant_id=tenant_id, action_type="COLLATERAL_ALLOCATION_APPROVAL", target_type="col_proposal",
                     target_id=p.proposal_id, target_revision=p.revision, target_hash=p.proposal_hash, maker_id=p.created_by,
                     checker_id=principal.user_id, decision="APPROVED", reason=reason.strip(), decided_at=now,
                     expires_at=min(p.expires_at, now + timedelta(seconds=get_settings().approval_ttl_seconds)))
    session.add(ap)
    session.flush()
    p.approval_id = ap.approval_id
    _evidence(session, tenant_id, "COLLATERAL_PROPOSAL_APPROVED", actor_id=principal.user_id, role=",".join(principal.roles),
              entity_type=PROPOSAL_TYPE, entity_id=p.proposal_id, revision=p.revision, correlation_id=p.correlation_id,
              rationale=reason.strip(), input_hashes=[p.proposal_hash], output_hash=p.proposal_hash,
              payload={"approval_id": ap.approval_id, "maker": p.created_by, "checker": principal.user_id,
                       "target_hash": p.proposal_hash, "expires_at": ap.expires_at.isoformat()})
    _evidence(session, tenant_id, "COLLATERAL_APPROVAL_RECORDED", actor_id=principal.user_id, role=",".join(principal.roles),
              entity_type="approval", entity_id=ap.approval_id, revision=1, correlation_id=p.correlation_id,
              rationale=reason.strip(), input_hashes=[p.proposal_hash], output_hash=p.proposal_hash,
              payload={"decision": "APPROVED", "target_type": PROPOSAL_TYPE, "target_id": p.proposal_id,
                       "target_hash": p.proposal_hash, "maker": p.created_by, "checker": principal.user_id,
                       "parents": [{"entity_type": PROPOSAL_TYPE, "entity_id": p.proposal_id}]})
    session.flush()
    return {**proposal_out(session, p), "approval": {"approval_id": ap.approval_id, "decision": ap.decision,
                                                     "target_hash": ap.target_hash, "expires_at": ap.expires_at.isoformat()}}


def _reserve_lot_units(session, tenant_id: str, lot_pk: str, q: int) -> bool:
    """THE double-pledge guard: a single conditional UPDATE. The availability predicate is evaluated inside the statement
    (re-evaluated against the committed row after a lock wait on PostgreSQL), so two reservations can never both win the
    same units. Returns False (rowcount 0) when fewer than q units are free."""
    res = session.execute(update(ColLot).where(ColLot.id == lot_pk, ColLot.tenant_id == tenant_id,
                                              ColLot.units_u - ColLot.encumbered_u - ColLot.reserved_u >= q)
                          .values(reserved_u=ColLot.reserved_u + q, version=ColLot.version + 1)
                          .execution_options(synchronize_session=False))
    return res.rowcount == 1


def reserve(session, tenant_id: str, proposal_id: str, approval_id: str, *, actor_id: str = "system",
            actor_type: str = "SERVICE", actor_role: str | None = None, expected_revision: int | None = None,
            now: datetime | None = None) -> dict:
    now = now or utcnow()
    p = _maybe_expire(session, _get(session, tenant_id, proposal_id, lock=True), now)
    if p.state == "RESERVED" and p.approval_id == approval_id:
        return proposal_out(session, p)  # idempotent retry
    _check_rev(p, expected_revision)
    if p.state == "EXPIRED":
        raise Conflict("Proposal expired or was invalidated; its approval is void", code="STALE_APPROVAL")
    if p.state != "APPROVED":
        raise Conflict(f"Only an APPROVED proposal can be reserved (state is {p.state})", code="ILLEGAL_TRANSITION")
    ap = session.get(ApprovalRow, approval_id)
    if ap is None or ap.tenant_id != tenant_id or ap.target_id != p.proposal_id:
        raise NotFound("Approval not found")
    if ap.decision != "APPROVED" or ap.target_hash != p.proposal_hash or ap.target_revision != p.revision:
        raise Conflict("Approval no longer matches the proposal (hash/revision changed)", code="STALE_APPROVAL")
    if now > ap.expires_at:
        raise Conflict("Approval has expired", code="APPROVAL_EXPIRED")
    # upstream inputs must still be what the approval bound (fail closed if the upstream service is unavailable)
    m = services.call("risk.get_result", session, tenant_id, p.margin_result_id)
    if not m or (p.margin_result_hash and m.get("result_hash") != p.margin_result_hash):
        raise Conflict("Margin result changed since approval", code="STALE_APPROVAL")
    if p.market_type == "OTC":
        t = services.call("contracts.resolve_terms", session, tenant_id, p.obligation_id, p.as_of, None)
        if t.get("status") != "RESOLVED" or t.get("terms_hash") != p.terms_hash:
            raise Conflict("Contract terms changed since approval", code="STALE_APPROVAL")
    snap = _get_snapshot(session, tenant_id, p.inventory_snapshot_id)
    if snap.content_hash != p.inventory_hash:
        raise Conflict("Inventory snapshot changed since approval", code="STALE_APPROVAL")
    ver = _run_verify(session, p, own_reserved_counts=False)
    if not ver["ok"]:
        raise Conflict("Current inventory no longer satisfies the constraints: " + "; ".join(f["name"] for f in ver["failures"][:3]),
                       code="STALE_INVENTORY")
    # ---- atomic reservation: lock, then conditional UPDATE (availability re-checked inside the statement)
    lots = {l.lot_id: l for l in _lots(session, tenant_id, p.inventory_snapshot_id)}
    # Savepoint: if ANY leg fails, every earlier leg's UPDATE is rolled back (all-or-nothing) even if the caller keeps the session.
    # Legs are processed in lot_id order => consistent lock order across concurrent proposals (no deadlock cycles).
    with session.begin_nested():
        for leg in _legs(session, p):
            lot = lots[leg.lot_id]
            session.execute(lock_for_update(select(ColLot).where(ColLot.id == lot.id)))
            q = V.to_u(Decimal(leg.quantity))
            if not _reserve_lot_units(session, tenant_id, lot.id, q):
                raise Conflict(f"Lot {leg.lot_id} no longer has {leg.quantity} available (concurrent reservation); nothing reserved",
                               code="RESERVATION_CONFLICT")
            session.add(ColReservation(tenant_id=tenant_id, proposal_id=p.proposal_id, lot_pk=lot.id, lot_id=leg.lot_id, quantity_u=q))
    p.state, p.revision, p.updated_at = "RESERVED", p.revision + 1, now
    session.flush()
    _evidence(session, tenant_id, "COLLATERAL_RESERVED", actor_id=actor_id, actor_type=actor_type, role=actor_role,
              entity_type=PROPOSAL_TYPE, entity_id=p.proposal_id, revision=p.revision, correlation_id=p.correlation_id,
              input_hashes=[p.proposal_hash], payload={"approval_id": approval_id, "legs": [l.lot_id for l in _legs(session, p)],
                                                       "status": "RESERVED_NOT_POSTED"})
    emit(session, event_type="ReservationCreated", tenant_id=tenant_id, correlation_id=p.correlation_id,
         aggregate_id=p.proposal_id, actor_id=actor_id, actor_type=actor_type, aggregate_revision=p.revision,
         payload={"proposal_hash": p.proposal_hash})
    return proposal_out(session, p)


def cancel(session, tenant_id: str, proposal_id: str, *, actor_id: str, actor_role: str | None = None, reason: str = "",
           expected_revision: int | None = None, now: datetime | None = None) -> dict:
    now = now or utcnow()
    p = _get(session, tenant_id, proposal_id, lock=True)
    if p.state == "CANCELLED":
        return proposal_out(session, p)  # idempotent
    _check_rev(p, expected_revision)
    if p.state not in OPEN_STATES + ("DRAFTED",) or p.simulated_settlement:
        raise Conflict(f"Cannot cancel a proposal in state {p.state}", code="ILLEGAL_TRANSITION")
    _release(session, p, now)
    p.state, p.revision, p.updated_at = "CANCELLED", p.revision + 1, now
    session.flush()
    _evidence(session, tenant_id, "COLLATERAL_PROPOSAL_CANCELLED", actor_id=actor_id, role=actor_role, entity_type=PROPOSAL_TYPE,
              entity_id=p.proposal_id, revision=p.revision, correlation_id=p.correlation_id, rationale=reason or None,
              payload={"reservations_released": True})
    return proposal_out(session, p)


def draft_instruction(session, tenant_id: str, proposal_id: str, *, actor_id: str, actor_role: str | None = None,
                      expected_revision: int | None = None, now: datetime | None = None) -> dict:
    now = now or utcnow()
    p = _maybe_expire(session, _get(session, tenant_id, proposal_id, lock=True), now)
    if p.state == "DRAFTED":
        return get_instruction(session, tenant_id, proposal_id)
    _check_rev(p, expected_revision)
    if p.state != "RESERVED" or p.solver_status not in ("optimal", "feasible"):
        raise Conflict(f"An instruction can only be drafted for a RESERVED, feasible proposal (state is {p.state})",
                       code="ILLEGAL_TRANSITION")
    ap = session.get(ApprovalRow, p.approval_id)
    legs = _legs(session, p)
    content = {
        "status": "NOT_TRANSMITTED", "watermark": WATERMARK, "account_or_netting_set": p.obligation_id,
        "market_type": p.market_type, "currency": p.currency, "required_coverage": p.required_coverage,
        "legs": [{c: getattr(l, c) for c in ("lot_id", "asset_class", "currency", "quantity", "effective_coverage")} for l in legs],
        "dates": {"as_of": p.as_of.isoformat(), "deadline": p.deadline.isoformat()},
        "source_hashes": {"proposal": p.proposal_hash, "margin_result": p.margin_result_hash, "inventory": p.inventory_hash,
                          "terms": p.terms_hash, "rule_set": p.rule_set_hash},
        "approval": {"approval_id": ap.approval_id, "maker": ap.maker_id, "checker": ap.checker_id,
                     "decision": ap.decision, "target_hash": ap.target_hash},
        "iso20022_like_sample": {
            "note": "ILLUSTRATIVE ONLY: not a validated ISO 20022 message; no schema validation was performed; never sent.",
            "MsgType": "colr.003-like (collateral substitution/instruction sample)", "Transmission": "NOT_TRANSMITTED",
            "Pledges": [{"LotId": l.lot_id, "Qty": l.quantity, "Ccy": l.currency} for l in legs]},
        "disclaimer": "Internal decision-support draft. No instruction was sent to any custodian, CCP or settlement system."}
    h = content_hash(content)
    session.add(ColInstructionDraft(proposal_id=p.proposal_id, tenant_id=tenant_id, content=content, content_hash=h, created_by=actor_id))
    p.state, p.revision, p.updated_at = "DRAFTED", p.revision + 1, now
    session.flush()
    _evidence(session, tenant_id, "COLLATERAL_INSTRUCTION_DRAFTED", actor_id=actor_id, role=actor_role, entity_type=PROPOSAL_TYPE,
              entity_id=p.proposal_id, revision=p.revision, correlation_id=p.correlation_id, output_hash=h,
              input_hashes=[p.proposal_hash], payload={"transmission": "NOT_TRANSMITTED"})
    emit(session, event_type="InstructionDrafted", tenant_id=tenant_id, correlation_id=p.correlation_id, aggregate_id=p.proposal_id,
         actor_id=actor_id, aggregate_revision=p.revision, payload={"content_hash": h, "transmission": "NOT_TRANSMITTED"})
    return get_instruction(session, tenant_id, proposal_id)


def get_instruction(session, tenant_id: str, proposal_id: str) -> dict:
    _get(session, tenant_id, proposal_id)
    d = session.get(ColInstructionDraft, proposal_id)
    if d is None or d.tenant_id != tenant_id:
        raise NotFound("No instruction draft exists for this proposal")
    return {"proposal_id": proposal_id, "status": d.status, "content_hash": d.content_hash, "content": d.content,
            "created_at": d.created_at.isoformat()}


def simulate_settlement(session, tenant_id: str, proposal_id: str, *, actor_id: str, actor_role: str | None = None,
                        expected_revision: int | None = None, now: datetime | None = None) -> dict:
    """Records a FAKE confirmed movement for the demo flow only. Clearly labelled SIMULATED; no external effect."""
    now = now or utcnow()
    p = _get(session, tenant_id, proposal_id, lock=True)
    _check_rev(p, expected_revision)
    if p.state != "DRAFTED" or p.simulated_settlement:
        raise Conflict("Simulated settlement requires a DRAFTED proposal not yet settled", code="ILLEGAL_TRANSITION")
    for r in session.execute(select(ColReservation).where(ColReservation.proposal_id == p.proposal_id,
                                                          ColReservation.status == "ACTIVE")).scalars():
        lot = session.get(ColLot, r.lot_pk)
        if lot.posted_account_id not in (None, p.obligation_id) and lot.posted_u > 0:
            raise Conflict("Lot already posted to a different account", code="POSTING_CONFLICT")
        session.execute(update(ColLot).where(ColLot.id == lot.id).values(
            units_u=ColLot.units_u - r.quantity_u, reserved_u=ColLot.reserved_u - r.quantity_u,
            posted_u=ColLot.posted_u + r.quantity_u, posted_account_id=p.obligation_id, version=ColLot.version + 1)
            .execution_options(synchronize_session=False))
        r.status = "SETTLED_SIM"
    p.simulated_settlement = {"status": "SIMULATED_SETTLED", "label": "SIMULATED - no real movement occurred", "at": now.isoformat()}
    p.revision, p.updated_at = p.revision + 1, now
    session.flush()
    _evidence(session, tenant_id, "COLLATERAL_SIMULATED_SETTLED", actor_id=actor_id, role=actor_role, entity_type=PROPOSAL_TYPE,
              entity_id=p.proposal_id, revision=p.revision, correlation_id=p.correlation_id,
              payload={"label": "SIMULATED", "transmission": "NOT_TRANSMITTED"})
    return proposal_out(session, p)


# ------------------------------------------------------------------ stress / desk
def stress(session, tenant_id: str, margin_result_id: str, inventory_snapshot_id: str, rule_set_hash: str | None,
           terms_hash: str | None, deadline, horizon_days: int, params: dict, *, actor_id: str, actor_role: str | None = None,
           correlation_id: str | None = None, now: datetime | None = None) -> dict:
    """Hypothetical liquidity stress: haircut add-on, unavailable lots, funding-cost shock, optional required-coverage
    override. Recomputed through the same pipeline; persisted as a STRESS record that can NEVER be approved/reserved."""
    now, cid = now or utcnow(), correlation_id or new_id()
    allowed = {"haircut_add_pct", "unavailable_lot_ids", "cost_add_bps", "required_coverage_override"}
    bad = set(params) - allowed
    if bad:
        raise ValidationFailed("Unknown stress parameter(s)", field_errors={k: "not allowed" for k in sorted(bad)})
    st: dict = {}
    try:
        if params.get("haircut_add_pct") is not None:
            v = V.D(params["haircut_add_pct"], "haircut_add_pct")
            if not (0 <= v <= 100):
                raise NumericError("haircut_add_pct must be within 0..100")
            st["haircut_add_pct"] = dstr(v)
        if params.get("cost_add_bps") is not None:
            st["cost_add_bps"] = dstr(V.D(params["cost_add_bps"], "cost_add_bps"))
        override = None
        if params.get("required_coverage_override") is not None:
            override = V.D(params["required_coverage_override"], "required_coverage_override")
            if override < 0:
                raise NumericError("required_coverage_override must be >= 0")
            st["required_coverage_override"] = dstr(override)
    except NumericError as exc:
        raise ValidationFailed(str(exc), field_errors={"params": str(exc)}) from exc
    if params.get("unavailable_lot_ids"):
        st["unavailable_lot_ids"] = sorted(str(x) for x in params["unavailable_lot_ids"])
    ctx = _context(session, tenant_id, margin_result_id, inventory_snapshot_id, rule_set_hash, terms_hash, deadline, horizon_days)
    rows = _lots(session, tenant_id, inventory_snapshot_id)
    need = ctx["need"]
    base = _evaluate(ctx, rows, need) if need > 0 else None
    need_s = Decimal(st["required_coverage_override"]) if "required_coverage_override" in st else need
    if need_s <= 0:
        ev = {"res": {"solver_status": "NOT_REQUIRED", "legs": [], "constraints": [], "baseline": {"status": "NOT_COMPUTED"},
                      "objective": "0", "residual": "0", "exclusions": []}, "params": {**(base or {"params": {}})["params"], "stress": st}}
    else:
        ev = _evaluate(ctx, rows, need_s, stress=st)
    state = "HYPOTHETICAL"
    p = _persist(session, tenant_id, ctx, ev, kind="STRESS", state=state, actor_id=actor_id, correlation_id=cid, need=need_s,
                 hypothetical=True, now=now)
    _evidence(session, tenant_id, "COLLATERAL_STRESS_RUN", actor_id=actor_id, role=actor_role, entity_type=PROPOSAL_TYPE,
              entity_id=p.proposal_id, revision=p.revision, correlation_id=cid, effective_at=ctx["as_of"],
              input_hashes=[ctx["snap"].content_hash, ctx["margin"].get("result_hash")],
              payload={"hypothetical": True, "stress": st, "solver_status": p.solver_status,
                       "parents": _parents(ctx, inventory_snapshot_id), "replay": _replay_spec(p, ctx)})
    out = proposal_out(session, p)
    summary = None
    if base is not None:
        b = base["res"]
        summary = {"solver_status": b["solver_status"], "objective": b.get("objective"), "residual": b.get("residual"),
                   "coverage": b.get("coverage"), "eligible_lots": b.get("eligible_lots")}
    delta = None
    if summary and summary["objective"] is not None and out["objective"] is not None:
        delta = dstr(Decimal(out["objective"]) - Decimal(summary["objective"]))
    return {"hypothetical": True, "label": "HYPOTHETICAL STRESS - nothing was reserved, approved or moved", "stress": st,
            "base": summary, "stressed": out, "objective_delta": delta}


def desk(session, tenant_id: str, margin_result_id: str, inventory_snapshot_id: str, rule_set_hash: str | None,
         terms_hash: str | None, deadline, horizon_days: int) -> dict:
    ctx = _context(session, tenant_id, margin_result_id, inventory_snapshot_id, rule_set_hash, terms_hash, deadline, horizon_days)
    rows = _candidates(_lots(session, tenant_id, inventory_snapshot_id))
    assessed = [assess_lot(l, obligation_id=ctx["obligation"], target_ccy=ctx["currency"], sources=ctx["sources"],
                           wrong_way_groups=ctx["snap"].wrong_way_issuer_groups, as_of=ctx["as_of"], deadline=ctx["deadline"],
                           horizon_days=horizon_days) for l in rows]
    el = [a for a in assessed if a.eligible]
    by_cls: dict[tuple[str, str], dict] = {}
    total = Decimal(0)
    for a in el:
        cov = a.available * a.e
        total += cov
        d = by_cls.setdefault((a.asset_class, a.currency), {"asset_class": a.asset_class, "currency": a.currency,
                                                            "lots": 0, "available_units": Decimal(0), "effective_coverage": Decimal(0)})
        d["lots"] += 1
        d["available_units"] += a.available
        d["effective_coverage"] += cov
    late = [a for a in assessed if a.reasons == ["SETTLEMENT_AFTER_DEADLINE"]]
    reasons: dict[str, int] = {}
    for a in assessed:
        for r in set(a.reasons):
            key = r.split(":")[0] if r.startswith("NO_SCHEDULE_MATCH") else r
            reasons[key] = reasons.get(key, 0) + 1
    need = ctx["need"]
    achievable = None
    if el and need > 0:
        groups, _ = E.build_groups(el, ctx["sources"])
        mc = O.max_coverage([O.Item(a.lot_id, float(a.e), float(a.cost_unit), float(a.available), float(a.lot_size)) for a in el],
                            float(need), groups)
        achievable = mc.get("achievable")
    active = session.execute(select(func.count(), func.coalesce(func.sum(ColReservation.quantity_u), 0)).where(
        ColReservation.tenant_id == tenant_id, ColReservation.status == "ACTIVE")).one()
    cash_cov = sum((a.available * a.e for a in el if a.asset_class == "CASH"), Decimal(0))
    return {
        "currency": ctx["currency"], "market_type": ctx["market"], "obligation_id": ctx["obligation"],
        "required_coverage_still_needed": dstr(need), "posted_coverage": ctx["posted"],
        "eligible_inventory": [{**{k: (dstr(v) if isinstance(v, Decimal) else v) for k, v in d.items()}}
                               for _, d in sorted(by_cls.items())],
        "eligible_effective_coverage_by_deadline": dstr(total),
        "achievable_under_caps": None if achievable is None else dstr(V.q6(Decimal(repr(achievable)))),
        "shortfall_vs_eligible": dstr(max(Decimal(0), need - total)),
        "currency_shortfall": {ctx["currency"]: dstr(max(Decimal(0), need - total))},
        "cash_buffer_effective": dstr(cash_cov),
        "late_settling_lots": [{"lot_id": a.lot_id, "settlement_lag_days": a.settlement_lag_days} for a in late],
        "exclusions_by_reason": dict(sorted(reasons.items())),
        "excluded_lots": [{"lot_id": a.lot_id, "reasons": sorted(set(a.reasons))} for a in assessed if not a.eligible],
        "active_reservations": {"count": active[0], "units": dstr(V.from_u(int(active[1])))},
        "warnings": ctx["warnings"], "snapshot": snapshot_out(ctx["snap"]), "deadline": ctx["deadline"].isoformat(),
        "horizon_days": horizon_days,
    }


def list_snapshots(session, tenant_id: str) -> list[dict]:
    return [snapshot_out(s) for s in session.execute(select(ColSnapshot).where(ColSnapshot.tenant_id == tenant_id)
                                                    .order_by(ColSnapshot.as_of.desc(), ColSnapshot.snapshot_id)).scalars()]


def snapshot_lots(session, tenant_id: str, snapshot_id: str) -> dict:
    s = _get_snapshot(session, tenant_id, snapshot_id)
    return {"snapshot": snapshot_out(s), "lots": [{
        "lot_id": l.lot_id, "asset_class": l.asset_class, "currency": l.currency, "issuer_group": l.issuer_group,
        "units": dstr(V.from_u(l.units_u)), "encumbered": dstr(V.from_u(l.encumbered_u)), "reserved": dstr(V.from_u(l.reserved_u)),
        "available": dstr(available_units(l)), "price": l.price, "rating": l.rating, "settlement_lag_days": l.settlement_lag_days,
        "posted_units": dstr(V.from_u(l.posted_u)), "posted_account_id": l.posted_account_id, "entitled_to": l.entitled_to}
        for l in _lots(session, tenant_id, snapshot_id)]}


# ------------------------------------------------------------------ contract registration (frozen names)
def _svc_optimise(session, tenant_id, margin_result_id, inventory_snapshot_id, rule_set_hash, terms_hash, deadline, horizon_days, **kw):
    return optimise(session, tenant_id, margin_result_id, inventory_snapshot_id, rule_set_hash, terms_hash, deadline, horizon_days, **kw)


def replay_collateral(session, tenant_id: str, spec: dict) -> dict:
    """replay.collateral (CONTRACTS v1.1): recompute a stored proposal from its PERSISTED inputs (margin result, inventory
    snapshot rows with the availability the original run saw, terms/rule-set hashes, deadline, horizon, stress, required
    coverage). Pure read: nothing is persisted; VERITAS additionally wraps this in a savepoint that is always rolled back.
    Raises (-> NOT_REPLAYABLE) if an upstream input changed (e.g. terms hash) or is unavailable."""
    inp = spec.get("inputs") or {}
    ctx = _context(session, tenant_id, inp["margin_result_id"], inp["inventory_snapshot_id"], inp.get("rule_set_hash"),
                   inp.get("terms_hash"), inp["deadline"], int(inp["horizon_days"]))
    need = Decimal(inp["required"])
    kind, stress = inp.get("kind", "PROPOSAL"), inp.get("stress")
    if need <= 0:
        res = {"solver_status": "NOT_REQUIRED", "legs": [], "objective": "0", "residual": "0"}
    else:
        res = _evaluate(ctx, _lots(session, tenant_id, inp["inventory_snapshot_id"]), need, stress=stress,
                        availability=inp.get("availability"))["res"]
    return {"engine_version": SERVICE_VERSION, "method_version": METHOD_VERSION,
            "business_hash": _business_hash(ctx, need, kind, stress, res.get("legs", [])),
            "values": {"objective": res.get("objective") or "0", "residual": res.get("residual") or "0"}}


def constraint_slacks(session, tenant_id: str, proposal_id: str) -> list[dict]:
    """collateral.constraint_slacks (CONTRACTS v1.1): per-constraint {name, limit, used, slack, binding} (+kind, unit) of a stored
    proposal. `used` is the measured value. Dual values are NOT included (valid only for continuous LP; see proposal result)."""
    _get(session, tenant_id, proposal_id)
    rows = session.execute(select(ColConstraint).where(ColConstraint.tenant_id == tenant_id, ColConstraint.proposal_id == proposal_id)
                           .order_by(ColConstraint.id)).scalars()
    return [{"name": c.name, "kind": c.kind, "limit": c.limit_value, "used": c.measured_value, "slack": c.slack,
             "binding": c.binding, "unit": c.unit} for c in rows]


services.register("replay.collateral", replay_collateral)
services.register("collateral.constraint_slacks", constraint_slacks)
services.register("collateral.optimise", _svc_optimise)
services.register("collateral.verify", verify)
services.register("collateral.reserve", reserve)
services.register("collateral.coverage", posted_coverage)
