"""分批暂存与按数据集/日期原子发布，查询永远只读取发布版本。"""

from datetime import date, datetime, timedelta
from decimal import Decimal
import hashlib
import json
from typing import Any, Literal
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import psycopg
from psycopg import sql

from data_service.config import Settings
from data_service.datasets import get_dataset, validate_record
from data_service.db import connect
from data_service.errors import ServiceError
from data_service.models import DatasetDefinition

BEIJING = ZoneInfo("Asia/Shanghai")
MAX_BATCH_ROWS = 1000000


def _date_window(definition: DatasetDefinition, business_date: date, *, pull: bool = False,
                 today: date | None = None) -> None:
    current = today or datetime.now(BEIJING).date()
    if type(business_date) is not date or not current - timedelta(days=definition.retention_days - 1) <= business_date <= current:
        raise ServiceError(422, "DATE_OUT_OF_RETENTION", "业务日期必须在数据集保留窗口内")
    if pull and business_date == current:
        raise ServiceError(422, "DATE_NOT_FINISHED", "源拉取仅支持昨日及以前已结束自然日")


def _id(value: UUID | str) -> UUID:
    try:
        return value if isinstance(value, UUID) else UUID(value)
    except (ValueError, TypeError, AttributeError):
        raise ServiceError(422, "INVALID_BATCH_ID", "批次ID格式无效") from None


def _check_owner(batch: dict, owner_key_id: UUID | None) -> None:
    if owner_key_id is not None and batch["owner_key_id"] != _id(owner_key_id):
        raise ServiceError(403, "BATCH_FORBIDDEN", "不允许操作其他调用方创建的批次")


def _fetch_batch(connection: psycopg.Connection, batch_id: UUID | str, *, lock: bool = False) -> dict:
    statement = "SELECT * FROM batches WHERE id=%s" + (" FOR UPDATE" if lock else "")
    batch = connection.execute(statement, (_id(batch_id),)).fetchone()
    if not batch:
        raise ServiceError(404, "BATCH_NOT_FOUND", "批次不存在")
    return batch


def get_batch(settings: Settings, batch_id: UUID | str) -> dict:
    with connect(settings) as connection:
        return _fetch_batch(connection, batch_id)


def _create(connection: psycopg.Connection, slug: str, business_date: date, expected_rows: int | None,
            idempotency_key: str | None, owner_key_id: UUID | None, mode: str, *,
            write_mode: Literal["replace_day", "upsert"] = "replace_day") -> dict:
    dataset = get_dataset(connection, slug)
    definition = DatasetDefinition.model_validate(dataset["definition"])
    _date_window(definition, business_date, pull=mode == "pull")
    if write_mode not in ("replace_day", "upsert"):
        raise ServiceError(422, "INVALID_WRITE_MODE", "写入方式必须为整日替换或按主键更新")
    if mode not in {"push", "pull"}:
        raise ServiceError(422, "INVALID_BATCH_MODE", "批次模式无效")
    if expected_rows is not None and (type(expected_rows) is not int or not 0 <= expected_rows <= MAX_BATCH_ROWS):
        raise ServiceError(422, "INVALID_EXPECTED_ROWS", "期望行数必须是0至1000000的整数")
    if idempotency_key is not None:
        if (not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 120
                or idempotency_key != idempotency_key.strip() or any(ord(c) < 32 or ord(c) == 127 for c in idempotency_key)):
            raise ServiceError(422, "INVALID_IDEMPOTENCY_KEY", "幂等键格式无效")
        connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", ("batch-id:" + slug + ":" + idempotency_key,))
        existing = connection.execute("SELECT * FROM batches WHERE dataset_slug=%s AND idempotency_key=%s",
                                      (slug, idempotency_key)).fetchone()
        if existing:
            _check_owner(existing, owner_key_id)
            if (existing["business_date"] != business_date or existing["expected_rows"] != expected_rows
                    or existing["mode"] != mode or existing["owner_key_id"] != owner_key_id
                    or existing["write_mode"] != write_mode):
                raise ServiceError(409, "IDEMPOTENCY_CONFLICT", "该幂等键已用于不同批次参数")
            return existing
    connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", ("batch-day:" + slug + ":" + business_date.isoformat(),))
    active = connection.execute("SELECT id FROM batches WHERE dataset_slug=%s AND business_date=%s "
                                "AND status IN ('pending','running','uploading')", (slug, business_date)).fetchone()
    if active:
        raise ServiceError(409, "BATCH_ALREADY_ACTIVE", "该数据集日期已有进行中的批次")
    return connection.execute(
        "INSERT INTO batches(id,dataset_slug,business_date,mode,status,owner_key_id,expected_rows,idempotency_key,write_mode) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
        (uuid4(), slug, business_date, mode, "pending" if mode == "pull" else "uploading",
         owner_key_id, expected_rows, idempotency_key, write_mode),
    ).fetchone()


def create_batch(settings: Settings, slug: str, business_date: date, expected_rows: int | None,
                 idempotency_key: str | None, owner_key_id: UUID | None = None, mode: str = "push", *,
                 write_mode: Literal["replace_day", "upsert"] = "replace_day") -> dict:
    with connect(settings) as connection:
        return _create(connection, slug, business_date, expected_rows, idempotency_key, owner_key_id, mode,
                       write_mode=write_mode)


def _json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return "0.000000" if value == 0 else str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    raise TypeError("unsupported canonical value")


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, default=_json_value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def append_chunk(settings: Settings, batch_id: UUID | str, chunk_index: int, rows: list[dict],
                 owner_key_id: UUID | None = None) -> dict:
    if type(chunk_index) is not int or not 0 <= chunk_index < 1000000:
        raise ServiceError(422, "INVALID_CHUNK_INDEX", "分片序号须为0至999999的整数")
    if not isinstance(rows, list) or len(rows) > 1000:
        raise ServiceError(422, "INVALID_CHUNK_ROWS", "单次分片最多1000条记录")
    try:
        with connect(settings) as connection:
            batch = _fetch_batch(connection, batch_id, lock=True)
            _check_owner(batch, owner_key_id)
            if batch["status"] not in {"uploading", "running"}:
                raise ServiceError(409, "BATCH_NOT_WRITABLE", "当前批次状态不可写入")
            dataset = get_dataset(connection, batch["dataset_slug"])
            definition = DatasetDefinition.model_validate(dataset["definition"])
            _date_window(definition, batch["business_date"], pull=batch["mode"] == "pull")
            records = [validate_record(definition, row, batch["business_date"]) for row in rows]
            digest = _digest(records)
            prior = connection.execute("SELECT digest FROM batch_chunks WHERE batch_id=%s AND chunk_index=%s",
                                       (batch["id"], chunk_index)).fetchone()
            if prior:
                if prior["digest"] != digest:
                    raise ServiceError(409, "CHUNK_CONFLICT", "分片序号已存在但内容不同")
                return batch
            total = batch["row_count"] + len(records)
            if total > MAX_BATCH_ROWS or (batch["expected_rows"] is not None and total > batch["expected_rows"]):
                raise ServiceError(422, "BATCH_ROW_LIMIT", "累计行数超过批次上限或期望行数")
            fields = [field.name for field in definition.fields]
            columns = ["_batch_id", "_date", "_key", *fields]
            insert = sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
                sql.Identifier(dataset["table_name"]), sql.SQL(",").join(map(sql.Identifier, columns)),
                sql.SQL(",").join(sql.Placeholder() for _ in columns))
            if records:
                with connection.cursor() as cursor:
                    cursor.executemany(insert, [(batch["id"], batch["business_date"],
                                                 _digest([record[key] for key in definition.key_fields]),
                                                 *(record[field] for field in fields)) for record in records])
            connection.execute("INSERT INTO batch_chunks(batch_id,chunk_index,digest,row_count) VALUES (%s,%s,%s,%s)",
                               (batch["id"], chunk_index, digest, len(records)))
            return connection.execute("UPDATE batches SET row_count=%s,page_count=page_count+1,updated_at=clock_timestamp() "
                                      "WHERE id=%s RETURNING *", (total, batch["id"])).fetchone()
    except psycopg.errors.UniqueViolation:
        # 当前分片整体回滚，既有分片不变，调用方可修正后重试，重复键从不发布。
        raise ServiceError(409, "DUPLICATE_RECORD", "本分片或既有分片存在重复业务主键") from None


def _retain_missing_rows(connection: psycopg.Connection, dataset: dict, definition: DatasetDefinition,
                         batch: dict, previous_batch_id: UUID) -> int:
    """只把旧快照未在本次出现的键复制到暂存版本；新行（包括NULL）始终优先。

    复制、指针切换和旧版本删除由发布事务一起提交，读者不会见到部分合并。
    额外最多复制剩余额度加一行，超限即回滚，避免无界膨胀或截断发布。
    """
    retained_columns = ["_date", "_key", *(field.name for field in definition.fields)]
    statement = sql.SQL(
        "INSERT INTO {} ({}) SELECT %s,{} FROM {} old "
        "WHERE old._batch_id=%s AND NOT EXISTS "
        "(SELECT 1 FROM {} incoming WHERE incoming._batch_id=%s AND incoming._key=old._key) LIMIT %s"
    ).format(
        sql.Identifier(dataset["table_name"]),
        sql.SQL(",").join(map(sql.Identifier, ["_batch_id", *retained_columns])),
        sql.SQL(",").join(sql.Identifier("old", name) for name in retained_columns),
        sql.Identifier(dataset["table_name"]), sql.Identifier(dataset["table_name"]),
    )
    retained = connection.execute(statement, (batch["id"], previous_batch_id, batch["id"],
                                              MAX_BATCH_ROWS - batch["row_count"] + 1)).rowcount
    published_count = batch["row_count"] + retained
    if published_count > MAX_BATCH_ROWS:
        raise ServiceError(422, "PUBLISHED_ROW_LIMIT", "合并后的业务日记录数超过1000000条，旧发布数据不变")
    return published_count


def commit_batch(settings: Settings, batch_id: UUID | str, owner_key_id: UUID | None = None) -> dict:
    with connect(settings) as connection:
        batch = _fetch_batch(connection, batch_id, lock=True)
        _check_owner(batch, owner_key_id)
        if batch["status"] == "succeeded":
            return batch
        if batch["status"] not in {"uploading", "running"}:
            raise ServiceError(409, "BATCH_NOT_COMMITTABLE", "当前批次不能发布")
        dataset = get_dataset(connection, batch["dataset_slug"])
        definition = DatasetDefinition.model_validate(dataset["definition"])
        _date_window(definition, batch["business_date"], pull=batch["mode"] == "pull")
        if batch["expected_rows"] is not None and batch["expected_rows"] != batch["row_count"]:
            raise ServiceError(409, "ROW_COUNT_MISMATCH", "已接收行数与期望行数不一致")
        chunks = connection.execute("SELECT COUNT(*) AS n,MIN(chunk_index) AS first,MAX(chunk_index) AS last "
                                    "FROM batch_chunks WHERE batch_id=%s", (batch["id"],)).fetchone()
        if chunks["n"] and (chunks["first"] != 0 or chunks["last"] != chunks["n"] - 1):
            raise ServiceError(409, "CHUNKS_INCOMPLETE", "分片序号须从0开始且连续")
        previous = connection.execute("SELECT batch_id FROM published_days WHERE dataset_slug=%s AND business_date=%s FOR UPDATE",
                                      (batch["dataset_slug"], batch["business_date"])).fetchone()
        published_count = batch["row_count"]
        if previous and batch["write_mode"] == "upsert":
            published_count = _retain_missing_rows(connection, dataset, definition, batch, previous["batch_id"])
        connection.execute("INSERT INTO published_days(dataset_slug,business_date,batch_id,row_count,published_at) "
                           "VALUES (%s,%s,%s,%s,clock_timestamp()) ON CONFLICT(dataset_slug,business_date) DO UPDATE "
                           "SET batch_id=EXCLUDED.batch_id,row_count=EXCLUDED.row_count,published_at=EXCLUDED.published_at",
                           (batch["dataset_slug"], batch["business_date"], batch["id"], published_count))
        result = connection.execute("UPDATE batches SET status='succeeded',finished_at=clock_timestamp(),"
                                    "updated_at=clock_timestamp(),published_row_count=%s WHERE id=%s RETURNING *",
                                    (published_count, batch["id"])).fetchone()
        if previous:
            connection.execute(sql.SQL("DELETE FROM {} WHERE _batch_id=%s").format(sql.Identifier(dataset["table_name"])),
                               (previous["batch_id"],))
        return result


def _clear_unpublished(connection: psycopg.Connection, batch: dict) -> None:
    dataset = get_dataset(connection, batch["dataset_slug"])
    connection.execute(sql.SQL("DELETE FROM {} WHERE _batch_id=%s AND NOT EXISTS "
                               "(SELECT 1 FROM published_days WHERE batch_id=%s)").format(sql.Identifier(dataset["table_name"])),
                       (batch["id"], batch["id"]))
    connection.execute("DELETE FROM batch_chunks WHERE batch_id=%s AND NOT EXISTS "
                       "(SELECT 1 FROM published_days WHERE batch_id=%s)", (batch["id"], batch["id"]))


def cancel_batch(settings: Settings, batch_id: UUID | str, owner_key_id: UUID | None = None) -> dict:
    with connect(settings) as connection:
        batch = _fetch_batch(connection, batch_id, lock=True)
        _check_owner(batch, owner_key_id)
        if batch["status"] == "cancelled":
            return batch
        if batch["status"] not in {"pending", "uploading"}:
            raise ServiceError(409, "BATCH_NOT_CANCELLABLE", "当前批次不可取消")
        _clear_unpublished(connection, batch)
        return connection.execute("UPDATE batches SET status='cancelled',updated_at=clock_timestamp(),"
                                  "finished_at=clock_timestamp() WHERE id=%s RETURNING *", (batch["id"],)).fetchone()


def enqueue_sync(settings: Settings, slug: str, start_date: date, end_date: date) -> list[dict]:
    from data_service.source_registry import load_active_sources

    sources = load_active_sources(settings)
    if slug not in sources:
        raise ServiceError(422, "SOURCE_NOT_CONFIGURED", "该数据集尚未配置拉取源")
    if type(start_date) is not date or type(end_date) is not date or not 0 <= (end_date - start_date).days < 180:
        raise ServiceError(422, "INVALID_DATE_RANGE", "同步范围须按日期升序且最多180天")
    write_mode = (sources[slug]._schedule or {}).get("write_mode", "replace_day")
    with connect(settings) as connection:
        # 同一范围入队全成或全败，写入方式入队冻结，后续改配置只影响新任务。
        return [_create(connection, slug, start_date + timedelta(days=offset), None, None, None, "pull",
                        write_mode=write_mode)
                for offset in range((end_date - start_date).days + 1)]
