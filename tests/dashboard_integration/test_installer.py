"""安装器走真实FastAPI与隔离PG，故障只在HTTP/文件系统边界注入。"""

from contextlib import contextmanager
import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

from fastapi.testclient import TestClient
import httpx
import pytest

from data_service import auth, db, datasets
from data_service.api import create_app
from data_service.config import Settings
from data_service.models import DatasetDefinition


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('dashboard_installer_under_test', ROOT / 'scripts/install-dashboard-services.py')
INSTALLER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(INSTALLER)
TEMPLATES = json.loads((ROOT / 'config/dashboard-services.example.json').read_text('utf-8'))


@pytest.fixture
def install_environment(tmp_path, monkeypatch):
    address = os.environ['DS_TEST_DATABASE_URL']
    assert urlsplit(address).path == '/standalone_query_test', '仅允许隔离专库'
    source_path = tmp_path / 'sources.json'
    source_path.write_text('{"sources":[]}', encoding='utf-8')
    settings = Settings(database_url=address, admin_username='installer_test',
                        admin_password='installer-isolated-password!', source_config_path=str(source_path))
    with db.connect(settings) as connection:
        connection.execute('DROP SCHEMA public CASCADE; CREATE SCHEMA public')
    db.migrate(settings)
    auth.initialize_admin(settings)
    numeric = [entry['field'] for entry in TEMPLATES[0]['definition']['aggregates'] if 'field' in entry]
    dataset = DatasetDefinition.model_validate({
        'slug': 'ads_product_dashboard_daily', 'name': '安装器隔离测试', 'date_field': 'dt', 'key_fields': ['id'],
        'fields': [{'name': 'id', 'type': 'string', 'required': True},
                   {'name': 'dt', 'type': 'date', 'dimension': True, 'required': True},
                   *[{'name': field, 'type': 'string', 'dimension': True}
                     for field in ['channel', 'shop_name', 'category', 'owner_person']],
                   *[{'name': field, 'type': 'decimal'} for field in numeric]]})
    datasets.register_dataset(settings, dataset)
    environment = {'settings': settings, 'calls': [], 'fault': None, 'after_response': None, 'key_path': tmp_path / 'private' / 'key.json'}

    class Bridge:
        def __init__(self, client):
            self.client = client
            self.headers = client.headers

        def request(self, method, path, **kwargs):
            environment['calls'].append((method, path))
            if environment['fault']:
                response = environment['fault'](method, path)
                if response is not None:
                    return response
            response = self.client.request(method, path, **kwargs)
            if environment['after_response']:
                environment['after_response'](method, path, response)
            return response

    @contextmanager
    def client_factory(**kwargs):
        assert kwargs['timeout'] == 60 and kwargs['follow_redirects'] is False
        with TestClient(create_app(settings), raise_server_exceptions=False, base_url=kwargs['base_url']) as client:
            yield Bridge(client)

    monkeypatch.setattr(INSTALLER, 'httpx', SimpleNamespace(Client=client_factory, HTTPError=httpx.HTTPError))
    yield environment
    # 合成测试在失败、撤销故障后也清理独立库，绝不遗留可用测试密钥。
    with db.connect(settings) as connection:
        connection.execute('DROP SCHEMA public CASCADE; CREATE SCHEMA public')


def install(environment, key_file=None):
    settings = environment['settings']
    return INSTALLER.install('http://127.0.0.1:8090', settings.admin_username, settings.admin_password, key_file)


def records(environment, relation):
    assert relation in {'api_keys', 'query_services', 'admin_sessions'}
    with db.connect(environment['settings']) as connection:
        return connection.execute('SELECT * FROM ' + relation).fetchall()


def test_install_and_repeat_reuse_without_revision_changes(install_environment):
    initial = install(install_environment)
    assert len(initial['created']) == 9 and initial['existing_reused'] == []
    repeated = install(install_environment)
    assert repeated['created'] == [] and len(repeated['existing_reused']) == 9
    assert all(row['revision'] == 1 for row in records(install_environment, 'query_services'))
    assert records(install_environment, 'api_keys') == []


def test_key_file_contains_exclusive_readonly_policy_and_result_hides_token(install_environment):
    path = install_environment['key_path']
    result = install(install_environment, path)
    secret = json.loads(path.read_text('utf-8'))
    assert secret['grants'] == {} and len(secret['policy']['services']) == 9
    assert secret['policy']['row_scopes'] == {'ads_product_dashboard_daily': {'mode': 'all', 'groups': []}}
    assert result['key']['id'] == secret['id'] and 'api_key' not in result['key']
    assert secret['api_key'] not in json.dumps(result)
    if os.name != 'nt':
        assert path.stat().st_mode & 0o777 == 0o600
    assert len(records(install_environment, 'api_keys')) == 1


def test_conflict_detected_before_any_creation_or_key_reservation(install_environment):
    from data_service.query_services import QueryServiceSave, save_service
    template = dict(TEMPLATES[-1])
    slug = template.pop('slug')
    template['name'] = '管理员自定义口径'
    save_service(install_environment['settings'], slug, QueryServiceSave.model_validate(template))
    path = install_environment['key_path']
    with pytest.raises(ValueError, match='未覆盖'):
        install(install_environment, path)
    assert not path.exists()
    rows = records(install_environment, 'query_services')
    assert len(rows) == 1 and rows[0]['name'] == '管理员自定义口径' and rows[0]['revision'] == 1
    assert records(install_environment, 'api_keys') == []


def test_existing_file_rejected_before_http_calls(install_environment):
    path = install_environment['key_path']
    path.parent.mkdir()
    path.write_text('existing-secret-must-stay', encoding='utf-8')
    with pytest.raises(ValueError, match='禁止覆盖'):
        install(install_environment, path)
    assert path.read_text('utf-8') == 'existing-secret-must-stay'
    assert install_environment['calls'] == []


def test_key_creation_http_failure_removes_reserved_file(install_environment):
    path = install_environment['key_path']
    install_environment['fault'] = lambda method, route: httpx.Response(503) if method == 'POST' and route.endswith('/admin/keys') else None
    with pytest.raises(RuntimeError, match='密钥签发结果未确认'):
        install(install_environment, path)
    assert not path.exists() and records(install_environment, 'api_keys') == []
    assert len(records(install_environment, 'query_services')) == 9


def fail_key_write(monkeypatch, path):
    original = Path.write_text

    def failing_write(target, contents, *args, **kwargs):
        if target == path:
            original(target, contents, *args, **kwargs)
            raise OSError('injected-disk-full')
        return original(target, contents, *args, **kwargs)

    monkeypatch.setattr(Path, 'write_text', failing_write)


def test_file_write_failure_revokes_key_and_cleans_partial_file(install_environment, monkeypatch):
    path = install_environment['key_path']
    fail_key_write(monkeypatch, path)
    with pytest.raises(OSError, match='injected-disk-full'):
        install(install_environment, path)
    assert not path.exists()
    rows = records(install_environment, 'api_keys')
    assert len(rows) == 1 and rows[0]['revoked_at'] is not None


def test_revoke_failure_still_cleans_file_and_reports_only_key_id(install_environment, monkeypatch):
    path = install_environment['key_path']
    fail_key_write(monkeypatch, path)
    install_environment['fault'] = lambda method, route: httpx.Response(503) if method == 'DELETE' else None
    with pytest.raises(RuntimeError, match='自动撤销失败') as error:
        install(install_environment, path)
    rows = records(install_environment, 'api_keys')
    assert len(rows) == 1 and rows[0]['revoked_at'] is None
    assert str(rows[0]['id']) in str(error.value) and not path.exists()
    assert rows[0]['token_hash'] not in str(error.value)


def test_logout_failure_does_not_turn_success_into_duplicate_key_retry(install_environment):
    install_environment['fault'] = lambda method, route: httpx.Response(503) if route.endswith('/logout') else None
    result = install(install_environment, install_environment['key_path'])
    assert len(result['created']) == 9 and result['key']
    assert '自动登出失败' in result['warnings'][0]
    assert install_environment['key_path'].exists()


def test_logout_failure_keeps_primary_failure(install_environment, capsys):
    def fault(method, route):
        if route.endswith('/admin/keys'):
            return httpx.Response(502)
        if route.endswith('/logout'):
            return httpx.Response(503)
        return None
    install_environment['fault'] = fault
    with pytest.raises(RuntimeError, match='密钥签发结果未确认'):
        install(install_environment, install_environment['key_path'])
    assert '自动登出失败' in capsys.readouterr().err
    assert not install_environment['key_path'].exists()


def test_partial_service_failure_can_resume_without_overwriting(install_environment):
    install_environment['fault'] = lambda method, route: httpx.Response(503) if method == 'PUT' and route.endswith('/ds_dashboard_shops') else None
    with pytest.raises(RuntimeError, match='可重新执行续接'):
        install(install_environment, install_environment['key_path'])
    created = {row['slug'] for row in records(install_environment, 'query_services')}
    assert len(created) == 3 and not install_environment['key_path'].exists()
    install_environment['fault'] = None
    result = install(install_environment)
    assert set(result['existing_reused']) == created and len(result['created']) == 6
    assert all(row['revision'] == 1 for row in records(install_environment, 'query_services'))


def test_login_failure_has_no_side_effects_or_password_in_error(install_environment):
    password = install_environment['settings'].admin_password
    install_environment['fault'] = lambda method, route: httpx.Response(401, json={'detail': password}) if route.endswith('/login') else None
    with pytest.raises(RuntimeError) as error:
        install(install_environment, install_environment['key_path'])
    assert password not in str(error.value)
    assert records(install_environment, 'query_services') == []
    assert records(install_environment, 'api_keys') == []
    assert not install_environment['key_path'].exists()


@pytest.mark.parametrize('url', ['http://remote.example', 'https://', 'https://user:pass@example.test',
                                  'https://example.test/path', 'https://example.test?key=secret'])
def test_invalid_endpoint_rejected_before_network(install_environment, url):
    with pytest.raises(ValueError):
        INSTALLER.install(url, 'user', 'password', None)
    assert install_environment['calls'] == []


def test_main_success_never_prints_password_or_key(install_environment, monkeypatch, capsys):
    settings = install_environment['settings']
    monkeypatch.setenv('DS_ADMIN_USERNAME', settings.admin_username)
    monkeypatch.setenv('DS_ADMIN_PASSWORD', settings.admin_password)
    monkeypatch.setattr(INSTALLER, 'dotenv_values', lambda path: {})
    monkeypatch.setattr(INSTALLER.sys, 'argv', ['installer', '--url', 'http://127.0.0.1:8090',
                                             '--key-file', str(install_environment['key_path'])])
    INSTALLER.main()
    output = capsys.readouterr()
    secret = json.loads(install_environment['key_path'].read_text('utf-8'))
    assert settings.admin_password not in output.out + output.err
    assert secret['api_key'] not in output.out + output.err
    assert json.loads(output.out)['key']['id'] == secret['id']


def test_lost_create_response_recovers_and_revokes_only_its_own_key(install_environment):
    first_path = install_environment['key_path'].with_name('previous-key.json')
    first = install(install_environment, first_path)
    first_id = first['key']['id']
    def lost_response(method, path, response):
        if method == 'POST' and path.endswith('/admin/keys'):
            assert response.status_code == 201
            raise httpx.ReadTimeout('injected-response-lost')
    install_environment['after_response'] = lost_response
    with pytest.raises(RuntimeError, match='已找到并撤销本次签发'):
        install(install_environment, install_environment['key_path'])
    rows = records(install_environment, 'api_keys')
    assert len(rows) == 2
    assert next(row for row in rows if str(row['id']) == first_id)['revoked_at'] is None
    assert next(row for row in rows if str(row['id']) != first_id)['revoked_at'] is not None
    assert not install_environment['key_path'].exists() and first_path.exists()


def test_main_failure_does_not_print_sensitive_response(install_environment, monkeypatch, capsys):
    settings = install_environment['settings']
    install_environment['fault'] = lambda method, path: httpx.Response(401, json={'secret': settings.admin_password}) if path.endswith('/login') else None
    monkeypatch.setenv('DS_ADMIN_USERNAME', settings.admin_username)
    monkeypatch.setenv('DS_ADMIN_PASSWORD', settings.admin_password)
    monkeypatch.setattr(INSTALLER, 'dotenv_values', lambda path: {})
    monkeypatch.setattr(INSTALLER.sys, 'argv', ['installer', '--url', 'http://127.0.0.1:8090'])
    with pytest.raises(SystemExit) as error:
        INSTALLER.main()
    assert error.value.code == 1
    output = capsys.readouterr()
    assert settings.admin_password not in output.out + output.err
    assert 'HTTP 401' in output.err


def test_unremovable_file_is_reported_after_key_revocation(install_environment, monkeypatch):
    path = install_environment['key_path']
    fail_key_write(monkeypatch, path)
    original_unlink = Path.unlink
    def fail_unlink(target, *args, **kwargs):
        if target == path:
            raise PermissionError('injected-file-lock')
        return original_unlink(target, *args, **kwargs)
    monkeypatch.setattr(Path, 'unlink', fail_unlink)
    with pytest.raises(RuntimeError, match='密钥文件清理失败'):
        install(install_environment, path)
    assert path.exists()
    rows = records(install_environment, 'api_keys')
    assert len(rows) == 1 and rows[0]['revoked_at'] is not None
    original_unlink(path)
