"""E03 read-only replay.

Contract for engines (frozen proposal, see docs/methodologies/evidence.md): an evidence event that is replayable carries
    payload["replay"] = {"replayer": "replay.<engine>", "engine_version": str, "method_version": str,
                         "business_hash": str, "inputs": {...persisted input refs...},
                         optional "values": {k: decimal-string}, optional "tolerance": decimal-string}
and the engine registers services.register("replay.<engine>", fn) with
    fn(session, tenant_id, spec) -> {"engine_version", "method_version", "business_hash", optional "values"}.
The business hash must exclude run IDs, timestamps and audit sequence numbers.

Rules enforced here:
  * missing versions / unregistered replayer / version drift => NOT_REPLAYABLE (never silently use the current version)
  * the replayer runs inside a SAVEPOINT that is ALWAYS rolled back => replay cannot create reservations or any write
  * a cached REPLAY-mode extraction can be replayed but is NOT generative determinism (reported in `caveats`)
  * the replay itself is recorded separately (ev_replays + REPLAY_COMPLETED event)
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

from sqlalchemy import select

from app.core import services
from app.db.base import new_id
from app.engines.evidence.models import EvEvent, EvReplay
from app.engines.evidence.service import append


def _compare(spec: dict, out: dict) -> tuple[str, dict]:
    if out.get("business_hash") == spec.get("business_hash"):
        return "MATCH", {}
    tol, rv, ov = spec.get("tolerance"), spec.get("values"), out.get("values")
    if tol is not None and isinstance(rv, dict) and isinstance(ov, dict):
        try:
            t = Decimal(str(tol))
            first = next((k for k in sorted(set(rv) | set(ov)) if k not in rv or k not in ov
                          or abs(Decimal(str(rv[k])) - Decimal(str(ov[k]))) > t), None)
        except InvalidOperation:
            first = "values"
        if first is None:
            return "MATCH_WITHIN_TOLERANCE", {"tolerance": str(tol)}
        return "DIVERGED", {"first_divergence": first, "recorded": rv.get(first), "replayed": ov.get(first), "tolerance": str(tol)}
    return "DIVERGED", {"first_divergence": "business_hash", "recorded": spec.get("business_hash"), "replayed": out.get("business_hash"),
                        "reason": "canonical business hash differs (bitwise comparison; no tolerance declared)"}


def replay_run(session, tenant_id: str, run_id: str, correlation_id: str, actor_id: str) -> dict:
    events = session.execute(select(EvEvent).where(EvEvent.tenant_id == tenant_id, EvEvent.correlation_id == correlation_id)
                             .order_by(EvEvent.sequence)).scalars().all()
    items, caveats = [], []
    for ev in events:
        spec = (ev.payload or {}).get("replay")
        if not isinstance(spec, dict):
            continue
        item = {"sequence": ev.sequence, "action": ev.action, "engine": ev.engine}
        if ev.source_mode != "MANUAL" and ev.source_mode in ("REPLAY", "LIVE_MODEL") and ev.engine in ("regulatory", "contracts"):
            caveats.append(f"seq {ev.sequence}: extraction replayed from cache ({ev.source_mode}); this is NOT generative determinism")
        if not spec.get("engine_version") or not spec.get("method_version"):
            item.update(status="NOT_REPLAYABLE", reason="MISSING_METHOD_VERSION")
        elif not services.is_registered(spec.get("replayer", "")):
            item.update(status="NOT_REPLAYABLE", reason=f"REPLAYER_NOT_AVAILABLE ({spec.get('replayer')})")
        else:
            sp = session.begin_nested()
            try:
                out = services.call(spec["replayer"], session, tenant_id, spec)
            except Exception as exc:  # noqa: BLE001 - reported, never silent
                item.update(status="NOT_REPLAYABLE", reason=f"REPLAYER_ERROR {type(exc).__name__}: {exc}"[:300])
                out = None
            finally:
                sp.rollback()  # ALWAYS: read-only guarantee
            if out is not None:
                if out.get("engine_version") != spec["engine_version"] or out.get("method_version") != spec["method_version"]:
                    item.update(status="NOT_REPLAYABLE", reason="VERSION_MISMATCH",
                                recorded={"engine_version": spec["engine_version"], "method_version": spec["method_version"]},
                                available={"engine_version": out.get("engine_version"), "method_version": out.get("method_version")})
                else:
                    st, detail = _compare(spec, out)
                    item.update(status=st, **detail)
        items.append(item)
    if not items:
        status = "NOTHING_TO_REPLAY"
    elif any(i["status"] == "NOT_REPLAYABLE" for i in items):
        status = "NOT_REPLAYABLE"
    elif any(i["status"] == "DIVERGED" for i in items):
        status = "DIVERGED"
    elif any(i["status"] == "MATCH_WITHIN_TOLERANCE" for i in items):
        status = "MATCH_WITHIN_TOLERANCE"
    else:
        status = "MATCH"
    first_div = next((i for i in items if i["status"] in ("DIVERGED", "NOT_REPLAYABLE")), None)
    result = {"status": status, "items": items, "first_divergence": first_div, "caveats": caveats,
              "read_only": True, "events_considered": len(events)}
    rid = new_id()
    session.add(EvReplay(replay_id=rid, tenant_id=tenant_id, run_id=run_id, actor_id=actor_id, status=status, result=result))
    session.flush()
    append(session, tenant_id, engine="evidence", action="REPLAY_COMPLETED", actor_id=actor_id, actor_type="USER",
           entity_type="replay", entity_id=rid, correlation_id=correlation_id,
           payload={"run_id": run_id, "status": status, "items": len(items)})
    return {"replay_id": rid, "run_id": run_id, **result}
