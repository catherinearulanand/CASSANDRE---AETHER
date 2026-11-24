"""Idempotent synthetic seed for the regulatory engine (tenant-scoped). Uses the SAME service paths as the API
(maker submits, a DIFFERENT checker approves, explicit activation) - no direct state writes except the clearly
documented demo conveniences noted below. Rule texts are fictional policy, not law."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.auth.security import Principal
from app.db.base import utcnow
from app.db.models_core import ApprovalRow
from app.engines.regulatory import documents as D
from app.engines.regulatory import registry
from app.engines.regulatory.models import RegRule, RegSource

EPOCH = datetime(2026, 1, 1, tzinfo=UTC)  # recording time for synthetic fixtures (deterministic bitemporal replay)
SCENARIO_CLOCK = datetime(2026, 8, 17, 4, 0, tzinfo=UTC)
DOCS = [
    ("DEMO-CIRC-2026-001_margin_revision.txt", "SYNTHETIC", "DEMO-CIRC-2026-001", "Synthetic clearing notice: margin multiplier revision"),
    ("DEMO-CIRC-2026-002_ifsc_coverage.txt", "SYNTHETIC", "DEMO-CIRC-2026-002", "Synthetic IFSC clearing notice: USD coverage"),
    ("DEMO-POLICY-OTC-001_internal.txt", "INTERNAL_POLICY", "DEMO-POLICY-OTC-001", "Synthetic internal OTC variation-margin policy"),
    ("DEMO-CONSULT-2026-009_draft.txt", "DRAFT_CONSULTATION", "DEMO-CONSULT-2026-009", "Synthetic draft consultation (not binding)"),
    ("DEMO-NEGATIVE-INJECTION.txt", "SYNTHETIC", "DEMO-NEG-INJECTION", "Negative control: document with embedded instructions"),
]
ETD = {"all": [{"eq": ["market_type", "ETD_DOMESTIC"]}, {"eq": ["venue", "DEMO_NSE"]}]}
IFSC = {"all": [{"eq": ["market_type", "ETD_IFSC"]}, {"eq": ["venue", "DEMO_IFSC"]}]}
OTC = {"eq": ["market_type", "OTC"]}
CTX_ETD = {"market_type": "ETD_DOMESTIC", "venue": "DEMO_NSE"}
CTX_IFSC = {"market_type": "ETD_IFSC", "venue": "DEMO_IFSC"}


def _principals(tenant_id: str) -> tuple[Principal, Principal]:
    mk = lambda uid, role: Principal(uid, tenant_id, uid, uid, (role,), (), "")  # noqa: E731
    return mk("seed-maker", "maker"), mk("seed-checker", "checker")


def _span(db, maker: Principal, src: RegSource, quote: str):
    i = src.text.index(quote)
    return registry.make_span(db, maker, src, i, i + len(quote), "MANUAL")


def _publish(db, maker, checker, body, activate: bool):
    r = registry.create_rule(db, maker, body)
    registry.submit(db, maker, r.rule_id, "seed: ready for independent review", None)
    if activate:
        registry.review(db, checker, r.rule_id, "APPROVE", "seed: independently reviewed against cited span", None)
        registry.activate(db, checker, r.rule_id, None, "seed: activate synthetic policy", as_of=SCENARIO_CLOCK, recorded_at=EPOCH)
    else:  # demo convenience: keep the pending review window open for 30 days so the maker-checker walkthrough works later
        ap = db.execute(select(ApprovalRow).where(ApprovalRow.target_id == r.rule_id, ApprovalRow.decision == "PENDING")).scalars().first()
        ap.expires_at = utcnow() + timedelta(days=30)
    return r


def seed_fixtures(session, tenant_id: str) -> dict:
    db = session
    maker, checker = _principals(tenant_id)
    srcs: dict[str, RegSource] = {}
    for fname, auth, num, title in DOCS:
        data = (D.fixtures_dir() / "documents" / fname).read_bytes()
        s, _ = registry.ingest_document(db, maker, data, "text/plain", {
            "authority_type": auth, "title": title, "doc_number": num, "issuer": "Synthetic (fictional) issuer",
            "doc_kind": "NOTICE", "source_ref": f"fixtures/regulatory/documents/{fname}"})
        srcs[num] = s
    if db.execute(select(RegRule).where(RegRule.tenant_id == tenant_id, RegRule.rule_key == "DEMO-ETD-MARGIN").limit(1)).first():
        return {"seeded": False, "reason": "demo rules already present"}
    c1, c2, c3 = srcs["DEMO-CIRC-2026-001"], srcs["DEMO-CIRC-2026-002"], srcs["DEMO-POLICY-OTC-001"]
    sp = lambda s, q: [_span(db, maker, s, q).span_id]  # noqa: E731
    base = {"authority_type": "SYNTHETIC_POLICY", "conflict_group": None}
    rules = []
    rules.append(_publish(db, maker, checker, base | {
        "rule_key": "DEMO-ETD-MARGIN", "title": "Synthetic ETD margin multiplier (current)", "scope": ETD, "condition": {"exists": "required_margin_inr"},
        "parameters": {"margin_multiplier": {"value": "1.00", "unit": "RATIO"}}, "effective_from": datetime(2026, 1, 1, tzinfo=UTC),
        "span_ids": sp(c1, "Until that date the margin multiplier of 1.00 times the base margin continues to apply."),
        "rationale": "Baseline synthetic multiplier 1.00 per notice para 2.2.",
        "test_vectors": [{"name": "in scope with margin", "context": CTX_ETD | {"required_margin_inr": "10000000"}, "expect_scope": "TRUE", "expect_condition": "TRUE"},
                         {"name": "IFSC out of scope", "context": CTX_IFSC, "expect_scope": "FALSE", "expect_condition": "FALSE"}]}, True))
    rules.append(_publish(db, maker, checker, base | {
        "rule_key": "DEMO-ETD-MARGIN", "title": "Synthetic ETD margin multiplier (revision 1.25, effective 2027-01-01)", "scope": ETD,
        "condition": {"exists": "required_margin_inr"}, "parameters": {"margin_multiplier": {"value": "1.25", "unit": "RATIO"}},
        "effective_from": datetime(2027, 1, 1, tzinfo=UTC),
        "span_ids": sp(c1, "With effect from 1 January 2027, the clearing member shall apply a margin multiplier of 1.25 times the base margin to index futures accounts on DEMO_NSE."),
        "rationale": "Revision per notice para 2.1; awaiting independent checker (usable as a HYPOTHETICAL counterfactual meanwhile).",
        "test_vectors": [{"name": "in scope with margin", "context": CTX_ETD | {"required_margin_inr": "12500000"}, "expect_scope": "TRUE", "expect_condition": "TRUE"}]}, False))
    rules.append(_publish(db, maker, checker, base | {
        "rule_key": "DEMO-ETD-COVERAGE", "title": "Synthetic ETD coverage: eligible posted collateral >= required margin", "scope": ETD,
        "condition": {"gte": ["eligible_collateral_inr", "required_margin_inr"]}, "effective_from": datetime(2026, 1, 1, tzinfo=UTC),
        "span_ids": sp(c1, "The clearing member shall hold eligible collateral of at least 100 per cent of required margin at all times."),
        "rationale": "Coverage ratio 100 per cent expressed as eligible >= required (same currency, INR).",
        "test_vectors": [{"name": "exact boundary passes", "context": CTX_ETD | {"eligible_collateral_inr": "10000000", "required_margin_inr": "10000000"}},
                         {"name": "shortfall fails", "context": CTX_ETD | {"eligible_collateral_inr": "11500000", "required_margin_inr": "12500000"}, "expect_condition": "FALSE"},
                         {"name": "missing collateral unknown", "context": CTX_ETD | {"required_margin_inr": "1"}, "expect_condition": "UNKNOWN"}]}, True))
    rules.append(_publish(db, maker, checker, base | {
        "rule_key": "DEMO-ETD-POSLIMIT", "title": "Synthetic per-client net position limit (lots)", "scope": ETD,
        "condition": {"all": [{"in": ["client_classification", ["CLIENT", "PROPRIETARY"]]}, {"lte": ["net_position_lots", {"param": "limit"}]}]},
        "parameters": {"limit": {"value": "100", "unit": "LOTS"}}, "effective_from": datetime(2026, 1, 1, tzinfo=UTC),
        "span_ids": sp(c1, "A client shall not exceed 100 lots of net position in a single index futures contract."),
        "rationale": "Limit 100 lots, inclusive (<=), net position, per client classification.",
        "test_vectors": [{"name": "100 passes", "context": CTX_ETD | {"client_classification": "CLIENT", "net_position_lots": "100"}},
                         {"name": "101 fails", "context": CTX_ETD | {"client_classification": "CLIENT", "net_position_lots": "101"}, "expect_condition": "FALSE"},
                         {"name": "unknown classification", "context": CTX_ETD | {"net_position_lots": "100"}, "expect_condition": "UNKNOWN"}]}, True))
    rules.append(_publish(db, maker, checker, base | {
        "rule_key": "DEMO-IFSC-COVERAGE", "title": "Synthetic IFSC coverage in USD (independent of domestic INR policy)", "scope": IFSC,
        "condition": {"gte": ["eligible_collateral_usd", "required_margin_usd"]}, "effective_from": datetime(2026, 1, 1, tzinfo=UTC),
        "span_ids": sp(c2, "The IFSC clearing member shall hold eligible collateral of at least 100 per cent of required margin, measured in USD, on DEMO_IFSC."),
        "rationale": "Separate IFSC profile, USD only.",
        "test_vectors": [{"name": "covered", "context": CTX_IFSC | {"eligible_collateral_usd": "5", "required_margin_usd": "5"}},
                         {"name": "domestic context excluded", "context": CTX_ETD, "expect_scope": "FALSE", "expect_condition": "UNKNOWN"}]}, True))
    rules.append(_publish(db, maker, checker, {
        "authority_type": "INTERNAL_POLICY", "rule_key": "DEMO-OTC-VM", "title": "Synthetic internal OTC variation margin policy", "scope": OTC,
        "condition": {"exists": "currency"}, "parameters": {"vm_exchange_daily": {"value": "1", "unit": "FLAG"}},
        "effective_from": datetime(2026, 1, 1, tzinfo=UTC),
        "span_ids": sp(c3, "The treasury desk shall exchange variation margin each business day with every bilateral counterparty that has an approved CSA."),
        "rationale": "Internal policy: daily VM; the margin currency must be identified.",
        "test_vectors": [{"name": "otc with ccy", "context": {"market_type": "OTC", "currency": "USD"}}]}, True))
    return {"seeded": True, "sources": len(srcs), "rules": [f"{r.rule_key} v{r.version} {r.status}" for r in rules]}
