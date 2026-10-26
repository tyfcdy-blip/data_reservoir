"""同一授权与数据库快照下，按本期分组匹配紧邻上一等长周期。"""

from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP, localcontext
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

import psycopg
from psycopg import sql

from data_service import query
from data_service.datasets import get_dataset
from data_service.errors import ServiceError
from data_service.key_policy import RowScope
from data_service.models import DatasetDefinition, QueryRequest


def previous_period(start_date: date, end_date: date) -> tuple[date, date]:
    """日期包含首尾；例如20至22日的上一期是17至19日。"""
    length = (end_date - start_date).days + 1
    return start_date - timedelta(days=length), start_date - timedelta(days=1)


def validate_request(connection: psycopg.Connection, slug: str, payload: QueryRequest) -> None:
    definition = DatasetDefinition.model_validate(get_dataset(connection, slug)["definition"])
    fields = {field.name: field for field in definition.fields}
    if not payload.aggregates or any(fields[name].type == "date" for name in payload.group_by):
        raise ServiceError(422, "COMPARISON_NOT_SUPPORTED", "环比仅支持不按日期分组的汇总接口")


def _numeric_aliases(definition: DatasetDefinition, payload: QueryRequest) -> list[str]:
    fields = {field.name: field for field in definition.fields}
    # min/max可以输出字符串；仅数值汇总和公式可计算环比，不强行转为金额。
    return [aggregate.alias for aggregate in payload.aggregates
            if aggregate.op in ("count", "count_distinct") or
            (aggregate.field and fields[aggregate.field].type in ("integer", "decimal"))] + [
                formula.alias for formula in payload.formulas]


def metric_comparison(current: Any, previous: Any, *, period_status: str,
                      previous_exists: bool) -> dict:
    result = {"current": current, "previous": previous, "delta": None,
              "relative_change": None, "status": period_status}
    if period_status != "ok":
        return result
    if current is None:
        result["status"] = "current_missing"
        return result
    if not previous_exists or previous is None:
        result["status"] = "previous_missing"
        return result
    # PostgreSQL numeric及count整数通过文本转Decimal，禁止金额先经二进制浮点。
    current_number, previous_number = Decimal(str(current)), Decimal(str(previous))
    with localcontext() as context:
        context.prec = max(80, len(current_number.as_tuple().digits) +
                           len(previous_number.as_tuple().digits) + 16)
        delta = current_number - previous_number
        result["delta"] = format(delta, "f")
        if previous_number == 0:
            result["status"] = "previous_zero"
        else:
            relative = (delta / abs(previous_number)).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
            result["relative_change"] = format(relative, "f")
    return result


def _matched_previous_rows(connection: psycopg.Connection, definition: DatasetDefinition, dataset: dict,
                           payload: QueryRequest, current_rows: list[dict], *,
                           row_scope: RowScope | None) -> dict[tuple, dict]:
    if not current_rows:
        return {}
    statement, params, _, _ = query._build_query(definition, dataset["table_name"], payload,
                                                row_scope=row_scope)
    # 只返回本页分组的上期数值；不先截取上期TOP，也不逐分组发送查询。
    if payload.group_by:
        groups = []
        for row in current_rows:
            groups.append(sql.SQL("({})").format(sql.SQL(" AND ").join(
                sql.SQL("{} IS NOT DISTINCT FROM %s").format(sql.Identifier("previous", field))
                for field in payload.group_by)))
            params.extend(row[field] for field in payload.group_by)
        statement = sql.SQL("SELECT previous.* FROM ({}) AS previous WHERE {}").format(
            statement, sql.SQL(" OR ").join(groups))
    result: dict[tuple, dict] = {}
    used_bytes = 0
    with connection.cursor(name="comparison_" + uuid4().hex) as cursor:
        cursor.execute(statement, params)
        while (record := cursor.fetchone()) is not None:
            serialized = {name: query.serialize_value(value) for name, value in record.items()}
            used_bytes += query._json_size(serialized)
            if used_bytes > query.MAX_QUERY_BYTES:
                raise query._size_error()
            key = tuple(serialized[field] for field in payload.group_by)
            result[key] = serialized
    return result


def attach_comparison(connection: psycopg.Connection, slug: str, payload: QueryRequest,
                      current: dict, *, row_scope: RowScope | None = None) -> None:
    """在调用方现有REPEATABLE READ只读事务中附加比较，不另开连接或扩大授权。"""
    dataset = get_dataset(connection, slug)
    definition = DatasetDefinition.model_validate(dataset["definition"])
    first, last = previous_period(payload.start_date, payload.end_date)
    earliest = datetime.now(ZoneInfo("Asia/Shanghai")).date() - timedelta(days=definition.retention_days - 1)
    comparison = {"previous_period": {"start_date": first.isoformat(), "end_date": last.isoformat()},
                  "coverage": {"complete": False, "missing_dates": []},
                  "published_versions": [], "status": "out_of_retention", "rows": []}
    previous_rows: dict[tuple, dict] = {}
    if first >= earliest:
        comparison.update(query.published_coverage(connection, dataset, definition, first, last,
                                                   row_scope=row_scope))
        comparison["status"] = "ok" if current["coverage"]["complete"] and comparison["coverage"]["complete"] else "incomplete"
        previous_request = payload.model_copy(update={"start_date": first, "end_date": last,
                                                       "offset": 0, "require_complete": False})
        previous_rows = _matched_previous_rows(connection, definition, dataset, previous_request,
                                               current["rows"], row_scope=row_scope)
    current["comparison"] = comparison
    used_bytes = query.ENVELOPE_BYTES + query._json_size(current)
    if used_bytes > query.MAX_QUERY_BYTES:
        raise query._size_error()
    aliases = _numeric_aliases(definition, payload)
    for row in current["rows"]:
        key = tuple(row[field] for field in payload.group_by)
        previous = previous_rows.get(key)
        metrics = {alias: metric_comparison(row[alias], previous.get(alias) if previous else None,
                                           period_status=comparison["status"], previous_exists=previous is not None)
                   for alias in aliases}
        compared = {"group": {field: row[field] for field in payload.group_by}, "metrics": metrics}
        used_bytes += query._json_size(compared) + int(bool(comparison["rows"]))
        if used_bytes > query.MAX_QUERY_BYTES:
            raise query._size_error()
        comparison["rows"].append(compared)
