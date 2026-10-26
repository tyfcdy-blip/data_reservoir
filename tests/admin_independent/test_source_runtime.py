"""真实本机 HTTP 与 PostgreSQL 测试，外部供应商仅在边界用合成来源隔离。"""

from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
import time
from zoneinfo import ZoneInfo

import pytest

from data_service import db
from data_service.source_registry import load_active_sources
from data_service.worker import acquire_worker_lock, enqueue_schedule, run_worker
from .conftest import register, source_body
from .test_admin_api import save_source


@pytest.fixture
def synthetic_source():
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append({"path": self.path, "payload": payload, "authorization": self.headers.get("Authorization")})
            rows = []
            if payload["pageIndex"] == 0:
                rows = [{"dt": payload["dt"], "id": "001", "shop": "A",
                         "amount": "12.500001" if self.path == "/v1" else "22.500001", "units": 2}]
            encoded = json.dumps({"data": rows}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def wait_for_batch(client, administrator, identifier):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        response = client.get(f"/api/v1/batches/{identifier}", headers=administrator)
        assert response.status_code == 200
        batch = response.json()["data"]
        if batch["status"] in ("succeeded", "failed", "cancelled"):
            assert batch["status"] == "succeeded", {key: batch[key] for key in ("status", "error_code", "error_message")}
            return batch
        time.sleep(.05)
    pytest.fail("合成来源任务未在15秒内完成")


def test_source_test_one_page_without_publishing(admin_service, synthetic_source, business_day):
    settings, client, administrator = admin_service
    register(client, administrator)
    url, requests = synthetic_source
    body = source_body()
    body["config"].update({"url": url + "/v1", "header_env": {"Authorization": "TEST_AUTH"}})
    body["secrets"] = {"TEST_AUTH": "Bearer synthetic-only-secret"}
    save_source(client, administrator, body)
    assert not requests  # 保存配置本身不产生源网络调用。
    response = client.post("/api/v1/admin/sources/ind_sales/test", headers=administrator,
                           json={"business_date": business_day})
    assert response.status_code == 200, response.text
    assert response.json()["data"]["row_count"] == 1
    assert response.json()["data"]["columns"] == ["dt", "id", "shop", "amount", "units"]
    assert len(requests) == 1 and requests[0]["payload"]["pageIndex"] == 0
    assert requests[0]["authorization"] == "Bearer synthetic-only-secret"
    assert "synthetic-only-secret" not in response.text and "12.500001" not in response.text
    with db.connect(settings) as connection:
        assert connection.execute("SELECT COUNT(*) AS count FROM batches").fetchone()["count"] == 0
        assert connection.execute("SELECT COUNT(*) AS count FROM published_days").fetchone()["count"] == 0


def test_running_worker_reloads_saved_source_and_secret(admin_service, synthetic_source, business_day):
    settings, client, administrator = admin_service
    register(client, administrator)
    url, requests = synthetic_source
    body = source_body()
    body["config"].update({"url": url + "/v1", "header_env": {"Authorization": "TEST_AUTH"}})
    body["secrets"] = {"TEST_AUTH": "Bearer synthetic-version-one"}
    save_source(client, administrator, body)
    stop = threading.Event()
    outcomes = []
    failures = []

    def target():
        try:
            outcomes.append(run_worker(settings, stop=stop))
        except Exception as exception:
            failures.append(type(exception).__name__)

    worker = threading.Thread(target=target, daemon=True)
    worker.start()
    try:
        for version in (1, 2):
            if version == 2:
                body["config"]["url"] = url + "/v2"
                body["secrets"]["TEST_AUTH"] = "Bearer synthetic-version-two"
                body["revision"] = 1
                save_source(client, administrator, body)
            response = client.post("/api/v1/admin/datasets/ind_sales/sync", headers=administrator,
                                   json={"start_date": business_day, "end_date": business_day})
            assert response.status_code == 202, response.text
            wait_for_batch(client, administrator, response.json()["data"][0]["id"])
            query = client.post("/api/v1/datasets/ind_sales/query", headers=administrator,
                                json={"start_date": business_day, "end_date": business_day})
            assert query.status_code == 200, query.text
            assert query.json()["data"]["rows"][0]["amount"] == ("12.500001" if version == 1 else "22.500001")
        assert [request["path"] for request in requests] == ["/v1", "/v1", "/v2", "/v2"]
        assert [request["authorization"] for request in requests] == [
            "Bearer synthetic-version-one", "Bearer synthetic-version-one",
            "Bearer synthetic-version-two", "Bearer synthetic-version-two",
        ]
        body["enabled"], body["revision"] = False, 2
        save_source(client, administrator, body)
        response = client.post("/api/v1/admin/datasets/ind_sales/sync", headers=administrator,
                               json={"start_date": business_day, "end_date": business_day})
        assert response.status_code == 409
    finally:
        stop.set()
        worker.join(timeout=15)
    assert not worker.is_alive() and not failures and outcomes == [0]


def test_managed_disable_masks_file_source(admin_service, synthetic_source):
    settings, client, administrator = admin_service
    register(client, administrator)
    url, _ = synthetic_source
    body = source_body(enabled=False)
    body["config"]["url"] = url + "/v1"
    settings.allowed_source_hosts = ["127.0.0.1"]
    Path(settings.source_config_path).write_text(json.dumps({"sources": [body["config"]]}), encoding="utf-8")
    save_source(client, administrator, body)
    assert "ind_sales" not in load_active_sources(settings)


def test_each_source_schedule_is_independent_and_idempotent(admin_service, synthetic_source):
    settings, client, administrator = admin_service
    register(client, administrator)
    url, _ = synthetic_source
    body = source_body(schedule_enabled=True, schedule_hour=0, refresh_days=2)
    body["config"]["url"] = url + "/v1"
    save_source(client, administrator, body)
    assert settings.schedule_enabled is False
    sources = load_active_sources(settings)
    with db.connect(settings) as connection:
        connection.autocommit = True
        assert acquire_worker_lock(connection)
        now = datetime.now(ZoneInfo("Asia/Shanghai"))
        assert enqueue_schedule(connection, settings, sources, now) == 2
        assert enqueue_schedule(connection, settings, sources, now) == 0
        assert connection.execute("SELECT COUNT(*) AS count FROM batches").fetchone()["count"] == 2
