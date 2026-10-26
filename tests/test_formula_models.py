"""公式模型及安全边界：无需数据库的明确拒绝与旧配置兼容测试。"""

from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from data_service.formulas import compile_formula, result_number
from data_service.models import Formula, QueryRequest
from data_service.query_services import QueryTemplate, ServiceQuery


def request(**updates):
    return QueryRequest(start_date=date(2026, 10, 21), end_date=date(2026, 10, 21), **updates)


@pytest.mark.parametrize("expression", [
    "", " ", "x ** 2", "x // 2", "x % 2", "sum(x)", "x.real", "x[0]", "__import__('os')",
    "x;DROP TABLE datasets", "x OR 1", "x > 1", "x if x else 0", "[x]", "{x: 1}", "(x, 1)",
    "True", "None", "1e309", "0x10", "1_000", "1j", "'x'", "x # ignore", "x\n", "x\t",
    "退款/支付", "9" * 41, "x" * 501, "-" * 20 + "x", "(" * 230 + "x" + ")" * 230,
])
def test_forbidden_expression(expression):
    with pytest.raises(ValidationError):
        Formula(alias="rate", expression=expression)


@pytest.mark.parametrize("places", [-1, 13, True, 1.0, "6"])
def test_decimal_places_strict(places):
    with pytest.raises(ValidationError):
        Formula(alias="rate", expression="x", decimal_places=places)


def test_exact_decimal_constants_and_numeric_references():
    compiled, constants = compile_formula("-paid / +count + 9007199254740993.123456789012", {"paid", "count"})
    assert Decimal("9007199254740993.123456789012") in constants
    assert "NULLIF" in compiled.as_string()
    assert '"aggregated"."paid"' in compiled.as_string()
    assert "9007199254740993" not in compiled.as_string()


@pytest.mark.parametrize("name", ["original_amount", "shop", "another_formula"])
def test_non_aggregate_reference_is_rejected(name):
    with pytest.raises(ValueError):
        compile_formula(name + " / paid", {"paid"})


@pytest.mark.parametrize("updates", [
    {"formulas": [{"alias": "rate", "expression": "1"}]},
    {"group_by": ["shop"], "formulas": [{"alias": "rate", "expression": "1"}]},
    {"result_filters": [{"field": "rate", "op": "gt", "value": "0"}]},
    {"fields": ["id"], "aggregates": [{"op": "count", "alias": "n"}], "formulas": [{"alias": "rate", "expression": "n"}]},
    {"group_by": ["shop"], "aggregates": [{"op": "count", "alias": "n"}], "formulas": [{"alias": "shop", "expression": "n"}]},
    {"aggregates": [{"op": "count", "alias": "n"}], "formulas": [{"alias": "n", "expression": "1"}]},
    {"aggregates": [{"op": "count", "alias": "n"}], "formulas": [{"alias": "rate", "expression": "n"}] * 2},
    {"aggregates": [{"op": "count", "alias": "n"}], "formulas": [{"alias": f"r{i}", "expression": "n"} for i in range(11)]},
    {"aggregates": [{"op": "count", "alias": "n"}], "result_filters": [{"field": "n", "op": "gt", "value": "0"}] * 21},
])
def test_query_shape_boundaries(updates):
    with pytest.raises(ValidationError):
        request(**updates)


def test_old_templates_and_query_are_compatible():
    old = QueryTemplate(aggregates=[{"op": "count", "alias": "n"}])
    assert old.formulas == [] and old.result_filters == []
    assert request().formulas == [] and request().result_filters == []
    new = QueryTemplate(aggregates=[{"op": "count", "alias": "n"}], formulas=[{"alias": "half", "expression": "n/2"}])
    assert new.request(date(2026, 10, 21), date(2026, 10, 21)).formulas[0].decimal_places == 6


@pytest.mark.parametrize("key", ["formulas", "result_filters", "order_by", "aggregates", "group_by"])
def test_public_service_cannot_override_saved_rules(key):
    with pytest.raises(ValidationError):
        ServiceQuery(start_date="2026-10-21", end_date="2026-10-21", **{key: []})


@pytest.mark.parametrize("value", [0.1, True, None, [], {}, "NaN", "Infinity", "1e2", "1 OR 1=1", "9" * 101])
def test_result_filter_rejects_imprecise_or_hostile_number(value):
    with pytest.raises(ValueError):
        result_number(value)


def test_result_filter_keeps_aggregate_precision():
    text = "199999999999999999999999999999999.999998000001"
    assert result_number(text) == Decimal(text)
    assert result_number(0) == Decimal(0)


def test_complex_formula_parameter_positions_are_bounded():
    expression = "number"
    for _ in range(4):
        expression = f"({expression}+{expression})/number"
    compiled, parameters = compile_formula(expression, {"number"}, 12)
    rendered = compiled.as_string()
    assert rendered.count("%s") == len(parameters)
    assert len(rendered.encode("utf-8")) <= 65536 and len(parameters) <= 512
