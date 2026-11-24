"""Service-level golden cases and lifecycle gates (A-A01..A-A09). Expected values come from fixtures/expected/collateral."""
import json
import threading
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select, text

from app.core import services
from app.core.errors import Blocked, Conflict, Forbidden, NotFound, Unavailable, ValidationFailed
from app.db.base import SessionLocal
from app.engines.collateral import services as S
from app.engines.collateral.fixtures import etd_domestic, etd_ifsc, otc, seed_fixtures
from app.engines.collateral.models import ColLot, ColProposal, ColReservation
from tests.engines.collateral.conftest import CHECKER, DEADLINE, MAKER, T, lot, principal, simple_policy, snapshot, uid

EXP = json.loads((Path(__file__).resolve().parents[3] / "fixtures" / "expected" / "collateral" / "golden.json").read_text())
D = Decimal


def run(db, mid, sid, **kw):
    out = S.optimise(db, kw.pop("tenant", T), mid, sid, kw.pop("rule_set_hash", None), kw.pop("terms_hash", None),
                     kw.pop("deadline", DEADLINE), kw.pop("horizon", 30), actor_id="u-maker", actor_type="USER", actor_role="maker", **kw)
    db.commit()
    return out


def toy_policy():
    return simple_policy({"asset_class": "GSEC", "currency": "INR", "valuation_pct": "100"},
                         {"asset_class": "CASH", "currency": "INR", "valuation_pct": "100"})


def approve(db, pid, who=CHECKER, **kw):
    out = S.approve_proposal(db, T, who, pid, reason="reviewed", **kw)
    db.commit()
    return out


# ------------------------------------------------------------------ goldens
@pytest.mark.gold
def test_g05_lp_optimum_baseline_and_savings(db, up):
    sid = uid("SNAP")
    snapshot(db, sid, [lot("A", "GSEC", "60", txn="1"), lot("B", "CASH", "100", txn="3")], toy_policy())
    out = run(db, up.add_margin(), sid)
    g = EXP["G05"]
    assert out["state"] == "CHECKED" and out["solver_status"] == "optimal"
    legs = {l["lot_id"]: D(l["quantity"]) for l in out["legs"]}
    assert legs == {k: D(v) for k, v in g["legs"].items()}
    assert D(out["objective"]) == D(g["optimal_cost"])
    assert out["baseline"]["status"] == "AVAILABLE" and out["baseline"]["policy"] == "B1_CASH_ONLY"
    assert D(out["baseline"]["cost"]) == D(g["all_B_cost"]) and D(out["baseline"]["savings_pct"]) == D(g["savings_pct"])
    assert D(out["result"]["coverage_shadow_price"]["value"]) == D("3")  # LP dual, continuous only
    assert out["transmission"] == "NOT_TRANSMITTED"


@pytest.mark.gold
def test_g06_haircut_and_fx_applied_once_cross_currency(db, up):
    sid = uid("SNAP")
    pol = simple_policy({"asset_class": "GSEC", "currency": "USD", "valuation_pct": "94", "haircut_pct": "6", "fx_haircut_pct": "8"})
    snapshot(db, sid, [lot("U", "GSEC", "1000", price="100", ccy="USD", fx_to_target="1")], pol)
    mid = up.add_margin(required_amount={"amount": EXP["G06"]["required"], "currency": "INR"}, rounded_amount=EXP["G06"]["required"])
    out = run(db, mid, sid)
    leg = out["legs"][0]
    assert D(leg["valuation_factor"]) == D("0.94") and D(leg["fx_factor"]) == D("0.92")
    assert D(leg["effective_coverage"]) / D(leg["quantity"]) == D(EXP["G06"]["effective_per_unit"])
    assert D(leg["quantity"]) == D(EXP["G06"]["quantity"])


@pytest.mark.gold
def test_g07_infeasible_no_instruction_no_approval(db, up):
    sid = uid("SNAP")
    snapshot(db, sid, [lot("A", "GSEC", "50"), lot("B", "CASH", "30")], toy_policy())
    out = run(db, up.add_margin(), sid)
    g = EXP["G07"]
    assert out["state"] == g["state"] and out["solver_status"] == "infeasible" and out["legs"] == []
    assert D(out["result"]["diagnostics"]["remaining"]) == D(g["remaining"])
    assert D(out["result"]["diagnostics"]["achievable_coverage"]) == D(g["eligible_max_coverage"])
    assert out["baseline"]["status"] == "NOT_COMPUTED"  # no savings card for an infeasible problem
    with pytest.raises(Conflict):
        S.approve_proposal(db, T, CHECKER, out["proposal_id"], reason="x")
    db.rollback()
    with pytest.raises(Conflict):
        S.draft_instruction(db, T, out["proposal_id"], actor_id="u")
    db.rollback()
    with pytest.raises(NotFound):
        S.get_instruction(db, T, out["proposal_id"])


@pytest.mark.gold
def test_zero_call_and_return_create_no_allocation(db, up):
    sid = uid("SNAP")
    snapshot(db, sid, [lot("A", "GSEC", "50")], toy_policy(), market="OTC")
    up.terms["ACC-X"] = {"status": "RESOLVED", "terms_hash": "TH", "terms": {"eligible_schedule": toy_policy()["schedule"]}}
    # G25-style RETURN (posted 150 > target 100): ATLAS allocates nothing
    mid = up.add_margin(market_type="OTC", netting_set_id="ACC-X", account_id=None, direction="RETURN", rounded_amount="50", terms_hash="TH")
    out = run(db, mid, sid, terms_hash="TH")
    assert out["state"] == "NOT_REQUIRED" and out["legs"] == [] and out["solver_status"] == "NOT_REQUIRED"
    with pytest.raises(Conflict):
        S.approve_proposal(db, T, CHECKER, out["proposal_id"], reason="x")
    db.rollback()
    # explicit zero call
    mid0 = up.add_margin(market_type="OTC", netting_set_id="ACC-X", account_id=None, direction="NONE", rounded_amount="0", terms_hash="TH")
    assert run(db, mid0, sid, terms_hash="TH")["state"] == "NOT_REQUIRED"


@pytest.mark.gold
def test_g01_side_posted_coverage_and_allocation_with_exclusions(db, up):
    t = uid("t")
    seed_fixtures(db, t)
    db.commit()
    g = EXP["G01_allocation"]
    cov = S.posted_coverage(db, t, "ACC-DEMO-NSE-001", datetime(2026, 8, 17, 4, tzinfo=timezone.utc))
    assert cov["eligible_posted"] == {"amount": EXP["G01_posted_coverage"]["total"], "currency": "INR"}
    mid = up.add_margin(account_id="ACC-DEMO-NSE-001", required_amount={"amount": g["required_total_inr"], "currency": "INR"})
    out = S.optimise(db, t, mid, "INV-ETD-001", None, None, DEADLINE, g["horizon_days"], actor_id="u-maker", actor_type="USER")
    db.commit()
    assert D(out["required_coverage"]) == D(g["needed_inr"])
    assert [l["lot_id"] for l in out["legs"]] == [g["selected_lot"]]
    assert abs(D(out["objective"]) - D(g["objective_inr"])) <= D(g["objective_tolerance"])
    assert out["state"] == "CHECKED" and out["result"]["verification"]["ok"]
    assert D(out["result"]["coverage"]) >= D(g["needed_inr"])
    assert out["baseline"]["policy"] == "B1_CASH_ONLY" and abs(D(out["baseline"]["cost"]) - D(g["baseline_B1_cash_only_cost_inr"])) < D("0.001")
    ex = {e["lot_id"]: e["reasons"] for e in out["result"]["exclusions"]}
    for lid, reason in g["excluded"].items():
        assert any(r.startswith(reason) for r in ex[lid]), (lid, ex.get(lid))
    assert "CORP-INR-WW" not in [l["lot_id"] for l in out["legs"]]  # G12: wrong-way never allocated


@pytest.mark.gold
def test_ifsc_usd_separate_cash_optimal_no_improvement(db, up):
    t = uid("t")
    seed_fixtures(db, t)
    db.commit()
    g = EXP["IFSC_cash_optimal"]
    mid = up.add_margin(market_type="ETD_IFSC", currency="USD", account_id="ACC-DEMO-IFSC-001",
                        required_amount={"amount": g["required_total_usd"], "currency": "USD"})
    out = S.optimise(db, t, mid, "INV-IFSC-001", None, None, DEADLINE, 30, actor_id="u-maker", actor_type="USER")
    assert D(out["required_coverage"]) == D(g["needed_usd"])
    assert [l["lot_id"] for l in out["legs"]] == ["CASH-USD-01"]
    assert out["baseline"]["status"] == "AVAILABLE" and out["baseline"]["savings_absolute"] == g["savings_absolute"]
    ex = {e["lot_id"]: e["reasons"] for e in out["result"]["exclusions"]}
    assert "MISSING_FX_HAIRCUT:CLEARING_POLICY" in ex["CASH-INR-X"]  # cross-currency without a reviewed FX haircut is excluded


@pytest.mark.gold
def test_g20_markets_and_currencies_never_blended(db, up):
    t = uid("t")
    seed_fixtures(db, t)
    db.commit()
    mid = up.add_margin(account_id="ACC-DEMO-NSE-001")  # domestic INR margin result vs IFSC USD pool
    with pytest.raises(Blocked) as e:
        S.optimise(db, t, mid, "INV-IFSC-001", None, None, DEADLINE, 30, actor_id="u")
    assert e.value.code == "MARKET_MISMATCH"
    mid2 = up.add_margin(market_type="ETD_IFSC", currency="INR", account_id="A")
    with pytest.raises(Blocked) as e2:
        S.optimise(db, t, mid2, "INV-IFSC-001", None, None, DEADLINE, 30, actor_id="u")
    assert e2.value.code == "CURRENCY_MISMATCH"


def test_otc_flow_uses_contract_schedule_and_blocks_stale_or_unresolved_terms(db, up):
    t = uid("t")
    seed_fixtures(db, t)
    db.commit()
    ns = "NS-MER-NWD-001"
    sched = [{"asset_class": "CASH", "valuation_pct": "100", "haircut_pct": "0"},
             {"asset_class": "GSEC", "currency": "USD", "valuation_pct": "98", "haircut_pct": "2", "min_rating": "AA"},
             {"asset_class": "CORP", "currency": "USD", "valuation_pct": "85", "min_rating": "A", "concentration_cap_pct": "20",
              "cap_denominator": "OBLIGATION"}]
    mid = up.add_margin(market_type="OTC", currency="USD", netting_set_id=ns, account_id=None, direction="POST", rounded_amount="9500000",
                        required_amount={"amount": "9469510.12", "currency": "USD"}, terms_hash="TH1")
    with pytest.raises(Blocked) as e:  # terms unresolved (e.g. unresolved rating trigger / missing MTA) => no allocation
        S.optimise(db, t, mid, "INV-OTC-001", None, "TH1", DEADLINE, 30, actor_id="u")
    assert e.value.code == "TERMS_BLOCKED"
    up.terms[ns] = {"status": "RESOLVED", "terms_hash": "TH1", "terms": {"eligible_schedule": sched}}
    out = S.optimise(db, t, mid, "INV-OTC-001", None, "TH1", DEADLINE, 30, actor_id="u-maker", actor_type="USER")
    db.commit()
    assert D(out["required_coverage"]) == D("9500000") and out["state"] == "CHECKED"
    assert [l["lot_id"] for l in out["legs"]] == [EXP["OTC_G03_call"]["selected_lot"]]
    up.terms[ns] = {"status": "RESOLVED", "terms_hash": "TH2", "terms": {"eligible_schedule": sched}}  # amendment
    with pytest.raises(Blocked) as e2:
        S.optimise(db, t, mid, "INV-OTC-001", None, "TH1", DEADLINE, 30, actor_id="u")
    assert e2.value.code == "STALE_TERMS"
    # contract schedule intersects with the margin's wrong-way restriction: Northwind-group bond never chosen
    assert "CORP-USD-WW" not in [l["lot_id"] for l in out["legs"]]


def test_invalid_eligibility_schedule_rejected(db, up):
    t = uid("t")
    seed_fixtures(db, t)
    db.commit()
    ns = "NS-MER-NWD-001"
    up.terms[ns] = {"status": "RESOLVED", "terms_hash": "TH", "terms": {"eligible_schedule": [
        {"asset_class": "GSEC", "valuation_pct": "94", "haircut_pct": "94"}]}}  # double-applied haircut
    mid = up.add_margin(market_type="OTC", currency="USD", netting_set_id=ns, account_id=None, rounded_amount="1000", terms_hash="TH")
    with pytest.raises(Blocked) as e:
        S.optimise(db, t, mid, "INV-OTC-001", None, "TH", DEADLINE, 30, actor_id="u")
    assert e.value.code == "INVALID_ELIGIBILITY_SCHEDULE"


def test_unsupported_cleared_otc_and_blocked_margin_produce_no_allocation(db, up):
    sid = uid("SNAP")
    snapshot(db, sid, [lot("A", "GSEC", "50")], toy_policy())
    for kw in ({"market_type": "CCP_OTC", "status": "UNSUPPORTED"}, {"status": "BLOCKED", "blocking_reasons": ["stale snapshot"]}):
        with pytest.raises(Blocked):
            S.optimise(db, T, up.add_margin(**kw), sid, None, None, DEADLINE, 30, actor_id="u")


def test_upstream_service_missing_is_explicit_unavailable(db, app_client):
    sid = uid("SNAP")
    snapshot(db, sid, [lot("A", "GSEC", "50")], toy_policy())
    saved = services._REGISTRY.pop("risk.get_result", None)
    try:
        with pytest.raises(Unavailable):
            S.optimise(db, T, "x", sid, None, None, DEADLINE, 30, actor_id="u")
    finally:
        if saved:
            services._REGISTRY["risk.get_result"] = saved


def test_unknown_inputs_exclude_lot_with_reason_not_default(db, up):
    sid = uid("SNAP")
    pol = simple_policy({"asset_class": "GSEC", "currency": "INR", "valuation_pct": "100", "min_rating": "AA", "max_maturity_years": "3"})
    snapshot(db, sid, [lot("NORATE", "GSEC", "100"), lot("OKAY", "GSEC", "100", rating="AAA", maturity_date="2027-01-01"),
                       lot("LONG", "GSEC", "100", rating="AAA", maturity_date="2040-01-01"), lot("NOCOST", "GSEC", "100", rate=None, rating="AAA", maturity_date="2027-01-01"),
                       lot("LOW", "GSEC", "100", rating="BBB", maturity_date="2027-01-01")], pol)
    out = run(db, up.add_margin(required_amount={"amount": "10", "currency": "INR"}, rounded_amount="10"), sid)
    ex = {e["lot_id"]: e["reasons"] for e in out["result"]["exclusions"]}
    assert "UNKNOWN_RATING" in ex["NORATE"] and "MATURITY_EXCEEDS_LIMIT" in ex["LONG"]
    assert "UNKNOWN_COST" in ex["NOCOST"] and "BELOW_MIN_RATING" in ex["LOW"]
    assert [l["lot_id"] for l in out["legs"]] == ["OKAY"]


def test_nondivisible_lots_milp_whole_blocks_and_verified(db, up):
    sid = uid("SNAP")
    snapshot(db, sid, [lot("A", "GSEC", "60", txn="1", lot_size="7"), lot("B", "CASH", "100", txn="3")], toy_policy())
    out = run(db, up.add_margin(), sid)
    a = next(l for l in out["legs"] if l["lot_id"] == "A")
    assert D(a["quantity"]) % 7 == 0 and out["result"]["verification"]["ok"]
    assert out["result"]["coverage_shadow_price"] is None  # no MILP dual claims
    assert D(out["result"]["coverage"]) >= 100


def test_concentration_caps_both_denominators_respected_end_to_end(db, up):
    sid = uid("SNAP")
    pol = simple_policy({"asset_class": "GSEC", "currency": "INR", "valuation_pct": "100"},
                        {"asset_class": "CORP", "currency": "INR", "valuation_pct": "100", "concentration_cap_pct": "30",
                         "cap_denominator": "TOTAL_ALLOCATED"},
                        issuer_group_cap_pct="40", issuer_cap_denominator="OBLIGATION")
    snapshot(db, sid, [lot("G1", "GSEC", "100", rate="0.10", issuer_group="IG1"), lot("G2", "GSEC", "100", rate="0.11", issuer_group="IG4"),
                       lot("C1", "CORP", "100", rate="0.01", issuer_group="IG2"), lot("C2", "CORP", "100", rate="0.012", issuer_group="IG3")], pol)
    out = run(db, up.add_margin(), sid)
    assert out["state"] == "CHECKED" and out["solver_status"] == "optimal"
    cov = {l["lot_id"]: D(l["effective_coverage"]) for l in out["legs"]}
    total = sum(cov.values())
    eps = D("0.000001")
    assert sum(v for k, v in cov.items() if k.startswith("C")) <= D("0.30") * total + eps          # CORP <= 30 % of total allocated coverage
    assert all(v <= D("40") + eps for v in cov.values())            # each issuer group <= 40 % of the obligation (100)
    assert cov["G1"] == D("40") or abs(cov["G1"] - D("40")) < eps     # binding: cheaper GSEC issuer fills its cap
    assert any(c["binding"] for c in out["constraints"] if c["kind"].startswith("CAP"))


# ------------------------------------------------------------------ lifecycle / approvals
def new_checked(db, up, units="100", required="80", sid=None):
    sid = sid or uid("SNAP")
    if not sid.startswith("EXISTING"):
        snapshot(db, sid, [lot("L1", "GSEC", units, txn="1")], toy_policy())
    mid = up.add_margin(required_amount={"amount": required, "currency": "INR"}, rounded_amount=required)
    return run(db, mid, sid), sid, mid


def test_full_lifecycle_states_and_not_transmitted_draft(db, up):
    acc = uid("ACC-SIM")  # own account: simulated settlement posts collateral and must not leak into other tests
    sid = uid("SNAP")
    snapshot(db, sid, [lot("L1", "GSEC", "100", txn="1", entitled=(acc,))], toy_policy())
    mid = up.add_margin(account_id=acc, required_amount={"amount": "80", "currency": "INR"})
    out = run(db, mid, sid)
    assert out["state"] == "CHECKED"
    ap = approve(db, out["proposal_id"])
    assert ap["state"] == "APPROVED" and ap["approval"]["target_hash"] == out["proposal_hash"]
    res = S.reserve(db, T, out["proposal_id"], ap["approval"]["approval_id"], actor_id="u-maker", actor_type="USER")
    db.commit()
    assert res["state"] == "RESERVED"
    lot_row = db.execute(select(ColLot).where(ColLot.snapshot_id == sid)).scalar_one()
    db.refresh(lot_row)
    assert lot_row.reserved_u == 80 * 10**6
    d = S.draft_instruction(db, T, out["proposal_id"], actor_id="u-maker")
    db.commit()
    assert d["status"] == "NOT_TRANSMITTED" and d["content"]["status"] == "NOT_TRANSMITTED" and "NOT TRANSMITTED" in d["content"]["watermark"]
    assert "ILLUSTRATIVE ONLY" in d["content"]["iso20022_like_sample"]["note"]
    assert S.get_proposal(db, T, out["proposal_id"])["state"] == "DRAFTED"
    sim = S.simulate_settlement(db, T, out["proposal_id"], actor_id="u-maker")
    db.commit()
    assert sim["simulated_settlement"]["status"] == "SIMULATED_SETTLED" and "SIMULATED" in sim["simulated_settlement"]["label"]
    # only SIMULATED-settled collateral counts as posted; it is then visible to coverage()
    cov = S.posted_coverage(db, T, acc, datetime(2026, 8, 17, 4, tzinfo=timezone.utc))
    assert D(cov["eligible_posted"]["amount"]) == D("80") and cov["eligible_posted"]["currency"] == "INR"


@pytest.mark.gold
def test_g15_self_approval_forbidden_and_non_checker_forbidden(db, up):
    out, _, _ = new_checked(db, up)
    both = principal("u-maker", "maker", "checker")  # same person as the maker
    with pytest.raises(Forbidden) as e:
        S.approve_proposal(db, T, both, out["proposal_id"], reason="self")
    assert e.value.code == "SELF_APPROVAL"
    db.rollback()
    with pytest.raises(Forbidden):
        S.approve_proposal(db, T, principal("u-other", "viewer"), out["proposal_id"], reason="x")
    db.rollback()
    with pytest.raises(ValidationFailed):
        S.approve_proposal(db, T, CHECKER, out["proposal_id"], reason="  ")


def test_cancel_releases_reservation_idempotently(db, up):
    out, sid, _ = new_checked(db, up)
    ap = approve(db, out["proposal_id"])
    S.reserve(db, T, out["proposal_id"], ap["approval"]["approval_id"], actor_id="u")
    db.commit()
    S.cancel(db, T, out["proposal_id"], actor_id="u-maker", reason="no longer needed")
    db.commit()
    row = db.execute(select(ColLot).where(ColLot.snapshot_id == sid)).scalar_one()
    db.refresh(row)
    assert row.reserved_u == 0
    assert S.cancel(db, T, out["proposal_id"], actor_id="u-maker")["state"] == "CANCELLED"  # idempotent
    assert db.execute(select(ColReservation).where(ColReservation.proposal_id == out["proposal_id"])).scalar_one().status == "RELEASED"


@pytest.mark.gold
def test_a_a07_expired_and_stale_approval_rejected(db, up):
    out, sid, mid = new_checked(db, up)
    ap = approve(db, out["proposal_id"])
    # (1) upstream margin result changed after approval -> stale
    up.margins[mid]["result_hash"] = "f" * 64
    with pytest.raises(Conflict) as e:
        S.reserve(db, T, out["proposal_id"], ap["approval"]["approval_id"], actor_id="u")
    assert e.value.code == "STALE_APPROVAL"
    db.rollback()
    # (2) approval/proposal expired
    future = datetime.now(timezone.utc) + timedelta(days=3)
    assert S.get_proposal(db, T, out["proposal_id"], now=future)["state"] == "EXPIRED"
    db.commit()
    up.margins[mid]["result_hash"] = out["result"].get("x", up.margins[mid]["result_hash"])
    with pytest.raises(Conflict):
        S.reserve(db, T, out["proposal_id"], ap["approval"]["approval_id"], actor_id="u")
    db.rollback()


def test_stale_inventory_after_competing_reservation(db, up):
    sid = uid("SNAP")
    snapshot(db, sid, [lot("L1", "GSEC", "100", txn="1")], toy_policy())
    mid = up.add_margin(required_amount={"amount": "80", "currency": "INR"}, rounded_amount="80")
    p1, p2 = run(db, mid, sid), run(db, mid, sid)
    a1, a2 = approve(db, p1["proposal_id"]), approve(db, p2["proposal_id"])
    S.reserve(db, T, p1["proposal_id"], a1["approval"]["approval_id"], actor_id="u")
    db.commit()
    with pytest.raises(Conflict) as e:
        S.reserve(db, T, p2["proposal_id"], a2["approval"]["approval_id"], actor_id="u")
    assert e.value.code == "STALE_INVENTORY"


@pytest.mark.gold
def test_g13_concurrent_reservations_no_double_pledge(db, up):
    sid = uid("SNAP")
    snapshot(db, sid, [lot("L1", "GSEC", "100", txn="1")], toy_policy())
    mid = up.add_margin(required_amount={"amount": "80", "currency": "INR"}, rounded_amount="80")
    p1, p2 = run(db, mid, sid), run(db, mid, sid)
    approvals = [approve(db, p["proposal_id"])["approval"]["approval_id"] for p in (p1, p2)]
    results: list = []
    barrier = threading.Barrier(2)

    def worker(pid, aid):
        s = SessionLocal()
        try:
            barrier.wait()
            S.reserve(s, T, pid, aid, actor_id="u")
            s.commit()
            results.append("OK")
        except Conflict as e:
            s.rollback()
            results.append(e.code)
        except Exception as e:  # noqa: BLE001
            s.rollback()
            results.append(f"ERR {type(e).__name__}: {e}")
        finally:
            s.close()

    th = [threading.Thread(target=worker, args=(p["proposal_id"], a)) for p, a in zip((p1, p2), approvals)]
    [t.start() for t in th]
    [t.join() for t in th]
    g = EXP["G13"]
    assert results.count("OK") == g["successes"], results
    assert len([r for r in results if r in ("RESERVATION_CONFLICT", "STALE_INVENTORY")]) == g["conflicts"], results
    row = db.execute(select(ColLot).where(ColLot.snapshot_id == sid)).scalar_one()
    db.refresh(row)
    assert row.reserved_u == 80 * 10**6 <= row.units_u  # never double pledged


def test_reservation_is_idempotent_retry(db, up):
    out, sid, _ = new_checked(db, up)
    ap = approve(db, out["proposal_id"])["approval"]["approval_id"]
    S.reserve(db, T, out["proposal_id"], ap, actor_id="u")
    db.commit()
    S.reserve(db, T, out["proposal_id"], ap, actor_id="u")  # retry => no second reservation
    db.commit()
    row = db.execute(select(ColLot).where(ColLot.snapshot_id == sid)).scalar_one()
    db.refresh(row)
    assert row.reserved_u == 80 * 10**6


def test_revision_conflict_on_stale_ui_view(db, up):
    out, _, _ = new_checked(db, up)
    with pytest.raises(Conflict) as e:
        S.approve_proposal(db, T, CHECKER, out["proposal_id"], reason="x", expected_revision=out["revision"] - 1)
    assert e.value.code == "REVISION_CONFLICT"


@pytest.mark.gold
def test_g24_evidence_unavailable_blocks_transition_and_rolls_back(db, up):
    out, _, _ = new_checked(db, up)
    saved = services._REGISTRY["evidence.append"]

    def boom(*a, **k):
        raise Unavailable("evidence store down")
    services.register("evidence.append", boom)
    try:
        with pytest.raises(Unavailable):
            S.approve_proposal(db, T, CHECKER, out["proposal_id"], reason="x")
        db.rollback()
    finally:
        services.register("evidence.append", saved)
    assert S.get_proposal(db, T, out["proposal_id"])["state"] == "CHECKED"  # no material transition
    assert db.execute(text("SELECT count(*) FROM core_approvals WHERE target_id=:t"), {"t": out["proposal_id"]}).scalar() == 0
    db.rollback()
    assert approve(db, out["proposal_id"])["state"] == "APPROVED"  # recovery is idempotent / clean


def test_evidence_emitted_for_each_consequential_write(db, up):
    out, _, _ = new_checked(db, up)
    ap = approve(db, out["proposal_id"])
    S.reserve(db, T, out["proposal_id"], ap["approval"]["approval_id"], actor_id="u")
    S.draft_instruction(db, T, out["proposal_id"], actor_id="u")
    db.commit()
    rows = db.execute(text("SELECT action FROM ev_events WHERE tenant_id=:t AND entity_id=:e ORDER BY sequence"),
                      {"t": T, "e": out["proposal_id"]}).scalars().all()
    assert rows == ["COLLATERAL_PROPOSAL_CREATED", "COLLATERAL_PROPOSAL_CHECKED", "COLLATERAL_PROPOSAL_APPROVED",
                    "COLLATERAL_RESERVED", "COLLATERAL_INSTRUCTION_DRAFTED"]
    ev = db.execute(text("SELECT count(*) FROM core_outbox WHERE tenant_id=:t AND event_type IN ('AllocationProposed','ReservationCreated','InstructionDrafted')"), {"t": T}).scalar()
    assert ev >= 3


def test_proposals_are_tenant_scoped(db, up):
    out, _, _ = new_checked(db, up)
    with pytest.raises(NotFound):
        S.get_proposal(db, "tenant-b", out["proposal_id"])
    db.rollback()
    with pytest.raises(NotFound):
        S.optimise(db, "tenant-b", up.add_margin(), "does-not-exist", None, None, DEADLINE, 30, actor_id="u")


# ------------------------------------------------------------------ stress
def test_stress_is_hypothetical_never_approvable_and_moves_nothing(db, up):
    t = uid("t")
    seed_fixtures(db, t)
    db.commit()
    mid = up.add_margin(account_id="ACC-DEMO-NSE-001", required_amount={"amount": "12500000", "currency": "INR"})
    st = S.stress(db, t, mid, "INV-ETD-001", None, None, DEADLINE, 30,
                  {"haircut_add_pct": "10", "cost_add_bps": "50", "unavailable_lot_ids": ["GSEC-INR-01"]}, actor_id="u-maker")
    db.commit()
    assert st["hypothetical"] and "HYPOTHETICAL" in st["label"] and st["stressed"]["kind"] == "STRESS"
    assert st["stressed"]["state"] == "HYPOTHETICAL" and "GSEC-INR-01" not in [l["lot_id"] for l in st["stressed"]["legs"]]
    assert st["stressed"]["result"]["verification"]["ok"]  # independent verifier agrees under stressed factors
    assert D(st["objective_delta"]) > 0  # losing the cheapest lot + higher funding cost raises cost
    with pytest.raises(Forbidden):
        S.approve_proposal(db, t, CHECKER, st["stressed"]["proposal_id"], reason="x")
    db.rollback()
    n = db.execute(text("SELECT sum(reserved_u) FROM col_inventory WHERE tenant_id=:t"), {"t": t}).scalar()
    assert (n or 0) == 0
    with pytest.raises(ValidationFailed):
        S.stress(db, t, mid, "INV-ETD-001", None, None, DEADLINE, 30, {"drop_table": "1"}, actor_id="u")


def test_stress_required_override_is_validated_and_marked_hypothetical(db, up):
    sid = uid("SNAP")
    snapshot(db, sid, [lot("L1", "GSEC", "1000", txn="1")], toy_policy())
    mid = up.add_margin(required_amount={"amount": "80", "currency": "INR"}, rounded_amount="80")
    st = S.stress(db, T, mid, sid, None, None, DEADLINE, 30, {"required_coverage_override": "500"}, actor_id="u")
    assert D(st["stressed"]["required_coverage"]) == D("500") and st["stressed"]["hypothetical"]
    with pytest.raises(ValidationFailed):
        S.stress(db, T, mid, sid, None, None, DEADLINE, 30, {"required_coverage_override": "-1"}, actor_id="u")


def test_desk_reports_available_by_deadline_and_reason_counts(db, up):
    t = uid("t")
    seed_fixtures(db, t)
    db.commit()
    mid = up.add_margin(account_id="ACC-DEMO-NSE-001", required_amount={"amount": "12500000", "currency": "INR"})
    d = S.desk(db, t, mid, "INV-ETD-001", None, None, DEADLINE, 30)
    assert d["required_coverage_still_needed"] == "1000000" and d["currency"] == "INR"
    assert d["exclusions_by_reason"]["WRONG_WAY_RISK"] == 1 and d["late_settling_lots"][0]["lot_id"] == "GSEC-INR-LATE"
    assert not any(l["lot_id"].startswith("POST-") for l in d["excluded_lots"])  # already-posted records are not candidates
    assert "NO_POLICY_SOURCE" not in d["exclusions_by_reason"] and "NO_AVAILABLE_UNITS" not in d["exclusions_by_reason"]
    assert D(d["eligible_effective_coverage_by_deadline"]) > D("1000000") and d["shortfall_vs_eligible"] == "0"


# ------------------------------------------------------------------ import validation
def test_import_is_atomic_and_strict(db, app_client):
    bad = [lot("OK1", "GSEC", "10"), lot("BAD", "GSEC", "-5"), {"lot_id": "OK1"}, lot("FLT", "GSEC", 1.5)]
    with pytest.raises(ValidationFailed) as e:
        S.import_inventory(db, T, actor_id="u", role=None, correlation_id="c", payload={
            "snapshot_id": "BADSNAP", "as_of": "2026-08-17T04:00:00Z", "currency": "INR", "market_type": "ETD_DOMESTIC", "policy": {}, "lots": bad})
    assert any("units" in k or "BAD" in k or "lots[1]" in k for k in e.value.field_errors)
    db.rollback()
    assert db.execute(text("SELECT count(*) FROM col_snapshots WHERE snapshot_id='BADSNAP'")).scalar() == 0  # nothing committed
    for payload in ({"currency": "XXX"}, {"market_type": "CCP_OTC"}, {"as_of": "2026-08-17T04:00:00"}):
        base = {"snapshot_id": "S", "as_of": "2026-08-17T04:00:00Z", "currency": "INR", "market_type": "ETD_DOMESTIC", "lots": [lot("A", "GSEC", "1")]}
        base.update(payload)
        with pytest.raises(ValidationFailed):
            S.import_inventory(db, T, actor_id="u", role=None, correlation_id="c", payload=base)
        db.rollback()


def test_import_idempotent_same_content_conflict_on_different(db, app_client):
    sid = uid("SNAP")
    lots = [lot("A", "GSEC", "10")]
    a = snapshot(db, sid, lots, toy_policy())
    b = snapshot(db, sid, lots, toy_policy())
    assert a["content_hash"] == b["content_hash"] and b["replayed"] is True
    with pytest.raises(Conflict):
        snapshot(db, sid, [lot("A", "GSEC", "11")], toy_policy())


def test_import_csv_strict_parse(db, app_client):
    sid = uid("SNAP")
    csv_text = ("lot_id,owner_entity,custody_account,asset_class,currency,units,price,annual_cost_rate,entitled_to\n"
                "C1,ENT-MERIDIAN,CUST,GSEC,INR,100,1,0.05,ACC-X;ACC-Y\n")
    out = S.import_inventory(db, T, actor_id="u", role=None, correlation_id="c", payload={
        "snapshot_id": sid, "as_of": "2026-08-17T04:00:00Z", "currency": "INR", "market_type": "ETD_DOMESTIC", "policy": toy_policy(), "csv": csv_text})
    assert out["lot_count"] == 1
    db.rollback()


def test_proposals_and_inventory_model_reserved_never_exceeds_units(db, up):
    sid = uid("SNAP")
    with pytest.raises(ValidationFailed):
        snapshot(db, sid, [lot("A", "GSEC", "10", encumbered_units="6", reserved_units="6")], toy_policy())
    db.rollback()


@pytest.mark.gold
def test_proposal_from_hypothetical_margin_is_labelled_and_never_approvable(db, up):
    """G09-style rule: a hypothetical rule revision can show a recommendation but never an operational approval/reservation."""
    sid = uid("SNAP")
    snapshot(db, sid, [lot("A", "GSEC", "100", txn="1")], toy_policy())
    mid = up.add_margin(hypothetical=True, required_amount={"amount": "80", "currency": "INR"}, rounded_amount="80")
    out = run(db, mid, sid)
    assert out["hypothetical"] is True and out["state"] == "CHECKED"
    with pytest.raises(Forbidden) as e:
        S.approve_proposal(db, T, CHECKER, out["proposal_id"], reason="try")
    assert e.value.code == "NOT_APPROVABLE"
    db.rollback()
