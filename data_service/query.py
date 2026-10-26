"""只读发布版本的有界参数化查询；批量在同一数据库快照读取。"""

from datetime import date, datetime, timedelta
from decimal import Decimal
import json
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

import psycopg
from psycopg import sql

from data_service.config import Settings
from data_service.datasets import get_dataset, validate_value
from data_service.db import connect
from data_service.errors import ServiceError
from data_service.formulas import compile_formula, result_number
from data_service.models import BatchQueryItem, DatasetDefinition, QueryRequest
from data_service.key_policy import RowScope, compile_scope


COMPARISONS = {"eq": "=", "gt": ">", "gte": ">=", "lt": "<", "lte": "<="}
FUNCTIONS = {"sum": "SUM", "avg": "AVG", "min": "MIN", "max": "MAX", "count": "COUNT"}
MAX_QUERY_BYTES = 8 * 1024 * 1024
MAX_BATCH_BYTES = 16 * 1024 * 1024
JSON_ENCODER = json.JSONEncoder(ensure_ascii=False, allow_nan=False, separators=(",", ":"))
# 与HTTP成功响应保持同一种紧凑UTF-8编码，封套也必须占用响应预算。
ENVELOPE_BYTES = len('{"code":200,"data":}'.encode("utf-8"))


def _json_size(value: Any) -> int:
    """逐片计算，避免为了测量大小再复制整个结果的JSON字符串。"""
    return sum(len(fragment.encode("utf-8")) for fragment in JSON_ENCODER.iterencode(value))


def _size_error(batch: bool = False) -> ServiceError:
    if batch:
        return ServiceError(413, "batch_result_too_large", "批量查询结果超过16MiB，请减少子查询、字段或limit")
    return ServiceError(413, "query_result_too_large", "查询结果超过8MiB，请减少字段或limit")


class ResponseBudget:
    """批量共享的显式预算；获取下一行前不预取未受控的客户端结果。"""

    def __init__(self) -> None:
        self.used = ENVELOPE_BYTES + 2  # 数组的[]。

    def consume(self, size: int) -> None:
        if self.used + size > MAX_BATCH_BYTES:
            raise _size_error(batch=True)
        self.used += size


def serialize_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def _query_error(message: str) -> ServiceError:
    return ServiceError(422, "invalid_query", message)


def _build_query(definition: DatasetDefinition, table_name: str, payload: QueryRequest, *, row_scope: RowScope | None = None) -> tuple[sql.Composed, list, list[str], list[str]]:
    fields = {field.name: field for field in definition.fields}
    aggregate_mode = bool(payload.group_by or payload.aggregates)
    selected = payload.fields or list(fields)
    referenced = list(selected) if not aggregate_mode else payload.group_by + [aggregate.field for aggregate in payload.aggregates if aggregate.field]
    referenced += [predicate.field for predicate in payload.filters]
    if any(name not in fields for name in referenced):
        raise _query_error("查询包含不存在的字段")
    if any(not fields[name].dimension and fields[name].type != "date" for name in payload.group_by):
        raise _query_error("仅dimension字段或date字段可用于分组")
    columns = payload.group_by + [aggregate.alias for aggregate in payload.aggregates] + [formula.alias for formula in payload.formulas] if aggregate_mode else selected
    allowed_sort = set(columns) if aggregate_mode else set(fields)
    if any(order.field not in allowed_sort for order in payload.order_by):
        raise _query_error("排序字段必须是合法明细字段或聚合输出列")
    expressions = [sql.Identifier("records", name) for name in (payload.group_by if aggregate_mode else selected)]
    numeric_aliases: set[str] = set()
    for aggregate in payload.aggregates:
        field = fields.get(aggregate.field)
        if aggregate.op in ("sum", "avg") and field.type not in ("integer", "decimal"):
            raise _query_error("sum/avg只接受integer或decimal字段")
        if aggregate.op in ("min", "max") and field.type == "boolean":
            raise _query_error("boolean字段不支持min/max")
        if aggregate.op in ("count", "count_distinct") or (field is not None and field.type in ("integer", "decimal")):
            numeric_aliases.add(aggregate.alias)
        argument = sql.Identifier("records", aggregate.field) if aggregate.field else sql.SQL("*")
        if aggregate.op == "count_distinct":
            expression = sql.SQL("COUNT(DISTINCT {})").format(argument)
        else:
            expression = sql.SQL("{}({})").format(sql.SQL(FUNCTIONS[aggregate.op]), argument)
        expressions.append(sql.SQL("{} AS {}").format(expression, sql.Identifier(aggregate.alias)))
    predicates = [sql.SQL("published.dataset_slug=%s"), sql.SQL("records._date BETWEEN %s AND %s")]
    params = [definition.slug, payload.start_date, payload.end_date]
    if row_scope is not None:
        permission_clause, permission_values = compile_scope(row_scope, definition)
        predicates.append(permission_clause)
        params.extend(permission_values)
    for predicate in payload.filters:
        column = sql.Identifier("records", predicate.field)
        if predicate.op == "is_null":
            predicates.append(sql.SQL("{} IS {}NULL").format(column, sql.SQL("" if predicate.value else "NOT ")))
        elif predicate.op == "in":
            values = [validate_value(fields[predicate.field], value, allow_null=False) for value in predicate.value]
            predicates.append(sql.SQL("{} IN ({})").format(column, sql.SQL(",").join(sql.Placeholder() for _ in values)))
            params.extend(values)
        else:
            if fields[predicate.field].type == "boolean" and predicate.op != "eq":
                raise _query_error("boolean字段只支持eq/in/is_null")
            predicates.append(sql.SQL("{} {} %s").format(column, sql.SQL(COMPARISONS[predicate.op])))
            params.append(validate_value(fields[predicate.field], predicate.value, allow_null=False))
    statement = sql.SQL("SELECT {} FROM {} AS records JOIN published_days AS published ON published.batch_id=records._batch_id AND published.business_date=records._date WHERE {}").format(
        sql.SQL(",").join(expressions), sql.Identifier(table_name), sql.SQL(" AND ").join(predicates)
    )
    if payload.group_by:
        statement += sql.SQL(" GROUP BY {} ").format(sql.SQL(",").join(sql.Identifier("records", name) for name in payload.group_by))
    if payload.formulas or payload.result_filters:
        statement, params = _apply_result_rules(statement, params, numeric_aliases, payload)
        if len(params) > 16000 or len(statement.as_string().encode("utf-8")) > 1024 * 1024:
            raise _query_error("汇总查询展开超过资源限制，请减少公式或筛选数量")
    default_sort = payload.group_by if aggregate_mode else definition.key_fields
    return statement, params, columns, default_sort


def _apply_result_rules(statement: sql.Composed, params: list, numeric_aliases: set[str], payload: QueryRequest) -> tuple[sql.Composed, list]:
    """先聚合、再公式及舍入、再结果筛选；外层排序分页与COUNT共用同一结果。"""
    if payload.formulas:
        computed = [sql.SQL("aggregated.*")]
        formula_params: list = []
        for formula in payload.formulas:
            try:
                expression, constants = compile_formula(formula.expression, numeric_aliases, formula.decimal_places)
            except ValueError as error:
                raise _query_error(str(error)) from None
            computed.append(sql.SQL("ROUND({}, %s) AS {}").format(expression, sql.Identifier(formula.alias)))
            formula_params.extend(constants)
            formula_params.append(formula.decimal_places)
        statement = sql.SQL("SELECT {} FROM ({}) AS aggregated").format(sql.SQL(",").join(computed), statement)
        # SELECT中的公式常量出现在内层WHERE占位符之前，参数必须遵循文本顺序。
        params = formula_params + params
    if payload.result_filters:
        allowed = numeric_aliases | {formula.alias for formula in payload.formulas}
        predicates = []
        for predicate in payload.result_filters:
            if predicate.field not in allowed:
                raise _query_error("汇总后筛选只能引用数字型汇总指标或公式返回列")
            column = sql.Identifier("computed", predicate.field)
            if predicate.op == "is_null":
                predicates.append(sql.SQL("{} IS {}NULL").format(column, sql.SQL("" if predicate.value else "NOT ")))
                continue
            try:
                values = [result_number(value) for value in predicate.value] if predicate.op == "in" else [result_number(predicate.value)]
            except ValueError as error:
                raise _query_error(str(error)) from None
            if predicate.op == "in":
                predicates.append(sql.SQL("{} IN ({})").format(column, sql.SQL(",").join(sql.Placeholder() for _ in values)))
            else:
                predicates.append(sql.SQL("{} {} %s").format(column, sql.SQL(COMPARISONS[predicate.op])))
            params.extend(values)
        statement = sql.SQL("SELECT computed.* FROM ({}) AS computed WHERE {}").format(statement, sql.SQL(" AND ").join(predicates))
    return statement, params


def validate_query_definition(dataset: dict, payload: QueryRequest) -> None:
    """保存查询配置时复用执行器的字段、类型及排序校验，不访问业务记录。"""
    _build_query(DatasetDefinition.model_validate(dataset["definition"]), dataset["table_name"], payload)


def published_coverage(connection: psycopg.Connection, dataset: dict, definition: DatasetDefinition,
                       start_date: date, end_date: date, *, row_scope: RowScope | None = None) -> dict:
    """读取日期覆盖及权限范围内行数，供同快照的本期与上期共用。"""
    slug = definition.slug
    published = connection.execute(
        "SELECT business_date,batch_id,row_count FROM published_days WHERE dataset_slug=%s AND business_date BETWEEN %s AND %s ORDER BY business_date",
        (slug, start_date, end_date),
    ).fetchall()
    if row_scope is not None and row_scope.mode == "filtered":
        # 发布覆盖仍按业务日期报告，但行数只能反映授权范围，不能泄露其他店铺规模。
        permission_clause, permission_values = compile_scope(row_scope, definition)
        counts = connection.execute(sql.SQL(
            "SELECT records._date,COUNT(*) AS row_count FROM {} AS records "
            "JOIN published_days AS published ON published.batch_id=records._batch_id "
            "AND published.business_date=records._date WHERE published.dataset_slug=%s "
            "AND records._date BETWEEN %s AND %s AND {} GROUP BY records._date"
        ).format(sql.Identifier(dataset["table_name"]), permission_clause),
            [slug, start_date, end_date, *permission_values]).fetchall()
        scoped_counts = {record["_date"]: record["row_count"] for record in counts}
        for record in published:
            record["row_count"] = scoped_counts.get(record["business_date"], 0)
    dates = {record["business_date"] for record in published}
    missing = [
        (start_date + timedelta(days=offset)).isoformat()
        for offset in range((end_date - start_date).days + 1)
        if start_date + timedelta(days=offset) not in dates
    ]
    return {
        "coverage": {"complete": not missing, "missing_dates": missing},
        "published_versions": [{"date": record["business_date"].isoformat(),
                                "batch_id": str(record["batch_id"]), "row_count": record["row_count"]}
                               for record in published],
    }


def execute_query(connection: psycopg.Connection, slug: str, payload: QueryRequest, *, batch_budget: ResponseBudget | None = None, row_scope: RowScope | None = None) -> dict:
    payload = QueryRequest.model_validate(payload)
    dataset = get_dataset(connection, slug)
    definition = DatasetDefinition.model_validate(dataset["definition"])
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    earliest = today - timedelta(days=definition.retention_days - 1)
    if payload.end_date > today or payload.start_date < earliest:
        raise _query_error("查询日期超出数据集保留窗口或包含未来日期")
    statement, params, columns, default_sort = _build_query(definition, dataset["table_name"], payload, row_scope=row_scope)
    publication = published_coverage(connection, dataset, definition, payload.start_date, payload.end_date,
                                     row_scope=row_scope)
    missing = publication["coverage"]["missing_dates"]
    if missing and payload.require_complete:
        raise ServiceError(409, "incomplete_date_range", "查询范围存在尚未发布的日期", {"missing_dates": missing})
    total = connection.execute(sql.SQL("SELECT COUNT(*) AS total FROM ({}) AS counted").format(statement), params).fetchone()["total"]
    aggregate_mode = bool(payload.group_by or payload.aggregates)
    orders = [(order.field, order.direction) for order in payload.order_by]
    ordered = {name for name, _ in orders}
    orders.extend((name, "asc") for name in default_sort if name not in ordered)
    # 主键可能不含日期，跨日分页必须把业务日期加入最后的稳定排序。
    if not aggregate_mode and definition.date_field not in {name for name, _ in orders}:
        orders.append((definition.date_field, "asc"))
    if orders:
        statement += sql.SQL(" ORDER BY {} ").format(sql.SQL(",").join(
            sql.SQL("{} {} NULLS LAST").format(
                sql.Identifier(name) if aggregate_mode else sql.Identifier("records", name), sql.SQL(direction.upper())
            ) for name, direction in orders
        ))
    statement += sql.SQL(" LIMIT %s OFFSET %s")
    result = {
        "dataset": slug, "columns": columns,
        "rows": [],
        "total": total, "limit": payload.limit, "offset": payload.offset,
        **publication,
    }
    metadata_bytes = _json_size(result)
    query_bytes = ENVELOPE_BYTES + metadata_bytes
    if query_bytes > MAX_QUERY_BYTES:
        raise _size_error()
    if batch_budget is not None:
        batch_budget.consume(metadata_bytes)
    # 普通游标即便fetchone也会先缓存完整libpq结果，必须使用命名服务器游标。
    # fetchone每次只FETCH 1；最多保留已经通过预算的记录和当前一行。
    with connection.cursor(name="result_" + uuid4().hex) as cursor:
        cursor.execute(statement, params + [payload.limit, payload.offset])
        while (record := cursor.fetchone()) is not None:
            serialized = {name: serialize_value(value) for name, value in record.items()}
            row_bytes = _json_size(serialized) + int(bool(result["rows"]))
            if query_bytes + row_bytes > MAX_QUERY_BYTES:
                raise _size_error()
            if batch_budget is not None:
                batch_budget.consume(row_bytes)
            query_bytes += row_bytes
            result["rows"].append(serialized)
    return result


def run_query(settings: Settings, slug: str, payload: QueryRequest, *, row_scope: RowScope | None = None) -> dict:
    with connect(settings) as connection:
        connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        return execute_query(connection, slug, payload, row_scope=row_scope)


def run_batch(settings: Settings, requests: list[BatchQueryItem], *, row_scopes: dict[str, RowScope] | None = None) -> list[dict]:
    if not isinstance(requests, list) or not 1 <= len(requests) <= 10:
        raise _query_error("批量查询必须包含1到10个子查询")
    validated = [BatchQueryItem.model_validate(request) for request in requests]
    with connect(settings) as connection:
        connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        budget = ResponseBudget()
        results = []
        for request in validated:
            if results:
                budget.consume(1)  # 子查询结果之间的逗号。
            # 传入授权字典时不允许缺项降级为整表查询。
            if row_scopes is not None and request.dataset not in row_scopes:
                from data_service.key_policy import policy_error
                raise policy_error()
            results.append(execute_query(connection, request.dataset, request.query, batch_budget=budget,
                                         row_scope=row_scopes[request.dataset] if row_scopes is not None else None))
        return results
