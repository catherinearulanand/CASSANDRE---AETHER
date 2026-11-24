"""Frozen service contract v1 for A1 (see docs/agent-handoffs/CONTRACTS.md)."""
from app.core import services
from app.engines.regulatory import resolve


def resolve_rules(session, tenant_id, context, as_of, known_at=None, hypothetical_rule_ids=None):
    return resolve.resolve_rules(session, tenant_id, context, as_of, known_at, hypothetical_rule_ids)


def rule_parameters(session, tenant_id, rule_set_hash):
    return resolve.rule_parameters(session, tenant_id, rule_set_hash)


services.register("regulatory.resolve_rules", resolve_rules)
services.register("regulatory.rule_parameters", rule_parameters)
