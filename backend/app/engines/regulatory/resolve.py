"""R03/R04: bitemporal applicability resolver. service: resolve_rules / rule_parameters (frozen contract v1).

Returns applicable / excluded / unknown / conflicts + trace. UNKNOWN never counts as applicable.
Rules of different rank are ordered by an APPROVED precedence_rank (lower wins) - never a model-selected precedence.
Equal-rank incompatible mandates => CONFLICT (neither applies). additive_stricter rules coexist.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import select

from app.core.canonical import domain_hash
from app.core.errors import NotFound, ValidationFailed
from app.engines.regulatory import dsl
from app.engines.regulatory.models import RegRule, RegRuleSet

REQUIRED_CONTEXT = ("market_type", "venue", "jurisdiction", "entity_category", "instrument_class", "account_id", "currency")


def _aware(dt: datetime | None, name: str):
    if dt is None:
        return None
    if dt.tzinfo is None:
        raise ValidationFailed(f"{name} must be timezone-aware", field_errors={name: "naive datetime rejected"})
    return dt


def in_force(db, tenant_id: str, as_of: datetime, known_at: datetime | None) -> dict[str, RegRule]:
    """Latest version of each key that (a) was recorded/activated by known_at, (b) has effective_from <= as_of,
    (c) is not withdrawn as known at known_at, and whose effective interval still covers as_of.
    Superseded versions stay visible for earlier as_of/known_at => historical replay is unchanged."""
    rows = db.execute(select(RegRule).where(RegRule.tenant_id == tenant_id, RegRule.activated_at.is_not(None),
                                            RegRule.status.in_(("SCHEDULED", "ACTIVE", "SUPERSEDED", "WITHDRAWN")))).scalars().all()
    by_key: dict[str, list[RegRule]] = {}
    for r in rows:
        if known_at is not None and r.activated_at > known_at:
            continue
        if r.withdrawn_at is not None and (known_at is None or r.withdrawn_at <= known_at):
            continue
        if r.effective_from > as_of:
            continue
        by_key.setdefault(r.rule_key, []).append(r)
    out = {}
    for k, vs in by_key.items():
        top = max(vs, key=lambda x: x.version)
        if top.effective_to is None or as_of < top.effective_to:
            out[k] = top
    return out


def _entry(r: RegRule, hypothetical: bool, trace) -> dict:
    return {"rule_id": r.rule_id, "rule_key": r.rule_key, "version": r.version, "rule_hash": r.content_hash,
            "dsl": {"scope": r.scope, "condition": r.condition}, "authority_type": r.authority_type, "parameters": r.parameters,
            "resolved_dsl": {"scope": dsl.inline_params(r.scope, r.parameters), "condition": dsl.inline_params(r.condition, r.parameters)},
            "source_spans": list(r.span_ids), "effective_from": r.effective_from.isoformat(),
            "effective_to": r.effective_to.isoformat() if r.effective_to else None, "status": r.status,
            "conflict_group": r.conflict_group, "precedence_rank": r.precedence_rank, "hypothetical": hypothetical, "trace": trace}


def _prepare(db, tenant_id: str, as_of: datetime, known_at: datetime | None, hypothetical_rule_ids: list[str] | None):
    """Everything that does NOT depend on the account context: the in-force rules (+ counterfactual overlay), their static
    entry parts and their additive-stricter flags. Done ONCE per resolve_rules call and once per batch."""
    chosen = in_force(db, tenant_id, as_of, known_at)
    hyp_ids = set()
    for hid in hypothetical_rule_ids or []:
        r = db.get(RegRule, hid)
        if r is None or r.tenant_id != tenant_id:
            raise NotFound("Hypothetical rule not found")
        if not r.compile_report.get("ok"):
            raise ValidationFailed(f"Hypothetical rule {r.rule_key} v{r.version} does not compile",
                                   field_errors={"hypothetical_rule_ids": "non-compiling rule"})
        chosen[r.rule_key] = r  # counterfactual overlay replaces the in-force version of the same key
        hyp_ids.add(r.rule_id)
    static = {}
    for key in sorted(chosen):
        r = chosen[key]
        static[key] = (r, r.rule_id in hyp_ids, _entry(r, r.rule_id in hyp_ids, None), bool(r.additive_stricter),
                       sorted(dsl.node_dependencies(r.scope, r.parameters)), dsl.normalise_params(r.parameters)[0])
    return {"hyp": bool(hyp_ids), "static": static, "pinned": set()}


def _resolve_one(db, tenant_id: str, prep: dict, context: dict, as_of: datetime, known_at: datetime | None) -> dict:
    ctx = dict(context)
    ctx.setdefault("as_of_date", as_of.date().isoformat())
    applicable, excluded, unknown = [], [], []
    for key, (r, hyp, base, _add, deps, norm) in prep["static"].items():
        s, trace = dsl.evaluate(r.scope, ctx, r.parameters, norm)
        if r.effective_from > as_of:  # hypothetical future revision evaluated at as_of: carry the date explicitly
            trace.append({"depth": 0, "op": "NOTE", "result": "TRUE", "detail": f"hypothetical effective_from {r.effective_from.isoformat()} is after as_of; applied as counterfactual"})
        e = dict(base) | {"trace": trace}
        if s == "TRUE":
            applicable.append(e)
        elif s == "FALSE":
            excluded.append(e | {"reason": "SCOPE_NOT_MATCHED"})
        else:
            unknown.append(e | {"reason": "SCOPE_UNKNOWN_MISSING_CONTEXT", "missing_context": [f for f in deps if ctx.get(f) is None]})
    additive = {base["rule_id"]: add for (_r, _h, base, add, _d, _n) in prep["static"].values()}
    conflicts, final = [], []
    groups: dict[str, list[dict]] = {}
    for e in applicable:
        (groups.setdefault(e["conflict_group"], []) if e["conflict_group"] else final).append(e)
    for g, es in sorted(groups.items()):
        if len(es) == 1:
            final += es
            continue
        top = min((e["precedence_rank"] if e["precedence_rank"] is not None else 10**9) for e in es)
        lead = [e for e in es if (e["precedence_rank"] if e["precedence_rank"] is not None else 10**9) == top]
        for e in es:
            if e not in lead:
                excluded.append(e | {"reason": "LOWER_PRECEDENCE_BY_APPROVED_RANK", "winner_rank": top})
        stricter_ok = all(additive.get(e["rule_id"], False) for e in lead)
        if len(lead) > 1 and not stricter_ok and _incompatible(lead):
            conflicts.append({"conflict_group": g, "rule_keys": [e["rule_key"] for e in lead],
                              "reason": "INCOMPATIBLE_MANDATES_EQUAL_RANK: human review/approved hierarchy required", "rules": lead})
        else:
            final += lead
    members = sorted(({"rule_key": e["rule_key"], "version": e["version"], "rule_hash": e["rule_hash"], "hypothetical": e["hypothetical"],
                       "parameters": e["parameters"]} for e in final), key=lambda m: (m["rule_key"], m["version"]))
    is_hyp = prep["hyp"]
    rsh = domain_hash("aether.reg.ruleset.v1", [{k: m[k] for k in ("rule_key", "version", "rule_hash", "hypothetical")} for m in members])
    if rsh not in prep["pinned"]:
        if db.get(RegRuleSet, (rsh, tenant_id)) is None:  # idempotent pin of the exact set (read-only semantics for callers)
            db.add(RegRuleSet(rule_set_hash=rsh, tenant_id=tenant_id, members=members, hypothetical=is_hyp))
            db.flush()
        prep["pinned"].add(rsh)
    return {"applicable": final, "excluded": excluded, "unknown": unknown, "conflicts": conflicts, "rule_set_hash": rsh,
            "hypothetical": is_hyp, "official": not is_hyp, "as_of": as_of.isoformat(), "known_at": known_at.isoformat() if known_at else None,
            "trace": [{"rule_key": e["rule_key"], "version": e["version"], "outcome": o} for o, es in
                      (("APPLICABLE", final), ("EXCLUDED", excluded), ("UNKNOWN", unknown)) for e in es]
                     + [{"conflict_group": c["conflict_group"], "outcome": "CONFLICT"} for c in conflicts]}


def resolve_rules(db, tenant_id: str, context: dict, as_of: datetime, known_at: datetime | None = None,
                  hypothetical_rule_ids: list[str] | None = None) -> dict:
    as_of, known_at = _aware(as_of, "as_of"), _aware(known_at, "known_at")
    prep = _prepare(db, tenant_id, as_of, known_at, hypothetical_rule_ids)
    return _resolve_one(db, tenant_id, prep, context, as_of, known_at)


def resolve_rules_batch(db, tenant_id: str, contexts: list[dict], as_of: datetime, known_at: datetime | None = None,
                        hypothetical_rule_ids: list[str] | None = None) -> list[dict]:
    """Additive service: the same result as resolve_rules() for every context, with the in-force rule set loaded and prepared ONCE.
    Output order == input order. Bounded (<= 10,000 contexts) so a request cannot monopolise a worker."""
    if len(contexts) > 10_000:
        raise ValidationFailed("at most 10,000 contexts per batch", field_errors={"contexts": "too many"})
    as_of, known_at = _aware(as_of, "as_of"), _aware(known_at, "known_at")
    prep = _prepare(db, tenant_id, as_of, known_at, hypothetical_rule_ids)
    return [_resolve_one(db, tenant_id, prep, c, as_of, known_at) for c in contexts]


def _additive(db, e: dict) -> bool:
    r = db.get(RegRule, e["rule_id"])
    return bool(r and r.additive_stricter)


def _incompatible(es: list[dict]) -> bool:
    seen: dict[str, tuple] = {}
    for e in es:
        for name, p in (e["parameters"] or {}).items():
            v = (p["value"], p["unit"])
            if name in seen and seen[name] != v:
                return True
            seen[name] = v
    return False


RESERVED = {"rule_set_hash", "hypothetical", "official", "parameters", "rules", "ambiguous_parameters"}


def rule_parameters(db, tenant_id: str, rule_set_hash: str) -> dict:
    """Pinned parameters of an exact rule set. `parameters` is {rule_key: {name: {value, unit}}}. For consumers that need a
    plain scalar (e.g. risk reads `margin_multiplier`), unambiguous parameter names are ALSO exposed flat at the top level as
    decimal strings. A name defined by several rules with different values is NOT flattened (see ambiguous_parameters)."""
    rs = db.get(RegRuleSet, (rule_set_hash, tenant_id))
    if rs is None:
        raise NotFound("Rule set not found")
    seen: dict[str, set] = {}
    for m in rs.members:
        for name, p in (m["parameters"] or {}).items():
            seen.setdefault(name, set()).add((p["value"], p["unit"]))
    flat = {n: next(iter(v))[0] for n, v in seen.items() if len(v) == 1 and n not in RESERVED}
    out = {"rule_set_hash": rs.rule_set_hash, "hypothetical": rs.hypothetical, "official": not rs.hypothetical,
           "parameters": {m["rule_key"]: m["parameters"] for m in rs.members},
           "rules": [{"rule_key": m["rule_key"], "version": m["version"], "rule_hash": m["rule_hash"], "hypothetical": m["hypothetical"]} for m in rs.members],
           "ambiguous_parameters": sorted(n for n, v in seen.items() if len(v) > 1)}
    return out | flat
