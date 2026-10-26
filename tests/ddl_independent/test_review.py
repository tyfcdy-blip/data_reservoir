"""D4独立验收：仅合成数据与admin_independent_test；不访问真实来源。"""

from datetime import datetime, timedelta
from decimal import Decimal
import hashlib
import json
import os
import random
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient
import httpx
import pytest
from psycopg import sql

from data_service import auth, db
from data_service.api import create_app
from data_service.config import Settings
from data_service.ddl import DDLParseError, parse_ddl
from data_service.ingest import create_batch
from data_service.source import SourceClient, SourceConfig
from data_service.worker import acquire_worker_lock, claim_next_job, process_job

PREVIEW = "/api/v1/admin/datasets/ddl-preview"
DDL = """CREATE TABLE isolated_cockpit (
 daily_id VARCHAR(80) COMMENT '稳定行标识',
 dt DATETIME COMMENT '业务日期',
 product_id VARCHAR(40) COMMENT '商品编号',
 owner_person VARCHAR(80) COMMENT '负责人',
 pay_amount DOUBLE COMMENT '支付金额',
 net_sales_amount VARCHAR(1048576) COMMENT '净销售额',
 notes TEXT COMMENT '备注, 分号; 括号()和''引号'
) COMMENT '独立测试表' DISTRIBUTED BY RANDOM BUCKETS 4"""


@pytest.fixture
def isolated(tmp_path):
    settings = Settings(_env_file=Path(__file__).resolve().parents[2] / ".local/test-admin-independent.env")
    assert urlsplit(settings.database_url).path == "/admin_independent_test", "拒绝非隔离数据库"
    if os.environ.get("DDL_DOCKER_TEST") == "1":
        settings = settings.model_copy(
            update={"database_url": settings.database_url.replace("127.0.0.1:15433", "db:5432")}
        )
    source_file = tmp_path / "empty.json"
    source_file.write_text('{"sources":[]}', encoding="utf-8")
    settings = settings.model_copy(
        update={
            "source_config_path": str(source_file),
            "schedule_enabled": False,
            "admin_username": "ddl_independent",
            "admin_password": "ddl-independent-local-only-2026!",
            "allowed_source_hosts": ["isolated.example"],
        }
    )
    with db.connect(settings) as connection:
        connection.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
    db.migrate(settings)
    auth.initialize_admin(settings)
    with TestClient(create_app(settings), raise_server_exceptions=False) as client:
        reply = client.post(
            "/api/v1/auth/login",
            json={"username": settings.admin_username, "password": settings.admin_password},
        )
        assert reply.status_code == 200
        yield settings, client, {"Authorization": "Bearer " + reply.json()["data"]["token"]}


def fingerprint(settings):
    digest = hashlib.sha256()
    with db.connect(settings) as connection:
        connection.execute("SET TRANSACTION READ ONLY")
        tables = connection.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename"
        ).fetchall()
        for table in tables:
            digest.update(table["tablename"].encode())
            rows = connection.execute(
                sql.SQL("SELECT row_to_json(r)::text AS value FROM {} r ORDER BY 1").format(
                    sql.Identifier(table["tablename"])
                )
            )
            for row in rows:
                digest.update(row["value"].encode())
    return digest.hexdigest()


def reviewed(client, admin):
    response = client.post(PREVIEW, headers=admin, json={"sql": DDL})
    assert response.status_code == 200
    draft = response.json()["data"]
    assert draft["key_fields"] == []
    fields = draft["fields"]
    for field in fields:
        if field["name"] == "dt":
            field.update(type="date", required=True, dimension=True)
        if field["name"] == "daily_id":
            field.update(required=True)
        if field["name"] == "net_sales_amount":
            field.update(type="decimal")
        if field["name"] == "owner_person":
            field.update(dimension=True)
    body = {
        "slug": "isolated_cockpit",
        "name": "独立测试驾驶舱",
        "fields": fields,
        "date_field": "dt",
        "key_fields": ["daily_id"],
    }
    created = client.post("/api/v1/admin/datasets", headers=admin, json=body)
    assert created.status_code == 201, created.text
    return body


@pytest.mark.parametrize(
    "suffix",
    [
        ";DROP TABLE auth_admin;",
        ";SELECT pg_sleep(1)",
        " UNION SELECT 1",
        " COPY x TO PROGRAM 'echo bad'",
        " AS SELECT 1",
        " /*;*/",
        " -- comment",
        "; COMMIT;",
        " DISTRIBUTED BY RANDOM BUCKETS 4; DELETE FROM datasets",
    ],
)
def test_no_suffix_can_be_executed_or_mutate_database(isolated, suffix):
    settings, client, admin = isolated
    before = fingerprint(settings)
    reply = client.post(PREVIEW, headers=admin, json={"sql": "CREATE TABLE x (a INT)" + suffix})
    assert reply.status_code == 422
    assert "pg_sleep" not in reply.text and "DROP TABLE" not in reply.text
    assert fingerprint(settings) == before


def test_preview_auth_repeated_read_only_and_comment_roundtrip(isolated):
    settings, client, admin = isolated
    before = fingerprint(settings)
    assert client.post(PREVIEW, json={"sql": DDL}).status_code == 401
    assert client.post(PREVIEW, headers={"Authorization": "Bearer bad"}, json={"sql": DDL}).status_code == 401
    results = [client.post(PREVIEW, headers=admin, json={"sql": DDL}) for _ in range(5)]
    assert all(result.status_code == 200 for result in results)
    assert all(result.json()["data"] == results[0].json()["data"] for result in results)
    assert results[0].json()["data"]["fields"][-1]["description"] == "备注, 分号; 括号()和'引号"
    assert fingerprint(settings) == before


@pytest.mark.parametrize("field", ["pay_amount", "dt", "missing"])
def test_saved_conversion_cannot_target_non_text_and_is_atomic(isolated, field):
    settings, client, admin = isolated
    reviewed(client, admin)
    before = fingerprint(settings)
    body = {
        "title": "独立来源",
        "config": {
            "dataset_slug": "isolated_cockpit",
            "url": "https://isolated.example/rows",
            "text_whitespace_fields": [field],
        },
        "secrets": {},
        "approve_host": True,
        "revision": 0,
    }
    reply = client.put("/api/v1/admin/sources/isolated_cockpit", headers=admin, json=body)
    assert reply.status_code == 422 and reply.json()["error_code"] == "TEXT_CONVERSION_INVALID"
    assert fingerprint(settings) == before


def test_real_postgres_worker_normalizes_selected_field_then_upserts_and_recovers(isolated):
    settings, client, admin = isolated
    reviewed(client, admin)
    day = datetime.now(ZoneInfo("Asia/Shanghai")).date() - timedelta(days=1)
    fields = ["daily_id", "dt", "product_id", "owner_person", "pay_amount", "net_sales_amount", "notes"]
    config = SourceConfig(
        dataset_slug="isolated_cockpit",
        url="https://isolated.example/rows",
        page_size=2,
        field_mapping={name: name for name in fields},
        date_fields_from_datetime=["dt"],
        text_whitespace_fields=["owner_person"],
        requests_per_second=20,
    )

    def row(identifier, owner, amount):
        return {
            "daily_id": identifier,
            "dt": day.isoformat() + " 00:00:00",
            "product_id": "0000123",
            "owner_person": owner,
            "pay_amount": amount,
            "net_sales_amount": "1.000001",
            "notes": None,
        }

    def run(rows):
        def upstream(request):
            page = json.loads(request.content)["pageIndex"]
            return httpx.Response(200, json={"data": rows[page * 2 : (page + 1) * 2]})

        created = create_batch(
            settings, "isolated_cockpit", day, None, None, mode="pull", write_mode="upsert"
        )
        with db.connect(settings) as connection:
            connection.autocommit = True
            assert acquire_worker_lock(connection)
            active = claim_next_job(connection)
            assert active["id"] == created["id"]
            with SourceClient(config, settings, transport=httpx.MockTransport(upstream)) as source:
                success = process_job(connection, settings, active, source)
        return success, created["id"]

    def published():
        with db.connect(settings) as connection:
            table = connection.execute(
                "SELECT table_name FROM datasets WHERE slug='isolated_cockpit'"
            ).fetchone()["table_name"]
            return connection.execute(
                sql.SQL(
                    "SELECT r.* FROM {} r JOIN published_days p ON r._batch_id=p.batch_id ORDER BY daily_id"
                ).format(sql.Identifier(table))
            ).fetchall()

    assert run([row("00001", " 甲\r\n乙\t丙 ", "0.000001"), row("00002", None, "999.123456")])[0]
    current = published()
    assert len(current) == 2
    assert current[0]["owner_person"] == " 甲 乙 丙 " and current[1]["owner_person"] is None
    assert current[0]["product_id"] == "0000123" and current[0]["pay_amount"] == Decimal("0.000001")
    assert run([row("00001", "甲\r乙", "2.000002")])[0]
    current = published()
    assert len(current) == 2 and current[0]["pay_amount"] == Decimal("2.000002")
    assert current[1]["daily_id"] == "00002" and current[1]["pay_amount"] == Decimal("999.123456")
    before = [(r["daily_id"], r["owner_person"], r["pay_amount"]) for r in current]
    invalid = row("00003", "甲\x0b乙", "3")
    success, batch_id = run([row("00001", "新\n名", "8"), invalid])
    assert success is False
    assert [(r["daily_id"], r["owner_person"], r["pay_amount"]) for r in published()] == before
    with db.connect(settings) as connection:
        failed = connection.execute(
            "SELECT status,error_code FROM batches WHERE id=%s", (batch_id,)
        ).fetchone()
        assert failed["status"] == "failed" and failed["error_code"] == "invalid_record"
    assert run([row("00003", "甲\n乙", "3")])[0] and len(published()) == 3


def test_parser_adversarial_mutation_is_bounded_and_errors_are_typed():
    generator = random.Random(20261024)
    alphabet = "ab(),;'`\\\" \r\n\t0123456789中--/*=\x00"
    for _ in range(3000):
        statement = (
            "CREATE TABLE t ("
            + "".join(generator.choice(alphabet) for _ in range(generator.randint(0, 160)))
            + ")"
        )
        try:
            parsed = parse_ddl(statement)
            assert 1 <= len(parsed.fields) <= 80
        except DDLParseError:
            pass


def test_comments_do_not_enable_expressions_or_silently_round_types():
    parsed = parse_ddl(
        "CREATE TABLE t (id VARCHAR(1048576) COMMENT '<svg/onload=alert(1)>', dt DATETIME(6), value DOUBLE) COMMENT 'SELECT 1;'"
    )
    assert parsed.fields[0].max_length == 10000 and parsed.fields[0].description == "<svg/onload=alert(1)>"
    assert parsed.fields[1].type == "datetime" and not parsed.fields[1].required
    assert parsed.key_fields == []
    assert any("不会截断" in message for message in parsed.warnings)
    assert any("不会自动舍入" in message for message in parsed.warnings)
