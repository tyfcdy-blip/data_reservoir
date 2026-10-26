"""管理员持久化HTTP来源；加密凭据、显式主机批准、版本更新和文件兼容。"""

from datetime import date, datetime, timezone
import json
import math
import os
import re
from typing import Any, Literal
from urllib.parse import urlsplit

from cryptography.fernet import Fernet, InvalidToken
import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from psycopg.types.json import Jsonb

from data_service.config import Settings
from data_service.datasets import get_dataset, validate_record
from data_service.db import connect
from data_service.errors import ServiceError
from data_service.models import DatasetDefinition
from data_service.source import SourceClient, SourceConfig, load_sources


class SourceSave(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    title: str = Field(min_length=1, max_length=120)
    config: SourceConfig
    secrets: dict[str, str] = Field(default_factory=dict, max_length=22, repr=False)
    enabled: bool = Field(default=True, strict=True)
    approve_host: bool = Field(default=False, strict=True)
    schedule_enabled: bool = Field(default=False, strict=True)
    schedule_hour: int = Field(default=3, ge=0, le=23, strict=True)
    schedule_times: list[str] | None = Field(default=None, min_length=1, max_length=24, strict=True)
    write_mode: Literal["replace_day", "upsert"] | None = None
    refresh_days: int = Field(default=7, ge=1, le=180, strict=True)
    revision: int = Field(default=0, ge=0, le=2147483646, strict=True)

    @field_validator("schedule_times", mode="before", json_schema_input_type=list[str])
    @classmethod
    def schedule_times_not_null(cls, value: Any) -> Any:
        # 省略表示保留旧值；显式空值没有可执行含义，不能悄悄当作省略处理。
        if value is None:
            raise ValueError("每日执行时间不可为null；不修改时请省略该字段")
        return value

    @field_validator("write_mode", mode="before", json_schema_input_type=Literal["replace_day", "upsert"])
    @classmethod
    def write_mode_not_null(cls, value: Any) -> Any:
        if value is None:
            raise ValueError("写入方式不可为null；不修改时请省略该字段")
        return value

    @field_validator("schedule_times")
    @classmethod
    def valid_schedule_times(cls, values: list[str] | None) -> list[str] | None:
        if values is None:
            return None
        if any(not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", value) for value in values):
            raise ValueError("每日执行时间必须为两位小时和分钟，例如03:00")
        if len(set(values)) != len(values):
            raise ValueError("每日执行时间不可重复")
        return sorted(values)

    @model_validator(mode="after")
    def compatible_schedule_hour(self) -> "SourceSave":
        if self.schedule_times is not None:
            # 旧客户端仍可读取首个执行小时，新客户端以分钟级时间列表为准。
            self.schedule_hour = int(self.schedule_times[0][:2])
        return self

    @field_validator("title")
    @classmethod
    def valid_title(cls, value: str) -> str:
        if not value.strip() or any(ord(char) < 32 or ord(char) == 127 or 0xD800 <= ord(char) <= 0xDFFF for char in value):
            raise ValueError("名称不可为空、包含控制字符或非法Unicode")
        return value.strip()

    @field_validator("secrets")
    @classmethod
    def valid_secrets(cls, values: dict[str, str]) -> dict[str, str]:
        for name, value in values.items():
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,119}", name):
                raise ValueError("凭据引用名无效")
            if not value or len(value) > 8192 or any(ord(char) < 32 or ord(char) == 127 or 0xD800 <= ord(char) <= 0xDFFF for char in value):
                raise ValueError("凭据值为空、过长、包含控制字符或非法Unicode")
        return values

    @field_validator("config")
    @classmethod
    def persistent_config_values(cls, config: SourceConfig) -> SourceConfig:
        # JSONB不接受NUL，UTF-8不接受代理字符；静态参数的无穷值也不属于可持久化JSON。
        # 在管理入口拒绝，避免合法结构在保存时退化为数据库/编码异常。
        def validate(value: Any) -> None:
            if isinstance(value, str):
                if any(ord(char) == 0 or 0xD800 <= ord(char) <= 0xDFFF for char in value):
                    raise ValueError("来源配置包含NUL或非法Unicode")
            elif isinstance(value, float) and not math.isfinite(value):
                raise ValueError("来源配置数值必须有限")
            elif isinstance(value, dict):
                for key, nested in value.items():
                    validate(key)
                    validate(nested)
            elif isinstance(value, list):
                for nested in value:
                    validate(nested)
        validate(config.model_dump())
        return config


def _secret_refs(config: SourceConfig) -> set[str]:
    return set(config.header_env.values()) | {
        value for value in (config.vendor_app_key_env, config.vendor_app_secret_env) if value is not None
    }


def _cipher(settings: Settings) -> Fernet:
    if not settings.config_encryption_key:
        raise ServiceError(503, "SOURCE_ENCRYPTION_UNAVAILABLE", "尚未配置来源凭据加密密钥，请先完成服务初始化")
    return Fernet(settings.config_encryption_key)


def _decrypt(settings: Settings, encrypted: str | None) -> dict[str, str]:
    if encrypted is None:
        return {}
    cipher = _cipher(settings)
    try:
        values = json.loads(cipher.decrypt(encrypted.encode()))
        if not isinstance(values, dict):
            raise ValueError("shape")
        return SourceSave.valid_secrets(values)
    except (InvalidToken, ValueError, TypeError, UnicodeError, AttributeError):
        raise ServiceError(503, "SOURCE_SECRET_DECRYPT_FAILED", "来源凭据解密失败，请检查当前加密密钥与备份是否匹配") from None


def _public_record(record: dict[str, Any]) -> dict[str, Any]:
    config = SourceConfig.model_validate(record["definition"])
    # 保存边界保证所有引用都已提供；列表无需解密就能展示已配置的引用名称。
    return {"dataset_slug": record["dataset_slug"], "title": record["title"], "config": config.model_dump(mode="json"),
            "enabled": record["enabled"], "schedule_enabled": record["schedule_enabled"],
            "schedule_hour": record["schedule_hour"], "schedule_times": record["schedule_times"],
            "write_mode": record["write_mode"], "refresh_days": record["refresh_days"],
            "revision": record["revision"], "secret_names": sorted(_secret_refs(config)),
            "created_at": record["created_at"], "updated_at": record["updated_at"], "origin": "managed"}


def _file_record(config: SourceConfig, settings: Settings) -> dict[str, Any]:
    return {"dataset_slug": config.dataset_slug, "title": config.dataset_slug, "config": config.model_dump(mode="json"),
            "enabled": True, "schedule_enabled": settings.schedule_enabled, "schedule_hour": settings.schedule_hour,
            "schedule_times": [f"{settings.schedule_hour:02d}:00"], "write_mode": "replace_day",
            "refresh_days": settings.refresh_days, "revision": 0,
            "secret_names": sorted(name for name in _secret_refs(config) if os.environ.get(name)),
            "created_at": None, "updated_at": None, "origin": "file"}


def list_sources(settings: Settings) -> list[dict[str, Any]]:
    sources = {slug: _file_record(config, settings) for slug, config in load_sources(settings).items()}
    with connect(settings) as connection:
        for record in connection.execute("SELECT * FROM managed_sources ORDER BY dataset_slug").fetchall():
            sources[record["dataset_slug"]] = _public_record(record)
    return [sources[slug] for slug in sorted(sources)]


def get_source(settings: Settings, dataset_slug: str) -> dict[str, Any]:
    with connect(settings) as connection:
        record = connection.execute("SELECT * FROM managed_sources WHERE dataset_slug=%s", (dataset_slug,)).fetchone()
    if record is not None:
        return _public_record(record)
    config = load_sources(settings).get(dataset_slug)
    if config is not None:
        return _file_record(config, settings)
    raise ServiceError(404, "SOURCE_NOT_CONFIGURED", "该数据表尚未配置来源")


def _validate_mapping(config: SourceConfig, definition: DatasetDefinition) -> None:
    if config.field_mapping and set(config.field_mapping) != {field.name for field in definition.fields}:
        raise ServiceError(422, "FIELD_MAPPING_INCOMPLETE", "字段映射必须覆盖数据表全部字段且不得包含额外目标字段")
    if set(config.date_fields_from_datetime) - {field.name for field in definition.fields if field.type == "date"}:
        raise ServiceError(422, "DATE_CONVERSION_INVALID", "日期转换只能指向数据表中的日期字段")
    if set(config.text_whitespace_fields) - {field.name for field in definition.fields if field.type == "string"}:
        raise ServiceError(422, "TEXT_CONVERSION_INVALID", "文本空白转换只能指向数据表中的文本字段")


def save_source(settings: Settings, dataset_slug: str, payload: SourceSave) -> dict[str, Any]:
    if payload.config.dataset_slug != dataset_slug:
        raise ServiceError(422, "SOURCE_DATASET_MISMATCH", "来源的数据表标识与地址不一致")
    hostname = (urlsplit(payload.config.url).hostname or "").lower()
    with connect(settings) as connection:
        # 新建时还没有行锁，按数据表锁定可同时保护首次保存与后续修订的比较。
        connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", ("source:" + dataset_slug,))
        dataset = get_dataset(connection, dataset_slug)
        _validate_mapping(payload.config, DatasetDefinition.model_validate(dataset["definition"]))
        current = connection.execute("SELECT * FROM managed_sources WHERE dataset_slug=%s FOR UPDATE", (dataset_slug,)).fetchone()
        if payload.revision != (current["revision"] if current else 0):
            raise ServiceError(409, "SOURCE_REVISION_CONFLICT", "来源已被更新，请重新读取后保存")
        if not payload.approve_host and (current is None or current["approved_host"] != hostname):
            raise ServiceError(422, "SOURCE_HOST_APPROVAL_REQUIRED", "请先确认允许服务访问此来源主机")
        refs = _secret_refs(payload.config)
        if payload.secrets.keys() - refs:
            raise ServiceError(422, "SOURCE_SECRET_UNUSED", "填写的凭据没有被当前来源引用")
        secrets = _decrypt(settings, current["encrypted_secrets"]) if current else {}
        secrets.update(payload.secrets)
        if refs - secrets.keys():
            raise ServiceError(422, "SOURCE_SECRET_MISSING", "请填写来源引用的全部凭据；已有凭据可留空保留")
        secrets = {name: secrets[name] for name in refs}
        header_refs = set(payload.config.header_env.values())
        if payload.config.vendor_app_key_env:
            header_refs.add(payload.config.vendor_app_key_env)
        if any(not secrets[name].isascii() for name in header_refs):
            raise ServiceError(422, "SOURCE_HEADER_SECRET_INVALID", "请求头凭据只接受ASCII字符，请填写来源要求的编码值")
        encrypted = _cipher(settings).encrypt(json.dumps(secrets, ensure_ascii=False).encode()).decode() if secrets else None
        # 旧客户端显式传小时仍按单时间保存；仅编辑其他配置则保留已有多时间与写入方式。
        if payload.schedule_times is not None:
            schedule_times = payload.schedule_times
        elif current is not None and "schedule_hour" not in payload.model_fields_set:
            schedule_times = current["schedule_times"]
        else:
            schedule_times = [f"{payload.schedule_hour:02d}:00"]
        schedule_hour = int(schedule_times[0][:2])
        write_mode = payload.write_mode or (current["write_mode"] if current else "replace_day")
        record = connection.execute(
            "INSERT INTO managed_sources(dataset_slug,title,definition,encrypted_secrets,approved_host,enabled,"
            "schedule_enabled,schedule_hour,schedule_times,write_mode,refresh_days,revision) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT(dataset_slug) DO UPDATE SET title=EXCLUDED.title,definition=EXCLUDED.definition,"
            "encrypted_secrets=EXCLUDED.encrypted_secrets,approved_host=EXCLUDED.approved_host,enabled=EXCLUDED.enabled,"
            "schedule_enabled=EXCLUDED.schedule_enabled,schedule_hour=EXCLUDED.schedule_hour,"
            "schedule_times=EXCLUDED.schedule_times,write_mode=EXCLUDED.write_mode,refresh_days=EXCLUDED.refresh_days,"
            "revision=EXCLUDED.revision,updated_at=clock_timestamp() RETURNING *",
            (dataset_slug, payload.title, Jsonb(payload.config.model_dump(mode="json")), encrypted, hostname,
             payload.enabled, payload.schedule_enabled, schedule_hour, Jsonb(schedule_times), write_mode,
             payload.refresh_days, payload.revision + 1),
        ).fetchone()
        return _public_record(record)


def _runtime_config(settings: Settings, record: dict[str, Any]) -> SourceConfig:
    config = SourceConfig.model_validate(record["definition"])
    config._managed_secrets = _decrypt(settings, record["encrypted_secrets"])
    if _secret_refs(config) - config._managed_secrets.keys():
        raise ServiceError(503, "SOURCE_SECRET_MISSING", "已保存来源的凭据不完整")
    config._approved_host = record["approved_host"]
    config._revision = record["revision"]
    config._schedule = {name: record[name] for name in ("schedule_enabled", "schedule_hour", "schedule_times", "write_mode", "refresh_days")}
    return config


def load_active_sources(settings: Settings) -> dict[str, SourceConfig]:
    sources = load_sources(settings)
    with connect(settings) as connection:
        records = connection.execute("SELECT * FROM managed_sources ORDER BY dataset_slug").fetchall()
    for record in records:
        # 禁用配置遮蔽同名文件配置，避免禁用后悄悄退回旧来源继续拉取。
        if record["enabled"]:
            sources[record["dataset_slug"]] = _runtime_config(settings, record)
        else:
            sources.pop(record["dataset_slug"], None)
    return sources


def test_source(settings: Settings, dataset_slug: str, business_date: date, *,
                transport: httpx.BaseTransport | None = None) -> dict[str, Any]:
    from data_service.ingest import _date_window

    with connect(settings) as connection:
        dataset = get_dataset(connection, dataset_slug)
        record = connection.execute("SELECT * FROM managed_sources WHERE dataset_slug=%s", (dataset_slug,)).fetchone()
    config = _runtime_config(settings, record) if record else load_sources(settings).get(dataset_slug)
    if config is None:
        raise ServiceError(404, "SOURCE_NOT_CONFIGURED", "请先保存来源配置")
    definition = DatasetDefinition.model_validate(dataset["definition"])
    _validate_mapping(config, definition)
    _date_window(definition, business_date, pull=True)
    with SourceClient(config, settings, transport=transport) as client:
        rows = client.query_page(business_date, config.page_start)
    for row in rows:
        validate_record(definition, row, business_date)
    # 只返回结构验证结果，不回显上游记录，避免回声式接口把凭据或敏感字段带入管理响应。
    return {"dataset_slug": dataset_slug, "business_date": business_date, "row_count": len(rows),
            "columns": [field.name for field in definition.fields],
            "field_types": {field.name: field.type for field in definition.fields},
            "tested_at": datetime.now(timezone.utc), "message": "单页请求与字段校验通过，未写入数据"}
