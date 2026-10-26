"""来源管理的管理员路由，复用主服务认证依赖和统一响应。"""

from collections.abc import Callable
from datetime import date
import re
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, field_validator

from data_service.config import Settings
from data_service.http_support import success
from data_service.source_registry import SourceSave, get_source, list_sources, save_source, test_source


class SourceTestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    business_date: date

    @field_validator("business_date", mode="before")
    @classmethod
    def iso_date(cls, value: object) -> object:
        if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError("日期必须使用YYYY-MM-DD格式")
        return value


def build_router(settings: Settings, administrator: Callable) -> APIRouter:
    router = APIRouter(prefix="/api/v1/admin/sources", tags=["来源配置"])
    Admin = Annotated[dict, Depends(administrator)]

    @router.get("")
    def sources(_admin: Admin):
        return success(list_sources(settings))

    @router.get("/{dataset_slug}")
    def source(dataset_slug: str, _admin: Admin):
        return success(get_source(settings, dataset_slug))

    @router.put("/{dataset_slug}")
    def save(dataset_slug: str, payload: SourceSave, _admin: Admin):
        return success(save_source(settings, dataset_slug, payload))

    @router.post("/{dataset_slug}/test")
    def test(dataset_slug: str, payload: SourceTestRequest, _admin: Admin):
        return success(test_source(settings, dataset_slug, payload.business_date))

    return router
