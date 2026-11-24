"""Service-level goldens and gates (G01-G03,G08-G10,G20,G21, V-A02..V-A10) against a real DB session.
A fake contracts.resolve_terms is injected ONLY here in tests (the API path blocks explicitly when the service is absent)."""
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.core import services
from app.core.canonical import content_hash
from app.core.errors import Conflict, Unavailable, ValidationFailed
from app.db.base import SessionLocal
from app.engines.evidence import service as ev
from app.engines.evidence.models import EvEvent
from app.engines.risk import fixtures as fx
from app.engines.risk import services as svc
from app.engines.risk.models import RiskResult, RiskSnapshot

D = Decimal
NS = "NS-MER-NWD-001"
BASE_TERMS = {"base_currency": "USD", "threshold_a": "10000000", "threshold_b": "10000000", "mta_a": "500000", "mta_b": "500000",
              "independent_amount_a": "0", "independent_amount_b": "0", "rounding_increment": "100000", "rounding_mode": "UP", "mta_semantics": "GTE"}
ZERO = {"PARTY_A": "0", "PARTY_B": "0"}


def make_provider(terms_fn):
    def provider(session, tenant_id, ns, as_of, known_at, fact_snapshot_id=None):
        t = terms_fn(as_of)
        if t is None:
            return {"status": "BLOCKED", "terms_hash": None, "blocking_reasons": ["unresolved rating trigger"], "terms": None}
        return {"status": "RESOLVED", "terms_hash": content_hash(t), "terms": t, "blocking_reasons": [], "source_ids": []}
    return provider


@pytest.fixture
def terms_provider(monkeypatch):
    holder = {"fn": lambda as_of: dict(BASE_TERMS)}
    monkeypatch.setitem(services._REGISTRY, "contracts.resolve_terms", make_provider(lambda a: holder["fn"](a)))
    return holder


@pytest.fixture
def tenant():
    t = f"risk-{uuid.uuid4().hex[:10]}"
    with SessionLocal() as s:
        fx.seed_fixtures(s, t)
        s.commit()
    return t


def otc_ctx(ns=NS, as_of="2026-08-17T04:00:00Z", held=None, **kw):
    return {"market_type": "OTC", "netting_set_id": ns, "as_of": as_of, "existing_vm_balance": held or ZERO, **kw}


def calc(tenant, ctx, snap, terms=None, method="OTC_VM_SIMPLE_V1", rules=None):
    with SessionLocal() as s:
        th = terms if terms is not None else content_hash(BASE_TERMS)
        r = svc.compute_margin(s, tenant, ctx, snap, th, rules, method)
        s.commit()
        return r


def thash(t):
    return content_hash(t)


def test_g02_quiet_zero_transfer_and_pv(tenant, terms_provider):
    r = calc(tenant, otc_ctx(), "SNAP-QUIET-001")
    assert r["status"] == "OK" and r["exposure"]["amount"] == "1500000" and r["direction"] == "NONE" and r["required_amount"]["amount"] == "0"
    assert r["trade_valuations"][0]["pv_base"]["amount"] == "1500000"
    assert r["netting"]["netting_benefit"]["amount"] == "0"


def test_g03_shock_call_9_5m(tenant, terms_provider):
    r = calc(tenant, otc_ctx(), "SNAP-SHOCK-001")
    assert r["exposure"]["amount"] == "19469510.12"
    assert r["unrounded_amount"]["amount"] == "9469510.12" and r["rounded_amount"]["amount"] == "9500000" and r["direction"] == "POST"
    assert r["currency"] == "USD" and r["method_version"] == "vector/1.0.0" and "SYNTHETIC" in r["provenance_flags"]
    assert any("citation" in w and w["citation"] for w in r["waterfall"])
    assert r["components"][0]["residual_vs_unrounded"] == "30489.88"  # 9,500,000 - 9,469,510.12


def test_g08_amendment_applies_only_from_effective_date(tenant, terms_provider):
    """Exposure 19m (EURUSD 1.30): threshold 10m before 2026-09-01, 8m after; historical run retains old terms."""
    snap = {"curves": {"EUR-FLAT0": {"currency": "EUR", "points": [["0", "1"], ["30", "1"]]}, "USD-FLAT0": {"currency": "USD", "points": [["0", "1"], ["30", "1"]]}}, "fx": {"EURUSD": "1.30"}}
    with SessionLocal() as s:
        from datetime import datetime, timezone
        svc.pin_snapshot(s, tenant, "SNAP-G08-B", datetime(2026, 8, 17, 4, tzinfo=timezone.utc), snap)
        svc.pin_snapshot(s, tenant, "SNAP-G08-A", datetime(2026, 9, 2, 4, tzinfo=timezone.utc), snap)
        s.commit()

    def fn(as_of):
        t = dict(BASE_TERMS)
        t["threshold_b"] = "8000000" if as_of.isoformat() >= "2026-09-01" else "10000000"
        return t
    terms_provider["fn"] = fn
    before = calc(tenant, otc_ctx(), "SNAP-G08-B", terms=thash(fn(__import__("datetime").datetime(2026, 8, 17, tzinfo=__import__("datetime").timezone.utc))))
    after = calc(tenant, otc_ctx(as_of="2026-09-02T04:00:00Z"), "SNAP-G08-A", terms=thash({**BASE_TERMS, "threshold_b": "8000000"}))
    assert before["exposure"]["amount"] == "19000000" and after["exposure"]["amount"] == "19000000"
    assert before["components"][0]["target"] == "9000000" and after["components"][0]["target"] == "11000000"
    with SessionLocal() as s:  # historical result reproducible and unchanged
        assert svc.get_result(s, tenant, before["result_id"])["result_hash"] == before["result_hash"]


def test_g09_cross_set_never_offsets(tenant, terms_provider):
    def trade(i, side, ns, master):
        return {"trade_id": f"G09-{i}", "master_id": master, "netting_set_id": ns, "type": "FXF", "side": side, "base_ccy": "EUR", "quote_ccy": "USD",
                "notional_base": "100000000", "strike": "1.075", "maturity": "2026-11-17", "curve_base": "EUR-FLAT0", "curve_quote": "USD-FLAT0"}
    with SessionLocal() as s:
        svc.import_portfolio(s, tenant, trades=[trade(1, "BUY", "NS-G09-A", "MA-G09-A"), trade(2, "SELL", "NS-G09-B", "MA-G09-B")])
        s.commit()
    zero_thr = {**BASE_TERMS, "threshold_a": "0", "threshold_b": "0"}
    terms_provider["fn"] = lambda a: dict(zero_thr)
    a = calc(tenant, otc_ctx("NS-G09-A"), "SNAP-QUIET-001", terms=thash(zero_thr))
    b = calc(tenant, otc_ctx("NS-G09-B"), "SNAP-QUIET-001", terms=thash(zero_thr))
    assert a["exposure"]["amount"] == "5000000" and b["exposure"]["amount"] == "-5000000"
    assert a["required_amount"]["amount"] == b["required_amount"]["amount"] == "5000000"
    assert a["components"][0]["obligor"] == "PARTY_B" and b["components"][0]["obligor"] == "PARTY_A"  # not netted to zero


def test_cross_master_trade_in_set_rejected(tenant, terms_provider):
    bad = {"trade_id": "XM-1", "master_id": "MA-OTHER", "netting_set_id": NS, "type": "FXF", "side": "BUY", "base_ccy": "EUR", "quote_ccy": "USD",
           "notional_base": "1", "strike": "1.1", "maturity": "2026-11-17", "curve_base": "EUR-FLAT0", "curve_quote": "USD-FLAT0"}
    with SessionLocal() as s:
        svc.import_portfolio(s, tenant, trades=[bad])
        s.commit()
    r = calc(tenant, otc_ctx(), "SNAP-QUIET-001")
    assert r["status"] == "BLOCKED" and any("cross-master" in x for x in r["blocking_reasons"])


def test_irs_in_set_matches_independent_reference(tenant, terms_provider):
    ns = "NS-MER-OTH-002"
    terms = {**BASE_TERMS}
    r = calc(tenant, otc_ctx(ns), "SNAP-QUIET-001")
    pv = {v["trade_id"]: D(v["pv_base"]["amount"]) for v in r["trade_valuations"]}
    assert pv["TRD-FXF-OTH-001"] == D("-5000000")
    assert abs(pv["TRD-IRS-OTH-001"] - D("418209.091704")) < D("0.001")  # fixtures/expected/risk/irs_2y_semi.json
    assert abs(D(r["exposure"]["amount"]) - (D("-5000000") + pv["TRD-IRS-OTH-001"])) < D("0.0000001")
    n = r["netting"]
    assert D(n["gross_positive"]["amount"]) == pv["TRD-IRS-OTH-001"] and D(n["gross_negative"]["amount"]) == D("-5000000")
    del terms


def test_opposite_signs_cancel_within_same_set(tenant, terms_provider):
    t2 = {"trade_id": "OPP-1", "master_id": "MA-MER-NWD-001", "netting_set_id": NS, "type": "FXF", "side": "SELL", "base_ccy": "EUR", "quote_ccy": "USD",
          "notional_base": "100000000", "strike": "1.11", "maturity": "2026-11-17", "curve_base": "EUR-FLAT0", "curve_quote": "USD-FLAT0"}
    with SessionLocal() as s:
        svc.import_portfolio(s, tenant, trades=[t2])
        s.commit()
    r = calc(tenant, otc_ctx(), "SNAP-QUIET-001")
    assert r["exposure"]["amount"] == "0" and r["netting"]["netting_benefit"]["amount"] == "1500000"


def test_g21_stale_snapshot_blocks_then_new_run_ok(tenant, terms_provider):
    r = calc(tenant, otc_ctx(), "SNAP-STALE-001")
    assert r["status"] == "BLOCKED" and any("STALE_MARKET" in x for x in r["blocking_reasons"]) and r["required_amount"] is None
    assert r["quality"]["stale"] is True
    ok = calc(tenant, otc_ctx(), "SNAP-QUIET-001")
    assert ok["status"] == "OK" and ok["result_id"] != r["result_id"]


def test_blocked_results_are_never_cached_as_current(tenant, terms_provider):
    r1 = calc(tenant, otc_ctx(), "SNAP-QUIET-001", terms="f" * 64)  # stale approval hash
    assert r1["status"] == "BLOCKED" and any("terms_hash mismatch" in x for x in r1["blocking_reasons"])
    r2 = calc(tenant, otc_ctx(), "SNAP-QUIET-001")
    assert r2["status"] == "OK"


def test_missing_mta_blocks(tenant, terms_provider):
    t = dict(BASE_TERMS)
    del t["mta_b"]
    terms_provider["fn"] = lambda a: dict(t)
    r = calc(tenant, otc_ctx(), "SNAP-QUIET-001", terms=thash(t))
    assert r["status"] == "BLOCKED" and any("mta_b" in x for x in r["blocking_reasons"])


def test_unresolved_terms_block(tenant, terms_provider):
    terms_provider["fn"] = lambda a: None
    r = calc(tenant, otc_ctx(), "SNAP-QUIET-001")
    assert r["status"] == "BLOCKED" and any("rating trigger" in x for x in r["blocking_reasons"])


def test_contracts_service_absent_blocks_explicitly(tenant, monkeypatch):
    monkeypatch.delitem(services._REGISTRY, "contracts.resolve_terms", raising=False)
    r = calc(tenant, otc_ctx(), "SNAP-QUIET-001")
    assert r["status"] == "BLOCKED" and "no default terms" in " ".join(r["blocking_reasons"])


def test_missing_context_and_inputs_block(tenant, terms_provider):
    assert calc(tenant, {"market_type": "OTC", "netting_set_id": NS, "as_of": "2026-08-17T04:00:00Z"}, "SNAP-QUIET-001")["status"] == "BLOCKED"  # no held balance
    assert calc(tenant, otc_ctx(), None)["status"] == "BLOCKED"
    assert calc(tenant, otc_ctx(), "SNAP-NOPE")["status"] == "BLOCKED"
    assert calc(tenant, otc_ctx("NS-NOPE"), "SNAP-QUIET-001")["status"] == "BLOCKED"  # unmapped set: never assumed zero
    with pytest.raises(ValidationFailed):
        calc(tenant, {"market_type": "OTC"}, "SNAP-QUIET-001")  # no implicit clock
    with pytest.raises(ValidationFailed):
        calc(tenant, otc_ctx(floatish=1.5), "SNAP-QUIET-001")  # floats rejected


def test_expired_instrument_blocks(tenant, terms_provider):
    old = {"trade_id": "EXP-1", "master_id": "MA-EXP", "netting_set_id": "NS-EXP", "type": "FXF", "side": "BUY", "base_ccy": "EUR", "quote_ccy": "USD",
           "notional_base": "1", "strike": "1.1", "maturity": "2026-08-01", "curve_base": "EUR-FLAT0", "curve_quote": "USD-FLAT0"}
    with SessionLocal() as s:
        svc.import_portfolio(s, tenant, trades=[old])
        s.commit()
    r = calc(tenant, otc_ctx("NS-EXP"), "SNAP-QUIET-001")
    assert r["status"] == "BLOCKED" and any("expired" in x for x in r["blocking_reasons"])


def test_malformed_trade_import_is_atomic(tenant):
    good = {"trade_id": "OK-1", "master_id": "M", "netting_set_id": "N", "type": "FXF", "side": "BUY", "base_ccy": "EUR", "quote_ccy": "USD",
            "notional_base": "1", "strike": "1.1", "maturity": "2026-11-17", "curve_base": "a", "curve_quote": "b"}
    bad = dict(good, trade_id="BAD-1", notional_base="-5")
    with SessionLocal() as s:
        with pytest.raises(ValidationFailed):
            svc.import_portfolio(s, tenant, trades=[good, bad])
        s.rollback()
        assert s.execute(select(func.count()).select_from(__import__("app.engines.risk.models", fromlist=["RiskTrade"]).RiskTrade).where(
            __import__("app.engines.risk.models", fromlist=["RiskTrade"]).RiskTrade.trade_id == "OK-1")).scalar_one() == 0
    with SessionLocal() as s:
        with pytest.raises(Conflict):  # immutable: same id, different terms
            svc.import_portfolio(s, tenant, trades=[dict(good, trade_id="TRD-FXF-001", strike="9")])


def test_unsupported_model_and_ccp(tenant, terms_provider):
    r = calc(tenant, otc_ctx(), "SNAP-QUIET-001", method="SIMM_V2_5")
    assert r["status"] == "BLOCKED" and "UNSUPPORTED_MODEL" in r["blocking_reasons"][0]
    c = calc(tenant, {"market_type": "CCP_OTC", "as_of": "2026-08-17T04:00:00Z", "netting_set_id": NS}, "SNAP-QUIET-001")
    assert c["status"] == "UNSUPPORTED" and "CCP" in c["blocking_reasons"][0] and c["required_amount"] is None


def test_result_cache_idempotent_no_duplicate_evidence(tenant, terms_provider):
    a = calc(tenant, otc_ctx(), "SNAP-SHOCK-001")
    with SessionLocal() as s:
        n1 = s.execute(select(func.count()).select_from(EvEvent).where(EvEvent.tenant_id == tenant, EvEvent.action == "MARGIN_COMPUTED")).scalar_one()
    b = calc(tenant, otc_ctx(), "SNAP-SHOCK-001")
    with SessionLocal() as s:
        n2 = s.execute(select(func.count()).select_from(EvEvent).where(EvEvent.tenant_id == tenant, EvEvent.action == "MARGIN_COMPUTED")).scalar_one()
    assert a["result_id"] == b["result_id"] and n1 == n2 == 1
    c = calc(tenant, otc_ctx(held={"PARTY_A": "0", "PARTY_B": "1000000"}), "SNAP-SHOCK-001")  # different input => new result
    assert c["result_id"] != a["result_id"] and c["rounded_amount"]["amount"] == "8500000"


def test_evidence_emitted_and_chain_valid(tenant, terms_provider):
    calc(tenant, otc_ctx(), "SNAP-SHOCK-001")
    with SessionLocal() as s:
        actions = {e.action for e in s.execute(select(EvEvent).where(EvEvent.tenant_id == tenant)).scalars()}
        assert {"SNAPSHOT_PINNED", "PORTFOLIO_IMPORTED", "CLEARING_FILE_IMPORTED", "MARGIN_COMPUTED"} <= actions
        assert ev.verify_chain(s, tenant)["valid"]


def test_g24_evidence_unavailable_rolls_back_result(tenant, terms_provider, monkeypatch):
    def boom(*a, **k):
        raise Unavailable("evidence store down")
    monkeypatch.setitem(services._REGISTRY, "evidence.append", boom)
    s = SessionLocal()
    with pytest.raises(Unavailable):
        svc.compute_margin(s, tenant, otc_ctx(), "SNAP-QUIET-001", thash(BASE_TERMS), None, "OTC_VM_SIMPLE_V1")
    s.rollback()
    s.close()
    with SessionLocal() as s2:
        assert s2.execute(select(func.count()).select_from(RiskResult).where(RiskResult.tenant_id == tenant)).scalar_one() == 0


# ------------------------------------------------------------------------ ETD
ETD = {"market_type": "ETD_DOMESTIC", "account_id": "ACC-DEMO-NSE-001", "venue": "DEMO_NSE", "as_of": "2026-08-17T04:00:00Z"}


def test_g01_margin_side_baseline_and_hypothetical_revision(tenant):
    base = calc(tenant, ETD, "SNAP-ETD-001", method="ETD_IMPORTED_V1", terms=None)
    assert base["status"] == "OK" and base["required_amount"] == {"amount": "10000000", "currency": "INR"} and base["hypothetical"] is False
    hyp = calc(tenant, {**ETD, "hypothetical_margin_multiplier": "1.25"}, "SNAP-ETD-001", method="ETD_IMPORTED_V1", terms=None)
    assert hyp["required_amount"]["amount"] == "12500000" and hyp["hypothetical"] is True and "HYPOTHETICAL_RULE_PARAMETER" in hyp["provenance_flags"]
    delta = D(hyp["required_amount"]["amount"]) - D(base["required_amount"]["amount"])
    assert delta == D("2500000")  # +25 lakh
    comp = {c["kind"]: D(c["amount"]) for c in hyp["components"]}
    assert comp["IMPORTED"] + comp["RULE_ADJUSTMENT"] == D(hyp["required_amount"]["amount"])  # reconciles
    assert base["input_hashes"] != hyp["input_hashes"] or base["result_hash"] != hyp["result_hash"]
    assert base["internal_estimate"]["label"].startswith("INTERNAL_ESTIMATE") and "estimated_loss" in base["internal_estimate"]
    assert base["internal_estimate"]["metrics"]["gross_lots"] == "60"


def test_rule_parameters_service_drives_multiplier(tenant, monkeypatch):
    monkeypatch.setitem(services._REGISTRY, "regulatory.rule_parameters", lambda s, t, h: {"margin_multiplier": "1.25", "hypothetical": True})
    r = calc(tenant, ETD, "SNAP-ETD-001", method="ETD_IMPORTED_V1", terms=None, rules="r" * 64)
    assert r["required_amount"]["amount"] == "12500000" and r["hypothetical"] is True


def test_g10_exception_file_blocks_and_reconcile_reports_difference(tenant):
    r = calc(tenant, {**ETD, "clearing_file_id": "CLR-DEMO-NSE-BADTOTAL"}, "SNAP-ETD-001", method="ETD_IMPORTED_V1", terms=None)
    assert r["status"] == "BLOCKED" and any("reconciliation exception" in x for x in r["blocking_reasons"])
    with SessionLocal() as s:
        rec = svc.reconcile_clearing(s, tenant, "CLR-DEMO-NSE-BADTOTAL", "SNAP-ETD-001")
        ok = svc.reconcile_clearing(s, tenant, "CLR-DEMO-NSE-20260817", "SNAP-ETD-001")
        s.commit()
    assert rec["status"] == "EXCEPTION" and rec["difference"]["amount"] == "100000" and rec["computed_total"]["amount"] == "10000000"
    assert ok["status"] == "RECONCILED" and ok["source_obligation_label"].startswith("IMPORTED_SOURCE_OBLIGATION")


def test_clearing_import_duplicate_corrupt_and_revision_rules(tenant):
    csv = (fx.FIX / "clearing_nse_001.csv").read_text()
    with SessionLocal() as s:
        again = svc.import_clearing_file(s, tenant, csv_text=csv, venue="DEMO_NSE")  # idempotent retry
        assert again["status"] == "RECONCILED"
        with pytest.raises(ValidationFailed, match="Checksum"):
            svc.import_clearing_file(s, tenant, csv_text=csv, venue="DEMO_NSE", expected_sha256="0" * 64)
        with pytest.raises(ValidationFailed):
            svc.import_clearing_file(s, tenant, csv_text=csv + csv.splitlines()[1] + "\n", venue="DEMO_NSE", revision=2)  # duplicate component
        with pytest.raises(Conflict):
            svc.import_clearing_file(s, tenant, csv_text=csv.replace("8000000", "8000001").replace("10000000", "10000001"), venue="DEMO_NSE")
        rev2 = svc.import_clearing_file(s, tenant, csv_text=csv.replace("8000000", "8000001").replace("10000000", "10000001"), venue="DEMO_NSE", revision=2)
        assert rev2["revision"] == 2 and rev2["status"] == "RECONCILED"
        s.commit()
    r = calc(tenant, ETD, "SNAP-ETD-001", method="ETD_IMPORTED_V1", terms=None)
    assert r["required_amount"]["amount"] == "10000001"  # latest revision used, recorded in quality
    assert r["quality"]["file_revision"] == 2


def test_stale_clearing_file_blocks(tenant):
    r = calc(tenant, {**ETD, "as_of": "2026-08-19T04:00:00Z"}, "SNAP-ETD-001", method="ETD_IMPORTED_V1", terms=None)
    assert r["status"] == "BLOCKED"  # snapshot is 2d old -> STALE_MARKET (file stale too)


def test_g20_ifsc_usd_profile_independent_of_domestic(tenant):
    ifsc = {"market_type": "ETD_IFSC", "account_id": "ACC-DEMO-IFSC-001", "venue": "DEMO_IFSC", "as_of": "2026-08-17T04:00:00Z"}
    r = calc(tenant, ifsc, "SNAP-IFSC-001", method="ETD_IMPORTED_V1", terms=None)
    assert r["status"] == "OK" and r["required_amount"] == {"amount": "150000", "currency": "USD"}
    mixed = calc(tenant, {**ifsc, "venue": "DEMO_NSE"}, "SNAP-IFSC-001", method="ETD_IMPORTED_V1", terms=None)
    assert mixed["status"] == "BLOCKED" and "independent" in mixed["blocking_reasons"][0]
    wrong = calc(tenant, {**ETD, "account_id": "ACC-DEMO-IFSC-001"}, "SNAP-ETD-001", method="ETD_IMPORTED_V1", terms=None)
    assert wrong["status"] == "BLOCKED"  # IFSC file under the domestic profile: venue/currency mismatch
    with SessionLocal() as s:
        with pytest.raises(ValidationFailed, match="profile"):
            svc.import_clearing_file(s, tenant, csv_text=(fx.FIX / "clearing_ifsc_001.csv").read_text().replace("CLR-DEMO-IFSC", "CLR-X"), venue="DEMO_NSE")


def test_etd_without_market_methodology_mismatch(tenant):
    r = calc(tenant, ETD, "SNAP-ETD-001", method="OTC_VM_SIMPLE_V1", terms=None)
    assert r["status"] == "BLOCKED" and "not valid for market" in r["blocking_reasons"][0]


# ------------------------------------------------------------------------ stress
def test_stress_fx_shock_hand_value_and_baseline_untouched(tenant, terms_provider):
    base = calc(tenant, otc_ctx(), "SNAP-QUIET-001")
    with SessionLocal() as s:
        h_before = s.execute(select(RiskSnapshot.content_hash).where(RiskSnapshot.tenant_id == tenant, RiskSnapshot.snapshot_id == "SNAP-QUIET-001")).scalar_one()
        st = svc.stress(s, tenant, base["result_id"], {"fx_shock_pct": {"EURUSD": "0.10"}})
        s.commit()
    # hand: 1.125*1.10 = 1.2375 ; 100m*(1.2375-1.11) = 12,750,000 ; delta = 11,250,000
    assert st["scenario"]["exposure"]["amount"] == "12750000" and st["delta"]["exposure"]["amount"] == "11250000"
    assert st["baseline"]["exposure"]["amount"] == "1500000" and st["baseline_mutated"] is False
    # scenario: target 12.75m-10m=2.75m, delta 2.75m >= MTA -> rounded up 2,800,000
    assert st["scenario"]["required"]["amount"] == "2800000"
    with SessionLocal() as s:
        assert s.execute(select(RiskSnapshot.content_hash).where(RiskSnapshot.tenant_id == tenant, RiskSnapshot.snapshot_id == "SNAP-QUIET-001")).scalar_one() == h_before
        assert svc.get_result(s, tenant, base["result_id"])["exposure"]["amount"] == "1500000"
    # DV01 hand value: both DFs shift by exp(-1e-4*92/365); PV = 1.5m*DF => 1.5e6*(exp(-2.52054795e-5)-1) = -37.80774 (x - x^2/2 expansion)
    assert abs(D(st["dv01"]["value"]["amount"]) - D("-37.80774")) < D("0.001")


def test_stress_rate_shock_irs_has_dv01(tenant, terms_provider):
    base = calc(tenant, otc_ctx("NS-MER-OTH-002"), "SNAP-QUIET-001")
    with SessionLocal() as s:
        st = svc.stress(s, tenant, base["result_id"], {"curve_shift_bp": "100"})
        s.commit()
    assert D(st["delta"]["exposure"]["amount"]) != 0 and st["dv01"]["method"].startswith("finite difference")
    assert {b["trade_id"] for b in st["breakdown"]} == {"TRD-FXF-OTH-001", "TRD-IRS-OTH-001"}


def test_stress_etd_internal_only_and_blocked_base_refused(tenant, terms_provider):
    base = calc(tenant, ETD, "SNAP-ETD-001", method="ETD_IMPORTED_V1", terms=None)
    with SessionLocal() as s:
        st = svc.stress(s, tenant, base["result_id"], {"price_shock_pct": "-0.05", "vol_shock_abs": "0.02"})
        s.commit()
    assert st["scenario"]["label"].startswith("INTERNAL_ESTIMATE") and "unchanged" in st["scenario"]["note"] or "not re-derived" in st["scenario"]["note"]
    fut = next(b for b in st["breakdown"] if b["instrument_id"].startswith("FUT"))
    assert fut["scenario_pnl"]["amount"] == "-1200000"  # hand: 40 lots * 25 * 24000 * -5% = -1,200,000
    blocked = calc(tenant, otc_ctx(), "SNAP-STALE-001")
    with SessionLocal() as s:
        with pytest.raises(Conflict):
            svc.stress(s, tenant, blocked["result_id"], {"curve_shift_bp": "1"})


def test_tenant_isolation_in_service(tenant, terms_provider):
    other = f"risk-{uuid.uuid4().hex[:10]}"
    base = calc(tenant, otc_ctx(), "SNAP-QUIET-001")
    from app.core.errors import NotFound
    assert calc(other, otc_ctx(), "SNAP-QUIET-001")["status"] == "BLOCKED"  # no snapshot in the other tenant
    with SessionLocal() as s:
        with pytest.raises(NotFound):
            svc.get_result(s, other, base["result_id"])
