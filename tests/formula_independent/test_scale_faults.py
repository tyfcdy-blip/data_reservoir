"""真实 PostgreSQL 的规模、超时恢复与快照一致性验收。"""
from collections import defaultdict
from decimal import Decimal, ROUND_HALF_UP, localcontext
import json
from pathlib import Path
from time import perf_counter

from fastapi.testclient import TestClient
from psycopg import sql

from data_service import db, query
from data_service.api import create_app
from data_service.datasets import register_dataset
from data_service.models import BatchQueryItem, QueryRequest
from tests.formula_independent.conftest import definition, publish
from tests.formula_independent.test_contract import request, SLUG


def test_40000_rows_correct_sorted_filtered_paginated(settings, day):
    slug = 'formula_load'
    register_dataset(settings, definition(slug))
    rows = []
    expected = defaultdict(lambda: [Decimal(0), Decimal(0)])
    for number in range(40000):
        shop = 'shop_' + format(number % 200, '03d')
        paid = Decimal(100 + number % 113) + Decimal(number % 100) / 100
        refund = Decimal(number % 17) + Decimal(number % 10) / 100
        rows.append({'dt': day.isoformat(), 'id': str(number), 'shop': shop, 'paid': str(paid),
                     'refund': str(refund), 'units': 1, 'flag': False})
        expected[shop][0] += paid
        expected[shop][1] += refund
    started = perf_counter()
    publish(settings, slug, day, rows)
    ingest_seconds = perf_counter() - started
    with localcontext() as context:
        context.prec = 80
        expected_rows = [{'shop': shop, 'rate': (refund / paid).quantize(Decimal('0.000001'), rounding=ROUND_HALF_UP)}
                         for shop, (paid, refund) in expected.items()]
    expected_rows = sorted([row for row in expected_rows if row['rate'] >= Decimal('0.05')],
                           key=lambda row: (-row['rate'], row['shop']))
    payload = QueryRequest.model_validate(request(day, order_by=[{'field': 'rate', 'direction': 'desc'}],
        result_filters=[{'field': 'rate', 'op': 'gte', 'value': '0.05'}], limit=17, offset=23))
    started = perf_counter()
    result = query.run_query(settings, slug, payload)
    query_seconds = perf_counter() - started
    assert result['total'] == len(expected_rows)
    actual = [{'shop': row['shop'], 'rate': Decimal(row['rate'])} for row in result['rows']]
    assert actual == expected_rows[23:40]
    assert result['published_versions'][0]['row_count'] == 40000
    Path('evidence/formula-independent-load.json').write_text(json.dumps({
        'synthetic_rows': 40000, 'fields': 7, 'groups': 200, 'filtered_groups': len(expected_rows),
        'returned_rows': len(actual), 'ingest_seconds': ingest_seconds, 'query_seconds': query_seconds,
        'correctness': 'PASS: exact independently calculated groups, rate rounding, sort, filter, count and page',
        'scope': 'Single execution on local real PostgreSQL; synthetic, not production capacity or SLA',
    }, ensure_ascii=False, indent=2), encoding='utf-8')


def test_real_database_lock_timeout_returns_504_then_query_recovers(service):
    settings, _, headers, day = service
    with db.connect(settings) as blocker:
        table = blocker.execute('SELECT table_name FROM datasets WHERE slug=%s', (SLUG,)).fetchone()['table_name']
        blocker.execute(sql.SQL('LOCK TABLE {} IN ACCESS EXCLUSIVE MODE').format(sql.Identifier(table)))
        timed_settings = settings.model_copy(update={'statement_timeout_ms': 100})
        with TestClient(create_app(timed_settings), raise_server_exceptions=False) as client:
            response = client.post('/api/v1/datasets/' + SLUG + '/query', headers=headers, json=request(day))
            assert response.status_code == 504, response.text
            assert response.json()['error_code'] == 'QUERY_TIMEOUT'
            assert table not in response.text and 'postgres' not in response.text.lower()
    response = service[1].post('/api/v1/datasets/' + SLUG + '/query', headers=headers, json=request(day))
    assert response.status_code == 200 and response.json()['data']['total'] == 8


def test_batch_formula_stays_on_one_snapshot_during_publish(settings, day, monkeypatch):
    slug = 'formula_snapshot'
    register_dataset(settings, definition(slug))
    original_rows = [{'dt': day.isoformat(), 'id': 'one', 'shop': 'A', 'paid': '100', 'refund': '10', 'units': 1, 'flag': False}]
    publish(settings, slug, day, original_rows)
    original = query.execute_query
    calls = []
    def concurrent_publish(connection, dataset, payload, **kwargs):
        result = original(connection, dataset, payload, **kwargs)
        calls.append(result['rows'][0]['rate'])
        if len(calls) == 1:
            publish(settings, slug, day, [{**original_rows[0], 'refund': '90'}])
        return result
    monkeypatch.setattr(query, 'execute_query', concurrent_publish)
    payload = QueryRequest.model_validate(request(day))
    results = query.run_batch(settings, [BatchQueryItem(dataset=slug, query=payload)] * 2)
    assert [result['rows'][0]['rate'] for result in results] == ['0.100000', '0.100000']
    monkeypatch.setattr(query, 'execute_query', original)
    assert query.run_query(settings, slug, payload)['rows'][0]['rate'] == '0.900000'
