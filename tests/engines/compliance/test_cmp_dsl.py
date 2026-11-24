from datetime import UTC, datetime

import pytest

from app.engines.compliance import dsl

ASOF = datetime(2026, 8, 17, 4, 0, tzinfo=UTC)
T, F, U = dsl.TRUE, dsl.FALSE, dsl.UNKNOWN


def ev(ast, facts=None):
    return dsl.evaluate_tri(ast, facts or {}, ASOF)[0]


def test_three_valued_truth_tables():
    miss = {"eq": ["venue", "X"]}                   # UNKNOWN: venue absent
    yes, no = {"eq": ["market_type", "A"]}, {"eq": ["market_type", "B"]}
    f = {"market_type": "A"}
    assert ev({"all": [yes, miss]}, f) == U
    assert ev({"all": [no, miss]}, f) == F          # FALSE dominates UNKNOWN in AND
    assert ev({"any": [yes, miss]}, f) == T         # TRUE dominates UNKNOWN in OR
    assert ev({"any": [no, miss]}, f) == U
    assert ev({"not": [miss]}, f) == U              # NOT UNKNOWN is UNKNOWN
    assert ev({"not": [no]}, f) == T


def test_unknown_is_not_pass_for_missing_numbers():
    assert ev({"gte": ["eligible_collateral_inr", "required_margin_inr"]}, {"required_margin_inr": "5"}) == U


def test_comparison_boundaries_and_decimal_strings():
    f = {"position_lots": "100"}
    assert ev({"lte": ["position_lots", 100]}, f) == T
    assert ev({"lte": ["position_lots", 100]}, {"position_lots": "101"}) == F
    assert ev({"lt": ["position_lots", 100]}, f) == F
    assert ev({"gte": ["position_lots", "100.00"]}, f) == T


def test_float_literals_and_facts_rejected():
    with pytest.raises(dsl.DslError):
        dsl.validate({"lte": ["position_lots", 1.5]})
    with pytest.raises(dsl.DslError):
        ev({"lte": ["position_lots", 100]}, {"position_lots": 99.5})


def test_unit_mismatch_is_an_error_not_a_blend():
    with pytest.raises(dsl.DslError) as e:
        ev({"gte": ["eligible_collateral_usd", "required_margin_inr"]}, {"eligible_collateral_usd": "1", "required_margin_inr": "1"})
    assert e.value.code == "UNIT_MISMATCH"


def test_validate_rejects_unknown_ops_fields_and_typos():
    for bad in ({"eval": ["x"]}, {"gte": ["not_a_field", 1]}, {"all": []}, {"lte": [5, "position_lots"]}, {"a": 1, "b": 2}, "string"):
        with pytest.raises(dsl.DslError):
            dsl.validate(bad)


def test_bounds_depth_nodes_rows():
    deep = {"eq": ["market_type", "A"]}
    for _ in range(dsl.MAX_DEPTH + 2):
        deep = {"not": [deep]}
    with pytest.raises(dsl.DslError) as e:
        dsl.validate(deep)
    assert e.value.code == "TOO_DEEP"
    wide = {"all": [{"eq": ["market_type", "A"]}] * (dsl.MAX_NODES + 5)}
    with pytest.raises(dsl.DslError) as e2:
        dsl.validate(wide)
    assert e2.value.code == "TOO_LARGE"
    rows = [{"q": "1"}] * (dsl.MAX_ROWS + 1)
    with pytest.raises(dsl.DslError) as e3:
        ev({"lte": [{"sum_by": ["positions", "q"]}, 5]}, {"positions": rows})
    assert e3.value.code == "TOO_MANY_ROWS"


def test_effective_on_boundaries():
    ast = {"effective_on": ["2026-08-17T04:00:00Z", "2026-09-01T00:00:00Z"]}
    at = lambda s: dsl.Evaluator({}, datetime.fromisoformat(s)).eval(ast)  # noqa: E731
    assert at("2026-08-17T03:59:59+00:00") == F
    assert at("2026-08-17T04:00:00+00:00") == T      # inclusive start (boundary)
    assert at("2026-08-31T23:59:59+00:00") == T
    assert at("2026-09-01T00:00:00+00:00") == F      # exclusive end


def test_sum_by_ratio_abs_between_in_exists():
    rows = [{"k": "A", "q": "40"}, {"k": "B", "q": "70"}, {"k": "A", "q": "10"}]
    assert ev({"eq": [{"sum_by": ["positions", "q", "k", "A"]}, 50]}, {"positions": rows}) == T
    assert ev({"eq": [{"sum_by": ["positions", "q"]}, 120]}, {"positions": rows}) == T
    assert ev({"eq": [{"sum_by": ["positions", "q"]}, 0]}, {"positions": [{"q": None}]}) == U   # missing => UNKNOWN, not zero
    assert ev({"lte": [{"ratio": ["required_margin_inr", "eligible_collateral_inr"]}, 1]},
              {"required_margin_inr": "5", "eligible_collateral_inr": "10"}) == T
    assert ev({"lte": [{"ratio": ["required_margin_inr", "eligible_collateral_inr"]}, 1]},
              {"required_margin_inr": "5", "eligible_collateral_inr": "0"}) == U               # division by zero => UNKNOWN
    assert ev({"gt": [{"abs": ["position_lots"]}, 5]}, {"position_lots": "-6"}) == T
    assert ev({"between": ["position_lots", 1, 10]}, {"position_lots": "10"}) == T
    assert ev({"between": ["position_lots", 1, 10]}, {"position_lots": "11"}) == F
    assert ev({"in": ["venue", ["A", "B"]]}, {"venue": "B"}) == T
    assert ev({"exists": ["venue"]}, {}) == F
