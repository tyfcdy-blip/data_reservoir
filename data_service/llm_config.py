"""单管理员模型连接配置；密钥加密保存，修改目标时禁止静默转发旧密钥。"""

from contextlib import contextmanager
from ipaddress import ip_address
from typing import Any, Iterator, Literal
from urllib.parse import urlsplit, urlunsplit

from cryptography.fernet import Fernet, InvalidToken
from pydantic import Field, field_validator, model_validator

from data_service import db
from data_service.config import Settings
from data_service.errors import ServiceError
from data_service.models import StrictModel

CALL_LOCK = 706223004
CONFIG_LOCK = 706223005


def blocked_address(hostname: str) -> bool:
    """允许管理员明确配置的局域网服务，但不允许云元数据及特殊路由地址。"""
    if hostname.lower().rstrip('.') in {'metadata', 'metadata.google.internal', 'instance-data.ec2.internal'}:
        return True
    try:
        address = ip_address(hostname)
    except ValueError:
        return False
    address = getattr(address, 'ipv4_mapped', None) or address
    return address.is_link_local or address.is_unspecified or address.is_multicast


def normalize_url(value: str) -> str:
    if value != value.strip() or any(ord(char) < 33 or ord(char) == 127 or 0xD800 <= ord(char) <= 0xDFFF for char in value) or '\\' in value:
        raise ValueError('模型地址含空白或非法字符')
    try:
        parts = urlsplit(value)
        port = parts.port
        if (parts.scheme not in {'http', 'https'} or not parts.hostname or parts.username is not None
                or parts.password is not None or '?' in value or '#' in value or '%' in parts.netloc
                or (port is not None and port < 1)
                or blocked_address(parts.hostname)):
            raise ValueError('模型地址不受支持')
        hostname = parts.hostname.encode('idna').decode('ascii').lower()
        # 禁止有歧义的IPv4写法，由客户端解析时再次核验最终地址。
        if hostname.startswith('0x') or (hostname.isdigit() and '.' not in hostname):
            raise ValueError('模型地址必须使用标准主机名或IP')
        if ':' in hostname:
            hostname = '[' + hostname + ']'
        authority = hostname + (f':{port}' if port is not None else '')
        return urlunsplit((parts.scheme, authority, parts.path.rstrip('/'), '', ''))
    except (ValueError, UnicodeError):
        raise ValueError('模型地址须为有效HTTP(S)地址，不可包含账号、查询参数、片段或元数据地址') from None


def origin(address: str) -> tuple[str, str, int]:
    parts = urlsplit(address)
    return parts.scheme, parts.hostname or '', parts.port or (443 if parts.scheme == 'https' else 80)


class LlmSave(StrictModel):
    base_url: str = Field(min_length=1, max_length=2048, strict=True)
    model: str = Field(min_length=1, max_length=200, strict=True)
    api_key: str | None = Field(default=None, max_length=8192, repr=False)
    clear_api_key: bool = Field(default=False, strict=True)
    enabled: bool = Field(default=False, strict=True)
    timeout_seconds: int = Field(default=45, ge=5, le=90, strict=True)
    token_parameter: Literal['max_tokens', 'max_completion_tokens'] = 'max_tokens'
    revision: int = Field(ge=0, le=2147483646, strict=True)

    _url = field_validator('base_url')(normalize_url)

    @field_validator('model')
    @classmethod
    def model_name(cls, value: str) -> str:
        if value != value.strip() or any(ord(char) < 32 or ord(char) == 127 or 0xD800 <= ord(char) <= 0xDFFF for char in value):
            raise ValueError('模型名称无效')
        return value

    @field_validator('api_key')
    @classmethod
    def secret_value(cls, value: str | None) -> str | None:
        if value is not None and (not value or any(ord(char) < 33 or ord(char) > 126 for char in value)):
            raise ValueError('API KEY须为非空可见ASCII字符')
        return value

    @model_validator(mode='after')
    def key_choice(self) -> 'LlmSave':
        if self.api_key is not None and self.clear_api_key:
            raise ValueError('不能同时提供新密钥并清除密钥')
        return self


class LlmConnection(StrictModel):
    base_url: str
    model: str
    api_key: str | None = Field(default=None, repr=False)
    enabled: bool
    timeout_seconds: int
    token_parameter: Literal['max_tokens', 'max_completion_tokens']


def _cipher(settings: Settings) -> Fernet:
    if not settings.config_encryption_key:
        raise ServiceError(503, 'LLM_ENCRYPTION_UNAVAILABLE', '未配置凭据加密密钥，请先完成服务初始化')
    return Fernet(settings.config_encryption_key)


def _decrypt(settings: Settings, encrypted: str | None) -> str | None:
    if encrypted is None:
        return None
    try:
        return _cipher(settings).decrypt(encrypted.encode('ascii')).decode('ascii')
    except (InvalidToken, ValueError, UnicodeError):
        raise ServiceError(503, 'LLM_KEY_DECRYPT_FAILED', '模型密钥解密失败，请检查服务加密配置或重新填写KEY') from None


def _record(connection) -> dict[str, Any] | None:
    return connection.execute('SELECT * FROM llm_settings WHERE id=1').fetchone()


def _public(record: dict[str, Any] | None) -> dict[str, Any]:
    if record is None:
        return {'base_url': '', 'model': '', 'has_api_key': False, 'enabled': False,
                'timeout_seconds': 45, 'token_parameter': 'max_tokens', 'revision': 0}
    return {**{key: record[key] for key in ('base_url', 'model', 'enabled', 'timeout_seconds',
                                           'token_parameter', 'revision')},
            'has_api_key': record['encrypted_api_key'] is not None}


def get_config(settings: Settings) -> dict[str, Any]:
    with db.connect(settings) as connection:
        return _public(_record(connection))


def _resolve(settings: Settings, payload: LlmSave, existing: dict[str, Any] | None) -> LlmConnection:
    revision = existing['revision'] if existing else 0
    if payload.revision != revision:
        raise ServiceError(409, 'CONFIG_CONFLICT', '模型配置已被修改，请重新加载后编辑')
    secret = payload.api_key
    if secret is None and not payload.clear_api_key and existing and existing['encrypted_api_key']:
        if origin(payload.base_url) != origin(existing['base_url']):
            raise ServiceError(422, 'LLM_TARGET_CHANGED', '模型地址的主机或协议已更改，请重新填写KEY或勾选清除KEY')
        secret = _decrypt(settings, existing['encrypted_api_key'])
    return LlmConnection(**payload.model_dump(exclude={'api_key', 'clear_api_key', 'revision'}), api_key=secret)


def save_config(settings: Settings, payload: LlmSave) -> dict[str, Any]:
    with db.connect(settings) as connection:
        connection.execute('SELECT pg_advisory_xact_lock(%s)', (CONFIG_LOCK,))
        resolved = _resolve(settings, payload, _record(connection))
        encrypted = (_cipher(settings).encrypt(resolved.api_key.encode('ascii')).decode('ascii')
                     if resolved.api_key else None)
        connection.execute('''INSERT INTO llm_settings
            (id,base_url,model,encrypted_api_key,enabled,timeout_seconds,token_parameter,revision)
            VALUES (1,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT(id) DO UPDATE SET base_url=EXCLUDED.base_url,model=EXCLUDED.model,
            encrypted_api_key=EXCLUDED.encrypted_api_key,enabled=EXCLUDED.enabled,
            timeout_seconds=EXCLUDED.timeout_seconds,token_parameter=EXCLUDED.token_parameter,
            revision=EXCLUDED.revision,updated_at=now()''',
            (resolved.base_url, resolved.model, encrypted, resolved.enabled, resolved.timeout_seconds,
             resolved.token_parameter, payload.revision + 1))
        return _public(_record(connection))


@contextmanager
def call_configuration(settings: Settings, payload: LlmSave | None = None) -> Iterator[LlmConnection]:
    """跨API进程最多一个模型请求；锁随事务释放，异常不遗留忙状态。"""
    with db.connect(settings) as connection:
        if not connection.execute('SELECT pg_try_advisory_xact_lock(%s) AS acquired', (CALL_LOCK,)).fetchone()['acquired']:
            raise ServiceError(429, 'LLM_BUSY', '已有模型请求正在处理，请稍后重试')
        existing = _record(connection)
        if payload is not None:
            yield _resolve(settings, payload, existing)
        else:
            if existing is None or not existing['enabled']:
                raise ServiceError(409, 'LLM_NOT_ENABLED', '请先在大模型配置中保存连接信息并启用自然语言配置')
            yield LlmConnection(**{key: existing[key] for key in ('base_url', 'model', 'enabled',
                                'timeout_seconds', 'token_parameter')},
                                api_key=_decrypt(settings, existing['encrypted_api_key']))
