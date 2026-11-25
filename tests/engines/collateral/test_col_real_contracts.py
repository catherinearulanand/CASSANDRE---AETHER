"""ATLAS against the REAL COVENANT service (no fake terms): vocabulary/units must interoperate, and the result must satisfy the
contract's concentration caps. Found by the browser flow (INVALID_ELIGIBILITY_SCHEDULE: COVENANT says TOTAL_EFFECTIVE_ALLOCATED /
GOVERNMENT_BOND / issuer_class ANY; ATLAS said TOTAL_ALLOCATED / GSEC). Only the risk result is faked (VECTOR is covered elsewhere)."""
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.core import services
from app.core.errors import Blocked
from app.engines.collateral import services as S
from app.engines.collateral.eligibility import match_entry
from app.engines.collateral.fixtures import otc
from app.engines.contracts.fixtures import seed_fixtures as seed_contracts
from tests.engines.collateral.conftest import DEADLINE, uid

D = Decimal
NS = "NS-MER-NWD-001"


@pytest.fixture
def real_terms_env(db):
    t = uid("tc")
    seed_contracts(db, t)
    db.commit()
    S.import_inventory(db, t, actor_id="u", role="maker", payload=otc(), correlation_id="c")
    db.commit()
    terms = services.call("contracts.resolve_terms", db, t, NS, datetime(2026, 8, 17, 4, tzinfo=UTC), None)
    assert terms["status"] == "RESOLVED", terms
    margins = {}
    saved = services._REGISTRY.get("risk.get_result")
    services.register("risk.get_result", lambda s, tn, rid: margins.get(rid))
    yield t, terms, margins
    if saved is None:
        services._REGISTRY.pop("risk.get_result", None)
    else:
        services._REGISTRY["risk.get_result"] = saved


def margin(margins, terms, amount="9500000", direction="POST"):
    rid = uid("MR")
    margins[rid] = {"result_id": rid, "status": "OK", "market_type": "OTC", "currency": "USD", "netting_set_id": NS,
                    "direction": direction, "required_amount": {"amount": amount, "currency": "USD"}, "rounded_amount": amount,
                    "result_hash": uid("H").ljust(64, "0")[:64], "terms_hash": terms["terms_hash"], "rule_set_hash": None,
                    "blocking_reasons": [], "hypothetical": False}
    return rid


@pytest.mark.gold
def test_real_covenant_terms_optimise_g03_respects_contract_caps_and_wrong_way(db, real_terms_env):
    t, terms, margins = real_terms_env
    mid = margin(margins, terms)
    out = S.optimise(db, t, mid, "INV-OTC-001", None, terms["terms_hash"], DEADLINE, 30, actor_id="u", actor_type="USER")
    db.commit()
    assert out["state"] == "CHECKED" and out["solver_status"] == "optimal", out["result"].get("solver_message")
    assert out["result"]["verification"]["ok"]
    assert any(w.startswith("VOCABULARY_NORMALISED: contract asset_class GOVERNMENT_BOND -> inventory class GSEC") for w in out["result"]["warnings"])
    assert any("TOTAL_EFFECTIVE_ALLOCATED -> TOTAL_ALLOCATED" in w for w in out["result"]["warnings"]), out["result"]["warnings"]
    legs = out["legs"]
    assert "CORP-USD-WW" not in [l["lot_id"] for l in legs]                     # wrong-way issuer group excluded
    cov = {c: D(0) for c in ("CASH", "GSEC", "CORP")}
    for l in legs:
        cov[l["asset_class"]] += D(l["effective_coverage"])
    required = D("9500000")
    assert sum(cov.values()) >= required                                         # independent coverage re-check
    # CSA caps (denominator OBLIGATION): government bonds <= 60 % of the call, corporates <= 20 % -- re-derived here from the legs
    assert cov["GSEC"] <= required * D("0.60") + D("0.000001"), cov
    assert cov["CORP"] <= required * D("0.20") + D("0.000001"), cov


def test_real_covenant_schedule_every_entry_matches_inventory_classes(db, real_terms_env):
    t, terms, _ = real_terms_env
    sched, notes = S._normalise_contract_schedule(terms["terms"]["eligible_schedule"])
    assert {e["asset_class"] for e in sched} == {"CASH", "GSEC", "CORP"}
    assert notes and all(n.startswith("VOCABULARY_NORMALISED") for n in notes)
    lots = {l["lot_id"]: l for l in otc()["lots"]}

    class L:  # minimal lot view for match_entry
        def __init__(self, d):
            self.asset_class, self.currency, self.issuer_class = d["asset_class"], d["currency"], d.get("issuer_class")

    assert match_entry(sched, L(lots["CASH-USD-OTC"])) is not None             # issuer_class ANY is a wildcard
    assert match_entry(sched, L(lots["GSEC-USD-OTC"])) is not None             # SOVEREIGN lot matches SOVEREIGN entry
    assert match_entry(sched, L(dict(lots["GSEC-USD-OTC"], issuer_class=None))) is None  # unknown issuer class never matches a specific one


def test_unknown_vocabulary_is_not_silently_coerced():
    sched, notes = S._normalise_contract_schedule([{"asset_class": "STRUCTURED_NOTE", "cap_denominator": "SOMETHING_ELSE"}])
    assert sched[0]["asset_class"] == "STRUCTURED_NOTE" and sched[0]["cap_denominator"] == "SOMETHING_ELSE" and notes == []


def test_unknown_cap_denominator_still_blocks(db, real_terms_env, monkeypatch):
    t, terms, margins = real_terms_env
    mid = margin(margins, terms)
    real = services._REGISTRY["contracts.resolve_terms"]

    def bad(session, tn, ns, as_of, known_at, fact=None):
        r = real(session, tn, ns, as_of, known_at, fact)
        r = dict(r, terms=dict(r["terms"], eligible_schedule=[dict(e, cap_denominator="WEIRD") if e.get("cap_denominator") else e
                                                               for e in r["terms"]["eligible_schedule"]]))
        return r

    monkeypatch.setitem(services._REGISTRY, "contracts.resolve_terms", bad)
    with pytest.raises(Blocked) as e:
        S.optimise(db, t, mid, "INV-OTC-001", None, terms["terms_hash"], DEADLINE, 30, actor_id="u")
    assert e.value.code == "INVALID_ELIGIBILITY_SCHEDULE"
