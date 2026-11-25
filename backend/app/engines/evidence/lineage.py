"""E03 lineage: directed provenance graph source -> span -> rule/terms -> snapshot -> method -> margin -> proposal
-> control -> approval, built from evidence events (payload.parents, source_spans) and explicit ev_lineage edges.

Missing links are REPORTED, never papered over: a required stage with no node, or a parent referenced by an edge that
has no recorded event, is listed in `missing`.
"""
from __future__ import annotations

from sqlalchemy import select

from app.db.base import utcnow
from app.engines.evidence._db import insert_ignore
from app.engines.evidence.models import EvEvent, EvLineage

STAGES = ["SOURCE", "SPAN", "RULE_TERMS", "SNAPSHOT", "METHOD", "MARGIN", "PROPOSAL", "CONTROL", "APPROVAL"]
TYPE_STAGE = {
    "source_document": "SOURCE", "document": "SOURCE", "source": "SOURCE",
    "source_span": "SPAN", "span": "SPAN",
    "rule": "RULE_TERMS", "rule_version": "RULE_TERMS", "terms": "RULE_TERMS", "terms_version": "RULE_TERMS",
    "agreement_version": "RULE_TERMS", "rule_set": "RULE_TERMS",
    "snapshot": "SNAPSHOT", "market_snapshot": "SNAPSHOT", "inventory_snapshot": "SNAPSHOT", "clearing_file": "SNAPSHOT",
    "method": "METHOD", "methodology": "METHOD",
    "margin_result": "MARGIN", "margin": "MARGIN",
    "allocation_proposal": "PROPOSAL", "proposal": "PROPOSAL",
    "control_result": "CONTROL", "control_evaluation": "CONTROL", "case": "CONTROL",
    "approval": "APPROVAL",
    # Vocabulary actually emitted by the engines (kept explicit; see docs/methodologies/evidence.md)
    "reg_source": "SOURCE", "contract_document": "SOURCE", "reg_span": "SPAN",
    "reg_rule": "RULE_TERMS", "contract_terms": "RULE_TERMS", "contract_package": "RULE_TERMS", "contract_field": "RULE_TERMS",
    "rule_param_set": "RULE_TERMS", "fact_snapshot": "SNAPSHOT", "trade_mapping": "SNAPSHOT",
    "risk_snapshot": "SNAPSHOT", "risk_clearing_file": "SNAPSHOT", "risk_import": "SNAPSHOT", "col_snapshot": "SNAPSHOT",
    "risk_result": "MARGIN", "risk_stress": "MARGIN", "col_proposal": "PROPOSAL", "col_instruction": "PROPOSAL",
    "cmp_evaluation": "CONTROL", "cmp_case": "CONTROL", "cmp_control": "CONTROL", "cmp_waiver": "APPROVAL",
    "reg_impact_run": "METHOD",
}
_PREFIX_STAGE = (("cmp_", "CONTROL"), ("col_", "PROPOSAL"), ("risk_", "MARGIN"), ("reg_", "RULE_TERMS"), ("contract_", "RULE_TERMS"))


# Engines name the same entity differently in `payload.parents` and in their own events. Identity is by canonical type.
ALIASES = {"margin_result": "risk_result", "margin": "risk_result"}
MAX_ANCESTOR_DEPTH, MAX_NODES = 6, 300


def canon(entity_type: str) -> str:
    return ALIASES.get(entity_type, entity_type)


def stage_of(entity_type: str) -> str:
    """Exact vocabulary first; then a tolerant prefix fallback so a new engine entity type is never silently dropped
    to OTHER when its prefix identifies the owning pipeline stage."""
    st = TYPE_STAGE.get(entity_type)
    if st:
        return st
    for pre, stage in _PREFIX_STAGE:
        if entity_type.startswith(pre):
            return stage
    return "OTHER"


def record_lineage(session, tenant_id: str, correlation_id: str, parent_type: str, parent_id: str,
                   child_type: str, child_id: str) -> bool:
    """Idempotent explicit edge (engines may call this in their own transaction). Returns True if newly recorded."""
    # atomic (the unique edge constraint decides): a check-then-insert raced into a unique violation on PostgreSQL
    return bool(insert_ignore(session, EvLineage, tenant_id=tenant_id, correlation_id=correlation_id, parent_type=parent_type,
                              parent_id=parent_id, child_type=child_type, child_id=child_id, created_at=utcnow()))


def _nid(t: str, i: str) -> str:
    return f"{canon(t)}:{i}"


def build_lineage(session, tenant_id: str, correlation_id: str) -> dict:
    events = session.execute(select(EvEvent).where(EvEvent.tenant_id == tenant_id, EvEvent.correlation_id == correlation_id)
                             .order_by(EvEvent.sequence)).scalars().all()
    nodes: dict[str, dict] = {}
    edges: set[tuple[str, str]] = set()

    def node(t: str, i: str) -> dict:
        n = nodes.setdefault(_nid(t, i), {"node_id": _nid(t, i), "entity_type": canon(t), "entity_id": i, "stage": stage_of(canon(t)),
                                          "events": [], "recorded": False, "external": False})
        return n


    def cite_spans(ev, child_id: str) -> None:
        """source_spans cited by a RECORDED event create SPAN (and SOURCE, when a document id is cited) nodes feeding that event."""
        for sp in ev.source_spans or []:
            if not isinstance(sp, dict):
                continue
            doc = sp.get("document_id") or sp.get("source_id")
            detail = {k: sp[k] for k in ("quote", "excerpt", "page", "start", "end", "char_start", "char_end", "document_id") if k in sp}
            if sp.get("span_id") and not doc:
                n2 = node("source_span", str(sp["span_id"]))
                n2["recorded"] = True
                n2["span"] = detail
                edges.add((_nid("source_span", str(sp["span_id"])), child_id))
            if doc:
                node("source_document", str(doc)).update(recorded=True)
                spid = sp.get("span_id") or f"{doc}:{sp.get('start', sp.get('char_start', '?'))}-{sp.get('end', sp.get('char_end', '?'))}"
                node("source_span", spid).update(recorded=True, span=detail)
                edges.add((_nid("source_document", str(doc)), _nid("source_span", spid)))
                edges.add((_nid("source_span", spid), child_id))

    for ev in events:
        n = node(ev.entity_type, ev.entity_id)
        n["recorded"] = True
        n["events"].append({"sequence": ev.sequence, "event_id": ev.event_id, "engine": ev.engine, "action": ev.action,
                            "recorded_at": ev.recorded_at.isoformat(), "event_hash": ev.event_hash})
        child = _nid(ev.entity_type, ev.entity_id)
        for p in (ev.payload or {}).get("parents", []) or []:
            if isinstance(p, dict) and p.get("entity_type") and p.get("entity_id"):
                node(p["entity_type"], str(p["entity_id"]))
                if _nid(p["entity_type"], str(p["entity_id"])) != child:       # never a self-loop
                    edges.add((_nid(p["entity_type"], str(p["entity_id"])), child))
        cite_spans(ev, child)
    for e in session.execute(select(EvLineage).where(EvLineage.tenant_id == tenant_id,
                                                     EvLineage.correlation_id == correlation_id)).scalars():
        node(e.parent_type, e.parent_id)
        node(e.child_type, e.child_id)
        edges.add((_nid(e.parent_type, e.parent_id), _nid(e.child_type, e.child_id)))

    # A parent not recorded under THIS decision's correlation id (e.g. a snapshot pinned at seed time, a rule approved earlier) is
    # looked up across the tenant. Found => included and flagged `external` (its own parents are followed, bounded). Only parents
    # with no event anywhere in the tenant remain reported as PARENT_NOT_RECORDED.
    for _depth in range(MAX_ANCESTOR_DEPTH):
        pending = [n for n in nodes.values() if not n["recorded"] and not n["external"] and n["entity_type"] != "source_span"]
        if not pending or len(nodes) >= MAX_NODES:
            break
        for n in pending:
            rows = session.execute(select(EvEvent).where(EvEvent.tenant_id == tenant_id, EvEvent.entity_id == n["entity_id"],
                                                         EvEvent.entity_type.in_(sorted({n["entity_type"]} | {a for a, c in ALIASES.items() if c == n["entity_type"]})))
                                   .order_by(EvEvent.sequence).limit(20)).scalars().all()
            if not rows:
                n["external"] = True  # looked up, nothing exists anywhere in the tenant => stays unrecorded (reported below)
                n["searched"] = True
                continue
            n["recorded"], n["external"] = True, True
            for ev in rows:
                cite_spans(ev, n["node_id"])
                n["events"].append({"sequence": ev.sequence, "event_id": ev.event_id, "engine": ev.engine, "action": ev.action,
                                    "recorded_at": ev.recorded_at.isoformat(), "event_hash": ev.event_hash, "correlation_id": ev.correlation_id})
                for p in (ev.payload or {}).get("parents", []) or []:
                    if isinstance(p, dict) and p.get("entity_type") and p.get("entity_id"):
                        node(p["entity_type"], str(p["entity_id"]))
                        if _nid(p["entity_type"], str(p["entity_id"])) != n["node_id"]:
                            edges.add((_nid(p["entity_type"], str(p["entity_id"])), n["node_id"]))
    covered = {n["stage"] for n in nodes.values() if n["recorded"]}
    missing = [{"kind": "STAGE_ABSENT", "stage": s, "detail": f"no recorded evidence for stage {s}"} for s in STAGES if s not in covered]
    for n in nodes.values():
        if not n["recorded"]:
            missing.append({"kind": "PARENT_NOT_RECORDED", "node_id": n["node_id"], "stage": n["stage"],
                            "detail": "referenced as a parent but no evidence event in this tenant records it"})
    order = {s: i for i, s in enumerate(STAGES + ["OTHER"])}
    ordered = sorted(nodes.values(), key=lambda n: (order[n["stage"]], n["events"][0]["sequence"] if n["events"] else 10**12, n["node_id"]))
    for i, n in enumerate(ordered, start=1):
        n["step"] = i
    return {"correlation_id": correlation_id, "nodes": ordered, "edges": [{"from": a, "to": b} for a, b in sorted(edges)],
            "required_stages": STAGES, "missing": missing, "complete": not missing, "event_count": len(events)}
