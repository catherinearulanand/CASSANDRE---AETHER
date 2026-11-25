"""Public service interface of the compliance engine (frozen contract v1)."""
from __future__ import annotations

from app.core import services
from app.engines.compliance import cases, engine


def _evaluate(session, tenant_id, context, snapshot_id, rule_set_hash, margin_result_id=None, allocation_id=None, **kw):
    return engine.evaluate(session, tenant_id, context, snapshot_id, rule_set_hash, margin_result_id, allocation_id, **kw)


def _recheck(session, tenant_id, case_id, snapshot_id, **kw):
    return cases.recheck(session, tenant_id, case_id, snapshot_id, **kw)


def _open_or_update_case(session, tenant_id, evaluation_id, actor_id="system"):
    from sqlalchemy import select

    from app.core.errors import NotFound
    from app.engines.compliance.models import CmpEvaluation
    ev = session.execute(select(CmpEvaluation).where(CmpEvaluation.tenant_id == tenant_id, CmpEvaluation.evaluation_id == evaluation_id)).scalar_one_or_none()
    if ev is None:
        raise NotFound("Evaluation not found")
    c = engine.open_or_update_case(session, tenant_id, ev, {"id": actor_id, "type": "SYSTEM", "role": None})
    return engine.case_out(c) if c else None


from app.core.events import subscribe  # noqa: E402
from app.engines.compliance import schedule  # noqa: E402

subscribe("RuleActivated", "compliance.reevaluate", schedule.on_rule_activated)
services.register("compliance.evaluate", _evaluate)
services.register("compliance.recheck", _recheck)
services.register("compliance.open_or_update_case", _open_or_update_case)
