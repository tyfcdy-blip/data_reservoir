"""实际PostgreSQL验证分片、原子发布、恢复、保留和并发。"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from decimal import Decimal
import json
import os
import httpx
from urllib.parse import urlsplit
from uuid import uuid4

from psycopg import sql
from psycopg.types.json import Jsonb
import pytest

from data_service.config import Settings
from data_service.datasets import register_dataset
from data_service.db import connect, migrate
from data_service.errors import ServiceError
from data_service.ingest import (BEIJING, append_chunk, cancel_batch, commit_batch, create_batch,
                                 enqueue_sync, get_batch)
from data_service.models import DatasetDefinition
from data_service.source import SourceClient, SourceConfig, SourceError
from data_service.worker import (acquire_worker_lock, claim_next_job, cleanup_history, enqueue_schedule,
                                 heartbeat, process_job, recover_interrupted_jobs, require_worker_lock)


@pytest.fixture(scope="module")
def configured_settings():
    dsn = os.environ["DS_TEST_DATABASE_URL"]
    if urlsplit(dsn).path.rsplit("/", 1)[-1] != "standalone_ingest_test":
        raise RuntimeError("仅允许专用接入测试库")
    result = Settings(database_url=dsn, allowed_source_hosts=["source.test"])
    with connect(result) as connection:
        connection.execute("DROP SCHEMA public CASCADE")
        connection.execute("CREATE SCHEMA public")
    migrate(result)
    return result


@pytest.fixture
def service(configured_settings, tmp_path):
    with connect(configured_settings) as connection:
        for row in connection.execute("SELECT table_name FROM datasets").fetchall():
            connection.execute(sql.SQL("DROP TABLE {} CASCADE").format(sql.Identifier(row["table_name"])))
        connection.execute("TRUNCATE datasets,api_keys,worker_heartbeat CASCADE")
    config = tmp_path / "sources.json"
    config.write_text(json.dumps({"sources": [{"dataset_slug": "orders", "url": "https://source.test/rows"}]}), encoding="utf-8")
    result = configured_settings.model_copy(update={"source_config_path": str(config)})
    definition = DatasetDefinition(slug="orders", name="订单合成测试", date_field="day", key_fields=["id"], fields=[
        {"name": "day", "type": "date", "required": True}, {"name": "id", "type": "string", "required": True, "max_length": 10000},
        {"name": "amount", "type": "decimal"}, {"name": "shop", "type": "string", "dimension": True},
    ])
    register_dataset(result, definition)
    return result


def day(offset=1):
    return datetime.now(BEIJING).date() - timedelta(days=offset)


def row(identifier="001", amount="10.500000", offset=1):
    return {"day": day(offset).isoformat(), "id": identifier, "amount": amount, "shop": " 店铺 "}


def batch(service, expected=1, **kwargs):
    return create_batch(service, "orders", day(), expected, None, **kwargs)


def published(service):
    with connect(service) as connection:
        table = connection.execute("SELECT table_name FROM datasets WHERE slug='orders'").fetchone()["table_name"]
        return connection.execute(sql.SQL("SELECT r.* FROM {} r JOIN published_days p ON p.batch_id=r._batch_id ORDER BY r.id")
                                  .format(sql.Identifier(table))).fetchall()


def worker_connection(service):
    connection = connect(service)
    connection.autocommit = True
    assert acquire_worker_lock(connection)
    return connection


def test_push_atomic_and_replacement_deletes_old(service):
    first = batch(service, 2)
    append_chunk(service, first["id"], 0, [row(), row("002", None)])
    assert published(service) == []
    assert commit_batch(service, first["id"])["status"] == "succeeded"
    assert len(published(service)) == 2
    second = batch(service)
    append_chunk(service, second["id"], 0, [row("003", "-1.000001")])
    assert len(published(service)) == 2
    commit_batch(service, second["id"])
    result = published(service)
    assert len(result) == 1 and result[0]["id"] == "003" and result[0]["amount"] == Decimal("-1.000001")
    with connect(service) as connection:
        table = connection.execute("SELECT table_name FROM datasets WHERE slug='orders'").fetchone()["table_name"]
        assert connection.execute(sql.SQL("SELECT COUNT(*) AS n FROM {} WHERE _batch_id=%s").format(sql.Identifier(table)),
                                  (first["id"],)).fetchone()["n"] == 0
    assert commit_batch(service, first["id"])["status"] == "succeeded"
    assert published(service)[0]["id"] == "003"


def test_chunk_idempotency_conflict_count_and_missing_sequence(service):
    active = batch(service, 2)
    append_chunk(service, active["id"], 0, [row()])
    assert append_chunk(service, active["id"], 0, [row()])["row_count"] == 1
    with pytest.raises(ServiceError) as caught:
        append_chunk(service, active["id"], 0, [row(amount="20")])
    assert caught.value.code == "CHUNK_CONFLICT"
    with pytest.raises(ServiceError) as caught:
        commit_batch(service, active["id"])
    assert caught.value.code == "ROW_COUNT_MISMATCH"
    append_chunk(service, active["id"], 2, [row("002")])
    with pytest.raises(ServiceError) as caught:
        commit_batch(service, active["id"])
    assert caught.value.code == "CHUNKS_INCOMPLETE"
    append_chunk(service, active["id"], 1, [])
    assert commit_batch(service, active["id"])["row_count"] == 2


def test_duplicate_within_and_across_chunks_rejected_without_partial_write(service):
    active = batch(service, 3)
    with pytest.raises(ServiceError) as caught:
        append_chunk(service, active["id"], 0, [row(), row()])
    assert caught.value.code == "DUPLICATE_RECORD"
    assert get_batch(service, active["id"])["row_count"] == 0
    append_chunk(service, active["id"], 0, [row()])
    with pytest.raises(ServiceError) as caught:
        append_chunk(service, active["id"], 1, [row("002"), row()])
    assert caught.value.code == "DUPLICATE_RECORD"
    assert get_batch(service, active["id"])["row_count"] == 1
    append_chunk(service, active["id"], 1, [row("002"), row("003")])
    assert commit_batch(service, active["id"])["row_count"] == 3


def test_empty_published_day_and_cancel(service):
    active = batch(service, 0)
    assert commit_batch(service, active["id"])["status"] == "succeeded"
    with connect(service) as connection:
        assert connection.execute("SELECT row_count FROM published_days").fetchone()["row_count"] == 0
    following = batch(service)
    append_chunk(service, following["id"], 0, [row()])
    assert cancel_batch(service, following["id"])["status"] == "cancelled"
    assert cancel_batch(service, following["id"])["status"] == "cancelled"
    with pytest.raises(ServiceError):
        commit_batch(service, following["id"])
    assert published(service) == []


def test_creation_idempotency_and_active_exclusion(service):
    active = create_batch(service, "orders", day(), 1, "import-run-1")
    same = create_batch(service, "orders", day(), 1, "import-run-1")
    assert active["id"] == same["id"]
    with pytest.raises(ServiceError) as caught:
        create_batch(service, "orders", day(2), 1, "import-run-1")
    assert caught.value.code == "IDEMPOTENCY_CONFLICT"
    with pytest.raises(ServiceError) as caught:
        batch(service)
    assert caught.value.code == "BATCH_ALREADY_ACTIVE"


def test_owner_isolation(service):
    owner = uuid4()
    other = uuid4()
    with connect(service) as connection:
        for identifier in [owner, other]:
            connection.execute("INSERT INTO api_keys(id,name,token_hash,token_hint,grants,requests_per_minute,expires_at) "
                               "VALUES (%s,'test',%s,'hint',%s,60,now()+interval '1 hour')",
                               (identifier, str(identifier), Jsonb({"orders": ["write"]})))
    active = batch(service, owner_key_id=owner)
    for operation in [lambda: append_chunk(service, active["id"], 0, [row()], other),
                      lambda: commit_batch(service, active["id"], other),
                      lambda: cancel_batch(service, active["id"], other)]:
        with pytest.raises(ServiceError) as caught:
            operation()
        assert caught.value.status == 403
    append_chunk(service, active["id"], 0, [row()], owner)
    assert commit_batch(service, active["id"], owner)["status"] == "succeeded"


@pytest.mark.parametrize("value", [-1, 1000001, True, "10", 1.5])
def test_expected_row_limit(service, value):
    with pytest.raises(ServiceError):
        batch(service, value)


@pytest.mark.parametrize("offset", [-1, 180, 181])
def test_date_window(service, offset):
    with pytest.raises(ServiceError):
        create_batch(service, "orders", day(offset), 0, None)


def test_date_window_boundary_and_push_today(service):
    assert create_batch(service, "orders", day(179), 0, None)["business_date"] == day(179)
    assert create_batch(service, "orders", day(0), 0, None)["business_date"] == day(0)
    with pytest.raises(ServiceError) as caught:
        create_batch(service, "orders", day(0), 0, None, mode="pull")
    assert caught.value.code == "DATE_NOT_FINISHED"


@pytest.mark.parametrize("mutation", [{"amount": "0.0000001"}, {"amount": 1.1}, {"amount": "1e32"},
                                      {"day": "2020-01-01"}, {"unknown": 1}, {"id": " "}])
def test_invalid_record_never_partially_stages(service, mutation):
    active = batch(service, 2)
    invalid = {**row("002"), **mutation}
    with pytest.raises(ServiceError):
        append_chunk(service, active["id"], 0, [row(), invalid])
    assert get_batch(service, active["id"])["row_count"] == 0
    assert published(service) == []


def test_long_business_key_hash_and_leading_zero(service):
    active = batch(service, 2)
    append_chunk(service, active["id"], 0, [row("0" * 9000), row("001")])
    commit_batch(service, active["id"])
    assert {len(record["_key"]) for record in published(service)} == {64}
    assert "001" in {record["id"] for record in published(service)}


def test_chunk_size_and_cumulative_cap(service):
    active = batch(service, None)
    with pytest.raises(ServiceError):
        append_chunk(service, active["id"], 0, [row()] * 1001)
    with connect(service) as connection:
        connection.execute("UPDATE batches SET row_count=1000000 WHERE id=%s", (active["id"],))
    with pytest.raises(ServiceError) as caught:
        append_chunk(service, active["id"], 0, [row()])
    assert caught.value.code == "BATCH_ROW_LIMIT"


def test_concurrent_same_day_and_same_chunk(service):
    def create():
        try:
            return batch(service)
        except ServiceError as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: create(), range(2)))
    assert sum(isinstance(result, dict) for result in outcomes) == 1
    active = next(result for result in outcomes if isinstance(result, dict))
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: append_chunk(service, active["id"], 0, [row()]), range(2)))
    assert all(result["row_count"] == 1 for result in outcomes)
    assert commit_batch(service, active["id"])["row_count"] == 1


def test_sync_range_all_or_nothing(service):
    create_batch(service, "orders", day(), 0, None)
    with pytest.raises(ServiceError):
        enqueue_sync(service, "orders", day(2), day())
    with connect(service) as connection:
        assert connection.execute("SELECT COUNT(*) AS n FROM batches").fetchone()["n"] == 1
    with pytest.raises(ServiceError) as caught:
        enqueue_sync(service, "missing", day(), day())
    assert caught.value.code == "SOURCE_NOT_CONFIGURED"


class Pages:
    def __init__(self, pages, error=None):
        self.pages = pages
        self.error = error

    def iter_pages(self, business_date):
        yield from self.pages
        if self.error:
            raise self.error


def test_worker_success_failure_preserves_previous_and_recovers(service):
    with worker_connection(service) as connection:
        enqueue_sync(service, "orders", day(), day())
        active = claim_next_job(connection)
        assert process_job(connection, service, active, Pages([[row()], []]))
        assert published(service)[0]["amount"] == Decimal("10.5")
        enqueue_sync(service, "orders", day(), day())
        failed = claim_next_job(connection)
        assert not process_job(connection, service, failed, Pages([[row("002")]], SourceError("UPSTREAM_TIMEOUT", "源超时")))
        assert get_batch(service, failed["id"])["status"] == "failed"
        assert published(service)[0]["id"] == "001"
        enqueue_sync(service, "orders", day(), day())
        interrupted = claim_next_job(connection)
        append_chunk(service, interrupted["id"], 0, [row("003")])
        assert recover_interrupted_jobs(connection) == 1
        assert get_batch(service, interrupted["id"])["error_code"] == "WORKER_INTERRUPTED"
        assert published(service)[0]["id"] == "001"
        assert recover_interrupted_jobs(connection) == 0


def test_worker_empty_page_required_duplicate_fails_and_next_day_continues(service):
    with worker_connection(service) as connection:
        enqueue_sync(service, "orders", day(3), day())
        no_end = claim_next_job(connection)
        assert not process_job(connection, service, no_end, Pages([[row(offset=3)]]))
        assert get_batch(service, no_end["id"])["error_code"] == "PAGINATION_INCOMPLETE"
        duplicate = claim_next_job(connection)
        assert not process_job(connection, service, duplicate, Pages([[row(offset=2)], [row(offset=2)], []]))
        assert get_batch(service, duplicate["id"])["error_code"] == "DUPLICATE_RECORD"
        successful = claim_next_job(connection)
        assert process_job(connection, service, successful, Pages([[]]))
        assert successful["business_date"] == day()


def test_worker_lock_exclusion_and_heartbeat(service):
    with connect(service) as connection:
        with pytest.raises(ValueError):
            acquire_worker_lock(connection)
    with worker_connection(service) as first, connect(service) as second:
        second.autocommit = True
        assert not acquire_worker_lock(second)
        with pytest.raises(RuntimeError):
            require_worker_lock(second)
        heartbeat(first)
        assert first.execute("SELECT status FROM worker_heartbeat WHERE worker_key='sync'").fetchone()["status"] == "idle"


def test_retention_and_abandoned_upload_cleanup(service):
    old = create_batch(service, "orders", day(179), 0, None)
    commit_batch(service, old["id"])
    active = batch(service)
    append_chunk(service, active["id"], 0, [row()])
    with connect(service) as connection:
        connection.execute("UPDATE batches SET updated_at=clock_timestamp()-interval '25 hours' WHERE id=%s", (active["id"],))
    with worker_connection(service) as connection:
        cleanup_history(connection, service, today=day(-1))
    assert get_batch(service, active["id"])["status"] == "failed"
    with pytest.raises(ServiceError) as caught:
        get_batch(service, old["id"])
    assert caught.value.status == 404
    assert published(service) == []


def test_schedule_disabled_enabled_and_idempotent(service):
    sources = {"orders": SourceConfig(dataset_slug="orders", url="https://source.test/rows")}
    now = datetime.combine(day(0), datetime.min.time(), BEIJING).replace(hour=4)
    with worker_connection(service) as connection:
        assert enqueue_schedule(connection, service, sources, now) == 0
        enabled = service.model_copy(update={"schedule_enabled": True, "refresh_days": 3})
        assert enqueue_schedule(connection, enabled, sources, now.replace(hour=2)) == 0
        assert enqueue_schedule(connection, enabled, sources, now) == 3
        assert enqueue_schedule(connection, enabled, sources, now) == 0


def test_http_mapping_and_date_conversion_to_typed_publish(service):
    requests = []

    def response(request):
        requests.append(request)
        payload = json.loads(request.content)
        rows = [{"daySource": day().isoformat() + " 00:00:00", "key": "0001", "money": "12.50", "shop": "原名", "extra": 1}] if payload["pageIndex"] == 0 else []
        return httpx.Response(200, json={"data": rows})

    config = SourceConfig(dataset_slug="orders", url="https://source.test/rows", date_fields_from_datetime=["day"],
                          field_mapping={"day": "daySource", "id": "key", "amount": "money", "shop": "shop"})
    with worker_connection(service) as connection, SourceClient(config, service, transport=httpx.MockTransport(response)) as source:
        enqueue_sync(service, "orders", day(), day())
        assert process_job(connection, service, claim_next_job(connection), source)
    assert len(requests) == 2 and published(service)[0]["amount"] == Decimal("12.5")
    assert published(service)[0]["id"] == "0001"


@pytest.mark.parametrize("changes,error_code", [
    ({"date_fields_from_datetime": ["amount"]}, "DATE_CONVERSION_INVALID"),
    ({"field_mapping": {"id": "key"}}, "FIELD_MAPPING_INCOMPLETE"),
])
def test_invalid_source_conversion_or_mapping_stops_before_network(service, changes, error_code):
    calls = []
    config = SourceConfig(dataset_slug="orders", url="https://source.test/rows", **changes)
    with worker_connection(service) as connection, SourceClient(config, service,
            transport=httpx.MockTransport(lambda request: calls.append(request))) as source:
        enqueue_sync(service, "orders", day(), day())
        active = claim_next_job(connection)
        assert not process_job(connection, service, active, source)
        assert get_batch(service, active["id"])["error_code"] == error_code
    assert calls == []


# 多次同步采用批次快照合并，输入计数与最终发布计数必须分别保持真实含义。
def test_upsert_updates_inserts_and_preserves_unreturned(service):
    first = batch(service, 2)
    append_chunk(service, first["id"], 0, [row("001", "10"), row("002", "20")])
    commit_batch(service, first["id"])
    second = batch(service, 2, write_mode="upsert")
    append_chunk(service, second["id"], 0, [row("001", "15.000001"), row("003", "30")])
    assert [r["id"] for r in published(service)] == ["001", "002"]
    result = commit_batch(service, second["id"])
    assert result["row_count"] == 2 and result["published_row_count"] == 3
    assert {r["id"]: r["amount"] for r in published(service)} == {
        "001": Decimal("15.000001"), "002": Decimal("20"), "003": Decimal("30")}
    assert commit_batch(service, second["id"]) == result
    with connect(service) as connection:
        assert connection.execute("SELECT row_count FROM published_days").fetchone()["row_count"] == 3


def test_upsert_whole_row_null_and_empty_source(service):
    original = batch(service, 2)
    append_chunk(service, original["id"], 0, [row("001"), row("002")])
    commit_batch(service, original["id"])
    updated = batch(service, 1, write_mode="upsert")
    append_chunk(service, updated["id"], 0, [{**row("001", None), "shop": None}])
    commit_batch(service, updated["id"])
    current = published(service)
    assert current[0]["amount"] is None and current[0]["shop"] is None
    empty = batch(service, 0, write_mode="upsert")
    append_chunk(service, empty["id"], 0, [])
    result = commit_batch(service, empty["id"])
    assert result["row_count"] == 0 and result["published_row_count"] == 2
    assert [(r["id"], r["amount"], r["shop"]) for r in published(service)] == [
        (r["id"], r["amount"], r["shop"]) for r in current]


def test_upsert_first_empty_and_different_business_days(service):
    empty = batch(service, 0, write_mode="upsert")
    assert commit_batch(service, empty["id"])["published_row_count"] == 0
    other = create_batch(service, "orders", day(2), 1, None)
    append_chunk(service, other["id"], 0, [row("001", "99", offset=2)])
    commit_batch(service, other["id"])
    today_batch = batch(service, 1, write_mode="upsert")
    append_chunk(service, today_batch["id"], 0, [row("001", "11")])
    commit_batch(service, today_batch["id"])
    assert {(r["day"], r["amount"]) for r in published(service)} == {
        (day(2), Decimal("99")), (day(), Decimal("11"))}


def test_upsert_merge_limit_rolls_back_without_truncating(service, monkeypatch):
    import data_service.ingest as ingest_module
    original = batch(service, 2)
    append_chunk(service, original["id"], 0, [row("001"), row("002")])
    commit_batch(service, original["id"])
    updated = batch(service, 2, write_mode="upsert")
    append_chunk(service, updated["id"], 0, [row("003"), row("004")])
    monkeypatch.setattr(ingest_module, "MAX_BATCH_ROWS", 3)
    with pytest.raises(ServiceError) as caught:
        commit_batch(service, updated["id"])
    assert caught.value.code == "PUBLISHED_ROW_LIMIT"
    assert [r["id"] for r in published(service)] == ["001", "002"]
    current = get_batch(service, updated["id"])
    assert current["status"] == "uploading" and current["published_row_count"] is None
    with connect(service) as connection:
        table = connection.execute("SELECT table_name FROM datasets WHERE slug='orders'").fetchone()["table_name"]
        assert connection.execute(sql.SQL("SELECT COUNT(*) AS n FROM {} WHERE _batch_id=%s").format(sql.Identifier(table)),
                                  (updated["id"],)).fetchone()["n"] == 2
    monkeypatch.setattr(ingest_module, "MAX_BATCH_ROWS", 1000000)
    assert commit_batch(service, updated["id"])["published_row_count"] == 4


def test_upsert_duplicate_pages_remain_rejected(service):
    original = batch(service, 1)
    append_chunk(service, original["id"], 0, [row("001", "99")])
    commit_batch(service, original["id"])
    updated = batch(service, 2, write_mode="upsert")
    append_chunk(service, updated["id"], 0, [row("001", "11")])
    with pytest.raises(ServiceError) as caught:
        append_chunk(service, updated["id"], 1, [row("001", "22")])
    assert caught.value.code == "DUPLICATE_RECORD"
    cancel_batch(service, updated["id"])
    assert published(service)[0]["amount"] == Decimal("99")


def test_write_mode_is_part_of_batch_idempotency(service):
    active = create_batch(service, "orders", day(), 0, "mode-key", write_mode="upsert")
    assert create_batch(service, "orders", day(), 0, "mode-key", write_mode="upsert")["id"] == active["id"]
    with pytest.raises(ServiceError) as caught:
        create_batch(service, "orders", day(), 0, "mode-key", write_mode="replace_day")
    assert caught.value.code == "IDEMPOTENCY_CONFLICT"


@pytest.mark.parametrize("invalid", [None, "merge", True, 1, []])
def test_invalid_write_mode_rejected(service, invalid):
    with pytest.raises(ServiceError) as caught:
        batch(service, 0, write_mode=invalid)
    assert caught.value.code == "INVALID_WRITE_MODE"


@pytest.mark.parametrize("target", ["amount", "missing", "shop"])
def test_worker_text_conversion_checks_target_and_preserves_publication(service, target):
    original = batch(service)
    append_chunk(service, original["id"], 0, [row()])
    commit_batch(service, original["id"])
    config = SourceConfig(dataset_slug="orders", url="https://source.test/rows", text_whitespace_fields=[target])
    upstream = row("new")
    upstream["shop"] = "店\r\n铺\tA"
    def respond(request):
        page_index = json.loads(request.content)["pageIndex"]
        return httpx.Response(200, json={"data": [upstream] if page_index == 0 else []})
    with worker_connection(service) as connection, SourceClient(config, service, transport=httpx.MockTransport(respond)) as source:
        enqueue_sync(service, "orders", day(), day())
        active = claim_next_job(connection)
        assert process_job(connection, service, active, source) is (target == "shop")
    if target == "shop":
        assert published(service)[0]["shop"] == "店 铺 A"
    else:
        assert get_batch(service, active["id"])["error_code"] == "TEXT_CONVERSION_INVALID"
        assert published(service)[0]["id"] == "001"
