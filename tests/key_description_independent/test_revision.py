"""数据库维护入口独立故障验证；所有状态构造仅在隔离库中完成。"""

from copy import deepcopy

from psycopg import sql
from psycopg.types.json import Jsonb
import pytest

from data_service import dataset_revision, db
from data_service.datasets import get_dataset, register_dataset
from data_service.errors import ServiceError
from data_service.ingest import append_chunk, cancel_batch, commit_batch, create_batch
from data_service.models import DatasetDefinition


def schemas():
    before = {"slug": "ind_revision", "name": "独立修订表", "date_field": "dt", "key_fields": ["dt", "product_id"],
              "fields": [
                  {"name": "product_day_id", "type": "string"},
                  {"name": "dt", "type": "date", "required": True},
                  {"name": "product_id", "type": "string", "required": True},
                  {"name": "amount", "type": "decimal"},
              ]}
    after = deepcopy(before)
    after["key_fields"] = ["product_day_id"]
    after["fields"][0]["required"] = True
    for field, description in zip(after["fields"], ["唯一行标识", "业务日期", "商品ID", "金额，缺失保留NULL"], strict=True):
        field["description"] = description
    return DatasetDefinition.model_validate(before), DatasetDefinition.model_validate(after)


def state(settings, slug):
    with db.connect(settings) as connection:
        dataset = get_dataset(connection, slug)
        columns = connection.execute("SELECT a.attname,a.attnotnull,col_description(c.oid,a.attnum) AS description "
                                     "FROM pg_class c JOIN pg_attribute a ON a.attrelid=c.oid "
                                     "WHERE c.relname=%s AND a.attnum>0 ORDER BY a.attnum",
                                     (dataset["table_name"],)).fetchall()
        batches = connection.execute("SELECT id,status,error_code FROM batches WHERE dataset_slug=%s ORDER BY id", (slug,)).fetchall()
        return {"definition": dataset["definition"], "table": dataset["table_name"], "columns": columns, "batches": batches}


def test_empty_revision_atomic_and_idempotent_keeps_history(isolated_settings, business_day):
    old, new = schemas()
    register_dataset(isolated_settings, old)
    historical = create_batch(isolated_settings, old.slug, business_day, 0, None)
    cancel_batch(isolated_settings, historical["id"])
    original = state(isolated_settings, old.slug)
    revised = dataset_revision.revise_empty_dataset(isolated_settings, old, new)
    actual = state(isolated_settings, old.slug)
    assert actual["table"] == original["table"]
    assert actual["batches"] == original["batches"]
    assert revised["definition"] == new.model_dump(mode="json")
    columns = {column["attname"]: column for column in actual["columns"]}
    assert columns["product_day_id"]["attnotnull"] is True
    assert all(columns[field.name]["description"] == field.description for field in new.fields)
    assert dataset_revision.revise_empty_dataset(isolated_settings, old, new)["definition"] == revised["definition"]


def test_revision_stale_expected_rejected(isolated_settings):
    old, new = schemas()
    register_dataset(isolated_settings, old)
    changed = old.model_dump(mode="json")
    changed["fields"][0]["description"] = "另一个维护者已修改"
    with db.connect(isolated_settings) as connection:
        connection.execute("UPDATE datasets SET definition=%s WHERE slug=%s", (Jsonb(changed), old.slug))
    original = state(isolated_settings, old.slug)
    with pytest.raises(ServiceError) as caught:
        dataset_revision.revise_empty_dataset(isolated_settings, old, new)
    assert caught.value.code == "DATASET_REVISION_CONFLICT"
    assert state(isolated_settings, old.slug) == original


@pytest.mark.parametrize("case", ["active", "published_empty", "failed_with_records"])
def test_revision_refuses_active_published_or_stored(isolated_settings, business_day, case):
    old, new = schemas()
    register_dataset(isolated_settings, old)
    batch = create_batch(isolated_settings, old.slug, business_day, None, None)
    if case == "published_empty":
        commit_batch(isolated_settings, batch["id"])
    if case == "failed_with_records":
        append_chunk(isolated_settings, batch["id"], 0, [
            {"dt": business_day.isoformat(), "product_day_id": "id", "product_id": "0001", "amount": None}
        ])
        with db.connect(isolated_settings) as connection:
            connection.execute("UPDATE batches SET status='failed' WHERE id=%s", (batch["id"],))
    original = state(isolated_settings, old.slug)
    with pytest.raises(ServiceError) as caught:
        dataset_revision.revise_empty_dataset(isolated_settings, old, new)
    expected = "DATASET_NOT_EMPTY" if case == "failed_with_records" else "DATASET_REVISION_BLOCKED"
    assert caught.value.code == expected
    assert state(isolated_settings, old.slug) == original


@pytest.mark.parametrize("case", ["name", "type", "add_field", "weaken_required"])
def test_revision_scope_rejects_other_changes(isolated_settings, case):
    old, new = schemas()
    register_dataset(isolated_settings, old)
    updated = new.model_dump(mode="json")
    if case == "name":
        updated["name"] = "越界改名"
    elif case == "type":
        updated["fields"][-1]["type"] = "integer"
    elif case == "add_field":
        updated["fields"].append({"name": "unexpected", "type": "string"})
    else:
        updated["fields"][2]["required"] = False
    original = state(isolated_settings, old.slug)
    with pytest.raises(ServiceError) as caught:
        dataset_revision.revise_empty_dataset(isolated_settings, old, DatasetDefinition.model_validate(updated))
    assert caught.value.code == "REVISION_SCOPE_INVALID"
    assert state(isolated_settings, old.slug) == original


def test_revision_failure_rolls_back_ddl_comments_metadata(isolated_settings, monkeypatch):
    old, new = schemas()
    register_dataset(isolated_settings, old)
    original = state(isolated_settings, old.slug)

    def fail_during_comments(connection, table, fields):
        connection.execute(sql.SQL("COMMENT ON COLUMN {}.product_day_id IS '故障注入暂存注释'").format(sql.Identifier(table)))
        raise RuntimeError("independent transaction failure")

    monkeypatch.setattr(dataset_revision, "apply_field_comments", fail_during_comments)
    with pytest.raises(RuntimeError, match="independent transaction failure"):
        dataset_revision.revise_empty_dataset(isolated_settings, old, new)
    assert state(isolated_settings, old.slug) == original
