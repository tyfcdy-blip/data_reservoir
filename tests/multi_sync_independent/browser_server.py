"""真实浏览器验收专用服务；合成账号和专用库，不启动 worker。"""
import os
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
import uvicorn

from data_service import auth, db
from data_service.api import create_app
from data_service.config import Settings
from data_service.datasets import register_dataset
from data_service.models import DatasetDefinition
from data_service.source_registry import SourceSave, save_source


def build_app():
    root = Path(__file__).resolve().parents[2]
    settings = Settings(_env_file=root / '.local/test-admin-independent.env')
    parsed = urlsplit(settings.database_url)
    assert parsed.path == '/admin_independent_test'
    assert os.environ.get('MULTI_SYNC_REVIEW_CONTAINER') == '1'
    userinfo = parsed.netloc.rsplit('@', 1)[0]
    settings = settings.model_copy(update={
        'database_url': urlunsplit(parsed._replace(netloc=userinfo + '@db:5432')),
        'admin_username': 'multi_review', 'admin_password': 'synthetic-multi-review-2026!',
        'allowed_source_hosts': [], 'schedule_enabled': False,
        'source_config_path': str(root / 'config/sources.json'),
    })
    with db.connect(settings) as connection:
        connection.execute('DROP SCHEMA public CASCADE; CREATE SCHEMA public')
    db.migrate(settings)
    auth.initialize_admin(settings)
    register_dataset(settings, DatasetDefinition(slug='review_orders', name='多时点合成验收', date_field='day', key_fields=['id'], fields=[
        {'name': 'day', 'type': 'date', 'required': True, 'description': '业务日期'},
        {'name': 'id', 'type': 'string', 'required': True, 'description': '唯一行标识'},
    ]))
    save_source(settings, 'review_orders', SourceSave.model_validate({
        'title': '旧小时配置验收', 'enabled': False, 'approve_host': True, 'schedule_hour': 5,
        'config': {'dataset_slug': 'review_orders', 'url': 'https://source.test/rows'},
    }))
    return create_app(settings)


if __name__ == '__main__':
    uvicorn.run(build_app(), host='0.0.0.0', port=18995, access_log=False, log_level='warning')
