"""部署配置的HTTP源，固定协议、有限资源和跨日期共享限频。"""

from collections.abc import Callable, Iterator
from datetime import date, datetime
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import re
import time
from typing import Any, Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, ValidationError, model_validator

from data_service.config import Settings


class SourceError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class _Retryable(SourceError):
    def __init__(self, code: str, message: str, retry_after: float = 0):
        super().__init__(code, message)
        self.retry_after = retry_after


class SourceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    # 管理来源的运行时信息不参加序列化，外部请求不能伪造批准主机或注入明文凭据。
    _managed_secrets: dict[str, str] | None = PrivateAttr(default=None)
    _approved_host: str | None = PrivateAttr(default=None)
    _revision: int | None = PrivateAttr(default=None)
    _schedule: dict[str, bool | int | str | list[str]] | None = PrivateAttr(default=None)

    dataset_slug: str = Field(pattern=r"^[a-z][a-z0-9_]{0,47}$")
    protocol: Literal["http_json", "vendor_batch"] = "http_json"
    url: str = Field(max_length=2048, repr=False)
    method: Literal["GET", "POST"] = "POST"
    parameter_location: Literal["query", "json"] = "json"
    date_param: str = "dt"
    page_param: str = "pageIndex"
    page_size_param: str = "pageSize"
    page_start: int = Field(default=0, ge=0, le=1)
    page_size: int = Field(default=200, ge=1, le=1000)
    rows_path: str = "data"
    static_params: dict[str, str | int | float | bool | None] = Field(default_factory=dict, repr=False)
    field_mapping: dict[str, str] = Field(default_factory=dict)
    date_fields_from_datetime: list[str] = Field(default_factory=list, max_length=80)
    text_whitespace_fields: list[str] = Field(default_factory=list, max_length=80, strict=True)
    source_timezone: str = "Asia/Shanghai"
    header_env: dict[str, str] = Field(default_factory=dict, repr=False)
    success_field: str | None = None
    success_value: str | int | bool | None = None
    vendor_api_id: str | None = Field(default=None, max_length=120)
    vendor_app_key_env: str | None = None
    vendor_app_secret_env: str | None = None
    timeout_seconds: float = Field(default=30, ge=1, le=120)
    max_retries: int = Field(default=2, ge=0, le=5)
    max_pages: int = Field(default=10000, ge=1, le=100000)
    max_response_bytes: int = Field(default=2097152, ge=1024, le=16777216)
    requests_per_second: float = Field(default=5, ge=.1, le=20)

    @model_validator(mode="after")
    def validate_protocol(self) -> "SourceConfig":
        try:
            url = urlsplit(self.url)
            valid_url = (url.scheme in {"http", "https"} and url.hostname and not url.username
                         and not url.password and not url.query and not url.fragment and url.port != 0)
        except ValueError:
            valid_url = False
        if not valid_url or any(ord(char) <= 32 for char in self.url):
            raise ValueError("源URL须为无凭据、查询串或片段的HTTP端点")
        names = [self.date_param, self.page_param, self.page_size_param]
        if len(set(names)) != 3 or any(not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,119}", name) for name in names):
            raise ValueError("分页参数名须唯一且有效")
        if self.method == "GET" and self.parameter_location != "query":
            raise ValueError("GET仅支持query参数")
        if set(names) & self.static_params.keys() or len(self.static_params) > 50:
            raise ValueError("静态参数不能覆盖日期或分页参数")
        if any(not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,119}", key) for key in self.static_params):
            raise ValueError("静态参数名无效")
        if any(re.search(r"secret|password|token|authorization|appkey", key, re.I) for key in self.static_params):
            raise ValueError("凭据必须使用环境变量头部引用")
        if (self.success_field is None) != (self.success_value is None):
            raise ValueError("成功标记路径和值必须同时配置")
        paths = [self.rows_path, *self.field_mapping.values()]
        if self.success_field:
            paths.append(self.success_field)
        if any(path != "$" and not re.fullmatch(r"[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)*", path) for path in paths):
            raise ValueError("响应字段路径无效")
        if len(self.field_mapping) > 80 or any(not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_]{0,62}", key) for key in self.field_mapping):
            raise ValueError("字段映射无效")
        if (len(set(self.date_fields_from_datetime)) != len(self.date_fields_from_datetime)
                or any(not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,62}", key) for key in self.date_fields_from_datetime)):
            raise ValueError("日期转换字段无效")
        if (len(set(self.text_whitespace_fields)) != len(self.text_whitespace_fields)
                or any(not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,62}", key) for key in self.text_whitespace_fields)):
            raise ValueError("文本空白转换字段无效")
        try:
            ZoneInfo(self.source_timezone)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("源时区无效") from None
        if len(self.header_env) > 20:
            raise ValueError("请求头数量超限")
        blocked = {"host", "content-length", "transfer-encoding", "accept-encoding", "connection"}
        for header, variable in self.header_env.items():
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9-]{0,100}", header) or header.lower() in blocked:
                raise ValueError("请求头名称无效")
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,119}", variable):
                raise ValueError("环境变量引用无效")
        vendor = (self.vendor_api_id, self.vendor_app_key_env, self.vendor_app_secret_env)
        if self.protocol == "vendor_batch":
            if not all(vendor) or self.page_size > 200:
                raise ValueError("供应商协议须配置接口ID、环境密钥及至多200条分页")
            if self.method != "POST" or self.parameter_location != "json" or self.static_params:
                raise ValueError("供应商协议固定POST JSON且不接收静态参数")
            for variable in vendor[1:]:
                if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,119}", variable or ""):
                    raise ValueError("供应商凭据环境变量引用无效")
        elif any(vendor):
            raise ValueError("通用协议不接收供应商专用参数")
        return self


class SourceFile(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    sources: list[SourceConfig] = Field(default_factory=list, max_length=100)


def load_sources(settings: Settings) -> dict[str, SourceConfig]:
    path = Path(settings.source_config_path)
    if not path.exists():
        raise SourceError("SOURCE_CONFIG_MISSING", "源配置文件不存在")
    try:
        if path.stat().st_size > 1048576:
            raise ValueError("size")
        document = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object,
                              parse_constant=_reject_constant)
        configs = SourceFile.model_validate(document).sources
    except (OSError, ValueError, ValidationError, RecursionError):
        raise SourceError("SOURCE_CONFIG_INVALID", "源配置格式或协议参数无效") from None
    result = {}
    for config in configs:
        validate_host(config, settings)
        if config.dataset_slug in result:
            raise SourceError("SOURCE_CONFIG_INVALID", "同一数据集不能配置多个数据源")
        result[config.dataset_slug] = config
    return result


def validate_host(config: SourceConfig, settings: Settings) -> None:
    hostname = (urlsplit(config.url).hostname or "").lower()
    if hostname != config._approved_host and hostname not in settings.allowed_source_hosts:
        raise SourceError("SOURCE_HOST_NOT_ALLOWED", "源主机不在部署白名单中")


def generate_sign(secret: str, timestamp: str) -> str:
    # 仅兼容第三方签名协议，传输保密依赖HTTPS。
    return hashlib.md5(("timestamp" + timestamp + secret).encode(), usedforsecurity=False).hexdigest().upper()


def _reject_constant(value: str) -> None:
    raise ValueError("non-finite")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate-key")
        result[key] = value
    return result


def _path(document: Any, path: str) -> Any:
    if path == "$":
        return document
    for part in path.split("."):
        if not isinstance(document, dict) or part not in document:
            raise SourceError("RESPONSE_SCHEMA_CHANGED", "源响应缺少已配置字段路径")
        document = document[part]
    return document


class SourceClient:
    def __init__(self, config: SourceConfig, settings: Settings, *,
                 transport: httpx.BaseTransport | None = None, heartbeat: Callable[[], None] | None = None,
                 monotonic: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep):
        validate_host(config, settings)
        self.config = config
        self._heartbeat = heartbeat
        self._monotonic = monotonic
        self._sleep = sleep
        self._last_started: float | None = None
        self._client = httpx.Client(timeout=httpx.Timeout(config.timeout_seconds, connect=min(5, config.timeout_seconds)),
                                    follow_redirects=False, trust_env=False, transport=transport)

    def __enter__(self) -> "SourceClient":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def iter_pages(self, business_date: date) -> Iterator[list[dict[str, Any]]]:
        for index in range(self.config.page_start, self.config.page_start + self.config.max_pages):
            rows = self.query_page(business_date, index)
            yield rows
            if not rows:
                return
        raise SourceError("PAGE_LIMIT_REACHED", "达到分页上限但未观察到空页，整日不发布")

    def query_page(self, business_date: date, page_index: int) -> list[dict[str, Any]]:
        if isinstance(page_index, bool) or not isinstance(page_index, int) or page_index < self.config.page_start:
            raise SourceError("INVALID_PAGE", "源分页参数无效")
        for attempt in range(self.config.max_retries + 1):
            try:
                return self._request(business_date, page_index)
            except _Retryable as error:
                if attempt == self.config.max_retries:
                    raise SourceError(error.code, error.message) from None
                self._sleep(min(30.0, max(2.0 ** attempt, error.retry_after)))
        raise AssertionError("有限重试循环未结束")

    def _secret(self, variable: str) -> str:
        # 数据库来源只能读自己已加密的凭据，不能把进程中的其他秘密当作HTTP请求头发送。
        value = (self.config._managed_secrets.get(variable) if self.config._managed_secrets is not None
                 else os.environ.get(variable))
        if not value or len(value) > 8192 or any(ord(char) < 32 or ord(char) == 127 or 0xD800 <= ord(char) <= 0xDFFF for char in value):
            raise SourceError("SOURCE_SECRET_MISSING", "源凭据环境变量缺失或无效")
        return value

    def _request(self, business_date: date, page_index: int) -> list[dict[str, Any]]:
        config = self.config
        if self._heartbeat:
            self._heartbeat()
        headers = {header: self._secret(variable) for header, variable in config.header_env.items()}
        headers["Accept-Encoding"] = "identity"
        payload = dict(config.static_params)
        payload.update({config.date_param: business_date.isoformat(), config.page_param: page_index,
                        config.page_size_param: config.page_size})
        if config.protocol == "vendor_batch":
            timestamp = str(int(time.time() * 1000))
            headers.update({"appKey": self._secret(config.vendor_app_key_env or ""), "timestamp": timestamp,
                            "sign": generate_sign(self._secret(config.vendor_app_secret_env or ""), timestamp)})
            payload = {"apiId": config.vendor_api_id, "pageIndex": page_index, "pageSize": config.page_size,
                       "batchQueryParams": [{"openFieldName": config.date_param,
                                             "openFieldValue": business_date.isoformat()}]}
        if any(not value.isascii() for value in headers.values()):
            raise SourceError("SOURCE_HEADER_SECRET_INVALID", "请求头凭据只接受ASCII字符，请填写来源要求的编码值")
        if self._last_started is not None:
            self._sleep(max(0, 1 / config.requests_per_second - (self._monotonic() - self._last_started)))
        self._last_started = self._monotonic()
        started = self._last_started
        try:
            arguments = {"params" if config.parameter_location == "query" else "json": payload}
            with self._client.stream(config.method, config.url, headers=headers, **arguments) as response:
                if response.status_code in {429, 500, 502, 503, 504}:
                    value = response.headers.get("Retry-After", "0")
                    seconds = float(value[:10]) if value.isascii() and value.isdigit() else 0
                    raise _Retryable("UPSTREAM_UNAVAILABLE", "源接口暂时不可用或限频", seconds)
                if not 200 <= response.status_code < 300:
                    raise SourceError("UPSTREAM_HTTP_ERROR", "源接口未返回成功HTTP状态")
                if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                    raise SourceError("RESPONSE_ENCODING_UNSUPPORTED", "源接口未遵守未压缩响应协商")
                buffer = bytearray()
                for chunk in response.iter_bytes(chunk_size=65536):
                    if self._monotonic() - started > config.timeout_seconds:
                        raise _Retryable("UPSTREAM_TIMEOUT", "源接口响应超时")
                    if len(buffer) + len(chunk) > config.max_response_bytes:
                        raise SourceError("RESPONSE_TOO_LARGE", "源响应超过大小上限")
                    buffer.extend(chunk)
                    if self._heartbeat:
                        self._heartbeat()
        except httpx.TimeoutException:
            raise _Retryable("UPSTREAM_TIMEOUT", "源接口响应超时") from None
        except httpx.RequestError:
            raise _Retryable("UPSTREAM_NETWORK_ERROR", "源接口网络连接失败") from None
        return self._validate(bytes(buffer))

    def _validate(self, payload: bytes) -> list[dict[str, Any]]:
        try:
            document = json.loads(payload, parse_float=Decimal, parse_constant=_reject_constant,
                                  object_pairs_hook=_unique_object)
        except (ValueError, UnicodeError, RecursionError):
            raise SourceError("INVALID_RESPONSE", "源接口未返回有效JSON") from None
        config = self.config
        if config.protocol == "vendor_batch":
            if not isinstance(document, dict) or document.get("success") is not True or document.get("code") != "200":
                raise SourceError("UPSTREAM_REJECTED", "源接口未返回已确认成功标志")
            if document.keys() - {"success", "code", "data", "msg", "traceId", "extraInfo"} or document.get("extraInfo") is not None:
                raise SourceError("RESPONSE_SCHEMA_CHANGED", "供应商响应结构与约定不符")
            rows = document.get("data")
        else:
            if config.success_field:
                success = _path(document, config.success_field)
                if type(success) is not type(config.success_value) or success != config.success_value:
                    raise SourceError("UPSTREAM_REJECTED", "源接口未返回配置的成功标志")
            rows = _path(document, config.rows_path)
        if not isinstance(rows, list) or len(rows) > config.page_size or any(not isinstance(row, dict) for row in rows):
            raise SourceError("RESPONSE_SCHEMA_CHANGED", "源数据列表类型或每页条数与配置不符")
        if config.field_mapping:
            # 映射是显式选列；未映射字段不进入本地类型表，缺少已映射源字段仍整批失败。
            rows = [{target: _path(row, origin) for target, origin in config.field_mapping.items()} for row in rows]
        if config.text_whitespace_fields:
            for row in rows:
                for field in config.text_whitespace_fields:
                    if field not in row:
                        raise SourceError("RESPONSE_SCHEMA_CHANGED", "源记录缺少已配置文本转换字段")
                    value = row[field]
                    if value is None:
                        continue
                    if not isinstance(value, str):
                        raise SourceError("INVALID_SOURCE_TEXT", "文本空白转换只接受字符串或NULL")
                    # 仅按管理员明确勾选转换；不裁剪首尾、不折叠已有空格、不删除其他控制字符。
                    row[field] = re.sub(r"\r\n|[\r\n\t]", " ", value)
        if config.date_fields_from_datetime:
            source_zone = ZoneInfo(config.source_timezone)
            for row in rows:
                for field in config.date_fields_from_datetime:
                    if field not in row:
                        raise SourceError("RESPONSE_SCHEMA_CHANGED", "源记录缺少已配置日期转换字段")
                    value = row[field]
                    if value is None:
                        continue
                    if not isinstance(value, str):
                        raise SourceError("INVALID_SOURCE_DATE", "源日期转换只接受明确格式的日期或日期时间字符串")
                    try:
                        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                            row[field] = date.fromisoformat(value).isoformat()
                        elif re.fullmatch(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})?", value):
                            timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
                            if timestamp.tzinfo is None:
                                timestamp = timestamp.replace(tzinfo=source_zone)
                            row[field] = timestamp.astimezone(source_zone).date().isoformat()
                        else:
                            raise ValueError("format")
                    except (ValueError, OverflowError):
                        raise SourceError("INVALID_SOURCE_DATE", "源日期转换失败，整日不发布") from None
        return rows
