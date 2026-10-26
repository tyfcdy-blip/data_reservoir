"""管理员保存查询规则，开放调用仅接受日期、分页和允许的筛选。"""

from datetime import date
from typing import Any

from psycopg.types.json import Jsonb
from pydantic import Field, ValidationError, field_validator, model_validator

from data_service import access, db, period_comparison, query
from data_service.config import Settings
from data_service.datasets import get_dataset
from data_service.errors import ServiceError
from data_service.models import Aggregate, DatasetDefinition, Filter, Formula, Order, QueryRequest, StrictModel, strict_date


class QueryTemplate(StrictModel):
    fields: list[str] | None = Field(default=None, min_length=1, max_length=80)
    filters: list[Filter] = Field(default_factory=list, max_length=20)
    group_by: list[str] = Field(default_factory=list, max_length=10)
    aggregates: list[Aggregate] = Field(default_factory=list, max_length=20)
    formulas: list[Formula] = Field(default_factory=list, max_length=10)
    result_filters: list[Filter] = Field(default_factory=list, max_length=20)
    order_by: list[Order] = Field(default_factory=list, max_length=20)
    require_complete: bool = Field(default=True, strict=True)

    @model_validator(mode="after")
    def validate_shape(self):
        # 结构校验始终走现有查询模型，避免两套规则随时间分叉。
        self.request(date(2000, 1, 1), date(2000, 1, 1))
        return self

    def request(self, start_date: date, end_date: date, **kwargs: Any) -> QueryRequest:
        return QueryRequest.model_validate({**self.model_dump(), "start_date": start_date,
                                            "end_date": end_date, **kwargs})


class QueryServiceSave(StrictModel):
    name: str = Field(min_length=1, max_length=120)
    dataset_slug: str
    definition: QueryTemplate
    allowed_filter_fields: list[str] = Field(default_factory=list, max_length=80)
    max_limit: int = Field(default=100, ge=1, le=500, strict=True)
    enabled: bool = Field(default=False, strict=True)
    revision: int = Field(ge=0, le=2147483646, strict=True)

    @field_validator("name")
    @classmethod
    def service_name(cls, value: str) -> str:
        return DatasetDefinition.check_name(value)

    @field_validator("dataset_slug")
    @classmethod
    def dataset_identifier(cls, value: str) -> str:
        return DatasetDefinition.check_slug(value)


class ServiceQuery(StrictModel):
    start_date: date
    end_date: date
    filters: list[Filter] = Field(default_factory=list, max_length=20)
    limit: int | None = Field(default=None, ge=1, le=500, strict=True)
    offset: int = Field(default=0, ge=0, le=100000, strict=True)

    compare_previous_period: bool = Field(default=False, strict=True)

    _dates = field_validator("start_date", "end_date", mode="before")(strict_date)

    @model_validator(mode="after")
    def date_range(self):
        QueryRequest(start_date=self.start_date, end_date=self.end_date)
        return self


class PreviewRequest(StrictModel):
    dataset_slug: str
    definition: QueryTemplate
    start_date: date
    end_date: date
    limit: int = Field(default=20, ge=1, le=100, strict=True)

    _dates = field_validator("start_date", "end_date", mode="before")(strict_date)

    @field_validator("dataset_slug")
    @classmethod
    def dataset_identifier(cls, value: str) -> str:
        return DatasetDefinition.check_slug(value)


def _slug(slug: str) -> None:
    try:
        DatasetDefinition.check_slug(slug)
    except ValueError:
        raise ServiceError(422, "INVALID_SERVICE_SLUG", "接口标识格式不正确") from None


def _get(connection, slug: str) -> dict:
    _slug(slug)
    result = connection.execute("SELECT * FROM query_services WHERE slug=%s", (slug,)).fetchone()
    if result is None:
        raise ServiceError(404, "SERVICE_NOT_FOUND", "查询服务不存在")
    return result


def get_service(settings: Settings, slug: str) -> dict:
    with db.connect(settings) as connection:
        return _get(connection, slug)


def list_services(settings: Settings, principal: access.Principal | None = None) -> list[dict]:
    with db.connect(settings) as connection:
        records = connection.execute("SELECT * FROM query_services ORDER BY created_at DESC,slug").fetchall()
    if principal is None:
        return records
    # 公共目录只返回调用方可以读取的已开放服务，不泄露固定筛选和表配置。
    return [{key: record[key] for key in ("slug", "name", "dataset_slug", "allowed_filter_fields", "max_limit")}
            for record in records if record["enabled"] and
            principal.allows_service(record["slug"], record["dataset_slug"])]


def save_service(settings: Settings, slug: str, payload: QueryServiceSave) -> dict:
    _slug(slug)
    with db.connect(settings) as connection:
        # 锁涵盖首次创建和修订，防止两名操作页互相覆盖配置。
        connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", ("query-service:" + slug,))
        existing = connection.execute("SELECT revision FROM query_services WHERE slug=%s", (slug,)).fetchone()
        revision = existing["revision"] if existing else 0
        if payload.revision != revision:
            raise ServiceError(409, "CONFIG_CONFLICT", "配置已被修改，请重新加载后编辑")
        dataset = get_dataset(connection, payload.dataset_slug)
        template = payload.definition
        query.validate_query_definition(dataset, template.request(date(2000, 1, 1), date(2000, 1, 1)))
        available = {field["name"] for field in dataset["definition"]["fields"]}
        if (len(set(payload.allowed_filter_fields)) != len(payload.allowed_filter_fields)
                or not set(payload.allowed_filter_fields) <= available):
            raise ServiceError(422, "INVALID_FILTER_FIELDS", "可传入筛选字段必须存在且不能重复")
        connection.execute(
            """INSERT INTO query_services
            (slug,name,dataset_slug,definition,allowed_filter_fields,max_limit,enabled,revision)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT(slug) DO UPDATE SET name=EXCLUDED.name,dataset_slug=EXCLUDED.dataset_slug,
            definition=EXCLUDED.definition,allowed_filter_fields=EXCLUDED.allowed_filter_fields,
            max_limit=EXCLUDED.max_limit,enabled=EXCLUDED.enabled,revision=EXCLUDED.revision,updated_at=now()""",
            (slug, payload.name, payload.dataset_slug, Jsonb(template.model_dump(mode="json")),
             Jsonb(payload.allowed_filter_fields), payload.max_limit, payload.enabled, revision + 1),
        )
        return _get(connection, slug)


def preview(settings: Settings, payload: PreviewRequest) -> dict:
    try:
        request = payload.definition.request(payload.start_date, payload.end_date, limit=payload.limit)
    except ValidationError:
        raise ServiceError(422, "INVALID_QUERY", "查询范围或配置不正确") from None
    return query.run_query(settings, payload.dataset_slug, request)


def run_service(settings: Settings, slug: str, payload: ServiceQuery, principal: access.Principal) -> dict:
    with db.connect(settings) as connection:
        # 服务规则及发布数据在同一快照读取，避免查询过程中切换加工版本。
        connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        service = _get(connection, slug)
        if not service["enabled"]:
            raise ServiceError(404, "SERVICE_NOT_FOUND", "查询服务不存在或尚未开放")
        principal.require_service(slug, service["dataset_slug"])
        if any(predicate.field not in service["allowed_filter_fields"] for predicate in payload.filters):
            raise ServiceError(422, "FILTER_NOT_ALLOWED", "该接口未开放所提交的筛选字段")
        limit = payload.limit if payload.limit is not None else min(100, service["max_limit"])
        if limit > service["max_limit"]:
            raise ServiceError(422, "LIMIT_EXCEEDED", "分页条数超过此接口配置的上限")
        template = QueryTemplate.model_validate(service["definition"])
        try:
            request = template.request(payload.start_date, payload.end_date,
                                       filters=template.filters + payload.filters, limit=limit, offset=payload.offset)
        except ValidationError:
            raise ServiceError(422, "INVALID_QUERY", "固定规则和传入筛选合计超出允许范围") from None
        scope = principal.scope_for(service["dataset_slug"])
        if payload.compare_previous_period:
            period_comparison.validate_request(connection, service["dataset_slug"], request)
        result = query.execute_query(connection, service["dataset_slug"], request, row_scope=scope)
        if payload.compare_previous_period:
            period_comparison.attach_comparison(connection, service["dataset_slug"], request, result,
                                                row_scope=scope)
        return result
