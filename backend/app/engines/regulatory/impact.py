"""R05 impact analysis: before/after on IDENTICAL pinned snapshots. A1 never calculates margin itself.

Margin implications come from risk.compute_margin (A3); posted coverage from collateral.coverage (A4).
Results are ALWAYS hypothetical (official=false) and never published as operational compliance status.
Accounts whose resolved rule-set hash is unchanged are not recomputed (dependency index).
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from app.auth.security import Principal
from app.core import services
from app.core.canonical import domain_hash
from app.core.errors import ApiError, NotFound, ValidationFailed
from app.core.money import NumericError, dstr, to_decimal
from app.db.base import new_id
from app.engines.regulatory import registry, replay
from app.engines.regulatory.models import RegImpactRun
from app.engines.regulatory.resolve import resolve_rules


def _amt(m: dict, name: str) -> tuple[Decimal, str]:
    try:
        return to_decimal(m[name]["amount"], field=name), m[name]["currency"]
    except (KeyError, NumericError, TypeError) as exc:
        raise ApiError(f"malformed margin result field {name}: {exc}", code="MALFORMED_RESULT") from exc


# Default methodology per market when the caller does not name one (names are A3's published methodology ids).
DEFAULT_METHOD = {"ETD_DOMESTIC": "ETD_IMPORTED_V1", "ETD_IFSC": "ETD_IMPORTED_V1", "OTC": "OTC_VM_SIMPLE_V1"}


def _margin(db, tenant_id, acct, rule_set_hash, as_of, known_at):
    # explicit clock + account identity travel in the context (no implicit clock anywhere)
    ctx = {"account_id": acct.get("account_id"), **acct["context"], "as_of": as_of.isoformat()}
    if known_at:
        ctx["known_at"] = known_at.isoformat()
    res = services.call("risk.compute_margin", db, tenant_id, ctx, acct["snapshot_id"], acct.get("terms_hash"),
                        rule_set_hash, acct.get("methodology") or DEFAULT_METHOD.get(acct["context"].get("market_type"), "DEFAULT"))
    if res.get("status") != "OK":
        return None, f"risk result {res.get('status')}: {'; '.join(res.get('blocking_reasons', [])) or 'no reason supplied'}"
    amt, ccy = _amt(res, "required_amount")
    return {"amount": amt, "currency": ccy, "result_id": res.get("result_id"), "result_hash": res.get("result_hash")}, None


def _feasibility(db, tenant_id: str, acct: dict, row: dict, rule_set_hash: str) -> dict:
    """Ask ATLAS (A4) whether the HYPOTHETICAL requirement can be covered. Contract-safe use of collateral.optimise:
    the margin result is hypothetical, so ATLAS flags the proposal hypothetical and can never approve/reserve it.
    This writes a hypothetical proposal row in A4 (visible, labelled); it moves no asset. Never fabricates a result."""
    need = {k: acct.get(k) for k in ("inventory_snapshot_id", "deadline", "horizon_days")}
    missing = [k for k, v in need.items() if v in (None, "")]
    if missing:
        return {"status": "NOT_EVALUATED", "reason": f"missing account fields: {', '.join(missing)}"}
    if row.get("hypothetical_shortfall") in (None, "0"):
        return {"status": "NOT_REQUIRED" if row.get("hypothetical_shortfall") == "0" else "NOT_EVALUATED",
                "reason": "no shortfall under the counterfactual" if row.get("hypothetical_shortfall") == "0" else "shortfall unknown (posted coverage unavailable)"}
    try:
        prop = services.call("collateral.optimise", db, tenant_id, row["hypothetical_result_id"], need["inventory_snapshot_id"],
                             rule_set_hash, acct.get("terms_hash"), need["deadline"], int(need["horizon_days"]),
                             actor_id="regulatory-impact", actor_type="SERVICE")
    except ApiError as exc:
        return {"status": "NOT_EVALUATED", "reason": f"{exc.code}: {exc.message}"}
    return {"status": "EVALUATED", "solver_status": prop.get("solver_status"), "proposal_state": prop.get("state"),
            "proposal_id": prop.get("proposal_id"), "required_coverage": prop.get("required_coverage"), "residual": prop.get("residual"),
            "objective": prop.get("objective"), "currency": prop.get("currency"), "hypothetical": bool(prop.get("hypothetical")),
            "note": "HYPOTHETICAL proposal: cannot be approved or reserved; no asset moves"}


def run_impact(db, actor: Principal, body: dict, corr: str | None = None) -> RegImpactRun:
    as_of = registry.aware(body["as_of"], "as_of")
    known_at = registry.aware(body["known_at"], "known_at") if body.get("known_at") else None
    hyp_ids = body.get("hypothetical_rule_ids") or []
    accounts = body.get("accounts") or []
    if not hyp_ids:
        raise ValidationFailed("hypothetical_rule_ids required (impact is an explicit counterfactual)", field_errors={"hypothetical_rule_ids": "required"})
    if not accounts or len(accounts) > 500:
        raise ValidationFailed("accounts: 1..500 required", field_errors={"accounts": "invalid"})
    rows, totals = [], {}
    counts = {"accounts": len(accounts), "affected": 0, "unaffected": 0, "blocked": 0, "with_unknown_rules": 0}
    changed_keys: set[str] = set()
    for a in accounts:
        base = resolve_rules(db, actor.tenant_id, a["context"], as_of, known_at)
        hyp = resolve_rules(db, actor.tenant_id, a["context"], as_of, known_at, hyp_ids)
        row = {"account_id": a.get("account_id"), "snapshot_id": a.get("snapshot_id"), "baseline_rule_set_hash": base["rule_set_hash"],
               "hypothetical_rule_set_hash": hyp["rule_set_hash"], "unknown_rules": [u["rule_key"] for u in hyp["unknown"]],
               "conflicts": [c["conflict_group"] for c in hyp["conflicts"]], "official": False}
        if row["unknown_rules"]:
            counts["with_unknown_rules"] += 1
        if base["rule_set_hash"] == hyp["rule_set_hash"]:
            row |= {"status": "UNAFFECTED", "note": "resolved rule set identical; not recomputed"}
            counts["unaffected"] += 1
            rows.append(row)
            continue
        changed_keys |= {m["rule_key"] for m in hyp["applicable"] if m["hypothetical"]}
        counts["affected"] += 1
        if hyp["conflicts"]:
            row |= {"status": "BLOCKED", "reason": "CONFLICT between applicable mandates; no authoritative result"}
            counts["blocked"] += 1
            rows.append(row)
            continue
        try:
            b, why_b = _margin(db, actor.tenant_id, a, base["rule_set_hash"], as_of, known_at)
            h, why_h = _margin(db, actor.tenant_id, a, hyp["rule_set_hash"], as_of, known_at)
        except ApiError as exc:  # explicit, never faked: includes SERVICE_NOT_IMPLEMENTED
            row |= {"status": "BLOCKED", "reason": f"{exc.code}: {exc.message}"}
            counts["blocked"] += 1
            rows.append(row)
            continue
        if b is None or h is None:
            row |= {"status": "BLOCKED", "reason": why_b or why_h}
            counts["blocked"] += 1
            rows.append(row)
            continue
        if b["currency"] != h["currency"]:
            row |= {"status": "BLOCKED", "reason": f"CURRENCY_MISMATCH {b['currency']} vs {h['currency']}: no implicit conversion"}
            counts["blocked"] += 1
            rows.append(row)
            continue
        ccy = b["currency"]
        row |= {"status": "COMPUTED", "currency": ccy, "baseline_required": dstr(b["amount"]), "hypothetical_required": dstr(h["amount"]),
                "delta_required": dstr(h["amount"] - b["amount"]), "baseline_result_id": b["result_id"], "hypothetical_result_id": h["result_id"],
                "baseline_result_hash": b["result_hash"], "hypothetical_result_hash": h["result_hash"]}
        try:
            cov = services.call("collateral.coverage", db, actor.tenant_id, a.get("account_id"), as_of)
            ep, ec = _amt(cov, "eligible_posted")
            if ec != ccy:
                raise ApiError(f"coverage currency {ec} != margin currency {ccy}", code="CURRENCY_MISMATCH")
            row |= {"eligible_posted": dstr(ep), "baseline_shortfall": dstr(max(Decimal(0), b["amount"] - ep)),
                    "hypothetical_shortfall": dstr(max(Decimal(0), h["amount"] - ep)),
                    "coverage_note": "eligible POSTED collateral only; proposals are not counted"}
        except ApiError as exc:
            row |= {"eligible_posted": None, "hypothetical_shortfall": None, "coverage_note": f"shortfall not computed: {exc.code}: {exc.message}"}
        if body.get("include_feasibility"):
            row["feasibility"] = _feasibility(db, actor.tenant_id, a, row, hyp["rule_set_hash"])
        t = totals.setdefault(ccy, {"baseline_required": Decimal(0), "hypothetical_required": Decimal(0), "delta_required": Decimal(0), "accounts": 0})
        t["baseline_required"] += b["amount"]
        t["hypothetical_required"] += h["amount"]
        t["delta_required"] += h["amount"] - b["amount"]
        t["accounts"] += 1
        rows.append(row)
    status = "BLOCKED" if counts["affected"] and counts["blocked"] == counts["affected"] else ("PARTIAL" if counts["blocked"] else "COMPLETED")
    results = {"rows": rows, "counts": counts, "aggregate_by_currency": {c: {k: (dstr(v) if isinstance(v, Decimal) else v) for k, v in t.items()} for c, t in totals.items()},
               "changed_rule_keys": sorted(changed_keys), "official": False, "publishes_compliance_status": False,
               "affected_controls": {"derived_from": "rule keys changed by the counterfactual", "keys": sorted(changed_keys),
                                     "note": "compliance.evaluate is deliberately NOT invoked for hypothetical rule sets"},
               "liquidity_feasibility": ("EVALUATED via collateral.optimise per account (HYPOTHETICAL proposals; see row.feasibility)"
                                         if body.get("include_feasibility") else "NOT_EVALUATED (opt-in: set include_feasibility)"),
               "assumptions": ["identical pinned snapshots for baseline and counterfactual", "aggregation only within a single currency",
                               "unknown-scope rules are counted, never treated as applicable or as zero"]}
    run = RegImpactRun(impact_id=new_id(), tenant_id=actor.tenant_id, status=status, hypothetical=True, created_by=actor.user_id,
                       params={"as_of": as_of.isoformat(), "known_at": known_at.isoformat() if known_at else None, "hypothetical_rule_ids": hyp_ids, "include_feasibility": bool(body.get("include_feasibility")),
                               "accounts": [{k: v for k, v in a.items()} for a in accounts]}, results=results, provenance="SYNTHETIC")
    db.add(run)
    db.flush()
    parents = ([{"entity_type": "reg_rule", "entity_id": i} for i in hyp_ids]
               + [{"entity_type": "rule_set", "entity_id": h} for h in sorted({r["baseline_rule_set_hash"] for r in rows} | {r["hypothetical_rule_set_hash"] for r in rows})]
               + [{"entity_type": "risk_result", "entity_id": r[k]} for r in rows for k in ("baseline_result_id", "hypothetical_result_id") if r.get(k)]
               + [{"entity_type": "col_proposal", "entity_id": r["feasibility"]["proposal_id"]} for r in rows
                  if (r.get("feasibility") or {}).get("proposal_id")])
    registry._ev(db, actor, "IMPACT_RUN_COMPLETED", "reg_impact_run", run.impact_id, 1, corr, source_mode="REPLAY",
                 effective_at=as_of, input_hashes=sorted({r["baseline_rule_set_hash"] for r in rows} | {r["hypothetical_rule_set_hash"] for r in rows}),
                 output_hash=domain_hash("aether.reg.impact.v1", results), rationale="HYPOTHETICAL counterfactual; not an operational compliance status",
                 payload={"status": status, "counts": counts, "hypothetical_rule_ids": hyp_ids, "parents": parents,
                          "replay": replay.impact_replay_spec(run.impact_id, as_of, known_at, hyp_ids, accounts, rows)})
    return run


def get_impact(db, tenant_id: str, impact_id: str) -> RegImpactRun:
    r = db.get(RegImpactRun, impact_id)
    if r is None or r.tenant_id != tenant_id:
        raise NotFound("Impact run not found")
    return r
