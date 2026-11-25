import threading
import time

from sqlalchemy import func, select
from tests.engines.compliance.conftest import CTX, P

from app.db.base import SessionLocal
from app.engines.compliance import cases as C
from app.engines.compliance import engine as E
from app.engines.compliance.models import CmpCase, CmpEvaluation


def test_concurrent_events_for_same_failure_yield_one_case_and_exact_count(tenant, up):
    errs: list = []

    def work(n):
        try:
            with SessionLocal() as s:
                for i in range(3):
                    E.evaluate(s, tenant, CTX, f"SNAP-{n}-{i}", "rsh", margin_result_id="MR-1")
                    s.commit()
        except Exception as e:  # noqa: BLE001
            errs.append(e)

    th = [threading.Thread(target=work, args=(n,)) for n in range(4)]
    [t.start() for t in th]
    [t.join() for t in th]
    assert not errs, errs
    with SessionLocal() as s:
        cases = s.execute(select(CmpCase).where(CmpCase.tenant_id == tenant)).scalars().all()
        assert len(cases) == 1 and cases[0].occurrence_count == 12                      # 12 distinct events aggregated, none lost
        assert s.execute(select(func.count()).select_from(CmpEvaluation).where(CmpEvaluation.tenant_id == tenant,
                                                                                 CmpEvaluation.status == "FAIL")).scalar_one() == 12


def test_concurrent_case_updates_exactly_one_wins(tenant, up):
    with SessionLocal() as s:
        cid = E.evaluate(s, tenant, CTX, "S0", "rsh", margin_result_id="MR-1")["cases"][0]["case_id"]
        s.commit()
        rev = C.get_case(s, tenant, cid).revision
    results: list = []

    def ack(uid):
        with SessionLocal() as s:
            try:
                C.transition(s, P(tenant, uid, "maker"), cid, "ACKNOWLEDGED", None, rev)
                s.commit()
                results.append("ok")
            except Exception as e:  # noqa: BLE001
                s.rollback()
                results.append(getattr(e, "code", type(e).__name__))

    th = [threading.Thread(target=ack, args=(f"u{i}",)) for i in range(5)]
    [t.start() for t in th]
    [t.join() for t in th]
    assert results.count("ok") == 1 and all(r in ("REVISION_CONFLICT", "ok") for r in results), results


def test_burst_of_events_no_silent_loss_and_measured_rate(tenant, up):
    n, t0 = 40, time.perf_counter()
    with SessionLocal() as s:
        for i in range(n):
            E.evaluate(s, tenant, {**CTX, "account_id": f"ACC-B-{i}"}, f"SB-{i}", "rsh", margin_result_id="MR-1")
        s.commit()
    dt = time.perf_counter() - t0
    with SessionLocal() as s:
        assert s.execute(select(func.count()).select_from(CmpCase).where(CmpCase.tenant_id == tenant)).scalar_one() == n   # one case per subject
    print(f"\nBURST: {n} evaluate() calls (2 controls each) in {dt:.2f}s = {n / dt:.1f} events/s (sqlite, single process)")
