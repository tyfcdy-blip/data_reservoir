"""独立服务HTTP协议；控制平面只允许管理员，数据平面接受授权机器密钥。"""

from datetime import date
from html import escape
from importlib.resources import files
import logging
import re
from typing import Annotated, Any
import uuid

from fastapi import Depends, FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.openapi.utils import get_openapi
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
import psycopg
from starlette.exceptions import HTTPException

from data_service import access, auth, datasets, db, ingest, query
from data_service.config import Settings, get_settings
from data_service.errors import ServiceError
from data_service.http_support import BodyLimitMiddleware, error, success
from data_service.models import BatchQueryItem, DatasetDefinition, QueryRequest
from data_service.source import SourceError

logger = logging.getLogger(__name__)


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=1024, repr=False)


class SyncRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start_date: date
    end_date: date

    @field_validator("start_date", "end_date", mode="before")
    @classmethod
    def iso_day(cls, value):
        if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError("日期必须为YYYY-MM-DD")
        return value

    @model_validator(mode="after")
    def ordered_dates(self):
        if self.start_date > self.end_date or (self.end_date - self.start_date).days >= 180:
            raise ValueError("日期范围无效")
        return self


class BatchCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    business_date: date
    expected_rows: int = Field(ge=0, le=1000000, strict=True)
    idempotency_key: str = Field(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9_.:-]+$")

    @field_validator("business_date", mode="before")
    @classmethod
    def iso_day(cls, value):
        return SyncRequest.iso_day(value)


class ChunkRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    rows: list[dict[str, Any]] = Field(min_length=1, max_length=1000)


class BatchQueries(BaseModel):
    model_config = ConfigDict(extra="forbid")
    queries: list[BatchQueryItem] = Field(min_length=1, max_length=10)


def _public_dataset(record: dict) -> dict:
    return {key: value for key, value in record.items() if key != "table_name"}


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(title="慕雅居独立数据服务", version="1.6.0", docs_url=None, redoc_url=None)
    app.state.settings = settings
    app.add_middleware(BodyLimitMiddleware, max_bytes=settings.max_request_bytes)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Request-ID"] = str(uuid.uuid4())
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        if request.url.path.startswith("/admin"):
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
                "base-uri 'self'; form-action 'self'; object-src 'none'"
            )
        return response

    @app.exception_handler(ServiceError)
    def service_error(_request, exception):
        return error(exception.status, exception.code, exception.message, exception.details)

    @app.exception_handler(auth.AuthError)
    def auth_error(_request, exception):
        return error(exception.status, exception.code, exception.message)

    @app.exception_handler(SourceError)
    def source_error(_request, exception):
        return error(503, exception.code, exception.message)

    @app.exception_handler(RequestValidationError)
    def validation_error(_request, _exception):
        return error(422, "INVALID_REQUEST", "参数类型、字段或范围不正确，请按接口定义提交")

    @app.exception_handler(psycopg.errors.QueryCanceled)
    def query_timeout(_request, _exception):
        return error(504, "QUERY_TIMEOUT", "查询超过允许时限，请缩小范围或减少维度")

    @app.exception_handler(psycopg.Error)
    def database_error(_request, _exception):
        logger.error("database_request_failed")
        return error(503, "DATABASE_UNAVAILABLE", "数据库暂时不可用")

    @app.exception_handler(HTTPException)
    def http_error(_request, exception):
        return error(exception.status_code, "HTTP_ERROR", "请求的地址或方法不可用")

    @app.exception_handler(Exception)
    def internal_error(_request, _exception):
        logger.error("unexpected_request_failure")
        return error(500, "INTERNAL_ERROR", "服务处理失败，请联系管理员")

    def administrator(authorization: Annotated[str | None, Header()] = None):
        return auth.authenticate(settings, authorization)

    def caller(authorization: Annotated[str | None, Header()] = None,
               x_api_key: Annotated[str | None, Header()] = None):
        return access.authenticate(settings, authorization, x_api_key)

    Admin = Annotated[dict, Depends(administrator)]
    Caller = Annotated[access.Principal, Depends(caller)]

    def permitted_batch(batch_id: uuid.UUID, principal: access.Principal):
        batch = ingest.get_batch(settings, batch_id)
        principal.require(batch["dataset_slug"], "write")
        if not principal.admin and batch["owner_key_id"] != principal.key_id:
            raise ServiceError(403, "BATCH_FORBIDDEN", "不能操作其他调用方的导入批次")
        return batch

    @app.get("/", tags=["说明"])
    def home(request: Request):
        if "text/html" in request.headers.get("accept", ""):
            return RedirectResponse("/admin/", status_code=307)
        return success({"service": "慕雅居独立数据服务", "version": "1.6.0", "docs": "/docs", "admin": "/admin/",
                        "openapi": "/openapi.json", "dashboard_included": False})

    @app.get("/health/live", tags=["健康检查"])
    def live():
        return {"status": "ok"}

    @app.get("/health/ready", tags=["健康检查"])
    def ready():
        if not db.ready(settings):
            return error(503, "NOT_INITIALIZED", "服务尚未初始化")
        return {"status": "ready"}

    @app.post("/api/v1/auth/login", tags=["管理员"])
    def login(payload: LoginRequest, request: Request):
        address = request.client.host if request.client else "unknown"
        return success({"token": auth.login(settings, payload.username, payload.password, address),
                        "expires_in_seconds": settings.session_hours * 3600})

    @app.post("/api/v1/auth/logout", tags=["管理员"])
    def logout(admin: Admin):
        auth.logout(settings, admin["token_hash"])
        return success({"logged_out": True})

    @app.post("/api/v1/admin/datasets", tags=["管理员"])
    def register_dataset(payload: DatasetDefinition, _admin: Admin):
        return success(_public_dataset(datasets.register_dataset(settings, payload)), 201)

    @app.post("/api/v1/admin/keys", tags=["管理员"])
    def issue_key(payload: access.KeyCreate, _admin: Admin):
        return success(access.create_key(settings, payload), 201)

    @app.get("/api/v1/admin/keys", tags=["管理员"])
    def keys(_admin: Admin):
        return success(access.list_keys(settings))

    @app.delete("/api/v1/admin/keys/{key_id}", tags=["管理员"])
    def revoke_key(key_id: uuid.UUID, _admin: Admin):
        access.revoke_key(settings, key_id)
        return success({"revoked": True})

    @app.get("/api/v1/datasets", tags=["数据查询"])
    def dataset_list(principal: Caller):
        available = datasets.list_datasets(settings)
        return success([_public_dataset(record) for record in available
                        if principal.admin or (principal.grants or {}).get(record["slug"])])

    @app.get("/api/v1/datasets/{slug}", tags=["数据查询"])
    def dataset_detail(slug: str, principal: Caller):
        principal.require(slug, "read")
        with db.connect(settings) as connection:
            return success(_public_dataset(datasets.get_dataset(connection, slug)))

    @app.post("/api/v1/datasets/{slug}/query", tags=["数据查询"])
    def dataset_query(slug: str, payload: QueryRequest, principal: Caller):
        principal.require(slug, "read")
        return success(query.run_query(settings, slug, payload, row_scope=principal.scope_for(slug)))

    @app.post("/api/v1/query/batch", tags=["数据查询"])
    def batch_query(payload: BatchQueries, principal: Caller):
        for item in payload.queries:
            principal.require(item.dataset, "read")
        return success(query.run_batch(settings, payload.queries,
                                       row_scopes=principal.policy.row_scopes if principal.policy is not None and not principal.admin else None))

    @app.post("/api/v1/datasets/{slug}/batches", tags=["分批导入"])
    def create_batch(slug: str, payload: BatchCreate, principal: Caller):
        principal.require(slug, "write")
        return success(ingest.create_batch(settings, slug, payload.business_date, payload.expected_rows,
                                          payload.idempotency_key, principal.key_id), 201)

    @app.get("/api/v1/batches/{batch_id}", tags=["分批导入"])
    def batch_detail(batch_id: uuid.UUID, principal: Caller):
        return success(permitted_batch(batch_id, principal))

    @app.put("/api/v1/batches/{batch_id}/chunks/{chunk_index}", tags=["分批导入"])
    def upload_chunk(batch_id: uuid.UUID, chunk_index: int, payload: ChunkRequest, principal: Caller):
        permitted_batch(batch_id, principal)
        return success(ingest.append_chunk(settings, batch_id, chunk_index, payload.rows, principal.key_id))

    @app.post("/api/v1/batches/{batch_id}/commit", tags=["分批导入"])
    def commit_batch(batch_id: uuid.UUID, principal: Caller):
        permitted_batch(batch_id, principal)
        return success(ingest.commit_batch(settings, batch_id, principal.key_id))

    @app.post("/api/v1/batches/{batch_id}/cancel", tags=["分批导入"])
    def cancel_batch(batch_id: uuid.UUID, principal: Caller):
        permitted_batch(batch_id, principal)
        return success(ingest.cancel_batch(settings, batch_id, principal.key_id))

    @app.post("/api/v1/admin/datasets/{slug}/sync", tags=["管理员"])
    def sync_dataset(slug: str, payload: SyncRequest, _admin: Admin):
        from data_service.source_registry import load_active_sources
        if slug not in load_active_sources(settings):
            raise ServiceError(409, "SOURCE_NOT_CONFIGURED", "数据集尚未配置HTTP来源")
        return success(ingest.enqueue_sync(settings, slug, payload.start_date, payload.end_date), 202)

    @app.get("/api/v1/admin/status", tags=["管理员"])
    def status(_admin: Admin):
        from data_service.source_registry import load_active_sources
        sources = load_active_sources(settings)
        with db.connect(settings) as connection:
            heartbeat = connection.execute(
                """SELECT worker_key,status,heartbeat_at,
                (heartbeat_at BETWEEN clock_timestamp()-interval '90 seconds' AND clock_timestamp()
                 AND status IN ('idle','syncing')) AS healthy
                FROM worker_heartbeat ORDER BY worker_key"""
            ).fetchall()
            jobs = connection.execute(
                "SELECT * FROM batches ORDER BY created_at DESC,id DESC LIMIT 100").fetchall()
            coverage = connection.execute(
                "SELECT dataset_slug,min(business_date) AS first_date,max(business_date) AS last_date,"
                "count(*) AS published_days,sum(row_count) AS row_count FROM published_days GROUP BY dataset_slug"
            ).fetchall()
        return success({"workers": heartbeat, "recent_batches": jobs, "coverage": coverage,
                        "configured_sources": list(sources), "schedule_enabled": settings.schedule_enabled})

    @app.get("/docs", include_in_schema=False, response_class=HTMLResponse)
    def local_docs():
        paths = app.openapi()["paths"]
        rows = "".join("<tr><td>" + escape(method.upper()) + "</td><td><code>" + escape(path)
                       + "</code></td><td>" + escape(operation.get("summary", "")) + "</td></tr>"
                       for path, methods in paths.items() for method, operation in methods.items())
        return """<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>慕雅居数据服务接口</title>
        <style>body{font:16px/1.7 system-ui;margin:40px auto;max-width:1100px;padding:0 24px;color:#142b42}
        table{border-collapse:collapse;width:100%}td,th{border:1px solid #dce4ec;padding:8px;text-align:left}
        code{color:#195fa6}a{color:#195fa6}</style><h1>慕雅居独立数据服务</h1>
        <p>多数据集接入、分批导入、日期范围查询、分组聚合和批量查询。此项目不包含经营看板。</p>
        <p>管理接口先登录，再传 Authorization: Bearer &lt;token&gt;；开放数据接口传 X-API-Key。
        密钥支持原有整表 read / write 授权，或只读接口与行级数据范围授权。金额以十进制字符串返回，NULL保留。</p>
        <p><a href="/admin/">进入管理员配置后台</a> · <a href="/openapi.json">下载完整OpenAPI定义</a>，可导入 Apifox / Postman 等接口工具。
        完整请求样例与部署步骤见项目README及docs/api-contract.md。</p>
        <table><tr><th>方法</th><th>路径</th><th>操作</th></tr>""" + rows + "</table></html>"

    def openapi_schema():
        if app.openapi_schema:
            return app.openapi_schema
        schema = get_openapi(title=app.title, version=app.version, routes=app.routes)
        schema.setdefault("components", {})["securitySchemes"] = {
            "AdminSession": {"type": "http", "scheme": "bearer", "description": "管理员登录返回的短期会话"},
            "DatasetKey": {"type": "apiKey", "in": "header", "name": "X-API-Key",
                           "description": "支持整表read/write或只读接口及行级范围授权的机器密钥"},
        }
        for path, methods in schema["paths"].items():
            for operation in methods.values():
                if path.startswith("/api/v1/admin/") or path == "/api/v1/auth/logout":
                    operation["security"] = [{"AdminSession": []}]
                elif path.startswith("/api/v1/") and path != "/api/v1/auth/login":
                    operation["security"] = [{"DatasetKey": []}, {"AdminSession": []}]
        app.openapi_schema = schema
        return schema

    from data_service.ddl_routes import build_router as ddl_router
    from data_service.llm_routes import build_router as llm_router
    from data_service.query_service_routes import build_router as query_service_router
    from data_service.source_routes import build_router as source_router
    app.include_router(ddl_router(administrator))
    app.include_router(llm_router(settings, administrator))
    app.include_router(query_service_router(settings, administrator, caller))
    app.include_router(source_router(settings, administrator))
    app.mount("/admin", StaticFiles(directory=str(files("data_service").joinpath("static")), html=True,
                                   check_dir=False), name="admin")
    app.openapi = openapi_schema
    return app
