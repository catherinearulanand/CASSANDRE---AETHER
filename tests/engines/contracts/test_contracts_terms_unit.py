"""Pure-logic tests: validation (C-A09), units, predicates (three-valued), effective-dated resolution (C-A06, G08)."""
from datetime import UTC, datetime

import pytest

from app.engines.contracts import terms as T
from app.engines.contracts.extract import parse_money

USD = lambda a: {"amount": a, "currency": "USD"}  # noqa: E731


def row(**kw):
    base = {"asset_class": "GOVERNMENT_BOND", "currency": "USD", "issuer_class": "SOVEREIGN", "valuation_pct": "94",
            "haircut_pct": "6", "fx_haircut_pct": "0", "concentration_cap_pct": None, "cap_denominator": None,
            "min_rating": None, "max_maturity_years": None}
    base.update(kw)
    return base


# ------------------------------------------------------------------ validation
def test_zero_threshold_is_valid_but_negative_is_not():
    assert T.validate_value("threshold_a", USD("0"), "USD")[0] == []
    assert T.validate_value("threshold_a", USD("-1"), "USD")[0]


def test_mta_zero_allowed_negative_rejected():
    assert T.validate_value("mta_a", USD("0"), "USD")[0] == []
    assert any("negative" in e for e in T.validate_value("mta_b", USD("-0.01"), "USD")[0])


def test_rounding_increment_must_be_positive():
    assert T.validate_value("rounding_increment", USD("0"), "USD")[0]
    assert T.validate_value("rounding_increment", USD("100000"), "USD")[0] == []


def test_currency_must_equal_base_no_silent_conversion():
    errs, _ = T.validate_value("threshold_a", {"amount": "1", "currency": "INR"}, "USD")
    assert errs and "base currency" in errs[0]


def test_float_amount_rejected():
    assert T.validate_value("threshold_a", {"amount": 10.5, "currency": "USD"}, "USD")[0]


def test_nan_and_garbage_rejected():
    for bad in ("NaN", "Infinity", "ten"):
        assert T.validate_value("threshold_a", USD(bad), "USD")[0]


def test_enum_and_bool_validation():
    assert T.validate_value("rounding_mode", "SIDEWAYS", "USD")[0]
    assert T.validate_value("mta_semantics", "GTE", "USD")[0] == []
    assert T.validate_value("segregated_im", "yes", "USD")[0]


def test_schedule_valuation_and_haircut_must_be_one_discount():
    ok_errs, rows = T.validate_schedule([row()], "USD")
    assert ok_errs == [] and rows[0]["valuation_pct"] == "94" and rows[0]["haircut_pct"] == "6"
    errs, rows = T.validate_schedule([row(valuation_pct="94", haircut_pct="94")], "USD")
    assert any("VALUATION_HAIRCUT_INCONSISTENT" in e for e in errs) and rows == []


def test_schedule_rejections():  # C-A09
    assert T.validate_schedule([], "USD")[0]
    assert T.validate_schedule([row(currency="XXX")], "USD")[0]
    assert T.validate_schedule([row(valuation_pct="0", haircut_pct="100")], "USD")[0]  # valuation must be > 0
    assert T.validate_schedule([row(valuation_pct="101", haircut_pct="-1")], "USD")[0]
    assert T.validate_schedule([row(concentration_cap_pct="60")], "USD")[0]  # cap without denominator
    assert T.validate_schedule([row(cap_denominator="OBLIGATION")], "USD")[0]  # denominator without cap
    assert T.validate_schedule([row(min_rating="ZZZ")], "USD")[0]
    assert T.validate_schedule([row(max_maturity_years="0")], "USD")[0]
    assert T.validate_schedule([row(), row()], "USD")[0]  # duplicate
    assert T.validate_schedule([row(valuation_pct=94.0)], "USD")[0]  # float


def test_schedule_cap_denominator_semantics_kept():
    errs, rows = T.validate_schedule([row(concentration_cap_pct="50", cap_denominator="TOTAL_EFFECTIVE_ALLOCATED")], "USD")
    assert errs == [] and rows[0]["cap_denominator"] == "TOTAL_EFFECTIVE_ALLOCATED"


# ------------------------------------------------------------------ decimal separators / units
@pytest.mark.parametrize("s,ok", [("USD 10,000,000", "10000000"), ("USD 500,000.50", "500000.50"), ("USD 0", "0"),
                                  ("USD 10000000", "10000000")])
def test_parse_money_ok(s, ok):
    v, err = parse_money(s)
    assert err is None and v["amount"] == ok and v["currency"] == "USD"


@pytest.mark.parametrize("s", ["USD 10.000.000,00", "USD 10.000", "USD 10,5", "USD 1,23", "10,000", "USD 1,2,3", "usd 5"])
def test_parse_money_ambiguous_or_invalid_never_guessed(s):
    v, err = parse_money(s)
    assert v is None and err


# ------------------------------------------------------------------ predicates (three-valued)
def test_rating_scale_ordering_and_unknown():
    p = {"fact": "rating:X", "op": "lt", "value": "BBB-", "scale": "RATING"}
    assert T.eval_predicate(p, {"rating:X": "BB+"}) is True      # worse than BBB-
    assert T.eval_predicate(p, {"rating:X": "BBB-"}) is False    # equal is not below
    assert T.eval_predicate(p, {"rating:X": "A"}) is False
    assert T.eval_predicate(p, {}) is None                       # missing fact: UNKNOWN
    assert T.eval_predicate(p, {"rating:X": "NR"}) is None       # off-scale: UNKNOWN, not PASS


def test_three_valued_logic():
    t, u = {"fact": "a", "op": "eq", "value": 1}, {"fact": "missing", "op": "eq", "value": 1}
    assert T.eval_predicate({"all": [t, u]}, {"a": 1}) is None
    assert T.eval_predicate({"all": [{"fact": "a", "op": "eq", "value": 2}, u]}, {"a": 1}) is False
    assert T.eval_predicate({"any": [t, u]}, {"a": 1}) is True
    assert T.eval_predicate({"not": u}, {}) is None


def test_predicate_validation_bounds():
    assert T.validate_predicate({"fact": "x", "op": "eval", "value": 1})
    assert T.validate_predicate({"fact": "x", "op": "lt", "value": "ZZZ", "scale": "RATING"})
    deep = {"fact": "x", "op": "eq", "value": 1}
    for _ in range(6):
        deep = {"not": deep}
    assert T.validate_predicate(deep)


def test_future_and_unsourced_facts_are_not_usable():
    as_of = datetime(2026, 8, 17, tzinfo=UTC)
    facts = {"r": {"value": "BB", "source_id": "S", "effective_at": "2026-12-31T00:00:00Z"},
             "q": {"value": "A", "source_id": "", "effective_at": "2026-01-01T00:00:00Z"},
             "ok": {"value": "A", "source_id": "S", "effective_at": "2026-01-01T00:00:00Z"}}
    assert T.usable_facts(facts, as_of) == {"ok": "A"}


# ------------------------------------------------------------------ effective-dated resolution
def E(key, val, eff, rank=3, cond=None):
    return {"key": key, "value": val, "effective_from": eff, "rank": rank, "condition": cond}


def test_g08_amendment_boundary_inclusive_and_exact():
    entries = [E("threshold_a", USD("10000000"), "2026-01-01T00:00:00Z"),
               E("threshold_a", USD("8000000"), "2026-09-01T00:00:00Z", rank=5)]
    f = lambda s: T.resolve_entries(entries, T.parse_dt(s), None)[0]["threshold_a"]["amount"]  # noqa: E731
    assert f("2026-08-31T23:59:59Z") == "10000000"
    assert f("2026-09-01T00:00:00Z") == "8000000"
    assert f("2026-09-01T00:00:01Z") == "8000000"


def test_nothing_effective_yet_blocks():
    vals, blocking, _ = T.resolve_entries([E("threshold_a", USD("1"), "2027-01-01T00:00:00Z")], datetime(2026, 1, 1, tzinfo=UTC), None)
    assert vals == {} and blocking[0].startswith("NO_TERMS_EFFECTIVE_AT_AS_OF")


def test_hierarchy_rank_beats_date_for_lower_rank_later_entry():
    entries = [E("mta_a", USD("500000"), "2026-01-01T00:00:00Z", rank=3), E("mta_a", USD("1"), "2026-06-01T00:00:00Z", rank=1)]
    assert T.resolve_entries(entries, datetime(2026, 8, 1, tzinfo=UTC), None)[0]["mta_a"]["amount"] == "500000"


def test_equal_precedence_disagreement_is_conflict_not_silent_pick():
    entries = [E("mta_a", USD("1"), "2026-01-01T00:00:00Z"), E("mta_a", USD("2"), "2026-01-01T00:00:00Z")]
    vals, blocking, _ = T.resolve_entries(entries, datetime(2026, 8, 1, tzinfo=UTC), None)
    assert "mta_a" not in vals and blocking[0].startswith("CONFLICTING_TERMS:mta_a")


def test_conditional_override_true_false_unknown():
    cond = {"fact": "rating:B", "op": "lt", "value": "BBB-", "scale": "RATING"}
    entries = [E("threshold_b", USD("10"), "2026-01-01T00:00:00Z"), E("threshold_b", USD("0"), "2026-01-01T00:00:00Z", cond=cond)]
    as_of = datetime(2026, 8, 1, tzinfo=UTC)
    fx = lambda r: {"rating:B": {"value": r, "source_id": "S", "effective_at": "2026-01-01T00:00:00Z"}}  # noqa: E731
    assert T.resolve_entries(entries, as_of, fx("BB"))[0]["threshold_b"]["amount"] == "0"
    assert T.resolve_entries(entries, as_of, fx("A"))[0]["threshold_b"]["amount"] == "10"
    vals, blocking, need = T.resolve_entries(entries, as_of, None)
    assert "threshold_b" not in vals and need == ["rating:B"] and blocking[0].startswith("UNRESOLVED_CONDITIONAL_FACT")


def test_later_unconditional_amendment_beats_earlier_conditional():
    cond = {"fact": "rating:B", "op": "lt", "value": "BBB-", "scale": "RATING"}
    entries = [E("threshold_b", USD("0"), "2026-01-01T00:00:00Z", cond=cond), E("threshold_b", USD("8"), "2026-06-01T00:00:00Z", rank=5)]
    facts = {"rating:B": {"value": "BB", "source_id": "S", "effective_at": "2026-01-01T00:00:00Z"}}
    assert T.resolve_entries(entries, datetime(2026, 8, 1, tzinfo=UTC), facts)[0]["threshold_b"]["amount"] == "8"


def test_freeze_blocking_lists_missing_required_and_unresolved():
    out = T.freeze_blocking([], [{"field_key": "mta_a", "status": "UNRESOLVED"}], [{"doc_ref": "D", "missing_annexes": ["Annex 2"]}])
    assert any(o.startswith("MISSING_ANNEX") for o in out)
    assert any(o.startswith("UNRESOLVED_FIELDS") for o in out)
    assert "MISSING_REQUIRED_FIELD:mta_a" in out and "MISSING_REQUIRED_FIELD:eligible_schedule" in out
