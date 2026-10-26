"""独立验收夹具：专库身份核对、合成数据和真实受控 HTTP 服务。"""
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import threading
import time
from types import SimpleNamespace
from urllib.parse import urlsplit, urlunsplit

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
import pytest

from data_service import auth, db, ingest
from data_service.api import create_app
from data_service.config import Settings
from data_service.datasets import register_dataset
from data_service.models import DatasetDefinition

KEY = "synthetic-independent-model-key"
SLUG = "assistant_review"
ROW_MARKER = "SYNTHETIC_BUSINESS_ROW_NEVER_SEND"
SOURCE_MARKER = "https://synthetic-source.invalid/private"
DAY = datetime.now(ingest.BEIJING).date() - timedelta(days=1)


def assert_database(settings):
    assert urlsplit(settings.database_url).path == "/admin_independent_test"
    assert os.environ.get("ASSISTANT_REVIEW_CONTAINER") == "1"
    with db.connect(settings) as connection:
        assert connection.execute("SELECT current_database() AS name").fetchone()["name"] == "admin_independent_test"


@pytest.fixture(scope="module")
def settings(tmp_path_factory):
    root = Path(__file__).resolve().parents[2]
    configured = Settings(_env_file=root / ".local/test-admin-independent.env")
    parsed = urlsplit(configured.database_url)
    assert parsed.path == "/admin_independent_test"
    assert parsed.hostname == "127.0.0.1" and parsed.port == 15433
    source = tmp_path_factory.mktemp("assistant-independent") / "sources.json"
    source.write_text('{"sources":[]}', encoding="utf-8")
    configured = configured.model_copy(update={
        "database_url": urlunsplit(parsed._replace(netloc=parsed.netloc.rsplit("@", 1)[0] + "@db:5432")),
        "admin_username": "assistant_independent",
        "admin_password": "assistant-independent-test-only-password",
        "config_encryption_key": Fernet.generate_key().decode(),
        "source_config_path": str(source), "schedule_enabled": False,
    })
    assert_database(configured)
    with db.connect(configured) as connection:
        connection.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
    db.migrate(configured)
    auth.initialize_admin(configured)
    definition = DatasetDefinition(
        slug=SLUG, name="独立合成销售验收", date_field="dt", key_fields=["id"],
        fields=[{"name": "dt", "type": "date", "required": True, "description": "合成业务日期"},
                {"name": "id", "type": "string", "required": True, "description": "合成行标识"},
                *[{"name": name, "type": "string", "dimension": True, "description": label}
                  for name, label in [("shop", "店铺"), ("category", "类目"), ("channel", "渠道"), ("owner", "负责人")]],
                {"name": "paid", "type": "decimal", "description": "支付金额"},
                {"name": "refund", "type": "decimal", "description": "成功退款金额"},
                {"name": "source_url", "type": "string", "description": "合成来源定位"}])
    register_dataset(configured, definition)
    register_dataset(configured, definition.model_copy(update={"slug": "other_review", "name": "未选择的表"}))
    rows = [
        {"dt": DAY.isoformat(), "id": str(index), "shop": shop, "category": category, "channel": channel,
         "owner": owner, "paid": paid, "refund": refund, "source_url": SOURCE_MARKER}
        for index, (shop, category, channel, owner, paid, refund) in enumerate([
            (ROW_MARKER, "寝具", "天猫", "甲", "1", "1"),
            (ROW_MARKER, "寝具", "天猫", "甲", "99", "9"),
            ("B", "寝具", "天猫", "甲", "100", "25"),
            ("B", "家具", "天猫", "甲", "50", "5"),
            ("B", "家具", "京东", "甲", "20", "10"),
            ("B", "家具", "京东", "乙", "0", "1")])]
    batch = ingest.create_batch(configured, SLUG, DAY, len(rows), None)
    ingest.append_chunk(configured, batch["id"], 0, rows)
    ingest.commit_batch(configured, batch["id"])
    yield configured
    assert_database(configured)
    with db.connect(configured) as connection:
        connection.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
    db.migrate(configured)


@pytest.fixture(scope="module")
def service_base(settings):
    with TestClient(create_app(settings), raise_server_exceptions=os.environ.get("ASSISTANT_REVIEW_DEBUG") == "1") as client:
        response = client.post("/api/v1/auth/login", json={"username": settings.admin_username, "password": settings.admin_password})
        assert response.status_code == 200
        yield SimpleNamespace(settings=settings, client=client,
            headers={"Authorization": "Bearer " + response.json()["data"]["token"]})


@pytest.fixture
def service(service_base):
    with db.connect(service_base.settings) as connection:
        connection.execute("DELETE FROM llm_settings")
        connection.execute("DELETE FROM query_services")
    return service_base


def valid_draft():
    return {"status": "ready", "name": "四维汇总", "slug": "independent_summary",
        "definition": {"fields": None, "group_by": ["shop", "category", "channel", "owner"],
            "aggregates": [{"field": "paid", "op": "sum", "alias": "paid_total"},
                           {"field": "refund", "op": "sum", "alias": "refund_total"}],
            "formulas": [{"alias": "refund_rate", "expression": "refund_total / paid_total", "decimal_places": 6}],
            "order_by": [{"field": "refund_rate", "direction": "desc"}]},
        "allowed_filter_fields": ["shop", "category", "channel", "owner"], "max_limit": 100,
        "questions": [], "notes": ["按四个维度汇总后计算退款率。"]}


@pytest.fixture
def provider():
    state = SimpleNamespace(mode="ok", document={"connection": "ok"}, requests=[], entered=threading.Event())
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"
        def log_message(self, *_args):
            pass
        def do_POST(self):
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            state.requests.append({"path": self.path, "headers": dict(self.headers), "body": json.loads(raw)})
            state.entered.set()
            mode = state.mode
            if mode == "slow":
                time.sleep(6)
            elif mode == "delay":
                time.sleep(0.8)
            if mode in ("401", "429", "500", "redirect"):
                self.send_response(302 if mode == "redirect" else int(mode))
                if mode == "redirect":
                    self.send_header("Location", state.url + "/redirected")
                self.end_headers()
                body = ("UPSTREAM_PRIVATE_ERROR " + KEY).encode()
            else:
                message = json.dumps(state.document, ensure_ascii=False)
                envelope = {"choices": [{"finish_reason": "length" if mode == "length" else "stop",
                                         "message": {"content": message}}]}
                body = json.dumps(envelope, ensure_ascii=False).encode()
                if mode == "invalid_json":
                    body = b"NOT_JSON_PRIVATE_ERROR"
                elif mode == "duplicate":
                    body = b'{"choices":[],"choices":[]}'
                elif mode == "secret":
                    body = json.dumps({"choices": [{"message": {"content": json.dumps({"leak": KEY})}}]}).encode()
                elif mode in ("large", "large_stream"):
                    body = b"x" * 300000
                elif mode == "truncated":
                    body = body[:40]
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                if mode == "encoded":
                    self.send_header("Content-Encoding", "gzip")
                if mode != "large_stream":
                    self.send_header("Content-Length", str(len(body) + (100 if mode == "truncated" else 0)))
                self.end_headers()
            try:
                if mode == "slow_stream":
                    for offset in range(0, len(body), 10):
                        self.wfile.write(body[offset:offset + 10])
                        self.wfile.flush()
                        time.sleep(1)
                else:
                    self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    state.url = "http://127.0.0.1:" + str(server.server_port) + "/v1"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield state
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def configuration(provider, **changes):
    return {"base_url": provider.url, "model": "independent-synthetic", "api_key": KEY,
            "enabled": True, "timeout_seconds": 5, "token_parameter": "max_tokens", "revision": 0, **changes}


def save(service, provider, **changes):
    return service.client.put("/api/v1/admin/llm", headers=service.headers, json=configuration(provider, **changes))


def generate(service, prompt="按店铺类目渠道负责人汇总支付金额和退款金额，并按退款率降序"):
    return service.client.post("/api/v1/admin/query-services/draft", headers=service.headers,
                               json={"dataset_slug": SLUG, "prompt": prompt})



