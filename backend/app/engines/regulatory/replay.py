"""VERITAS replay contract for REGULUS (docs/agent-handoffs/CONTRACTS.md v1.1, app/engines/evidence/replay.py).

Two replayable computations, each with a CANONICAL BUSINESS HASH that excludes run ids, timestamps and audit sequence:

  RULE_CONTENT  business_hash = H({rule content hash, per-span sha256 of the exact source text slice})
                -> detects any change of the approved rule content OR of the quoted source text it relies on.
  IMPACT        business_hash = H([per account: rule-set hashes, recorded risk result hashes/amounts, status])
                -> re-resolves both rule sets bitemporally from persisted rules and re-reads the immutable risk results.
                A1 never recomputes margin: replay re-reads the stored A3 results by id (it must not create new ones).

The replayer is called by evidence.replay_run inside a SAVEPOINT that is always rolled back, so it must only read.
If the persisted inputs are missing it raises -> VERITAS reports NOT_REPLAYABLE (never a silent fallback).
"""
from __future__ import annotations

import hashlib
from datetime import datetime

from app.core import services
from app.core.canonical import domain_hash
from app.core.errors import NotFound
from app.core.money import dstr, to_decimal
from app.engines.regulatory.models import RegRule, RegSource, RegSpan

ENGINE_VERSION = "regulus/2.0.0"
METHOD_RULE = "rule-content-hash/1"
METHOD_IMPACT = "impact-resolution/1"
REPLAYER = "replay.regulatory"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def span_refs(db, span_ids) -> list[dict]:
    """Source-span references with document id + offsets (VERITAS lineage builds source -> span -> rule from these)."""
    out = []
    for sid in sorted(span_ids or []):
        sp = db.get(RegSpan, sid)
        if sp is not None:
            out.append({"span_id": sp.span_id, "document_id": sp.source_id, "start": sp.start, "end": sp.end, "page": sp.page})
    return out


def rule_business_hash(db, r: RegRule) -> str:
    from app.engines.regulatory.registry import compute_hash  # local import: registry imports this module
    spans = []
    for sid in sorted(r.span_ids or []):
        sp = db.get(RegSpan, sid)
        src = db.get(RegSource, sp.source_id) if sp is not None else None
        spans.append({"span_id": sid, "text_sha256": _sha(src.text[sp.start:sp.end]) if sp is not None and src is not None else "MISSING"})
    return domain_hash("aether.reg.replay.rule.v1", {"content_hash": compute_hash(r), "spans": spans})


def rule_replay_spec(db, r: RegRule) -> dict:
    return {"replayer": REPLAYER, "engine_version": ENGINE_VERSION, "method_version": METHOD_RULE,
            "business_hash": rule_business_hash(db, r), "inputs": {"kind": "RULE_CONTENT", "rule_id": r.rule_id}}


def impact_row_facts(row: dict) -> dict:
    """The business facts of one impact row (no ids of runs, no timestamps)."""
    return {"account_id": row.get("account_id"), "status": row.get("status"),
            "baseline_rule_set_hash": row.get("baseline_rule_set_hash"), "hypothetical_rule_set_hash": row.get("hypothetical_rule_set_hash"),
            "baseline_result_hash": row.get("baseline_result_hash"), "hypothetical_result_hash": row.get("hypothetical_result_hash"),
            "baseline_required": row.get("baseline_required"), "hypothetical_required": row.get("hypothetical_required"),
            "currency": row.get("currency")}


def impact_business_hash(rows: list[dict]) -> str:
    return domain_hash("aether.reg.replay.impact.v1", [impact_row_facts(r) for r in rows])


def impact_replay_spec(impact_id: str, as_of: datetime, known_at: datetime | None, hyp_ids: list[str], accounts: list[dict], rows: list[dict]) -> dict:
    return {"replayer": REPLAYER, "engine_version": ENGINE_VERSION, "method_version": METHOD_IMPACT,
            "business_hash": impact_business_hash(rows),
            "inputs": {"kind": "IMPACT", "impact_id": impact_id, "as_of": as_of.isoformat(),
                       "known_at": known_at.isoformat() if known_at else None, "hypothetical_rule_ids": list(hyp_ids),
                       "accounts": [{"account_id": a.get("account_id"), "context": a["context"],
                                     "baseline_result_id": r.get("baseline_result_id"), "hypothetical_result_id": r.get("hypothetical_result_id"),
                                     "status": r.get("status"), "currency": r.get("currency")} for a, r in zip(accounts, rows)]}}


def _dt(s: str | None):
    return datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


def replay_regulatory(session, tenant_id: str, spec: dict) -> dict:
    """fn(session, tenant_id, spec) -> {engine_version, method_version, business_hash}. READ-ONLY (runs in a rolled-back savepoint)."""
    from app.engines.regulatory.resolve import resolve_rules
    inp = spec.get("inputs") or {}
    kind = inp.get("kind")
    if kind == "RULE_CONTENT":
        r = session.get(RegRule, inp["rule_id"])
        if r is None or r.tenant_id != tenant_id:
            raise NotFound("rule no longer present")
        return {"engine_version": ENGINE_VERSION, "method_version": METHOD_RULE, "business_hash": rule_business_hash(session, r)}
    if kind == "IMPACT":
        as_of, known_at, hyp = _dt(inp["as_of"]), _dt(inp.get("known_at")), inp["hypothetical_rule_ids"]
        rows = []
        for a in inp["accounts"]:
            base = resolve_rules(session, tenant_id, a["context"], as_of, known_at)
            hyp_r = resolve_rules(session, tenant_id, a["context"], as_of, known_at, hyp)
            row = {"account_id": a.get("account_id"), "baseline_rule_set_hash": base["rule_set_hash"],
                   "hypothetical_rule_set_hash": hyp_r["rule_set_hash"], "currency": a.get("currency"),
                   "status": "UNAFFECTED" if base["rule_set_hash"] == hyp_r["rule_set_hash"] else a.get("status")}
            if a.get("baseline_result_id") and a.get("hypothetical_result_id"):
                b = services.call("risk.get_result", session, tenant_id, a["baseline_result_id"])  # immutable stored results
                h = services.call("risk.get_result", session, tenant_id, a["hypothetical_result_id"])
                row |= {"baseline_result_hash": b.get("result_hash"), "hypothetical_result_hash": h.get("result_hash"),
                        "baseline_required": dstr(to_decimal(b["required_amount"]["amount"])),
                        "hypothetical_required": dstr(to_decimal(h["required_amount"]["amount"]))}
            rows.append(row)
        return {"engine_version": ENGINE_VERSION, "method_version": METHOD_IMPACT, "business_hash": impact_business_hash(rows)}
    raise ValueError(f"unknown replay kind {kind!r}")
