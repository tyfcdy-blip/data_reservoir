"""独立单worker、按日拉取、故障恢复、调度与保留清理。"""

import argparse
from datetime import date, datetime, time, timedelta
import logging
import signal
import threading
from typing import Protocol, Any
from collections.abc import Callable, Iterator

import psycopg

from data_service.config import Settings, get_settings
from data_service.datasets import get_dataset
from data_service.db import connect
from data_service.errors import ServiceError
from data_service.ingest import (BEIJING, _clear_unpublished, _create, _date_window, append_chunk,
                                 commit_batch)
from data_service.models import DatasetDefinition
from data_service.source import SourceClient, SourceConfig, SourceError
from data_service.source_registry import load_active_sources

logger = logging.getLogger(__name__)
WORKER_LOCK = 706223002
WORKER_KEY = "sync"


class PageSource(Protocol):
    def iter_pages(self, business_date: date) -> Iterator[list[dict[str, Any]]]: ...


def acquire_worker_lock(connection: psycopg.Connection) -> bool:
    if not connection.autocommit:
        raise ValueError("worker连接必须显式使用autocommit")
    return bool(connection.execute("SELECT pg_try_advisory_lock(%s) AS locked", (WORKER_LOCK,)).fetchone()["locked"])


def require_worker_lock(connection: psycopg.Connection) -> None:
    locked = connection.execute("SELECT EXISTS(SELECT 1 FROM pg_locks WHERE locktype='advisory' "
                                "AND pid=pg_backend_pid() AND classid=0 AND objid=%s AND objsubid=1 AND granted) AS locked",
                                (WORKER_LOCK,)).fetchone()["locked"]
    if not connection.autocommit or not locked:
        raise RuntimeError("worker操作要求同连接持有独占锁")


def heartbeat(connection: psycopg.Connection, *, status: str = "idle") -> None:
    require_worker_lock(connection)
    connection.execute("INSERT INTO worker_heartbeat(worker_key,heartbeat_at,status) VALUES (%s,clock_timestamp(),%s) "
                       "ON CONFLICT(worker_key) DO UPDATE SET heartbeat_at=EXCLUDED.heartbeat_at,status=EXCLUDED.status",
                       (WORKER_KEY, status))


def recover_interrupted_jobs(connection: psycopg.Connection) -> int:
    """只有取得全局锁的新worker能够宣布上次进程中断，不能凭超时抢任务。"""
    require_worker_lock(connection)
    with connection.transaction():
        batches = connection.execute("SELECT * FROM batches WHERE status='running' FOR UPDATE").fetchall()
        for batch in batches:
            _clear_unpublished(connection, batch)
            connection.execute("UPDATE batches SET status='failed',finished_at=clock_timestamp(),updated_at=clock_timestamp(),"
                               "error_code='WORKER_INTERRUPTED',error_message='上次同步中断，请重新入队，旧发布数据不变' "
                               "WHERE id=%s", (batch["id"],))
    return len(batches)


def claim_next_job(connection: psycopg.Connection) -> dict | None:
    require_worker_lock(connection)
    with connection.transaction():
        batch = connection.execute("SELECT * FROM batches WHERE status='pending' AND mode='pull' "
                                   "ORDER BY created_at,business_date,id FOR UPDATE SKIP LOCKED LIMIT 1").fetchone()
        if not batch:
            return None
        return connection.execute("UPDATE batches SET status='running',started_at=clock_timestamp(),updated_at=clock_timestamp() "
                                  "WHERE id=%s RETURNING *", (batch["id"],)).fetchone()


def _fail(connection: psycopg.Connection, batch: dict, code: str, message: str) -> None:
    require_worker_lock(connection)
    with connection.transaction():
        current = connection.execute("SELECT * FROM batches WHERE id=%s FOR UPDATE", (batch["id"],)).fetchone()
        if current and current["status"] == "running":
            _clear_unpublished(connection, current)
            connection.execute("UPDATE batches SET status='failed',finished_at=clock_timestamp(),updated_at=clock_timestamp(),"
                               "error_code=%s,error_message=%s WHERE id=%s", (code, message, batch["id"]))
    logger.warning("sync_failed batch_id=%s error_code=%s", batch["id"], code)


def process_job(connection: psycopg.Connection, settings: Settings, batch: dict, source: PageSource,
                *, today: date | None = None) -> bool:
    require_worker_lock(connection)
    try:
        dataset = get_dataset(connection, batch["dataset_slug"])
        definition = DatasetDefinition.model_validate(dataset["definition"])
        _date_window(definition, batch["business_date"], pull=True, today=today)
        if isinstance(source, SourceClient) and source.config.field_mapping:
            if set(source.config.field_mapping) != {field.name for field in definition.fields}:
                raise SourceError("FIELD_MAPPING_INCOMPLETE", "字段映射必须覆盖数据集全部字段且不得包含额外目标字段")
        if isinstance(source, SourceClient):
            date_fields = {field.name for field in definition.fields if field.type == "date"}
            if set(source.config.date_fields_from_datetime) - date_fields:
                raise SourceError("DATE_CONVERSION_INVALID", "日期时间转换只能指向数据集中date类型字段")
            text_fields = {field.name for field in definition.fields if field.type == "string"}
            if set(source.config.text_whitespace_fields) - text_fields:
                raise SourceError("TEXT_CONVERSION_INVALID", "文本空白转换只能指向数据集中string类型字段")
        ended = False
        for index, rows in enumerate(source.iter_pages(batch["business_date"])):
            heartbeat(connection, status="syncing")
            append_chunk(settings, batch["id"], index, rows)
            if not rows:
                ended = True
                break
        if not ended:
            raise SourceError("PAGINATION_INCOMPLETE", "未观察到分页结束空页，整日不发布")
        # 发布前再次确认锁连接仍存活，失锁worker不得恢复新连接后继续原任务。
        heartbeat(connection, status="syncing")
        commit_batch(settings, batch["id"])
    except (SourceError, ServiceError) as error:
        _fail(connection, batch, error.code, error.message)
        return False
    except (psycopg.OperationalError, psycopg.InterfaceError):
        raise
    except psycopg.Error:
        _fail(connection, batch, "DATABASE_WRITE_ERROR", "数据库写入失败，旧发布数据不变")
        return False
    except Exception as error:
        _fail(connection, batch, "INTERNAL_SYNC_ERROR", "同步内部异常，旧发布数据不变")
        logger.error("sync_internal_error batch_id=%s exception_type=%s", batch["id"], type(error).__name__)
        return False
    logger.info("sync_succeeded batch_id=%s dataset=%s", batch["id"], batch["dataset_slug"])
    return True


def cleanup_history(connection: psycopg.Connection, settings: Settings, *, today: date | None = None) -> None:
    require_worker_lock(connection)
    current = today or datetime.now(BEIJING).date()
    # 清理在数据集粒度事务完成，批次行锁与写入/发布互斥；不删除其他数据集的记录。
    datasets = connection.execute("SELECT slug,definition FROM datasets ORDER BY slug").fetchall()
    for dataset in datasets:
        cutoff = current - timedelta(days=dataset["definition"]["retention_days"] - 1)
        with connection.transaction():
            expired = connection.execute("SELECT * FROM batches WHERE dataset_slug=%s AND "
                                         "((status='uploading' AND updated_at<clock_timestamp()-interval '24 hours') "
                                         "OR (business_date<%s AND status IN ('pending','uploading'))) "
                                         "FOR UPDATE", (dataset["slug"], cutoff)).fetchall()
            for batch in expired:
                _clear_unpublished(connection, batch)
                connection.execute("UPDATE batches SET status='failed',updated_at=clock_timestamp(),finished_at=clock_timestamp(),"
                                   "error_code='BATCH_EXPIRED',error_message='未完成批次已超时或超出保留窗口' WHERE id=%s",
                                   (batch["id"],))
            connection.execute("DELETE FROM published_days WHERE dataset_slug=%s AND business_date<%s", (dataset["slug"], cutoff))
            connection.execute("DELETE FROM batches WHERE dataset_slug=%s AND business_date<%s "
                               "AND status IN ('succeeded','failed','cancelled') "
                               "AND NOT EXISTS(SELECT 1 FROM published_days p WHERE p.batch_id=batches.id)",
                               (dataset["slug"], cutoff))


def enqueue_schedule(connection: psycopg.Connection, settings: Settings, sources: dict[str, SourceConfig],
                     now: datetime) -> int:
    require_worker_lock(connection)
    local_now = now.astimezone(BEIJING)
    queued = 0
    for slug in sorted(sources):
        schedule = sources[slug]._schedule or {"schedule_enabled": settings.schedule_enabled,
                                               "schedule_times": [f"{settings.schedule_hour:02d}:00"],
                                               "write_mode": "replace_day", "refresh_days": settings.refresh_days}
        if not schedule["schedule_enabled"]:
            continue
        schedule_times = schedule["schedule_times"]
        due_times = [value for value in schedule_times
                     if local_now >= datetime.combine(local_now.date(), time.fromisoformat(value), BEIJING)]
        if not due_times:
            continue
        try:
            dataset = get_dataset(connection, slug)
        except ServiceError:
            logger.warning("schedule_dataset_missing dataset=%s", slug)
            continue
        retention = dataset["definition"]["retention_days"]
        for execution_time in due_times:
            for offset in range(1, min(schedule["refresh_days"], retention - 1) + 1):
                business_date = local_now.date() - timedelta(days=offset)
                legacy_key = f"schedule:{local_now.date().isoformat()}:{business_date.isoformat()}"
                key = f"schedule:{local_now.date().isoformat()}:{execution_time.replace(':', '')}:{business_date.isoformat()}"
                try:
                    with connection.transaction():
                        # 升级前的单时间任务视作当天首个时点；失败任务也不会被每分钟重复创建。
                        keys = [key, legacy_key] if execution_time == schedule_times[0] else [key]
                        before = connection.execute(
                            "SELECT id FROM batches WHERE dataset_slug=%s AND idempotency_key=ANY(%s)",
                            (slug, keys),
                        ).fetchone()
                        if before is not None:
                            continue
                        _create(connection, slug, business_date, None, key, None, "pull",
                                write_mode=schedule["write_mode"])
                        queued += 1
                except ServiceError as error:
                    # 同一日期的前次任务完成后，下轮检查再创建后续时点，不覆盖正在执行的批次。
                    if error.code != "BATCH_ALREADY_ACTIVE":
                        raise
    return queued


def configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    for library in ("httpx", "httpcore"):
        logging.getLogger(library).setLevel(logging.WARNING)


def refresh_source_clients(settings: Settings, clients: dict[str, SourceClient], on_heartbeat: Callable[[], None], *,
                           client_factory: Callable[..., SourceClient] = SourceClient) -> dict[str, SourceConfig]:
    """只在任务间刷新；未变来源保留限速状态，变更和禁用关闭旧连接。"""
    sources = load_active_sources(settings)
    replacements: dict[str, SourceClient] = {}
    try:
        for slug, config in sources.items():
            if slug not in clients or clients[slug].config != config:
                replacements[slug] = client_factory(config, settings, heartbeat=on_heartbeat)
    except Exception:
        for client in replacements.values():
            client.close()
        raise
    for slug in list(clients):
        if slug not in sources or slug in replacements:
            clients.pop(slug).close()
    clients.update(replacements)
    return sources


def run_worker(settings: Settings, *, once: bool = False, stop: threading.Event | None = None) -> int:
    stopping = stop or threading.Event()
    with connect(settings) as connection:
        connection.autocommit = True
        if not acquire_worker_lock(connection):
            logger.error("worker_lock_unavailable")
            return 2
        logger.info("worker_started recovered=%s", recover_interrupted_jobs(connection))
        source_clients: dict[str, SourceClient] = {}
        cleanup_at: datetime | None = None
        schedule_at: datetime | None = None
        try:
            while not stopping.is_set():
                sources = refresh_source_clients(settings, source_clients, lambda: heartbeat(connection, status="syncing"))
                now = datetime.now(BEIJING)
                if cleanup_at is None or (now - cleanup_at).total_seconds() >= 300:
                    cleanup_history(connection, settings, today=now.date())
                    cleanup_at = now
                # 每分钟重新检查允许当天新启用来源立即获得调度；批次幂等键阻止重复发布。
                if schedule_at is None or (now - schedule_at).total_seconds() >= 60:
                    enqueue_schedule(connection, settings, sources, now)
                    schedule_at = now
                heartbeat(connection)
                batch = claim_next_job(connection)
                if batch:
                    source = source_clients.get(batch["dataset_slug"])
                    if source:
                        process_job(connection, settings, batch, source)
                    else:
                        _fail(connection, batch, "SOURCE_NOT_CONFIGURED", "该数据集源配置已禁用、移除或未配置")
                if once:
                    break
                if not batch:
                    stopping.wait(3)
            heartbeat(connection, status="stopped")
        finally:
            for client in source_clients.values():
                client.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="独立数据服务同步worker")
    parser.add_argument("--once", action="store_true")
    arguments = parser.parse_args()
    configure_logging()
    stopping = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda signum, frame: stopping.set())
    try:
        return run_worker(get_settings(), once=arguments.once, stop=stopping)
    except (psycopg.Error, ValueError, ServiceError):
        logger.error("worker_start_or_connection_failed")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
