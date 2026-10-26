"""不可变类型化数据集注册，以及写入/筛选共用的无损边界校验。"""

from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
import re
from typing import Any
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from data_service.config import Settings
from data_service.db import connect
from data_service.errors import ServiceError
from data_service.models import DatasetDefinition, FieldDefinition, strict_date


TYPE_SQL = {
    "integer": "BIGINT", "decimal": "NUMERIC(38,6)", "date": "DATE",
    "datetime": "TIMESTAMPTZ", "boolean": "BOOLEAN",
}
DECIMAL_QUANTUM = Decimal("0.000001")
DECIMAL_LIMIT = Decimal("1e32")
INTEGER_PATTERN = re.compile(r"^[+-]?\d+$")
DECIMAL_PATTERN = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$")
DATETIME_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$")


def get_dataset(connection: psycopg.Connection, slug: str) -> dict:
    dataset = connection.execute(
        "SELECT slug,name,table_name,definition,created_at FROM datasets WHERE slug=%s", (slug,)
    ).fetchone()
    if dataset is None:
        raise ServiceError(404, "dataset_not_found", "数据集不存在")
    return dataset


def list_datasets(settings: Settings) -> list[dict]:
    with connect(settings) as connection:
        return connection.execute(
            "SELECT slug,name,definition,created_at FROM datasets ORDER BY slug"
        ).fetchall()


def apply_field_comments(
    connection: psycopg.Connection, table_name: str, fields: list[FieldDefinition]
) -> None:
    """在调用方事务内设置列说明；空说明对应无注释，避免元数据与物理列不一致。"""
    for field in fields:
        connection.execute(sql.SQL("COMMENT ON COLUMN {}.{} IS {}").format(
            sql.Identifier(table_name), sql.Identifier(field.name), sql.Literal(field.description or None)
        ))


def register_dataset(settings: Settings, definition: DatasetDefinition) -> dict:
    """DDL和元数据一笔提交；按slug串行注册防止并发创建孤立物理表。"""
    definition = DatasetDefinition.model_validate(definition)
    serialized = definition.model_dump(mode="json")
    with connect(settings) as connection:
        connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", ("dataset:" + definition.slug,))
        existing = connection.execute("SELECT * FROM datasets WHERE slug=%s", (definition.slug,)).fetchone()
        if existing is not None:
            # 旧定义未保存新增的可选元数据时，只补默认值比较，不隐式修改原定义或说明。
            existing_definition = DatasetDefinition.model_validate(existing["definition"]).model_dump(mode="json")
            if existing_definition != serialized:
                raise ServiceError(409, "dataset_schema_conflict", "数据集已存在，禁止隐式修改字段或保留策略")
            return existing
        table_name = "records_" + uuid4().hex
        connection.execute(
            "INSERT INTO datasets(slug,name,table_name,definition) VALUES(%s,%s,%s,%s)",
            (definition.slug, definition.name, table_name, Jsonb(serialized)),
        )
        columns = [
            sql.SQL("_batch_id UUID NOT NULL REFERENCES batches(id) ON DELETE CASCADE"),
            sql.SQL("_date DATE NOT NULL"), sql.SQL("_key TEXT NOT NULL"),
        ]
        for field in definition.fields:
            field_type = sql.SQL("VARCHAR({})").format(sql.Literal(field.max_length)) if field.type == "string" else sql.SQL(TYPE_SQL[field.type])
            columns.append(sql.SQL("{} {} {}").format(
                sql.Identifier(field.name), field_type, sql.SQL("NOT NULL" if field.required else "")
            ))
        columns.append(sql.SQL("PRIMARY KEY (_batch_id,_key)"))
        connection.execute(sql.SQL("CREATE TABLE {} ({})").format(sql.Identifier(table_name), sql.SQL(",").join(columns)))
        apply_field_comments(connection, table_name, definition.fields)
        connection.execute(sql.SQL("CREATE INDEX ON {} (_date,_batch_id)").format(sql.Identifier(table_name)))
        return get_dataset(connection, definition.slug)


def _invalid(field: FieldDefinition, reason: str) -> ServiceError:
    # 只返回字段及原因，不回显原始业务值或外部响应。
    return ServiceError(422, "invalid_record", "字段值不符合数据集定义", {"field": field.name, "reason": reason})


def validate_value(field: FieldDefinition, value: Any, *, allow_null: bool = True) -> Any:
    """所有入口共用类型解释；十进制浮点已失去原始精度，因此要求字符串。"""
    if value is None:
        if not allow_null or field.required:
            raise _invalid(field, "必填字段不可缺失或为NULL")
        return None
    try:
        if field.type == "string":
            if not isinstance(value, str) or len(value) > field.max_length:
                raise ValueError("必须为长度范围内字符串")
            if any(ord(char) < 32 or 0xD800 <= ord(char) <= 0xDFFF for char in value):
                raise ValueError("字符串不可包含控制字符或非法Unicode")
            return value
        if field.type == "integer":
            if type(value) is int:
                integer = value
            elif isinstance(value, str) and len(value) <= 21 and INTEGER_PATTERN.fullmatch(value):
                integer = int(value)
            else:
                raise ValueError("整数必须为int或整数字符串")
            if not -(2**63) <= integer < 2**63:
                raise ValueError("整数超出BIGINT范围")
            return integer
        if field.type == "decimal":
            if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
                raise ValueError("金额使用十进制字符串，禁止浮点数")
            if isinstance(value, str) and (len(value) > 100 or not DECIMAL_PATTERN.fullmatch(value)):
                raise ValueError("十进制格式非法")
            number = Decimal(value)
            if not number.is_finite() or number.copy_abs() >= DECIMAL_LIMIT:
                raise ValueError("金额超出NUMERIC(38,6)范围")
            with localcontext() as context:
                context.prec = 80
                fixed = number.quantize(DECIMAL_QUANTUM)
            if fixed != number:
                raise ValueError("金额不得含超过6位的非零小数")
            # 固定刻度同时保证Decimal主键的1.0与1.00属于相同身份。
            return fixed.copy_abs() if fixed == 0 else fixed
        if field.type == "date":
            return strict_date(value)
        if field.type == "datetime":
            if isinstance(value, str) and DATETIME_PATTERN.fullmatch(value):
                timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
            elif isinstance(value, datetime):
                timestamp = value
            else:
                raise ValueError("日期时间必须为含时区的ISO格式")
            if timestamp.tzinfo is None or timestamp.utcoffset() is None:
                raise ValueError("日期时间必须包含时区")
            return timestamp.astimezone(timezone.utc)
        if field.type == "boolean":
            if type(value) is not bool:
                raise ValueError("布尔字段只接受true或false")
            return value
    except (ValueError, TypeError, OverflowError, InvalidOperation):
        # Python日期解析错误可能含原文，不直接对外返回其异常字符串。
        raise _invalid(field, "类型、格式、范围或精度不合法") from None
    raise _invalid(field, "不支持的字段类型")


def validate_record(definition: DatasetDefinition, row: dict, business_date: date) -> dict:
    definition = DatasetDefinition.model_validate(definition)
    if not isinstance(row, dict):
        raise ServiceError(422, "invalid_record", "每条记录必须为对象")
    fields = {field.name: field for field in definition.fields}
    if any(name not in fields for name in row):
        raise ServiceError(422, "unknown_record_field", "记录包含未配置的字段")
    result = {name: validate_value(field, row.get(name)) for name, field in fields.items()}
    if result[definition.date_field] != business_date:
        raise ServiceError(422, "record_date_mismatch", "记录业务日期与导入批次不一致")
    for name in definition.key_fields:
        if isinstance(result[name], str) and not result[name].strip():
            raise _invalid(fields[name], "主键字符串不可为空或全空白")
    return result
