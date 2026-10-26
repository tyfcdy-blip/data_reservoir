"""隔离PostgreSQL验证接口授权、行级范围、计数和旧密钥兼容。"""

from datetime import datetime, timedelta
from importlib.resources import files
import os
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb
import pytest
from pydantic import ValidationError

from data_service import auth, db, datasets, ingest
from data_service.access import KeyCreate
from data_service.api import create_app
from data_service.config import Settings
from data_service.key_policy import KeyPolicy, RowScope
from data_service.models import DatasetDefinition


def scope(*groups):
    return {"mode": "filtered", "groups": [{"conditions": [
        {"field": field, "values": values} for field, values in group.items()]} for group in groups]}


def policy(*groups, services=None):
    return {"services": {"by_shop": "sales"} if services is None else services,
            "row_scopes": {"sales": scope(*groups) if groups else {"mode": "all"}}}


@pytest.fixture
def service(tmp_path):
    address = os.environ["DS_TEST_DATABASE_URL"]
    assert urlsplit(address).path == "/admin_api_test", "只允许清理权限后端专用测试库"
    source_file = tmp_path / "sources.json"
    source_file.write_text('{"sources": []}', encoding="utf-8")
    settings = Settings(database_url=address, admin_username="policy_test",
                        admin_password="policy-test-only-password!", source_config_path=str(source_file))
    with db.connect(settings) as connection:
        connection.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
    db.migrate(settings)
    auth.initialize_admin(settings)
    day = (datetime.now(ZoneInfo("Asia/Shanghai")).date() - timedelta(days=1)).isoformat()
    definition = DatasetDefinition.model_validate({
        "slug": "sales", "name": "权限隔离测试", "date_field": "dt", "key_fields": ["id"],
        "fields": [{"name": "dt", "type": "date", "required": True},
                   {"name": "id", "type": "string", "required": True},
                   *[{"name": field, "type": "string", "dimension": True} for field in ("channel", "shop", "owner")],
                   {"name": "amount", "type": "decimal"}, {"name": "refund", "type": "decimal"}]})
    datasets.register_dataset(settings, definition)
    datasets.register_dataset(settings, definition.model_copy(update={"slug": "other"}))
    records = [
        ("1", "淘宝", "A", "甲", "100", "10"),
        ("2", "淘宝", "A", "甲", "300", "90"),
        ("3", "淘宝", "B", "乙", "100", "80"),
        ("4", "京东", "A", "乙", "100", "99"),
        ("5", "京东", "B", "甲", "200", "10"),
        ("6", None, None, None, "400", "200"),
        ("7", "淘宝", " A ", "甲", "10", "0"),
        ("8", "淘宝", "C", "甲", "0", "5"),
    ]
    batch = ingest.create_batch(settings, "sales", datetime.fromisoformat(day).date(), len(records), "policy-sales")
    ingest.append_chunk(settings, batch["id"], 0, [dict(zip(
        ("id", "channel", "shop", "owner", "amount", "refund"), record, strict=True), dt=day) for record in records])
    ingest.commit_batch(settings, batch["id"])
    with TestClient(create_app(settings), raise_server_exceptions=False) as client:
        login = client.post("/api/v1/auth/login", json={"username": settings.admin_username,
                                                      "password": settings.admin_password})
        assert login.status_code == 200
        headers = {"Authorization": "Bearer " + login.json()["data"]["token"]}
        config = {"name": "店铺退款率", "dataset_slug": "sales", "definition": {
            "group_by": ["shop"], "aggregates": [
                {"field": "amount", "op": "sum", "alias": "paid"},
                {"field": "refund", "op": "sum", "alias": "refunded"}],
            "formulas": [{"alias": "refund_rate", "expression": "refunded / paid", "decimal_places": 6}],
            "order_by": [{"field": "refund_rate", "direction": "desc"}]},
            "allowed_filter_fields": ["shop", "channel", "owner"], "enabled": True, "revision": 0}
        for slug in ("by_shop", "other_service"):
            saved = client.put("/api/v1/admin/query-services/" + slug, headers=headers, json=config)
            assert saved.status_code == 200, saved.text
        yield settings, client, headers, day, config


def issue(service, *, grants=None, key_policy=None, **extra):
    body = {"name": "权限测试", "grants": grants or {}, "policy": key_policy, **extra}
    result = service[1].post("/api/v1/admin/keys", headers=service[2], json=body)
    assert result.status_code == 201, result.text
    issued = result.json()["data"]
    return {"X-API-Key": issued["api_key"]}, issued


def query(service, headers, *, url="/api/v1/datasets/sales/query", **changes):
    return service[1].post(url, headers=headers,
                           json={"start_date": service[3], "end_date": service[3], **changes})


def test_service_only_scope_before_formula_and_no_schema_or_direct_bypass(service):
    headers, _ = issue(service, key_policy=policy({"channel": ["淘宝"], "shop": ["A"]}))
    client = service[1]
    result = query(service, headers, url="/api/v1/services/by_shop/query")
    assert result.status_code == 200, result.text
    data = result.json()["data"]
    assert data["rows"] == [{"shop": "A", "paid": "400.000000", "refunded": "100.000000", "refund_rate": "0.250000"}]
    assert data["total"] == 1 and data["published_versions"][0]["row_count"] == 2
    assert client.get("/api/v1/datasets", headers=headers).json()["data"] == []
    assert client.get("/api/v1/datasets/sales", headers=headers).status_code == 403
    assert query(service, headers).status_code == 403
    assert query(service, headers, url="/api/v1/services/other_service/query").status_code == 403
    directory = client.get("/api/v1/services", headers=headers).json()["data"]
    assert [entry["slug"] for entry in directory] == ["by_shop"]
    assert "definition" not in directory[0]
    assert client.get("/api/v1/admin/keys", headers=headers).status_code == 401
    assert client.post("/api/v1/datasets/sales/batches", headers=headers, json={
        "business_date": service[3], "expected_rows": 0, "idempotency_key": "forbidden"}).status_code == 403


def test_or_groups_pair_channels_and_shops_without_cross_product(service):
    headers, _ = issue(service, grants={"sales": ["read"]}, key_policy=policy(
        {"channel": ["淘宝"], "shop": ["A"]}, {"channel": ["京东"], "shop": ["B"]}, services={}))
    result = query(service, headers, fields=["id"], limit=2).json()["data"]
    assert result["rows"] == [{"id": "1"}, {"id": "2"}]
    assert result["total"] == 3 and result["published_versions"][0]["row_count"] == 3
    assert query(service, headers, fields=["id"], limit=2, offset=2).json()["data"]["rows"] == [{"id": "5"}]
    assert query(service, headers, filters=[{"field": "shop", "op": "eq", "value": "B"}]).json()["data"]["total"] == 1
    assert query(service, headers, filters=[{"field": "channel", "op": "eq", "value": "京东"},
                                          {"field": "shop", "op": "eq", "value": "A"}]).json()["data"]["rows"] == []
    assert query(service, headers, url="/api/v1/services/by_shop/query").status_code == 403
    assert service[1].get("/api/v1/services", headers=headers).json()["data"] == []
    assert [entry["slug"] for entry in service[1].get("/api/v1/datasets", headers=headers).json()["data"]] == ["sales"]


def test_owner_multiple_values_count_result_filter_and_order(service):
    headers, _ = issue(service, grants={"sales": ["read"]}, key_policy=policy({"owner": ["甲"], "shop": ["A", "B", "C"]}))
    request = {**service[4]["definition"], "limit": 1}
    data = query(service, headers, **request).json()["data"]
    assert data["rows"][0]["shop"] == "A" and data["total"] == 3
    assert data["published_versions"][0]["row_count"] == 4
    data = query(service, headers, **request, offset=2).json()["data"]
    assert data["rows"] == [{"shop": "C", "paid": "0.000000", "refunded": "5.000000", "refund_rate": None}]
    data = query(service, headers, **request, result_filters=[{"field": "refund_rate", "op": "gt", "value": "0.1"}]).json()["data"]
    assert data["total"] == 1 and data["rows"][0]["shop"] == "A"


def test_service_filters_only_intersect_and_scope_not_client_editable(service):
    headers, _ = issue(service, key_policy=policy({"channel": ["淘宝"], "shop": ["A"]}))
    result = query(service, headers, url="/api/v1/services/by_shop/query", filters=[{"field": "shop", "op": "eq", "value": "B"}])
    assert result.status_code == 200 and result.json()["data"]["rows"] == []
    for field in ("policy", "row_scope", "row_scopes", "grants"):
        assert query(service, headers, url="/api/v1/services/by_shop/query", **{field: {}}).status_code == 422


def test_batch_scope_and_mixed_dataset_denied(service):
    headers, _ = issue(service, grants={"sales": ["read"]}, key_policy=policy({"shop": ["B"]}))
    request = {"dataset": "sales", "query": {"start_date": service[3], "end_date": service[3], "fields": ["id"]}}
    result = service[1].post("/api/v1/query/batch", headers=headers, json={"queries": [request, request]})
    assert result.status_code == 200, result.text
    assert all(part["rows"] == [{"id": "3"}, {"id": "5"}] and part["published_versions"][0]["row_count"] == 2
               for part in result.json()["data"])
    assert service[1].post("/api/v1/query/batch", headers=headers,
                           json={"queries": [request, {**request, "dataset": "other"}]}).status_code == 403
    service_headers, _ = issue(service, key_policy=policy({"shop": ["B"]}))
    assert service[1].post("/api/v1/query/batch", headers=service_headers, json={"queries": [request]}).status_code == 403


@pytest.mark.parametrize("shop,expected", [(" A ", ["7"]), ("missing", []), ("A' OR 1=1 --", []), ("A", ["1", "2", "4"])])
def test_exact_match_null_and_sql_values(service, shop, expected):
    headers, _ = issue(service, grants={"sales": ["read"]}, key_policy=policy({"shop": [shop]}))
    result = query(service, headers, fields=["id"])
    assert result.status_code == 200, result.text
    data = result.json()["data"]
    assert [row["id"] for row in data["rows"]] == expected
    assert data["total"] == len(expected) and data["published_versions"][0]["row_count"] == len(expected)


def test_legacy_full_read_and_write_retained(service):
    headers, issued = issue(service, grants={"sales": ["read", "write"]})
    assert issued["policy"] is None
    assert query(service, headers).json()["data"]["total"] == 8
    assert query(service, headers).json()["data"]["published_versions"][0]["row_count"] == 8
    assert len(service[1].get("/api/v1/services", headers=headers).json()["data"]) == 2
    assert query(service, headers, url="/api/v1/services/other_service/query").status_code == 200
    response = service[1].post("/api/v1/datasets/sales/batches", headers=headers, json={
        "business_date": service[3], "expected_rows": 0, "idempotency_key": "legacy-write"})
    assert response.status_code == 201, response.text


def test_explicit_all_allows_null_rows_but_services_still_explicit(service):
    headers, _ = issue(service, grants={"sales": ["read"]}, key_policy=policy(services={}))
    assert query(service, headers).json()["data"]["total"] == 8
    assert query(service, headers, url="/api/v1/services/by_shop/query").status_code == 403


@pytest.mark.parametrize("mutation", ["expired", "revoked", "rate"])
def test_restricted_key_lifecycle(service, mutation):
    headers, issued = issue(service, key_policy=policy(), requests_per_minute=1 if mutation == "rate" else 60)
    if mutation == "expired":
        with db.connect(service[0]) as connection:
            connection.execute("UPDATE api_keys SET expires_at=now()-interval '1 second' WHERE id=%s", (issued["id"],))
    elif mutation == "revoked":
        assert service[1].delete("/api/v1/admin/keys/" + issued["id"], headers=service[2]).status_code == 200
    else:
        assert query(service, headers, url="/api/v1/services/by_shop/query").status_code == 200
    assert query(service, headers, url="/api/v1/services/by_shop/query").status_code == (429 if mutation == "rate" else 401)


@pytest.mark.parametrize("broken", [None, {}, {"services": {}, "row_scopes": {}},
                                     {"services": {"by_shop": "sales"}, "row_scopes": {"sales": {"mode": "filtered", "groups": []}}}])
def test_corrupt_stored_policy_fails_closed(service, broken):
    headers, issued = issue(service, key_policy=policy())
    with db.connect(service[0]) as connection:
        connection.execute("UPDATE api_keys SET policy=%s WHERE id=%s", (Jsonb(broken), issued["id"]))
    result = service[1].get("/api/v1/services", headers=headers)
    assert result.status_code == 403 and result.json()["error_code"] == "KEY_POLICY_INVALID"


@pytest.mark.parametrize("change", ["field_removed", "not_dimension", "type_changed", "grants_changed", "grants_malformed"])
def test_invalid_scope_references_and_grants_fail_closed(service, change):
    headers, issued = issue(service, key_policy=policy({"shop": ["A"]}))
    with db.connect(service[0]) as connection:
        if change.startswith("grants"):
            connection.execute("UPDATE api_keys SET grants=%s WHERE id=%s",
                               (Jsonb({"sales": ["write"]} if change == "grants_changed" else ["sales"]), issued["id"]))
        else:
            definition = connection.execute("SELECT definition FROM datasets WHERE slug='sales'").fetchone()["definition"]
            for field in definition["fields"]:
                if field["name"] == "shop":
                    if change == "not_dimension":
                        field["dimension"] = False
                    elif change == "type_changed":
                        field["type"] = "decimal"
                    else:
                        field["name"] = "removed"
            connection.execute("UPDATE datasets SET definition=%s WHERE slug='sales'", (Jsonb(definition),))
    assert query(service, headers, url="/api/v1/services/by_shop/query").status_code == 403
    assert service[1].get("/api/v1/datasets", headers=headers).status_code == 403


def test_service_changed_dataset_or_disabled_cannot_expand_key(service):
    headers, _ = issue(service, key_policy=policy())
    config = {**service[4], "revision": 1, "dataset_slug": "other"}
    assert service[1].put("/api/v1/admin/query-services/by_shop", headers=service[2], json=config).status_code == 200
    assert query(service, headers, url="/api/v1/services/by_shop/query").status_code == 403
    assert service[1].get("/api/v1/services", headers=headers).json()["data"] == []
    config.update(dataset_slug="sales", revision=2, enabled=False)
    assert service[1].put("/api/v1/admin/query-services/by_shop", headers=service[2], json=config).status_code == 200
    assert query(service, headers, url="/api/v1/services/by_shop/query").status_code == 404


@pytest.mark.parametrize("key_policy", [policy({"id": ["1"]}), policy({"amount": ["100"]}), policy({"missing": ["A"]}),
                                         policy({"shop": ["a" * 256]}), policy(services={"missing": "sales"}),
                                         {"services": {"by_shop": "other"}, "row_scopes": {"other": {"mode": "all"}}}])
def test_creation_invalid_reference_rejected(service, key_policy):
    result = service[1].post("/api/v1/admin/keys", headers=service[2], json={"name": "bad", "policy": key_policy})
    assert result.status_code == 422, result.text
    assert service[1].get("/api/v1/admin/keys", headers=service[2]).json()["data"] == []


@pytest.mark.parametrize("value", [None, 1, True, "", " ", "\t", "A\nB", "A\x7f", "\u0085", "\ud800", "x" * 10001])
def test_authorization_value_validation(value):
    with pytest.raises(ValidationError):
        KeyPolicy.model_validate(policy({"shop": [value]}))


@pytest.mark.parametrize("raw", [
    {"mode": "filtered"}, {"mode": "all", "groups": [{"conditions": [{"field": "shop", "values": ["A"]}]}]},
    {"mode": "filtered", "groups": [{"conditions": []}]}, scope({"shop": ["A", "A"]}),
    scope({"shop": []}), scope({"shop": [str(i) for i in range(101)]}),
    scope({"shop": ["A"]}, *[{"shop": [str(i)]} for i in range(20)]),
    scope({"a": ["x"], "b": ["x"], "c": ["x"], "d": ["x"], "e": ["x"], "f": ["x"]}),
    {"mode": "filtered", "groups": [{"conditions": [{"field": "shop", "values": ["A"]}, {"field": "shop", "values": ["B"]}]}]},
    scope({"shop); DROP TABLE api_keys--": ["A"]}),
])
def test_scope_shape_validation(raw):
    with pytest.raises(ValidationError):
        RowScope.model_validate(raw)


def test_scope_total_budget_and_grants_coverage():
    allowed = policy(*[{"shop": [str(i) for i in range(100)]} for _ in range(20)])
    assert KeyPolicy.model_validate(allowed)
    denied = {**allowed, "row_scopes": {**allowed["row_scopes"], "other": scope({"shop": ["A"]})}}
    with pytest.raises(ValidationError):
        KeyPolicy.model_validate(denied)
    for body in ({"name": "bad"}, {"name": "bad", "policy": {"services": {}, "row_scopes": {"sales": {"mode": "all"}}}},
                 {"name": "bad", "grants": {"sales": ["write"]}, "policy": policy()},
                 {"name": "bad", "grants": {"other": ["read"]}, "policy": policy()}):
        with pytest.raises(ValidationError):
            KeyCreate.model_validate(body)


def test_migration5_preserves_legacy_grants_and_is_idempotent(service):
    headers, issued = issue(service, grants={"sales": ["read", "write"]})
    with db.connect(service[0]) as connection:
        connection.execute("ALTER TABLE api_keys DROP COLUMN policy")
        connection.execute("DELETE FROM schema_versions WHERE version=5")
        before = connection.execute("SELECT id,token_hash,grants FROM api_keys").fetchall()
    db.migrate(service[0])
    db.migrate(service[0])
    with db.connect(service[0]) as connection:
        assert connection.execute("SELECT id,token_hash,grants FROM api_keys").fetchall() == before
        assert connection.execute("SELECT policy IS NULL AS legacy FROM api_keys WHERE id=%s", (issued["id"],)).fetchone()["legacy"]
        assert connection.execute("SELECT max(version) AS version FROM schema_versions").fetchone()["version"] == 5
        # 同一迁移SQL重复执行亦不会覆写已存策略。
        connection.execute(files("data_service").joinpath("migration_5.sql").read_text(encoding="utf-8"))
    assert query(service, headers).json()["data"]["total"] == 8
