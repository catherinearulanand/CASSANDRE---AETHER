import pytest

from app.engines.regulatory import dsl

S = {"eq": ["venue", "X"]}
BAD = [
    ({"all": []}, "non-empty"), ({"any": "x"}, "non-empty"), ({"not": 5}, "exactly one operator"), ({"eq": ["venue"]}, "two operands"),
    ({"in": ["venue", []]}, "non-empty list"), ({"in": ["venue", [1]]}, "strings"), ({"in": ["required_margin_inr", ["a"]]}, "string operands"),
    ({"exists": "nope"}, "registry field"), ({"exists": {"field": "venue"}}, None), ({"between": ["required_margin_inr", {"const": "1", "unit": "INR"}]}, "[x, lo, hi]"),
    ({"effective_on": ["venue", "2026-01-01", None]}, "date field"), ({"effective_on": ["as_of_date", "not-a-date", None]}, "invalid ISO"),
    ({"effective_on": "x"}, "needs"), ({"eq": [{"field": "nope"}, "x"]}, "unknown field"), ({"eq": ["venue", {"const": True}]}, None),
    ({"eq": ["required_margin_inr", {"const": 1.5, "unit": "INR"}]}, "floating"), ({"eq": ["venue", {"const": [1]}]}, "unsupported constant"),
    ({"eq": ["venue", {"abs": "venue"}]}, "decimal operand"), ({"eq": ["venue", {"ratio": ["venue"]}]}, "[numerator"),
    ({"gt": [{"ratio": ["required_margin_inr", "required_margin_usd"]}, {"const": "1", "unit": "RATIO"}]}, "identical units"),
    ({"eq": ["venue", {"sum_by": {"rows": "nope", "value": "lots"}}]}, "must be one of"),
    ({"eq": ["venue", {"sum_by": {"rows": "positions", "value": "account_id"}}]}, "decimal row field"),
    ({"eq": ["venue", {"zzz": 1}]}, "unsupported value operator"), ({"eq": ["venue", {"a": 1, "b": 2, "c": 3}]}, "single-operator"),
    ({"eq": ["positions", "x"]}, "only usable inside sum_by"), ({"eq": ["as_of_date", {"const": "1", "unit": "INR"}]}, "date compares"),
    ({"eq": ["venue", True]}, "type mismatch"), ({"gt": ["as_of_date", "2026-01-01"]}, None),
]


@pytest.mark.parametrize("cond,frag", BAD)
def test_compile_negative_and_edge(cond, frag):
    rep = dsl.compile_rule(S, cond, {})
    if frag is None:
        return  # accepted forms are exercised for no-crash only
    assert not rep["ok"] and any(frag in e["message"] for e in rep["errors"]), rep["errors"]


def test_params_value_validation_and_serialisation():
    p, errs = dsl.normalise_params({"a": {"value": "1.50", "unit": "RATIO"}, "b": "bad", "c": {"value": "NaN", "unit": "RATIO"}})
    assert set(p) == {"a"} and len(errs) == 2
    assert dsl.params_to_json(p) == {"a": {"value": "1.5", "unit": "RATIO"}}
    assert dsl.params_to_json({"z": {"value": __import__("decimal").Decimal(0), "unit": "COUNT"}}) == {"z": {"value": "0", "unit": "COUNT"}}


def test_evaluation_branches_in_date_param_not_and_invalid_inputs():
    ev = lambda n, c, p=None: dsl.evaluate(n, c, p)[0]  # noqa: E731
    assert ev({"in": ["venue", ["A", "B"]]}, {"venue": "B"}) == "TRUE" and ev({"in": ["venue", ["A"]]}, {}) == "UNKNOWN"
    assert ev({"gte": ["as_of_date", "2026-06-01"]}, {"as_of_date": "2026-06-01"}) == "TRUE"
    assert ev({"gte": ["as_of_date", "2026-06-01"]}, {"as_of_date": "garbage"}) == "UNKNOWN"
    assert ev({"gt": ["required_margin_inr", {"const": "5", "unit": "INR"}]}, {"required_margin_inr": "not-a-number"}) == "UNKNOWN"
    assert ev({"lt": ["required_margin_inr", {"param": "p"}]}, {"required_margin_inr": "3"}, {"p": {"value": "4", "unit": "INR"}}) == "TRUE"
    assert ev({"not": {"lt": ["required_margin_inr", {"const": "1", "unit": "INR"}]}}, {"required_margin_inr": "3"}) == "TRUE"
    assert ev({"between": ["required_margin_inr", {"const": "1", "unit": "INR"}, {"const": "2", "unit": "INR"}]}, {}) == "UNKNOWN"
    assert ev({"effective_on": ["as_of_date", "2026-06-01", "2026-07-01"]}, {"as_of_date": "2026-07-01"}) == "FALSE"
    assert ev({"effective_on": ["as_of_date", "2026-06-01", None]}, {}) == "UNKNOWN"
    assert ev({"any": [{"eq": ["venue", "A"]}, {"eq": ["jurisdiction", "B"]}]}, {"venue": "A"}) == "TRUE"
    assert ev({"eq": ["venue", "A"]}, {"venue": "A"}) == "TRUE" and ev({"eq": ["venue", "A"]}, {"venue": "B"}) == "FALSE"
    # sum_by: a row with a missing or invalid value yields UNKNOWN, never a partial sum
    node = {"lte": [{"sum_by": {"rows": "positions", "value": "lots"}}, {"const": "10", "unit": "LOTS"}]}
    assert ev(node, {"positions": [{"lots": "1"}, {}]}) == "UNKNOWN" and ev(node, {"positions": [{"lots": "x"}]}) == "UNKNOWN"
    assert ev(node, {"positions": [{"lots": "4"}, {"lots": "5"}]}) == "TRUE"


def test_node_dependencies_and_vector_runner():
    assert dsl.node_dependencies({"gte": ["eligible_collateral_inr", "required_margin_inr"]}) == ["eligible_collateral_inr", "required_margin_inr"]
    out = dsl.run_test_vectors(S, {"exists": "venue"}, {}, [{"context": {"venue": "X"}}, {"context": {"venue": "Y"}, "expect_scope": "FALSE", "expect_condition": "TRUE"}])
    assert [o["passed"] for o in out] == [True, True]


def test_scope_and_condition_are_compiled_separately_with_paths():
    rep = dsl.compile_rule({"eq": ["nope", "x"]}, {"eq": ["nope2", "x"]}, {})
    assert {e["path"].split(".")[0] for e in rep["errors"]} == {"scope", "condition"}
