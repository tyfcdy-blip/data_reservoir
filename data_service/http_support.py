"""统一无损JSON和请求体边界，不缓存调用者凭据。"""

from datetime import date, datetime
from decimal import Decimal
import json
import uuid

from fastapi.responses import JSONResponse


def json_value(value):
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, dict):
        return {str(key): json_value(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_value(child) for child in value]
    return value


def success(payload, status: int = 200) -> JSONResponse:
    return JSONResponse({"code": 200, "data": json_value(payload)}, status_code=status)


def error(status: int, code: str, message: str, details: dict | None = None) -> JSONResponse:
    body = {"code": status, "error_code": code, "message": message}
    if details:
        body["details"] = json_value(details)
    return JSONResponse(body, status_code=status)


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _nonfinite(_value):
    raise ValueError("nonfinite JSON value")


class BodyLimitMiddleware:
    """读取时累计实际字节，不相信Content-Length，最多缓冲配置上限。"""

    def __init__(self, app, max_bytes: int):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        chunks = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunks.extend(message.get("body", b""))
            if len(chunks) > self.max_bytes:
                response = error(413, "BODY_TOO_LARGE", "请求体超过允许大小，请分批提交")
                return await response(scope, receive, send)
            if not message.get("more_body", False):
                break
        if chunks:
            try:
                json.loads(chunks, object_pairs_hook=_unique_pairs, parse_constant=_nonfinite)
            except (ValueError, UnicodeError, RecursionError):
                return await error(422, "INVALID_JSON", "请求必须为有效JSON且字段不可重复")(scope, receive, send)
        sent = False

        async def buffered_receive():
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": bytes(chunks), "more_body": False}
            return await receive()

        await self.app(scope, buffered_receive, send)
