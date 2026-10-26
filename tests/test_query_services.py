"""加工配置使用真实隔离PostgreSQL，覆盖固定规则、授权、发布和迁移。"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from importlib.resources import files
import os
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient
import pytest

from data_service import auth, db, datasets, ingest, query_services
from data_service.access import Principal
from data_service.api import create_app
from data_service.config import Settings
from data_service.errors import ServiceError
from data_service.models import DatasetDefinition
from data_service.query_services import QueryServiceSave, ServiceQuery


@pytest.fixture
def service(tmp_path):
    address = os.environ["DS_TEST_DATABASE_URL"]
    assert urlsplit(address).path == "/admin_api_test", "只允许清理管理员API专用测试库"
    source_file = tmp_path / "sources.json"
    source_file.write_text('{"sources": []}', encoding="utf-8")
    settings = Settings(database_url=address, admin_username="admin_test",
                        admin_password="admin-console-api-test-only!", source_config_path=str(source_file))
    with db.connect(settings) as connection:
        connection.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
    db.migrate(settings)
    auth.initialize_admin(settings)
    day = (datetime.now(ZoneInfo("Asia/Shanghai")).date() - timedelta(days=1)).isoformat()
    definition = DatasetDefinition.model_validate({
        "slug": "sales", "name": "销售测试", "date_field": "dt", "key_fields": ["id"],
        "fields": [{"name": "dt", "type": "date", "required": True},
                   {"name": "id", "type": "string", "required": True},
                   {"name": "shop", "type": "string", "dimension": True},
                   {"name": "amount", "type": "decimal"}, {"name": "units", "type": "integer"}]})
    datasets.register_dataset(settings, definition)
    batch = ingest.create_batch(settings, "sales", datetime.fromisoformat(day).date(), 3, "test-sales")
    ingest.append_chunk(settings, batch["id"], 0, [
        {"dt": day, "id": "001", "shop": "A", "amount": "12.30", "units": 2},
        {"dt": day, "id": "002", "shop": "A", "amount": "2.20", "units": 1},
        {"dt": day, "id": "003", "shop": "B", "amount": None, "units": 3}])
    ingest.commit_batch(settings, batch["id"])
    with TestClient(create_app(settings), raise_server_exceptions=False) as client:
        login = client.post("/api/v1/auth/login", json={"username": settings.admin_username,
                                                       "password": settings.admin_password})
        assert login.status_code == 200
        headers = {"Authorization": "Bearer " + login.json()["data"]["token"]}
        yield settings, client, headers, day


def configuration(**changes):
    return {"name": "店铺汇总", "dataset_slug": "sales", "definition": {
        "group_by": ["shop"], "aggregates": [{"field": "amount", "op": "sum", "alias": "sales_amount"}]},
        "allowed_filter_fields": ["shop"], "max_limit": 20, "enabled": True, "revision": 0, **changes}


def save(service, **changes):
    response = service[1].put("/api/v1/admin/query-services/by_shop", headers=service[2], json=configuration(**changes))
    assert response.status_code == 200, response.text
    return response.json()["data"]


def request(service, **changes):
    return service[1].post("/api/v1/services/by_shop/query", headers=service[2],
                           json={"start_date": service[3], "end_date": service[3], **changes})


def test_saved_aggregate_and_precision(service):
    saved = save(service)
    assert saved["revision"] == 1
    response = request(service)
    assert response.status_code == 200, response.text
    assert response.json()["data"]["rows"] == [{"shop": "A", "sales_amount": "14.500000"},
                                                {"shop": "B", "sales_amount": None}]
    assert response.json()["data"]["limit"] == 20
    assert request(service, filters=[{"field": "shop", "op": "eq", "value": "A"}]).json()["data"]["total"] == 1


@pytest.mark.parametrize("body", [{"fields": ["id"]}, {"group_by": []}, {"aggregates": []},
                                  {"require_complete": False}, {"order_by": []}])
def test_caller_cannot_override_template(service, body):
    save(service)
    assert request(service, **body).status_code == 422


def test_filter_whitelist_limit_and_fixed_filter(service):
    save(service, definition={"fields": ["id", "amount"], "filters": [{"field": "shop", "op": "eq", "value": "A"}]})
    assert request(service, limit=21).status_code == 422
    assert request(service, filters=[{"field": "id", "op": "eq", "value": "003"}]).status_code == 422
    result = request(service, filters=[{"field": "shop", "op": "eq", "value": "B"}])
    assert result.status_code == 200 and result.json()["data"]["rows"] == []


@pytest.mark.parametrize("definition", [
    {"fields": ["missing"]}, {"group_by": ["id"]},
    {"aggregates": [{"op": "sum", "field": "shop", "alias": "bad"}]},
    {"group_by": ["shop"], "aggregates": [{"op": "count", "alias": "shop"}]},
    {"order_by": [{"field": "missing"}]}, {"fields": ["id"], "group_by": ["shop"]},
])
def test_bad_configuration_rejected_before_save(service, definition):
    response = service[1].put("/api/v1/admin/query-services/by_shop", headers=service[2],
                              json=configuration(definition=definition))
    assert response.status_code == 422, response.text
    assert service[1].get("/api/v1/admin/query-services", headers=service[2]).json()["data"] == []


def test_revision_and_disable(service):
    save(service)
    conflict = service[1].put("/api/v1/admin/query-services/by_shop", headers=service[2], json=configuration())
    assert conflict.status_code == 409
    save(service, revision=1, enabled=False)
    assert request(service).status_code == 404
    assert service[1].get("/api/v1/services", headers=service[2]).json()["data"] == []


def test_concurrent_save_one_winner(service):
    settings = service[0]
    payload = QueryServiceSave.model_validate(configuration())
    def submit():
        try:
            query_services.save_service(settings, "race", payload)
            return 200
        except ServiceError as exc:
            return exc.status
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _: submit(), range(2))) == [200, 409]


def test_access_and_revocation(service):
    save(service)
    client, admin = service[1:3]
    issued = client.post("/api/v1/admin/keys", headers=admin,
                         json={"name": "consumer", "grants": {"sales": ["write"]}}).json()["data"]
    key = {"X-API-Key": issued["api_key"]}
    assert client.get("/api/v1/admin/query-services", headers=key).status_code == 401
    assert client.get("/api/v1/services", headers=key).json()["data"] == []
    url = "/api/v1/services/by_shop/query"
    body = {"start_date": service[3], "end_date": service[3]}
    assert client.post(url, headers=key, json=body).status_code == 403
    issued = client.post("/api/v1/admin/keys", headers=admin,
                         json={"name": "reader", "grants": {"sales": ["read"]}}).json()["data"]
    key = {"X-API-Key": issued["api_key"]}
    directory = client.get("/api/v1/services", headers=key).json()["data"]
    assert len(directory) == 1 and "definition" not in directory[0]
    assert client.post(url, headers=key, json=body).status_code == 200
    client.delete("/api/v1/admin/keys/" + issued["id"], headers=admin)
    assert client.post(url, headers=key, json=body).status_code == 401


def test_preview_does_not_publish(service):
    response = service[1].post("/api/v1/admin/query-services/preview", headers=service[2], json={
        "dataset_slug": "sales", "definition": configuration()["definition"],
        "start_date": service[3], "end_date": service[3]})
    assert response.status_code == 200 and response.json()["data"]["total"] == 2
    assert service[1].get("/api/v1/admin/query-services", headers=service[2]).json()["data"] == []


def test_date_coverage_and_filter_budget(service):
    save(service)
    earlier = (datetime.fromisoformat(service[3]).date() - timedelta(days=1)).isoformat()
    assert request(service, start_date=earlier).status_code == 409
    assert request(service, end_date=earlier).status_code == 422
    twenty = [{"field": "shop", "op": "eq", "value": "A"}] * 20
    save(service, revision=1, definition={"fields": ["id"], "filters": twenty})
    assert request(service, filters=twenty).status_code == 422


def test_v1_migration_retains_rows_and_is_idempotent(service):
    settings = service[0]
    # fixture专库中重建真正v1结构，再升级；不触碰其他库。
    with db.connect(settings) as connection:
        connection.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
        connection.execute(files("data_service").joinpath("schema.sql").read_text(encoding="utf-8"))
        connection.execute("INSERT INTO schema_versions(version) VALUES (1)")
        connection.execute("INSERT INTO worker_heartbeat VALUES('migration-marker',now(),'idle')")
    db.migrate(settings)
    db.migrate(settings)
    with db.connect(settings) as connection:
        assert connection.execute("SELECT count(*) AS n FROM worker_heartbeat").fetchone()["n"] == 1
        assert connection.execute("SELECT max(version) AS v FROM schema_versions").fetchone()["v"] == 5
        assert connection.execute("SELECT count(*) AS n FROM query_services").fetchone()["n"] == 0


def test_public_function_checks_permissions(service):
    save(service)
    with pytest.raises(ServiceError) as error:
        query_services.run_service(service[0], "by_shop",
                                   ServiceQuery(start_date=service[3], end_date=service[3]),
                                   Principal(admin=False, grants={}))
    assert error.value.status == 403
