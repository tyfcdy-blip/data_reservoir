"""只读DDL审核草稿接口；使用既有管理员登录，不执行或持久化输入SQL。"""

from collections.abc import Callable
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from data_service.ddl import DDLParseError, MAX_SQL_BYTES, parse_ddl
from data_service.errors import ServiceError
from data_service.http_support import success


class DDLPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    sql: str = Field(min_length=1, max_length=MAX_SQL_BYTES, repr=False)


def build_router(administrator: Callable) -> APIRouter:
    router = APIRouter(prefix="/api/v1/admin/datasets", tags=["数据表配置"])
    Admin = Annotated[dict, Depends(administrator)]

    @router.post("/ddl-preview", summary="解析建表语句为审核草稿（不创建数据表）")
    def preview(payload: DDLPreviewRequest, _admin: Admin):
        try:
            return success(parse_ddl(payload.sql).model_dump())
        except DDLParseError as exc:
            raise ServiceError(422, "DDL_INVALID", str(exc)) from exc

    return router
