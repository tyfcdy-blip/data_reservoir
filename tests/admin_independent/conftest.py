"""管理员配置后台独立验收：只允许隔离数据库，不触达业务库或真实来源。"""

from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient
import pytest

from data_service import auth, db
from data_service.config import Settings


@pytest.fixture
def clean_settings(tmp_path):
    env_file = Path(__file__).resolve().parents[2] / ".local" / "test-admin-independent.env"
    settings = Settings(_env_file=env_file, _env_file_encoding="utf-8")
    assert urlsplit(settings.database_url).path == "/admin_independent_test", "拒绝连接非独立测试库"
    source_file = tmp_path / "sources.json"
    source_file.write_text('{"sources":[]}', encoding="utf-8")
    settings = settings.model_copy(update={
        "admin_username": "independent_admin",
        "admin_password": "independent-admin-local-test-only-2026!",
        "source_config_path": str(source_file),
        "allowed_source_hosts": [],
        "schedule_enabled": False,
    })
    with db.connect(settings) as connection:
        connection.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
    return settings


@pytest.fixture
def admin_service(clean_settings):
    from data_service.api import create_app

    db.migrate(clean_settings)
    auth.initialize_admin(clean_settings)
    with TestClient(create_app(clean_settings), raise_server_exceptions=False) as client:
        response = client.post("/api/v1/auth/login", json={
            "username": clean_settings.admin_username, "password": clean_settings.admin_password,
        })
        assert response.status_code == 200, response.text
        yield clean_settings, client, {"Authorization": "Bearer " + response.json()["data"]["token"]}


@pytest.fixture
def business_day():
    return (datetime.now(ZoneInfo("Asia/Shanghai")).date() - timedelta(days=1)).isoformat()


def register(client, administrator, slug="ind_sales"):
    schema = {"slug": slug, "name": "独立验收商品日表", "date_field": "dt", "key_fields": ["id"], "fields": [
        {"name": "dt", "type": "date", "required": True},
        {"name": "id", "type": "string", "required": True},
        {"name": "shop", "type": "string", "dimension": True},
        {"name": "amount", "type": "decimal"},
        {"name": "units", "type": "integer"},
    ]}
    response = client.post("/api/v1/admin/datasets", headers=administrator, json=schema)
    assert response.status_code == 201, response.text
    return schema


def publish(client, administrator, day, rows, slug="ind_sales"):
    response = client.post(f"/api/v1/datasets/{slug}/batches", headers=administrator, json={
        "business_date": day, "expected_rows": len(rows), "idempotency_key": "independent-publish-" + day,
    })
    assert response.status_code == 201, response.text
    identifier = response.json()["data"]["id"]
    if rows:
        response = client.put(f"/api/v1/batches/{identifier}/chunks/0", headers=administrator, json={"rows": rows})
        assert response.status_code == 200, response.text
    response = client.post(f"/api/v1/batches/{identifier}/commit", headers=administrator)
    assert response.status_code == 200, response.text


def machine_key(client, administrator, grants):
    response = client.post("/api/v1/admin/keys", headers=administrator, json={
        "name": "独立测试消费方", "grants": grants, "requests_per_minute": 600,
    })
    assert response.status_code == 201, response.text
    return {"X-API-Key": response.json()["data"]["api_key"]}


def source_body(slug="ind_sales", **overrides):
    body = {
        "title": "独立测试来源", "config": {
            "dataset_slug": slug, "url": "http://127.0.0.1:18991/rows", "method": "POST",
            "parameter_location": "json", "page_start": 0, "page_size": 2,
            "field_mapping": {"dt": "dt", "id": "id", "shop": "shop", "amount": "amount", "units": "units"},
        }, "secrets": {}, "enabled": True, "approve_host": True,
        "schedule_enabled": False, "schedule_hour": 3, "refresh_days": 7, "revision": 0,
    }
    body.update(overrides)
    return body


def service_body(**overrides):
    body = {
        "name": "独立验收店铺汇总", "dataset_slug": "ind_sales", "definition": {
            "fields": None, "filters": [{"field": "shop", "op": "eq", "value": "A"}],
            "group_by": ["shop"], "aggregates": [
                {"field": "amount", "op": "sum", "alias": "sales"},
                {"field": "id", "op": "count_distinct", "alias": "products"},
            ], "order_by": [{"field": "sales", "direction": "desc"}], "require_complete": True,
        }, "allowed_filter_fields": ["shop"], "max_limit": 20, "enabled": True, "revision": 0,
    }
    body.update(overrides)
    return body
