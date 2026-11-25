"""S01 scheduled evaluation: explicit business clock, watermarks, missed-run detection, rule-change re-evaluation.

* The clock is ALWAYS the supplied `as_of` (replay mode has no implicit clock). Slot = the UTC business date of as_of.
* A slot is DUE once as_of >= slot 12:00Z + 2h grace (fictional close; configurable constants below, not an exchange schedule).
* A sweep evaluates every active SUPPORTED control with frequency DAILY for each subject in the population. Inputs come from the
  owning engines; stale inputs fail closed (UNKNOWN), so a sweep over stale margin results can never read as PASS.
* Missed runs are DETECTED (slot due, no DONE sweep job) and reported + recorded as evidence once per slot. They are never
  silently skipped and an empty alert list is never equated with compliance.
"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select

from app.core.errors import ValidationFailed
from app.db.base import new_id, utcnow
from app.engines.compliance import controls as K
from app.engines.compliance import engine as E
from app.engines.compliance.models import CmpControl, CmpEvaluation, CmpJob

SLOT_HOUR_UTC = 12
GRACE_HOURS = 2
LOOKBACK_DAYS = 30
SWEEP, MISSED = "SCHEDULED_SWEEP", "MISSED_RUN"
SYSTEM = {"id": "scheduler", "type": "SYSTEM", "role": None}


def slot_of(as_of: datetime) -> date:
    return as_of.astimezone(UTC).date()


def due_time(slot: date) -> datetime:
    return datetime(slot.year, slot.month, slot.day, SLOT_HOUR_UTC, tzinfo=UTC) + timedelta(hours=GRACE_HOURS)


def known_population(session, tenant_id: str) -> list[dict]:
    """Subjects previously evaluated (latest non-recheck evaluation per subject+market), with their last pinned inputs.
    Re-using last inputs is safe: freshness limits make stale ones evaluate to UNKNOWN, never PASS."""
    rows = session.execute(select(CmpEvaluation).where(CmpEvaluation.tenant_id == tenant_id, CmpEvaluation.is_recheck.is_(False))
                           .order_by(CmpEvaluation.as_of, CmpEvaluation.created_at)).scalars().all()
    latest: dict[tuple, CmpEvaluation] = {}
    for e in rows:
        latest[(e.subject_id, e.market_type)] = e
    out = []
    for (_, _), e in sorted(latest.items(), key=lambda kv: kv[0]):
        f = e.facts or {}
        out.append({"context": {k: f.get(k) for k in ("market_type", "venue", "account_id", "currency", "jurisdiction") if f.get(k)},
                    "snapshot_id": e.snapshot_id, "rule_set_hash": e.rule_set_hash, "margin_result_id": e.margin_result_id,
                    "allocation_id": e.allocation_id})
    return out


def run_sweep(session, tenant_id: str, as_of: datetime, population: list[dict] | None, actor: dict | None = None) -> dict:
    actor = actor or SYSTEM
    slot = slot_of(as_of)
    pop = population if population is not None else known_population(session, tenant_id)
    if not pop:
        raise ValidationFailed("no population: supply subjects or evaluate at least one subject first", field_errors={"population": "empty"})
    job = CmpJob(tenant_id=tenant_id, job_id=new_id(), kind=SWEEP, status="RUNNING", watermark_as_of=as_of, provenance="SYNTHETIC",
                 detail={"slot": slot.isoformat(), "as_of": as_of.isoformat()})
    session.add(job)
    session.flush()
    cid = new_id()
    n_eval, n_new, cases, counts = 0, 0, set(), {}
    try:
        for item in pop:
            ctx = {**item["context"], "as_of": as_of.isoformat(), "frequency": "DAILY", "full_sweep": True}
            r = E.evaluate(session, tenant_id, ctx, item.get("snapshot_id"), item.get("rule_set_hash"), item.get("margin_result_id"),
                           item.get("allocation_id"), actor_id=actor["id"], actor_type=actor["type"], actor_role=actor.get("role"),
                           correlation_id=cid, facts=item.get("facts"))
            n_eval += len(r["evaluations"])
            n_new += r["new_evaluations"]
            cases.update(c["case_id"] for c in r["cases"])
            for e in r["evaluations"]:
                counts[e["status"]] = counts.get(e["status"], 0) + 1
        job.status = "DONE"
    except Exception as exc:  # noqa: BLE001 - recorded, never silent
        job.status = "FAILED"
        job.detail = {**job.detail, "error": f"{type(exc).__name__}: {exc}"[:500]}
        job.finished_at = utcnow()
        session.flush()
        raise
    job.finished_at = utcnow()
    job.detail = {**job.detail, "subjects": len(pop), "evaluations": n_eval, "new_evaluations": n_new, "status_counts": counts,
                  "cases": sorted(cases), "correlation_id": cid}
    E._ev(session, tenant_id, actor, "SCHEDULED_SWEEP_COMPLETED", "job", job.job_id, 1, cid,
          {"slot": slot.isoformat(), "subjects": len(pop), "evaluations": n_eval, "status_counts": counts}, effective_at=as_of)
    session.flush()
    return {"job_id": job.job_id, "slot": slot.isoformat(), "status": job.status, "watermark_as_of": as_of.isoformat(),
            **{k: job.detail[k] for k in ("subjects", "evaluations", "new_evaluations", "status_counts", "cases", "correlation_id")}}


def schedule_status(session, tenant_id: str, as_of: datetime, record: bool = False, actor: dict | None = None) -> dict:
    """Watermark + missed-run detection against the explicit as_of. record=True writes one evidence event per newly missed slot."""
    daily = [c for c in session.execute(select(CmpControl).where(CmpControl.tenant_id == tenant_id, CmpControl.active.is_(True),
                                                                 CmpControl.support_status == "SUPPORTED",
                                                                 CmpControl.frequency == "DAILY")).scalars()]
    if not daily:
        return {"as_of": as_of.isoformat(), "daily_controls": 0, "due_slots": [], "completed_slots": [], "missed_slots": [],
                "watermark_as_of": None, "stale": False, "note": "no DAILY controls registered"}
    start = max(min(c.effective_from for c in daily).astimezone(UTC).date(), slot_of(as_of) - timedelta(days=LOOKBACK_DAYS))
    due, d = [], start
    while d <= slot_of(as_of):
        if as_of >= due_time(d):
            due.append(d)
        d += timedelta(days=1)
    jobs = session.execute(select(CmpJob).where(CmpJob.tenant_id == tenant_id, CmpJob.kind == SWEEP)).scalars().all()
    done = {j.detail.get("slot") for j in jobs if j.status == "DONE"}
    wm = max((j.watermark_as_of for j in jobs if j.status == "DONE"), default=None)
    missed = [x.isoformat() for x in due if x.isoformat() not in done]
    recorded: list[str] = []
    if record and missed:
        known = {j.detail.get("slot") for j in session.execute(select(CmpJob).where(CmpJob.tenant_id == tenant_id, CmpJob.kind == MISSED)).scalars()}
        for slot in missed:
            if slot in known:
                continue
            j = CmpJob(tenant_id=tenant_id, job_id=new_id(), kind=MISSED, status="MISSED", watermark_as_of=as_of, provenance="SYNTHETIC",
                       detail={"slot": slot, "detected_as_of": as_of.isoformat()}, finished_at=utcnow())
            session.add(j)
            session.flush()
            E._ev(session, tenant_id, actor or SYSTEM, "MISSED_RUN_DETECTED", "job", j.job_id, 1, j.job_id,
                  {"slot": slot, "detected_as_of": as_of.isoformat()}, effective_at=as_of,
                  rationale="Scheduled sweep slot is due but no completed sweep exists")
            recorded.append(slot)
    stale = wm is None or (as_of - wm) > timedelta(hours=24 + GRACE_HOURS)
    return {"as_of": as_of.isoformat(), "daily_controls": len(daily), "due_slots": [x.isoformat() for x in due],
            "completed_slots": sorted(s for s in done if s), "missed_slots": missed, "newly_recorded_missed": recorded,
            "watermark_as_of": wm.isoformat() if wm else None, "stale": stale,
            "rule": f"slot due at {SLOT_HOUR_UTC:02d}:00Z + {GRACE_HOURS}h grace (fictional close; not an exchange schedule); lookback {LOOKBACK_DAYS}d",
            "note": "Absence of alerts is not compliance: a stale watermark or missed slot means controls were NOT evaluated."}


def on_rule_activated(session, envelope: dict) -> None:
    """Outbox consumer (idempotent via the inbox): a rule revision became active -> re-evaluate ONLY the subjects previously
    evaluated by controls bound to that rule_key. Controls pinned to an older revision then evaluate UNKNOWN
    (CONTROL_STALE_VS_ACTIVE_RULE) until a reviewed control revision supersedes them; unaffected controls are not touched."""
    tenant_id = envelope["tenant_id"]
    key = (envelope.get("payload") or {}).get("rule_key")
    if not key:
        return
    as_of = E.ts(envelope["effective_at"], "effective_at")
    keys = sorted({c.control_key for c in session.execute(select(CmpControl).where(
        CmpControl.tenant_id == tenant_id, CmpControl.rule_key == key, CmpControl.active.is_(True),
        CmpControl.support_status == "SUPPORTED")).scalars()})
    for ck in keys:
        K.reevaluate_affected(session, tenant_id, ck, as_of, SYSTEM, force=True)
