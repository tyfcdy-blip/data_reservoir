"""管理来源的数据库、加密、协议边界与动态运行测试；仅合成源。"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import os
from urllib.parse import urlsplit

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
import httpx
from psycopg import sql
import pytest
from pydantic import ValidationError

from data_service.config import Settings
from data_service import auth
from data_service.api import create_app
from data_service.datasets import register_dataset
from data_service.db import connect, migrate
from data_service.errors import ServiceError
from data_service.ingest import BEIJING, create_batch, enqueue_sync
from data_service.models import DatasetDefinition
from data_service.source import SourceClient, SourceError, load_sources
from data_service.source_registry import (
    SourceSave, get_source, list_sources, load_active_sources, save_source, test_source as check_source,
)
from data_service.worker import acquire_worker_lock, enqueue_schedule, refresh_source_clients


@pytest.fixture(scope="module")
def configured_settings():
    dsn = os.environ["DS_TEST_DATABASE_URL"]
    if urlsplit(dsn).path.rsplit("/", 1)[-1] != "admin_sources_test":
        raise RuntimeError("仅允许独立管理员来源测试库")
    settings = Settings(database_url=dsn, config_encryption_key=Fernet.generate_key().decode(),
                        admin_password="admin-source-test-only-2026!")
    with connect(settings) as connection:
        connection.execute("DROP SCHEMA public CASCADE")
        connection.execute("CREATE SCHEMA public")
    migrate(settings)
    auth.initialize_admin(settings)
    return settings


@pytest.fixture
def service(configured_settings, tmp_path):
    with connect(configured_settings) as connection:
        for record in connection.execute("SELECT table_name FROM datasets").fetchall():
            connection.execute(sql.SQL("DROP TABLE {} CASCADE").format(sql.Identifier(record["table_name"])))
        connection.execute("TRUNCATE datasets,api_keys,worker_heartbeat CASCADE")
    path = tmp_path / "sources.json"
    path.write_text('{"sources":[]}', encoding="utf-8")
    settings = configured_settings.model_copy(update={"source_config_path": str(path)})
    register_dataset(settings, DatasetDefinition(slug="orders", name="合成订单", date_field="day", key_fields=["id"], fields=[
        {"name": "day", "type": "date", "required": True},
        {"name": "id", "type": "string", "required": True},
        {"name": "amount", "type": "decimal"},
    ]))
    return settings


def payload(**changes):
    return SourceSave.model_validate({"title": "合成来源", "config": {"dataset_slug": "orders", "url": "http://source.test/rows"},
                                     "approve_host": True, **changes})


def yesterday():
    return datetime.now(BEIJING).date() - timedelta(days=1)


def test_roundtrip_encrypted_and_masked_without_network(service, monkeypatch):
    monkeypatch.setattr(httpx.Client, "request", lambda *args, **kwargs: pytest.fail("保存不能请求来源"))
    incoming = payload(config={"dataset_slug": "orders", "url": "http://source.test/rows", "header_env": {"Authorization": "SOURCE_AUTH"}},
                       secrets={"SOURCE_AUTH": "Bearer test-secret"})
    saved = save_source(service, "orders", incoming)
    assert saved["revision"] == 1 and saved["secret_names"] == ["SOURCE_AUTH"]
    assert "test-secret" not in json.dumps(saved, default=str)
    with connect(service) as connection:
        record = connection.execute("SELECT * FROM managed_sources").fetchone()
    assert "test-secret" not in record["encrypted_secrets"]
    assert "test-secret" not in json.dumps(record["definition"])
    assert json.loads(Fernet(service.config_encryption_key).decrypt(record["encrypted_secrets"])) == incoming.secrets
    assert get_source(service, "orders") == list_sources(service)[0]


def test_preserve_secret_rotate_and_drop_unused(service):
    config = {"dataset_slug": "orders", "url": "http://source.test/rows", "header_env": {"Authorization": "AUTH"}}
    save_source(service, "orders", payload(config=config, secrets={"AUTH": "old"}))
    save_source(service, "orders", payload(config=config, revision=1, approve_host=False))
    assert load_active_sources(service)["orders"]._managed_secrets == {"AUTH": "old"}
    save_source(service, "orders", payload(config=config, revision=2, secrets={"AUTH": "new"}))
    assert load_active_sources(service)["orders"]._managed_secrets == {"AUTH": "new"}
    saved = save_source(service, "orders", payload(revision=3))
    assert saved["secret_names"] == []
    with connect(service) as connection:
        assert connection.execute("SELECT encrypted_secrets FROM managed_sources").fetchone()["encrypted_secrets"] is None


@pytest.mark.parametrize("changes,code", [
    ({"approve_host": False}, "SOURCE_HOST_APPROVAL_REQUIRED"),
    ({"revision": 1}, "SOURCE_REVISION_CONFLICT"),
    ({"config": {"dataset_slug": "other", "url": "http://source.test/rows"}}, "SOURCE_DATASET_MISMATCH"),
    ({"config": {"dataset_slug": "orders", "url": "http://source.test/rows", "field_mapping": {"day": "dt"}}}, "FIELD_MAPPING_INCOMPLETE"),
    ({"config": {"dataset_slug": "orders", "url": "http://source.test/rows", "date_fields_from_datetime": ["amount"]}}, "DATE_CONVERSION_INVALID"),
    ({"secrets": {"UNUSED": "secret"}}, "SOURCE_SECRET_UNUSED"),
    ({"config": {"dataset_slug": "orders", "url": "http://source.test/rows", "header_env": {"Authorization": "AUTH"}}}, "SOURCE_SECRET_MISSING"),
])
def test_invalid_save_has_no_partial_configuration(service, changes, code):
    with pytest.raises(ServiceError) as caught:
        save_source(service, "orders", payload(**changes))
    assert caught.value.code == code
    assert list_sources(service) == []


def test_managed_config_cannot_reference_process_secret(service, monkeypatch):
    monkeypatch.setenv("DS_ADMIN_PASSWORD", "process-password-not-a-source-secret")
    with pytest.raises(ServiceError) as caught:
        save_source(service, "orders", payload(config={"dataset_slug": "orders", "url": "http://source.test/rows",
                                                       "header_env": {"Authorization": "DS_ADMIN_PASSWORD"}}))
    assert caught.value.code == "SOURCE_SECRET_MISSING"


def test_new_host_requires_new_approval(service):
    save_source(service, "orders", payload())
    with pytest.raises(ServiceError) as caught:
        save_source(service, "orders", payload(revision=1, approve_host=False,
                                               config={"dataset_slug": "orders", "url": "http://other.test/rows"}))
    assert caught.value.code == "SOURCE_HOST_APPROVAL_REQUIRED"


def test_optimistic_concurrency_has_one_winner(service):
    save_source(service, "orders", payload())
    def update(title):
        try:
            return save_source(service, "orders", payload(revision=1, title=title))["revision"]
        except ServiceError as error:
            return error.code
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(update, ["更新甲", "更新乙"]))
    assert sorted(map(str, results)) == ["2", "SOURCE_REVISION_CONFLICT"]


def test_missing_encryption_key_and_wrong_key_fail_closed(service):
    config = {"dataset_slug": "orders", "url": "http://source.test/rows", "header_env": {"Authorization": "AUTH"}}
    with pytest.raises(ServiceError) as caught:
        save_source(service.model_copy(update={"config_encryption_key": None}), "orders", payload(config=config, secrets={"AUTH": "secret"}))
    assert caught.value.code == "SOURCE_ENCRYPTION_UNAVAILABLE"
    save_source(service, "orders", payload(config=config, secrets={"AUTH": "secret"}))
    with pytest.raises(ServiceError) as caught:
        load_active_sources(service.model_copy(update={"config_encryption_key": Fernet.generate_key().decode()}))
    assert caught.value.code == "SOURCE_SECRET_DECRYPT_FAILED"


def test_active_loader_file_compatibility_and_disabled_shadow(service):
    from pathlib import Path
    Path(service.source_config_path).write_text(json.dumps({"sources": [{"dataset_slug": "orders", "url": "http://source.test/file"}]}))
    allowed = service.model_copy(update={"allowed_source_hosts": ["source.test"]})
    assert load_sources(allowed)["orders"].url.endswith("/file")
    assert list_sources(allowed)[0]["origin"] == "file"
    save_source(allowed, "orders", payload(enabled=False))
    assert load_active_sources(allowed) == {}
    assert list_sources(allowed)[0]["origin"] == "managed"
    with pytest.raises(ServiceError) as caught:
        enqueue_sync(allowed, "orders", yesterday(), yesterday())
    assert caught.value.code == "SOURCE_NOT_CONFIGURED"


def test_managed_client_approved_host_and_secret_isolation(service, monkeypatch):
    config = {"dataset_slug": "orders", "url": "http://source.test/rows", "header_env": {"Authorization": "AUTH"}}
    save_source(service, "orders", payload(config=config, secrets={"AUTH": "managed"}))
    monkeypatch.setenv("AUTH", "process")
    observed = []
    def upstream(request):
        observed.append(request.headers["Authorization"])
        return httpx.Response(200, json={"data": []})
    with SourceClient(load_active_sources(service)["orders"], service, transport=httpx.MockTransport(upstream)) as client:
        assert client.query_page(yesterday(), 0) == []
    assert observed == ["managed"] and os.environ["AUTH"] == "process"


def test_single_page_connection_test_validates_records_without_publish(service):
    save_source(service, "orders", payload())
    observed = []
    def upstream(request):
        observed.append(json.loads(request.content))
        return httpx.Response(200, json={"data": [{"day": yesterday().isoformat(), "id": "001", "amount": "1.20"}]})
    result = check_source(service, "orders", yesterday(), transport=httpx.MockTransport(upstream))
    assert result["row_count"] == 1 and result["columns"] == ["day", "id", "amount"]
    assert "rows" not in result and len(observed) == 1
    with connect(service) as connection:
        assert connection.execute("SELECT COUNT(*) AS n FROM batches").fetchone()["n"] == 0


def test_connection_test_rejects_wrong_record_type(service):
    save_source(service, "orders", payload())
    upstream = httpx.MockTransport(lambda request: httpx.Response(200, json={"data": [{"day": yesterday().isoformat(), "id": "001", "amount": "bad"}]}))
    with pytest.raises(ServiceError) as caught:
        check_source(service, "orders", yesterday(), transport=upstream)
    assert caught.value.code == "invalid_record"


def test_connection_test_upstream_error_has_no_secret(service):
    save_source(service, "orders", payload())
    with pytest.raises(SourceError) as caught:
        check_source(service, "orders", yesterday(), transport=httpx.MockTransport(lambda request: httpx.Response(400, text="private upstream text")))
    assert "private" not in caught.value.message


def test_schedule_uses_source_config_and_is_idempotent(service):
    save_source(service, "orders", payload(schedule_enabled=True, schedule_hour=6, refresh_days=2))
    sources = load_active_sources(service)
    with connect(service) as connection:
        connection.autocommit = True
        assert acquire_worker_lock(connection)
        now = datetime.now(BEIJING).replace(hour=5)
        assert enqueue_schedule(connection, service, sources, now) == 0
        assert enqueue_schedule(connection, service, sources, now.replace(hour=6)) == 2
        assert enqueue_schedule(connection, service, sources, now.replace(hour=7)) == 0
    save_source(service, "orders", payload(revision=1, schedule_enabled=False))
    with connect(service) as connection:
        connection.autocommit = True
        assert acquire_worker_lock(connection)
        assert enqueue_schedule(connection, service.model_copy(update={"schedule_enabled": True}), load_active_sources(service), now.replace(hour=7)) == 0


def test_reload_reuses_unchanged_clients_and_closes_changed_disabled(service):
    save_source(service, "orders", payload())
    clients = {}
    class Client:
        def __init__(self, config, settings, **kwargs):
            self.config, self.closed = config, False
        def close(self):
            self.closed = True
    refresh_source_clients(service, clients, lambda: None, client_factory=Client)
    first = clients["orders"]
    refresh_source_clients(service, clients, lambda: None, client_factory=Client)
    assert clients["orders"] is first
    save_source(service, "orders", payload(revision=1, title="新版"))
    refresh_source_clients(service, clients, lambda: None, client_factory=Client)
    assert first.closed and clients["orders"] is not first
    second = clients["orders"]
    save_source(service, "orders", payload(revision=2, enabled=False))
    refresh_source_clients(service, clients, lambda: None, client_factory=Client)
    assert second.closed and clients == {}


@pytest.mark.parametrize("change", [{"revision": True}, {"schedule_hour": 24}, {"refresh_days": 0}, {"enabled": "yes"},
                                    {"secrets": {"AUTH": "\r\ninjected"}}, {"secrets": {"AUTH": ""}}, {"title": "   "},
                                    {"secrets": {"AUTH": "\ud800"}}, {"secrets": {"AUTH": "\udfff"}},
                                    {"title": "bad\ud800"}, {"title": "bad\udfff"}])
def test_form_validation(change):
    with pytest.raises(ValidationError):
        payload(**change)


def test_source_routes_enforce_admin_and_revision(service):
    with TestClient(create_app(service)) as client:
        response = client.post("/api/v1/auth/login", json={"username": service.admin_username, "password": service.admin_password})
        assert response.status_code == 200
        headers = {"Authorization": "Bearer " + response.json()["data"]["token"]}
        request = payload().model_dump(mode="json", exclude_none=True)
        assert client.put("/api/v1/admin/sources/orders", json=request).status_code == 401
        key = client.post("/api/v1/admin/keys", headers=headers, json={"name": "调用方", "grants": {"orders": ["read", "write"]}})
        assert key.status_code == 201
        machine = {"X-API-Key": key.json()["data"]["api_key"]}
        assert client.get("/api/v1/admin/sources", headers=machine).status_code == 401
        assert client.put("/api/v1/admin/sources/orders", headers=machine, json=request).status_code == 401
        saved = client.put("/api/v1/admin/sources/orders", headers=headers, json=request)
        assert saved.status_code == 200 and saved.json()["data"]["revision"] == 1
        assert client.put("/api/v1/admin/sources/orders", headers=headers, json=request).status_code == 409
        assert client.get("/api/v1/admin/sources/orders", headers=headers).json()["data"]["revision"] == 1
        assert len(client.get("/api/v1/admin/sources", headers=headers).json()["data"]) == 1
        assert client.get("/api/v1/admin/sources/missing", headers=headers).status_code == 404
        assert client.post("/api/v1/admin/sources/orders/test", headers=headers, json={"business_date": "2026-10-01T00:00:00"}).status_code == 422
        assert client.post("/api/v1/admin/sources/orders/test", headers=headers, json={"business_date": 1}).status_code == 422
        assert client.get("/api/v1/admin/status", headers=headers).json()["data"]["configured_sources"] == ["orders"]


def test_source_route_invalid_secret_does_not_echo(service):
    with TestClient(create_app(service)) as client:
        response = client.post("/api/v1/auth/login", json={"username": service.admin_username, "password": service.admin_password})
        headers = {"Authorization": "Bearer " + response.json()["data"]["token"]}
        request = payload().model_dump(mode="json", exclude_none=True)
        request["secrets"] = {"AUTH": "sensitive\r\nvalue"}
        rejected = client.put("/api/v1/admin/sources/orders", headers=headers, json=request)
        assert rejected.status_code == 422 and "sensitive" not in rejected.text


def test_header_secret_non_ascii_rejected_before_save(service):
    config = {"dataset_slug": "orders", "url": "http://source.test/rows", "header_env": {"Authorization": "AUTH"}}
    with pytest.raises(ServiceError) as caught:
        save_source(service, "orders", payload(config=config, secrets={"AUTH": "Bearer 中文"}))
    assert caught.value.code == "SOURCE_HEADER_SECRET_INVALID" and list_sources(service) == []


def test_file_header_secret_non_ascii_has_safe_source_error(service, monkeypatch):
    from data_service.source import SourceConfig
    monkeypatch.setenv("AUTH", "Bearer 中文")
    config = SourceConfig(dataset_slug="orders", url="http://source.test/rows", header_env={"Authorization": "AUTH"})
    def upstream(request):
        pytest.fail("非法凭据不能进入网络请求")
    with SourceClient(config, service.model_copy(update={"allowed_source_hosts": ["source.test"]}),
                      transport=httpx.MockTransport(upstream)) as client:
        with pytest.raises(SourceError) as caught:
            client.query_page(yesterday(), 0)
    assert caught.value.code == "SOURCE_HEADER_SECRET_INVALID"


@pytest.mark.parametrize("changes", [
    {"url": "http://source.test/\ud800"},
    {"static_params": {"filter": "bad\u0000value"}},
    {"static_params": {"filter": "bad\udfffvalue"}},
    {"static_params": {"filter": float("inf")}},
    {"static_params": {"filter": float("-inf")}},
    {"static_params": {"filter": float("nan")}},
    {"protocol": "vendor_batch", "vendor_api_id": "api\ud800", "vendor_app_key_env": "APP_KEY", "vendor_app_secret_env": "APP_SECRET"},
])
def test_persistent_source_config_rejects_non_json_values(changes):
    with pytest.raises(ValidationError):
        payload(config={"dataset_slug": "orders", "url": "http://source.test/rows", **changes})


def test_schedule_fields_roundtrip_and_legacy_hour(service):
    saved = save_source(service, "orders", payload(schedule_times=["18:05", "03:15", "09:45"], write_mode="upsert"))
    assert saved["schedule_times"] == ["03:15", "09:45", "18:05"] and saved["schedule_hour"] == 3
    assert saved["write_mode"] == "upsert"
    config = load_active_sources(service)["orders"]
    assert config._schedule["schedule_times"] == saved["schedule_times"]
    assert config._schedule["write_mode"] == "upsert"
    assert "_schedule" not in config.model_dump()
    preserved = save_source(service, "orders", payload(revision=1, title="只修改名称"))
    assert preserved["schedule_times"] == saved["schedule_times"] and preserved["write_mode"] == "upsert"
    legacy = save_source(service, "orders", payload(revision=2, schedule_hour=8))
    assert legacy["schedule_times"] == ["08:00"] and legacy["write_mode"] == "upsert"
    replaced = save_source(service, "orders", payload(revision=3, write_mode="replace_day"))
    assert replaced["schedule_times"] == ["08:00"] and replaced["write_mode"] == "replace_day"


def test_new_source_keeps_replace_day_default_and_file_schedule(service):
    from pathlib import Path
    Path(service.source_config_path).write_text(json.dumps({"sources": [{"dataset_slug": "orders", "url": "http://source.test/file"}]}))
    allowed = service.model_copy(update={"allowed_source_hosts": ["source.test"], "schedule_hour": 8})
    file_record = get_source(allowed, "orders")
    assert file_record["write_mode"] == "replace_day" and file_record["schedule_times"] == ["08:00"]
    saved = save_source(service, "orders", payload())
    assert saved["write_mode"] == "replace_day" and saved["schedule_times"] == ["03:00"]


def test_multiple_times_minute_boundaries_and_failed_slot_no_repeat(service):
    save_source(service, "orders", payload(schedule_enabled=True, schedule_times=["03:15", "09:45", "18:05"],
                                           write_mode="upsert", refresh_days=2))
    sources = load_active_sources(service)
    now = datetime.now(BEIJING).replace(hour=3, minute=14, second=59, microsecond=0)
    with connect(service) as connection:
        connection.autocommit = True
        assert acquire_worker_lock(connection)
        assert enqueue_schedule(connection, service, sources, now) == 0
        assert enqueue_schedule(connection, service, sources, now.replace(minute=15)) == 2
        first = connection.execute("SELECT * FROM batches ORDER BY business_date").fetchall()
        assert all(batch["write_mode"] == "upsert" and ":0315:" in batch["idempotency_key"] for batch in first)
        assert all(batch["business_date"] < now.date() for batch in first)
        later = now.replace(hour=9, minute=45)
        assert enqueue_schedule(connection, service, sources, later) == 0
        connection.execute("UPDATE batches SET status='failed',error_code='TEST_FAILURE' WHERE status='pending'")
        assert enqueue_schedule(connection, service, sources, later) == 2
        assert enqueue_schedule(connection, service, sources, later) == 0
        connection.execute("UPDATE batches SET status='failed',error_code='TEST_FAILURE' WHERE status='pending'")
        assert enqueue_schedule(connection, service, sources, later.replace(hour=18, minute=4)) == 0
        assert enqueue_schedule(connection, service, sources, later.replace(hour=18, minute=5)) == 2
        assert connection.execute("SELECT count(*) AS n FROM batches").fetchone()["n"] == 6
    # 重启后新锁连接仍依赖持久化幂等记录，不会重复建立任务。
    with connect(service) as connection:
        connection.autocommit = True
        assert acquire_worker_lock(connection)
        assert enqueue_schedule(connection, service, load_active_sources(service), later.replace(hour=23)) == 0


def test_past_slots_catch_up_serially_and_use_beijing_timezone(service):
    save_source(service, "orders", payload(schedule_enabled=True, schedule_times=["01:10", "02:20", "03:30"], refresh_days=1))
    sources = load_active_sources(service)
    local_now = datetime.now(BEIJING).replace(hour=4, minute=0, second=0, microsecond=0)
    with connect(service) as connection:
        connection.autocommit = True
        assert acquire_worker_lock(connection)
        for expected in ("0110", "0220", "0330"):
            assert enqueue_schedule(connection, service, sources, local_now.astimezone(timezone.utc)) == 1
            active = connection.execute("SELECT * FROM batches WHERE status='pending'").fetchone()
            assert f":{expected}:" in active["idempotency_key"]
            assert active["business_date"] == local_now.date() - timedelta(days=1)
            assert active["write_mode"] == "replace_day"
            connection.execute("UPDATE batches SET status='cancelled' WHERE id=%s", (active["id"],))
        assert enqueue_schedule(connection, service, sources, local_now) == 0
        assert connection.execute("SELECT count(*) AS n FROM batches").fetchone()["n"] == 3


@pytest.mark.parametrize("legacy_status", ["failed", "cancelled", "pending"])
def test_first_slot_recognizes_legacy_idempotency_key(service, legacy_status):
    save_source(service, "orders", payload(schedule_enabled=True, schedule_times=["03:00", "06:30"], refresh_days=1))
    now = datetime.now(BEIJING).replace(hour=7, minute=0, second=0, microsecond=0)
    legacy_key = f"schedule:{now.date().isoformat()}:{yesterday().isoformat()}"
    previous = create_batch(service, "orders", yesterday(), None, legacy_key, mode="pull")
    sources = load_active_sources(service)
    with connect(service) as connection:
        connection.autocommit = True
        assert acquire_worker_lock(connection)
        connection.execute("UPDATE batches SET status=%s WHERE id=%s", (legacy_status, previous["id"]))
        assert enqueue_schedule(connection, service, sources, now) == (0 if legacy_status == "pending" else 1)
        if legacy_status == "pending":
            connection.execute("UPDATE batches SET status='failed' WHERE id=%s", (previous["id"],))
            assert enqueue_schedule(connection, service, sources, now) == 1
        keys = [record["idempotency_key"] for record in connection.execute("SELECT idempotency_key FROM batches").fetchall()]
        assert len(keys) == 2 and legacy_key in keys
        assert any(":0630:" in key for key in keys) and not any(":0300:" in key for key in keys)


def test_schedule_change_does_not_reinterpret_existing_batches(service):
    save_source(service, "orders", payload(schedule_enabled=True, schedule_times=["03:00", "06:00"],
                                           refresh_days=1, write_mode="upsert"))
    now = datetime.now(BEIJING).replace(hour=3, minute=0, second=0, microsecond=0)
    with connect(service) as connection:
        connection.autocommit = True
        assert acquire_worker_lock(connection)
        assert enqueue_schedule(connection, service, load_active_sources(service), now) == 1
        save_source(service, "orders", payload(revision=1, schedule_enabled=True, refresh_days=1, write_mode="replace_day"))
        assert enqueue_schedule(connection, service, load_active_sources(service), now) == 0
        first = connection.execute("SELECT * FROM batches").fetchone()
        assert first["write_mode"] == "upsert"
        connection.execute("UPDATE batches SET status='cancelled' WHERE id=%s", (first["id"],))
        assert enqueue_schedule(connection, service, load_active_sources(service), now.replace(hour=6)) == 1
        second = connection.execute("SELECT * FROM batches WHERE status='pending'").fetchone()
        assert second["write_mode"] == "replace_day"


def test_schedule_metadata_changes_refresh_source_client(service):
    save_source(service, "orders", payload())
    clients = {}
    class Client:
        def __init__(self, config, settings, **kwargs):
            self.config, self.closed = config, False
        def close(self):
            self.closed = True
    refresh_source_clients(service, clients, lambda: None, client_factory=Client)
    first = clients["orders"]
    save_source(service, "orders", payload(revision=1, schedule_times=["04:30", "15:45"], write_mode="upsert"))
    refresh_source_clients(service, clients, lambda: None, client_factory=Client)
    assert first.closed and clients["orders"] is not first
    assert clients["orders"].config._schedule["schedule_times"] == ["04:30", "15:45"]
    assert clients["orders"].config._schedule["write_mode"] == "upsert"


@pytest.mark.parametrize("field", ["schedule_times", "write_mode"])
def test_source_route_explicit_null_rejected_without_configuration_change(service, field):
    saved = save_source(service, "orders", payload(schedule_times=["03:15", "15:45"], write_mode="upsert"))
    with TestClient(create_app(service)) as client:
        login = client.post("/api/v1/auth/login", json={"username": service.admin_username, "password": service.admin_password})
        assert login.status_code == 200
        headers = {"Authorization": "Bearer " + login.json()["data"]["token"]}
        request = payload(revision=1).model_dump(mode="json", exclude_none=True)
        request[field] = None
        response = client.put("/api/v1/admin/sources/orders", headers=headers, json=request)
        assert response.status_code == 422
        current = client.get("/api/v1/admin/sources/orders", headers=headers).json()["data"]
        assert current["revision"] == saved["revision"]
        assert current["schedule_times"] == saved["schedule_times"]
        assert current["write_mode"] == saved["write_mode"]


@pytest.mark.parametrize("fields", [["amount"], ["day"], ["missing"]])
def test_text_conversion_mapping_rejects_non_string_targets(service, fields):
    with pytest.raises(ServiceError) as caught:
        save_source(service, "orders", payload(config={"dataset_slug": "orders", "url": "https://source.test/rows",
                                                     "text_whitespace_fields": fields}))
    assert caught.value.code == "TEXT_CONVERSION_INVALID"
    assert list_sources(service) == []


def test_text_conversion_configuration_persists_and_runtime_applies(service):
    saved = save_source(service, "orders", payload(config={"dataset_slug": "orders", "url": "https://source.test/rows",
                                                         "text_whitespace_fields": ["id"]}))
    assert saved["config"]["text_whitespace_fields"] == ["id"]
    config = load_active_sources(service)["orders"]
    with SourceClient(config, service, transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"data": [{"day": yesterday().isoformat(), "id": "甲\r\n乙", "amount": "1.2"}]}))) as client:
        assert client.query_page(yesterday(), 0)[0]["id"] == "甲 乙"
