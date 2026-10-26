"""独立黑盒验收：管理授权、配置凭据及命名查询边界，全部使用真实 PostgreSQL。"""

from copy import deepcopy
from decimal import Decimal
import json

from cryptography.fernet import Fernet
import pytest

from data_service import db
from .conftest import machine_key, publish, register, service_body, source_body


def save_service(client, administrator, body=None, slug="shop_summary"):
    response = client.put(f"/api/v1/admin/query-services/{slug}", headers=administrator,
                          json=body if body is not None else service_body())
    assert response.status_code in (200, 201), response.text
    return response.json()["data"]


def save_source(client, administrator, body=None):
    response = client.put("/api/v1/admin/sources/ind_sales", headers=administrator,
                          json=body if body is not None else source_body())
    assert response.status_code in (200, 201), response.text
    return response.json()["data"]


@pytest.mark.parametrize("method,path", [
    ("GET", "/api/v1/admin/sources"), ("GET", "/api/v1/admin/sources/ind_sales"),
    ("PUT", "/api/v1/admin/sources/ind_sales"), ("POST", "/api/v1/admin/sources/ind_sales/test"),
    ("GET", "/api/v1/admin/query-services"), ("GET", "/api/v1/admin/query-services/summary"),
    ("PUT", "/api/v1/admin/query-services/summary"), ("POST", "/api/v1/admin/query-services/preview"),
])
def test_management_requires_admin(admin_service, method, path):
    _, client, administrator = admin_service
    register(client, administrator)
    reader = machine_key(client, administrator, {"ind_sales": ["read", "write"]})
    for credentials in ({}, reader):
        response = client.request(method, path, headers=credentials, json={})
        assert response.status_code == 401, response.text


def test_source_secret_authenticated_encryption_and_redaction(admin_service):
    settings, client, administrator = admin_service
    register(client, administrator)
    secret = "Bearer independent-synthetic-secret-2026"
    body = source_body()
    body["config"]["header_env"] = {"Authorization": "INDEPENDENT_AUTH"}
    body["secrets"] = {"INDEPENDENT_AUTH": secret}
    result = save_source(client, administrator, body)
    assert result["revision"] == 1
    assert "INDEPENDENT_AUTH" in result["secret_names"]
    for path in ("/api/v1/admin/sources", "/api/v1/admin/sources/ind_sales", "/api/v1/admin/status"):
        response = client.get(path, headers=administrator)
        assert response.status_code == 200
        assert secret not in response.text
        assert "encrypted_secrets" not in response.text
    with db.connect(settings) as connection:
        stored = connection.execute("SELECT * FROM managed_sources WHERE dataset_slug='ind_sales'").fetchone()
    assert secret not in json.dumps(stored["definition"])
    assert secret not in stored["encrypted_secrets"]
    decrypted = json.loads(Fernet(settings.config_encryption_key).decrypt(stored["encrypted_secrets"].encode()))
    assert decrypted == {"INDEPENDENT_AUTH": secret}
    body["revision"], body["secrets"], body["title"] = 1, {}, "改名保留凭据"
    updated = save_source(client, administrator, body)
    assert updated["revision"] == 2
    with db.connect(settings) as connection:
        encrypted = connection.execute("SELECT encrypted_secrets FROM managed_sources").fetchone()["encrypted_secrets"]
    assert json.loads(Fernet(settings.config_encryption_key).decrypt(encrypted.encode())) == decrypted


def test_source_revisions_prevent_lost_update(admin_service):
    _, client, administrator = admin_service
    register(client, administrator)
    save_source(client, administrator)
    response = client.put("/api/v1/admin/sources/ind_sales", headers=administrator, json=source_body(title="过期写入"))
    assert response.status_code == 409, response.text
    current = client.get("/api/v1/admin/sources/ind_sales", headers=administrator).json()["data"]
    assert current["title"] == "独立测试来源" and current["revision"] == 1


@pytest.mark.parametrize("mutation", ["path", "mapping", "approval", "credential", "plaintext", "schedule"])
def test_invalid_source_is_atomic(admin_service, mutation):
    settings, client, administrator = admin_service
    register(client, administrator)
    body = source_body()
    if mutation == "path":
        body["config"]["dataset_slug"] = "other_table"
    elif mutation == "mapping":
        body["config"]["field_mapping"] = {"id": "id"}
    elif mutation == "approval":
        body["approve_host"] = False
    elif mutation == "credential":
        body["config"]["header_env"] = {"Authorization": "INDEPENDENT_MISSING_SECRET"}
    elif mutation == "plaintext":
        body["config"]["static_params"] = {"password": "must-not-be-echoed"}
    else:
        body["schedule_hour"] = 24
    response = client.put("/api/v1/admin/sources/ind_sales", headers=administrator, json=body)
    assert response.status_code in (400, 409, 422, 503), response.text
    assert "must-not-be-echoed" not in response.text
    with db.connect(settings) as connection:
        assert connection.execute("SELECT COUNT(*) AS count FROM managed_sources").fetchone()["count"] == 0


def test_aggregates_use_fixed_filter_exact_decimal_and_null(admin_service, business_day):
    _, client, administrator = admin_service
    register(client, administrator)
    publish(client, administrator, business_day, [
        {"dt": business_day, "id": "001", "shop": "A", "amount": "9007199254740993.100001", "units": 1},
        {"dt": business_day, "id": "002", "shop": "A", "amount": "0.200002", "units": 2},
        {"dt": business_day, "id": "003", "shop": "A", "amount": None, "units": None},
        {"dt": business_day, "id": "004", "shop": "B", "amount": "100.000000", "units": 3},
    ])
    save_service(client, administrator)
    reader = machine_key(client, administrator, {"ind_sales": ["read"]})
    payload = {"start_date": business_day, "end_date": business_day}
    response = client.post("/api/v1/services/shop_summary/query", headers=reader, json=payload)
    assert response.status_code == 200, response.text
    result = response.json()["data"]
    assert result["limit"] == 20
    assert result["total"] == 1
    assert result["rows"][0]["shop"] == "A"
    assert Decimal(result["rows"][0]["sales"]) == Decimal("9007199254740993.300003")
    assert result["rows"][0]["products"] == 3
    # 运行时筛选与保存的固定规则相交，不能改写为另一店铺。
    response = client.post("/api/v1/services/shop_summary/query", headers=reader,
                           json={**payload, "filters": [{"field": "shop", "op": "eq", "value": "B"}]})
    assert response.status_code == 200, response.text
    assert response.json()["data"]["rows"] == []


@pytest.mark.parametrize("extra", [
    {"fields": ["id"]}, {"group_by": []}, {"aggregates": []}, {"order_by": []},
    {"require_complete": False}, {"limit": 21}, {"limit": 0}, {"offset": -1},
    {"filters": [{"field": "amount", "op": "gte", "value": "0"}]},
])
def test_external_cannot_override_rules_or_bounds(admin_service, business_day, extra):
    _, client, administrator = admin_service
    register(client, administrator)
    save_service(client, administrator)
    reader = machine_key(client, administrator, {"ind_sales": ["read"]})
    response = client.post("/api/v1/services/shop_summary/query", headers=reader,
                           json={"start_date": business_day, "end_date": business_day, **extra})
    assert response.status_code in (400, 422), response.text


def test_external_service_visibility_and_revocation(admin_service, business_day):
    _, client, administrator = admin_service
    register(client, administrator)
    register(client, administrator, "ind_private")
    published = save_service(client, administrator)
    save_service(client, administrator, service_body(dataset_slug="ind_private"), "private_summary")
    save_service(client, administrator, service_body(enabled=False), "disabled_summary")
    reader = machine_key(client, administrator, {"ind_sales": ["read"]})
    writer = machine_key(client, administrator, {"ind_sales": ["write"]})
    assert [row["slug"] for row in client.get("/api/v1/services", headers=reader).json()["data"]] == ["shop_summary"]
    assert client.get("/api/v1/services", headers=writer).json()["data"] == []
    query = {"start_date": business_day, "end_date": business_day}
    assert client.post("/api/v1/services/private_summary/query", headers=reader, json=query).status_code == 403
    assert client.post("/api/v1/services/shop_summary/query", headers=writer, json=query).status_code == 403
    assert client.post("/api/v1/services/disabled_summary/query", headers=reader, json=query).status_code in (404, 409)
    save_service(client, administrator, service_body(enabled=False, revision=published["revision"]))
    assert client.get("/api/v1/services", headers=reader).json()["data"] == []
    assert client.post("/api/v1/services/shop_summary/query", headers=reader, json=query).status_code in (404, 409)


def test_query_service_revision_and_completeness(admin_service, business_day):
    _, client, administrator = admin_service
    register(client, administrator)
    save_service(client, administrator)
    response = client.put("/api/v1/admin/query-services/shop_summary", headers=administrator,
                          json=service_body(name="过期覆盖", revision=0))
    assert response.status_code == 409, response.text
    assert client.get("/api/v1/admin/query-services/shop_summary", headers=administrator).json()["data"]["revision"] == 1
    response = client.post("/api/v1/services/shop_summary/query", headers=administrator,
                           json={"start_date": business_day, "end_date": business_day})
    assert response.status_code == 409, response.text


@pytest.mark.parametrize("mutation", ["field", "dimension", "alias", "unknown_filter", "sql", "expression"])
def test_query_definition_rejected_before_persist(admin_service, mutation):
    settings, client, administrator = admin_service
    register(client, administrator)
    body = deepcopy(service_body())
    if mutation == "field":
        body["definition"]["aggregates"][0]["field"] = "unknown"
    elif mutation == "dimension":
        body["definition"]["group_by"] = ["amount"]
    elif mutation == "alias":
        body["definition"]["aggregates"][0]["alias"] = "shop"
    elif mutation == "unknown_filter":
        body["allowed_filter_fields"] = ["unknown"]
    elif mutation == "sql":
        body["definition"]["aggregates"][0]["field"] = "amount); DROP TABLE datasets; --"
    else:
        body["definition"]["aggregates"][0]["expression"] = "amount * 10"
    response = client.put("/api/v1/admin/query-services/rejected", headers=administrator, json=body)
    assert response.status_code == 422, response.text
    with db.connect(settings) as connection:
        assert connection.execute("SELECT COUNT(*) AS count FROM query_services").fetchone()["count"] == 0
        assert connection.execute("SELECT COUNT(*) AS count FROM datasets").fetchone()["count"] == 1


def test_preview_runs_real_query_without_publishing(admin_service, business_day):
    settings, client, administrator = admin_service
    register(client, administrator)
    publish(client, administrator, business_day, [
        {"dt": business_day, "id": "01", "shop": "A", "amount": "12.50", "units": 2},
    ])
    response = client.post("/api/v1/admin/query-services/preview", headers=administrator, json={
        "dataset_slug": "ind_sales", "definition": service_body()["definition"],
        "start_date": business_day, "end_date": business_day, "limit": 20,
    })
    assert response.status_code == 200, response.text
    assert Decimal(response.json()["data"]["rows"][0]["sales"]) == Decimal("12.5")
    with db.connect(settings) as connection:
        assert connection.execute("SELECT COUNT(*) AS count FROM query_services").fetchone()["count"] == 0


@pytest.mark.parametrize("target", ["title", "secret"])
def test_source_invalid_unicode_is_validation_error(admin_service, target):
    _, client, administrator = admin_service
    register(client, administrator)
    body = source_body()
    if target == "title":
        body["title"] = "bad\ud800name"
    else:
        body["config"]["header_env"] = {"Authorization": "BAD_UNICODE"}
        body["secrets"] = {"BAD_UNICODE": "bad\ud800secret"}
    response = client.put("/api/v1/admin/sources/ind_sales", headers={**administrator, "Content-Type": "application/json"},
                          content=json.dumps(body, ensure_ascii=True).encode())
    assert response.status_code == 422, response.text
