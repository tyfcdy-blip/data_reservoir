"""按环境隔离配置，读取不触发数据库或网络操作。"""

from functools import lru_cache
from urllib.parse import urlsplit

from pydantic import Field, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DS_", extra="ignore", hide_input_in_errors=True)

    database_url: str = Field(repr=False)
    admin_username: str = Field(default="admin", min_length=1, max_length=64)
    admin_password: str | None = Field(default=None, repr=False, min_length=12, max_length=1024)
    session_hours: int = Field(default=8, ge=1, le=24)
    statement_timeout_ms: int = Field(default=15000, ge=100, le=120000)
    max_request_bytes: int = Field(default=2097152, ge=1024, le=16777216)
    source_config_path: str = "config/sources.json"
    dataset_config_path: str = "config/datasets.json"
    allowed_source_hosts: list[str] = Field(default_factory=list)
    schedule_enabled: bool = False
    schedule_hour: int = Field(default=3, ge=0, le=23)
    refresh_days: int = Field(default=7, ge=1, le=180)
    config_encryption_key: str | None = Field(default=None, repr=False)

    @field_validator("config_encryption_key", mode="before")
    @classmethod
    def encryption_key(cls, value: object) -> object:
        if value in (None, ""):
            return None
        from cryptography.fernet import Fernet
        try:
            Fernet(value)
        except (ValueError, TypeError):
            raise ValueError("配置加密密钥无效") from None
        return value

    @field_validator("database_url")
    @classmethod
    def database_scheme(cls, value: str) -> str:
        if urlsplit(value).scheme not in {"postgres", "postgresql"}:
            raise ValueError("仅支持PostgreSQL")
        return value

    @field_validator("admin_password", mode="before")
    @classmethod
    def empty_password(cls, value: object) -> object:
        return None if value == "" else value

    @field_validator("admin_username")
    @classmethod
    def username_format(cls, value: str) -> str:
        if value != value.strip() or any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("管理员名称无效")
        return value

    @field_validator("allowed_source_hosts")
    @classmethod
    def hostnames(cls, values: list[str]) -> list[str]:
        if len(values) > 100 or any(not value or any(c in value for c in "/@?# ") for value in values):
            raise ValueError("源主机白名单无效")
        return [value.lower() for value in values]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    try:
        return Settings()
    except ValidationError:
        raise ValueError("服务配置无效，请检查环境变量") from None
