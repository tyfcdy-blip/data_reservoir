"""供其他系统使用的可撤销数据集密钥；与管理员会话分离。"""

from dataclasses import dataclass
import hashlib
import re
import secrets
from typing import Literal
import uuid

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from psycopg.types.json import Jsonb

from data_service import auth
from data_service.config import Settings
from data_service.db import connect
from data_service.errors import ServiceError
from data_service.key_policy import KeyPolicy, RowScope, policy_error, validate_scope_datasets

KEY_PATTERN = re.compile(r"ds_[A-Za-z0-9_-]{43}")


class KeyCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: str = Field(min_length=1, max_length=120)
    grants: dict[str, list[Literal["read", "write"]]] = Field(default_factory=dict)
    policy: KeyPolicy | None = None
    expires_in_days: int = Field(default=90, ge=1, le=365)
    requests_per_minute: int = Field(default=60, ge=1, le=600)

    @field_validator("grants")
    @classmethod
    def valid_grants(cls, value: dict[str, list[str]]) -> dict[str, list[str]]:
        if len(value) > 100:
            raise ValueError("最多指定100个数据集")
        for slug, actions in value.items():
            if not re.fullmatch(r"[a-z][a-z0-9_]{0,47}", slug) or not actions or len(actions) != len(set(actions)):
                raise ValueError("数据集授权无效")
        return value

    @model_validator(mode="after")
    def valid_policy(self) -> "KeyCreate":
        if self.policy is None:
            if not self.grants:
                raise ValueError("必须指定1至100个数据集")
        else:
            self.policy.validate_grants(self.grants)
        return self


@dataclass(frozen=True)
class Principal:
    admin: bool
    key_id: uuid.UUID | None = None
    grants: dict[str, list[str]] | None = None
    policy: KeyPolicy | None = None

    def require(self, slug: str, permission: str) -> None:
        if self.admin:
            return
        if (permission not in (self.grants or {}).get(slug, []) or
                (self.policy is not None and permission != "read")):
            raise ServiceError(403, "DATASET_FORBIDDEN", "此密钥没有该数据集的操作权限")
        self.scope_for(slug)

    def scope_for(self, slug: str) -> RowScope | None:
        if self.admin or self.policy is None:
            return None
        scope = self.policy.row_scopes.get(slug)
        if scope is None:
            raise policy_error()
        return scope

    def allows_service(self, service: str, dataset: str) -> bool:
        if self.admin:
            return True
        if self.policy is None:
            return "read" in (self.grants or {}).get(dataset, [])
        return self.policy.services.get(service) == dataset and dataset in self.policy.row_scopes

    def require_service(self, service: str, dataset: str) -> None:
        if not self.allows_service(service, dataset):
            raise ServiceError(403, "SERVICE_FORBIDDEN", "此密钥没有该开放接口的调用权限")


def create_key(settings: Settings, payload: KeyCreate) -> dict:
    key_id = uuid.uuid4()
    token = "ds_" + secrets.token_urlsafe(32)
    with connect(settings) as connection:
        found = connection.execute("SELECT slug FROM datasets WHERE slug=ANY(%s)", (list(payload.grants),)).fetchall()
        if len(found) != len(payload.grants):
            raise ServiceError(422, "UNKNOWN_DATASET", "授权中包含不存在的数据集")
        if payload.policy is not None:
            try:
                validate_scope_datasets(connection, payload.policy)
            except (ServiceError, ValidationError):
                raise ServiceError(422, "INVALID_KEY_SCOPE", "数据范围必须引用存在的字符串分组字段，且授权值不能超长") from None
            services = connection.execute(
                "SELECT slug,dataset_slug,enabled FROM query_services WHERE slug=ANY(%s)",
                (list(payload.policy.services),)).fetchall()
            if (len(services) != len(payload.policy.services) or
                    any(not service["enabled"] or payload.policy.services[service["slug"]] != service["dataset_slug"]
                        for service in services)):
                raise ServiceError(422, "INVALID_KEY_SERVICES", "授权接口必须已开放且对应的数据表一致")
        row = connection.execute(
            """INSERT INTO api_keys(id,name,token_hash,token_hint,grants,policy,expires_at,requests_per_minute)
            VALUES(%s,%s,%s,%s,%s,%s,clock_timestamp()+make_interval(days=>%s),%s)
            RETURNING id,name,token_hint,grants,policy,created_at,expires_at,requests_per_minute""",
            (key_id, payload.name, hashlib.sha256(token.encode()).hexdigest(), token[-4:],
             Jsonb(payload.grants), Jsonb(payload.policy.model_dump()) if payload.policy is not None else None,
             payload.expires_in_days, payload.requests_per_minute),
        ).fetchone()
    return {**row, "api_key": token}


def list_keys(settings: Settings) -> list[dict]:
    with connect(settings) as connection:
        return connection.execute(
            "SELECT id,name,token_hint,grants,policy,created_at,expires_at,revoked_at,requests_per_minute "
            "FROM api_keys ORDER BY created_at DESC LIMIT 1000"
        ).fetchall()


def revoke_key(settings: Settings, key_id: uuid.UUID) -> None:
    with connect(settings) as connection:
        result = connection.execute(
            "UPDATE api_keys SET revoked_at=COALESCE(revoked_at,clock_timestamp()) WHERE id=%s", (key_id,))
        if not result.rowcount:
            raise ServiceError(404, "KEY_NOT_FOUND", "密钥不存在")


def authenticate(settings: Settings, authorization: str | None, api_key: str | None) -> Principal:
    if authorization and api_key:
        raise ServiceError(400, "AMBIGUOUS_CREDENTIALS", "一次请求只能使用一种身份凭据")
    if authorization:
        auth.authenticate(settings, authorization)
        return Principal(admin=True)
    if not api_key or not KEY_PATTERN.fullmatch(api_key):
        raise ServiceError(401, "API_KEY_REQUIRED", "请提供有效的X-API-Key")
    token_hash = hashlib.sha256(api_key.encode()).hexdigest()
    with connect(settings) as connection:
        key = connection.execute(
            """SELECT id,grants,policy,policy IS NULL AS legacy_policy,requests_per_minute FROM api_keys
            WHERE token_hash=%s AND revoked_at IS NULL AND expires_at>clock_timestamp()""", (token_hash,)
        ).fetchone()
        if not key:
            raise ServiceError(401, "API_KEY_INVALID", "密钥无效、已过期或已撤销")
        attempts = connection.execute(
            """INSERT INTO api_key_limits(key_id,window_start,attempts)
            VALUES(%s,clock_timestamp(),1) ON CONFLICT(key_id) DO UPDATE SET
            attempts=CASE WHEN api_key_limits.window_start <= clock_timestamp()-interval '60 seconds'
                THEN 1 ELSE api_key_limits.attempts+1 END,
            window_start=CASE WHEN api_key_limits.window_start <= clock_timestamp()-interval '60 seconds'
                THEN clock_timestamp() ELSE api_key_limits.window_start END RETURNING attempts""", (key["id"],)
        ).fetchone()["attempts"]
    # 提交计数之后拒绝，不能因异常回滚导致限流失效。
    if attempts > key["requests_per_minute"]:
        raise ServiceError(429, "KEY_RATE_LIMIT", "调用过于频繁，请稍后重试")
    policy = None
    if not key["legacy_policy"]:
        try:
            if key["policy"] is None:
                raise policy_error()
            policy = KeyCreate.model_validate({"name": "stored-policy", "grants": key["grants"],
                                               "policy": key["policy"]}).policy
            with connect(settings) as connection:
                validate_scope_datasets(connection, policy)
        except (ValidationError, ValueError, TypeError, ServiceError):
            # 损坏策略包括JSON null；仅真正SQL NULL可以走原有授权。
            raise policy_error() from None
    return Principal(admin=False, key_id=key["id"], grants=key["grants"], policy=policy)
