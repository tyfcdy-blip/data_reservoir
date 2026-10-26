"""多次同步独立验收，只清理和写入 admin_independent_test。"""
import os
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest
from data_service import db
from data_service.config import Settings
from data_service.datasets import register_dataset
from data_service.ingest import BEIJING
from data_service.models import DatasetDefinition


@pytest.fixture
def settings(tmp_path):
    root = Path(__file__).resolve().parents[2]
    configured = Settings(_env_file=root / '.local' / 'test-admin-independent.env')
    parsed = urlsplit(configured.database_url)
    assert parsed.path == '/admin_independent_test'
    assert os.environ.get('MULTI_SYNC_REVIEW_CONTAINER') == '1', '只能通过 Docker 测试环境执行'
    assert parsed.hostname == '127.0.0.1' and parsed.port == 15433
    userinfo = parsed.netloc.rsplit('@', 1)[0]
    source_path = tmp_path / 'sources.json'
    source_path.write_text('{"sources":[]}', encoding='utf-8')
    configured = configured.model_copy(update={
        'database_url': urlunsplit(parsed._replace(netloc=userinfo + '@db:5432')),
        'source_config_path': str(source_path), 'schedule_enabled': False,
        'allowed_source_hosts': ['source.test'],
    })
    with db.connect(configured) as connection:
        connection.execute('DROP SCHEMA public CASCADE; CREATE SCHEMA public')
    db.migrate(configured)
    yield configured
    with db.connect(configured) as connection:
        connection.execute('DROP SCHEMA public CASCADE; CREATE SCHEMA public')
    db.migrate(configured)


@pytest.fixture
def today():
    return datetime.now(BEIJING).date()


@pytest.fixture
def business_day(today):
    return today - timedelta(days=1)


@pytest.fixture
def definition():
    return DatasetDefinition(slug='review_orders', name='独立合成验收表', date_field='day', key_fields=['id'], fields=[
        {'name': 'day', 'type': 'date', 'required': True, 'description': '业务日期'},
        {'name': 'id', 'type': 'string', 'required': True, 'description': '唯一行标识'},
        {'name': 'amount', 'type': 'decimal', 'description': '合成金额'},
        {'name': 'shop', 'type': 'string', 'dimension': True, 'description': '合成店铺'},
    ])


@pytest.fixture
def service(settings, definition):
    register_dataset(settings, definition)
    return settings

