"""Persist surveillance models/scores and open INVESTIGATION cases. Never creates control evaluations (G22)."""
from __future__ import annotations

from datetime import timedelta

import numpy as np
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.core.errors import Conflict, NotFound
from app.core.events import emit
from app.db.base import lock_for_update, new_id, utcnow
from app.engines.compliance import engine as E
from app.engines.compliance import surveillance as S
from app.engines.compliance.models import CmpCase, CmpJob, CmpModelVersion, CmpScore


def _fs(o):
    """Floats are analytics-only; the hashed evidence record carries them as fixed-precision strings."""
    if isinstance(o, float):
        return format(o, ".6f")
    if isinstance(o, dict):
        return {k: _fs(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_fs(v) for v in o]
    return o


DISCLAIMER = ("Statistical anomaly = investigation signal only. It is not a finding of misconduct, does not change any "
              "deterministic control result, and triggers no trade blocking or regulatory report.")


def train_and_persist(session, tenant_id: str, actor: dict, seed: int = 42, n_accounts: int = 20, n_days: int = 240) -> dict:
    cid = new_id()
    job = CmpJob(tenant_id=tenant_id, job_id=new_id(), kind="SURVEILLANCE_TRAIN", status="RUNNING", provenance="SYNTHETIC")
    session.add(job)
    session.flush()
    res = S.run_experiment(seed, n_accounts, n_days)
    ds, Z, score, th = res["dataset"], res["Z"], res["score"], res["threshold"]
    ver = (session.execute(select(func.max(CmpModelVersion.version)).where(CmpModelVersion.tenant_id == tenant_id,
                                                                           CmpModelVersion.name == S.MODEL_NAME)).scalar_one() or 0) + 1
    card = S.model_card(res)
    drift_flag = res["drift_ratio"] > 2.0
    metrics = {"primary": res["primary"], "challenger": res["challenger"], "sensitivity": res["sensitivity"],
               "drift_ratio": res["drift_ratio"], "drift_suspected": drift_flag}
    def _mk(version: int) -> CmpModelVersion:
        return CmpModelVersion(tenant_id=tenant_id, model_id=new_id(), name=S.MODEL_NAME, version=version, status="ACTIVE", seed=seed,
                         feature_definitions=card["features"], split=res["split"], training_data_hash=res["training_data_hash"],
                         parameters={"threshold": th, "normalisation": "median/MAD per account x regime (train only)",
                                     "n_accounts": n_accounts, "n_days": n_days}, threshold=float(th), metrics=metrics, model_card=card,
                         provenance="SYNTHETIC", source_ref="generator:surveillance/1")
    for attempt in range(6):  # concurrent trainings race for the next version number: loser takes the next free one
        mv = _mk(ver)
        try:
            with session.begin_nested():
                session.add(mv)
                session.flush()
            break
        except IntegrityError:
            if mv in session:
                session.expunge(mv)
            ver = (session.execute(select(func.max(CmpModelVersion.version)).where(
                CmpModelVersion.tenant_id == tenant_id, CmpModelVersion.name == S.MODEL_NAME)).scalar_one() or 0) + 1
    else:
        raise Conflict("could not allocate a model version (concurrent trainings)", code="MODEL_VERSION_CONFLICT")
    flagged_by_acct: dict[int, list[int]] = {}
    for part in ("validation", "holdout"):
        for i in np.where(res["masks"][part])[0]:
            fl = bool(score[i] >= th)
            med = res["params"][(int(ds.account_idx[i]), int(ds.regime[i]))][0]
            session.add(CmpScore(tenant_id=tenant_id, score_id=new_id(), model_id=mv.model_id, split=part, account_id=ds.accounts[ds.account_idx[i]],
                                 obs_day=int(ds.day[i]), score=float(score[i]), flagged=fl,
                                 top_features=S.top_features(Z[i], ds.X[i], med) if fl else [], provenance="SYNTHETIC"))
            if fl and part == "holdout":
                flagged_by_acct.setdefault(int(ds.account_idx[i]), []).append(int(i))
    session.flush()
    E._ev(session, tenant_id, actor, "SURVEILLANCE_MODEL_TRAINED", "cmp_model", mv.model_id, ver, cid,
          _fs({"seed": seed, "threshold": float(th), "training_data_hash": res["training_data_hash"], "holdout": res["primary"]["holdout"]}),
          inputs=[res["training_data_hash"]], effective_at=S.obs_date(int(ds.day.max())))
    cases = []
    last_day = int(ds.day[res["masks"]["holdout"]].max())
    as_of = S.obs_date(last_day)
    for a, idxs in sorted(flagged_by_acct.items()):
        acct = ds.accounts[a]
        key = f"SURV-ANOMALY|{acct}|m{ver}"
        c = CmpCase(tenant_id=tenant_id, case_id=new_id(), occurrence_key=key, kind="ANOMALY_INVESTIGATION", control_key="SURV-ANOMALY",
                    rule_version=ver, family="SURVEILLANCE", subject_id=acct, market_type="ETD_DOMESTIC",
                    title=f"Investigation signal (not a finding): {len(idxs)} high-ranking account-day(s) for {acct}",
                    state="OPEN", severity="MEDIUM" if len(idxs) > 1 else "LOW", last_status="ANOMALY_SIGNAL",
                    opened_as_of=as_of, last_as_of=as_of, due_at=as_of + timedelta(hours=E.SLA_HOURS["MEDIUM"]),
                    occurrence_count=len(idxs), eval_context={"model_id": mv.model_id, "disclaimer": DISCLAIMER, "correlation_id": cid},
                    provenance="SYNTHETIC", source_ref=f"model:{mv.model_id}")
        session.add(c)
        session.flush()
        E.add_case_event(session, tenant_id, c.case_id, "OPENED", actor["id"], to="OPEN", as_of=as_of,
                         comment=DISCLAIMER, data={"model_id": mv.model_id, "max_score": float(max(score[i] for i in idxs))})
        E._ev(session, tenant_id, actor, "CASE_OPENED", "cmp_case", c.case_id, 1, cid,
              {"kind": "ANOMALY_INVESTIGATION", "model_id": mv.model_id, "subject_id": acct, "observations": len(idxs)}, effective_at=as_of)
        emit(session, event_type="CaseOpened", tenant_id=tenant_id, correlation_id=cid, aggregate_id=c.case_id, actor_id=actor["id"],
             actor_type=actor["type"], payload={"kind": "ANOMALY_INVESTIGATION"}, effective_at=as_of)
        cases.append(c.case_id)
    job.status, job.finished_at, job.detail = "DONE", utcnow(), {"model_id": mv.model_id, "cases_opened": len(cases)}
    session.flush()
    return {"model_id": mv.model_id, "version": ver, "cases_opened": len(cases), "case_ids": cases, "metrics": metrics, "disclaimer": DISCLAIMER}


def model_out(m: CmpModelVersion) -> dict:
    return {"model_id": m.model_id, "name": m.name, "version": m.version, "status": m.status, "seed": m.seed, "threshold": m.threshold,
            "split": m.split, "training_data_hash": m.training_data_hash, "metrics": m.metrics, "model_card": m.model_card,
            "created_at": m.created_at.isoformat(), "provenance": m.provenance, "disclaimer": DISCLAIMER}


def get_model(session, tenant_id: str, model_id: str) -> CmpModelVersion:
    m = session.execute(select(CmpModelVersion).where(CmpModelVersion.tenant_id == tenant_id, CmpModelVersion.model_id == model_id)).scalar_one_or_none()
    if m is None:
        raise NotFound("Model not found")
    return m


def set_model_status(session, tenant_id: str, model_id: str, status: str, actor: dict) -> CmpModelVersion:
    get_model(session, tenant_id, model_id)  # 404 for foreign tenants
    q = select(CmpModelVersion).where(CmpModelVersion.tenant_id == tenant_id, CmpModelVersion.model_id == model_id)
    m = session.execute(lock_for_update(q).execution_options(populate_existing=True)).scalar_one()
    if m.status == status:
        return m  # idempotent: no duplicate evidence for a no-op flip
    m.status = status
    E._ev(session, tenant_id, actor, f"SURVEILLANCE_MODEL_{status}", "cmp_model", m.model_id, m.version, m.model_id, {"status": status})
    return m


def surveillance_view(session, tenant_id: str, limit: int = 50) -> dict:
    m = session.execute(select(CmpModelVersion).where(CmpModelVersion.tenant_id == tenant_id, CmpModelVersion.name == S.MODEL_NAME)
                        .order_by(CmpModelVersion.version.desc())).scalars().first()
    if m is None:
        return {"model": None, "scores": [], "disclaimer": DISCLAIMER, "note": "No surveillance model has been trained for this tenant yet."}
    rows = session.execute(select(CmpScore).where(CmpScore.tenant_id == tenant_id, CmpScore.model_id == m.model_id, CmpScore.split == "holdout")
                           .order_by(CmpScore.score.desc(), CmpScore.score_id).limit(max(1, min(limit, 500)))).scalars().all()
    series = session.execute(select(CmpScore.account_id, CmpScore.obs_day, CmpScore.score, CmpScore.flagged).where(
        CmpScore.tenant_id == tenant_id, CmpScore.model_id == m.model_id).order_by(CmpScore.account_id, CmpScore.obs_day)).all()
    return {"model": model_out(m) if m.status == "ACTIVE" else {**model_out(m), "note": "DISABLED: no new cases are created"},
            "scores": [{"account_id": r.account_id, "obs_day": r.obs_day, "score": round(r.score, 3), "flagged": r.flagged, "top_features": r.top_features}
                       for r in rows],
            "series": [{"account_id": a, "obs_day": d, "score": round(s, 3), "flagged": f} for a, d, s, f in series],
            "disclaimer": DISCLAIMER}
