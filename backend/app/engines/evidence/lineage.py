"""E03 lineage: directed provenance graph source -> span -> rule/terms -> snapshot -> method -> margin -> proposal
-> control -> approval, built from evidence events (payload.parents, source_spans) and explicit ev_lineage edges.

Missing links are REPORTED, never papered over: a required stage with no node, or a parent referenced by an edge that
has no recorded event, is listed in `missing`.
"""
from __future__ import annotations

from sqlalchemy import select

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
}


def stage_of(entity_type: str) -> str:
    return TYPE_STAGE.get(entity_type, "OTHER")


def record_lineage(session, tenant_id: str, correlation_id: str, parent_type: str, parent_id: str,
                   child_type: str, child_id: str) -> bool:
    """Idempotent explicit edge (engines may call this in their own transaction). Returns True if newly recorded."""
    exists = session.execute(select(EvLineage.id).where(
        EvLineage.tenant_id == tenant_id, EvLineage.parent_type == parent_type, EvLineage.parent_id == parent_id,
        EvLineage.child_type == child_type, EvLineage.child_id == child_id)).first()
    if exists:
        return False
    session.add(EvLineage(tenant_id=tenant_id, correlation_id=correlation_id, parent_type=parent_type,
                          parent_id=parent_id, child_type=child_type, child_id=child_id))
    session.flush()
    return True


def _nid(t: str, i: str) -> str:
    return f"{t}:{i}"


def build_lineage(session, tenant_id: str, correlation_id: str) -> dict:
    events = session.execute(select(EvEvent).where(EvEvent.tenant_id == tenant_id, EvEvent.correlation_id == correlation_id)
                             .order_by(EvEvent.sequence)).scalars().all()
    nodes: dict[str, dict] = {}
    edges: set[tuple[str, str]] = set()

    def node(t: str, i: str) -> dict:
        n = nodes.setdefault(_nid(t, i), {"node_id": _nid(t, i), "entity_type": t, "entity_id": i, "stage": stage_of(t),
                                          "events": [], "recorded": False})
        return n

    for ev in events:
        n = node(ev.entity_type, ev.entity_id)
        n["recorded"] = True
        n["events"].append({"sequence": ev.sequence, "event_id": ev.event_id, "engine": ev.engine, "action": ev.action,
                            "recorded_at": ev.recorded_at.isoformat(), "event_hash": ev.event_hash})
        child = _nid(ev.entity_type, ev.entity_id)
        for p in (ev.payload or {}).get("parents", []) or []:
            if isinstance(p, dict) and p.get("entity_type") and p.get("entity_id"):
                node(p["entity_type"], str(p["entity_id"]))
                edges.add((_nid(p["entity_type"], str(p["entity_id"])), child))
        for sp in ev.source_spans or []:
            if not isinstance(sp, dict):
                continue
            doc = sp.get("document_id") or sp.get("source_id")
            if doc:
                node("source_document", str(doc)).update(recorded=True)
                spid = sp.get("span_id") or f"{doc}:{sp.get('start', sp.get('char_start', '?'))}-{sp.get('end', sp.get('char_end', '?'))}"
                node("source_span", spid).update(recorded=True)
                edges.add((_nid("source_document", str(doc)), _nid("source_span", spid)))
                edges.add((_nid("source_span", spid), child))
    for e in session.execute(select(EvLineage).where(EvLineage.tenant_id == tenant_id,
                                                     EvLineage.correlation_id == correlation_id)).scalars():
        node(e.parent_type, e.parent_id)
        node(e.child_type, e.child_id)
        edges.add((_nid(e.parent_type, e.parent_id), _nid(e.child_type, e.child_id)))

    covered = {n["stage"] for n in nodes.values() if n["recorded"]}
    missing = [{"kind": "STAGE_ABSENT", "stage": s, "detail": f"no recorded evidence for stage {s}"} for s in STAGES if s not in covered]
    for n in nodes.values():
        if not n["recorded"]:
            missing.append({"kind": "PARENT_NOT_RECORDED", "node_id": n["node_id"], "stage": n["stage"],
                            "detail": "referenced as a parent but no evidence event records it"})
    order = {s: i for i, s in enumerate(STAGES + ["OTHER"])}
    ordered = sorted(nodes.values(), key=lambda n: (order[n["stage"]], n["events"][0]["sequence"] if n["events"] else 10**12, n["node_id"]))
    for i, n in enumerate(ordered, start=1):
        n["step"] = i
    return {"correlation_id": correlation_id, "nodes": ordered, "edges": [{"from": a, "to": b} for a, b in sorted(edges)],
            "required_stages": STAGES, "missing": missing, "complete": not missing, "event_count": len(events)}
