"""多结构真实PG测试；查询、金额与原子发布均使用产品实现。"""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from threading import Barrier

import pytest
from psycopg import sql
from pydantic import ValidationError

from data_service import query
from data_service.datasets import register_dataset, validate_record, validate_value
from data_service.db import connect
from data_service.errors import ServiceError
from data_service.ingest import append_chunk, cancel_batch, commit_batch, create_batch, get_batch
from data_service.models import BatchQueryItem, DatasetDefinition, FieldDefinition, QueryRequest


def publish(settings, slug, day, rows):
    batch = create_batch(settings, slug, day, len(rows), None)
    if rows:
        append_chunk(settings, batch["id"], 0, rows)
    return commit_batch(settings, batch["id"])


@pytest.fixture
def registered(independent_settings, dataset_definitions):
    return [register_dataset(independent_settings, DatasetDefinition.model_validate(definition))
            for definition in dataset_definitions]


def request(day, **changes):
    return QueryRequest(start_date=day, end_date=day, **changes)


def sale(day, identifier="001", **changes):
    return {"dt": day.isoformat(), "product_id": identifier, "shop": " shop ", "amount": "10.100001", "units": 2, **changes}


def test_two_structures_register_idempotent_and_conflict(independent_settings, registered, dataset_definitions):
    again = register_dataset(independent_settings, DatasetDefinition.model_validate(dataset_definitions[0]))
    assert again["table_name"] == registered[0]["table_name"]
    assert registered[0]["table_name"] != registered[1]["table_name"]
    changed = deepcopy(dataset_definitions[0])
    changed["fields"].append({"name": "unexpected", "type": "boolean"})
    with pytest.raises(ServiceError) as failure:
        register_dataset(independent_settings, DatasetDefinition.model_validate(changed))
    assert failure.value.status == 409
    with connect(independent_settings) as connection:
        sales_columns = connection.execute("SELECT column_name,data_type FROM information_schema.columns WHERE table_name=%s", (registered[0]["table_name"],)).fetchall()
        sensor_columns = connection.execute("SELECT column_name,data_type FROM information_schema.columns WHERE table_name=%s", (registered[1]["table_name"],)).fetchall()
    assert {column["column_name"] for column in sales_columns} != {column["column_name"] for column in sensor_columns}
    assert next(column for column in sensor_columns if column["column_name"] == "ok")["data_type"] == "boolean"


@pytest.mark.parametrize("value", ["99999999999999999999999999999999.999999", "-99999999999999999999999999999999.999999"])
def test_decimal_extrema_are_lossless(value):
    assert validate_value(FieldDefinition(name="amount", type="decimal"), value) == Decimal(value)


@pytest.mark.parametrize("value", ["100000000000000000000000000000000", "-100000000000000000000000000000000", "0.0000001", "NaN", "Infinity", 0.1, True])
def test_decimal_invalid_values_rejected(value):
    with pytest.raises(ServiceError):
        validate_value(FieldDefinition(name="amount", type="decimal"), value)


@pytest.mark.parametrize("changes", [
    {"product_id": ""}, {"product_id": "   "}, {"product_id": "x" * 81}, {"product_id": "a\x00b"},
    {"amount": "0.0000001"}, {"units": 2**63}, {"units": True}, {"unknown": "x"}, {"dt": "2026-01-01T00:00:00"},
])
def test_record_boundaries(day, dataset_definitions, changes):
    with pytest.raises(ServiceError):
        validate_record(DatasetDefinition.model_validate(dataset_definitions[0]), sale(day, **changes), day)


def test_atomic_replacement_and_failed_chunk_preserve_old(independent_settings, registered, day):
    slug = registered[0]["slug"]
    old = publish(independent_settings, slug, day, [sale(day), sale(day, "002")])
    pending = create_batch(independent_settings, slug, day, 2, "replace")
    append_chunk(independent_settings, pending["id"], 0, [sale(day, "003", amount="50")])
    with pytest.raises(ServiceError):
        append_chunk(independent_settings, pending["id"], 1, [sale(day, "004", amount="bad")])
    visible = query.run_query(independent_settings, slug, request(day))
    assert [row["product_id"] for row in visible["rows"]] == ["001", "002"]
    assert visible["published_versions"][0]["batch_id"] == str(old["id"])
    with pytest.raises(ServiceError):
        commit_batch(independent_settings, pending["id"])
    append_chunk(independent_settings, pending["id"], 1, [sale(day, "004", amount="-3")])
    commit_batch(independent_settings, pending["id"])
    assert [row["product_id"] for row in query.run_query(independent_settings, slug, request(day))["rows"]] == ["003", "004"]
    with connect(independent_settings) as connection:
        assert connection.execute(sql.SQL("SELECT COUNT(*) AS n FROM {} WHERE _batch_id=%s").format(sql.Identifier(registered[0]["table_name"])), (old["id"],)).fetchone()["n"] == 0
    assert commit_batch(independent_settings, pending["id"])["status"] == "succeeded"


def test_chunks_idempotent_conflict_duplicate_and_cancel(independent_settings, registered, day):
    slug = registered[0]["slug"]
    original = publish(independent_settings, slug, day, [sale(day)])
    batch = create_batch(independent_settings, slug, day, 2, "idempotent")
    assert create_batch(independent_settings, slug, day, 2, "idempotent")["id"] == batch["id"]
    row = sale(day, "003", amount="1.0")
    append_chunk(independent_settings, batch["id"], 0, [row])
    replay = append_chunk(independent_settings, batch["id"], 0, [{**row, "amount": "1.00"}])
    assert replay["row_count"] == 1
    with pytest.raises(ServiceError) as changed:
        append_chunk(independent_settings, batch["id"], 0, [{**row, "amount": "2"}])
    assert changed.value.status == 409
    with pytest.raises(ServiceError) as duplicate:
        append_chunk(independent_settings, batch["id"], 1, [row])
    assert duplicate.value.status == 409
    assert get_batch(independent_settings, batch["id"])["row_count"] == 1
    cancel_batch(independent_settings, batch["id"])
    assert cancel_batch(independent_settings, batch["id"])["status"] == "cancelled"
    assert query.run_query(independent_settings, slug, request(day))["published_versions"][0]["batch_id"] == str(original["id"])


def test_non_contiguous_chunks_cannot_publish(independent_settings, registered, day):
    batch = create_batch(independent_settings, registered[0]["slug"], day, 1, None)
    append_chunk(independent_settings, batch["id"], 1, [sale(day)])
    with pytest.raises(ServiceError) as failure:
        commit_batch(independent_settings, batch["id"])
    assert failure.value.code == "CHUNKS_INCOMPLETE"


def test_null_zero_negative_and_group_aggregates(independent_settings, registered, day):
    slug = registered[0]["slug"]
    publish(independent_settings, slug, day, [
        sale(day, "001", shop="A", amount="1.100001", units=2),
        sale(day, "002", shop="A", amount="-0.100001", units=0),
        sale(day, "003", shop="A", amount=None, units=None),
        sale(day, "004", shop="B", amount=None, units=None),
        sale(day, "005", shop="", amount="0", units=0),
    ])
    result = query.run_query(independent_settings, slug, request(day, group_by=["shop"], aggregates=[
        {"op": "sum", "field": "amount", "alias": "sum_amount"},
        {"op": "count", "alias": "records"},
        {"op": "count", "field": "amount", "alias": "valid"},
        {"op": "count_distinct", "field": "units", "alias": "distinct_units"},
    ]))
    indexed = {row["shop"]: row for row in result["rows"]}
    assert indexed["A"] == {"shop": "A", "sum_amount": "1.000000", "records": 3, "valid": 2, "distinct_units": 2}
    assert indexed["B"]["sum_amount"] is None and indexed["B"]["valid"] == 0
    assert indexed[""]["sum_amount"] == "0.000000"
    assert result["total"] == 3


def test_missing_and_empty_days_are_distinct(independent_settings, registered, day):
    slug = registered[0]["slug"]
    publish(independent_settings, slug, day, [])
    assert query.run_query(independent_settings, slug, request(day))["coverage"] == {"complete": True, "missing_dates": []}
    previous = day - timedelta(days=1)
    with pytest.raises(ServiceError) as missing:
        query.run_query(independent_settings, slug, QueryRequest(start_date=previous, end_date=day))
    assert missing.value.status == 409
    partial = query.run_query(independent_settings, slug, QueryRequest(start_date=previous, end_date=day, require_complete=False))
    assert partial["coverage"] == {"complete": False, "missing_dates": [previous.isoformat()]}


def test_exact_text_filter_and_sql_value_are_safe(independent_settings, registered, day):
    slug = registered[0]["slug"]
    suspicious = "x' OR 1=1; DROP TABLE datasets; --"
    publish(independent_settings, slug, day, [sale(day, "001", shop=suspicious), sale(day, "002", shop=" shop "), sale(day, "003", shop="shop")])
    for value, expected in [(suspicious, "001"), (" shop ", "002"), ("shop", "003")]:
        result = query.run_query(independent_settings, slug, request(day, filters=[{"field": "shop", "op": "eq", "value": value}]))
        assert result["total"] == 1 and result["rows"][0]["product_id"] == expected
    with pytest.raises(ValidationError):
        request(day, fields=["amount); DROP TABLE datasets; --"])


def test_multiple_dates_stable_pagination(independent_settings, registered, day):
    slug = registered[0]["slug"]
    earlier = day - timedelta(days=1)
    for when in [day, earlier]:
        publish(independent_settings, slug, when, [sale(when, "002"), sale(when, "001")])
    rows = []
    for offset in range(4):
        result = query.run_query(independent_settings, slug, QueryRequest(start_date=earlier, end_date=day, limit=1, offset=offset))
        assert result["total"] == 4
        rows.extend(result["rows"])
    assert [(row["product_id"], row["dt"]) for row in rows] == [(identifier, when.isoformat()) for identifier in ["001", "002"] for when in [earlier, day]]


def test_sensor_structure_and_timezone(independent_settings, registered, day):
    slug = registered[1]["slug"]
    publish(independent_settings, slug, day, [{"sample_day": day.isoformat(), "device": "0007", "sequence": 3, "captured_at": f"{day}T08:00:00+08:00", "ok": False, "temperature": "-0.123456"}])
    row = query.run_query(independent_settings, slug, request(day))["rows"][0]
    assert row["device"] == "0007" and row["ok"] is False and row["temperature"] == "-0.123456"
    # API约定带时区ISO；数据库会话可用北京时间展示，必须比较同一瞬间而非强制UTC文本。
    assert datetime.fromisoformat(row["captured_at"]) == datetime.combine(day, datetime.min.time(), timezone.utc)


def test_batch_same_snapshot_during_second_dataset_publish(independent_settings, registered, day, monkeypatch):
    sale_slug, sensor_slug = [dataset["slug"] for dataset in registered]
    publish(independent_settings, sale_slug, day, [sale(day)])
    old_rows = [{"sample_day": day.isoformat(), "device": "old", "sequence": 1}]
    old = publish(independent_settings, sensor_slug, day, old_rows)
    original = query.execute_query
    calls = []

    def concurrent_publication(connection, slug, payload, **options):
        result = original(connection, slug, payload, **options)
        calls.append(slug)
        if len(calls) == 1:
            publish(independent_settings, sensor_slug, day, [{"sample_day": day.isoformat(), "device": "new", "sequence": 1}])
        return result

    monkeypatch.setattr(query, "execute_query", concurrent_publication)
    results = query.run_batch(independent_settings, [BatchQueryItem(dataset=slug, query=request(day)) for slug in [sale_slug, sensor_slug]])
    assert results[1]["rows"][0]["device"] == "old"
    assert results[1]["published_versions"][0]["batch_id"] == str(old["id"])
    monkeypatch.setattr(query, "execute_query", original)
    assert query.run_query(independent_settings, sensor_slug, request(day))["rows"][0]["device"] == "new"


def test_same_day_concurrent_creation_has_single_winner(independent_settings, registered, day):
    barrier = Barrier(2)

    def create_one():
        barrier.wait(timeout=5)
        try:
            return create_batch(independent_settings, registered[0]["slug"], day, 0, None)["status"]
        except ServiceError as failure:
            return failure.status

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(lambda _: create_one(), range(2)))
    assert sorted(map(str, outcomes)) == ["409", "uploading"]
