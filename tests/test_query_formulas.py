"""真实 PostgreSQL：先汇总再公式、舍入、后筛、排序分页，同一执行器覆盖批量。"""

from decimal import Decimal

import pytest

from data_service import query
from data_service.datasets import register_dataset
from data_service.errors import ServiceError
from data_service.models import BatchQueryItem
from test_datasets import definition
from test_query import day as day, settings as settings, publish, request


@pytest.fixture
def formula_dataset(settings, day):
    schema = definition(fields=[
        {"name": "dt", "type": "date", "required": True},
        {"name": "id", "type": "string", "required": True},
        {"name": "shop", "type": "string", "dimension": True},
        {"name": "paid", "type": "decimal"},
        {"name": "refund", "type": "decimal"},
        {"name": "units", "type": "integer"},
    ])
    register_dataset(settings, schema)
    publish(settings, schema, day, [
        {"id": "1", "shop": "A", "paid": "100", "refund": "50", "units": 1},
        {"id": "2", "shop": "A", "paid": "900", "refund": "0", "units": 2},
        {"id": "3", "shop": "B", "paid": "200", "refund": "20", "units": 2},
        {"id": "4", "shop": "C", "paid": "0", "refund": "10", "units": 0},
        {"id": "5", "shop": "D", "paid": None, "refund": "10", "units": 1},
        {"id": "6", "shop": "E", "paid": "20", "refund": None, "units": 1},
        {"id": "7", "shop": "F", "paid": "200", "refund": "20", "units": 2},
    ])
    return schema


def ratio_request(day, **updates):
    base = {
        "group_by": ["shop"],
        "aggregates": [
            {"field": "paid", "op": "sum", "alias": "paid_total"},
            {"field": "refund", "op": "sum", "alias": "refund_total"},
            {"op": "count", "alias": "rows_count"},
            {"field": "units", "op": "sum", "alias": "units_total"},
        ],
        "formulas": [{"alias": "refund_rate", "expression": "refund_total / paid_total"}],
    }
    return request(day, **{**base, **updates})


def run(settings, schema, day, **updates):
    return query.run_query(settings, schema.slug, ratio_request(day, **updates))


def test_ratio_of_sums_null_and_zero(settings, formula_dataset, day):
    result = run(settings, formula_dataset, day)
    rows = {row["shop"]: row for row in result["rows"]}
    assert rows["A"]["refund_rate"] == "0.050000"  # 平均逐行比率为0.25，不能作为退款率。
    assert rows["B"]["refund_rate"] == "0.100000"
    assert all(rows[shop]["refund_rate"] is None for shop in ["C", "D", "E"])
    assert result["columns"][-1] == "refund_rate" and result["total"] == 6


def test_final_sort_pagination_ties_and_null_last(settings, formula_dataset, day):
    result = run(settings, formula_dataset, day, order_by=[{"field": "refund_rate", "direction": "desc"}], limit=1, offset=1)
    assert result["total"] == 6 and result["rows"][0]["shop"] == "F"
    result = run(settings, formula_dataset, day, order_by=[{"field": "refund_rate", "direction": "asc"}])
    assert [row["shop"] for row in result["rows"]] == ["A", "B", "F", "C", "D", "E"]


def test_after_filter_total_before_paging(settings, formula_dataset, day):
    result = run(settings, formula_dataset, day, result_filters=[{"field": "refund_rate", "op": "gte", "value": "0.1"}], order_by=[{"field": "refund_rate", "direction": "desc"}], limit=1, offset=1)
    assert result["total"] == 2 and [row["shop"] for row in result["rows"]] == ["F"]
    result = run(settings, formula_dataset, day, result_filters=[{"field": "paid_total", "op": "gt", "value": "900"}])
    assert result["total"] == 1 and result["rows"][0]["shop"] == "A"
    result = run(settings, formula_dataset, day, result_filters=[{"field": "paid_total", "op": "gt", "value": "9000"}])
    assert result["total"] == 0 and result["rows"] == []


@pytest.mark.parametrize("predicate,expected", [
    ({"field": "refund_rate", "op": "is_null", "value": True}, ["C", "D", "E"]),
    ({"field": "refund_rate", "op": "is_null", "value": False}, ["A", "B", "F"]),
    ({"field": "refund_rate", "op": "in", "value": ["0.05", "0.1"]}, ["A", "B", "F"]),
    ({"field": "refund_rate", "op": "lt", "value": "0.1"}, ["A"]),
    ({"field": "refund_rate", "op": "lte", "value": "0.1"}, ["A", "B", "F"]),
    ({"field": "refund_rate", "op": "eq", "value": "0.05"}, ["A"]),
])
def test_result_filter_operators(settings, formula_dataset, day, predicate, expected):
    result = run(settings, formula_dataset, day, result_filters=[predicate])
    assert [row["shop"] for row in result["rows"]] == expected


def test_integer_division_complex_arithmetic_and_final_rounding(settings, formula_dataset, day):
    result = run(settings, formula_dataset, day, filters=[{"field": "shop", "op": "eq", "value": "A"}], formulas=[
        {"alias": "average_units", "expression": "units_total / rows_count", "decimal_places": 2},
        {"alias": "percent", "expression": "(refund_total / paid_total) * 100", "decimal_places": 0},
        {"alias": "negative", "expression": "-(units_total + 0.005) / +2", "decimal_places": 3},
        {"alias": "empty", "expression": "1 / (units_total - units_total) + 10"},
        {"alias": "third", "expression": "1/3", "decimal_places": 12},
    ])
    row = result["rows"][0]
    assert row["average_units"] == "1.50" and row["percent"] == "5"
    assert row["negative"] == "-1.503" and row["empty"] is None and row["third"] == "0.333333333333"


def test_filter_uses_returned_rounded_value(settings, formula_dataset, day):
    result = run(settings, formula_dataset, day, formulas=[{"alias": "third", "expression": "1/3", "decimal_places": 2}], result_filters=[{"field": "third", "op": "eq", "value": "0.33"}])
    assert result["total"] == 6 and all(row["third"] == "0.33" for row in result["rows"])


def test_decimal_literal_is_not_binary_float_and_large_division(settings, formula_dataset, day):
    result = run(settings, formula_dataset, day, formulas=[
        {"alias": "constant", "expression": "9007199254740993.123456789012", "decimal_places": 12},
        {"alias": "big_ratio", "expression": "100000000000000000000 / 3", "decimal_places": 12},
    ])
    assert result["rows"][0]["constant"] == "9007199254740993.123456789012"
    assert result["rows"][0]["big_ratio"] == "33333333333333333333.333333333333"


def test_total_without_groups_batch_and_filter_only(settings, formula_dataset, day):
    payload = ratio_request(day, group_by=[])
    response = query.run_batch(settings, [BatchQueryItem(dataset=formula_dataset.slug, query=payload)] * 2)
    assert response[0] == response[1] and response[0]["total"] == 1
    assert response[0]["rows"][0]["refund_rate"] == "0.077465"  # 110 / 1420。
    result = run(settings, formula_dataset, day, formulas=[], result_filters=[{"field": "rows_count", "op": "eq", "value": "2"}])
    assert result["total"] == 1 and result["rows"][0]["shop"] == "A"


def test_empty_global_aggregate_preserves_null(settings, formula_dataset, day):
    result = run(settings, formula_dataset, day, group_by=[], filters=[{"field": "shop", "op": "eq", "value": "missing"}])
    assert result["total"] == 1 and result["rows"][0]["refund_rate"] is None


@pytest.mark.parametrize("changes", [
    {"formulas": [{"alias": "rate", "expression": "paid / refund"}]},
    {"formulas": [{"alias": "rate", "expression": "shop / rows_count"}]},
    {"formulas": [{"alias": "rate", "expression": "rate / rows_count"}]},
    {"formulas": [{"alias": "rate", "expression": "rows_count"}, {"alias": "half", "expression": "rate / 2"}]},
    {"result_filters": [{"field": "shop", "op": "is_null", "value": False}]},
    {"result_filters": [{"field": "paid", "op": "eq", "value": "0"}]},
    {"result_filters": [{"field": "refund_rate", "op": "gte", "value": 0.1}]},
    {"result_filters": [{"field": "refund_rate", "op": "gte", "value": True}]},
    {"result_filters": [{"field": "refund_rate", "op": "in", "value": ["0.1", "NaN"]}]},
    {"aggregates": [{"field": "shop", "op": "min", "alias": "first_shop"}], "formulas": [{"alias": "rate", "expression": "first_shop/2"}]},
])
def test_semantic_rejections(settings, formula_dataset, day, changes):
    with pytest.raises(ServiceError) as caught:
        run(settings, formula_dataset, day, **changes)
    assert caught.value.status == 422 and caught.value.code == "invalid_query"


def test_count_distinct_and_numeric_min_max_are_formula_inputs(settings, formula_dataset, day):
    result = run(settings, formula_dataset, day, aggregates=[
        {"field": "id", "op": "count_distinct", "alias": "unique_rows"},
        {"field": "units", "op": "min", "alias": "minimum"},
        {"field": "units", "op": "max", "alias": "maximum"},
        {"field": "paid", "op": "avg", "alias": "average"},
    ], formulas=[{"alias": "metric", "expression": "(maximum-minimum)/unique_rows + average"}])
    assert Decimal(result["rows"][0]["metric"]) == Decimal("500.5")


@pytest.mark.parametrize("expression,places,expected", [
    ("(1/3)*1000000000000000000000000000000000000000", 6, "3" * 39 + ".333333"),
    ("(1/3)*3-1", 12, "0.000000000000"),
    ("0.1234564999999", 6, "0.123456"),
    ("2469129999998/20000000000000", 6, "0.123456"),
    ("1/-8", 2, "-0.13"),
    ("-1/-8", 2, "0.13"),
    ("0*(1/0)", 6, None),
    ("1/(1/0)", 6, None),
    ("(1/0)*0", 6, None),
    ("(1/0)/(1/0)", 6, None),
    ("1/((1/3)*3-1)", 6, None),
])
def test_exact_fraction_final_rounding_and_null_propagation(settings, formula_dataset, day, expression, places, expected):
    result = run(settings, formula_dataset, day, formulas=[{"alias": "exact", "expression": expression, "decimal_places": places}])
    assert all(row["exact"] == expected for row in result["rows"])


def test_exact_arithmetic_matches_independent_fraction_oracle(settings, formula_dataset, day):
    """仅测试使用Fraction作独立判定；被测路径仍由真实PG执行完整聚合查询。"""
    from fractions import Fraction
    import random

    randomizer = random.Random(20261023)

    def expression(depth):
        if depth == 0:
            text = randomizer.choice(["0", "1", "2", "3", "7", "0.125", "-1.5"])
            return text, Fraction(text)
        left_text, left = expression(depth - 1)
        right_text, right = expression(depth - 1)
        operator = randomizer.choice(["+", "-", "*", "/"])
        if left is None or right is None or (operator == "/" and right == 0):
            value = None
        elif operator == "+":
            value = left + right
        elif operator == "-":
            value = left - right
        elif operator == "*":
            value = left * right
        else:
            value = left / right
        return f"({left_text}{operator}{right_text})", value

    for _ in range(24):
        text, expected = expression(3)
        result = run(settings, formula_dataset, day, group_by=[], formulas=[{"alias": "exact", "expression": text, "decimal_places": 6}])
        actual = result["rows"][0]["exact"]
        if expected is None:
            assert actual is None, text
        else:
            whole, remainder = divmod(abs(expected.numerator) * 10**6, expected.denominator)
            units = whole + (2 * remainder >= expected.denominator)
            if expected < 0:
                units = -units
            assert Decimal(actual) == Decimal(units).scaleb(-6), text
