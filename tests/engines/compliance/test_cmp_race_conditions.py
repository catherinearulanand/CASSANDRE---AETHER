"""Real-thread regression tests for read-check-write races (class of bug first found on PostgreSQL).
Each test asserts an invariant that holds only with lock-before-check; see docs/methodologies/compliance.md (Concurrency)."""
import threading
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from app.core.errors import ApiError
from app.db.base import SessionLocal
from app.db.models_core import ApprovalRow
from app.engines.compliance import cases as C
from app.engines.compliance import engine as E
from app.engines.compliance import schedule as SCH
from app.engines.compliance import surv_service as SV
from app.engines.compliance.models import CmpCase, CmpEvaluation, CmpJob, CmpModelVersion
from app.engines.evidence.models import EvEvent
from tests.engines.compliance.conftest import CTX, P

ASOF = datetime(2026, 8, 17, 4, 0, tzinfo=UTC)


def run_threads(n, fn):
    out, errs = [], []

    def w(i):
        try:
            out.append(fn(i))
        except ApiError as e:
            out.append("API:" + e.code)
        except Exception as e:  # noqa: BLE001 - an unexpected exception type is itself a finding
            errs.append(f"{type(e).__name__}: {e}"[:240])

    th = [threading.Thread(target=w, args=(i,)) for i in range(n)]
    [t.start() for t in th]
    [t.join() for t in th]
    return out, errs


def open_case_id(tenant):
    with SessionLocal() as s:
        cid = E.evaluate(s, tenant, CTX, "S-RACE", "rsh", margin_result_id="MR-1")["cases"][0]["case_id"]
        s.commit()
    return cid


def test_two_checkers_cannot_both_approve_the_same_waiver(tenant, up):
    maker = P(tenant, "u-maker", "maker")
    with SessionLocal() as s:
        w = C.create_waiver(s, maker, {"control_key": "CMP-COV-ETD-INR", "subject_id": "ACC-DEMO-NSE-001", "reason": "temporary exemption approved by desk head",
                                       "valid_from": ASOF.isoformat(), "valid_to": (ASOF + timedelta(days=3)).isoformat()})
        s.commit()
        wid = w.waiver_id

    def approve(i):
        with SessionLocal() as s:
            try:
                C.approve_waiver(s, P(tenant, f"u-checker-{i}", "checker"), wid, "APPROVE", "independent review: scope and expiry are narrow")
                s.commit()
                return "ok"
            except Exception:
                s.rollback()
                raise

    out, errs = run_threads(5, approve)
    assert not errs, errs
    assert out.count("ok") == 1, out
    with SessionLocal() as s:
        n = s.execute(select(func.count()).select_from(ApprovalRow).where(ApprovalRow.tenant_id == tenant, ApprovalRow.target_id == wid,
                                                                          ApprovalRow.action_type == "APPROVE_WAIVER")).scalar_one()
        ev = s.execute(select(func.count()).select_from(EvEvent).where(EvEvent.tenant_id == tenant, EvEvent.entity_id == wid,
                                                                       EvEvent.action == "WAIVER_APPROVED")).scalar_one()
    assert n == 1 and ev == 1


def test_concurrent_dismissal_approvals_dismiss_exactly_once(tenant, up):
    cid = open_case_id(tenant)
    with SessionLocal() as s:
        maker = P(tenant, "u-maker", "maker")
        c = C.get_case(s, tenant, cid)
        C.transition(s, maker, cid, "DISMISSED", "duplicate feed, false alarm", c.revision)    # maker REQUESTS dismissal
        s.commit()
        rev = C.get_case(s, tenant, cid).revision

    def approve(i):
        with SessionLocal() as s:
            try:
                C.transition(s, P(tenant, f"u-checker-{i}", "checker"), cid, "DISMISSED", "independent confirmation of duplicate", rev)
                s.commit()
                return "ok"
            except Exception:
                s.rollback()
                raise

    out, errs = run_threads(5, approve)
    assert not errs, errs
    assert out.count("ok") == 1, out
    with SessionLocal() as s:
        n = s.execute(select(func.count()).select_from(ApprovalRow).where(ApprovalRow.tenant_id == tenant, ApprovalRow.target_id == cid,
                                                                          ApprovalRow.action_type == "DISMISS_CASE")).scalar_one()
        assert n == 1 and C.get_case(s, tenant, cid).state == "DISMISSED"


def test_concurrent_rechecks_do_not_lose_revision_increments(tenant, up):
    cid = open_case_id(tenant)
    with SessionLocal() as s:
        start = C.get_case(s, tenant, cid).revision

    def recheck(i):
        with SessionLocal() as s:
            try:
                C.recheck(s, tenant, cid, f"S-RC-{i}", as_of=(ASOF + timedelta(minutes=i + 1)).isoformat(), actor_id=f"u-{i}")
                s.commit()
                return "ok"
            except Exception:
                s.rollback()
                raise

    out, errs = run_threads(4, recheck)
    assert not errs, errs
    assert out.count("ok") == 4
    with SessionLocal() as s:
        c = C.get_case(s, tenant, cid)
        assert c.revision == start + 4, (start, c.revision)          # a lost update would leave fewer increments
        n = s.execute(select(func.count()).select_from(CmpEvaluation).where(CmpEvaluation.tenant_id == tenant, CmpEvaluation.is_recheck.is_(True))).scalar_one()
        assert n == 4


def test_concurrent_sweeps_for_the_same_slot_never_duplicate_evaluations_or_crash(tenant, up):
    up.extra = {"snapshot_as_of": "2026-08-17T03:00:00Z"}
    pop = [{"context": {k: CTX[k] for k in ("market_type", "venue", "account_id", "currency")}, "snapshot_id": "S-SWP", "rule_set_hash": "h",
            "margin_result_id": "MR-1"}]

    def sweep(i):
        with SessionLocal() as s:
            try:
                r = SCH.run_sweep(s, tenant, ASOF, pop)
                s.commit()
                return r["status"]
            except Exception:
                s.rollback()
                raise

    out, errs = run_threads(4, sweep)
    assert not errs, errs
    assert out == ["DONE"] * 4
    with SessionLocal() as s:
        keys = s.execute(select(CmpEvaluation.eval_key).where(CmpEvaluation.tenant_id == tenant)).scalars().all()
        assert len(keys) == len(set(keys))
        n_cov = s.execute(select(func.count()).select_from(CmpEvaluation).where(CmpEvaluation.tenant_id == tenant, CmpEvaluation.control_key == "CMP-COV-ETD-INR")).scalar_one()
        assert n_cov == 1                                              # identical inputs + as_of => ONE evaluation, not four
        assert s.execute(select(func.count()).select_from(CmpCase).where(CmpCase.tenant_id == tenant, CmpCase.control_key == "CMP-COV-ETD-INR")).scalar_one() == 1
        assert s.execute(select(func.count()).select_from(CmpJob).where(CmpJob.tenant_id == tenant, CmpJob.kind == SCH.SWEEP, CmpJob.status == "DONE")).scalar_one() == 4


def test_concurrent_model_disable_records_one_evidence_event(tenant):
    with SessionLocal() as s:
        out = SV.train_and_persist(s, tenant, {"id": "u-co", "type": "USER", "role": "compliance_officer"}, 42, 8, 120)
        s.commit()
        mid = out["model"]["model_id"] if "model" in out else out["model_id"]

    def disable(i):
        with SessionLocal() as s:
            try:
                SV.set_model_status(s, tenant, mid, "DISABLED", {"id": f"u-{i}", "type": "USER", "role": "compliance_officer"})
                s.commit()
                return "ok"
            except Exception:
                s.rollback()
                raise

    res, errs = run_threads(4, disable)
    assert not errs, errs
    with SessionLocal() as s:
        n = s.execute(select(func.count()).select_from(EvEvent).where(EvEvent.tenant_id == tenant, EvEvent.action == "SURVEILLANCE_MODEL_DISABLED")).scalar_one()
    assert n == 1, n


def test_concurrent_trainings_get_unique_model_versions(tenant):
    def train(i):
        with SessionLocal() as s:
            try:
                r = SV.train_and_persist(s, tenant, {"id": f"u-{i}", "type": "USER", "role": "compliance_officer"}, 42 + i, 8, 120)
                s.commit()
                return "ok"
            except Exception:
                s.rollback()
                raise

    out, errs = run_threads(3, train)
    assert not errs, errs
    assert out.count("ok") == 3
    with SessionLocal() as s:
        vers = sorted(s.execute(select(CmpModelVersion.version).where(CmpModelVersion.tenant_id == tenant)).scalars().all())
    assert vers == [1, 2, 3], vers
