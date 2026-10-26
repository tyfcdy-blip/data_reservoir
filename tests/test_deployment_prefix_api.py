"""复用隔离库夹具，验证前缀路由上的真实认证、导入、金额查询及撤销。"""

from fastapi.testclient import TestClient

from data_service.api import create_app
from test_api import day, query_body, register

pytest_plugins = ["test_api"]


def test_prefixed_authenticated_data_lifecycle(service):
    configured, original_client, admin = service
    register(original_client, admin)
    prefix = "/data-service"
    with TestClient(create_app(configured.model_copy(update={"root_path": prefix})), root_path=prefix) as client:
        login = client.post(prefix + "/api/v1/auth/login", json={
            "username": configured.admin_username, "password": configured.admin_password})
        assert login.status_code == 200
        headers = {"Authorization": "Bearer " + login.json()["data"]["token"]}
        key = client.post(prefix + "/api/v1/admin/keys", headers=headers, json={
            "name": "isolated-prefix-test", "grants": {"orders": ["read", "write"]}})
        assert key.status_code == 201
        api_headers = {"X-API-Key": key.json()["data"]["api_key"]}
        response = client.post(prefix + "/api/v1/datasets/orders/batches", headers=api_headers, json={
            "business_date": day(), "expected_rows": 1, "idempotency_key": "prefix-test"})
        assert response.status_code == 201
        batch_path = prefix + "/api/v1/batches/" + response.json()["data"]["id"]
        assert client.put(batch_path + "/chunks/0", headers=api_headers, json={"rows": [
            {"day": day(), "id": "001", "shop": "测试店铺", "amount": "123.456789"}]}).status_code == 200
        assert client.post(batch_path + "/commit", headers=api_headers).status_code == 200
        query_path = prefix + "/api/v1/datasets/orders/query"
        result = client.post(query_path, headers=api_headers, json=query_body())
        assert result.status_code == 200
        assert result.json()["data"]["rows"][0]["amount"] == "123.456789"
        assert client.get(prefix + "/api/v1/admin/keys", headers=api_headers).status_code == 401
        assert client.delete(prefix + "/api/v1/admin/keys/" + key.json()["data"]["id"],
                             headers=headers).status_code == 200
        assert client.post(query_path, headers=api_headers, json=query_body()).status_code == 401
        assert client.post(prefix + "/api/v1/auth/logout", headers=headers).status_code == 200
        assert client.get(prefix + "/api/v1/admin/status", headers=headers).status_code == 401
