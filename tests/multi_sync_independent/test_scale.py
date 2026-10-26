"""4万旧行 + 4万输入的合成规模验证，不推断长期容量或吞吐。"""
from decimal import Decimal
import json
from pathlib import Path
from time import perf_counter
from psycopg import sql

from data_service.db import connect
from data_service.ingest import append_chunk, commit_batch, create_batch
from tests.multi_sync_independent.helpers import SLUG, record


def test_forty_thousand_merge_publishes_sixty_thousand(service, business_day):
    metrics = {'旧记录': 40000, '本次输入': 40000, '覆盖键': 20000, '新增键': 20000, '保留旧键': 20000,
               '环境': '本机 Docker PostgreSQL，4列合成数据，仅供本次验收，不推断180天容量'}
    started = perf_counter()
    old_batch = create_batch(service, SLUG, business_day, 40000, None)
    for chunk_index, start in enumerate(range(0, 40000, 1000)):
        append_chunk(service, old_batch['id'], chunk_index,
                     [record(business_day, f'K{index:06d}', '10.000001', '旧行') for index in range(start, start + 1000)])
    metrics['旧数据分片写入秒'] = round(perf_counter() - started, 4)
    commit_batch(service, old_batch['id'])
    started = perf_counter()
    incoming = create_batch(service, SLUG, business_day, 40000, None, write_mode='upsert')
    for chunk_index, start in enumerate(range(20000, 60000, 200)):
        append_chunk(service, incoming['id'], chunk_index,
                     [record(business_day, f'K{index:06d}', '20.000002', '本次行') for index in range(start, start + 200)])
    metrics['本次200条分片写入秒'] = round(perf_counter() - started, 4)
    started = perf_counter()
    result = commit_batch(service, incoming['id'])
    metrics['合并发布秒'] = round(perf_counter() - started, 4)
    assert result['row_count'] == 40000 and result['published_row_count'] == 60000
    with connect(service) as connection:
        name = connection.execute('SELECT table_name FROM datasets WHERE slug=%s', (SLUG,)).fetchone()['table_name']
        totals = connection.execute(sql.SQL('SELECT COUNT(*) AS n,COUNT(DISTINCT id) AS unique_n,SUM(amount) AS amount,'
                                             "COUNT(*) FILTER(WHERE shop='旧行') AS old_n,COUNT(*) FILTER(WHERE shop='本次行') AS new_n "
                                             'FROM {} r JOIN published_days p ON p.batch_id=r._batch_id').format(sql.Identifier(name))).fetchone()
    assert totals['n'] == totals['unique_n'] == 60000
    assert totals['old_n'] == 20000 and totals['new_n'] == 40000
    assert totals['amount'] == Decimal('1000000.100000')
    metrics.update({'发布记录': totals['n'], '唯一主键数': totals['unique_n'], '金额合计': str(totals['amount']), '结果': '通过'})
    Path(__file__).with_name('scale-result.json').write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding='utf-8')
