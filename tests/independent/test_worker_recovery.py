"""真实worker进程中断、锁和按数据集保留策略的独立验证。"""

from datetime import timedelta
import json
import os
from pathlib import Path
import subprocess
import sys
from threading import Event

from psycopg import sql
import pytest

from data_service.datasets import register_dataset
from data_service.db import connect
from data_service.ingest import append_chunk, commit_batch, create_batch, get_batch
from data_service.models import DatasetDefinition, QueryRequest
from data_service.query import run_query
from data_service.worker import acquire_worker_lock, cleanup_history, recover_interrupted_jobs
from test_source_boundary import local_source


def publish_one(settings, definition, day, amount):
    batch = create_batch(settings, definition["slug"], day, 1, None)
    append_chunk(settings, batch["id"], 0, [{"dt": str(day), "product_id": "001", "amount": amount}])
    return commit_batch(settings, batch["id"])


def test_worker_requires_live_exclusive_lock(independent_settings):
    with connect(independent_settings) as first, connect(independent_settings) as second:
        first.autocommit = True
        second.autocommit = True
        with pytest.raises(RuntimeError):
            recover_interrupted_jobs(first)
        assert acquire_worker_lock(first) is True
        assert acquire_worker_lock(second) is False


def test_retention_is_dataset_specific_and_stale_upload_cleared(independent_settings, dataset_definitions, day):
    short = dict(dataset_definitions[0], retention_days=2)
    long = dict(dataset_definitions[0], slug=dataset_definitions[0]["slug"] + "_long", retention_days=180)
    short_registered = register_dataset(independent_settings, DatasetDefinition.model_validate(short))
    long_registered = register_dataset(independent_settings, DatasetDefinition.model_validate(long))
    publish_one(independent_settings, short_registered, day, "7")
    kept = publish_one(independent_settings, long_registered, day, "9")
    stale = create_batch(independent_settings, long["slug"], day, 1, None)
    append_chunk(independent_settings, stale["id"], 0, [{"dt": str(day), "product_id": "new", "amount": "11"}])
    with connect(independent_settings) as connection:
        connection.execute("UPDATE batches SET updated_at=clock_timestamp()-interval '25 hours' WHERE id=%s", (stale["id"],))
    with connect(independent_settings) as connection:
        connection.autocommit = True
        assert acquire_worker_lock(connection)
        cleanup_history(connection, independent_settings, today=day + timedelta(days=2))
        assert connection.execute("SELECT COUNT(*) AS n FROM published_days WHERE dataset_slug=%s", (short["slug"],)).fetchone()["n"] == 0
        assert connection.execute(sql.SQL("SELECT COUNT(*) AS n FROM {}").format(sql.Identifier(short_registered["table_name"]))).fetchone()["n"] == 0
        assert connection.execute("SELECT batch_id FROM published_days WHERE dataset_slug=%s", (long["slug"],)).fetchone()["batch_id"] == kept["id"]
        assert connection.execute(sql.SQL("SELECT COUNT(*) AS n FROM {} WHERE _batch_id=%s").format(sql.Identifier(long_registered["table_name"])), (stale["id"],)).fetchone()["n"] == 0
    assert get_batch(independent_settings, stale["id"])["status"] == "failed"


def test_real_worker_kill_restart_recovers_without_replacing_old(independent_settings, dataset_definitions, day, tmp_path):
    definition = register_dataset(independent_settings, DatasetDefinition.model_validate(dataset_definitions[0]))
    old = publish_one(independent_settings, definition, day, "5")
    queued = create_batch(independent_settings, definition["slug"], day, None, None, mode="pull")
    waiting = Event()
    release = Event()

    def upstream(payload, _count):
        if payload["pageIndex"] == 0:
            body = {"data": [{"dt": str(day), "product_id": "002", "shop": None, "amount": "100", "units": None}]}
        else:
            waiting.set()
            release.wait(timeout=20)
            body = {"data": []}
        return 200, {}, json.dumps(body).encode()

    process = None
    with local_source(upstream) as (url, _requests):
        path = tmp_path / "sources.json"
        path.write_text(json.dumps({"sources": [{"dataset_slug": definition["slug"], "url": url, "max_retries": 0, "requests_per_second": 20}]}), encoding="utf-8")
        environment = os.environ.copy()
        environment.update({"DS_DATABASE_URL": independent_settings.database_url,
                            "DS_SOURCE_CONFIG_PATH": str(path), "DS_ALLOWED_SOURCE_HOSTS": '["127.0.0.1"]',
                            "DS_SCHEDULE_ENABLED": "false", "PYTHONIOENCODING": "utf-8"})
        try:
            process = subprocess.Popen([sys.executable, "-m", "data_service.worker"], cwd=Path(__file__).resolve().parents[2],
                                       env=environment, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8")
            assert waiting.wait(timeout=15), "worker未到达第二页等待点"
            active = get_batch(independent_settings, queued["id"])
            assert active["status"] == "running" and active["row_count"] == 1
            visible = run_query(independent_settings, definition["slug"], QueryRequest(start_date=day, end_date=day))
            assert visible["rows"][0]["amount"] == "5.000000"
            process.kill()
            process.communicate(timeout=5)
            release.set()
            restarted = subprocess.run([sys.executable, "-m", "data_service.worker", "--once"], cwd=Path(__file__).resolve().parents[2],
                                       env=environment, capture_output=True, text=True, encoding="utf-8", timeout=20)
            assert restarted.returncode == 0, restarted.stdout + restarted.stderr
        finally:
            release.set()
            if process is not None and process.poll() is None:
                process.kill()
                process.communicate(timeout=5)
    recovered = get_batch(independent_settings, queued["id"])
    assert recovered["status"] == "failed" and recovered["error_code"] == "WORKER_INTERRUPTED"
    visible = run_query(independent_settings, definition["slug"], QueryRequest(start_date=day, end_date=day))
    assert visible["published_versions"][0]["batch_id"] == str(old["id"])
    with connect(independent_settings) as connection:
        assert connection.execute(sql.SQL("SELECT COUNT(*) AS n FROM {} WHERE _batch_id=%s").format(sql.Identifier(definition["table_name"])), (queued["id"],)).fetchone()["n"] == 0
    assert create_batch(independent_settings, definition["slug"], day, None, None, mode="pull")["status"] == "pending"
    # 新pending用于证明可重排后即取消，不留给其他进程测试消费。
    from data_service.ingest import cancel_batch
    with connect(independent_settings) as connection:
        pending = connection.execute("SELECT id FROM batches WHERE dataset_slug=%s AND status='pending'", (definition["slug"],)).fetchone()
    cancel_batch(independent_settings, pending["id"])
