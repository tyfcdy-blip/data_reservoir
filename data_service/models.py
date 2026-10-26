"""数据集和开放查询的严格输入模型，仅允许声明式查询及受限的聚合后算术公式。"""

from datetime import date
from typing import Any, Literal
import re

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from data_service.formulas import parse_formula


IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,62}$")
SLUG = re.compile(r"^[a-z][a-z0-9_]{0,47}$")


def identifier(value: str) -> str:
    if not IDENTIFIER.fullmatch(value):
        raise ValueError("字段及别名须以英文字母开头，仅含字母数字下划线，最长63字符")
    return value


def strict_date(value: Any) -> date:
    # 避免自动将时间戳、日期时间或数字静默转成业务日期。
    if type(value) is date:
        return value
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("日期必须是 YYYY-MM-DD")
    return date.fromisoformat(value)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True, hide_input_in_errors=True)


class FieldDefinition(StrictModel):
    name: str
    type: Literal["string", "integer", "decimal", "date", "datetime", "boolean"]
    description: str = Field(default="", max_length=500, strict=True)
    required: bool = Field(default=False, strict=True)
    dimension: bool = Field(default=False, strict=True)
    max_length: int = Field(default=255, ge=1, le=10000, strict=True)

    _name = field_validator("name")(identifier)

    @field_validator("description")
    @classmethod
    def check_description(cls, value: str) -> str:
        if any(ord(char) < 32 or 0x7F <= ord(char) <= 0x9F or 0xD800 <= ord(char) <= 0xDFFF for char in value):
            raise ValueError("字段说明不可包含控制字符或非法Unicode")
        return value


class DatasetDefinition(StrictModel):
    slug: str
    name: str = Field(min_length=1, max_length=120)
    date_field: str
    key_fields: list[str] = Field(min_length=1, max_length=5)
    fields: list[FieldDefinition] = Field(min_length=1, max_length=80)
    retention_days: int = Field(default=180, ge=1, le=180, strict=True)

    @field_validator("slug")
    @classmethod
    def check_slug(cls, value: str) -> str:
        if not SLUG.fullmatch(value):
            raise ValueError("数据集标识须为小写字母开头的字母数字下划线，最长48字符")
        return value

    @field_validator("name")
    @classmethod
    def check_name(cls, value: str) -> str:
        if not value.strip() or any(ord(char) < 32 or 0xD800 <= ord(char) <= 0xDFFF for char in value):
            raise ValueError("数据集名称不可为空白或包含控制字符")
        return value

    @model_validator(mode="after")
    def check_schema(self) -> "DatasetDefinition":
        names = [field.name for field in self.fields]
        if len(names) != len(set(names)):
            raise ValueError("字段名称不可重复")
        fields = {field.name: field for field in self.fields}
        if len(self.key_fields) != len(set(self.key_fields)):
            raise ValueError("主键字段不可重复")
        if any(name not in fields or not fields[name].required for name in self.key_fields):
            raise ValueError("主键必须引用存在且必填的字段")
        day = fields.get(self.date_field)
        if day is None or day.type != "date" or not day.required:
            raise ValueError("业务日期字段必须存在，类型为date且必填")
        return self


class Filter(StrictModel):
    field: str
    op: Literal["eq", "in", "gt", "gte", "lt", "lte", "is_null"]
    value: Any = None

    _field = field_validator("field")(identifier)

    @model_validator(mode="after")
    def check_value(self) -> "Filter":
        if self.op == "is_null":
            if type(self.value) is not bool:
                raise ValueError("is_null.value必须为boolean")
        elif self.op == "in":
            if not isinstance(self.value, list) or not 1 <= len(self.value) <= 200:
                raise ValueError("in需要1到200个值")
            if any(value is None for value in self.value):
                raise ValueError("NULL请使用is_null筛选")
        elif self.value is None or isinstance(self.value, (list, dict)):
            raise ValueError("比较值必须为非NULL标量；NULL请使用is_null")
        return self


class Aggregate(StrictModel):
    field: str | None = None
    op: Literal["sum", "avg", "min", "max", "count", "count_distinct"]
    alias: str

    _alias = field_validator("alias")(identifier)

    @model_validator(mode="after")
    def check_field(self) -> "Aggregate":
        if self.field is not None:
            identifier(self.field)
        elif self.op != "count":
            raise ValueError("除count外聚合必须指定字段")
        return self


class Formula(StrictModel):
    alias: str
    expression: str = Field(min_length=1, max_length=500, strict=True)
    decimal_places: int = Field(default=6, ge=0, le=12, strict=True)

    _alias = field_validator("alias")(identifier)

    @field_validator("expression")
    @classmethod
    def check_expression(cls, value: str) -> str:
        parse_formula(value)
        return value.strip()


class Order(StrictModel):
    field: str
    direction: Literal["asc", "desc"] = "asc"

    _field = field_validator("field")(identifier)


class QueryRequest(StrictModel):
    start_date: date
    end_date: date
    fields: list[str] | None = Field(default=None, min_length=1, max_length=80)
    filters: list[Filter] = Field(default_factory=list, max_length=20)
    group_by: list[str] = Field(default_factory=list, max_length=10)
    aggregates: list[Aggregate] = Field(default_factory=list, max_length=20)
    formulas: list[Formula] = Field(default_factory=list, max_length=10)
    result_filters: list[Filter] = Field(default_factory=list, max_length=20)
    order_by: list[Order] = Field(default_factory=list, max_length=20)
    limit: int = Field(default=100, ge=1, le=500, strict=True)
    offset: int = Field(default=0, ge=0, le=100000, strict=True)
    require_complete: bool = Field(default=True, strict=True)

    _dates = field_validator("start_date", "end_date", mode="before")(strict_date)

    @model_validator(mode="after")
    def check_query(self) -> "QueryRequest":
        if self.start_date > self.end_date or (self.end_date - self.start_date).days >= 180:
            raise ValueError("日期范围须为正序且最多180天")
        if (self.formulas or self.result_filters) and not self.aggregates:
            raise ValueError("公式及汇总后筛选必须先配置汇总指标")
        if self.fields is not None and (self.group_by or self.aggregates or self.formulas or self.result_filters):
            raise ValueError("fields明细模式与group_by/aggregates聚合模式互斥")
        for names in [self.fields or [], self.group_by, [order.field for order in self.order_by]]:
            if len(names) != len(set(names)):
                raise ValueError("字段或排序不可重复")
            for name in names:
                identifier(name)
        columns = self.group_by + [aggregate.alias for aggregate in self.aggregates] + [formula.alias for formula in self.formulas]
        if len(columns) != len(set(columns)):
            raise ValueError("聚合、公式别名与分组字段不可重名")
        return self


class BatchQueryItem(StrictModel):
    dataset: str
    query: QueryRequest

    @field_validator("dataset")
    @classmethod
    def check_dataset(cls, value: str) -> str:
        if not SLUG.fullmatch(value):
            raise ValueError("数据集标识格式非法")
        return value
