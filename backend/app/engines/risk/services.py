"""A3 VECTOR services. Sole authoritative producer of financial margin results.

Public (registered) service names are frozen in docs/agent-handoffs/CONTRACTS.md:
  risk.compute_margin, risk.get_result, risk.reconcile_clearing, risk.stress
Additional (additive) services used by this engine's own API: pin_snapshot, import_portfolio, import_clearing_file.

Transaction model: every function uses the CALLER's session and never commits. Evidence is appended in the same
transaction, so if evidence is unavailable the whole consequential write rolls back (G24).
Results are immutable. BLOCKED results are stored (audit) but never served from the cache.
"""
from __future__ import annotations

import contextvars
import hashlib
from datetime import date, datetime, timezone
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core import services
from app.core.canonical import CanonicalError, canonical_bytes, content_hash
from app.core.config import get_settings
from app.core.errors import Conflict, NotFound, Unavailable, ValidationFailed
from app.core.events import emit
from app.core.money import NumericError, dstr, to_decimal
from app.db.base import new_id
from app.engines.risk import clearing as clr
from app.engines.risk import etd
from app.engines.risk import margin as mg
from app.engines.risk.models import (RiskClearingFile, RiskInstrument, RiskPosition, RiskResult, RiskSnapshot,
                                     RiskStressRun, RiskTrade)
from app.engines.risk.pricing import (METHOD_VERSION, PREC, MarketData, PricingError, residual_years, schedule,
                                      shock_payload, validate_snapshot_payload, value_fxf, value_irs)

OTC, ETD_DOM, ETD_IFSC, CCP = "OTC", "ETD_DOMESTIC", "ETD_IFSC", "CCP_OTC"
PROFILES = {ETD_DOM: ("DEMO_NSE", "INR"), ETD_IFSC: ("DEMO_IFSC", "USD")}  # independent policy profiles (G20)
METHODS = {"OTC_VM_SIMPLE_V1": {OTC}, "ETD_IMPORTED_V1": {ETD_DOM, ETD_IFSC}}
Q8 = Decimal("0.00000001")
Q2 = Decimal("0.01")
ACTOR_KEYS = ("actor_id", "actor_role", "correlation_id", "source_mode")
ENGINE_VERSION = f"aether-risk/{METHOD_VERSION}"
# Read-only replay pins (set only by replay_risk): bypass the result cache and pin the exact persisted inputs used originally.
_REPLAY: contextvars.ContextVar[dict | None] = contextvars.ContextVar("risk_replay", default=None)
# Fields excluded from the canonical BUSINESS hash: run identity, and float-based internal analytics (compared with a tolerance instead).
BUSINESS_HASH_EXCLUDE = ("result_id", "result_hash", "internal_estimate")


class _Block(Exception):
    def __init__(self, *reasons: str):
        self.reasons = list(reasons)


class _Hit(Exception):
    def __init__(self, payload: dict):
        self.payload = payload


def _m(amount: Decimal | None, ccy: str | None) -> dict | None:
    return None if amount is None else {"amount": dstr(amount), "currency": ccy}


def _q8(x: Decimal) -> Decimal:
    with localcontext() as c:
        c.prec = PREC
        return x.quantize(Q8, rounding=ROUND_HALF_EVEN)


def _ts(s: str, field: str = "as_of") -> datetime:
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationFailed(f"{field}: invalid ISO-8601 timestamp", field_errors={field: "invalid"}) from exc
    if d.tzinfo is None:
        raise ValidationFailed(f"{field}: timezone required (UTC)", field_errors={field: "timezone required"})
    return d.astimezone(timezone.utc)


def _actor(ctx: dict) -> dict:
    aid = ctx.get("actor_id") or "SYSTEM"
    return {"actor_id": aid, "actor_type": "USER" if aid != "SYSTEM" else "SERVICE", "actor_role": ctx.get("actor_role"),
            "correlation_id": ctx.get("correlation_id") or new_id(), "source_mode": ctx.get("source_mode") or "REPLAY"}


def _evidence(session, tenant_id: str, ctx: dict, action: str, entity_type: str, entity_id: str, *, effective_at=None,
              input_hashes=None, output_hash=None, rationale=None, payload=None, revision=1):
    a = _actor(ctx)
    services.call("evidence.append", session, tenant_id, engine="risk", action=action, entity_type=entity_type,
                  entity_id=entity_id, entity_revision=revision, effective_at=effective_at, input_hashes=input_hashes or [],
                  output_hash=output_hash, rationale=rationale, payload=payload or {}, service_version=METHOD_VERSION, **a)
    return a


def _emit(session, tenant_id: str, ctx: dict, event_type: str, aggregate_id: str, payload: dict, effective_at=None):
    a = _actor(ctx)
    emit(session, event_type=event_type, tenant_id=tenant_id, correlation_id=a["correlation_id"], aggregate_id=aggregate_id,
         actor_id=a["actor_id"], actor_type=a["actor_type"], payload=payload, effective_at=effective_at, source_mode=a["source_mode"])


# --------------------------------------------------------------------------- snapshots
def pin_snapshot(session, tenant_id: str, snapshot_id: str, data_as_of: datetime, payload: dict, *, source_mode: str = "REPLAY",
                 max_age_seconds: int = 86400, ctx: dict | None = None) -> dict:
    ctx = ctx or {}
    errs = validate_snapshot_payload(payload)
    if errs:
        raise ValidationFailed("Snapshot validation failed", field_errors={f"e{i}": e for i, e in enumerate(errs)})
    try:
        h = content_hash({"data_as_of": data_as_of, "payload": payload, "max_age_seconds": max_age_seconds})
    except CanonicalError as exc:
        raise ValidationFailed(f"snapshot contains non-canonical values: {exc}") from exc
    row = session.execute(select(RiskSnapshot).where(RiskSnapshot.tenant_id == tenant_id, RiskSnapshot.snapshot_id == snapshot_id)).scalar_one_or_none()
    if row:
        if row.content_hash != h:
            raise Conflict(f"snapshot {snapshot_id} already pinned with different content (snapshots are immutable; use a new id)")
        return snapshot_out(row)
    row = RiskSnapshot(tenant_id=tenant_id, snapshot_id=snapshot_id, data_as_of=data_as_of, source_mode=source_mode,
                       max_age_seconds=max_age_seconds, payload=payload, content_hash=h, source_ref=f"snapshot:{snapshot_id}")
    session.add(row)
    session.flush()
    _evidence(session, tenant_id, ctx, "SNAPSHOT_PINNED", "risk_snapshot", snapshot_id, effective_at=data_as_of, output_hash=h,
              rationale="market snapshot pinned (immutable, content-hashed)", payload={"snapshot_id": snapshot_id, "content_hash": h})
    _emit(session, tenant_id, ctx, "SnapshotPinned", snapshot_id, {"snapshot_id": snapshot_id, "content_hash": h}, data_as_of)
    return snapshot_out(row)


def snapshot_out(r: RiskSnapshot) -> dict:
    p = r.payload
    return {"snapshot_id": r.snapshot_id, "data_as_of": r.data_as_of.isoformat(), "source_mode": r.source_mode,
            "max_age_seconds": r.max_age_seconds, "content_hash": r.content_hash, "provenance": r.provenance,
            "curves": sorted(p.get("curves", {})), "fx_pairs": sorted(p.get("fx", {})), "marks": len(p.get("marks", {}))}


def _load_snapshot(session, tenant_id: str, snapshot_id: str | None) -> RiskSnapshot:
    if not snapshot_id:
        raise _Block("no snapshot_id supplied: current market data is never used implicitly")
    r = session.execute(select(RiskSnapshot).where(RiskSnapshot.tenant_id == tenant_id, RiskSnapshot.snapshot_id == snapshot_id)).scalar_one_or_none()
    if r is None:
        raise _Block(f"snapshot {snapshot_id} not found")
    return r


def _check_fresh(snap: RiskSnapshot, as_of: datetime, quality: dict) -> None:
    age = (as_of - snap.data_as_of).total_seconds()
    quality.update(snapshot_data_as_of=snap.data_as_of.isoformat(), age_seconds=int(age), max_age_seconds=snap.max_age_seconds)
    if snap.data_as_of > as_of:
        raise _Block(f"snapshot {snap.snapshot_id} is dated after the run as_of (future data not allowed)")
    if age > snap.max_age_seconds:
        quality["stale"] = True
        raise _Block(f"STALE_MARKET: snapshot {snap.snapshot_id} is {int(age)}s old, limit {snap.max_age_seconds}s; a later valid snapshot needs a new run")
    quality["stale"] = False


# --------------------------------------------------------------------------- portfolio import
IRS_FIELDS = ("pay_receive", "notional", "currency", "fixed_rate", "effective", "maturity", "freq_months", "fixed_dc", "float_dc", "curve_disc", "curve_proj")
FXF_FIELDS = ("side", "base_ccy", "quote_ccy", "notional_base", "strike", "maturity", "curve_base", "curve_quote")
ETD_INST_FIELDS = ("type", "venue", "currency", "multiplier", "expiry", "settlement_type")


def validate_trade(t: dict) -> list[str]:
    from app.core.money import SUPPORTED_CURRENCIES
    e: list[str] = []
    for k in ("trade_id", "master_id", "type"):
        if not t.get(k):
            e.append(f"missing {k}")
    ty = t.get("type")
    need = IRS_FIELDS if ty == "IRS" else FXF_FIELDS if ty == "FXF" else None
    if need is None:
        return e + [f"unsupported trade type {ty!r} (only vanilla IRS and FXF; no exotics)"]
    e += [f"missing {k}" for k in need if t.get(k) in (None, "")]
    if e:
        return e
    try:
        n = to_decimal(t["notional"] if ty == "IRS" else t["notional_base"], field="notional", allow_negative=False)
        if ty == "IRS":
            to_decimal(t["fixed_rate"], field="fixed_rate")  # negative rates are legal
            if t["pay_receive"] not in ("PAY_FIXED", "RECEIVE_FIXED"):
                e.append("pay_receive must be PAY_FIXED|RECEIVE_FIXED")
            if t["currency"] not in SUPPORTED_CURRENCIES:
                e.append("unsupported currency")
            eff, mat = date.fromisoformat(t["effective"]), date.fromisoformat(t["maturity"])
            schedule(eff, mat, int(t["freq_months"]))
        else:
            if t["side"] not in ("BUY", "SELL"):
                e.append("side must be BUY|SELL")
            if to_decimal(t["strike"], field="strike") <= 0:
                e.append("strike must be positive")
            for c in (t["base_ccy"], t["quote_ccy"]):
                if c not in SUPPORTED_CURRENCIES:
                    e.append(f"unsupported currency {c}")
            if t["base_ccy"] == t["quote_ccy"]:
                e.append("base and quote currency identical")
            date.fromisoformat(t["maturity"])
        _ = n
    except (NumericError, ValueError, PricingError) as exc:
        e.append(str(exc))
    return e


def validate_instrument(i: dict) -> list[str]:
    e = [f"missing {k}" for k in ("instrument_id", *ETD_INST_FIELDS) if not i.get(k)]
    if e:
        return e
    try:
        if to_decimal(i["multiplier"], field="multiplier") <= 0:
            e.append("multiplier must be positive")
        date.fromisoformat(i["expiry"])
        if i["type"] == "OPT":
            for k in ("strike", "option_type", "underlying_id", "exercise"):
                if not i.get(k):
                    e.append(f"option missing {k}")
            if i.get("exercise") != "EUROPEAN":
                e.append("only European options supported")
            if i.get("option_type") not in ("CALL", "PUT"):
                e.append("option_type must be CALL|PUT")
        elif i["type"] != "FUT":
            e.append("type must be FUT|OPT")
        prof = {v[0]: v[1] for v in PROFILES.values()}
        if i["venue"] not in prof:
            e.append(f"unknown venue {i['venue']}")
        elif prof[i["venue"]] != i["currency"]:
            e.append(f"currency {i['currency']} invalid for venue {i['venue']} (expected {prof[i['venue']]}): no blending")
    except (NumericError, ValueError) as exc:
        e.append(str(exc))
    return e


def import_portfolio(session, tenant_id: str, *, instruments=(), trades=(), positions=(), ctx: dict | None = None) -> dict:
    """Atomic critical-batch import. Any error rejects everything (no silent partial import). Idempotent on identical content;
    changed content under the same id => 409 (inputs are immutable; revised data needs a new id/revision)."""
    ctx = ctx or {}
    errs: dict[str, str] = {}
    for i, t in enumerate(trades):
        for m in validate_trade(t):
            errs[f"trades[{i}]({t.get('trade_id')})"] = m
    for i, x in enumerate(instruments):
        for m in validate_instrument(x):
            errs[f"instruments[{i}]({x.get('instrument_id')})"] = m
    for i, p in enumerate(positions):
        try:
            if not p.get("account_id") or not p.get("instrument_id"):
                raise ValueError("account_id and instrument_id required")
            to_decimal(p["signed_lots"], field="signed_lots")
            to_decimal(p["settlement_price"], field="settlement_price", allow_negative=False)
            _ts(p["as_of"])
        except (KeyError, ValueError, NumericError, ValidationFailed) as exc:
            errs[f"positions[{i}]"] = str(exc)
    if len({t.get("trade_id") for t in trades}) != len(trades):
        errs["trades"] = "duplicate trade_id in batch"
    if errs:
        raise ValidationFailed("Portfolio import rejected atomically (no partial import)", field_errors=errs)
    added = {"trades": 0, "instruments": 0, "positions": 0}
    # Bulk existence checks (one query per chunk, not one per row): the per-row SELECT + autoflushed INSERT made a 2,000-row
    # position import take 22-32 s on PostgreSQL. Semantics are unchanged: identical content is idempotent, changed content => 409.
    CH = 500

    def _chunks(xs):
        xs = list(xs)
        for k in range(0, len(xs), CH):
            yield xs[k:k + CH]

    ex_trades: dict[str, RiskTrade] = {}
    for ids in _chunks(t["trade_id"] for t in trades):
        ex_trades.update({r.trade_id: r for r in session.execute(select(RiskTrade).where(RiskTrade.tenant_id == tenant_id, RiskTrade.trade_id.in_(ids))).scalars()})
    new_rows: list = []
    for t in trades:
        h = content_hash(t)
        row = ex_trades.get(t["trade_id"])
        if row:
            if row.content_hash != h:
                raise Conflict(f"trade {t['trade_id']} exists with different terms; trades are immutable")
            continue
        new_rows.append(RiskTrade(tenant_id=tenant_id, trade_id=t["trade_id"], master_id=t["master_id"], netting_set_id=t.get("netting_set_id"),
                                  trade_type=t["type"], maturity=t["maturity"], payload=t, content_hash=h, source_ref="import"))
        added["trades"] += 1
    ex_inst: dict[str, RiskInstrument] = {}
    for ids in _chunks(x["instrument_id"] for x in instruments):
        ex_inst.update({r.instrument_id: r for r in session.execute(select(RiskInstrument).where(RiskInstrument.tenant_id == tenant_id, RiskInstrument.instrument_id.in_(ids))).scalars()})
    seen_inst: dict[str, str] = {}
    for x in instruments:
        h = content_hash(x)
        row = ex_inst.get(x["instrument_id"])
        if row:
            if row.content_hash != h:
                raise Conflict(f"instrument {x['instrument_id']} exists with different terms")
            continue
        if x["instrument_id"] in seen_inst:  # duplicate inside the batch
            if seen_inst[x["instrument_id"]] != h:
                raise Conflict(f"instrument {x['instrument_id']} appears twice in the batch with different terms")
            continue
        seen_inst[x["instrument_id"]] = h
        new_rows.append(RiskInstrument(tenant_id=tenant_id, instrument_id=x["instrument_id"], venue=x["venue"], payload=x, content_hash=h))
        added["instruments"] += 1
    ex_pos: dict[tuple, RiskPosition] = {}
    groups: dict[tuple, list[str]] = {}
    for p_ in positions:
        groups.setdefault((p_["account_id"], _ts(p_["as_of"]), int(p_.get("revision", 1))), []).append(p_["instrument_id"])
    for (acc, as_of_g, rev_g), ids in groups.items():
        for chunk in _chunks(ids):
            for r in session.execute(select(RiskPosition).where(RiskPosition.tenant_id == tenant_id, RiskPosition.account_id == acc, RiskPosition.as_of == as_of_g,
                                                               RiskPosition.revision == rev_g, RiskPosition.instrument_id.in_(chunk))).scalars():
                ex_pos[(r.account_id, r.instrument_id, r.as_of, r.revision)] = r
    seen_pos: dict[tuple, tuple] = {}
    for p_ in positions:
        as_of, rev = _ts(p_["as_of"]), int(p_.get("revision", 1))
        vals = (dstr(to_decimal(p_["signed_lots"])), dstr(to_decimal(p_["settlement_price"])))
        key = (p_["account_id"], p_["instrument_id"], as_of, rev)
        row = ex_pos.get(key)
        if row:
            if (row.signed_lots, row.settlement_price) != vals:
                raise Conflict("position revision exists with different values; supply a new revision")
            continue
        if key in seen_pos:  # duplicate inside the batch
            if seen_pos[key] != vals:
                raise Conflict("position appears twice in the batch with different values; supply a new revision")
            continue
        seen_pos[key] = vals
        new_rows.append(RiskPosition(tenant_id=tenant_id, account_id=p_["account_id"], instrument_id=p_["instrument_id"], as_of=as_of, revision=rev,
                                     signed_lots=vals[0], settlement_price=vals[1]))
        added["positions"] += 1
    session.add_all(new_rows)  # one flush below: batched INSERTs
    session.flush()
    h = content_hash({"trades": sorted(t["trade_id"] for t in trades), "instruments": sorted(x["instrument_id"] for x in instruments), "n_pos": len(positions)})
    if any(added.values()):
        _evidence(session, tenant_id, ctx, "PORTFOLIO_IMPORTED", "risk_import", h[:16], output_hash=h, rationale="synthetic portfolio inputs imported atomically", payload=added)
    return {"added": added, "content_hash": h}


# --------------------------------------------------------------------------- clearing files
def import_clearing_file(session, tenant_id: str, *, csv_text: str, venue: str, revision: int = 1, declared_total: str | None = None,
                         expected_sha256: str | None = None, ctx: dict | None = None) -> dict:
    ctx = ctx or {}
    raw = csv_text.encode("utf-8")
    if len(raw) > get_settings().max_upload_bytes:
        raise ValidationFailed("file exceeds upload size limit")
    sha = hashlib.sha256(raw).hexdigest()
    if expected_sha256 and expected_sha256.lower() != sha:
        raise ValidationFailed("Checksum mismatch: file corrupt or altered in transit", field_errors={"expected_sha256": f"actual {sha}"})
    try:
        f = clr.parse_file(csv_text)
    except clr.ClearingFileError as exc:
        raise ValidationFailed("Clearing file rejected atomically", field_errors={f"row {e['row']} {e['field']}": e["error"] for e in exc.errors}) from exc
    prof = {v[0]: v[1] for v in PROFILES.values()}
    if venue not in prof:
        raise ValidationFailed(f"unknown venue {venue}", field_errors={"venue": "unknown"})
    if prof[venue] != f["currency"]:
        raise ValidationFailed(f"currency {f['currency']} is not the {venue} profile currency {prof[venue]} (no currency blending)", field_errors={"currency": "profile mismatch"})
    dt = to_decimal(declared_total, field="declared_total") if declared_total is not None else None
    rec = clr.reconcile(f["rows"], dt)
    existing = session.execute(select(RiskClearingFile).where(RiskClearingFile.tenant_id == tenant_id, RiskClearingFile.file_id == f["file_id"],
                                                              RiskClearingFile.revision == revision)).scalar_one_or_none()
    if existing:
        if existing.content_hash != sha:
            raise Conflict(f"file {f['file_id']} revision {revision} already imported with different content; submit a new revision")
        return clearing_out(existing)  # idempotent retry
    rows_json = [{"component": r["component"], "amount": dstr(r["amount"]), "currency": r["currency"], "kind": r["kind"]} for r in f["rows"]]
    rec_json = {k: (dstr(v) if isinstance(v, Decimal) else v) for k, v in rec.items()}
    row = RiskClearingFile(tenant_id=tenant_id, file_id=f["file_id"], revision=revision, account_id=f["account_id"], venue=venue,
                           currency=f["currency"], as_of=f["as_of"], rows=rows_json, content_hash=sha, computed_total=dstr(rec["computed_total"]),
                           reported_total=dstr(rec["reported_total"]) if rec["reported_total"] is not None else None, status=rec["status"],
                           reconciliation=rec_json, source_ref=f"clearing:{f['file_id']}")
    session.add(row)
    session.flush()
    _evidence(session, tenant_id, ctx, "CLEARING_FILE_IMPORTED", "risk_clearing_file", f"{f['file_id']}#{revision}", effective_at=f["as_of"],
              output_hash=sha, rationale=f"clearing file imported; reconciliation {rec['status']}", payload={"status": rec["status"], "account_id": f["account_id"], "computed_total": rec_json["computed_total"], "reported_total": rec_json["reported_total"]})
    if rec["status"] == "RECONCILED":
        _emit(session, tenant_id, ctx, "MarginReconciled", f["file_id"], {"file_id": f["file_id"], "status": "RECONCILED"}, f["as_of"])
    return clearing_out(row)


def clearing_out(r: RiskClearingFile) -> dict:
    return {"file_id": r.file_id, "revision": r.revision, "account_id": r.account_id, "venue": r.venue, "currency": r.currency,
            "as_of": r.as_of.isoformat(), "status": r.status, "computed_total": {"amount": r.computed_total, "currency": r.currency},
            "reported_total": {"amount": r.reported_total, "currency": r.currency} if r.reported_total is not None else None,
            "reconciliation": r.reconciliation, "rows": r.rows, "content_hash": r.content_hash, "provenance": r.provenance}


def _latest_file(session, tenant_id: str, account_id: str, file_id: str | None, revision: int | None):
    q = select(RiskClearingFile).where(RiskClearingFile.tenant_id == tenant_id, RiskClearingFile.account_id == account_id)
    if file_id:
        q = q.where(RiskClearingFile.file_id == file_id)
    if revision:
        q = q.where(RiskClearingFile.revision == revision)
    # deterministic: business date, then revision, then most recent import, then id (two files for one account/date never tie arbitrarily)
    return session.execute(q.order_by(RiskClearingFile.as_of.desc(), RiskClearingFile.revision.desc(), RiskClearingFile.created_at.desc(),
                                      RiskClearingFile.file_id.desc())).scalars().first()


def reconcile_clearing(session, tenant_id: str, file_id: str, snapshot_id: str | None = None, ctx: dict | None = None) -> dict:
    """Recompute reconciliation from stored rows (independent of the import-time status) and attach the INTERNAL estimate
    (labelled, never mixed with the source obligation)."""
    ctx = ctx or {}
    f = session.execute(select(RiskClearingFile).where(RiskClearingFile.tenant_id == tenant_id, RiskClearingFile.file_id == file_id)
                        .order_by(RiskClearingFile.revision.desc())).scalars().first()
    if f is None:
        raise NotFound("Clearing file not found")
    rows = [{"component": r["component"], "amount": Decimal(r["amount"]), "currency": r["currency"], "kind": r["kind"]} for r in f.rows]
    rec = clr.reconcile(rows, None)
    out = {"file_id": f.file_id, "revision": f.revision, "account_id": f.account_id, "currency": f.currency, "status": rec["status"],
           "computed_total": _m(rec["computed_total"], f.currency), "reported_total": _m(rec["reported_total"], f.currency),
           "difference": _m(rec["difference"], f.currency), "reason": rec["reason"], "source_obligation_label": "IMPORTED_SOURCE_OBLIGATION_NOT_COMPUTED_BY_AETHER",
           "internal_estimate": None, "warnings": []}
    if snapshot_id:
        try:
            snap = _load_snapshot(session, tenant_id, snapshot_id)
            insts = {r.instrument_id: r.payload for r in session.execute(select(RiskInstrument).where(RiskInstrument.tenant_id == tenant_id, RiskInstrument.venue == f.venue)).scalars()}
            pos = _positions(session, tenant_id, f.account_id, f.as_of)
            if pos:
                out["internal_estimate"] = etd.internal_estimate(pos, insts, MarketData(snap.payload), f.as_of.date(), Decimal("0.03"))
        except (_Block, PricingError, KeyError) as exc:
            out["warnings"].append(f"internal estimate unavailable: {exc if not isinstance(exc, _Block) else exc.reasons}")
    h = content_hash(out)
    _evidence(session, tenant_id, ctx, "CLEARING_RECONCILED", "risk_clearing_file", f"{f.file_id}#{f.revision}", effective_at=f.as_of, output_hash=h,
              input_hashes=[f.content_hash], rationale=f"independent component sum vs reported total: {rec['status']}", payload={"status": rec["status"]})
    _emit(session, tenant_id, ctx, "MarginReconciled", f.file_id, {"file_id": f.file_id, "status": rec["status"]}, f.as_of)
    return out


def _positions(session, tenant_id: str, account_id: str, as_of: datetime) -> list[dict]:
    rows = session.execute(select(RiskPosition).where(RiskPosition.tenant_id == tenant_id, RiskPosition.account_id == account_id, RiskPosition.as_of <= as_of)
                           .order_by(RiskPosition.as_of.desc(), RiskPosition.revision.desc())).scalars().all()
    if not rows:
        return []
    latest = rows[0].as_of
    seen: dict[str, RiskPosition] = {}
    for r in rows:  # highest revision at the latest as_of per instrument
        if r.as_of == latest and r.instrument_id not in seen:
            seen[r.instrument_id] = r
    return [{"instrument_id": k, "signed_lots": v.signed_lots, "settlement_price": v.settlement_price} for k, v in sorted(seen.items())]


# --------------------------------------------------------------------------- compute_margin
def _cache_lookup(session, tenant_id: str, key: str) -> None:
    if (_REPLAY.get() or {}).get("no_cache"):
        return
    r = session.execute(select(RiskResult).where(RiskResult.tenant_id == tenant_id, RiskResult.cache_key == key,
                                                 RiskResult.status.in_(("OK", "UNSUPPORTED"))).limit(1)).scalar_one_or_none()
    if r is not None:
        raise _Hit(r.payload)


def _shell(market: str, ctx: dict, snapshot_id, terms_hash, rule_set_hash, methodology, as_of_s: str) -> dict:
    return {"result_id": None, "market_type": market, "status": "OK", "blocking_reasons": [], "currency": None,
            "account_id": ctx.get("account_id"), "netting_set_id": ctx.get("netting_set_id"), "as_of": as_of_s,
            "exposure": None, "required_amount": None, "direction": "NONE", "unrounded_amount": None, "rounded_amount": None,
            "components": [], "waterfall": [], "input_hashes": [], "snapshot_id": snapshot_id, "snapshot_as_of": None, "terms_hash": terms_hash,
            "rule_set_hash": rule_set_hash, "method": methodology, "method_version": METHOD_VERSION, "hypothetical": False,
            "provenance_flags": ["SYNTHETIC"], "warnings": [], "quality": {}, "trade_valuations": [], "netting": None,
            "terms_used": None, "context_used": None, "internal_estimate": None, "result_hash": None}


_KEYS_FOR_CACHE = ("market_type", "netting_set_id", "account_id", "venue", "as_of", "known_at", "fact_snapshot_id", "existing_vm_balance",
                   "im_schedule", "hypothetical_margin_multiplier", "clearing_file_id", "clearing_revision", "file_max_age_seconds", "internal_shock_pct")


def _compute(session, tenant_id: str, context: dict, snapshot_id: str | None, terms_hash: str | None,
             rule_set_hash: str | None, methodology: str) -> tuple[dict, str, datetime]:
    """Pure computation of a MarginResult shell + its cache key. Raises _Hit on a cache hit (never during replay)."""
    try:
        canonical_bytes(context)
    except CanonicalError as exc:
        raise ValidationFailed(f"context contains non-canonical values (use decimal strings, not floats): {exc}") from exc
    mt = context.get("market_type")
    as_of = _ts(context["as_of"]) if context.get("as_of") else None
    if as_of is None:
        raise ValidationFailed("context.as_of is required (no implicit clock)", field_errors={"as_of": "required"})
    shell = _shell(mt, context, snapshot_id, terms_hash, rule_set_hash, methodology, as_of.isoformat())
    shell["context_used"] = {k: context[k] for k in _KEYS_FOR_CACHE if k in context}
    cache_key = None
    try:
        if mt == CCP:
            shell["status"] = "UNSUPPORTED"
            shell["blocking_reasons"] = ["CCP_OTC is unsupported: no CCP adapter exists; the bilateral CSA waterfall must not be reused for centrally cleared OTC"]
            cache_key = content_hash({"t": tenant_id, "m": mt, "v": METHOD_VERSION})
        elif mt not in (OTC, ETD_DOM, ETD_IFSC):
            raise _Block(f"unknown market_type {mt!r}")
        elif methodology not in METHODS:
            raise _Block(f"UNSUPPORTED_MODEL: methodology {methodology!r} is not available (supported: {sorted(METHODS)})")
        elif mt not in METHODS[methodology]:
            raise _Block(f"methodology {methodology} is not valid for market {mt}")
        elif mt == OTC:
            cache_key = _otc(session, tenant_id, context, shell, as_of, snapshot_id, terms_hash, rule_set_hash)
        else:
            cache_key = _etd(session, tenant_id, context, shell, as_of, snapshot_id, rule_set_hash, mt)
    except _Block as b:
        shell["status"] = "BLOCKED"
        shell["blocking_reasons"] = b.reasons
        shell["required_amount"] = shell["rounded_amount"] = shell["unrounded_amount"] = None
        shell["direction"] = "NONE"
        cache_key = cache_key or content_hash({"t": tenant_id, "blocked": shell["blocking_reasons"], "ctx": shell["context_used"]})
    return shell, cache_key, as_of


def compute_margin(session, tenant_id: str, context: dict, snapshot_id: str | None, terms_hash: str | None,
                   rule_set_hash: str | None, methodology: str) -> dict:
    try:
        shell, cache_key, as_of = _compute(session, tenant_id, context, snapshot_id, terms_hash, rule_set_hash, methodology)
    except _Hit as h:
        return h.payload
    return _persist(session, tenant_id, context, shell, cache_key, as_of)


def business_hash(payload: dict) -> str:
    """Canonical BUSINESS hash of a MarginResult: excludes run identity (result_id/result_hash) and the float-based internal
    analytics, which replay compares with a declared tolerance instead (platform-dependent math library)."""
    return content_hash({k: v for k, v in payload.items() if k not in BUSINESS_HASH_EXCLUDE})


def _replay_spec(shell: dict) -> dict:
    """Evidence payload.replay for a computed result (see app/engines/evidence/replay.py contract)."""
    ie = shell.get("internal_estimate") or {}
    values = {"required_amount": (shell.get("required_amount") or {}).get("amount"), "exposure": (shell.get("exposure") or {}).get("amount")}
    if ie.get("estimated_loss") is not None:
        values["internal_estimated_loss"] = ie["estimated_loss"]
    values = {k: v for k, v in values.items() if v is not None}
    q = shell.get("quality") or {}
    spec = {"replayer": "replay.risk", "engine_version": ENGINE_VERSION, "method_version": f"{shell['method']}/{METHOD_VERSION}",
            "business_hash": business_hash(shell), "values": values,
            "inputs": {"context": shell["context_used"], "snapshot_id": shell["snapshot_id"], "terms_hash": shell["terms_hash"],
                       "rule_set_hash": shell["rule_set_hash"], "methodology": shell["method"],
                       "trade_ids": sorted(v["trade_id"] for v in shell.get("trade_valuations") or []),
                       "clearing_file": [q["file_id"], q["file_revision"]] if q.get("file_id") else None}}
    if "internal_estimated_loss" in values:
        spec["tolerance"] = "0.01"  # only the float-based internal estimate is tolerance-compared; the business hash is bitwise
    return spec


def _parents(shell: dict) -> list[dict]:
    """Lineage edges (parent -> this result) in the vocabulary of app/engines/evidence/lineage.py."""
    ps = []
    if shell.get("snapshot_id"):
        ps.append({"entity_type": "risk_snapshot", "entity_id": shell["snapshot_id"]})
    q = shell.get("quality") or {}
    if q.get("file_id"):
        ps.append({"entity_type": "risk_clearing_file", "entity_id": f"{q['file_id']}#{q['file_revision']}"})
    if shell.get("terms_hash"):
        ps.append({"entity_type": "contract_terms_hash", "entity_id": shell["terms_hash"]})
    if shell.get("rule_set_hash"):
        ps.append({"entity_type": "rule_set", "entity_id": shell["rule_set_hash"]})
    return ps


def _persist(session, tenant_id: str, ctx: dict, shell: dict, cache_key: str, as_of: datetime) -> dict:
    shell["result_hash"] = content_hash({k: v for k, v in shell.items() if k not in ("result_id", "result_hash")})
    shell["result_id"] = new_id()
    row = RiskResult(result_id=shell["result_id"], tenant_id=tenant_id, market_type=shell["market_type"] or "?",
                     subject_id=shell.get("netting_set_id") or shell.get("account_id"), status=shell["status"],
                     cache_key=cache_key, result_hash=shell["result_hash"], payload=shell, source_ref=f"risk:{shell['method']}")
    if shell["status"] in ("OK", "UNSUPPORTED"):
        # Atomic create-if-absent: the partial unique index (tenant, cache_key) makes a concurrent identical compute lose cleanly.
        # The loser returns the winner's immutable result and writes NO evidence of its own (no duplicate material effect).
        sp = session.begin_nested()
        session.add(row)
        try:
            session.flush()
            sp.commit()
        except IntegrityError:
            sp.rollback()
            if row in session:
                session.expunge(row)
            winner = session.execute(select(RiskResult).where(RiskResult.tenant_id == tenant_id, RiskResult.cache_key == cache_key,
                                                              RiskResult.status.in_(("OK", "UNSUPPORTED")))
                                     .execution_options(populate_existing=True).limit(1)).scalar_one()
            return winner.payload
    else:
        session.add(row)
        session.flush()
    action = {"OK": "MARGIN_COMPUTED", "UNSUPPORTED": "MARGIN_UNSUPPORTED"}.get(shell["status"], "MARGIN_BLOCKED")
    ev_payload = {"status": shell["status"], "market_type": shell["market_type"], "required_amount": shell["required_amount"],
                  "direction": shell["direction"], "hypothetical": shell["hypothetical"], "snapshot_id": shell["snapshot_id"],
                  "snapshot_as_of": shell["snapshot_as_of"], "terms_hash": shell["terms_hash"], "rule_set_hash": shell["rule_set_hash"],
                  "parents": _parents(shell)}
    if shell["status"] == "OK":
        ev_payload["replay"] = _replay_spec(shell)
    _evidence(session, tenant_id, ctx, action, "risk_result", shell["result_id"], effective_at=as_of, input_hashes=shell["input_hashes"],
              output_hash=shell["result_hash"], rationale="; ".join(shell["blocking_reasons"]) or f"{shell['method']} {shell['method_version']}",
              payload=ev_payload)
    if shell["status"] == "OK":
        _emit(session, tenant_id, ctx, "MarginComputed", shell["result_id"], {"result_id": shell["result_id"], "required_amount": shell["required_amount"],
                                                                              "direction": shell["direction"], "market_type": shell["market_type"]}, as_of)
    return shell


def get_result(session, tenant_id: str, result_id: str) -> dict:
    r = session.get(RiskResult, result_id)
    if r is None or r.tenant_id != tenant_id:
        raise NotFound("Result not found")
    return r.payload


# ---- OTC
def _trade_dicts(rows) -> list[dict]:
    return [{"trade_id": r.trade_id, "type": r.trade_type, "master_id": r.master_id, "netting_set_id": r.netting_set_id, "payload": r.payload} for r in rows]


def value_set(trades: list[dict], md: MarketData, val: date, ccy: str) -> tuple[list[dict], list[str]]:
    vals, errs = [], []
    for t in trades:
        p = t["payload"]
        try:
            if t["type"] == "IRS":
                pv, pccy = value_irs(p, md, val, t["trade_id"]), p["currency"]
                notional_b = md.convert(Decimal(p["notional"]), p["currency"], ccy)
            else:
                pv, pccy = value_fxf(p, md, val), p["quote_ccy"]
                notional_b = md.convert(Decimal(p["notional_base"]), p["base_ccy"], ccy)
            vals.append({"trade_id": t["trade_id"], "type": t["type"], "side": p.get("side") or p.get("pay_receive"), "netting_set_id": t["netting_set_id"],
                         "master_id": t["master_id"], "pv_trade_ccy": _m(_q8(pv), pccy), "pv_base": _m(_q8(md.convert(pv, pccy, ccy)), ccy),
                         "fx_used": None if pccy == ccy else dstr(md.fx_rate(pccy, ccy)), "maturity": p["maturity"],
                         "_notional_base": notional_b, "_years": residual_years(p, val)})
        except (PricingError, KeyError, ArithmeticError) as exc:
            errs.append(f"{t['trade_id']}: {exc}")  # unknown is never aggregated as zero
    return vals, errs


def _otc(session, tenant_id, context, shell, as_of, snapshot_id, terms_hash, rule_set_hash) -> str:
    ns = context.get("netting_set_id")
    if not ns:
        raise _Block("netting_set_id is required for OTC")
    held = context.get("existing_vm_balance")
    if not isinstance(held, dict) or "PARTY_A" not in held or "PARTY_B" not in held:
        raise _Block("context.existing_vm_balance {PARTY_A,PARTY_B} is required (no silent default for existing collateral)")
    snap = _load_snapshot(session, tenant_id, snapshot_id)
    shell["snapshot_as_of"] = snap.data_as_of.isoformat()
    rows = session.execute(select(RiskTrade).where(RiskTrade.tenant_id == tenant_id, RiskTrade.netting_set_id == ns).order_by(RiskTrade.trade_id)).scalars().all()
    pin = (_REPLAY.get() or {}).get("trade_ids")
    if pin is not None:  # replay: exactly the trades that were valued originally (trades added later must not change a replay)
        rows = [r for r in rows if r.trade_id in pin]
    trades = _trade_dicts(rows)
    trades_hash = content_hash([(t["trade_id"], t["payload"]) for t in trades])
    key = content_hash({"t": tenant_id, "snap": snap.content_hash, "terms": terms_hash, "rules": rule_set_hash, "trades": trades_hash,
                        "ctx": shell["context_used"], "v": METHOD_VERSION, "method": shell["method"]})
    shell["input_hashes"] = sorted(h for h in (snap.content_hash, terms_hash, rule_set_hash, trades_hash) if h)
    _cache_lookup(session, tenant_id, key)
    _check_fresh(snap, as_of, shell["quality"])
    # terms: contracts engine, never a guess
    try:
        tr = services.call("contracts.resolve_terms", session, tenant_id, ns, as_of, _ts(context["known_at"]) if context.get("known_at") else as_of, context.get("fact_snapshot_id"))
    except Unavailable:
        raise _Block("contracts.resolve_terms unavailable: approved terms cannot be resolved (no default terms are assumed)") from None
    if tr.get("status") != "RESOLVED":
        raise _Block(*(["terms not resolved by contracts engine"] + list(tr.get("blocking_reasons") or [])))
    if terms_hash and tr.get("terms_hash") != terms_hash:
        raise _Block(f"terms_hash mismatch: caller pinned {terms_hash[:12]} but approved terms at as_of are {str(tr.get('terms_hash'))[:12]} (stale approval)")
    terms = tr["terms"]
    shell["terms_hash"] = tr.get("terms_hash")
    shell["terms_used"] = terms
    shell["input_hashes"] = sorted(set(shell["input_hashes"]) | {tr.get("terms_hash")})
    try:
        mg.validate_terms(terms)
    except mg.TermsError as exc:
        raise _Block(f"terms invalid: {exc}") from None
    ccy = terms["base_currency"]
    shell["currency"] = ccy
    # regulatory parameters (applicability). Absent pin => explicit contract-only flag.
    if rule_set_hash:
        try:
            services.call("regulatory.rule_parameters", session, tenant_id, rule_set_hash)
            shell["provenance_flags"].append("REGULATORY_RULE_SET_PINNED")
        except Unavailable:
            shell["warnings"].append("regulatory.rule_parameters unavailable: regulatory applicability of IM/VM NOT established")
        except NotFound:
            raise _Block(f"rule_set_hash {rule_set_hash[:12]} is not a known approved rule set (cannot establish regulatory applicability)") from None
    else:
        shell["warnings"].append("no rule_set_hash pinned: result is contract-driven synthetic policy; regulatory IM/VM applicability UNKNOWN (no real-policy conclusion)")
    if not trades:
        raise _Block(f"no trades mapped to netting set {ns} (unknown mapping is quarantined, never assumed zero)")
    masters = {t["master_id"] for t in trades}
    if len(masters) > 1:
        raise _Block(f"cross-master trades in netting set {ns}: {sorted(masters)} (cross-master netting rejected)")
    val = as_of.date()
    expired = [t["trade_id"] for t in trades if date.fromisoformat(t["payload"]["maturity"]) <= val]
    if expired:
        raise _Block(f"expired/matured trades present in netting set: {expired}")
    md = MarketData(snap.payload)
    vals, errs = value_set(trades, md, val, ccy)
    if errs:
        raise _Block(*errs)
    pos = sum((Decimal(v["pv_base"]["amount"]) for v in vals if Decimal(v["pv_base"]["amount"]) > 0), Decimal(0))
    neg = sum((Decimal(v["pv_base"]["amount"]) for v in vals if Decimal(v["pv_base"]["amount"]) < 0), Decimal(0))
    net = pos + neg
    shell["netting"] = {"netting_set_id": ns, "gross_positive": _m(pos, ccy), "gross_negative": _m(neg, ccy), "net": _m(net, ccy),
                        "netting_benefit": _m(pos - max(net, Decimal(0)), ccy),
                        "netting_benefit_definition": "sum of positive trade PVs minus max(net PV, 0): reduction in positive credit exposure from netting; not notional, not realised cash saving"}
    shell["exposure"] = _m(net, ccy)
    try:
        w = mg.otc_waterfall(net, terms, held, ccy, shell["terms_hash"] or "")
    except (mg.TermsError, NumericError) as exc:
        raise _Block(str(exc)) from None
    shell["waterfall"] = w["steps"]
    shell["direction"] = w["direction"]
    shell["unrounded_amount"] = _m(w["unrounded_amount"], ccy)
    shell["rounded_amount"] = _m(w["rounded_amount"], ccy)
    shell["required_amount"] = _m(w["rounded_amount"], ccy)
    shell["components"] = [{"kind": "VM", "amount": dstr(w["rounded_amount"]), "currency": ccy, "direction": w["direction"], "obligor": w["obligor"],
                            "secured_party": w["secured_party"], "target": dstr(w["target"]), "held_same_direction": dstr(w["held_same_direction"]),
                            "residual_vs_unrounded": dstr(w["residual"]), "mta_triggered": w["mta_triggered"]}]
    if w["opposite_return"] > 0:
        shell["components"].append({"kind": "VM_OPPOSITE_RETURN", "amount": dstr(w["opposite_return"]), "currency": ccy, "direction": "RETURN",
                                    "note": "collateral posted by the opposite party: separate obligation, never offsets the new direction", "detail": w.get("opposite_detail")})
    sched = context.get("im_schedule")
    if terms.get("segregated_im"):
        if sched:
            try:
                im = mg.im_illustrative([(v["_notional_base"], v["_years"]) for v in vals], sched)
                shell["components"].append({"kind": "IM", "amount": dstr(_q8(im)), "currency": ccy, "label": "ILLUSTRATIVE_SCHEDULE_NOT_SIMM",
                                            "note": "segregated; never netted against VM; demonstrative notional-bucket schedule", "hypothetical": True})
                shell["hypothetical"] = True
                shell["provenance_flags"].append("HYPOTHETICAL_IM_SCHEDULE")
                shell["waterfall"].append({"step": "illustrative IM (segregated, separate from VM)", "value": dstr(_q8(im)), "citation": "context.im_schedule (HYPOTHETICAL, not SIMM)"})
            except (NumericError, KeyError, IndexError) as exc:
                raise _Block(f"im_schedule invalid: {exc}") from None
        else:
            shell["warnings"].append("terms.segregated_im is true but no im_schedule supplied: IM NOT computed (not zero)")
    for v in vals:
        v.pop("_notional_base"); v.pop("_years")
    shell["trade_valuations"] = vals
    return key


# ---- ETD
def _etd(session, tenant_id, context, shell, as_of, snapshot_id, rule_set_hash, mt) -> str:
    venue_expected, ccy = PROFILES[mt]
    acc = context.get("account_id")
    if not acc:
        raise _Block("account_id is required for ETD")
    if context.get("venue") and context["venue"] != venue_expected:
        raise _Block(f"venue {context['venue']} does not belong to the {mt} profile (expected {venue_expected}); domestic and IFSC policies are independent")
    shell["currency"] = ccy
    snap = _load_snapshot(session, tenant_id, snapshot_id)
    shell["snapshot_as_of"] = snap.data_as_of.isoformat()
    rp = (_REPLAY.get() or {}).get("file")  # replay: the exact clearing file revision used originally, not "latest now"
    f = _latest_file(session, tenant_id, acc, (context.get("clearing_file_id") or (rp[0] if rp else None)),
                     (context.get("clearing_revision") or (rp[1] if rp else None)))
    mult_ctx = context.get("hypothetical_margin_multiplier")
    key = content_hash({"t": tenant_id, "snap": snap.content_hash, "file": f.content_hash if f else None, "rules": rule_set_hash, "ctx": shell["context_used"],
                        "v": METHOD_VERSION, "method": shell["method"]})
    shell["input_hashes"] = sorted(h for h in (snap.content_hash, f.content_hash if f else None, rule_set_hash) if h)
    _cache_lookup(session, tenant_id, key)
    _check_fresh(snap, as_of, shell["quality"])
    if f is None:
        raise _Block(f"no clearing margin file imported for account {acc}")
    if f.venue != venue_expected or f.currency != ccy:
        raise _Block(f"clearing file {f.file_id} venue/currency ({f.venue}/{f.currency}) does not match profile {mt} ({venue_expected}/{ccy})")
    age = (as_of - f.as_of).total_seconds()
    max_age = int(context.get("file_max_age_seconds", 86400))
    shell["quality"].update(file_id=f.file_id, file_revision=f.revision, file_as_of=f.as_of.isoformat(), file_age_seconds=int(age), file_status=f.status)
    if f.as_of > as_of:
        raise _Block(f"clearing file {f.file_id} is dated after run as_of")
    if age > max_age:
        raise _Block(f"STALE_FILE: clearing file {f.file_id} is {int(age)}s old (limit {max_age}s)")
    if f.status != "RECONCILED":
        raise _Block(f"clearing file {f.file_id} has a reconciliation exception: {f.reconciliation.get('reason')}")
    base = Decimal(f.computed_total)
    steps = shell["waterfall"]
    steps.append({"step": "imported source margin obligation (independent sum of additive components)", "value": dstr(base), "citation": f"clearing file {f.file_id} rev {f.revision} sha256 {f.content_hash[:12]}"})
    mult, hyp = None, False
    if rule_set_hash:
        try:
            rp = services.call("regulatory.rule_parameters", session, tenant_id, rule_set_hash) or {}
            if rp.get("margin_multiplier") is not None:
                mult, hyp = to_decimal(rp["margin_multiplier"], field="margin_multiplier"), bool(rp.get("hypothetical"))
                shell["provenance_flags"].append("REGULATORY_RULE_SET_PINNED")
        except Unavailable:
            shell["warnings"].append("regulatory.rule_parameters unavailable: no rule-driven adjustment applied")
        except NotFound:
            raise _Block(f"rule_set_hash {rule_set_hash[:12]} is not a known approved rule set") from None
    if mult is None and mult_ctx is not None:
        mult, hyp = to_decimal(mult_ctx, field="hypothetical_margin_multiplier"), True
    if mult is not None and mult <= 0:
        raise _Block("margin multiplier must be positive")
    required = base
    shell["components"].append({"kind": "IMPORTED", "amount": dstr(base), "currency": ccy, "label": "SOURCE_OBLIGATION_IMPORTED_NOT_SPAN"})
    if mult is not None:
        required = (base * mult).quantize(Q2, rounding=ROUND_HALF_EVEN)
        adj = required - base
        steps.append({"step": "rule revision multiplier" + (" (HYPOTHETICAL)" if hyp else ""), "value": dstr(mult),
                      "citation": "regulatory.rule_parameters" if not mult_ctx or rule_set_hash else "context.hypothetical_margin_multiplier"})
        steps.append({"step": "revised margin requirement = imported * multiplier", "value": dstr(required), "citation": "derived"})
        shell["components"].append({"kind": "RULE_ADJUSTMENT", "amount": dstr(adj), "currency": ccy, "hypothetical": hyp})
        steps.append({"step": "change vs baseline", "value": dstr(adj), "citation": "derived (reconciles to components)"})
        shell["hypothetical"] = hyp
        if hyp:
            shell["provenance_flags"].append("HYPOTHETICAL_RULE_PARAMETER")
    shell["baseline_amount"] = _m(base, ccy)
    shell["exposure"] = _m(base, ccy)
    shell["unrounded_amount"] = shell["rounded_amount"] = shell["required_amount"] = _m(required, ccy)
    shell["direction"] = "POST" if required > 0 else "NONE"
    try:
        insts = {r.instrument_id: r.payload for r in session.execute(select(RiskInstrument).where(RiskInstrument.tenant_id == tenant_id, RiskInstrument.venue == venue_expected)).scalars()}
        pos = _positions(session, tenant_id, acc, as_of)
        if pos:
            shell["internal_estimate"] = etd.internal_estimate(pos, insts, MarketData(snap.payload), as_of.date(), to_decimal(context.get("internal_shock_pct", "0.03")))
        else:
            shell["warnings"].append("no positions imported for account: internal estimate and position metrics unavailable")
    except (PricingError, KeyError, NumericError) as exc:
        shell["warnings"].append(f"internal estimate unavailable: {exc}")
    return key


# --------------------------------------------------------------------------- stress
def stress(session, tenant_id: str, base_result_id: str, shock_spec: dict, ctx: dict | None = None) -> dict:
    """Deterministic shocks recomputed on a COPY; the baseline result and pinned snapshot are never mutated."""
    ctx = ctx or {}
    base = get_result(session, tenant_id, base_result_id)
    if base["status"] != "OK":
        raise Conflict(f"base result is {base['status']}; stress requires an OK baseline")
    try:
        canonical_bytes(shock_spec)
    except CanonicalError as exc:
        raise ValidationFailed(f"shock_spec must use decimal strings: {exc}") from exc
    snap = _load_snapshot_or_404(session, tenant_id, base["snapshot_id"])
    as_of = _ts(base["as_of"])
    try:
        shocked = shock_payload(snap.payload, shock_spec)
    except PricingError as exc:
        raise ValidationFailed(str(exc)) from exc
    md0, md1 = MarketData(snap.payload), MarketData(shocked)
    out: dict[str, Any] = {"base_result_id": base_result_id, "market_type": base["market_type"], "shock_spec": shock_spec, "baseline_snapshot_id": snap.snapshot_id,
                           "baseline_mutated": False, "method_version": METHOD_VERSION, "label": "STRESS_SCENARIO_NOT_A_REGULATORY_MARGIN"}
    ccy = base["currency"]
    if base["market_type"] == OTC:
        ns = base["netting_set_id"]
        rows = session.execute(select(RiskTrade).where(RiskTrade.tenant_id == tenant_id, RiskTrade.netting_set_id == ns).order_by(RiskTrade.trade_id)).scalars().all()
        trades = _trade_dicts(rows)
        terms, held = base["terms_used"], base["context_used"]["existing_vm_balance"]
        val = as_of.date()

        def run(md):
            vals, errs = value_set(trades, md, val, ccy)
            if errs:
                raise ValidationFailed("stress valuation failed", field_errors={f"e{i}": e for i, e in enumerate(errs)})
            net = sum((Decimal(v["pv_base"]["amount"]) for v in vals), Decimal(0))
            w = mg.otc_waterfall(net, terms, held, ccy, base["terms_hash"])
            return vals, net, w

        v0, n0, w0 = run(md0)
        v1, n1, w1 = run(md1)
        _, n_dv, _ = run(MarketData(shock_payload(snap.payload, {"curve_shift_bp": "1"})))
        out.update(baseline={"exposure": _m(n0, ccy), "required": _m(w0["rounded_amount"], ccy), "direction": w0["direction"]},
                   scenario={"exposure": _m(n1, ccy), "required": _m(w1["rounded_amount"], ccy), "direction": w1["direction"], "waterfall": w1["steps"]},
                   delta={"exposure": _m(n1 - n0, ccy), "required_signed": _m((w1["rounded_amount"] if w1["direction"] == "POST" else -w1["rounded_amount"]) - (w0["rounded_amount"] if w0["direction"] == "POST" else -w0["rounded_amount"]), ccy)},
                   dv01={"value": _m(_q8(n_dv - n0), ccy), "method": "finite difference: +1bp parallel zero-rate bump on all curves, full revaluation"},
                   breakdown=[{"trade_id": a["trade_id"], "baseline": a["pv_base"], "scenario": b["pv_base"],
                               "delta": _m(_q8(Decimal(b["pv_base"]["amount"]) - Decimal(a["pv_base"]["amount"])), ccy)} for a, b in zip(v0, v1)])
    else:
        prof_venue = PROFILES[base["market_type"]][0]
        insts = {r.instrument_id: r.payload for r in session.execute(select(RiskInstrument).where(RiskInstrument.tenant_id == tenant_id, RiskInstrument.venue == prof_venue)).scalars()}
        pos = _positions(session, tenant_id, base["account_id"], as_of)
        if not pos:
            raise Conflict("no positions for account: ETD stress needs positions")
        rows_out, tot = [], Decimal(0)
        try:
            for p in pos:
                pnl = etd.position_pnl_between(insts[p["instrument_id"]], Decimal(p["signed_lots"]), md0, md1, as_of.date())
                tot += pnl
                rows_out.append({"instrument_id": p["instrument_id"], "signed_lots": p["signed_lots"], "scenario_pnl": _m(pnl, ccy)})
        except (PricingError, KeyError) as exc:
            raise ValidationFailed(f"ETD stress failed: {exc}") from exc
        out.update(baseline={"imported_margin": base["baseline_amount"], "required": base["required_amount"]},
                   scenario={"internal_pnl": _m(tot, ccy), "label": "INTERNAL_ESTIMATE_NOT_SOURCE_OBLIGATION_NOT_SPAN",
                             "note": "imported clearing margin is a source obligation and is not re-derived under stress"},
                   delta={"internal_pnl": _m(tot, ccy)}, breakdown=rows_out)
    sh = content_hash(shock_spec)
    sid = new_id()
    out["stress_id"] = sid
    session.add(RiskStressRun(stress_id=sid, tenant_id=tenant_id, base_result_id=base_result_id, shock_hash=sh, shock_spec=shock_spec, payload=out,
                              source_ref=f"stress:{base_result_id}"))
    session.flush()
    _evidence(session, tenant_id, ctx, "STRESS_RUN", "risk_stress", sid, effective_at=as_of, input_hashes=[base["result_hash"], snap.content_hash, sh],
              output_hash=content_hash(out), rationale="deterministic stress on a copy of the pinned snapshot", payload={"base_result_id": base_result_id})
    return out


def _load_snapshot_or_404(session, tenant_id, snapshot_id):
    try:
        return _load_snapshot(session, tenant_id, snapshot_id)
    except _Block as b:
        raise NotFound("; ".join(b.reasons)) from None


# --------------------------------------------------------------------------- contract v1.1: positions, reconciliation, replay
def get_positions(session, tenant_id: str, account_id: str, as_of) -> dict:
    """Contract v1.1. Raw position metrics for A5 position-limit rules. NEVER fabricates: any metric that cannot be computed from
    pinned inputs is None with a reason (UNKNOWN downstream, not zero). Gross/net lots need only the imported positions;
    notional and delta-equivalent additionally need marks from the latest pinned snapshot at or before as_of."""
    as_of = _ts(as_of) if isinstance(as_of, str) else as_of
    pos = _positions(session, tenant_id, account_id, as_of)
    out = {"account_id": account_id, "gross_lots": None, "net_lots": None, "delta_equivalent_lots": None, "notional": None, "currency": None,
           "client_class": "UNKNOWN", "as_of": None, "source": None, "warnings": [], "available": False}
    out["client_class_note"] = ("client/proprietary/member classification is NOT an input of the risk engine; reported as UNKNOWN "
                                "(a position-limit control must treat it as UNKNOWN, never as a pass)")
    if not pos:
        out["warnings"].append("NO_POSITIONS_IMPORTED: no positions at or before as_of for this account")
        return out
    latest = max(session.execute(select(RiskPosition.as_of).where(RiskPosition.tenant_id == tenant_id, RiskPosition.account_id == account_id,
                                                                   RiskPosition.as_of <= as_of)).scalars().all())
    ids = [p["instrument_id"] for p in pos]
    insts = {r.instrument_id: r.payload for r in session.execute(select(RiskInstrument).where(RiskInstrument.tenant_id == tenant_id,
                                                                                               RiskInstrument.instrument_id.in_(ids))).scalars()}
    out.update(available=True, as_of=latest.isoformat(), source=f"risk_positions rows={len(pos)} at {latest.isoformat()}")
    lots = [Decimal(p["signed_lots"]) for p in pos]
    out["gross_lots"] = dstr(sum((abs(x) for x in lots), Decimal(0)))
    out["net_lots"] = dstr(sum(lots, Decimal(0)))
    ccys = {insts[i]["currency"] for i in ids if i in insts}
    if len(ccys) == 1:
        out["currency"] = next(iter(ccys))
    elif ccys:
        out["warnings"].append(f"MIXED_CURRENCIES {sorted(ccys)}: notional not aggregated across currencies")
    missing = [i for i in ids if i not in insts]
    if missing:
        out["warnings"].append(f"instrument definitions missing for {missing}: notional/delta unavailable")
        return out
    snaps = session.execute(select(RiskSnapshot).where(RiskSnapshot.tenant_id == tenant_id, RiskSnapshot.data_as_of <= as_of)
                            .order_by(RiskSnapshot.data_as_of.desc(), RiskSnapshot.snapshot_id)).scalars().all()
    if not snaps or len(ccys) != 1:
        out["warnings"].append("NO_SNAPSHOT_AT_OR_BEFORE_AS_OF: notional and delta-equivalent unavailable (not zero)")
        return out
    last_err = None
    for snap in snaps:  # latest pinned snapshot at/before as_of that actually carries the marks/vols these instruments need
        try:
            m = etd.position_metrics(pos, insts, MarketData(snap.payload), latest.date())
        except (PricingError, KeyError, ValueError, ArithmeticError) as exc:
            last_err = last_err or exc  # keep the reason from the LATEST snapshot (the one a user would expect to be used)
            continue
        out["notional"], out["delta_equivalent_lots"] = m["notional"], m["delta_equivalent_lots"]
        out["source"] += f"; marks from snapshot {snap.snapshot_id} (data_as_of {snap.data_as_of.isoformat()})"
        return out
    out["warnings"].append(f"notional/delta unavailable from every pinned snapshot at or before as_of (last error: {last_err}); reported as unknown, not zero")
    return out


def reconciliation_status(session, tenant_id: str, account_id: str, as_of) -> dict:
    """Contract v1.1. Status of the latest clearing file for the account dated at or before as_of, RECOMPUTED from stored rows
    (independent of the import-time status). UNKNOWN (never OK) when there is no file."""
    as_of = _ts(as_of) if isinstance(as_of, str) else as_of
    f = session.execute(select(RiskClearingFile).where(RiskClearingFile.tenant_id == tenant_id, RiskClearingFile.account_id == account_id,
                                                       RiskClearingFile.as_of <= as_of)
                        .order_by(RiskClearingFile.as_of.desc(), RiskClearingFile.revision.desc(), RiskClearingFile.created_at.desc(),
                                RiskClearingFile.file_id.desc())).scalars().first()
    if f is None:
        return {"status": "UNKNOWN", "expected_total": None, "imported_total": None, "difference": None, "currency": None, "file_id": None,
                "reason": f"no clearing file for {account_id} at or before {as_of.isoformat()}"}
    rows = [{"component": r["component"], "amount": Decimal(r["amount"]), "currency": r["currency"], "kind": r["kind"]} for r in f.rows]
    rec = clr.reconcile(rows, None)
    # EXCEPTION = a discrepancy was found; UNKNOWN = it cannot be reconciled at all (no reported total supplied). UNKNOWN is never OK.
    status = "OK" if rec["status"] == "RECONCILED" else ("UNKNOWN" if rec["reported_total"] is None else "EXCEPTION")
    return {"status": status, "expected_total": dstr(rec["computed_total"]), "imported_total": dstr(rec["reported_total"]) if rec["reported_total"] is not None else None,
            "difference": dstr(rec["difference"]) if rec["difference"] is not None else None, "currency": f.currency, "file_id": f.file_id,
            "revision": f.revision, "file_as_of": f.as_of.isoformat(), "reason": rec["reason"],
            "expected_total_definition": "independent sum of additive component rows; the reported total is never trusted or added"}


def replay_risk(session, tenant_id: str, spec: dict) -> dict:
    """Replayer registered as replay.risk (called by evidence.replay_run inside a SAVEPOINT that is always rolled back).
    Recomputes from persisted inputs with the result cache bypassed and the original trades / clearing file revision pinned.
    Raises when a replay cannot be performed (the caller reports NOT_REPLAYABLE) - it never silently substitutes inputs."""
    inp = spec.get("inputs") or {}
    meth = inp.get("methodology")
    if meth not in METHODS:
        raise ValueError(f"methodology {meth!r} is not available in this engine version")
    pins = {"no_cache": True, "trade_ids": set(inp["trade_ids"]) if inp.get("trade_ids") is not None else None,
            "file": tuple(inp["clearing_file"]) if inp.get("clearing_file") else None}
    tok = _REPLAY.set(pins)
    try:
        shell, _, _ = _compute(session, tenant_id, dict(inp["context"]), inp.get("snapshot_id"), inp.get("terms_hash"), inp.get("rule_set_hash"), meth)
    finally:
        _REPLAY.reset(tok)
    if shell["status"] != "OK":
        raise RuntimeError(f"REPLAY_INPUT_UNAVAILABLE: recomputation is {shell['status']}: {'; '.join(shell['blocking_reasons'])[:200]}")
    rs = _replay_spec(shell)
    out = {"engine_version": ENGINE_VERSION, "method_version": f"{meth}/{METHOD_VERSION}", "business_hash": rs["business_hash"], "values": rs["values"]}
    return out


services.register("risk.get_positions", get_positions)
services.register("risk.reconciliation_status", reconciliation_status)
services.register("replay.risk", replay_risk)


services.register("risk.compute_margin", compute_margin)
services.register("risk.get_result", get_result)
services.register("risk.reconcile_clearing", reconcile_clearing)
services.register("risk.stress", stress)
