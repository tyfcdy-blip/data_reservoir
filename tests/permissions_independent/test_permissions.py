"""独立权限验收：真实FastAPI、真实隔离PG，核心鉴权/SQL/公式未Mock。"""

import copy
from pathlib import Path
from decimal import Decimal
from importlib.resources import files

from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb
import pytest

from data_service import db
from data_service.api import create_app
from .fixtures import initialize, USERNAME, PASSWORD


@pytest.fixture
def isolated(tmp_path):
    settings, period, aggregate = initialize(tmp_path)
    with TestClient(create_app(settings), raise_server_exceptions=False) as client:
        login = client.post("/api/v1/auth/login", json={"username": USERNAME, "password": PASSWORD})
        assert login.status_code == 200
        yield (
            settings,
            client,
            {"Authorization": "Bearer " + login.json()["data"]["token"]},
            period,
            aggregate,
        )


def scope(*groups):
    return {
        "mode": "filtered",
        "groups": [
            {
                "conditions": [
                    {"field": field, "values": values if isinstance(values, list) else [values]}
                    for field, values in group.items()
                ]
            }
            for group in groups
        ],
    }


def policy(*groups, services=None):
    return {
        "services": services
        if services is not None
        else {"sales_rate": "sales", "sales_rows": "sales", "fixed_b": "sales"},
        "row_scopes": {"sales": scope(*groups)},
    }


def issue(env, selected=None, grants=None, **extra):
    payload = {
        "name": "独立测试调用者",
        "grants": {} if grants is None else grants,
        "requests_per_minute": 600,
        **extra,
    }
    if selected is not None:
        payload["policy"] = selected
    response = env[1].post("/api/v1/admin/keys", headers=env[2], json=payload)
    assert response.status_code == 201, response.text
    body = response.json()["data"]
    return {"X-API-Key": body["api_key"]}, body


def fetch(env, key, service="sales_rows", **options):
    return env[1].post("/api/v1/services/" + service + "/query", headers=key, json={**env[3], **options})


def direct(env, key, **options):
    return env[1].post("/api/v1/datasets/sales/query", headers=key, json={**env[3], **options})


def data(response):
    assert response.status_code == 200, response.text
    return response.json()["data"]


def ids(response):
    return [record["id"] for record in data(response)["rows"]]


def test_two_store_keys_and_hidden_scope_columns(isolated):
    a, _ = issue(isolated, policy({"channel": "C1", "shop_name": "A"}))
    b, _ = issue(isolated, policy({"channel": "C1", "shop_name": "B"}))
    assert ids(fetch(isolated, a)) == ["001", "002", "009", "010"]
    assert ids(fetch(isolated, b)) == ["003", "011"]
    assert data(fetch(isolated, a))["columns"] == ["id", "pay_amount"]
    for headers, expected in [(a, [3, 1]), (b, [1, 1])]:
        result = data(fetch(isolated, headers))
        assert result["total"] == sum(expected)
        assert [entry["row_count"] for entry in result["published_versions"]] == expected


def test_service_only_keys_cannot_escape_via_other_entrypoints(isolated):
    settings, client, admin, period, _ = isolated
    key, _ = issue(isolated, policy({"shop_name": "A"}))
    assert data(client.get("/api/v1/datasets", headers=key)) == []
    assert client.get("/api/v1/datasets/sales", headers=key).status_code == 403
    assert direct(isolated, key).status_code == 403
    assert (
        client.post(
            "/api/v1/query/batch", headers=key, json={"queries": [{"dataset": "sales", "query": period}]}
        ).status_code
        == 403
    )
    assert fetch(isolated, key, "unselected").status_code == 403
    assert fetch(isolated, key, "other_service").status_code == 403
    directory = data(client.get("/api/v1/services", headers=key))
    assert {record["slug"] for record in directory} == {"sales_rate", "sales_rows", "fixed_b"}
    assert all("definition" not in record and "filters" not in record for record in directory)
    assert client.get("/api/v1/admin/keys", headers=key).status_code == 401
    assert client.get("/api/v1/admin/status", headers=key).status_code == 401
    assert (
        client.post(
            "/api/v1/datasets/sales/batches",
            headers=key,
            json={"business_date": period["end_date"], "expected_rows": 0, "idempotency_key": "cannot-write"},
        ).status_code
        == 403
    )


def test_direct_and_batch_queries_share_same_scope_without_service_inheritance(isolated):
    settings, client, admin, period, aggregate = isolated
    key, _ = issue(isolated, policy({"channel": "C1", "shop_name": "A"}, services={}), {"sales": ["read"]})
    assert ids(direct(isolated, key, fields=["id"])) == ["001", "002", "009", "010"]
    assert data(client.get("/api/v1/services", headers=key)) == []
    assert fetch(isolated, key).status_code == 403
    listed = data(client.get("/api/v1/datasets", headers=key))
    assert [record["slug"] for record in listed] == ["sales"]
    assert "table_name" not in listed[0]
    results = data(
        client.post(
            "/api/v1/query/batch",
            headers=key,
            json={
                "queries": [
                    {"dataset": "sales", "query": {**period, "fields": ["id"]}},
                    {"dataset": "sales", "query": {**period, **aggregate}},
                ]
            },
        )
    )
    assert results[0]["total"] == 4 and results[1]["total"] == 3
    assert [entry["row_count"] for entry in results[1]["published_versions"]] == [3, 1]
    rejected = client.post(
        "/api/v1/query/batch",
        headers=key,
        json={
            "queries": [{"dataset": "sales", "query": period}, {"dataset": "other_sales", "query": period}]
        },
    )
    assert rejected.status_code == 403 and "rows" not in rejected.json()


def test_or_groups_preserve_channel_shop_pair_and_owner_intersection(isolated):
    key, _ = issue(isolated, policy({"channel": "C1", "shop_name": "A"}, {"channel": "C2", "shop_name": "B"}))
    assert ids(fetch(isolated, key)) == ["001", "002", "005", "009", "010"]
    owner, _ = issue(isolated, policy({"channel": "C1", "shop_name": "A", "owner_person": "甲"}))
    assert ids(fetch(isolated, owner)) == ["001", "002", "010"]
    multi, _ = issue(isolated, policy({"channel": "C1", "shop_name": ["A", "B"]}))
    assert ids(fetch(isolated, multi)) == ["001", "002", "003", "009", "010", "011"]


def test_request_filters_intersect_fixed_and_security_filters(isolated):
    key, _ = issue(isolated, policy({"channel": "C1", "shop_name": "A"}))
    assert ids(fetch(isolated, key, filters=[{"field": "shop_name", "op": "eq", "value": "B"}])) == []
    assert ids(fetch(isolated, key, filters=[{"field": "shop_name", "op": "in", "value": ["A", "B"]}])) == [
        "001",
        "002",
        "009",
        "010",
    ]
    assert ids(fetch(isolated, key, filters=[{"field": "shop_name", "op": "is_null", "value": True}])) == []
    assert ids(fetch(isolated, key, "fixed_b")) == []
    # 把policy放进请求体不能替换保存在服务器的范围。
    response = fetch(isolated, key, policy={"row_scopes": {"sales": {"mode": "all"}}})
    assert response.status_code == 422


def test_aggregation_formula_sort_pagination_and_result_filter_after_scope(isolated):
    _, client, _, period, aggregate = isolated
    selected = policy({"channel": "C1", "shop_name": "A"})
    key, _ = issue(isolated, selected, {"sales": ["read"]})
    result = data(fetch(isolated, key, "sales_rate", limit=1))
    assert result["total"] == 3 and len(result["rows"]) == 1
    assert result["rows"][0] == {
        "category": "Y",
        "payments": "300.000000",
        "refunds": "90.000000",
        "refund_rate": "0.300000",
    }
    assert data(fetch(isolated, key, "sales_rate", limit=1, offset=1))["rows"][0]["refund_rate"] == "0.100000"
    assert data(fetch(isolated, key, "sales_rate", limit=1, offset=2))["rows"][0]["refund_rate"] is None
    result = data(
        direct(
            isolated, key, **aggregate, result_filters=[{"field": "refund_rate", "op": "gte", "value": "0.2"}]
        )
    )
    assert result["total"] == 1 and result["rows"][0]["category"] == "Y"
    total = data(direct(isolated, key, aggregates=aggregate["aggregates"], formulas=aggregate["formulas"]))[
        "rows"
    ][0]
    assert Decimal(total["payments"]) == Decimal("450") and Decimal(total["refunds"]) == Decimal("110")
    assert total["refund_rate"] == "0.244444"


def test_exact_match_injection_and_unknown_store_do_not_widen_scope(isolated):
    injection, _ = issue(isolated, policy({"shop_name": "A' OR 1=1 --"}))
    assert ids(fetch(isolated, injection)) == ["008"]
    empty, _ = issue(isolated, policy({"shop_name": "missing"}))
    result = data(fetch(isolated, empty))
    assert result["rows"] == [] and result["total"] == 0
    assert all(record["row_count"] == 0 for record in result["published_versions"])
    lower, _ = issue(isolated, policy({"shop_name": "a"}))
    assert ids(fetch(isolated, lower)) == []


def test_explicit_all_and_legacy_null_compatibility(isolated):
    key, _ = issue(isolated, {"services": {"sales_rows": "sales"}, "row_scopes": {"sales": {"mode": "all"}}})
    assert data(fetch(isolated, key))["total"] == 11
    legacy, old = issue(isolated, grants={"sales": ["read", "write"]})
    assert data(direct(isolated, legacy))["total"] == 11
    assert fetch(isolated, legacy, "unselected").status_code == 200
    assert len(data(isolated[1].get("/api/v1/services", headers=legacy))) == 4
    with db.connect(isolated[0]) as connection:
        row = connection.execute("SELECT policy,grants FROM api_keys WHERE id=%s", (old["id"],)).fetchone()
        assert row["policy"] is None and row["grants"] == {"sales": ["read", "write"]}
    listed = data(isolated[1].get("/api/v1/admin/keys", headers=isolated[2]))
    assert all("api_key" not in entry and "token_hash" not in entry for entry in listed)


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_scope",
        "extra_scope",
        "empty_groups",
        "empty_conditions",
        "empty_values",
        "null_value",
        "blank_value",
        "duplicate_values",
        "duplicate_field",
        "unknown_field",
        "non_dimension",
        "numeric",
        "unknown_service",
        "wrong_binding",
        "write",
        "empty_authorization",
        "oversized_groups",
        "oversized_values",
        "injected_identifier",
    ],
)
def test_invalid_policy_rejected_atomically(isolated, mutation):
    selected = policy({"shop_name": "A"})
    grants = {}
    condition = selected["row_scopes"]["sales"]["groups"][0]["conditions"][0]
    if mutation == "missing_scope":
        selected["row_scopes"] = {}
    elif mutation == "extra_scope":
        selected["row_scopes"]["other_sales"] = {"mode": "all"}
    elif mutation == "empty_groups":
        selected["row_scopes"]["sales"]["groups"] = []
    elif mutation == "empty_conditions":
        selected["row_scopes"]["sales"]["groups"][0]["conditions"] = []
    elif mutation == "empty_values":
        condition["values"] = []
    elif mutation == "null_value":
        condition["values"] = [None]
    elif mutation == "blank_value":
        condition["values"] = [" "]
    elif mutation == "duplicate_values":
        condition["values"] = ["A", "A"]
    elif mutation == "duplicate_field":
        selected["row_scopes"]["sales"]["groups"][0]["conditions"].append(copy.deepcopy(condition))
    elif mutation == "unknown_field":
        condition["field"] = "missing"
    elif mutation == "non_dimension":
        condition["field"] = "private_note"
    elif mutation == "numeric":
        condition["field"] = "pay_amount"
    elif mutation == "unknown_service":
        selected["services"]["missing"] = "sales"
    elif mutation == "wrong_binding":
        selected["services"]["sales_rows"] = "other_sales"
    elif mutation == "write":
        grants = {"sales": ["write"]}
    elif mutation == "empty_authorization":
        selected = {"services": {}, "row_scopes": {}}
    elif mutation == "oversized_groups":
        selected["row_scopes"]["sales"]["groups"] *= 21
    elif mutation == "oversized_values":
        condition["values"] = [str(index) for index in range(101)]
    elif mutation == "injected_identifier":
        condition["field"] = "shop_name) OR true --"
    before = data(isolated[1].get("/api/v1/admin/keys", headers=isolated[2]))
    response = isolated[1].post(
        "/api/v1/admin/keys",
        headers=isolated[2],
        json={"name": "invalid", "grants": grants, "policy": selected},
    )
    assert response.status_code == 422, response.text
    assert data(isolated[1].get("/api/v1/admin/keys", headers=isolated[2])) == before


@pytest.mark.parametrize(
    "tamper", ["unknown_field", "empty_scopes", "json_null", "bad_type", "non_dimension"]
)
def test_stored_policy_corruption_fails_closed(isolated, tamper):
    settings, client, admin, period, aggregate = isolated
    selected = policy({"shop_name": "A"})
    key, created = issue(isolated, selected, {"sales": ["read"]})
    with db.connect(settings) as connection:
        if tamper == "unknown_field":
            selected["row_scopes"]["sales"]["groups"][0]["conditions"][0]["field"] = "removed_field"
        elif tamper == "empty_scopes":
            selected["row_scopes"] = {}
        elif tamper == "json_null":
            selected = None
        elif tamper == "bad_type":
            selected = []
        elif tamper == "non_dimension":
            definition = connection.execute("SELECT definition FROM datasets WHERE slug='sales'").fetchone()[
                "definition"
            ]
            next(field for field in definition["fields"] if field["name"] == "shop_name")["dimension"] = False
            connection.execute("UPDATE datasets SET definition=%s WHERE slug='sales'", (Jsonb(definition),))
        connection.execute("UPDATE api_keys SET policy=%s WHERE id=%s", (Jsonb(selected), created["id"]))
    for response in [
        fetch(isolated, key),
        direct(isolated, key),
        client.get("/api/v1/datasets", headers=key),
        client.get("/api/v1/services", headers=key),
    ]:
        assert response.status_code == 403, response.text
        assert response.json()["error_code"] == "KEY_POLICY_INVALID"
        assert "rows" not in response.json()


def test_service_rebinding_or_disabled_never_grants_other_table(isolated):
    settings, client, admin, period, _ = isolated
    key, _ = issue(isolated, policy({"shop_name": "A"}))
    with db.connect(settings) as connection:
        connection.execute("UPDATE query_services SET dataset_slug='other_sales' WHERE slug='sales_rows'")
    response = fetch(isolated, key)
    assert response.status_code in [403, 404] and "rows" not in response.json()
    # 任何列举成功的服务不得把绑定后的新表暴露出来。
    directory = client.get("/api/v1/services", headers=key)
    if directory.status_code == 200:
        assert "sales_rows" not in {record["slug"] for record in directory.json()["data"]}
    else:
        assert directory.status_code == 403


def test_revocation_expiration_and_rate_limit_retain_boundary(isolated):
    settings, client, admin, _, _ = isolated
    key, created = issue(isolated, policy({"shop_name": "A"}), requests_per_minute=1)
    assert fetch(isolated, key).status_code == 200
    assert fetch(isolated, key).status_code == 429
    key, created = issue(isolated, policy({"shop_name": "A"}))
    assert client.delete("/api/v1/admin/keys/" + created["id"], headers=admin).status_code == 200
    assert fetch(isolated, key).status_code == 401
    key, created = issue(isolated, policy({"shop_name": "A"}))
    with db.connect(settings) as connection:
        connection.execute(
            "UPDATE api_keys SET expires_at=now()-interval '1 second' WHERE id=%s", (created["id"],)
        )
    assert fetch(isolated, key).status_code == 401


def test_migration_v4_is_additive_idempotent_and_transaction_rollback(isolated):
    settings, client, admin, period, _ = isolated
    old, issued = issue(isolated, grants={"sales": ["read"]})
    with db.connect(settings) as connection:
        connection.execute("ALTER TABLE api_keys DROP COLUMN policy")
        connection.execute("DELETE FROM schema_versions WHERE version=5")
        before = connection.execute("SELECT id,name,token_hash,grants FROM api_keys").fetchall()
        coverage = connection.execute(
            "SELECT dataset_slug,business_date,batch_id,row_count FROM published_days ORDER BY 1,2"
        ).fetchall()
    # 故意回滚迁移SQL：列和版本都不应残留，也不影响原key和发布记录。
    with db.connect(settings) as connection:
        connection.execute(files("data_service").joinpath("migration_5.sql").read_text(encoding="utf-8"))
        connection.execute("INSERT INTO schema_versions(version) VALUES(5)")
        connection.rollback()
        assert (
            connection.execute(
                "SELECT count(*) AS n FROM information_schema.columns WHERE table_name='api_keys' AND column_name='policy'"
            ).fetchone()["n"]
            == 0
        )
    db.migrate(settings)
    db.migrate(settings)
    with db.connect(settings) as connection:
        assert connection.execute("SELECT max(version) AS v FROM schema_versions").fetchone()["v"] == 5
        assert connection.execute("SELECT id,name,token_hash,grants FROM api_keys").fetchall() == before
        assert (
            connection.execute(
                "SELECT dataset_slug,business_date,batch_id,row_count FROM published_days ORDER BY 1,2"
            ).fetchall()
            == coverage
        )
        assert connection.execute("SELECT policy FROM api_keys").fetchone()["policy"] is None
    assert data(direct(isolated, old))["total"] == 11


def test_concurrent_keys_never_share_scope_or_results(isolated):
    from concurrent.futures import ThreadPoolExecutor

    key_a, _ = issue(isolated, policy({"channel": "C1", "shop_name": "A"}))
    key_b, _ = issue(isolated, policy({"channel": "C1", "shop_name": "B"}))

    def query_once(index):
        chosen = key_a if index % 2 == 0 else key_b
        expected = ["001", "002", "009", "010"] if index % 2 == 0 else ["003", "011"]
        assert ids(fetch(isolated, chosen)) == expected

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(query_once, range(40)))


def test_forty_thousand_rows_and_max_policy_values_remain_scoped(isolated):
    from datetime import date
    from time import perf_counter
    import json
    from data_service import ingest

    settings, client, admin, period, aggregate = isolated
    day = date.fromisoformat(period["start_date"])
    batch = ingest.create_batch(settings, "sales", day, 40000, "large-independent-permissions")
    started = perf_counter()
    for chunk in range(40):
        rows = [
            {
                "id": f"load-{index:06}",
                "dt": day.isoformat(),
                "channel": "C1",
                "shop_name": "A" if index % 10 == 0 else "B",
                "owner_person": "甲",
                "category": "X",
                "pay_amount": "100.000001",
                "refund_amount": "10.000001",
                "private_note": None,
            }
            for index in range(chunk * 1000, (chunk + 1) * 1000)
        ]
        ingest.append_chunk(settings, batch["id"], chunk, rows)
    ingest.commit_batch(settings, batch["id"])
    ingest_seconds = perf_counter() - started
    groups = [{"shop_name": [f"unknown-{group}-{index}" for index in range(100)]} for group in range(20)]
    groups[0]["shop_name"][0] = "A"
    key, _ = issue(isolated, policy(*groups), {"sales": ["read"]})
    started = perf_counter()
    result = data(
        direct(
            isolated,
            key,
            aggregates=[
                {"op": "sum", "field": "pay_amount", "alias": "payments"},
                {"op": "count", "alias": "records"},
            ],
        )
    )
    duration = perf_counter() - started
    assert result["rows"] == [{"payments": "400000.004000", "records": 4001}]
    assert [entry["row_count"] for entry in result["published_versions"]] == [4000, 1]
    # 记录实际样本，不能将单机合成测试宣称为生产吞吐或响应承诺。
    evidence = {
        "records_added": 40000,
        "policy_groups": 20,
        "policy_values": 2000,
        "scoped_records": 4001,
        "query_seconds": round(duration, 6),
        "ingest_seconds": round(ingest_seconds, 6),
        "scope": "本机真实PostgreSQL、合成记录与最大允许policy值；非生产性能SLA",
    }
    (Path(__file__).resolve().parents[2] / "evidence/permissions-independent-load.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8"
    )
