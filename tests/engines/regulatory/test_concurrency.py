"""Concurrent review of the same rule revision: exactly one approval may win (lost-update regression, found on PostgreSQL)."""
import threading

from sqlalchemy import select

from app.core.errors import ApiError
from app.db.base import SessionLocal
from app.db.models_core import ApprovalRow
from app.engines.regulatory import registry
from tests.engines.regulatory.helpers import P


def test_concurrent_approvals_of_same_revision_exactly_one_wins(env):
    r = env.draft()
    registry.submit(env.db, env.maker, r.rule_id, "ready", None)
    env.db.commit()
    rev = registry.get_rule(env.db, env.tenant, r.rule_id).revision
    results: list = []

    def approve(n):
        with SessionLocal() as s:
            try:
                registry.review(s, P(f"checker-{n}", env.tenant, "checker"), r.rule_id, "APPROVE", "independent review ok", rev)
                s.commit()
                results.append("ok")
            except ApiError as e:
                s.rollback()
                results.append(e.code)
            except Exception as e:  # noqa: BLE001
                s.rollback()
                results.append(type(e).__name__)

    th = [threading.Thread(target=approve, args=(i,)) for i in range(5)]
    [t.start() for t in th]
    [t.join() for t in th]
    assert results.count("ok") == 1, results
    with SessionLocal() as s:
        approvals = s.execute(select(ApprovalRow).where(ApprovalRow.target_id == r.rule_id, ApprovalRow.decision == "APPROVED")).scalars().all()
    assert len(approvals) == 1
