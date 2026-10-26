"""独立API测试以真实鉴权、机器密钥与数据库运行；外部源不在此夹具模拟。"""

import hashlib

from fastapi.testclient import TestClient
import pytest

from data_service.api import create_app
from data_service.db import connect


@pytest.fixture(scope="session")
def api_client(independent_settings):
    with TestClient(create_app(independent_settings), raise_server_exceptions=False) as client:
        yield client


@pytest.fixture(scope="session")
def administrator(api_client, independent_settings):
    response = api_client.post("/api/v1/auth/login", json={"username": independent_settings.admin_username, "password": independent_settings.admin_password})
    assert response.status_code == 200
    return {"Authorization": "Bearer " + response.json()["data"]["token"]}


@pytest.fixture
def api_datasets(api_client, administrator, dataset_definitions):
    for definition in dataset_definitions:
        response = api_client.post("/api/v1/admin/datasets", headers=administrator, json=definition)
        assert response.status_code == 201, response.text
        assert "table_name" not in response.json()["data"]
    return [definition["slug"] for definition in dataset_definitions]


def issue(api_client, administrator, grants, **changes):
    response = api_client.post("/api/v1/admin/keys", headers=administrator,
                              json={"name": "独立自动测试", "grants": grants, **changes})
    assert response.status_code == 201, response.text
    return response.json()["data"]


def key_header(key):
    return {"X-API-Key": key["api_key"]}


def test_service_is_independent_and_docs_local(api_client):
    root = api_client.get("/")
    assert root.status_code == 200 and root.json()["data"]["dashboard_included"] is False
    assert api_client.get("/health/ready").status_code == 200
    assert api_client.get("/api/getInfo").status_code == 404
    document = api_client.get("/docs")
    assert document.status_code == 200 and "<script src=" not in document.text
    assert "Cache-Control" in document.headers and document.headers["X-Content-Type-Options"] == "nosniff"
    assert "/api/v1/query/batch" in api_client.get("/openapi.json").json()["paths"]


def test_key_scope_and_control_plane_isolation(api_client, administrator, api_datasets, day):
    one, two = api_datasets
    reader = issue(api_client, administrator, {one: ["read"]})
    headers = key_header(reader)
    assert api_client.get("/api/v1/datasets").status_code == 401
    listed = api_client.get("/api/v1/datasets", headers=headers)
    assert [row["slug"] for row in listed.json()["data"]] == [one]
    assert api_client.get(f"/api/v1/datasets/{two}", headers=headers).status_code == 403
    assert api_client.post(f"/api/v1/datasets/{one}/batches", headers=headers,
                           json={"business_date": str(day), "expected_rows": 0, "idempotency_key": "read-only"}).status_code == 403
    assert api_client.get("/api/v1/admin/keys", headers=headers).status_code == 401
    assert api_client.get("/api/v1/datasets", headers={**headers, **administrator}).status_code == 400
    batch_query = {"queries": [{"dataset": slug, "query": {"start_date": str(day), "end_date": str(day), "require_complete": False}} for slug in [one, two]]}
    denied = api_client.post("/api/v1/query/batch", headers=headers, json=batch_query)
    assert denied.status_code == 403 and "data" not in denied.json()


def test_machine_push_roundtrip_and_other_owner_denied(api_client, administrator, api_datasets, day):
    slug = api_datasets[0]
    owner = issue(api_client, administrator, {slug: ["read", "write"]})
    other = issue(api_client, administrator, {slug: ["write"]})
    request = {"business_date": str(day), "expected_rows": 1, "idempotency_key": "upload-001"}
    created = api_client.post(f"/api/v1/datasets/{slug}/batches", headers=key_header(owner), json=request)
    assert created.status_code == 201
    batch_id = created.json()["data"]["id"]
    assert api_client.get(f"/api/v1/batches/{batch_id}", headers=key_header(other)).status_code == 403
    assert api_client.post(f"/api/v1/batches/{batch_id}/commit", headers=key_header(other)).status_code == 403
    row = {"dt": str(day), "product_id": "000005", "shop": "s", "amount": "9007199254740993.123456", "units": 1}
    uploaded = api_client.put(f"/api/v1/batches/{batch_id}/chunks/0", headers=key_header(owner), json={"rows": [row]})
    assert uploaded.status_code == 200
    pending = api_client.post(f"/api/v1/datasets/{slug}/query", headers=key_header(owner), json={"start_date": str(day), "end_date": str(day)})
    assert pending.status_code == 409
    assert api_client.post(f"/api/v1/batches/{batch_id}/commit", headers=key_header(owner)).status_code == 200
    completed = api_client.post(f"/api/v1/datasets/{slug}/query", headers=key_header(owner), json={"start_date": str(day), "end_date": str(day)})
    assert completed.status_code == 200
    assert completed.json()["data"]["rows"][0]["amount"] == "9007199254740993.123456"
    assert completed.json()["data"]["rows"][0]["product_id"] == "000005"


def test_key_hash_revoke_expiry_and_no_secret_list(api_client, administrator, api_datasets, independent_settings):
    key = issue(api_client, administrator, {api_datasets[0]: ["read"]})
    listed = api_client.get("/api/v1/admin/keys", headers=administrator)
    assert key["api_key"] not in listed.text and "token_hash" not in listed.text
    with connect(independent_settings) as connection:
        stored = connection.execute("SELECT token_hash FROM api_keys WHERE id=%s", (key["id"],)).fetchone()["token_hash"]
    assert stored == hashlib.sha256(key["api_key"].encode()).hexdigest()
    assert api_client.delete(f"/api/v1/admin/keys/{key['id']}", headers=administrator).status_code == 200
    assert api_client.get("/api/v1/datasets", headers=key_header(key)).status_code == 401
    expired = issue(api_client, administrator, {api_datasets[0]: ["read"]})
    with connect(independent_settings) as connection:
        connection.execute("UPDATE api_keys SET expires_at=clock_timestamp()-interval '1 second' WHERE id=%s", (expired["id"],))
    assert api_client.get("/api/v1/datasets", headers=key_header(expired)).status_code == 401


def test_key_limit_counter_commits_denials(api_client, administrator, api_datasets, independent_settings):
    key = issue(api_client, administrator, {api_datasets[0]: ["read"]}, requests_per_minute=2)
    statuses = [api_client.get("/api/v1/datasets", headers=key_header(key)).status_code for _ in range(4)]
    assert statuses == [200, 200, 429, 429]
    with connect(independent_settings) as connection:
        assert connection.execute("SELECT attempts FROM api_key_limits WHERE key_id=%s", (key["id"],)).fetchone()["attempts"] == 4


@pytest.mark.parametrize("body", [b'{"username":"admin","username":"other","password":"secret"}', b'{"number":NaN}', b'{"unterminated":'])
def test_invalid_json_is_safe(api_client, body):
    response = api_client.post("/api/v1/auth/login", content=body, headers={"Content-Type": "application/json"})
    assert response.status_code == 422 and "secret" not in response.text


def test_large_body_rejected_without_relying_on_content_length(api_client, independent_settings):
    payload = b'{"payload":"' + b"x" * independent_settings.max_request_bytes + b'"}'
    response = api_client.post("/api/v1/auth/login", content=iter([payload[:1000], payload[1000:]]),
                               headers={"Content-Type": "application/json"})
    assert response.status_code == 413


def test_invalid_parameters_do_not_reflect_sensitive_value(api_client, administrator, api_datasets, day):
    secret_marker = "INDEPENDENT_SECRET_SENTINEL"
    response = api_client.post(f"/api/v1/datasets/{api_datasets[0]}/query", headers=administrator,
                               json={"start_date": str(day), "end_date": str(day), "fields": [secret_marker + "'"]})
    assert response.status_code == 422 and secret_marker not in response.text


def test_batch_query_one_missing_day_returns_no_partial_data(api_client, administrator, api_datasets, day):
    slug = api_datasets[0]
    created = api_client.post(f"/api/v1/datasets/{slug}/batches", headers=administrator,
                              json={"business_date": str(day), "expected_rows": 0, "idempotency_key": "empty"})
    batch_id = created.json()["data"]["id"]
    assert api_client.post(f"/api/v1/batches/{batch_id}/commit", headers=administrator).status_code == 200
    response = api_client.post("/api/v1/query/batch", headers=administrator,
                               json={"queries": [{"dataset": value, "query": {"start_date": str(day), "end_date": str(day)}} for value in api_datasets]})
    assert response.status_code == 409 and "data" not in response.json()
