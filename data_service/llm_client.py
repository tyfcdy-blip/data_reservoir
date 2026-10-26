"""有界的OpenAI兼容HTTP调用；不重试计费请求，不记录供应商响应原文。"""

import asyncio
import json
import socket
from typing import Any
from urllib.parse import urlsplit

import httpx

from data_service.errors import ServiceError
from data_service.llm_config import LlmConnection, blocked_address

MAX_RESPONSE_BYTES = 262144
MAX_REQUEST_BYTES = 262144


def strict_json(text: str) -> Any:
    def pairs(values: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in values:
            if key in result:
                raise ValueError('重复JSON键')
            result[key] = value
        return result

    def reject_constant(_value: str) -> None:
        raise ValueError('非有限JSON数值')

    return json.loads(text, object_pairs_hook=pairs, parse_constant=reject_constant)


def _invalid_response() -> ServiceError:
    return ServiceError(502, 'LLM_INVALID_RESPONSE', '模型未返回可用的结构化结果，请调整描述或检查模型兼容性后重试')


async def _check_addresses(url: str) -> str:
    parts = urlsplit(url)
    try:
        records = await asyncio.get_running_loop().getaddrinfo(parts.hostname, parts.port or
                    (443 if parts.scheme == 'https' else 80), type=socket.SOCK_STREAM)
    except OSError:
        raise ServiceError(502, 'LLM_CONNECTION_FAILED', '无法解析模型服务地址，请检查地址及网络') from None
    if not records or any(blocked_address(record[4][0]) for record in records):
        raise ServiceError(422, 'LLM_ADDRESS_FORBIDDEN', '模型服务地址解析到不允许访问的特殊网络地址')
    # 使用已检查IP连接，Host和TLS SNI仍为原主机名，避免第二次DNS解析重绑定。
    selected = next((record for record in records if record[0] == socket.AF_INET), records[0])
    return str(httpx.URL(url).copy_with(host=selected[4][0]))


def _content(body: bytes, secret: str | None) -> dict[str, Any]:
    try:
        envelope = strict_json(body.decode('utf-8'))
        choices = envelope['choices']
        if not isinstance(choices, list) or len(choices) != 1:
            raise ValueError('choices')
        choice = choices[0]
        if choice.get('finish_reason') not in (None, 'stop'):
            raise ValueError('incomplete')
        message = choice['message']
        if message.get('refusal') or message.get('tool_calls'):
            raise ValueError('refusal')
        content = message['content']
        if not isinstance(content, str) or (secret and secret in content):
            raise ValueError('content')
        document = strict_json(content)
        if not isinstance(document, dict):
            raise ValueError('document')
        return document
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError, AttributeError):
        raise _invalid_response() from None


async def _complete(configuration: LlmConnection, messages: list[dict[str, str]], token_budget: int) -> dict[str, Any]:
    address = configuration.base_url
    if not address.endswith('/chat/completions'):
        address += '/chat/completions'
    payload = {'model': configuration.model, 'messages': messages, 'stream': False,
               configuration.token_parameter: token_budget}
    encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode('utf-8')
    if len(encoded) > MAX_REQUEST_BYTES:
        raise ServiceError(422, 'LLM_INPUT_TOO_LARGE', '表结构和描述过长，请缩短字段说明或需求描述')
    headers = {'Content-Type': 'application/json', 'Accept': 'application/json', 'Accept-Encoding': 'identity'}
    if configuration.api_key:
        headers['Authorization'] = 'Bearer ' + configuration.api_key
    try:
        # 总时限包含DNS、连接、响应头及完整响应读取，慢速分块不能无限续期。
        async with asyncio.timeout(configuration.timeout_seconds):
            target = await _check_addresses(address)
            headers['Host'] = httpx.URL(address).netloc.decode('ascii')
            extensions = {'sni_hostname': urlsplit(address).hostname}
            timeout = httpx.Timeout(configuration.timeout_seconds, connect=min(10, configuration.timeout_seconds))
            async with httpx.AsyncClient(timeout=timeout, trust_env=False, follow_redirects=False) as client:
                async with client.stream('POST', target, content=encoded, headers=headers, extensions=extensions) as response:
                    if response.status_code in (401, 403):
                        raise ServiceError(502, 'LLM_AUTH_FAILED', '模型服务鉴权失败，请检查API KEY及模型访问权限')
                    if response.status_code == 429:
                        raise ServiceError(503, 'LLM_RATE_LIMITED', '模型服务限流或额度不足，请检查供应商账户后重试')
                    if response.status_code < 200 or response.status_code >= 300:
                        raise ServiceError(502, 'LLM_UPSTREAM_FAILED', '模型服务请求失败，请检查地址、模型名称及兼容参数')
                    if response.headers.get('content-encoding', 'identity').lower() not in ('', 'identity'):
                        raise _invalid_response()
                    length = response.headers.get('content-length')
                    if length and (not length.isdigit() or int(length) > MAX_RESPONSE_BYTES):
                        raise _invalid_response()
                    body = bytearray()
                    async for chunk in response.aiter_bytes(chunk_size=16384):
                        body.extend(chunk)
                        if len(body) > MAX_RESPONSE_BYTES:
                            raise _invalid_response()
            return _content(bytes(body), configuration.api_key)
    except (TimeoutError, httpx.TimeoutException):
        raise ServiceError(504, 'LLM_TIMEOUT', '模型响应超时，请缩短描述或调整超时时间后重试') from None
    except (httpx.HTTPError, OSError, UnicodeError, ValueError):
        raise ServiceError(502, 'LLM_CONNECTION_FAILED', '无法连接模型服务，请检查地址、证书及网络') from None


def complete(configuration: LlmConnection, messages: list[dict[str, str]], token_budget: int = 6000) -> dict[str, Any]:
    # FastAPI同步路由在线程池内执行；异步客户端在此独立事件循环中提供严格总超时。
    return asyncio.run(_complete(configuration, messages, token_budget))


def test_connection(configuration: LlmConnection) -> dict[str, Any]:
    response = complete(configuration, [
        {'role': 'system', 'content': 'Return a JSON object only, without Markdown or explanation.'},
        {'role': 'user', 'content': 'Return exactly {"connection":"ok"}.'}], token_budget=100)
    if response != {'connection': 'ok'}:
        raise _invalid_response()
    return {'ok': True, 'message': '连接成功，模型已按要求响应'}
