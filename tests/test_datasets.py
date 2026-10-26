"""WP2 数据集测试设计（先于实现）。

DS01 两张不同 schema 注册、独立物理表、完全相同注册幂等。
DS02 已存在 schema 变更拒绝；字段/主键/日期/保留期/标识符边界。
DS03 Decimal 大额/零/负数/精度/溢出/浮点拒绝，BIGINT 边界。
DS04 NULL 与空字符串、首尾空格、前导零、未知字段、日期错位。
DS05 参数化标识符与文本、日期时间时区、必填与 Unicode 非法值。
真实 PG 测试只使用 DS_TEST_DATABASE_URL 中名称以 _test 结尾的隔离库。
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from decimal import Decimal
import os
from urllib.parse import urlsplit
from uuid import uuid4

import pytest
from pydantic import ValidationError
from psycopg import sql
from psycopg.types.json import Jsonb

from data_service.config import Settings
from data_service.datasets import get_dataset, list_datasets, register_dataset, validate_record, validate_value
from data_service.db import connect, migrate
from data_service.errors import ServiceError
from data_service.models import DatasetDefinition, FieldDefinition


@pytest.fixture(scope="module")
def settings():
    url = os.environ["DS_TEST_DATABASE_URL"]
    if not urlsplit(url).path.endswith("_test"):
        raise RuntimeError("只允许专用_test数据库")
    configured = Settings(database_url=url)
    migrate(configured)
    return configured


def definition(**updates):
    payload = {
        "slug": "dataset_" + uuid4().hex[:16], "name": "查询测试商品日表", "date_field": "dt",
        "key_fields": ["id"],
        "fields": [
            {"name": "dt", "type": "date", "required": True},
            {"name": "id", "type": "string", "required": True, "max_length": 10000},
            {"name": "shop", "type": "string", "dimension": True},
            {"name": "amount", "type": "decimal"},
            {"name": "quantity", "type": "integer"},
            {"name": "active", "type": "boolean"},
            {"name": "event_at", "type": "datetime"},
        ],
    }
    payload.update(updates)
    return DatasetDefinition.model_validate(payload)


def test_register_two_types_and_idempotence(settings):
    first = definition()
    second = definition(fields=[{"name": "dt", "type": "date", "required": True}, {"name": "id", "type": "integer", "required": True}])
    result = register_dataset(settings, first)
    assert result == register_dataset(settings, first)
    other = register_dataset(settings, second)
    assert result["table_name"] != other["table_name"]
    with connect(settings) as connection:
        columns = connection.execute(
            "SELECT column_name,data_type,numeric_precision,numeric_scale FROM information_schema.columns WHERE table_name=%s", (result["table_name"],)
        ).fetchall()
        mapped = {column["column_name"]: column for column in columns}
        assert mapped["amount"]["numeric_precision"] == 38
        assert mapped["amount"]["numeric_scale"] == 6
        assert mapped["quantity"]["data_type"] == "bigint"
        assert get_dataset(connection, first.slug)["definition"] == first.model_dump(mode="json")
    assert first.slug in {dataset["slug"] for dataset in list_datasets(settings)}


def test_changed_schema_rejected(settings):
    schema = definition()
    registered = register_dataset(settings, schema)
    with pytest.raises(ServiceError) as error:
        register_dataset(settings, schema.model_copy(update={"name": "不同名称"}))
    assert error.value.code == "dataset_schema_conflict"
    with connect(settings) as connection:
        assert get_dataset(connection, schema.slug)["table_name"] == registered["table_name"]


def test_concurrent_registration_one_physical_table(settings):
    schema = definition()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: register_dataset(settings, schema), range(2)))
    assert results[0]["table_name"] == results[1]["table_name"]


@pytest.mark.parametrize("change", [
    {"slug": "evil;DROP_TABLE"}, {"slug": "a" * 49}, {"name": "   "},
    {"key_fields": []}, {"key_fields": ["id", "id"]}, {"key_fields": ["unknown"]},
    {"key_fields": ["amount"]}, {"date_field": "amount"},
    {"retention_days": 0}, {"retention_days": 181}, {"retention_days": True},
    {"fields": [{"name": "_batch_id", "type": "string"}]},
    {"fields": [{"name": "dt", "type": "date", "required": True}, {"name": "dt", "type": "date", "required": True}]},
])
def test_schema_boundaries(change):
    with pytest.raises(ValidationError):
        definition(**change)


@pytest.mark.parametrize("raw,expected", [
    ("99999999999999999999999999999999.999999", "99999999999999999999999999999999.999999"),
    ("-99999999999999999999999999999999.999999", "-99999999999999999999999999999999.999999"),
    ("1.0000000", "1.000000"), ("1e-6", "0.000001"),
    ("-0", "0.000000"), (0, "0.000000"), (Decimal("-25.35"), "-25.350000"),
])
def test_decimal_exact(raw, expected):
    assert str(validate_value(FieldDefinition(name="amount", type="decimal"), raw)) == expected


@pytest.mark.parametrize("raw", ["1e32", "-1e32", "0.0000001", "NaN", "Infinity", "1,000", " 1.5 ", 0.1, True, {}, "1e999999", "1e-999999"])
def test_invalid_decimal_rejected(raw):
    with pytest.raises(ServiceError):
        validate_value(FieldDefinition(name="amount", type="decimal"), raw)


@pytest.mark.parametrize("raw,expected", [(str(2**63 - 1), 2**63 - 1), (-(2**63), -(2**63)), ("0004", 4), (0, 0)])
def test_bigint_exact(raw, expected):
    assert validate_value(FieldDefinition(name="quantity", type="integer"), raw) == expected


@pytest.mark.parametrize("raw", [2**63, -(2**63) - 1, True, 1.0, "1.0", " 1 "])
def test_invalid_bigint(raw):
    with pytest.raises(ServiceError):
        validate_value(FieldDefinition(name="quantity", type="integer"), raw)


def test_record_preserves_identity_and_null():
    schema = definition()
    day = date(2026, 1, 1)
    row = validate_record(schema, {"dt": "2026-01-01", "id": "0001", "shop": " 旗舰店 ", "amount": None}, day)
    assert row["id"] == "0001" and row["shop"] == " 旗舰店 "
    assert row["amount"] is None and row["quantity"] is None
    for text in ["", " "]:
        assert validate_record(schema, {"dt": day, "id": "a", "shop": text}, day)["shop"] == text


@pytest.mark.parametrize("changes", [
    {"id": None}, {"id": ""}, {"id": "  "}, {"id": 123}, {"dt": "2026-01-02"},
    {"dt": "2026-01-01T00:00:00"}, {"dt": 20260101}, {"unexpected": "value"},
    {"shop": "\u0000"}, {"shop": "\ud800"}, {"active": 1}, {"active": "true"},
    {"event_at": "2026-01-01T12:00:00"}, {"event_at": "2026-01-01T12:00:00.1234567Z"},
])
def test_record_rejects_invalid_values(changes):
    with pytest.raises(ServiceError):
        validate_record(definition(), {"dt": "2026-01-01", "id": "0001", **changes}, date(2026, 1, 1))


def test_timestamp_canonical_timezone_and_bool():
    row = validate_record(definition(), {"dt": "2026-01-01", "id": "0", "active": False, "event_at": "2026-01-01T12:00:00.123456+08:00"}, date(2026, 1, 1))
    assert row["event_at"] == datetime(2026, 1, 1, 4, 0, 0, 123456, tzinfo=timezone.utc)
    assert row["active"] is False


def test_failure_does_not_echo_raw_value():
    secret = "2026-01-01Tnot-a-password"
    with pytest.raises(ServiceError) as error:
        validate_value(FieldDefinition(name="event_at", type="datetime"), secret)
    assert secret not in str(error.value) and secret not in str(error.value.details)


def test_sql_names_safe_and_long_unicode_preserved(settings):
    schema = definition(fields=[{"name": "dt", "type": "date", "required": True}, {"name": "id", "type": "string", "required": True, "max_length": 10000}, {"name": "select", "type": "string"}])
    registered = register_dataset(settings, schema)
    with connect(settings) as connection:
        assert connection.execute(sql.SQL("SELECT {} FROM {} LIMIT 0").format(sql.Identifier("select"), sql.Identifier(registered["table_name"]))).fetchall() == []
    assert validate_record(schema, {"dt": "2026-01-01", "id": "中文" * 3000, "select": "x'; DROP TABLE datasets;--"}, date(2026, 1, 1))["select"].startswith("x';")


def test_legacy_schema_registration_defaults_are_compatible_without_rewrite(settings):
    schema = definition()
    registered = register_dataset(settings, schema)
    legacy = schema.model_dump(mode="json")
    for field in legacy["fields"]:
        field.pop("description")
    with connect(settings) as connection:
        connection.execute("UPDATE datasets SET definition=%s WHERE slug=%s", (Jsonb(legacy), schema.slug))
    repeated = register_dataset(settings, schema)
    assert repeated["table_name"] == registered["table_name"]
    assert repeated["definition"] == legacy
    with connect(settings) as connection:
        assert get_dataset(connection, schema.slug)["definition"] == legacy


def test_field_descriptions_preserved_in_metadata_and_safe_column_comments(settings):
    descriptions = ["北京时间业务自然日", "唯一标识，保留前导零", "店铺名：O'Reilly；'); DROP TABLE datasets; -- \\"]
    schema = definition(fields=[
        {"name": "dt", "type": "date", "required": True, "description": descriptions[0]},
        {"name": "id", "type": "string", "required": True, "description": descriptions[1]},
        {"name": "select", "type": "string", "description": descriptions[2]},
    ])
    registered = register_dataset(settings, schema)
    assert [field["description"] for field in registered["definition"]["fields"]] == descriptions
    with connect(settings) as connection:
        comments = connection.execute(
            "SELECT a.attname, col_description(a.attrelid,a.attnum) AS description "
            "FROM pg_attribute a WHERE a.attrelid=%s::regclass AND a.attnum>0 ORDER BY a.attnum",
            (registered["table_name"],),
        ).fetchall()
        comments_by_name = {comment["attname"]: comment["description"] for comment in comments}
        assert [comments_by_name[field.name] for field in schema.fields] == descriptions
        assert get_dataset(connection, schema.slug)["definition"] == schema.model_dump(mode="json")
    assert register_dataset(settings, schema)["table_name"] == registered["table_name"]


@pytest.mark.parametrize("existing_description,new_description", [("", "补充说明"), ("已有说明", "修改说明"), ("已有说明", "")])
def test_existing_description_changes_are_not_implicit(settings, existing_description, new_description):
    schema = definition()
    schema.fields[0].description = existing_description
    registered = register_dataset(settings, schema)
    changed = schema.model_copy(deep=True)
    changed.fields[0].description = new_description
    with pytest.raises(ServiceError) as error:
        register_dataset(settings, changed)
    assert error.value.code == "dataset_schema_conflict"
    with connect(settings) as connection:
        assert get_dataset(connection, schema.slug)["definition"] == registered["definition"]


@pytest.mark.parametrize("description", ["", " 支付金额，缺失保留NULL ", "字" * 500])
def test_description_text_is_preserved(description):
    field = FieldDefinition(name="amount", type="decimal", description=description)
    assert field.description == description
    assert FieldDefinition.model_validate(field.model_dump()).description == description


@pytest.mark.parametrize("description", ["字" * 501, None, 1, True, [], "说明\x00", "说明\n", "说明\t", "说明\x7f", "说明\x85", "说明\ud800", "说明\udfff"])
def test_description_rejects_invalid_text(description):
    with pytest.raises(ValidationError):
        FieldDefinition(name="amount", type="decimal", description=description)


def test_description_defaults_and_assignment_validation():
    field = FieldDefinition(name="amount", type="decimal")
    assert field.description == ""
    with pytest.raises(ValidationError):
        field.description = "错误\x00"
    assert field.description == ""
