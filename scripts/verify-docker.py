"""真实隔离Docker链路：两种HTTP源→worker→独立PG→API密钥→多表批量，并验重启/恢复。"""

from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import time
from zoneinfo import ZoneInfo

from dotenv import dotenv_values
import httpx

ROOT = Path(__file__).resolve().parents[1]
PREFIX = ["docker", "compose", "--env-file", ".local/e2e.env", "-f", "tests/e2e/compose.yml"]


def command(arguments, expected=0):
    process = subprocess.run(arguments, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if process.returncode != expected:
        raise AssertionError("本地验证子命令失败，退出码=" + str(process.returncode))
    return process.stdout


def sql(database, statement):
    return command(PREFIX + ["exec", "-T", "db", "psql", "-X", "-A", "-t", "-U", "data_service",
                             "-d", database, "-v", "ON_ERROR_STOP=1", "-c", statement])


def fingerprint(database):
    names = sql(database, "SELECT table_name FROM datasets ORDER BY table_name").decode().split()
    assert all(re.fullmatch(r"records_[a-f0-9]{32}", name) for name in names)
    tables = {"datasets": "slug", "batches": "id", "published_days": "dataset_slug,business_date",
              "auth_admin": "id", "api_keys": "id", "schema_versions": "version",
              "managed_sources": "dataset_slug", "query_services": "slug", **{name: "_batch_id,_key" for name in names}}
    return {table: hashlib.sha256(sql(database, f'SELECT row_to_json(t)::text FROM (SELECT * FROM "{table}" ORDER BY {order}) t')).hexdigest()
            for table, order in tables.items()}


def login(client, config):
    result = client.post("/api/v1/auth/login", json={"username": config["DS_ADMIN_USERNAME"], "password": config["DS_ADMIN_PASSWORD"]})
    assert result.status_code == 200
    return {"Authorization": "Bearer " + result.json()["data"]["token"]}


def main():
    config = dotenv_values(ROOT / ".local/e2e.env")
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    start, end = (today-timedelta(days=3)).isoformat(), (today-timedelta(days=1)).isoformat()
    with httpx.Client(base_url="http://127.0.0.1:18090", timeout=20, trust_env=False) as client:
        admin = login(client, config)
        assert client.get("/api/v1/datasets").status_code == 401
        assert client.get("/api/business/dashboard").status_code == 404
        source = client.get("/api/v1/admin/sources/inventory_daily", headers=admin).json()["data"]
        managed = client.put("/api/v1/admin/sources/inventory_daily", headers=admin, json={
            "title": "隔离容器库存接入", "config": source["config"],
            "secrets": {"E2E_INVENTORY_TOKEN": config["E2E_INVENTORY_TOKEN"]},
            "enabled": True, "approve_host": True, "schedule_enabled": False,
            "schedule_hour": 3, "refresh_days": 7, "revision": source["revision"]})
        assert managed.status_code == 200
        existing = client.get("/api/v1/admin/query-services/inventory_by_warehouse", headers=admin)
        revision = existing.json()["data"]["revision"] if existing.status_code == 200 else 0
        saved = client.put("/api/v1/admin/query-services/inventory_by_warehouse", headers=admin, json={
            "name": "隔离容器仓库汇总", "dataset_slug": "inventory_daily",
            "definition": {"group_by": ["warehouse"], "aggregates": [
                {"op": "sum", "field": "available_qty", "alias": "stock"}]},
            "allowed_filter_fields": ["warehouse"], "max_limit": 100, "enabled": True, "revision": revision})
        assert saved.status_code == 200
        for slug in ["product_daily", "inventory_daily"]:
            result = client.post(f"/api/v1/admin/datasets/{slug}/sync", headers=admin,
                                 json={"start_date": start, "end_date": end})
            assert result.status_code == 202, result.text
        deadline = time.monotonic() + 60
        while True:
            status = client.get("/api/v1/admin/status", headers=admin).json()["data"]
            if any(batch["status"] == "failed" for batch in status["recent_batches"]):
                raise AssertionError("真实同步任务失败")
            if all(batch["status"] == "succeeded" for batch in status["recent_batches"]):
                break
            assert time.monotonic() < deadline, "worker处理超时"
            time.sleep(.2)
        issue = client.post("/api/v1/admin/keys", headers=admin, json={"name": "isolated-e2e-consumer", "grants": {
            "product_daily": ["read"], "inventory_daily": ["read"]}})
        assert issue.status_code == 201
        key = {"X-API-Key": issue.json()["data"]["api_key"]}
        request = {"queries": [
            {"dataset": "product_daily", "query": {"start_date": start, "end_date": end,
             "aggregates": [{"op": "sum", "field": "pay_amount", "alias": "paid"},
                            {"op": "sum", "field": "profit_amount", "alias": "profit"}, {"op": "count", "alias": "records"}]}},
            {"dataset": "inventory_daily", "query": {"start_date": end, "end_date": end,
             "group_by": ["warehouse"], "aggregates": [{"op": "sum", "field": "available_qty", "alias": "stock"}]}},
        ]}
        result = client.post("/api/v1/query/batch", headers=key, json=request)
        assert result.status_code == 200, result.text
        before = result.json()["data"]
        assert before[0]["rows"] == [{"paid": "240.000000", "profit": "75.000000", "records": 9}]
        assert before[1]["rows"] == [{"warehouse": "仓库A", "stock": "7"}, {"warehouse": "仓库B", "stock": "3"}]
        detail = client.post("/api/v1/datasets/product_daily/query", headers=key,
                             json={"start_date": end, "end_date": end, "filters": [{"field": "shop_name", "op": "eq", "value": " 自营 "}]}).json()["data"]
        assert detail["total"] == 2 and detail["rows"][0]["product_id"] == "0001"
        assert client.get("/api/v1/admin/status", headers=key).status_code == 401
        named_path = "/api/v1/services/inventory_by_warehouse/query"
        named_request = {"start_date": end, "end_date": end}
        named_before = client.post(named_path, headers=key, json=named_request).json()["data"]
        assert named_before["rows"] == before[1]["rows"]
        command(PREFIX + ["stop", "api", "worker"])
        command(PREFIX + ["restart", "db"])
        command(PREFIX + ["up", "-d", "--wait"])
        login(client, config)
        after = client.post("/api/v1/query/batch", headers=key, json=request).json()["data"]
        assert before == after, "重启后发布结果或密钥发生变化"
        assert client.post(named_path, headers=key, json=named_request).json()["data"] == named_before
        tested = client.post("/api/v1/admin/sources/inventory_daily/test", headers=admin, json={"business_date": end})
        assert tested.status_code == 200 and tested.json()["data"]["row_count"] == 2
    stamp = datetime.now().strftime("%Y%m%d%H%M%S%f")
    backup = ROOT / ".local/backups" / ("e2e-" + stamp + ".dump")
    target = "ds_restore_e2e_" + stamp
    baseline = fingerprint("standalone_e2e_test")
    snapshot = [sys.executable, "scripts/database-snapshot.py", "--test-environment"]
    command(snapshot + ["backup", "--output", str(backup)])
    command(snapshot + ["restore", "--input", str(backup), "--database", target])
    restored = fingerprint(target)
    assert baseline == restored
    command(snapshot + ["restore", "--input", str(backup), "--database", target], expected=2)
    report = {"exit_code": 0, "range": [start, end], "datasets": 2, "published_days": 6,
              "facts": {"product_daily": 9, "inventory_daily": 6}, "paid": "240.000000", "bill_profit": "75.000000",
              "real_http_sources": ["signed_vendor_post", "mapped_generic_get"], "query_batch_same_after_restart": before == after,
              "secret_key_persisted_after_restart": True, "restore_fingerprints_equal": baseline == restored,
              "managed_source_secrets_work_after_restart": True, "named_query_rules_persist_after_restart": True,
              "restored_tables": len(baseline), "repeat_restore_refused_exit": 2,
              "scope": "专用Docker项目与合成HTTP源，未调用真实第三方"}
    (ROOT / "evidence/admin-docker-e2e.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=True))


if __name__ == "__main__":
    main()
