import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.engines.regulatory import dsl
from tests.engines.regulatory.helpers import EXPECTED

S = {"eq": ["market_type", "OTC"]}  # TRUE / FALSE / UNKNOWN driven by the context below
T, F, U = {"market_type": "OTC"}, {"market_type": "ETD_DOMESTIC"}, {}


def ev(node, ctx, params=None):
    return dsl.evaluate(node, ctx, params)[0]


def leaf(name, val):  # leaf with deterministic tri-value
    return {"eq": [name, val]}


def ctx_for(a, b):
    c = {}
    for name, v in (("venue", a), ("jurisdiction", b)):
        if v == "T":
            c[name] = "X"
        elif v == "F":
            c[name] = "Y"
    return c


@pytest.mark.parametrize("op", ["all", "any"])
def test_gold_three_valued_tables(op):
    m = {"TRUE": "T", "FALSE": "F", "UNKNOWN": "U"}
    for a, b, expect in EXPECTED["three_valued_tables"][op]:
        node = {op: [leaf("venue", "X"), leaf("jurisdiction", "X")]}
        ctx = ctx_for(m[a], m[b])
        # jurisdiction 'F' means value differs; independent oracle is the table, not the evaluator
        assert ev(node, ctx) == expect, (op, a, b)


def test_gold_not_table():
    m = {"TRUE": "T", "FALSE": "F", "UNKNOWN": "U"}
    for a, expect in EXPECTED["three_valued_tables"]["not"]:
        assert ev({"not": leaf("venue", "X")}, ctx_for(m[a], "T")) == expect


@settings(max_examples=60, deadline=None)
@given(st.sampled_from("TFU"), st.sampled_from("TFU"))
def test_property_de_morgan_holds_in_three_valued_logic(a, b):
    ctx = ctx_for(a, b)
    A, B = leaf("venue", "X"), leaf("jurisdiction", "X")
    assert ev({"not": {"all": [A, B]}}, ctx) == ev({"any": [{"not": A}, {"not": B}]}, ctx)
    assert ev({"not": {"any": [A, B]}}, ctx) == ev({"all": [{"not": A}, {"not": B}]}, ctx)


def test_unknown_is_never_true_and_missing_context_is_unknown():
    assert ev({"gte": ["eligible_collateral_inr", "required_margin_inr"]}, {"required_margin_inr": "5"}) == "UNKNOWN"
    assert ev({"eq": ["venue", "X"]}, {}) == "UNKNOWN"
    assert ev({"exists": "venue"}, {}) == "FALSE"  # presence test is definitive


def test_g11_limit_boundary_and_unknown_classification():
    node = {"all": [{"in": ["client_classification", ["CLIENT", "PROPRIETARY"]]}, {"lte": ["net_position_lots", {"param": "limit"}]}]}
    params = {"limit": {"value": "100", "unit": "LOTS"}}
    assert dsl.compile_rule({"eq": ["venue", "X"]}, node, params)["ok"]
    for c in EXPECTED["G11"]["cases"]:
        ctx = {"net_position_lots": c["lots"]} | ({"client_classification": c["classification"]} if c["classification"] else {})
        assert ev(node, ctx, params) == c["condition"], c


def test_between_ratio_abs_sum_by_effective_on():
    assert ev({"between": ["required_margin_inr", {"const": "1", "unit": "INR"}, {"const": "10", "unit": "INR"}]}, {"required_margin_inr": "10"}) == "TRUE"
    assert ev({"gte": [{"ratio": ["eligible_collateral_inr", "required_margin_inr"]}, {"const": "1", "unit": "RATIO"}]},
              {"eligible_collateral_inr": "115", "required_margin_inr": "100"}) == "TRUE"
    assert ev({"gt": [{"ratio": ["eligible_collateral_inr", "required_margin_inr"]}, {"const": "0", "unit": "RATIO"}]},
              {"eligible_collateral_inr": "1", "required_margin_inr": "0"}) == "UNKNOWN"  # division by zero never yields a verdict
    assert ev({"lt": [{"abs": "net_position_lots"}, {"const": "6", "unit": "LOTS"}]}, {"net_position_lots": "-5"}) == "TRUE"
    node = {"lte": [{"sum_by": {"rows": "positions", "value": "lots", "group": "account_id", "key": "A"}}, {"const": "10", "unit": "LOTS"}]}
    rows = [{"lots": "4", "account_id": "A"}, {"lots": "6", "account_id": "A"}, {"lots": "99", "account_id": "B"}]
    assert ev(node, {"positions": rows}) == "TRUE"
    assert ev(node, {"positions": rows + [{"lots": "1", "account_id": "A"}]}) == "FALSE"
    assert ev(node, {}) == "UNKNOWN"
    eff = {"effective_on": ["as_of_date", "2026-06-01", None]}
    assert ev(eff, {"as_of_date": "2026-05-31"}) == "FALSE" and ev(eff, {"as_of_date": "2026-06-01"}) == "TRUE"


@pytest.mark.parametrize("cond,frag", [
    ({"eq": ["required_margin_inr", 100]}, "explicit unit"),
    ({"eq": ["required_margin_inr", 1.5]}, "floating"),
    ({"eq": ["required_margin_inr", {"const": "100", "unit": "USD"}]}, "unit mismatch"),
    ({"eq": ["required_margin_inr", {"const": "100", "unit": "BANANAS"}]}, "invalid unit"),
    ({"eq": ["no_such_field", "x"]}, None),
    ({"gt": ["venue", "A"]}, "not supported"),
    ({"eval": "__import__('os').system('echo pwned')"}, "unsupported operator"),
    ({"gte": ["required_margin_inr", "required_margin_usd"]}, "unit mismatch"),
    ({"gte": ["required_margin_inr", {"param": "nope"}]}, "not defined"),
])
def test_compile_rejects_invalid(cond, frag):
    rep = dsl.compile_rule({"eq": ["venue", "X"]}, cond, {})
    assert not rep["ok"]
    if frag:
        assert any(frag in e["message"] for e in rep["errors"]), rep["errors"]


def test_g29_threshold_missing_or_invalid_unit_cannot_compile():
    cond = {"lte": ["net_position_lots", {"param": "limit"}]}
    for params in ({"limit": {"value": "100"}}, {"limit": {"value": "100", "unit": "XX"}}, {"limit": {"unit": "LOTS"}}, {"limit": {"value": "abc", "unit": "LOTS"}}, {}):
        assert not dsl.compile_rule({"eq": ["venue", "X"]}, cond, params)["ok"], params
    assert dsl.compile_rule({"eq": ["venue", "X"]}, cond, {"limit": {"value": "100", "unit": "LOTS"}})["ok"]


def test_depth_node_count_row_and_step_limits():
    deep = {"eq": ["venue", "X"]}
    for _ in range(12):
        deep = {"not": deep}
    assert not dsl.compile_rule({"eq": ["venue", "X"]}, deep, {})["ok"]
    wide = {"all": [{"eq": ["venue", "X"]}] * 250}
    assert not dsl.compile_rule({"eq": ["venue", "X"]}, wide, {})["ok"]
    big = {"lte": [{"sum_by": {"rows": "positions", "value": "lots"}}, {"const": "1", "unit": "LOTS"}]}
    assert ev(big, {"positions": [{"lots": "1"}] * (dsl.MAX_ROWS + 1)}) == "UNKNOWN"  # bounded: no verdict beyond the budget


def test_param_inlining_for_external_evaluators():
    out = dsl.inline_params({"lte": ["net_position_lots", {"param": "limit"}]}, {"limit": {"value": "100", "unit": "LOTS"}})
    assert out == {"lte": ["net_position_lots", {"const": "100", "unit": "LOTS"}]}
