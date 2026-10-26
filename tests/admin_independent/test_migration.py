"""AC09：真实 PostgreSQL v1→v2 增量迁移、重复初始化与拒绝降级。"""

from importlib.resources import files
from uuid import uuid4

from psycopg import sql

import pytest

from data_service import auth, db, datasets, ingest, query
from data_service.models import DatasetDefinition, QueryRequest


def test_v1_upgrade_preserves_identity_and_dataset(clean_settings, business_day):
    with db.connect(clean_settings) as connection:
        connection.execute(files("data_service").joinpath("schema.sql").read_text(encoding="utf-8"))
        connection.execute("INSERT INTO schema_versions(version) VALUES (1)")
    auth.initialize_admin(clean_settings)
    datasets.register_dataset(clean_settings, DatasetDefinition.model_validate({
        "slug": "migration_keep", "name": "升级保留表", "date_field": "dt", "key_fields": ["id"],
        "fields": [{"name": "dt", "type": "date", "required": True},
                   {"name": "id", "type": "string", "required": True}],
    }))
    # 在旧版本结构上写入真实旧格式，不调用要求新列的新版接入逻辑。
    batch_id = uuid4()
    with db.connect(clean_settings) as connection:
        table = connection.execute("SELECT table_name FROM datasets WHERE slug='migration_keep'").fetchone()["table_name"]
        connection.execute("INSERT INTO batches(id,dataset_slug,business_date,mode,status,row_count,idempotency_key) "
                           "VALUES(%s,'migration_keep',%s,'push','succeeded',1,'migration-record')", (batch_id, business_day))
        connection.execute(sql.SQL("INSERT INTO {}(_batch_id,_date,_key,dt,id) VALUES(%s,%s,%s,%s,%s)").format(sql.Identifier(table)),
                           (batch_id, business_day, ingest._digest(["0000001"]), business_day, "0000001"))
        connection.execute("INSERT INTO published_days(dataset_slug,business_date,batch_id,row_count) "
                           "VALUES('migration_keep',%s,%s,1)", (business_day, batch_id))
    request = QueryRequest(start_date=business_day, end_date=business_day)
    published_before = query.run_query(clean_settings, "migration_keep", request)
    with db.connect(clean_settings) as connection:
        before = connection.execute("SELECT * FROM datasets WHERE slug='migration_keep'").fetchone()
        account = connection.execute("SELECT * FROM auth_admin WHERE id=1").fetchone()
    db.migrate(clean_settings)
    db.migrate(clean_settings)
    with db.connect(clean_settings) as connection:
        assert connection.execute("SELECT * FROM datasets WHERE slug='migration_keep'").fetchone() == before
        assert connection.execute("SELECT * FROM auth_admin WHERE id=1").fetchone() == account
        assert connection.execute("SELECT version FROM schema_versions ORDER BY version").fetchall() == [
            {"version": 1}, {"version": 2}, {"version": 3}, {"version": 4}, {"version": 5},
        ]
        for relation in ("managed_sources", "query_services", before["table_name"]):
            assert connection.execute("SELECT to_regclass(%s) AS relation", (relation,)).fetchone()["relation"]
    assert db.ready(clean_settings)
    assert query.run_query(clean_settings, "migration_keep", request) == published_before
    assert published_before["rows"] == [{"dt": business_day, "id": "0000001"}]


def test_newer_database_refuses_downgrade(clean_settings):
    db.migrate(clean_settings)
    with db.connect(clean_settings) as connection:
        connection.execute("INSERT INTO schema_versions(version) VALUES (99)")
    with pytest.raises(RuntimeError, match="拒绝降级"):
        db.migrate(clean_settings)
    with db.connect(clean_settings) as connection:
        assert connection.execute("SELECT MAX(version) AS version FROM schema_versions").fetchone()["version"] == 99
