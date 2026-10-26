"""schema 2 到 3 的真实迁移与失败回滚，不碰业务库。"""
from importlib.resources import files
from uuid import uuid4

from psycopg import sql
from psycopg.types.json import Jsonb
import psycopg
import pytest

from data_service import db
from data_service.datasets import register_dataset
from data_service.ingest import _digest
from data_service.source import SourceConfig
from tests.multi_sync_independent.helpers import SLUG, rows


def prepare_v2(settings, definition, business_day):
    with db.connect(settings) as connection:
        connection.execute('DROP SCHEMA public CASCADE; CREATE SCHEMA public')
        connection.execute(files('data_service').joinpath('schema.sql').read_text(encoding='utf-8'))
        connection.execute(files('data_service').joinpath('migration_2.sql').read_text(encoding='utf-8'))
        connection.execute('INSERT INTO schema_versions(version) VALUES(1),(2)')
    register_dataset(settings, definition)
    identifier = uuid4()
    with db.connect(settings) as connection:
        config = SourceConfig(dataset_slug=SLUG, url='https://source.test/rows')
        connection.execute('INSERT INTO managed_sources(dataset_slug,title,definition,encrypted_secrets,approved_host,schedule_enabled,schedule_hour,refresh_days,revision) '
                           'VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)', (SLUG, '旧版合成来源', Jsonb(config.model_dump(mode='json')), 'synthetic-encrypted-placeholder', 'source.test', False, 5, 7, 9))
        connection.execute("INSERT INTO auth_admin(id,username,password_hash) VALUES(1,'synthetic-admin','synthetic-hash')")
        connection.execute("INSERT INTO batches(id,dataset_slug,business_date,mode,status,row_count) VALUES(%s,%s,%s,'push','succeeded',1)", (identifier, SLUG, business_day))
        connection.execute('INSERT INTO published_days(dataset_slug,business_date,batch_id,row_count) VALUES(%s,%s,%s,1)', (SLUG, business_day, identifier))
        name = connection.execute('SELECT table_name FROM datasets WHERE slug=%s', (SLUG,)).fetchone()['table_name']
        connection.execute(sql.SQL('INSERT INTO {}(_batch_id,_date,_key,day,id,amount,shop) VALUES(%s,%s,%s,%s,%s,%s,%s)').format(sql.Identifier(name)),
                           (identifier, business_day, _digest(['A']), business_day, 'A', '1.230000', '店铺甲'))
    return identifier


def test_schema2_migration_preserves_rows_accounts_and_source(settings, definition, business_day):
    identifier = prepare_v2(settings, definition, business_day)
    before_rows = rows(settings, business_day)
    db.migrate(settings)
    db.migrate(settings)
    with db.connect(settings) as connection:
        source = connection.execute('SELECT * FROM managed_sources WHERE dataset_slug=%s', (SLUG,)).fetchone()
        assert source['schedule_times'] == ['05:00'] and source['write_mode'] == 'replace_day'
        assert source['revision'] == 9 and source['encrypted_secrets'] == 'synthetic-encrypted-placeholder'
        assert not source['schedule_enabled']
        batch = connection.execute('SELECT * FROM batches WHERE id=%s', (identifier,)).fetchone()
        assert batch['write_mode'] == 'replace_day' and batch['published_row_count'] == batch['row_count'] == 1
        assert connection.execute('SELECT password_hash FROM auth_admin').fetchone()['password_hash'] == 'synthetic-hash'
        assert connection.execute('SELECT MAX(version) AS v FROM schema_versions').fetchone()['v'] == 5
    assert rows(settings, business_day) == before_rows


def test_migration_sql_failure_rolls_back_columns_version_and_data(settings, definition, business_day, monkeypatch):
    prepare_v2(settings, definition, business_day)
    before_rows = rows(settings, business_day)
    original_files = db.files
    class BrokenResource:
        def joinpath(self, name):
            resource = original_files('data_service').joinpath(name)
            class Content:
                def read_text(self, **kwargs):
                    return resource.read_text(**kwargs) + '\nSELECT independent_missing_column FROM schema_versions;'
            return Content()
    with monkeypatch.context() as scoped:
        scoped.setattr(db, 'files', lambda package: BrokenResource())
        with pytest.raises(psycopg.errors.UndefinedColumn):
            db.migrate(settings)
    with db.connect(settings) as connection:
        assert connection.execute('SELECT MAX(version) AS v FROM schema_versions').fetchone()['v'] == 2
        assert connection.execute("SELECT COUNT(*) AS n FROM information_schema.columns WHERE table_schema='public' AND table_name='batches' AND column_name='write_mode'").fetchone()['n'] == 0
        assert connection.execute('SELECT revision FROM managed_sources').fetchone()['revision'] == 9
    assert rows(settings, business_day) == before_rows
    db.migrate(settings)
    assert rows(settings, business_day) == before_rows
