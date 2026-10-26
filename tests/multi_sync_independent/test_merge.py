"""业务合并的独立验收：保留、NULL、失败、边界与并发可见性。"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import timedelta
from decimal import Decimal
import threading

from psycopg import sql
import pytest

from data_service import ingest
from data_service.db import connect
from data_service.errors import ServiceError
from data_service.ingest import commit_batch, create_batch, get_batch
from data_service.source import SourceError
from data_service.worker import claim_next_job, process_job
from tests.multi_sync_independent.helpers import SLUG, Pages, publish, record, rows, stage, worker_connection


def test_merge_updates_entire_record_retains_and_inserts(service, business_day):
    old = publish(service, business_day, [record(business_day), record(business_day, 'B', '23')])
    incoming = stage(service, business_day, [record(business_day, 'A', None, None), record(business_day, 'C', '-0.000001')], 'upsert')
    assert [r['id'] for r in rows(service, business_day)] == ['A', 'B']
    result = commit_batch(service, incoming['id'])
    values = rows(service, business_day)
    assert values == [
        {'id': 'A', 'day': business_day, 'amount': None, 'shop': None},
        {'id': 'B', 'day': business_day, 'amount': Decimal('23.000000'), 'shop': '店铺甲'},
        {'id': 'C', 'day': business_day, 'amount': Decimal('-0.000001'), 'shop': '店铺甲'},
    ]
    assert result['row_count'] == 2 and result['published_row_count'] == 3
    assert commit_batch(service, incoming['id'])['published_row_count'] == 3
    with connect(service) as connection:
        name = connection.execute('SELECT table_name FROM datasets WHERE slug=%s', (SLUG,)).fetchone()['table_name']
        assert connection.execute(sql.SQL('SELECT COUNT(*) AS n FROM {} WHERE _batch_id=%s').format(sql.Identifier(name)),
                                  (old['id'],)).fetchone()['n'] == 0


def test_empty_response_preserves_existing_snapshot(service, business_day):
    publish(service, business_day, [record(business_day)])
    result = publish(service, business_day, [], 'upsert')
    assert result['row_count'] == 0 and result['published_row_count'] == 1
    assert rows(service, business_day)[0]['amount'] == Decimal('12.340000')


def test_first_empty_upsert_is_valid_zero_coverage(service, business_day):
    result = publish(service, business_day, [], 'upsert')
    assert result['published_row_count'] == 0
    with connect(service) as connection:
        assert connection.execute('SELECT row_count FROM published_days').fetchone()['row_count'] == 0


def test_default_push_retains_replace_day_semantics(service, business_day):
    publish(service, business_day, [record(business_day), record(business_day, 'B')])
    result = publish(service, business_day, [record(business_day, 'C')])
    assert result['write_mode'] == 'replace_day'
    assert result['row_count'] == result['published_row_count'] == 1
    assert [r['id'] for r in rows(service, business_day)] == ['C']
    publish(service, business_day, [])
    assert rows(service, business_day) == []


def test_same_key_different_business_days_are_isolated(service, business_day):
    other_day = business_day - timedelta(days=1)
    publish(service, business_day, [record(business_day, amount='1')])
    publish(service, other_day, [record(other_day, amount='2')])
    publish(service, business_day, [record(business_day, amount='3')], 'upsert')
    assert rows(service, business_day)[0]['amount'] == Decimal('3')
    assert rows(service, other_day)[0]['amount'] == Decimal('2')


def test_duplicate_pages_fail_without_publishing(service, business_day):
    publish(service, business_day, [record(business_day)])
    pending = create_batch(service, SLUG, business_day, None, None, mode='pull', write_mode='upsert')
    with worker_connection(service) as connection:
        running = claim_next_job(connection)
        assert running['id'] == pending['id']
        assert not process_job(connection, service, running, Pages([[record(business_day, 'C')], [record(business_day, 'C')], []]))
    result = get_batch(service, pending['id'])
    assert result['status'] == 'failed' and result['error_code'] == 'DUPLICATE_RECORD'
    assert result['published_row_count'] is None
    assert [r['id'] for r in rows(service, business_day)] == ['A']


def test_upstream_failure_preserves_old_and_discards_staged(service, business_day):
    class BrokenSource:
        def iter_pages(self, business_date):
            yield [record(business_date, 'C')]
            raise SourceError('TEST_UPSTREAM_TIMEOUT', '独立测试合成超时')
    publish(service, business_day, [record(business_day)])
    pending = create_batch(service, SLUG, business_day, None, None, mode='pull', write_mode='upsert')
    with worker_connection(service) as connection:
        assert not process_job(connection, service, claim_next_job(connection), BrokenSource())
    assert get_batch(service, pending['id'])['error_code'] == 'TEST_UPSTREAM_TIMEOUT'
    assert [r['id'] for r in rows(service, business_day)] == ['A']
    with connect(service) as connection:
        assert connection.execute('SELECT COUNT(*) AS n FROM batch_chunks WHERE batch_id=%s', (pending['id'],)).fetchone()['n'] == 0


def test_merge_limit_rolls_back_copies_and_publication(service, business_day, monkeypatch):
    publish(service, business_day, [record(business_day, str(n)) for n in range(3)])
    pending = stage(service, business_day, [record(business_day, 'NEW')], 'upsert')
    monkeypatch.setattr(ingest, 'MAX_BATCH_ROWS', 3)
    with pytest.raises(ServiceError) as caught:
        commit_batch(service, pending['id'])
    assert caught.value.code == 'PUBLISHED_ROW_LIMIT'
    assert [r['id'] for r in rows(service, business_day)] == ['0', '1', '2']
    with connect(service) as connection:
        name = connection.execute('SELECT table_name FROM datasets WHERE slug=%s', (SLUG,)).fetchone()['table_name']
        assert connection.execute(sql.SQL('SELECT COUNT(*) AS n FROM {} WHERE _batch_id=%s').format(sql.Identifier(name)),
                                  (pending['id'],)).fetchone()['n'] == 1
    monkeypatch.setattr(ingest, 'MAX_BATCH_ROWS', 1000000)
    assert commit_batch(service, pending['id'])['published_row_count'] == 4


def test_failure_after_copy_rolls_back_whole_commit(service, business_day, monkeypatch):
    publish(service, business_day, [record(business_day)])
    pending = stage(service, business_day, [record(business_day, 'B')], 'upsert')
    original_connect = ingest.connect
    class ConnectionProxy:
        def __init__(self, underlying):
            self.underlying = underlying
        def execute(self, statement, *args, **kwargs):
            if isinstance(statement, str) and statement.startswith('INSERT INTO published_days'):
                raise RuntimeError('independent injected publication failure')
            return self.underlying.execute(statement, *args, **kwargs)
    @contextmanager
    def failed_connection(settings):
        with original_connect(settings) as connection:
            yield ConnectionProxy(connection)
    with monkeypatch.context() as scoped:
        scoped.setattr(ingest, 'connect', failed_connection)
        with pytest.raises(RuntimeError, match='injected publication'):
            commit_batch(service, pending['id'])
    assert [r['id'] for r in rows(service, business_day)] == ['A']
    assert get_batch(service, pending['id'])['published_row_count'] is None
    assert commit_batch(service, pending['id'])['published_row_count'] == 2


def test_reader_cannot_observe_partial_merge(service, business_day, monkeypatch):
    publish(service, business_day, [record(business_day)])
    pending = stage(service, business_day, [record(business_day, 'B')], 'upsert')
    copied = threading.Event()
    release = threading.Event()
    original_retain = ingest._retain_missing_rows
    def pause_after_copy(*args, **kwargs):
        count = original_retain(*args, **kwargs)
        copied.set()
        assert release.wait(10)
        return count
    monkeypatch.setattr(ingest, '_retain_missing_rows', pause_after_copy)
    with ThreadPoolExecutor(max_workers=1) as executor:
        result = executor.submit(commit_batch, service, pending['id'])
        try:
            assert copied.wait(10)
            assert [r['id'] for r in rows(service, business_day)] == ['A']
        finally:
            release.set()
        assert result.result(timeout=10)['published_row_count'] == 2
    assert [r['id'] for r in rows(service, business_day)] == ['A', 'B']


def test_duplicate_commit_concurrency_is_idempotent(service, business_day):
    publish(service, business_day, [record(business_day)])
    pending = stage(service, business_day, [record(business_day, 'B')], 'upsert')
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(commit_batch, service, pending['id']) for _ in range(2)]
        results = [future.result(timeout=10) for future in futures]
    assert all(result['published_row_count'] == 2 for result in results)
    assert [r['id'] for r in rows(service, business_day)] == ['A', 'B']


def test_idempotency_key_cannot_change_frozen_mode(service, business_day):
    create_batch(service, SLUG, business_day, 0, 'stable-key', write_mode='upsert')
    with pytest.raises(ServiceError) as caught:
        create_batch(service, SLUG, business_day, 0, 'stable-key', write_mode='replace_day')
    assert caught.value.code == 'IDEMPOTENCY_CONFLICT'

