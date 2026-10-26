"""DDL01/DDL02 API：真实管理员认证、只读预览与已有创建协议兼容，专用测试库。"""

import hashlib

from psycopg import sql
import pytest

from data_service import db
from test_api import key, register
from test_api import service as service
from test_ddl_parser import DASHBOARD_DDL

PATH = "/api/v1/admin/datasets/ddl-preview"


def database_fingerprint(settings):
    digest = hashlib.sha256()
    with db.connect(settings) as connection:
        connection.execute("SET TRANSACTION READ ONLY")
        tables = connection.execute("SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename").fetchall()
        for table in tables:
            name = table["tablename"]
            digest.update(name.encode())
            records = connection.execute(sql.SQL("SELECT row_to_json(r)::text AS value FROM {} r ORDER BY 1").format(sql.Identifier(name)))
            for record in records:
                digest.update(record["value"].encode())
    return digest.hexdigest()


def test_preview_uses_admin_auth_and_does_not_write_any_database_rows(service):
    settings, client, admin = service
    register(client, admin)
    machine, _identifier = key(client, admin, {"orders": ["read", "write"]})
    before = database_fingerprint(settings)
    for headers in ({}, machine, {"Authorization": "Bearer invalid"}):
        response = client.post(PATH, json={"sql": DASHBOARD_DDL}, headers=headers)
        assert response.status_code == 401
    response = client.post(PATH, json={"sql": DASHBOARD_DDL}, headers=admin)
    assert response.status_code == 200, response.text
    assert response.json()["code"] == 200
    assert len(response.json()["data"]["fields"]) == 50
    assert response.json()["data"]["key_fields"] == []
    assert response.headers["cache-control"] == "no-store"
    assert database_fingerprint(settings) == before
    assert client.post("/api/v1/auth/logout", headers=admin).status_code == 200
    assert client.post(PATH, json={"sql": DASHBOARD_DDL}, headers=admin).status_code == 401


@pytest.mark.parametrize("payload", [
    {}, {"sql": None}, {"sql": 1}, {"sql": []}, {"sql": ""}, {"sql": " "},
    {"sql": "CREATE TABLE t (a INT)", "execute": True},
    {"sql": "CREATE TABLE t (a INT); DROP TABLE auth_admin;"},
    {"sql": "CREATE TABLE t (a INT)" + " " * 65536},
    {"sql": "CREATE TABLE t (a INT)" + "中" * 22000},
])
def test_invalid_requests_return_422_without_database_mutation(service, payload):
    settings, client, admin = service
    before = database_fingerprint(settings)
    response = client.post(PATH, json=payload, headers=admin)
    assert response.status_code == 422, response.text
    assert "DROP TABLE" not in response.text
    assert "auth_admin" not in response.text
    assert database_fingerprint(settings) == before


def test_comment_sql_is_preserved_only_as_data_and_parse_error_is_not_echoed(service):
    settings, client, admin = service
    before = database_fingerprint(settings)
    statement = "CREATE TABLE t (a INT COMMENT '说明; DROP TABLE auth_admin;')"
    response = client.post(PATH, headers=admin, json={"sql": statement})
    assert response.status_code == 200
    assert response.json()["data"]["fields"][0]["description"] == "说明; DROP TABLE auth_admin;"
    invalid = client.post(PATH, headers=admin, json={"sql": statement + "; secret_value_ABC"})
    assert invalid.status_code == 422
    assert invalid.json()["error_code"] == "DDL_INVALID"
    assert "secret_value_ABC" not in invalid.text
    assert database_fingerprint(settings) == before


def test_reviewed_draft_creates_through_existing_api_and_preserves_50_descriptions(service):
    settings, client, admin = service
    preview = client.post(PATH, headers=admin, json={"sql": DASHBOARD_DDL}).json()["data"]
    for field in preview["fields"]:
        if field["name"] == "dt":
            field.update(type="date", required=True, dimension=True)
        if field["name"] == "daily_id":
            field["required"] = True
    definition = {"slug": preview["table_name"], "name": "商品驾驶舱日表", "date_field": "dt",
                  "fields": preview["fields"], "key_fields": ["daily_id"], "retention_days": 180}
    response = client.post("/api/v1/admin/datasets", headers=admin, json=definition)
    assert response.status_code == 201, response.text
    with db.connect(settings) as connection:
        record = connection.execute("SELECT table_name,definition FROM datasets WHERE slug=%s", (definition["slug"],)).fetchone()
        assert record["definition"]["key_fields"] == ["daily_id"]
        comments = connection.execute(
            "SELECT a.attname,col_description(a.attrelid,a.attnum) AS description "
            "FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid "
            "WHERE c.relname=%s AND a.attnum>0 AND NOT a.attisdropped", (record["table_name"],)).fetchall()
    by_name = {column["attname"]: column["description"] for column in comments}
    assert all(by_name[field["name"]] == field["description"] for field in definition["fields"])
    schema = client.get("/openapi.json").json()
    assert schema["paths"][PATH]["post"]["security"] == [{"AdminSession": []}]
