"""复用测试数据操作；数据库与合并逻辑均使用真实 PostgreSQL。"""
from psycopg import sql
from data_service.db import connect
from data_service.ingest import append_chunk, commit_batch, create_batch
from data_service.source_registry import SourceSave, save_source
from data_service.worker import acquire_worker_lock

SLUG = 'review_orders'


def record(day, key='A', amount='12.340000', shop='店铺甲'):
    return {'day': day.isoformat(), 'id': key, 'amount': amount, 'shop': shop}


def stage(settings, day, rows, mode='replace_day'):
    batch = create_batch(settings, SLUG, day, len(rows), None, write_mode=mode)
    append_chunk(settings, batch['id'], 0, rows)
    return batch


def publish(settings, day, rows, mode='replace_day'):
    return commit_batch(settings, stage(settings, day, rows, mode)['id'])


def rows(settings, day):
    with connect(settings) as connection:
        name = connection.execute('SELECT table_name FROM datasets WHERE slug=%s', (SLUG,)).fetchone()['table_name']
        return connection.execute(sql.SQL('SELECT r.id,r.amount,r.shop,r.day FROM {} r JOIN published_days p '
                                           'ON p.batch_id=r._batch_id WHERE p.business_date=%s ORDER BY r.id')
                                  .format(sql.Identifier(name)), (day,)).fetchall()


def source(settings, **overrides):
    payload = {'title': '独立测试来源', 'approve_host': True,
               'config': {'dataset_slug': SLUG, 'url': 'https://source.test/rows'},
               'schedule_enabled': True, 'schedule_times': ['03:15', '13:45'],
               'refresh_days': 1, 'write_mode': 'upsert'}
    payload.update(overrides)
    return save_source(settings, SLUG, SourceSave.model_validate(payload))


def worker_connection(settings):
    connection = connect(settings)
    connection.autocommit = True
    assert acquire_worker_lock(connection)
    return connection


class Pages:
    def __init__(self, contents):
        self.contents = contents

    def iter_pages(self, business_date):
        yield from self.contents
