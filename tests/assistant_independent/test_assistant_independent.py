"""模型助手独立验收：真实 PostgreSQL 与真实 HTTP；不调用任何外部模型。"""
from concurrent.futures import ThreadPoolExecutor
import copy
from decimal import Decimal
import hashlib
from importlib.resources import files
import json
import time

from cryptography.fernet import Fernet
import pytest

from data_service import access, db, llm_config, query
from data_service.errors import ServiceError
from data_service.llm_config import LlmSave

from conftest import DAY, KEY, ROW_MARKER, SLUG, SOURCE_MARKER, configuration, generate, save, valid_draft


def model_test(service, provider, **changes):
    return service.client.post("/api/v1/admin/llm/test", headers=service.headers,
                               json=configuration(provider, **changes))


def test_config_encryption_omitted_key_clear_revision(service, provider):
    assert service.client.get("/api/v1/admin/llm", headers=service.headers).json()["data"]["revision"] == 0
    saved = save(service, provider)
    assert saved.status_code == 200, saved.text
    assert saved.json()["data"]["has_api_key"] is True
    assert KEY not in saved.text and "encrypted_api_key" not in saved.text and '"api_key"' not in saved.text
    with db.connect(service.settings) as connection:
        record = connection.execute("SELECT * FROM llm_settings").fetchone()
    assert record["encrypted_api_key"] != KEY
    assert Fernet(service.settings.config_encryption_key).decrypt(record["encrypted_api_key"].encode()).decode() == KEY
    payload = configuration(provider, revision=1)
    payload.pop("api_key")
    kept = service.client.put("/api/v1/admin/llm", headers={**service.headers, "Content-Type": "application/json"}, content=json.dumps(payload, ensure_ascii=True))
    assert kept.status_code == 200
    with llm_config.call_configuration(service.settings) as resolved:
        assert resolved.api_key == KEY
    conflict = save(service, provider, revision=1)
    assert conflict.status_code == 409 and conflict.json()["error_code"] == "CONFIG_CONFLICT"
    clear = save(service, provider, revision=2, api_key=None, clear_api_key=True)
    assert clear.status_code == 200 and clear.json()["data"]["has_api_key"] is False
    assert model_test(service, provider, revision=3, api_key=None).status_code == 200
    assert "Authorization" not in provider.requests[-1]["headers"]


def test_unsaved_real_http_connection_test(service, provider):
    assert model_test(service, provider, enabled=False, token_parameter="max_completion_tokens").status_code == 200
    request = provider.requests[-1]
    assert request["path"] == "/v1/chat/completions"
    assert request["headers"]["Authorization"] == "Bearer " + KEY
    assert request["body"]["max_completion_tokens"] == 100 and "max_tokens" not in request["body"]
    assert request["body"]["stream"] is False
    assert "dataset" not in json.dumps(request["body"]["messages"])
    assert service.client.get("/api/v1/admin/llm", headers=service.headers).json()["data"]["revision"] == 0


@pytest.mark.parametrize("target", ["http://localhost:9999/v1", "https://127.0.0.1:9999/v1", "http://127.0.0.1:9999/v1"])
def test_changed_target_does_not_send_stored_key(service, provider, target):
    assert save(service, provider).status_code == 200
    for method, path in [("put", "/api/v1/admin/llm"), ("post", "/api/v1/admin/llm/test")]:
        response = getattr(service.client, method)(path, headers=service.headers,
            json=configuration(provider, base_url=target, revision=1, api_key=None))
        assert response.status_code == 422 and response.json()["error_code"] == "LLM_TARGET_CHANGED"
    assert provider.requests == []


def test_config_concurrent_revision_and_request_lock(service, provider):
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: save(service, provider), range(2)))
    assert sorted(response.status_code for response in responses) == [200, 409]
    provider.mode = "delay"
    with ThreadPoolExecutor(max_workers=2) as pool:
        active = pool.submit(model_test, service, provider, revision=1, api_key=None)
        assert provider.entered.wait(timeout=3)
        blocked = model_test(service, provider, revision=1, api_key=None)
        assert blocked.status_code == 429 and blocked.json()["error_code"] == "LLM_BUSY"
        assert active.result(timeout=5).status_code == 200
    provider.mode = "ok"
    assert model_test(service, provider, revision=1, api_key=None).status_code == 200


@pytest.mark.parametrize("method,path", [
    ("get", "/api/v1/admin/llm"), ("put", "/api/v1/admin/llm"),
    ("post", "/api/v1/admin/llm/test"), ("post", "/api/v1/admin/query-services/draft")])
def test_missing_and_dataset_key_cannot_administer(service, provider, method, path):
    key = access.create_key(service.settings, access.KeyCreate(name="独立只读", grants={SLUG: ["read"]}))["api_key"]
    payload = {"dataset_slug": SLUG, "prompt": "汇总"} if path.endswith("/draft") else configuration(provider)
    for headers in ({}, {"X-API-Key": key}, {"Authorization": "Bearer invalid"}):
        response = getattr(service.client, method)(path, headers=headers, **({"json": payload} if method != "get" else {}))
        assert response.status_code == 401
        assert KEY not in response.text
    assert provider.requests == []


@pytest.mark.parametrize("mode,status,code", [
    ("401", 502, "LLM_AUTH_FAILED"), ("429", 503, "LLM_RATE_LIMITED"),
    ("500", 502, "LLM_UPSTREAM_FAILED"), ("redirect", 502, "LLM_UPSTREAM_FAILED"),
    ("invalid_json", 502, "LLM_INVALID_RESPONSE"), ("duplicate", 502, "LLM_INVALID_RESPONSE"),
    ("length", 502, "LLM_INVALID_RESPONSE"), ("secret", 502, "LLM_INVALID_RESPONSE"),
    ("large", 502, "LLM_INVALID_RESPONSE"), ("large_stream", 502, "LLM_INVALID_RESPONSE"),
    ("encoded", 502, "LLM_INVALID_RESPONSE"), ("truncated", 502, "LLM_CONNECTION_FAILED"),
    ("slow", 504, "LLM_TIMEOUT"), ("slow_stream", 504, "LLM_TIMEOUT")])
def test_real_http_failure_redaction_and_recovery(service, provider, mode, status, code):
    provider.mode = mode
    started = time.monotonic()
    response = model_test(service, provider)
    elapsed = time.monotonic() - started
    assert response.status_code == status, response.text
    assert response.json()["error_code"] == code
    assert KEY not in response.text and "PRIVATE_ERROR" not in response.text and provider.url not in response.text
    assert len(provider.requests) == 1
    assert elapsed < 6.5
    provider.mode = "ok"
    assert model_test(service, provider).status_code == 200
    assert len(provider.requests) == 2


def test_transport_unreachable_releases_busy_lock(service, provider):
    # 该端口由夹具分配后已关闭，避免接触未授权远端。
    import socket
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    response = model_test(service, provider, base_url=f"http://127.0.0.1:{port}/v1")
    assert response.status_code == 502 and response.json()["error_code"] == "LLM_CONNECTION_FAILED"
    assert KEY not in response.text
    assert model_test(service, provider).status_code == 200


def test_metadata_only_draft_no_query_no_save_and_real_preview(service, provider, monkeypatch):
    provider.document = valid_draft()
    assert save(service, provider).status_code == 200
    with db.connect(service.settings) as connection:
        original_batches = connection.execute("SELECT count(*) AS n FROM batches").fetchone()["n"]
    with monkeypatch.context() as guard:
        guard.setattr(query, "execute_query", lambda *_a, **_kw: pytest.fail("生成草稿不得执行业务查询"))
        response = generate(service)
    assert response.status_code == 200, response.text
    document = response.json()["data"]
    sent = provider.requests[-1]["body"]
    metadata = json.loads(sent["messages"][1]["content"])
    assert set(metadata) == {"dataset", "request"}
    assert set(metadata["dataset"]) == {"name", "slug", "date_field", "fields"}
    assert all(set(field) == {"name", "type", "dimension", "description"} for field in metadata["dataset"]["fields"])
    for forbidden in (KEY, ROW_MARKER, SOURCE_MARKER, "other_review", "table_name", "database_url", "password", provider.url):
        assert forbidden not in json.dumps(sent, ensure_ascii=False)
    with db.connect(service.settings) as connection:
        assert connection.execute("SELECT count(*) AS n FROM query_services").fetchone()["n"] == 0
        assert connection.execute("SELECT count(*) AS n FROM batches").fetchone()["n"] == original_batches
    preview = service.client.post("/api/v1/admin/query-services/preview", headers=service.headers, json={
        "dataset_slug": SLUG, "definition": document["definition"], "start_date": DAY.isoformat(), "end_date": DAY.isoformat()})
    assert preview.status_code == 200, preview.text
    rows = preview.json()["data"]["rows"]
    assert len(rows) == 5
    first = rows[0]
    assert (first["shop"], first["category"], first["channel"], first["owner"]) == ("B", "家具", "京东", "甲")
    assert Decimal(first["refund_rate"]) == Decimal("0.5")
    combined = next(row for row in rows if row["shop"] == ROW_MARKER)
    assert Decimal(combined["paid_total"]) == 100
    assert Decimal(combined["refund_total"]) == 10
    assert Decimal(combined["refund_rate"]) == Decimal("0.1")  # 先汇总后相除，不能平均行比例
    assert rows[-1]["refund_rate"] is None
    manual = service.client.put("/api/v1/admin/query-services/" + document["slug"], headers=service.headers, json={
        "name": document["name"], "dataset_slug": SLUG, "definition": document["definition"],
        "allowed_filter_fields": document["allowed_filter_fields"], "max_limit": 100, "enabled": False, "revision": 0})
    assert manual.status_code == 200 and manual.json()["data"]["enabled"] is False
    assert save(service, provider, revision=1, enabled=False).status_code == 200
    # 模型关闭后原有手工预览仍正常。
    assert service.client.post("/api/v1/admin/query-services/preview", headers=service.headers, json={
        "dataset_slug": SLUG, "definition": document["definition"], "start_date": DAY.isoformat(), "end_date": DAY.isoformat()}).status_code == 200


@pytest.mark.parametrize("replacement", [
    {"sql": "DROP TABLE datasets"}, {"enabled": True}, {"dataset_slug": "other_review"}, {"max_limit": 501},
    {"definition": {"aggregates": [{"field": "secret_unknown", "op": "sum", "alias": "value"}]}},
    {"definition": {"aggregates": [{"field": "shop", "op": "sum", "alias": "value"}]}},
    {"definition": {"aggregates": [{"field": "paid", "op": "sum", "alias": "value"}],
                    "formulas": [{"alias": "bad", "expression": "__import__('os').system('id')"}]}},
    {"definition": {"aggregates": [{"field": "paid", "op": "sum", "alias": "value"}],
                    "formulas": [{"alias": "bad", "expression": "unknown/value"}]}},
    {"definition": {"fields": ["id"]}}, {"definition": {"group_by": ["id"], "aggregates": [{"op": "count", "alias": "n"}]}},
    {"definition": {"aggregates": [{"field": "paid", "op": "sum", "alias": "value"}],
                    "order_by": [{"field": "DROP TABLE datasets", "direction": "desc"}]}},
    {"allowed_filter_fields": ["paid_total"]}, {"allowed_filter_fields": ["shop", "shop"]},
    {"questions": ["待确认"], "status": "ready"}, {"name": "x\u0000"}, {"notes": ["x" * 1001]},
    {"status": "needs_clarification", "questions": ["选字段"]},
])
def test_invalid_model_contract_rejected_without_write(service, provider, replacement):
    provider.document = {**valid_draft(), **copy.deepcopy(replacement)}
    assert save(service, provider).status_code == 200
    response = generate(service)
    assert response.status_code == 502, response.text
    assert response.json()["error_code"] == "LLM_DRAFT_INVALID"
    assert KEY not in response.text and "DROP TABLE" not in response.text
    assert service.client.get("/api/v1/admin/query-services", headers=service.headers).json()["data"] == []
    with db.connect(service.settings) as connection:
        assert connection.execute("SELECT count(*) AS n FROM datasets").fetchone()["n"] == 2


def test_ambiguous_clarification_never_creates_rule(service, provider):
    assert save(service, provider).status_code == 200
    provider.document = {"status": "needs_clarification", "definition": None, "questions": ["需要按支付日还是退款日统计？"],
                         "notes": ["请明确时间口径。"]}
    response = generate(service)
    assert response.status_code == 200
    result = response.json()["data"]
    assert result["status"] == "needs_clarification" and result["definition"] is None
    assert service.client.get("/api/v1/admin/query-services", headers=service.headers).json()["data"] == []


@pytest.mark.parametrize("payload", [
    {"dataset_slug": SLUG, "prompt": " "}, {"dataset_slug": SLUG, "prompt": "x" * 4001},
    {"dataset_slug": SLUG, "prompt": "\ud800"}, {"dataset_slug": SLUG, "prompt": "x", "sql": "select 1"}])
def test_invalid_request_not_sent(service, provider, payload):
    assert save(service, provider).status_code == 200
    response = service.client.post("/api/v1/admin/query-services/draft", headers={**service.headers, "Content-Type": "application/json"}, content=json.dumps(payload, ensure_ascii=True))
    assert response.status_code == 422 and provider.requests == []


def test_missing_cipher_failure_keeps_configuration(service, provider):
    assert save(service, provider).status_code == 200
    broken = service.settings.model_copy(update={"config_encryption_key": Fernet.generate_key().decode()})
    with pytest.raises(ServiceError) as failure:
        llm_config.save_config(broken, LlmSave(**configuration(provider, revision=1, api_key=None)))
    assert failure.value.code == "LLM_KEY_DECRYPT_FAILED"
    assert KEY not in str(failure.value)
    with llm_config.call_configuration(service.settings) as resolved:
        assert resolved.api_key == KEY
    assert service.client.get("/api/v1/admin/llm", headers=service.headers).json()["data"]["revision"] == 1


def test_schema3_migration_transaction_preserves_legacy_rows(service, provider, monkeypatch):
    assert save(service, provider).status_code == 200
    def fingerprint():
        with db.connect(service.settings) as connection:
            result = []
            for table in ("auth_admin", "datasets", "batches", "published_days"):
                result.append(connection.execute("SELECT row_to_json(t) AS row FROM " + table + " t").fetchall())
            return hashlib.sha256(json.dumps(result, default=str, sort_keys=True).encode()).hexdigest()
    before = fingerprint()
    # 仅对已确认的独立专库构造 schema3；不修改应用迁移脚本。
    with db.connect(service.settings) as connection:
        assert connection.execute("SELECT current_database() AS name").fetchone()["name"] == "admin_independent_test"
        connection.execute("DROP TABLE llm_settings")
        connection.execute("ALTER TABLE api_keys DROP COLUMN policy")
        connection.execute("DELETE FROM schema_versions WHERE version>=4")
    real_files = files
    class InjectedResource:
        def joinpath(self, name):
            if name != "migration_4.sql":
                return real_files("data_service").joinpath(name)
            return self
        def read_text(self, encoding):
            return real_files("data_service").joinpath("migration_4.sql").read_text(encoding=encoding) + "\nTHIS IS INVALID SQL;"
    with monkeypatch.context() as guard:
        guard.setattr(db, "files", lambda _package: InjectedResource())
        with pytest.raises(Exception) as failure:
            db.migrate(service.settings)
        assert "syntax" in str(failure.value).lower()
    with db.connect(service.settings) as connection:
        assert connection.execute("SELECT max(version) AS v FROM schema_versions").fetchone()["v"] == 3
        assert connection.execute("SELECT to_regclass('llm_settings') AS t").fetchone()["t"] is None
    assert fingerprint() == before
    db.migrate(service.settings)
    assert fingerprint() == before
    with db.connect(service.settings) as connection:
        assert connection.execute("SELECT max(version) AS v FROM schema_versions").fetchone()["v"] == 5
        assert connection.execute("SELECT count(*) AS n FROM llm_settings").fetchone()["n"] == 0
    db.migrate(service.settings)
    assert fingerprint() == before




def test_actual_host_header_preserved_after_dns_pin(service, provider):
    from urllib.parse import urlsplit
    parsed = urlsplit(provider.url)
    address = "http://localhost:" + str(parsed.port) + "/v1"
    response = model_test(service, provider, base_url=address)
    assert response.status_code == 200, response.text
    assert provider.requests[-1]["headers"]["Host"] == "localhost:" + str(parsed.port)


def test_formula_cannot_reference_previous_formula(service, provider):
    provider.document = valid_draft()
    provider.document["definition"]["formulas"].append({"alias": "percent", "expression": "refund_rate * 100"})
    assert save(service, provider).status_code == 200
    response = generate(service)
    assert response.status_code == 502 and response.json()["error_code"] == "LLM_DRAFT_INVALID"
    assert service.client.get("/api/v1/admin/query-services", headers=service.headers).json()["data"] == []


@pytest.mark.parametrize("prompt", ["按店铺计算退款率", "按负责人汇总退款金额", "统计有效订单退款金额"])
def test_multiple_refund_candidates_require_clarification(prompt):
    from data_service.query_drafts import refund_clarification
    metadata = {"fields": [
        {"name": "all_refund", "type": "decimal", "description": "退款金额"},
        {"name": "valid_refund", "type": "decimal", "description": "有效成功退款，含代发"},
        {"name": "pre_refund", "type": "decimal", "description": "有效发货前成功退款"}]}
    result = refund_clarification(metadata, prompt)
    assert result["status"] == "needs_clarification" and result["definition"] is None
    assert "all_refund" in result["questions"][0] and "valid_refund" in result["questions"][0]


def test_draft_unconfigured_disabled_and_missing_dataset(service, provider):
    response = generate(service)
    assert response.status_code == 409 and response.json()["error_code"] == "LLM_NOT_ENABLED"
    assert save(service, provider, enabled=False).status_code == 200
    assert generate(service).status_code == 409
    response = service.client.post("/api/v1/admin/query-services/draft", headers=service.headers,
        json={"dataset_slug": "missing_review", "prompt": "按店铺汇总"})
    assert response.status_code == 404 and provider.requests == []


