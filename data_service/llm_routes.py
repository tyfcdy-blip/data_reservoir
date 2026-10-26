"""模型配置与自然语言草稿仅限管理员；不会自动修改开放查询服务。"""

from typing import Annotated

from fastapi import APIRouter, Depends

from data_service import llm_client, llm_config, query_drafts
from data_service.config import Settings
from data_service.http_support import success
from data_service.llm_config import LlmSave
from data_service.query_drafts import DraftRequest


def build_router(settings: Settings, administrator) -> APIRouter:
    router = APIRouter()
    Admin = Annotated[dict, Depends(administrator)]

    @router.get('/api/v1/admin/llm', tags=['大模型配置'])
    def get_configuration(_admin: Admin):
        return success(llm_config.get_config(settings))

    @router.put('/api/v1/admin/llm', tags=['大模型配置'])
    def save_configuration(payload: LlmSave, _admin: Admin):
        return success(llm_config.save_config(settings, payload))

    @router.post('/api/v1/admin/llm/test', tags=['大模型配置'])
    def test_configuration(payload: LlmSave, _admin: Admin):
        with llm_config.call_configuration(settings, payload) as configuration:
            return success(llm_client.test_connection(configuration))

    @router.post('/api/v1/admin/query-services/draft', tags=['加工配置'])
    def generate_draft(payload: DraftRequest, _admin: Admin):
        return success(query_drafts.generate(settings, payload))

    return router
