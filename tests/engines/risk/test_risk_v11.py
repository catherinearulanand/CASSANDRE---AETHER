"""Round 2: contract v1.1 (snapshot_as_of, risk.get_positions, risk.reconciliation_status), evidence replay contract
(payload.parents / payload.replay / replay.risk through evidence.replay_run) and the atomic result-cache gap.
Expected values are hand-derived below from the fixture inputs (independent arithmetic; statistics.NormalDist is a different
implementation from the engine's math.erf Black-Scholes)."""
import json
import threading
import uuid
from decimal import Decimal
from pathlib import Path
from statistics import NormalDist

import pytest
from sqlalchemy import func, select, text

from app.core import services
from app.core.canonical import content_hash
from app.db.base import SessionLocal, is_sqlite
from app.engines.evidence import service as ev
from app.engines.evidence.models import EvEvent
from app.engines.risk import fixtures as fx
from app.engines.risk import services as svc
from app.engines.risk.models import RiskResult, RiskSnapshot
from app.engines.risk.pricing import shock_payload

D = Decimal
NS = "NS-MER-NWD-001"
NSE, IFSC = "ACC-DEMO-NSE-001", "ACC-DEMO-IFSC-001"
AS_OF = "2026-08-17T04:00:00Z"
TERMS = {"base_currency": "USD", "threshold_a": "10000000", "threshold_b": "10000000", "mta_a": "500000", "mta_b": "500000",
         "independent_amount_a": "0", "independent_amount_b": "0", "rounding_increment": "100000", "rounding_mode": "UP", "mta_semantics": "GTE"}
ZERO = {"PARTY_A": "0", "PARTY_B": "0"}
PF = json.loads((Path(__file__).resolve().parents[3] / "fixtures" / "risk" / "portfolio.json").read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def terms_provider(monkeypatch):
    def provider(session, tenant_id, ns, as_of, known_at, fact_snapshot_id=None):
        return {"status": "RESOLVED", "terms_hash": content_hash(TERMS), "terms": dict(TERMS), "blocking_reasons": [], "source_ids": []}
    monkeypatch.setitem(services._REGISTRY, "contracts.resolve_terms", provider)


@pytest.fixture
def tenant():
    t = f"r11-{uuid.uuid4().hex[:10]}"
    with SessionLocal() as s:
        fx.seed_fixtures(s, t)
        s.commit()
    return t


def otc_ctx(cid=None, **kw):
    c = {"market_type": "OTC", "netting_set_id": NS, "as_of": AS_OF, "existing_vm_balance": ZERO, **kw}
    if cid:
        c["correlation_id"] = cid
    return c


def calc(tenant, ctx, snap, method="OTC_VM_SIMPLE_V1", terms=True, rules=None):
    with SessionLocal() as s:
        r = svc.compute_margin(s, tenant, ctx, snap, content_hash(TERMS) if terms else None, rules, method)
        s.commit()
        return r


# ------------------------------------------------------------------------------------------ (1) contract v1.1
def test_margin_result_carries_as_of_and_snapshot_as_of(tenant):
    r = calc(tenant, otc_ctx(), "SNAP-SHOCK-001")
    assert r["status"] == "OK"
    assert r["as_of"] == "2026-08-17T04:00:00+00:00" and r["snapshot_as_of"] == "2026-08-17T04:00:00+00:00"
    e = calc(tenant, {"market_type": "ETD_DOMESTIC", "account_id": NSE, "venue": "DEMO_NSE", "as_of": AS_OF}, "SNAP-ETD-001", "ETD_IMPORTED_V1", terms=False)
    assert e["status"] == "OK" and e["snapshot_as_of"] == "2026-08-17T03:30:00+00:00"  # the SNAPSHOT's own time, not the run's
    b = calc(tenant, otc_ctx(), "SNAP-DOES-NOT-EXIST")
    assert b["status"] == "BLOCKED" and b["snapshot_as_of"] is None  # unknown stays unknown


def test_get_positions_domestic_hand_values_and_independent_delta(tenant):
    assert services.is_registered("risk.get_positions") and services.is_registered("risk.reconciliation_status")
    with SessionLocal() as s:
        g = services.call("risk.get_positions", s, tenant, NSE, AS_OF)
    # hand arithmetic: FUT 40 lots x 25 x 24,000 = 24,000,000; OPT |-20| x 25 x underlying mark 24,000 = 12,000,000
    assert g["gross_lots"] == "60" and g["net_lots"] == "20" and g["currency"] == "INR"
    assert g["notional"] == "36000000.00" and g["available"] is True
    assert g["client_class"] == "UNKNOWN" and "NOT an input" in g["client_class_note"]
    assert g["as_of"] == "2026-08-17T03:30:00+00:00" and "SNAP-ETD-001" in g["source"]
    # independent BSM call delta (q = 0): S = K = 24000, T = 43/365, r = 6.5%, vol = 18%
    t = 43 / 365
    import math
    d1 = (math.log(1.0) + (0.065 + 0.5 * 0.18 ** 2) * t) / (0.18 * math.sqrt(t))
    expected = 40 * 1.0 + (-20) * NormalDist().cdf(d1)
    assert abs(float(g["delta_equivalent_lots"]) - expected) < 1e-6


def test_get_positions_ifsc_is_usd_and_never_blended_with_domestic(tenant):
    with SessionLocal() as s:
        g = svc.get_positions(s, tenant, IFSC, AS_OF)
    assert g["currency"] == "USD" and g["gross_lots"] == "30" and g["net_lots"] == "30"
    assert g["notional"] == "7500000.00" and D(g["delta_equivalent_lots"]) == 30  # 30 x 10 x 25,000


def test_get_positions_unknown_is_none_never_zero(tenant):
    with SessionLocal() as s:
        none = svc.get_positions(s, tenant, "ACC-NOBODY", AS_OF)
        early = svc.get_positions(s, tenant, NSE, "2026-08-17T03:00:00Z")  # before the positions' as_of
    for g in (none, early):
        assert g["available"] is False and g["gross_lots"] is None and g["notional"] is None and g["warnings"]


def test_get_positions_without_a_snapshot_carrying_marks_gives_lots_but_null_notional(tenant):
    with SessionLocal() as s:
        s.execute(text("DELETE FROM risk_snapshots WHERE tenant_id = :t AND snapshot_id = 'SNAP-ETD-001'"), {"t": tenant})
        s.commit()
        g = svc.get_positions(s, tenant, NSE, AS_OF)
    assert g["gross_lots"] == "60" and g["notional"] is None and g["delta_equivalent_lots"] is None
    assert any("unavailable from every pinned snapshot" in w for w in g["warnings"])


def test_reconciliation_status_ok_exception_unknown(tenant):
    with SessionLocal() as s:
        ok = svc.reconciliation_status(s, tenant, NSE, AS_OF)                      # good file 03:30 is the latest <= as_of
        exc = svc.reconciliation_status(s, tenant, NSE, "2026-08-17T03:10:00Z")    # only the bad-total file (03:00) is dated <= as_of
        ifs = svc.reconciliation_status(s, tenant, IFSC, AS_OF)
        unk = svc.reconciliation_status(s, tenant, "ACC-NOBODY", AS_OF)
        before = svc.reconciliation_status(s, tenant, NSE, "2026-08-17T02:00:00Z")
    assert ok["status"] == "OK" and ok["expected_total"] == "10000000" and ok["imported_total"] == "10000000" and ok["currency"] == "INR"
    assert ok["file_id"] == "CLR-DEMO-NSE-20260817"
    # G10: components 8,000,000 + 2,000,000 = 10,000,000 (independent); supplied total 10,100,000 -> exception, never trusted
    assert exc["status"] == "EXCEPTION" and exc["expected_total"] == "10000000" and exc["imported_total"] == "10100000"
    assert abs(D(exc["difference"])) == D("100000") and exc["file_id"] == "CLR-DEMO-NSE-BADTOTAL"
    assert ifs["status"] == "OK" and ifs["currency"] == "USD"
    for u in (unk, before):
        assert u["status"] == "UNKNOWN" and u["expected_total"] is None and u["file_id"] is None  # no file => UNKNOWN, never OK


def test_latest_clearing_file_selection_is_deterministic_good_file_wins(tenant):
    # with both the good and the bad-total file seeded for the same account the default ETD run must always use the good file
    for _ in range(3):
        r = calc(tenant, {"market_type": "ETD_DOMESTIC", "account_id": NSE, "venue": "DEMO_NSE", "as_of": AS_OF}, "SNAP-ETD-001", "ETD_IMPORTED_V1", terms=False)
        assert r["status"] == "OK" and r["quality"]["file_id"] == "CLR-DEMO-NSE-20260817"


# ------------------------------------------------------------------------------------------ (2) evidence contract
def events_for(tenant, cid):
    with SessionLocal() as s:
        return s.execute(select(EvEvent).where(EvEvent.tenant_id == tenant, EvEvent.correlation_id == cid).order_by(EvEvent.sequence)).scalars().all()


def replay(tenant, cid):
    with SessionLocal() as s:
        out = services.call("evidence.replay_run", s, tenant, "run-" + cid, cid, "tester")
        s.commit()
        return out


def test_margin_event_carries_parents_and_replay_spec(tenant, monkeypatch):
    monkeypatch.setitem(services._REGISTRY, "regulatory.rule_parameters", lambda session, tenant_id, rule_set_hash: {"rules": []})
    cid = "cid-" + uuid.uuid4().hex[:8]
    r = calc(tenant, otc_ctx(cid), "SNAP-SHOCK-001", rules="a" * 64)
    e = [x for x in events_for(tenant, cid) if x.action == "MARGIN_COMPUTED"][0]
    kinds = {(p["entity_type"], p["entity_id"]) for p in e.payload["parents"]}
    assert ("risk_snapshot", "SNAP-SHOCK-001") in kinds and ("rule_set", "a" * 64) in kinds and ("contract_terms_hash", content_hash(TERMS)) in kinds
    rp = e.payload["replay"]
    assert rp["replayer"] == "replay.risk" and rp["engine_version"].startswith("aether-risk/") and rp["method_version"].startswith("OTC_VM_SIMPLE_V1/")
    assert rp["values"]["required_amount"] == "9500000" and rp["business_hash"] == svc.business_hash(r)
    assert e.payload["snapshot_as_of"] == "2026-08-17T04:00:00+00:00"
    # only the pinned snapshot is a SNAPSHOT-stage parent; the engine also lists no parent it cannot name
    assert all(p["entity_id"] for p in e.payload["parents"])


def test_replay_matches_and_is_read_only(tenant):
    cid = "cid-" + uuid.uuid4().hex[:8]
    calc(tenant, otc_ctx(cid), "SNAP-SHOCK-001")
    with SessionLocal() as s:
        before = s.execute(select(func.count()).select_from(RiskResult).where(RiskResult.tenant_id == tenant)).scalar_one()
    out = replay(tenant, cid)
    assert out["status"] == "MATCH" and out["items"][0]["status"] == "MATCH" and out["read_only"] is True
    with SessionLocal() as s:
        after = s.execute(select(func.count()).select_from(RiskResult).where(RiskResult.tenant_id == tenant)).scalar_one()
    assert after == before  # replay wrote no result (and bypassed the cache: otherwise it would trivially match itself)


def test_replay_etd_match_with_tolerance_declared_for_float_analytics(tenant):
    cid = "cid-" + uuid.uuid4().hex[:8]
    r = calc(tenant, {"market_type": "ETD_DOMESTIC", "account_id": NSE, "venue": "DEMO_NSE", "as_of": AS_OF, "hypothetical_margin_multiplier": "1.25",
                      "correlation_id": cid}, "SNAP-ETD-001", "ETD_IMPORTED_V1", terms=False)
    assert r["required_amount"]["amount"] == "12500000.00" or D(r["required_amount"]["amount"]) == D("12500000")
    e = [x for x in events_for(tenant, cid) if x.action == "MARGIN_COMPUTED"][0]
    assert e.payload["replay"]["tolerance"] == "0.01" and "internal_estimated_loss" in e.payload["replay"]["values"]
    assert {"entity_type": "risk_clearing_file", "entity_id": "CLR-DEMO-NSE-20260817#1"} in e.payload["parents"]
    assert replay(tenant, cid)["status"] == "MATCH"


def test_replay_diverges_when_persisted_snapshot_is_perturbed(tenant):
    cid = "cid-" + uuid.uuid4().hex[:8]
    calc(tenant, otc_ctx(cid), "SNAP-SHOCK-001")
    with SessionLocal() as s:  # simulate silent corruption of an "immutable" input (the API cannot do this)
        row = s.execute(select(RiskSnapshot).where(RiskSnapshot.tenant_id == tenant, RiskSnapshot.snapshot_id == "SNAP-SHOCK-001")).scalar_one()
        row.payload = shock_payload(row.payload, {"curve_shift_bp": "50"})
        s.commit()
    out = replay(tenant, cid)
    assert out["status"] == "DIVERGED" and out["first_divergence"]["status"] == "DIVERGED"


def test_replay_not_replayable_for_unknown_method_version_or_methodology(tenant):
    cid = "cid-" + uuid.uuid4().hex[:8]
    spec = {"replayer": "replay.risk", "engine_version": svc.ENGINE_VERSION, "method_version": "OTC_VM_SIMPLE_V1/vector/0.0.1-removed",
            "business_hash": "0" * 64, "inputs": {"context": otc_ctx(), "snapshot_id": "SNAP-SHOCK-001", "terms_hash": None, "rule_set_hash": None,
                                                 "methodology": "OTC_VM_SIMPLE_V1"}}
    with SessionLocal() as s:
        ev.append(s, tenant, engine="risk", action="MARGIN_COMPUTED", actor_id="t", actor_type="SERVICE", entity_type="risk_result",
                  entity_id="old-1", correlation_id=cid, payload={"replay": spec})
        s.commit()
    out = replay(tenant, cid)
    assert out["status"] == "NOT_REPLAYABLE" and out["items"][0]["reason"] == "VERSION_MISMATCH"  # never silently the current version
    cid2 = "cid-" + uuid.uuid4().hex[:8]
    bad = json.loads(json.dumps(spec))
    bad["inputs"]["methodology"] = "SIMM_V2"
    bad["method_version"] = f"SIMM_V2/{svc.METHOD_VERSION}"
    with SessionLocal() as s:
        ev.append(s, tenant, engine="risk", action="MARGIN_COMPUTED", actor_id="t", actor_type="SERVICE", entity_type="risk_result",
                  entity_id="old-2", correlation_id=cid2, payload={"replay": bad})
        s.commit()
    out2 = replay(tenant, cid2)
    assert out2["status"] == "NOT_REPLAYABLE" and "REPLAYER_ERROR" in out2["items"][0]["reason"]


def test_replay_ignores_trades_added_after_the_original_run(tenant):
    cid = "cid-" + uuid.uuid4().hex[:8]
    first = calc(tenant, otc_ctx(cid), "SNAP-SHOCK-001")
    extra = dict(next(t for t in PF["trades"] if t.get("netting_set_id") == NS), trade_id="TRD-LATE-ADDED")
    with SessionLocal() as s:
        svc.import_portfolio(s, tenant, trades=[extra], ctx={"actor_id": "t"})
        s.commit()
    fresh = calc(tenant, otc_ctx(), "SNAP-SHOCK-001")
    assert fresh["exposure"]["amount"] != first["exposure"]["amount"]  # a new compute DOES see the new trade...
    assert replay(tenant, cid)["status"] == "MATCH"                     # ...but replay reproduces the original trade set exactly


# ------------------------------------------------------------------------------------------ (3) atomic result cache
def _race(tenant, n, barrier_on):
    """n sessions compute the SAME margin concurrently (aligned just before persist on PostgreSQL)."""
    ctx = otc_ctx()
    out, errs = [], []
    real = svc._persist
    bar = threading.Barrier(n, timeout=30) if barrier_on else None

    def aligned(*a, **k):
        if bar is not None:
            bar.wait()
        return real(*a, **k)
    svc._persist = aligned

    def work():
        try:
            with SessionLocal() as s:
                r = svc.compute_margin(s, tenant, dict(ctx), "SNAP-SHOCK-001", content_hash(TERMS), None, "OTC_VM_SIMPLE_V1")
                s.commit()
                out.append(r["result_id"])
        except Exception as e:  # noqa: BLE001
            errs.append(repr(e))
    try:
        th = [threading.Thread(target=work) for _ in range(n)]
        [t.start() for t in th]
        [t.join() for t in th]
    finally:
        svc._persist = real
    return out, errs


def _counts(tenant):
    with SessionLocal() as s:
        ok_rows = s.execute(select(func.count()).select_from(RiskResult).where(RiskResult.tenant_id == tenant, RiskResult.status == "OK")).scalar_one()
        events = s.execute(select(func.count()).select_from(EvEvent).where(EvEvent.tenant_id == tenant, EvEvent.action == "MARGIN_COMPUTED")).scalar_one()
        valid = ev.verify_chain(s, tenant)["valid"]
    return ok_rows, events, valid


def test_concurrent_identical_computes_store_exactly_one_result(tenant):
    out, errs = _race(tenant, 6, barrier_on=not is_sqlite())
    assert not errs, errs
    assert len(set(out)) == 1 and len(out) == 6          # every caller got the SAME immutable result
    ok_rows, events, valid = _counts(tenant)
    assert ok_rows == 1 and events == 1 and valid        # one stored result, one MARGIN_COMPUTED event, chain intact


@pytest.mark.skipif(is_sqlite(), reason="negative control needs real concurrent writers (PostgreSQL); SQLite serialises writers")
def test_negative_control_without_the_unique_index_duplicates_appear(tenant):
    """Proves the race is real and that the partial unique index is what prevents it: drop the index, race, expect >1 OK row."""
    with SessionLocal() as s:
        s.execute(text("DROP INDEX IF EXISTS uq_risk_result_cacheable"))
        s.commit()
    try:
        out, errs = _race(tenant, 4, barrier_on=True)
        assert not errs, errs
        ok_rows, _, _ = _counts(tenant)
        assert ok_rows > 1, "expected duplicate OK rows without the unique index (the gap this round closes)"
    finally:
        with SessionLocal() as s:  # restore the schema for the rest of the suite
            s.execute(text("DELETE FROM risk_results WHERE tenant_id = :t"), {"t": tenant})
            s.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_risk_result_cacheable ON risk_results (tenant_id, cache_key) "
                           "WHERE status IN ('OK','UNSUPPORTED')"))
            s.commit()


def test_unknown_rule_set_hash_blocks_with_a_reason_not_a_crash(tenant, monkeypatch):
    def not_found(session, tenant_id, rule_set_hash):
        from app.core.errors import NotFound
        raise NotFound("Rule set not found")
    monkeypatch.setitem(services._REGISTRY, "regulatory.rule_parameters", not_found)
    r = calc(tenant, otc_ctx(), "SNAP-SHOCK-001", rules="b" * 64)
    assert r["status"] == "BLOCKED" and any("not a known approved rule set" in x for x in r["blocking_reasons"])


def test_reconciliation_status_file_without_reported_total_is_unknown_not_exception_or_ok(tenant):
    csv_text = chr(10).join(["file_id,account_id,component,amount,currency,aggregation_kind,as_of",
                             "CLR-NOTOTAL,ACC-NOTOTAL,INITIAL_MARGIN,500,INR,ADDITIVE,2026-08-17T03:00:00Z"])
    with SessionLocal() as s:
        svc.import_clearing_file(s, tenant, csv_text=csv_text, venue="DEMO_NSE", ctx={"actor_id": "t"})
        s.commit()
        r = svc.reconciliation_status(s, tenant, "ACC-NOTOTAL", AS_OF)
    assert r["status"] == "UNKNOWN" and r["expected_total"] == "500" and r["imported_total"] is None and "cannot reconcile" in r["reason"]
