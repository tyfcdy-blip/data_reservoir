"""密钥专用的行级授权模型；不接受客户端查询参数作为授权依据。"""

from typing import Literal

import psycopg
from psycopg import sql
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from data_service.errors import ServiceError
from data_service.models import DatasetDefinition, identifier


class PolicyModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)


class ScopeCondition(PolicyModel):
    field: str
    values: list[str] = Field(min_length=1, max_length=100)

    _field = field_validator("field")(identifier)

    @field_validator("values")
    @classmethod
    def valid_values(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("授权值不可重复")
        for value in values:
            if (not value.strip() or len(value) > 10000 or
                    any(ord(char) < 32 or 0x7F <= ord(char) <= 0x9F or
                        0xD800 <= ord(char) <= 0xDFFF for char in value)):
                raise ValueError("授权值不可为空白、过长或含控制字符")
        # 授权是精确匹配，不去除有意义的前后空格。
        return values


class ScopeGroup(PolicyModel):
    conditions: list[ScopeCondition] = Field(min_length=1, max_length=5)

    @model_validator(mode="after")
    def distinct_fields(self) -> "ScopeGroup":
        names = [condition.field for condition in self.conditions]
        if len(names) != len(set(names)):
            raise ValueError("同一授权组的字段不可重复")
        return self


class RowScope(PolicyModel):
    mode: Literal["all", "filtered"]
    groups: list[ScopeGroup] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def valid_mode(self) -> "RowScope":
        if (self.mode == "all" and self.groups) or (self.mode == "filtered" and not self.groups):
            raise ValueError("全部数据不设置条件；限定范围必须至少有一个授权组")
        return self


class KeyPolicy(PolicyModel):
    services: dict[str, str] = Field(default_factory=dict, max_length=100)
    row_scopes: dict[str, RowScope] = Field(min_length=1, max_length=100)

    @field_validator("services")
    @classmethod
    def valid_services(cls, services: dict[str, str]) -> dict[str, str]:
        for service, dataset in services.items():
            DatasetDefinition.check_slug(service)
            DatasetDefinition.check_slug(dataset)
        return services

    @field_validator("row_scopes")
    @classmethod
    def valid_datasets(cls, scopes: dict[str, RowScope]) -> dict[str, RowScope]:
        for slug in scopes:
            DatasetDefinition.check_slug(slug)
        if sum(len(condition.values) for scope in scopes.values()
               for group in scope.groups for condition in group.conditions) > 2000:
            raise ValueError("全部授权值合计不可超过2000个")
        return scopes

    def validate_grants(self, grants: dict[str, list[str]]) -> None:
        if any(actions != ["read"] for actions in grants.values()):
            raise ValueError("带数据范围的密钥只支持读取权限")
        required = set(grants) | set(self.services.values())
        if not required or set(self.row_scopes) != required:
            raise ValueError("每个授权查询表必须且只能配置一个数据范围")


def policy_error() -> ServiceError:
    return ServiceError(403, "KEY_POLICY_INVALID", "密钥的数据授权配置已失效，请联系管理员")


def validate_scope(scope: RowScope, definition: DatasetDefinition) -> None:
    fields = {field.name: field for field in definition.fields}
    for group in scope.groups:
        for condition in group.conditions:
            field = fields.get(condition.field)
            if (field is None or field.type != "string" or not field.dimension or
                    any(len(value) > field.max_length for value in condition.values)):
                raise policy_error()


def validate_scope_datasets(connection: psycopg.Connection, policy: KeyPolicy) -> None:
    rows = connection.execute("SELECT slug,definition FROM datasets WHERE slug=ANY(%s)",
                              (list(policy.row_scopes),)).fetchall()
    if len(rows) != len(policy.row_scopes):
        raise policy_error()
    for row in rows:
        validate_scope(policy.row_scopes[row["slug"]], DatasetDefinition.model_validate(row["definition"]))


def compile_scope(scope: RowScope | None, definition: DatasetDefinition) -> tuple[sql.Composable, list[str]]:
    """独立WHERE条件：组内AND，组间OR，避免跨渠道店铺组合产生笛卡尔越权。"""
    if scope is None:
        return sql.SQL("TRUE"), []
    validate_scope(scope, definition)
    if scope.mode == "all":
        return sql.SQL("TRUE"), []
    groups = []
    values: list[str] = []
    for group in scope.groups:
        conditions = []
        for condition in group.conditions:
            conditions.append(sql.SQL("{} IN ({})").format(
                sql.Identifier("records", condition.field),
                sql.SQL(",").join(sql.Placeholder() for _ in condition.values)))
            values.extend(condition.values)
        groups.append(sql.SQL("({})").format(sql.SQL(" AND ").join(conditions)))
    return sql.SQL("({})").format(sql.SQL(" OR ").join(groups)), values
