"""多时点调度与兼容性契约的独立检查，不请求第三方。"""
from datetime import datetime, time, timedelta, timezone
import json

from pydantic import ValidationError
import pytest

from data_service.errors import ServiceError
from data_service.ingest import BEIJING, cancel_batch, create_batch, enqueue_sync, get_batch
from data_service.source_registry import SourceSave, get_source, load_active_sources, save_source
from data_service.worker import claim_next_job, enqueue_schedule, process_job
from tests.multi_sync_independent.helpers import SLUG, Pages, source, worker_connection


def payload(**updates):
    value = {'title': '合成来源', 'approve_host': True, 'config': {'dataset_slug': SLUG, 'url': 'https://source.test/rows'}}
    value.update(updates)
    return value


def at(today, hour, minute=0):
    return datetime.combine(today, time(hour, minute), BEIJING)


def finish_one(connection, settings):
    batch = claim_next_job(connection)
    assert batch is not None
    assert process_job(connection, settings, batch, Pages([[]]))
    return get_batch(settings, batch['id'])


@pytest.mark.parametrize('invalid', [[], ['3:00'], ['03:0'], ['24:00'], ['12:60'], [' 03:00'], ['03:00 '], ['03:00:00'], ['03:00', '03:00'], [300], None, [f'{hour:02}:00' for hour in range(24)] + ['00:01']])
def test_invalid_schedule_times_rejected(invalid):
    with pytest.raises(ValidationError):
        SourceSave.model_validate(payload(schedule_times=invalid))


def test_times_sort_and_old_hour_compatibility():
    configured = SourceSave.model_validate(payload(schedule_times=['23:59', '00:00', '08:30']))
    assert configured.schedule_times == ['00:00', '08:30', '23:59']
    old = SourceSave.model_validate(payload(schedule_hour=6))
    assert old.schedule_hour == 6


def test_twenty_four_distinct_slots_accepted():
    values = [f'{hour:02}:59' for hour in range(24)]
    assert SourceSave.model_validate(payload(schedule_times=values)).schedule_times == values


@pytest.mark.parametrize('mode', ['merge', '', None, 1])
def test_invalid_write_mode_rejected(mode):
    with pytest.raises(ValidationError):
        SourceSave.model_validate(payload(write_mode=mode))


def test_save_read_roundtrip_and_legacy_edit_preserve_upsert(service):
    saved = source(service, schedule_times=['14:25', '03:15'])
    assert saved['schedule_times'] == ['03:15', '14:25'] and saved['write_mode'] == 'upsert'
    updated = save_source(service, SLUG, SourceSave.model_validate(payload(revision=saved['revision'], schedule_hour=5)))
    assert updated['write_mode'] == 'upsert'
    assert updated['schedule_times'] == ['05:00']
    assert get_source(service, SLUG)['write_mode'] == 'upsert'


def test_manual_queue_freezes_mode_and_blocks_today(service, business_day, today):
    saved = source(service)
    queued = enqueue_sync(service, SLUG, business_day, business_day)[0]
    assert queued['write_mode'] == 'upsert'
    source(service, revision=saved['revision'], write_mode='replace_day')
    assert get_batch(service, queued['id'])['write_mode'] == 'upsert'
    cancel_batch(service, queued['id'])
    assert enqueue_sync(service, SLUG, business_day, business_day)[0]['write_mode'] == 'replace_day'
    with pytest.raises(ServiceError) as caught:
        enqueue_sync(service, SLUG, today, today)
    assert caught.value.code == 'DATE_NOT_FINISHED'


def test_exact_minute_utc_conversion_and_restart_idempotency(service, today):
    source(service)
    with worker_connection(service) as connection:
        configs = load_active_sources(service)
        assert enqueue_schedule(connection, service, configs, at(today, 3, 14)) == 0
        assert enqueue_schedule(connection, service, configs, at(today, 3, 15).astimezone(timezone.utc)) == 1
        first = finish_one(connection, service)
        assert first['write_mode'] == 'upsert'
        assert enqueue_schedule(connection, service, load_active_sources(service), at(today, 3, 15)) == 0
        assert enqueue_schedule(connection, service, configs, at(today, 13, 44)) == 0
    with worker_connection(service) as connection:
        assert enqueue_schedule(connection, service, load_active_sources(service), at(today, 13, 45)) == 1
        second = finish_one(connection, service)
        assert second['id'] != first['id']
        assert enqueue_schedule(connection, service, load_active_sources(service), at(today, 23, 59)) == 0


def test_missed_slots_catch_up_serially_without_duplicate(service, today):
    source(service, schedule_times=['01:00', '09:30', '17:45'])
    with worker_connection(service) as connection:
        configs = load_active_sources(service)
        keys = []
        for _unused in range(3):
            assert enqueue_schedule(connection, service, configs, at(today, 18)) == 1
            assert enqueue_schedule(connection, service, configs, at(today, 18)) == 0
            keys.append(finish_one(connection, service)['idempotency_key'])
        assert len(set(keys)) == 3
        assert enqueue_schedule(connection, service, configs, at(today, 23, 59)) == 0


def test_active_manual_job_defers_due_slot(service, today, business_day):
    source(service)
    active = create_batch(service, SLUG, business_day, None, None, mode='pull')
    with worker_connection(service) as connection:
        configs = load_active_sources(service)
        assert enqueue_schedule(connection, service, configs, at(today, 3, 15)) == 0
        cancel_batch(service, active['id'])
        assert enqueue_schedule(connection, service, configs, at(today, 3, 15)) == 1


def test_legacy_daily_key_counts_only_as_first_slot(service, today, business_day):
    source(service)
    old_key = f'schedule:{today.isoformat()}:{business_day.isoformat()}'
    create_batch(service, SLUG, business_day, None, old_key, mode='pull', write_mode='replace_day')
    with worker_connection(service) as connection:
        finish_one(connection, service)
        configs = load_active_sources(service)
        assert enqueue_schedule(connection, service, configs, at(today, 3, 15)) == 0
        assert enqueue_schedule(connection, service, configs, at(today, 13, 45)) == 1
        assert finish_one(connection, service)['write_mode'] == 'upsert'
        assert enqueue_schedule(connection, service, configs, at(today, 13, 45)) == 0


def test_retention_limits_refreshed_dates_and_disabled_source(service, today):
    saved = source(service, refresh_days=180, schedule_times=['01:00'])
    with worker_connection(service) as connection:
        assert enqueue_schedule(connection, service, load_active_sources(service), at(today, 2)) == 179
        dates = connection.execute('SELECT business_date FROM batches').fetchall()
        assert min(row['business_date'] for row in dates) == today - timedelta(days=179)
        assert max(row['business_date'] for row in dates) == today - timedelta(days=1)
    source(service, revision=saved['revision'], enabled=False)
    assert SLUG not in load_active_sources(service)


def test_file_source_keeps_legacy_hour_and_replace_semantics(service, today):
    from pathlib import Path
    Path(service.source_config_path).write_text(json.dumps({'sources': [{'dataset_slug': SLUG, 'url': 'https://source.test/rows'}]}), encoding='utf-8')
    service = service.model_copy(update={'schedule_enabled': True, 'schedule_hour': 4, 'refresh_days': 1})
    with worker_connection(service) as connection:
        configs = load_active_sources(service)
        assert enqueue_schedule(connection, service, configs, at(today, 3, 59)) == 0
        assert enqueue_schedule(connection, service, configs, at(today, 4)) == 1
        first = finish_one(connection, service)
        assert first['write_mode'] == 'replace_day'
        assert enqueue_schedule(connection, service, configs, at(today, 23, 59)) == 0


