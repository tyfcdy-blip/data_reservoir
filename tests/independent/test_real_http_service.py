"""在新端口启动实际ASGI服务，通过TCP验证第三方系统取数链路。"""

import os
from pathlib import Path
import socket
import subprocess
import sys
from time import monotonic, sleep

import httpx


def test_real_tcp_api_accepts_machine_import_and_range_aggregation(independent_settings, dataset_definitions, day):
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    environment = os.environ.copy()
    environment.update({"DS_DATABASE_URL": independent_settings.database_url, "PYTHONIOENCODING": "utf-8"})
    process = subprocess.Popen([sys.executable, "-m", "uvicorn", "data_service.api:create_app", "--factory", "--host", "127.0.0.1", "--port", str(port)],
                               cwd=Path(__file__).resolve().parents[2], env=environment,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8")
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=5, trust_env=False) as client:
            deadline = monotonic() + 15
            while monotonic() < deadline:
                assert process.poll() is None, "独立API子进程异常退出"
                try:
                    if client.get("/health/ready").status_code == 200:
                        break
                except httpx.ConnectError:
                    pass
                sleep(0.1)
            else:
                raise AssertionError("独立API未在时限内就绪")
            assert client.get("/api/v1/datasets").status_code == 401
            login = client.post("/api/v1/auth/login", json={"username": independent_settings.admin_username, "password": independent_settings.admin_password})
            assert login.status_code == 200
            admin = {"Authorization": "Bearer " + login.json()["data"]["token"]}
            for definition in dataset_definitions:
                assert client.post("/api/v1/admin/datasets", headers=admin, json=definition).status_code == 201
            slug = dataset_definitions[0]["slug"]
            key = client.post("/api/v1/admin/keys", headers=admin, json={"name": "TCP独立取数", "grants": {slug: ["read", "write"]}})
            assert key.status_code == 201
            headers = {"X-API-Key": key.json()["data"]["api_key"]}
            creation = client.post(f"/api/v1/datasets/{slug}/batches", headers=headers,
                                   json={"business_date": str(day), "expected_rows": 2, "idempotency_key": "tcp-push"})
            assert creation.status_code == 201
            batch_id = creation.json()["data"]["id"]
            records = [{"dt": str(day), "product_id": identifier, "shop": "实体店", "amount": amount, "units": 1}
                       for identifier, amount in [("001", "0.100001"), ("002", "0.200002")]]
            assert client.put(f"/api/v1/batches/{batch_id}/chunks/0", headers=headers, json={"rows": records}).status_code == 200
            assert client.post(f"/api/v1/batches/{batch_id}/commit", headers=headers).status_code == 200
            result = client.post(f"/api/v1/datasets/{slug}/query", headers=headers,
                                 json={"start_date": str(day), "end_date": str(day), "group_by": ["shop"],
                                       "aggregates": [{"op": "sum", "field": "amount", "alias": "total_amount"}]})
            assert result.status_code == 200
            assert result.json()["data"]["rows"] == [{"shop": "实体店", "total_amount": "0.300003"}]
            assert client.post("/api/v1/auth/logout", headers=admin).status_code == 200
            assert client.get("/api/v1/admin/keys", headers=admin).status_code == 401
    finally:
        process.terminate()
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate(timeout=5)
