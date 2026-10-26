"""独立用例：说明兼容、安全边界、单键身份、分页和重复原子失败。"""

from pathlib import Path
import json

import httpx
from psycopg import sql
from psycopg.types.json import Jsonb
from pydantic import ValidationError
import pytest

from data_service import db
from data_service.config import Settings
from data_service.datasets import get_dataset, register_dataset, validate_record
from data_service.errors import ServiceError
from data_service.ingest import append_chunk, commit_batch, create_batch, get_batch
from data_service.models import DatasetDefinition, FieldDefinition
from data_service.source import SourceClient, SourceConfig
from data_service.worker import acquire_worker_lock, claim_next_job, process_job


def definition():
    return DatasetDefinition.model_validate({
        "slug": "key_description_case", "name": "独立主键说明验收", "date_field": "dt",
        "key_fields": ["product_day_id"], "fields": [
            {"name": "dt", "type": "date", "required": True, "description": "北京时间业务自然日"},
            {"name": "product_day_id", "type": "string", "required": True,
             "description": "唯一行标识，业务日期_商品ID，重跑保持一致"},
            {"name": "product_id", "type": "string", "description": "商品ID，保留前导零"},
            {"name": "paid_amount", "type": "decimal", "description": "支付金额，缺失保留NULL"},
        ],
    })


def record(day, identifier="001", product="0001"):
    return {"dt": day.isoformat(), "product_day_id": identifier,
            "product_id": product, "paid_amount": None}


def source_config(start):
    config = SourceConfig(dataset_slug="key_description_case", url="https://source.test/rows",
                          protocol="vendor_batch", vendor_api_id="independent-api", page_start=start,
                          page_size=200, vendor_app_key_env="CASE_KEY", vendor_app_secret_env="CASE_SECRET")
    config._managed_secrets = {"CASE_KEY": "synthetic-key", "CASE_SECRET": "synthetic-secret"}
    return config


def test_description_omission_compatible():
    field = FieldDefinition(name="id", type="string")
    assert field.description == ""


@pytest.mark.parametrize("invalid", [None, 123, "a" * 501, "\x00", "说明\n内容", "\ud800"])
def test_description_invalid_values_rejected(invalid):
    with pytest.raises(ValidationError):
        FieldDefinition(name="id", type="string", description=invalid)


def test_description_plain_html_preserved_for_escape():
    payload = '<img src=x onerror="window.__descriptionXss=1">字段说明'
    field = FieldDefinition(name="id", type="string", description=payload)
    assert field.description == payload


def test_register_roundtrip_and_legacy_idempotency(isolated_settings):
    schema = definition()
    dataset = register_dataset(isolated_settings, schema)
    with db.connect(isolated_settings) as connection:
        assert get_dataset(connection, schema.slug)["definition"] == schema.model_dump(mode="json")
        # 模拟升级前数据库中的 schema JSON；不动任何业务库。
        legacy = schema.model_dump(mode="json")
        for field in legacy["fields"]:
            field.pop("description")
        connection.execute("UPDATE datasets SET definition=%s WHERE slug=%s", (Jsonb(legacy), schema.slug))
    legacy_schema = DatasetDefinition.model_validate(legacy)
    same = register_dataset(isolated_settings, legacy_schema)
    assert same["table_name"] == dataset["table_name"]
    with pytest.raises(ServiceError) as caught:
        register_dataset(isolated_settings, schema)
    assert caught.value.code == "dataset_schema_conflict"


@pytest.mark.parametrize("invalid", [None, "", "   "])
def test_single_key_null_empty_rejected(invalid, business_day):
    row = record(business_day)
    row["product_day_id"] = invalid
    with pytest.raises(ServiceError) as caught:
        validate_record(definition(), row, business_day)
    assert caught.value.code == "invalid_record"


def test_single_key_not_dt_product_composite(isolated_settings, business_day):
    schema = definition()
    register_dataset(isolated_settings, schema)
    batch = create_batch(isolated_settings, schema.slug, business_day, 2, None)
    # 同一天相同商品，唯一行标识不同时应保存为两条，不再错误依赖旧复合键。
    append_chunk(isolated_settings, batch["id"], 0,
                 [record(business_day, "00001"), record(business_day, "00002")])
    assert commit_batch(isolated_settings, batch["id"])["row_count"] == 2
    with db.connect(isolated_settings) as connection:
        table = get_dataset(connection, schema.slug)["table_name"]
        rows = connection.execute(sql.SQL("SELECT product_day_id,product_id,paid_amount FROM {} ORDER BY product_day_id")
                                  .format(sql.Identifier(table))).fetchall()
    assert [row["product_day_id"] for row in rows] == ["00001", "00002"]
    assert all(row["product_id"] == "0001" and row["paid_amount"] is None for row in rows)


def test_duplicate_key_different_product_is_atomic(isolated_settings, business_day):
    schema = definition()
    register_dataset(isolated_settings, schema)
    batch = create_batch(isolated_settings, schema.slug, business_day, 3, None)
    append_chunk(isolated_settings, batch["id"], 0, [record(business_day)])
    with pytest.raises(ServiceError) as caught:
        append_chunk(isolated_settings, batch["id"], 1,
                     [record(business_day, "002"), record(business_day, product="different")])
    assert caught.value.code == "DUPLICATE_RECORD"
    assert get_batch(isolated_settings, batch["id"])["row_count"] == 1
    with db.connect(isolated_settings) as connection:
        table = get_dataset(connection, schema.slug)["table_name"]
        assert connection.execute(sql.SQL("SELECT count(*) AS n FROM {}").format(sql.Identifier(table))).fetchone()["n"] == 1


@pytest.mark.parametrize("start", [0, 1])
def test_vendor_start_zero_and_one_increment_until_empty(start, business_day):
    observed = []

    def respond(request):
        payload = json.loads(request.content)
        observed.append(payload)
        page = payload["pageIndex"]
        rows = [] if page == start + 2 else [record(business_day, str(page))]
        return httpx.Response(200, json={"success": True, "code": "200", "data": rows, "extraInfo": None})

    settings = Settings(database_url="postgresql://unused/unused_test", allowed_source_hosts=["source.test"])
    with SourceClient(source_config(start), settings, transport=httpx.MockTransport(respond),
                      sleep=lambda _: None) as client:
        pages = list(client.iter_pages(business_day))
    assert [payload["pageIndex"] for payload in observed] == [start, start + 1, start + 2]
    assert [len(page) for page in pages] == [1, 1, 0]
    assert all(payload["pageSize"] == 200 for payload in observed)
    assert all(payload["batchQueryParams"] == [{"openFieldName": "dt", "openFieldValue": business_day.isoformat()}]
               for payload in observed)


def test_duplicate_vendor_pages_fail_preserve_published(isolated_settings, business_day):
    schema = definition()
    dataset = register_dataset(isolated_settings, schema)
    old = create_batch(isolated_settings, schema.slug, business_day, 1, None)
    append_chunk(isolated_settings, old["id"], 0, [record(business_day, "previous-published")])
    commit_batch(isolated_settings, old["id"])
    pending = create_batch(isolated_settings, schema.slug, business_day, None, None, mode="pull")
    observed = []

    def respond(request):
        page = json.loads(request.content)["pageIndex"]
        observed.append(page)
        return httpx.Response(200, json={"success": True, "code": "200", "data": [record(business_day)]})

    with db.connect(isolated_settings) as connection:
        connection.autocommit = True
        assert acquire_worker_lock(connection)
        claimed = claim_next_job(connection)
        assert claimed["id"] == pending["id"]
        with SourceClient(source_config(1), isolated_settings, transport=httpx.MockTransport(respond),
                          sleep=lambda _: None) as client:
            assert process_job(connection, isolated_settings, claimed, client) is False
    assert observed == [1, 2]
    assert get_batch(isolated_settings, pending["id"])["error_code"] == "DUPLICATE_RECORD"
    with db.connect(isolated_settings) as connection:
        current = connection.execute("SELECT batch_id FROM published_days").fetchone()
        assert current["batch_id"] == old["id"]
        rows = connection.execute(sql.SQL("SELECT product_day_id FROM {}")
                                  .format(sql.Identifier(dataset["table_name"]))).fetchall()
    assert rows == [{"product_day_id": "previous-published"}]


def test_real_product_config_single_key_and_32_descriptions():
    root = Path(__file__).resolve().parents[2]
    payload = json.loads((root / "config/product_day.dataset.json").read_text(encoding="utf-8"))
    schema = DatasetDefinition.model_validate(payload)
    assert schema.key_fields == ["product_day_id"]
    assert schema.date_field == "dt" and schema.retention_days == 180
    assert len(schema.fields) == 32
    assert next(field for field in schema.fields if field.name == "product_day_id").required
    assert all(field.description.strip() and any("\u4e00" <= char <= "\u9fff" for char in field.description)
               for field in schema.fields)
