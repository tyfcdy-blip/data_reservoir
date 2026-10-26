"""加工规则的管理协议和受控开放查询协议。"""

from typing import Annotated

from fastapi import APIRouter, Depends

from data_service import access, query_services
from data_service.config import Settings
from data_service.http_support import success
from data_service.query_services import PreviewRequest, QueryServiceSave, ServiceQuery


def build_router(settings: Settings, administrator, caller) -> APIRouter:
    router = APIRouter()
    Admin = Annotated[dict, Depends(administrator)]
    Caller = Annotated[access.Principal, Depends(caller)]

    @router.get("/api/v1/admin/query-services", tags=["加工配置"])
    def list_configurations(_admin: Admin):
        return success(query_services.list_services(settings))

    @router.post("/api/v1/admin/query-services/preview", tags=["加工配置"])
    def preview(payload: PreviewRequest, _admin: Admin):
        return success(query_services.preview(settings, payload))

    @router.get("/api/v1/admin/query-services/{slug}", tags=["加工配置"])
    def get_configuration(slug: str, _admin: Admin):
        return success(query_services.get_service(settings, slug))

    @router.put("/api/v1/admin/query-services/{slug}", tags=["加工配置"])
    def save_configuration(slug: str, payload: QueryServiceSave, _admin: Admin):
        return success(query_services.save_service(settings, slug, payload))

    @router.get("/api/v1/services", tags=["开放查询"])
    def public_services(principal: Caller):
        return success(query_services.list_services(settings, principal))

    @router.post("/api/v1/services/{slug}/query", tags=["开放查询"])
    def public_query(slug: str, payload: ServiceQuery, principal: Caller):
        return success(query_services.run_service(settings, slug, payload, principal))

    return router
