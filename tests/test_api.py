"""WP1计划：独立协议/管理鉴权/机器授权/限流/撤销/分批API/无损金额/请求边界。

仅清理standalone_api_test独立库，真实PG与业务代码，不模拟鉴权/查询/导入。
"""

from datetime import datetime, timedelta
import json
import os
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient
import pytest

from data_service import auth, db
from data_service.api import create_app
from data_service.config import Settings


@pytest.fixture
def service(tmp_path):
    address = os.environ["DS_TEST_DATABASE_URL"]
    if urlsplit(address).path != "/standalone_api_test":
        pytest.fail("拒绝清理非本测试专用库")
    sources = tmp_path / "sources.json"
    sources.write_text('{"sources":[]}', encoding="utf-8")
    settings = Settings(database_url=address, admin_username="test_admin",
                        admin_password="test-only-password-standalone-2026!", source_config_path=str(sources))
    with db.connect(settings) as connection:
        connection.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
    db.migrate(settings)
    auth.initialize_admin(settings)
    with TestClient(create_app(settings), raise_server_exceptions=False) as client:
        response = client.post("/api/v1/auth/login", json={"username": settings.admin_username,
                                                         "password": settings.admin_password})
        assert response.status_code == 200, response.text
        admin = {"Authorization": "Bearer " + response.json()["data"]["token"]}
        yield settings, client, admin


def register(client, admin, slug="orders"):
    schema = {"slug": slug, "name": slug, "date_field": "day", "key_fields": ["id"], "fields": [
        {"name": "day", "type": "date", "required": True},
        {"name": "id", "type": "string", "required": True},
        {"name": "shop", "type": "string", "dimension": True},
        {"name": "amount", "type": "decimal"},
    ]}
    response = client.post("/api/v1/admin/datasets", headers=admin, json=schema)
    assert response.status_code == 201, response.text
    assert "table_name" not in response.json()["data"]
    return schema


def key(client, admin, grants, **kwargs):
    response = client.post("/api/v1/admin/keys", headers=admin, json={"name": "test-client", "grants": grants, **kwargs})
    assert response.status_code == 201, response.text
    payload = response.json()["data"]
    return {"X-API-Key": payload["api_key"]}, payload["id"]


def day():
    return (datetime.now(ZoneInfo("Asia/Shanghai")).date() - timedelta(days=1)).isoformat()


def query_body():
    return {"start_date": day(), "end_date": day(), "require_complete": False}


def test_standalone_health_docs_and_no_dashboard(service):
    _settings, client, _admin = service
    assert client.get("/health/ready").json() == {"status": "ready"}
    assert client.get("/").json()["data"]["dashboard_included"] is False
    assert client.get("/docs").status_code == 200
    schema = client.get("/openapi.json").json()
    assert "/api/v1/query/batch" in schema["paths"]
    assert client.get("/api/business/dashboard").status_code == 404
    assert client.get("/api/getRouters").status_code == 404


@pytest.mark.parametrize("path", ["/api/v1/admin/keys", "/api/v1/admin/status", "/api/v1/datasets"])
def test_anonymous_denied(service, path):
    assert service[1].get(path).status_code == 401


def test_key_scopes_and_secret_not_listed(service):
    settings, client, admin = service
    register(client, admin)
    register(client, admin, "inventory")
    headers, identifier = key(client, admin, {"orders": ["read"]})
    assert [item["slug"] for item in client.get("/api/v1/datasets", headers=headers).json()["data"]] == ["orders"]
    assert client.post("/api/v1/datasets/orders/query", headers=headers, json=query_body()).status_code == 200
    assert client.post("/api/v1/datasets/inventory/query", headers=headers, json=query_body()).status_code == 403
    assert client.get("/api/v1/admin/keys", headers=headers).status_code == 401
    assert client.post("/api/v1/datasets/orders/batches", headers=headers,
                       json={"business_date": day(), "expected_rows": 0, "idempotency_key": "denied"}).status_code == 403
    listing = client.get("/api/v1/admin/keys", headers=admin).text
    assert headers["X-API-Key"] not in listing and "token_hash" not in listing
    with db.connect(settings) as connection:
        stored = connection.execute("SELECT * FROM api_keys WHERE id=%s", (identifier,)).fetchone()
        assert stored["token_hash"] != headers["X-API-Key"]
    assert client.delete("/api/v1/admin/keys/" + identifier, headers=admin).status_code == 200
    assert client.get("/api/v1/datasets", headers=headers).status_code == 401


def test_key_expiration_and_ambiguous_credentials(service):
    settings, client, admin = service
    register(client, admin)
    headers, identifier = key(client, admin, {"orders": ["read"]})
    assert client.get("/api/v1/datasets", headers={**headers, **admin}).status_code == 400
    with db.connect(settings) as connection:
        connection.execute("UPDATE api_keys SET expires_at=clock_timestamp()-interval '1 second' WHERE id=%s", (identifier,))
    assert client.get("/api/v1/datasets", headers=headers).status_code == 401


def test_key_rate_limit_commits_counter(service):
    settings, client, admin = service
    register(client, admin)
    headers, identifier = key(client, admin, {"orders": ["read"]}, requests_per_minute=1)
    assert client.get("/api/v1/datasets", headers=headers).status_code == 200
    for _ in range(2):
        assert client.get("/api/v1/datasets", headers=headers).status_code == 429
    with db.connect(settings) as connection:
        assert connection.execute("SELECT attempts FROM api_key_limits WHERE key_id=%s", (identifier,)).fetchone()["attempts"] == 3


def test_login_logout_reset_and_idempotent_initialization(service):
    settings, client, admin = service
    assert auth.initialize_admin(settings) is False
    assert client.post("/api/v1/auth/login", json={"username": "bad", "password": "bad"}).status_code == 401
    auth.reset_password(settings)
    assert client.get("/api/v1/admin/status", headers=admin).status_code == 401
    response = client.post("/api/v1/auth/login", json={"username": settings.admin_username, "password": settings.admin_password})
    new_header = {"Authorization": "Bearer " + response.json()["data"]["token"]}
    assert client.post("/api/v1/auth/logout", headers=new_header).status_code == 200
    assert client.get("/api/v1/admin/status", headers=new_header).status_code == 401


def test_push_query_and_cross_owner_protection(service):
    _settings, client, admin = service
    register(client, admin)
    headers, _ = key(client, admin, {"orders": ["read", "write"]})
    other, _ = key(client, admin, {"orders": ["read", "write"]})
    payload = {"business_date": day(), "expected_rows": 2, "idempotency_key": "first"}
    created = client.post("/api/v1/datasets/orders/batches", headers=headers, json=payload)
    assert created.status_code == 201, created.text
    batch_id = created.json()["data"]["id"]
    path = "/api/v1/batches/" + batch_id
    assert client.get(path, headers=other).status_code == 403
    assert client.post(path + "/commit", headers=other).status_code == 403
    records = [{"day": day(), "id": "001", "shop": " 店 ", "amount": "9007199254740993.123456"},
               {"day": day(), "id": "002", "shop": None, "amount": None}]
    assert client.put(path + "/chunks/0", headers=headers, json={"rows": records}).status_code == 200
    before = client.post("/api/v1/datasets/orders/query", headers=headers, json=query_body()).json()["data"]
    assert before["rows"] == []
    assert client.post(path + "/commit", headers=headers).status_code == 200
    after = client.post("/api/v1/datasets/orders/query", headers=headers, json=query_body()).json()["data"]
    assert after["rows"][0]["amount"] == "9007199254740993.123456"
    assert after["rows"][0]["id"] == "001"
    assert after["rows"][1]["amount"] is None
    assert client.post(path + "/commit", headers=headers).status_code == 200


def test_batch_queries_scope_all_or_none(service):
    _settings, client, admin = service
    register(client, admin)
    register(client, admin, "inventory")
    headers, _ = key(client, admin, {"orders": ["read"]})
    request = {"queries": [{"dataset": "orders", "query": query_body()},
                           {"dataset": "inventory", "query": query_body()}]}
    result = client.post("/api/v1/query/batch", headers=headers, json=request)
    assert result.status_code == 403 and "data" not in result.json()
    assert client.post("/api/v1/query/batch", headers=admin, json=request).status_code == 200


@pytest.mark.parametrize("raw", ['{"username":"a","username":"b","password":"SECRET"}',
                                 '{"password":NaN}', '{"password":', '[1,2]'])
def test_invalid_json_has_no_input_echo(service, raw):
    client = service[1]
    response = client.post("/api/v1/auth/login", content=raw, headers={"Content-Type": "application/json"})
    assert response.status_code == 422
    assert "SECRET" not in response.text


def test_body_limit(service):
    client = service[1]
    response = client.post("/api/v1/auth/login", content=b"x" * (2097152 + 1))
    assert response.status_code == 413


def test_source_not_configured_and_safe_errors(service):
    _settings, client, admin = service
    register(client, admin)
    result = client.post("/api/v1/admin/datasets/orders/sync", headers=admin,
                         json={"start_date": day(), "end_date": day()})
    assert result.status_code == 409
    assert client.get("/api/v1/admin/status", headers=admin).json()["data"]["configured_sources"] == []


@pytest.mark.parametrize("state,interval,healthy", [("idle", "0 seconds", True), ("syncing", "0 seconds", True),
                                                  ("stopped", "0 seconds", False), ("idle", "-100 seconds", False),
                                                  ("idle", "10 seconds", False)])
def test_worker_health_uses_database_time_and_state(service, state, interval, healthy):
    settings, client, admin = service
    with db.connect(settings) as connection:
        connection.execute("INSERT INTO worker_heartbeat VALUES('sync',clock_timestamp()+%s::interval,%s)", (interval, state))
    workers = client.get("/api/v1/admin/status", headers=admin).json()["data"]["workers"]
    assert workers[0]["healthy"] is healthy


def test_unknown_grant_and_sql_like_field_rejected(service):
    _settings, client, admin = service
    result = client.post("/api/v1/admin/keys", headers=admin,
                         json={"name": "unknown", "grants": {"missing": ["read"]}})
    assert result.status_code == 422
    schema = register(client, admin)
    schema["slug"] = "malicious"
    schema["fields"][2]["name"] = "shop;DROP TABLE datasets;--"
    assert client.post("/api/v1/admin/datasets", headers=admin, json=schema).status_code == 422


def test_config_and_image_do_not_depend_on_dashboard():
    root = Path(__file__).resolve().parents[1]
    compose = (root / "docker-compose.yml").read_text(encoding="utf-8")
    image = (root / "Dockerfile").read_text(encoding="utf-8")
    assert "ruoyi" not in compose + image and "../" not in compose + image
    assert "web:" not in compose and "Node" not in image
    assert json.loads((root / "config/datasets.json").read_text(encoding="utf-8")) == []
