"""WP2 查询测试设计（先于实现）。

Q01 范围含两端/180天/数据集保留窗/未来日/缺日/完整空日。
Q02 明细字段选择、筛选、NULL、稳定排序与分页；SQL 注入参数。
Q03 多维分组与 sum/avg/min/max/count/count_distinct，Decimal/NULL。
Q04 SUM/AVG 类型限制、分组资格、别名冲突、非法列与操作拒绝。
Q05 多表隔离；只读已发布批次，暂存不可见；替换后读取新版本。
Q06 批量<=10、同一 REPEATABLE READ 只读快照、任一失败整体失败。
Q07 查询总数、空数据、重复维度、原样保留的首尾空格及大金额。
Q08 真实大文本服务器游标逐行读取，含元数据8MiB与批量总16MiB字节上限。
真实 PG 测试只使用 DS_TEST_DATABASE_URL 中名称以 _test 结尾的隔离库。
"""

from datetime import datetime, timedelta
import json
import os
from urllib.parse import urlsplit
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from psycopg import sql
from pydantic import ValidationError

from data_service.config import Settings
from data_service.datasets import get_dataset, register_dataset, validate_record
from data_service.db import connect, migrate
from data_service.errors import ServiceError
from data_service.models import BatchQueryItem, QueryRequest
from data_service import query
from test_datasets import definition


@pytest.fixture(scope="module")
def settings():
    url = os.environ["DS_TEST_DATABASE_URL"]
    if not urlsplit(url).path.endswith("_test"):
        raise RuntimeError("只允许专用_test数据库")
    configured = Settings(database_url=url)
    migrate(configured)
    return configured


@pytest.fixture
def dataset(settings):
    schema = definition()
    register_dataset(settings, schema)
    return schema


@pytest.fixture
def day():
    return datetime.now(ZoneInfo("Asia/Shanghai")).date() - timedelta(days=2)


def publish(settings, schema, day, rows, *, do_publish=True):
    """查询夹具直接写独立PG，故意绕开接入模块，不模拟被测查询。"""
    batch_id = uuid4()
    with connect(settings) as connection:
        registered = get_dataset(connection, schema.slug)
        connection.execute(
            "INSERT INTO batches(id,dataset_slug,business_date,mode,status,row_count) VALUES(%s,%s,%s,'push',%s,%s)",
            (batch_id, schema.slug, day, "succeeded" if do_publish else "uploading", len(rows)),
        )
        names = [field.name for field in schema.fields]
        statement = sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
            sql.Identifier(registered["table_name"]),
            sql.SQL(",").join(sql.Identifier(name) for name in ["_batch_id", "_date", "_key"] + names),
            sql.SQL(",").join(sql.Placeholder() for _ in range(3 + len(names))),
        )
        for index, row in enumerate(rows):
            typed = validate_record(schema, {"dt": day, **row}, day)
            connection.execute(statement, [batch_id, day, str(index)] + [typed[name] for name in names])
        if do_publish:
            connection.execute(
                "INSERT INTO published_days(dataset_slug,business_date,batch_id,row_count) VALUES(%s,%s,%s,%s) ON CONFLICT(dataset_slug,business_date) DO UPDATE SET batch_id=EXCLUDED.batch_id,row_count=EXCLUDED.row_count",
                (schema.slug, day, batch_id, len(rows)),
            )
    return batch_id


def request(day, **changes):
    return QueryRequest(start_date=day, end_date=day, **changes)


def test_published_only_and_date_boundaries(settings, dataset, day):
    version = publish(settings, dataset, day, [{"id": "001", "amount": "10.50"}])
    publish(settings, dataset, day + timedelta(days=1), [{"id": "002", "amount": "20"}])
    publish(settings, dataset, day, [{"id": "unpublished", "amount": "999"}], do_publish=False)
    result = query.run_query(settings, dataset.slug, QueryRequest(start_date=day, end_date=day + timedelta(days=1)))
    assert result["total"] == 2
    assert [row["id"] for row in result["rows"]] == ["001", "002"]
    assert result["rows"][0]["amount"] == "10.500000"
    assert result["coverage"] == {"complete": True, "missing_dates": []}
    assert result["published_versions"][0]["batch_id"] == str(version)


def test_missing_day_default_failure_and_explicit_partial(settings, dataset, day):
    publish(settings, dataset, day, [{"id": "a"}])
    payload = QueryRequest(start_date=day, end_date=day + timedelta(days=1))
    with pytest.raises(ServiceError) as error:
        query.run_query(settings, dataset.slug, payload)
    assert error.value.code == "incomplete_date_range"
    assert error.value.details["missing_dates"] == [(day + timedelta(days=1)).isoformat()]
    result = query.run_query(settings, dataset.slug, payload.model_copy(update={"require_complete": False}))
    assert result["total"] == 1 and result["coverage"]["complete"] is False


def test_complete_empty_day_is_not_missing(settings, dataset, day):
    publish(settings, dataset, day, [])
    result = query.run_query(settings, dataset.slug, request(day))
    assert result["total"] == 0 and result["rows"] == [] and result["coverage"]["complete"]
    totals = query.run_query(settings, dataset.slug, request(day, aggregates=[{"op": "sum", "field": "amount", "alias": "amount_sum"}, {"op": "count", "alias": "rows_count"}]))
    assert totals["rows"] == [{"amount_sum": None, "rows_count": 0}]
    assert totals["total"] == 1


def test_outside_retention_future_and_full_window(settings):
    schema = definition(retention_days=7)
    register_dataset(settings, schema)
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    for invalid in [today + timedelta(days=1), today - timedelta(days=7)]:
        with pytest.raises(ServiceError) as error:
            query.run_query(settings, schema.slug, request(invalid, require_complete=False))
        assert error.value.code == "invalid_query"
    assert query.run_query(settings, schema.slug, QueryRequest(start_date=today - timedelta(days=6), end_date=today, require_complete=False))["coverage"]["complete"] is False


def test_full_180_day_boundary(settings, dataset):
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    payload = QueryRequest(start_date=today - timedelta(days=179), end_date=today, require_complete=False)
    assert len(query.run_query(settings, dataset.slug, payload)["coverage"]["missing_dates"]) == 180
    with pytest.raises(ValidationError):
        QueryRequest(start_date=today - timedelta(days=180), end_date=today)


def test_exact_filters_projection_stable_pagination_and_input_unchanged(settings, dataset, day):
    publish(settings, dataset, day, [{"id": "003", "shop": " 店 ", "amount": "5"}, {"id": "001", "shop": " 店 ", "amount": "10"}, {"id": "002", "shop": "店", "amount": "7"}])
    payload = request(day, fields=["id"], filters=[{"field": "shop", "op": "eq", "value": " 店 "}], limit=1, offset=1)
    result = query.run_query(settings, dataset.slug, payload)
    assert result["rows"] == [{"id": "003"}] and result["total"] == 2
    assert result["columns"] == ["id"] and payload.fields == ["id"]
    descending = query.run_query(settings, dataset.slug, request(day, fields=["id"], order_by=[{"field": "amount", "direction": "desc"}]))
    assert [row["id"] for row in descending["rows"]] == ["001", "002", "003"]


def test_null_filters_in_and_numeric_operators(settings, dataset, day):
    publish(settings, dataset, day, [{"id": "1", "amount": None, "active": False}, {"id": "2", "amount": "0", "active": True}, {"id": "3", "amount": "10"}])
    base = {"fields": ["id"]}
    for predicate, expected in [
        ({"field": "amount", "op": "is_null", "value": True}, ["1"]),
        ({"field": "amount", "op": "is_null", "value": False}, ["2", "3"]),
        ({"field": "amount", "op": "in", "value": ["0", "10"]}, ["2", "3"]),
        ({"field": "amount", "op": "gt", "value": "0"}, ["3"]),
        ({"field": "amount", "op": "gte", "value": "0"}, ["2", "3"]),
        ({"field": "amount", "op": "lt", "value": "10"}, ["2"]),
        ({"field": "amount", "op": "lte", "value": "10"}, ["2", "3"]),
        ({"field": "active", "op": "eq", "value": False}, ["1"]),
    ]:
        result = query.run_query(settings, dataset.slug, request(day, filters=[predicate], **base))
        assert [row["id"] for row in result["rows"]] == expected


def test_sql_injection_is_data(settings, dataset, day):
    hostile = "x' OR 1=1; DROP TABLE datasets; --"
    publish(settings, dataset, day, [{"id": "1", "shop": hostile}, {"id": "2", "shop": "safe"}])
    result = query.run_query(settings, dataset.slug, request(day, filters=[{"field": "shop", "op": "eq", "value": hostile}], fields=["id"]))
    assert result["rows"] == [{"id": "1"}]
    with connect(settings) as connection:
        assert get_dataset(connection, dataset.slug)


def test_grouping_aggregates_decimal_and_null(settings, dataset, day):
    publish(settings, dataset, day, [{"id": "1", "shop": "A", "amount": "1.000001", "quantity": 2}, {"id": "2", "shop": "A", "amount": "2.000003", "quantity": 3}, {"id": "3", "shop": "B", "amount": None}, {"id": "4", "shop": "A", "amount": None}])
    aggregates = [
        {"field": "amount", "op": "sum", "alias": "amount_sum"},
        {"field": "amount", "op": "avg", "alias": "amount_avg"},
        {"field": "quantity", "op": "min", "alias": "quantity_min"},
        {"field": "quantity", "op": "max", "alias": "quantity_max"},
        {"field": "amount", "op": "count", "alias": "nonnull_count"},
        {"op": "count", "alias": "row_count"},
        {"field": "shop", "op": "count_distinct", "alias": "distinct_shops"},
    ]
    result = query.run_query(settings, dataset.slug, request(day, group_by=["shop"], aggregates=aggregates))
    assert result["total"] == 2
    first, second = result["rows"]
    assert first["shop"] == "A" and first["amount_sum"] == "3.000004"
    assert first["amount_avg"] == "1.5000020000000000"
    assert (first["quantity_min"], first["quantity_max"], first["nonnull_count"], first["row_count"], first["distinct_shops"]) == (2, 3, 2, 3, 1)
    assert second["amount_sum"] is None and second["amount_avg"] is None
    assert second["nonnull_count"] == 0 and second["row_count"] == 1


def test_multi_dimensional_date_groups_and_aggregate_alias_sort(settings, dataset, day):
    publish(settings, dataset, day, [{"id": "1", "shop": "A", "amount": "-2"}, {"id": "2", "shop": "B", "amount": "9"}])
    publish(settings, dataset, day + timedelta(days=1), [{"id": "1", "shop": "A", "amount": "3"}])
    result = query.run_query(settings, dataset.slug, QueryRequest(start_date=day, end_date=day + timedelta(days=1), group_by=["dt", "shop"], aggregates=[{"field": "amount", "op": "sum", "alias": "total_amount"}], order_by=[{"field": "total_amount", "direction": "desc"}], limit=2))
    assert result["total"] == 3 and [row["total_amount"] for row in result["rows"]] == ["9.000000", "3.000000"]
    distinct = query.run_query(settings, dataset.slug, request(day, group_by=["shop"]))
    assert distinct["rows"] == [{"shop": "A"}, {"shop": "B"}]


def test_numeric_sum_exceeds_single_record_numeric_precision_without_loss(settings, dataset, day):
    maximum = "99999999999999999999999999999999.999999"
    publish(settings, dataset, day, [{"id": "1", "amount": maximum}, {"id": "2", "amount": maximum}])
    result = query.run_query(settings, dataset.slug, request(day, aggregates=[{"field": "amount", "op": "sum", "alias": "amount_sum"}]))
    assert result["rows"] == [{"amount_sum": "199999999999999999999999999999999.999998"}]


@pytest.mark.parametrize("change", [
    {"fields": ["unknown"]}, {"filters": [{"field": "missing", "op": "eq", "value": 1}]},
    {"group_by": ["amount"]}, {"aggregates": [{"field": "shop", "op": "sum", "alias": "total"}]},
    {"aggregates": [{"field": "active", "op": "min", "alias": "minimum"}]},
    {"group_by": ["shop"], "order_by": [{"field": "amount"}]},
    {"filters": [{"field": "amount", "op": "eq", "value": 0.1}]},
    {"filters": [{"field": "active", "op": "gt", "value": True}]},
])
def test_invalid_semantic_query(settings, dataset, day, change):
    with pytest.raises(ServiceError) as error:
        query.run_query(settings, dataset.slug, request(day, require_complete=False, **change))
    assert error.value.status == 422


@pytest.mark.parametrize("change", [
    {"fields": ["amount;drop"]}, {"fields": ["id", "id"]}, {"fields": []},
    {"fields": ["id"], "group_by": ["shop"]}, {"limit": 501}, {"limit": True}, {"offset": 100001},
    {"group_by": ["shop", "shop"]},
    {"group_by": ["shop"], "aggregates": [{"field": "amount", "op": "sum", "alias": "shop"}]},
    {"aggregates": [{"op": "sum", "alias": "total"}]},
    {"aggregates": [{"op": "eval", "field": "amount", "alias": "total"}]},
    {"filters": [{"field": "shop", "op": "eq", "value": None}]},
    {"filters": [{"field": "shop", "op": "in", "value": []}]},
    {"filters": [{"field": "shop", "op": "in", "value": [None]}]},
    {"filters": [{"field": "shop", "op": "in", "value": ["x"] * 201}]},
    {"filters": [{"field": "shop", "op": "is_null", "value": "true"}]},
    {"order_by": [{"field": "id"}, {"field": "id"}]},
    {"order_by": [{"field": "id", "direction": "desc;drop"}]},
])
def test_invalid_query_model(day, change):
    with pytest.raises(ValidationError):
        request(day, **change)


def test_query_date_input_strict():
    for start in ["2026-01-01T00:00:00", 1767225600, "2026-1-1"]:
        with pytest.raises(ValidationError):
            QueryRequest(start_date=start, end_date="2026-01-02")


def test_two_datasets_isolated_and_batch_all_or_nothing(settings, dataset, day):
    other = definition()
    register_dataset(settings, other)
    publish(settings, dataset, day, [{"id": "same", "amount": "1"}])
    publish(settings, other, day, [{"id": "same", "amount": "99"}])
    result = query.run_batch(settings, [BatchQueryItem(dataset=dataset.slug, query=request(day)), BatchQueryItem(dataset=other.slug, query=request(day))])
    assert result[0]["rows"][0]["amount"] == "1.000000"
    assert result[1]["rows"][0]["amount"] == "99.000000"
    with pytest.raises(ServiceError) as error:
        query.run_batch(settings, [BatchQueryItem(dataset=dataset.slug, query=request(day)), BatchQueryItem(dataset="nonexistent", query=request(day))])
    assert error.value.status == 404
    for count in [0, 11]:
        with pytest.raises(ServiceError):
            query.run_batch(settings, [BatchQueryItem(dataset=dataset.slug, query=request(day))] * count)


def test_batch_same_readonly_snapshot_despite_concurrent_publish(settings, dataset, day, monkeypatch):
    original = publish(settings, dataset, day, [{"id": "old", "amount": "1"}])
    actual = query.execute_query
    observed = []

    def intercept(connection, slug, payload, **kwargs):
        observed.append(connection.execute("SHOW transaction_isolation").fetchone()["transaction_isolation"])
        assert connection.execute("SHOW transaction_read_only").fetchone()["transaction_read_only"] == "on"
        result = actual(connection, slug, payload, **kwargs)
        if len(observed) == 1:
            publish(settings, dataset, day, [{"id": "new", "amount": "2"}])
            with connect(settings) as writer:
                registered = get_dataset(writer, dataset.slug)
                writer.execute(sql.SQL("DELETE FROM {} WHERE _batch_id=%s").format(sql.Identifier(registered["table_name"])), (original,))
        return result

    monkeypatch.setattr(query, "execute_query", intercept)
    result = query.run_batch(settings, [BatchQueryItem(dataset=dataset.slug, query=request(day))] * 2)
    assert observed == ["repeatable read", "repeatable read"]
    assert all(response["rows"][0]["id"] == "old" for response in result)
    monkeypatch.setattr(query, "execute_query", actual)
    assert query.run_query(settings, dataset.slug, request(day))["rows"][0]["id"] == "new"


def large_text_schema(settings):
    schema = definition(fields=[
        {"name": "dt", "type": "date", "required": True},
        {"name": "id", "type": "string", "required": True},
        *[{"name": f"body_{index}", "type": "string", "max_length": 10000} for index in range(4)],
    ])
    register_dataset(settings, schema)
    return schema


def response_bytes(payload):
    return len(json.dumps({"code": 200, "data": payload}, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8"))


def test_real_large_text_stops_before_materializing_all_rows(settings, day, monkeypatch):
    schema = large_text_schema(settings)
    body = "数" * 10000
    publish(settings, schema, day, [{"id": f"{index:03d}", **{f"body_{column}": body for column in range(4)}} for index in range(100)])
    observed = {"rows": 0, "server_cursors": 0}
    original_cursor = query.psycopg.Connection.cursor
    original_size = query._json_size

    def counted_cursor(connection, *args, **kwargs):
        if kwargs.get("name"):
            observed["server_cursors"] += 1
        return original_cursor(connection, *args, **kwargs)

    def counted_size(value):
        if isinstance(value, dict) and "body_0" in value:
            observed["rows"] += 1
        return original_size(value)

    monkeypatch.setattr(query.psycopg.Connection, "cursor", counted_cursor)
    monkeypatch.setattr(query, "_json_size", counted_size)
    with pytest.raises(ServiceError) as error:
        query.run_query(settings, schema.slug, request(day, limit=100))
    assert error.value.status == 413 and error.value.code == "query_result_too_large"
    assert observed["server_cursors"] == 1
    assert 1 < observed["rows"] < 100
    reduced = query.run_query(settings, schema.slug, request(day, limit=60))
    assert len(reduced["rows"]) == 60 and reduced["total"] == 100
    assert response_bytes(reduced) < 8 * 1024 * 1024
    projected = query.run_query(settings, schema.slug, request(day, fields=["id"], limit=100))
    assert len(projected["rows"]) == 100


def test_batch_total_budget_counts_all_results_and_fails_atomically(settings, day, monkeypatch):
    schema = large_text_schema(settings)
    body = "数" * 10000
    publish(settings, schema, day, [{"id": f"{index:03d}", **{f"body_{column}": body for column in range(4)}} for index in range(60)])
    item = BatchQueryItem(dataset=schema.slug, query=request(day, limit=60))
    successful = query.run_batch(settings, [item, item])
    assert len(successful) == 2 and response_bytes(successful) < 16 * 1024 * 1024
    observed = {"rows": 0}
    original_size = query._json_size

    def counted_size(value):
        if isinstance(value, dict) and "body_0" in value:
            observed["rows"] += 1
        return original_size(value)

    monkeypatch.setattr(query, "_json_size", counted_size)
    with pytest.raises(ServiceError) as error:
        query.run_batch(settings, [item, item, item])
    assert error.value.status == 413 and error.value.code == "batch_result_too_large"
    # 第三个结果中途即停止，不能先取得完整180行再计算批量大小。
    assert 120 < observed["rows"] < 180


def test_exact_byte_budget_includes_metadata_utf8_escapes_and_envelope(settings, dataset, day, monkeypatch):
    publish(settings, dataset, day, [{"id": "001", "shop": '中文"\\emoji😀'}])
    payload = request(day)
    result = query.run_query(settings, dataset.slug, payload)
    exact = response_bytes(result)
    monkeypatch.setattr(query, "MAX_QUERY_BYTES", exact)
    assert query.run_query(settings, dataset.slug, payload) == result
    monkeypatch.setattr(query, "MAX_QUERY_BYTES", exact - 1)
    with pytest.raises(ServiceError) as error:
        query.run_query(settings, dataset.slug, payload)
    assert error.value.code == "query_result_too_large"
    monkeypatch.setattr(query, "MAX_QUERY_BYTES", 8 * 1024 * 1024)
    item = BatchQueryItem(dataset=dataset.slug, query=payload)
    exact_batch = response_bytes([result, result])
    monkeypatch.setattr(query, "MAX_BATCH_BYTES", exact_batch)
    assert query.run_batch(settings, [item, item]) == [result, result]
    monkeypatch.setattr(query, "MAX_BATCH_BYTES", exact_batch - 1)
    with pytest.raises(ServiceError) as error:
        query.run_batch(settings, [item, item])
    assert error.value.code == "batch_result_too_large"


def test_metadata_alone_consumes_budget_on_empty_day(settings, dataset, day, monkeypatch):
    publish(settings, dataset, day, [])
    payload = request(day)
    result = query.run_query(settings, dataset.slug, payload)
    monkeypatch.setattr(query, "MAX_QUERY_BYTES", response_bytes(result) - 1)
    with pytest.raises(ServiceError) as error:
        query.run_query(settings, dataset.slug, payload)
    assert error.value.code == "query_result_too_large"
