"""Service registry: the ONLY sanctioned way engines call each other (no cross-engine table access).

Each engine registers its public service functions at import of `app.engines.<key>.services`.
A caller asking for a service that is not registered receives an explicit Unavailable error
(never a silent fallback or fabricated answer).

Frozen service names (contract v1; change only via versioned proposal accepted by lead):
  regulatory.resolve_rules(session, tenant_id, context, as_of, known_at) -> RuleResolution dict
  contracts.resolve_terms(session, tenant_id, netting_set_id, as_of, known_at, fact_snapshot_id) -> TermsResolution
  risk.compute_margin(session, tenant_id, context, snapshot_id, terms_hash, rule_set_hash, methodology) -> MarginResult
  risk.reconcile_clearing(session, tenant_id, file_id, snapshot_id)
  risk.stress(session, tenant_id, base_result_id, shock_spec)
  collateral.optimise(session, tenant_id, margin_result_id, inventory_snapshot_id, rule_set_hash, terms_hash, deadline, horizon)
  collateral.verify(session, tenant_id, proposal_id)
  collateral.reserve(session, tenant_id, proposal_id, approval_id)
  compliance.evaluate(session, tenant_id, context, snapshot_id, rule_set_hash, margin_result_id, allocation_id)
  compliance.recheck(session, tenant_id, case_id, snapshot_id)
  evidence.append(session, tenant_id, event)            (see app.engines.evidence.service)
  evidence.store_object(session, tenant_id, data, classification)
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from app.core.errors import Unavailable

_REGISTRY: dict[str, Callable[..., Any]] = {}


def register(name: str, fn: Callable[..., Any]) -> None:
    _REGISTRY[name] = fn


def is_registered(name: str) -> bool:
    return name in _REGISTRY


def call(name: str, *args, **kwargs):
    fn = _REGISTRY.get(name)
    if fn is None:
        raise Unavailable(f"Service '{name}' is not available in this build", code="SERVICE_NOT_IMPLEMENTED")
    return fn(*args, **kwargs)


def registered() -> list[str]:
    return sorted(_REGISTRY)
