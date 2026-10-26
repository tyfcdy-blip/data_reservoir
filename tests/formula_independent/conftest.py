"""独立公式验收：只使用指定的 admin_independent_test 数据库。"""
import os
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from fastapi.testclient import TestClient
import pytest

from data_service import auth, db, ingest
from data_service.api import create_app
from data_service.config import Settings
from data_service.datasets import register_dataset
from data_service.models import DatasetDefinition

SLUG = 'formula_review'


def definition(slug=SLUG):
    return DatasetDefinition(slug=slug, name='公式独立合成验收', date_field='dt', key_fields=['id'], fields=[
        {'name': 'dt', 'type': 'date', 'required': True, 'description': '合成业务日期'},
        {'name': 'id', 'type': 'string', 'required': True, 'description': '合成唯一行'},
        {'name': 'shop', 'type': 'string', 'dimension': True, 'description': '合成分组'},
        {'name': 'paid', 'type': 'decimal', 'description': '合成支付金额'},
        {'name': 'refund', 'type': 'decimal', 'description': '合成退款金额'},
        {'name': 'units', 'type': 'integer', 'description': '合成整数'},
        {'name': 'flag', 'type': 'boolean', 'description': '类型拒绝校验字段'},
    ])


def publish(settings, slug, day, rows):
    batch = ingest.create_batch(settings, slug, day, len(rows), None)
    for index in range(0, len(rows), 1000):
        ingest.append_chunk(settings, batch['id'], index // 1000, rows[index:index + 1000])
    ingest.commit_batch(settings, batch['id'])


@pytest.fixture(scope='module')
def settings(tmp_path_factory):
    root = Path(__file__).resolve().parents[2]
    configured = Settings(_env_file=root / '.local/test-admin-independent.env')
    parsed = urlsplit(configured.database_url)
    assert parsed.path == '/admin_independent_test'
    assert os.environ.get('FORMULA_REVIEW_CONTAINER') == '1', '仅允许专用 Docker 测试环境'
    assert parsed.hostname == '127.0.0.1' and parsed.port == 15433
    source = tmp_path_factory.mktemp('formula') / 'sources.json'
    source.write_text('{"sources":[]}', encoding='utf-8')
    configured = configured.model_copy(update={
        'database_url': urlunsplit(parsed._replace(netloc=parsed.netloc.rsplit('@', 1)[0] + '@db:5432')),
        'admin_username': 'formula_review_admin', 'admin_password': 'formula-review-test-only-password',
        'source_config_path': str(source), 'schedule_enabled': False,
    })
    with db.connect(configured) as connection:
        connection.execute('DROP SCHEMA public CASCADE; CREATE SCHEMA public')
    db.migrate(configured)
    auth.initialize_admin(configured)
    yield configured
    with db.connect(configured) as connection:
        connection.execute('DROP SCHEMA public CASCADE; CREATE SCHEMA public')
    db.migrate(configured)


@pytest.fixture(scope='module')
def day():
    return datetime.now(ingest.BEIJING).date() - timedelta(days=1)


@pytest.fixture(scope='module')
def service(settings, day):
    register_dataset(settings, definition())
    values = [('A','1','1',1), ('A','99','9',7), ('B','100','25',2), ('C','0','5',0),
              ('D',None,None,None), ('E','40','-4',2), ('F','200','50',4), ('G','3','1',3), ('H','1','0',1)]
    rows = [{'dt': day.isoformat(), 'id': str(index), 'shop': shop, 'paid': paid,
             'refund': refund, 'units': units, 'flag': False}
            for index, (shop, paid, refund, units) in enumerate(values)]
    publish(settings, SLUG, day, rows)
    with TestClient(create_app(settings), raise_server_exceptions=False) as client:
        response = client.post('/api/v1/auth/login', json={
            'username': settings.admin_username, 'password': settings.admin_password})
        assert response.status_code == 200
        token = response.json()['data']['token']
        yield settings, client, {'Authorization': 'Bearer ' + token}, day
