"""隔离数据库验证模型配置、凭据保护和管理员权限，不访问真实供应商。"""

from concurrent.futures import ThreadPoolExecutor
import os
from urllib.parse import urlsplit

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from pydantic import ValidationError
import pytest

from data_service import auth, db, datasets, llm_config
from data_service.api import create_app
from data_service.config import Settings
from data_service.errors import ServiceError
from data_service.llm_config import LlmSave
from data_service.models import DatasetDefinition

KEY = 'test-provider-key-not-real'


@pytest.fixture
def service(tmp_path):
    address = os.environ['DS_TEST_DATABASE_URL']
    assert urlsplit(address).path == '/admin_api_test', '只允许清理管理员API专用测试库'
    settings = Settings(database_url=address, admin_username='llm_test',
                        admin_password='llm-test-admin-only!', config_encryption_key=Fernet.generate_key().decode(),
                        source_config_path=str(tmp_path / 'unused.json'))
    with db.connect(settings) as connection:
        connection.execute('DROP SCHEMA public CASCADE; CREATE SCHEMA public')
    db.migrate(settings)
    auth.initialize_admin(settings)
    definition = DatasetDefinition.model_validate({
        'slug': 'sales', 'name': '销售测试', 'date_field': 'dt', 'key_fields': ['id'],
        'fields': [{'name': 'dt', 'type': 'date', 'required': True, 'description': '业务日期'},
                   {'name': 'id', 'type': 'string', 'required': True, 'description': '唯一行'},
                   *[{'name': name, 'type': 'string', 'dimension': True, 'description': label}
                     for name, label in [('shop', '店铺'), ('category', '类目'), ('channel', '渠道'), ('owner', '负责人')]],
                   {'name': 'paid', 'type': 'decimal', 'description': '支付金额'},
                   {'name': 'refund', 'type': 'decimal', 'description': '退款金额'},
                   {'name': 'units', 'type': 'integer', 'description': '支付件数'}]})
    datasets.register_dataset(settings, definition)
    with TestClient(create_app(settings), raise_server_exceptions=False) as client:
        login = client.post('/api/v1/auth/login', json={'username': settings.admin_username,
                                                       'password': settings.admin_password})
        assert login.status_code == 200
        headers = {'Authorization': 'Bearer ' + login.json()['data']['token']}
        yield settings, client, headers


def configuration(**changes):
    return {'base_url': 'https://models.example.test/v1', 'model': 'example-model', 'api_key': KEY,
            'enabled': True, 'timeout_seconds': 45, 'token_parameter': 'max_tokens', 'revision': 0, **changes}


def put(service, **changes):
    return service[1].put('/api/v1/admin/llm', headers=service[2], json=configuration(**changes))


def test_defaults_save_encrypt_preserve_clear(service):
    initial = service[1].get('/api/v1/admin/llm', headers=service[2]).json()['data']
    assert initial['revision'] == 0 and not initial['has_api_key'] and not initial['enabled']
    saved = put(service)
    assert saved.status_code == 200 and saved.json()['data']['has_api_key']
    assert KEY not in saved.text and 'encrypted_api_key' not in saved.text
    with db.connect(service[0]) as connection:
        encrypted = connection.execute('SELECT encrypted_api_key FROM llm_settings').fetchone()['encrypted_api_key']
    assert KEY not in encrypted
    assert Fernet(service[0].config_encryption_key).decrypt(encrypted.encode()).decode() == KEY
    assert put(service, api_key=None, revision=1).status_code == 200
    with llm_config.call_configuration(service[0]) as resolved:
        assert resolved.api_key == KEY
    assert put(service, api_key=None, clear_api_key=True, revision=2).json()['data']['has_api_key'] is False
    with llm_config.call_configuration(service[0]) as resolved:
        assert resolved.api_key is None


def test_target_change_never_reuses_saved_key(service):
    assert put(service).status_code == 200
    for changes in ({'base_url': 'https://other.example.test/v1'}, {'base_url': 'http://models.example.test/v1'},
                    {'base_url': 'https://models.example.test:8443/v1'}):
        payload = configuration(api_key=None, revision=1, **changes)
        for path, method in [('/api/v1/admin/llm', 'put'), ('/api/v1/admin/llm/test', 'post')]:
            response = getattr(service[1], method)(path, headers=service[2], json=payload)
            assert response.status_code == 422 and response.json()['error_code'] == 'LLM_TARGET_CHANGED'
            assert KEY not in response.text
    assert put(service, api_key=None, revision=1, base_url='https://models.example.test:443/v2').status_code == 200
    assert put(service, revision=2, base_url='https://other.example.test/v1', api_key='new-test-key').status_code == 200


def test_revision_and_config_lock(service):
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: put(service).status_code, range(2)))
    assert sorted(results) == [200, 409]
    assert put(service).status_code == 409


def test_missing_cipher_and_wrong_cipher(service):
    no_cipher = service[0].model_copy(update={'config_encryption_key': None})
    with pytest.raises(ServiceError) as failure:
        llm_config.save_config(no_cipher, LlmSave(**configuration()))
    assert failure.value.code == 'LLM_ENCRYPTION_UNAVAILABLE'
    assert put(service).status_code == 200
    wrong = service[0].model_copy(update={'config_encryption_key': Fernet.generate_key().decode()})
    with pytest.raises(ServiceError) as failure:
        with llm_config.call_configuration(wrong):
            pass
    assert failure.value.code == 'LLM_KEY_DECRYPT_FAILED' and KEY not in str(failure.value)


def test_global_call_lock_and_exception_release(service):
    assert put(service).status_code == 200
    with pytest.raises(RuntimeError):
        with llm_config.call_configuration(service[0]):
            with pytest.raises(ServiceError) as failure:
                with llm_config.call_configuration(service[0]):
                    pass
            assert failure.value.status == 429
            raise RuntimeError('test-only')
    with llm_config.call_configuration(service[0]) as resolved:
        assert resolved.model == 'example-model'


@pytest.mark.parametrize('method,path', [('get', '/api/v1/admin/llm'), ('put', '/api/v1/admin/llm'),
    ('post', '/api/v1/admin/llm/test'), ('post', '/api/v1/admin/query-services/draft')])
def test_administrator_only(service, method, path):
    response = getattr(service[1], method)(path, **({'json': configuration()} if method != 'get' else {}))
    assert response.status_code == 401


@pytest.mark.parametrize('address', ['ftp://example.com', 'https://name:secret@example.com/v1',
    'https://example.com/?key=x', 'https://example.com/#x', 'https://169.254.169.254/latest',
    'http://[::ffff:169.254.169.254]/v1', 'http://[fe80::1]/v1', 'http://0.0.0.0/v1',
    'http://224.0.0.1/v1', 'http://metadata.google.internal/v1', 'http://2852039166/v1',
    'http://0xA9FEA9FE/v1', 'https://example.com:bad/v1', 'https://example.com\n/v1',
    'https://example.com/v1?', 'https://example.com/v1#', 'https://example.com:0/v1',
    'https://example.com/\ud800', ' https://example.com/v1', 'http://example.com\\@evil.test/v1', 'http://[fe80::1%25eth0]/v1'])
def test_bad_urls(address):
    with pytest.raises(ValidationError):
        LlmSave(**configuration(base_url=address))


@pytest.mark.parametrize('address', ['http://127.0.0.1:11434/v1', 'http://192.168.1.8:8000/v1',
                                    'https://EXAMPLE.test/v1/', 'http://[::1]:8080/v1'])
def test_explicit_local_or_private_urls(address):
    assert LlmSave(**configuration(base_url=address)).base_url


@pytest.mark.parametrize('changes', [{'timeout_seconds': 0}, {'timeout_seconds': 91}, {'revision': -1},
    {'token_parameter': 'evil'}, {'api_key': 'abc\nsecret'}, {'api_key': '密钥'}, {'api_key': ''},
    {'api_key': KEY, 'clear_api_key': True}, {'model': '   '}, {'model': 'a\ud800'}, {'enabled': 1},
    {'unexpected': 'x'}])
def test_config_boundaries(changes):
    with pytest.raises(ValidationError):
        LlmSave(**configuration(**changes))

