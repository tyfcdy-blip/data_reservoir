"""独立黑盒验收，期望值从业务输入推导，不复用公式实现计算。"""
from decimal import Decimal, localcontext, ROUND_HALF_UP
from datetime import timedelta

import pytest
from psycopg.types.json import Jsonb

from data_service import db, query
from data_service.models import QueryRequest

SLUG = 'formula_review'
AGGREGATES = [
    {'field': 'paid', 'op': 'sum', 'alias': 'p'},
    {'field': 'refund', 'op': 'sum', 'alias': 'r'},
    {'op': 'count', 'alias': 'n'},
    {'field': 'units', 'op': 'sum', 'alias': 'u'},
]


def request(day, **changes):
    return {'start_date': day.isoformat(), 'end_date': day.isoformat(), 'group_by': ['shop'],
            'aggregates': AGGREGATES, 'formulas': [{'alias': 'rate', 'expression': 'r / p'}], **changes}


def direct(service, **changes):
    return service[1].post('/api/v1/datasets/' + SLUG + '/query', headers=service[2],
                           json=request(service[3], **changes))


def template(**changes):
    return {'group_by': ['shop'], 'aggregates': AGGREGATES,
            'formulas': [{'alias': 'rate', 'expression': 'r / p'}],
            'order_by': [{'field': 'rate', 'direction': 'desc'}], **changes}


def save(service, slug, definition=None):
    return service[1].put('/api/v1/admin/query-services/' + slug, headers=service[2], json={
        'name': '独立验收公式', 'dataset_slug': SLUG, 'definition': definition or template(),
        'allowed_filter_fields': ['shop'], 'max_limit': 20, 'enabled': True, 'revision': 0})


def public(service, slug, headers=None, **changes):
    return service[1].post('/api/v1/services/' + slug + '/query', headers=headers or service[2],
                           json={'start_date': service[3].isoformat(), 'end_date': service[3].isoformat(), **changes})


def test_grouped_ratio_precedes_sort_and_pagination(service):
    response = direct(service, order_by=[{'field': 'rate', 'direction': 'desc'}], limit=2)
    assert response.status_code == 200, response.text
    result = response.json()['data']
    assert result['total'] == 8
    assert [(row['shop'], row['rate']) for row in result['rows']] == [('G', '0.333333'), ('B', '0.250000')]
    later = direct(service, order_by=[{'field': 'rate', 'direction': 'desc'}], limit=2, offset=2).json()['data']
    assert [(row['shop'], row['rate']) for row in later['rows']] == [('F', '0.250000'), ('A', '0.100000')]
    assert Decimal(later['rows'][1]['rate']) != (Decimal(1) + Decimal(9) / Decimal(99)) / 2


@pytest.mark.parametrize('direction', ['asc', 'desc'])
def test_nulls_sort_last_in_both_directions(service, direction):
    response = direct(service, order_by=[{'field': 'rate', 'direction': direction}])
    assert response.status_code == 200
    rows = response.json()['data']['rows']
    assert [(row['shop'], row['rate']) for row in rows[-2:]] == [('C', None), ('D', None)]
    assert {row['shop']: row['rate'] for row in rows}['H'] == '0.000000'


def test_result_filters_apply_after_rounding_and_total_before_page(service):
    response = direct(service, formulas=[{'alias': 'rate', 'expression': 'r/p', 'decimal_places': 2}],
                      result_filters=[{'field': 'rate', 'op': 'eq', 'value': '0.33'}], limit=1, offset=1)
    assert response.status_code == 200, response.text
    assert response.json()['data']['total'] == 1
    assert response.json()['data']['rows'] == []
    response = direct(service, result_filters=[{'field': 'rate', 'op': 'gte', 'value': '0.25'},
                                               {'field': 'p', 'op': 'gte', 'value': '100'}],
                      order_by=[{'field': 'rate', 'direction': 'desc'}], limit=1)
    assert response.json()['data']['total'] == 2
    assert response.json()['data']['rows'][0]['shop'] == 'B'


def test_integer_fraction_percent_unary_precedence_and_rounding(service):
    response = direct(service, filters=[{'field': 'shop', 'op': 'eq', 'value': 'A'}], formulas=[
        {'alias': 'fraction', 'expression': 'n/u', 'decimal_places': 12},
        {'alias': 'percent', 'expression': '(r / p) * 100', 'decimal_places': 2},
        {'alias': 'negative_round', 'expression': '-1.005+n*0', 'decimal_places': 2},
        {'alias': 'precedence', 'expression': '1+2*3-(4/2)', 'decimal_places': 0},
    ])
    assert response.status_code == 200, response.text
    row = response.json()['data']['rows'][0]
    assert (row['fraction'], row['percent'], row['negative_round'], row['precedence']) == (
        '0.250000000000', '10.00', '-1.01', '5')


def test_zero_null_empty_and_global_aggregate(service):
    response = direct(service, group_by=[], filters=[{'field': 'shop', 'op': 'eq', 'value': 'absent'}])
    assert response.status_code == 200
    row = response.json()['data']['rows'][0]
    assert row['p'] is None and row['n'] == 0 and row['rate'] is None
    response = direct(service, formulas=[{'alias': 'rate', 'expression': 'p/(n-n)+1'}])
    assert response.status_code == 200 and all(row['rate'] is None for row in response.json()['data']['rows'])
    response = direct(service, result_filters=[{'field': 'rate', 'op': 'is_null', 'value': True}])
    assert [row['shop'] for row in response.json()['data']['rows']] == ['C', 'D']


def test_result_in_filter_count_distinct_and_raw_filter_precedence(service):
    response = direct(service, filters=[{'field': 'shop', 'op': 'in', 'value': ['B', 'F', 'H']}],
                      aggregates=[*AGGREGATES, {'field': 'id', 'op': 'count_distinct', 'alias': 'distinct_ids'}],
                      formulas=[{'alias': 'rate', 'expression': 'r/p'}, {'alias': 'mean', 'expression': 'p/distinct_ids'}],
                      result_filters=[{'field': 'rate', 'op': 'in', 'value': ['0', '0.25']}])
    assert response.status_code == 200, response.text
    assert [(row['shop'], row['mean']) for row in response.json()['data']['rows']] == [
        ('B', '100.000000'), ('F', '200.000000'), ('H', '1.000000')]


@pytest.mark.parametrize('expression', [
    "p;DROP TABLE datasets", '__import__("os")', 'SUM(p)', 'p.__class__', 'p[0]', 'p**2', 'p%2',
    'p//2', 'p or 1', 'p=1', 'p::numeric', 'p/*a*/1', 'p + NaN', 'p + Infinity',
    'p + 1e100000', 'p + 0x10', '(SELECT 1)', 'p ? 1 : 0', 'p + \x00',
    '9'*101, 'p+(' * 30 + 'p' + ')' * 30,
])
def test_malicious_or_unbounded_expression_rejected(service, expression):
    response = direct(service, formulas=[{'alias': 'rate', 'expression': expression}])
    assert response.status_code == 422, response.text
    assert 'DROP TABLE' not in response.text and '__import__' not in response.text


@pytest.mark.parametrize('changes', [
    {'formulas': [{'alias': 'rate', 'expression': 'paid/p'}]},
    {'formulas': [{'alias': 'rate', 'expression': 'p/rate'}]},
    {'formulas': [{'alias': 'first', 'expression': 'r/p'}, {'alias': 'second', 'expression': 'first*100'}]},
    {'formulas': [{'alias': 'p', 'expression': 'r/p'}]},
    {'formulas': [{'alias': 'shop', 'expression': 'r/p'}]},
    {'formulas': [{'alias': 'rate', 'expression': 'r/p', 'decimal_places': True}]},
    {'formulas': [{'alias': 'rate', 'expression': 'r/p', 'decimal_places': 13}]},
    {'formulas': [{'alias': 'rate', 'expression': 'r/p', 'decimal_places': -1}]},
    {'formulas': [{'alias': 'rate', 'expression': 'r/p', 'decimal_places': '2'}]},
    {'result_filters': [{'field': 'shop', 'op': 'eq', 'value': 'A'}]},
    {'result_filters': [{'field': 'paid', 'op': 'eq', 'value': 1}]},
    {'result_filters': [{'field': 'rate', 'op': 'eq', 'value': True}]},
    {'result_filters': [{'field': 'rate', 'op': 'eq', 'value': 'NaN'}]},
    {'result_filters': [{'field': 'rate', 'op': 'eq', 'value': 'Infinity'}]},
    {'result_filters': [{'field': 'rate', 'op': 'eq', 'value': '1e999999'}]},
    {'result_filters': [{'field': 'rate', 'op': 'in', 'value': [None]}]},
    {'order_by': [{'field': 'missing', 'direction': 'desc'}]},
    {'fields': ['id']},
    {'aggregates': [], 'group_by': []},
    {'aggregates': [{'field': 'shop', 'op': 'min', 'alias': 'p'}], 'formulas': [{'alias': 'rate', 'expression': 'p+1'}]},
    {'aggregates': [{'field': 'dt', 'op': 'max', 'alias': 'p'}], 'formulas': [{'alias': 'rate', 'expression': 'p+1'}]},
])
def test_invalid_type_reference_or_mode_rejected(service, changes):
    response = direct(service, **changes)
    assert response.status_code == 422, response.text


def test_limits_and_partial_failure_leave_service_usable(service):
    assert direct(service, formulas=[{'alias': f'f{i}', 'expression': 'r/p'} for i in range(11)]).status_code == 422
    assert direct(service, result_filters=[{'field': 'p', 'op': 'gt', 'value': 0}] * 21).status_code == 422
    response = service[1].post('/api/v1/query/batch', headers=service[2], json={'queries': [
        {'dataset': SLUG, 'query': request(service[3])},
        {'dataset': SLUG, 'query': request(service[3], formulas=[{'alias': 'bad', 'expression': 'missing+1'}])},
    ]})
    assert response.status_code == 422
    assert direct(service).status_code == 200


def test_preview_save_direct_and_batch_agree_without_publishing_preview(service):
    rule = template(result_filters=[{'field': 'rate', 'op': 'gte', 'value': '0.1'}])
    preview = service[1].post('/api/v1/admin/query-services/preview', headers=service[2], json={
        'dataset_slug': SLUG, 'definition': rule,
        'start_date': service[3].isoformat(), 'end_date': service[3].isoformat()})
    assert preview.status_code == 200, preview.text
    assert service[1].get('/api/v1/admin/query-services/formula_agreement', headers=service[2]).status_code == 404
    assert save(service, 'formula_agreement', rule).status_code == 200
    saved = public(service, 'formula_agreement')
    direct_result = direct(service, **rule)
    batch = service[1].post('/api/v1/query/batch', headers=service[2], json={'queries': [
        {'dataset': SLUG, 'query': request(service[3], **rule)}]})
    assert saved.status_code == direct_result.status_code == batch.status_code == 200
    assert preview.json()['data']['rows'] == saved.json()['data']['rows'] == direct_result.json()['data']['rows'] == batch.json()['data'][0]['rows']
    loaded = service[1].get('/api/v1/admin/query-services/formula_agreement', headers=service[2]).json()['data']
    assert loaded['definition']['formulas'][0]['decimal_places'] == 6
    assert loaded['definition']['result_filters'] == rule['result_filters']


@pytest.mark.parametrize('override', ['formulas', 'result_filters', 'order_by', 'aggregates', 'fields'])
def test_external_caller_cannot_override_processing_rules(service, override):
    slug = 'no_override_' + override
    assert save(service, slug).status_code == 200
    assert public(service, slug, **{override: []}).status_code == 422


def test_old_stored_json_without_new_keys_remains_compatible(service):
    slug = 'legacy_rule'
    old = {'group_by': ['shop'], 'aggregates': [{'field': 'paid', 'op': 'sum', 'alias': 'p'}]}
    assert save(service, slug, old).status_code == 200
    with db.connect(service[0]) as connection:
        connection.execute('UPDATE query_services SET definition=%s WHERE slug=%s', (Jsonb(old), slug))
    response = public(service, slug)
    assert response.status_code == 200
    assert response.json()['data']['columns'] == ['shop', 'p']
    assert response.json()['data']['total'] == 8


def test_formula_permissions_and_key_revocation(service):
    slug = 'permissions'
    assert save(service, slug).status_code == 200
    assert service[1].post('/api/v1/services/' + slug + '/query', json={
        'start_date': service[3].isoformat(), 'end_date': service[3].isoformat()}).status_code == 401
    for grant, expected in [('write', 403), ('read', 200)]:
        issued = service[1].post('/api/v1/admin/keys', headers=service[2], json={
            'name': 'formula-' + grant, 'grants': {SLUG: [grant]}}).json()['data']
        header = {'X-API-Key': issued['api_key']}
        assert public(service, slug, headers=header).status_code == expected
        assert service[1].post('/api/v1/admin/query-services/preview', headers=header,
                              json={'dataset_slug': SLUG, 'definition': template(),
                                    'start_date': service[3].isoformat(), 'end_date': service[3].isoformat()}).status_code == 401
        assert service[1].delete('/api/v1/admin/keys/' + issued['id'], headers=service[2]).status_code == 200
        assert public(service, slug, headers=header).status_code == 401


def test_query_connection_is_read_only_and_request_failure_recovers(service, monkeypatch):
    original = query.execute_query
    observed = []
    def checked(connection, slug, payload, **kwargs):
        observed.append(connection.execute('SHOW transaction_read_only').fetchone()['transaction_read_only'])
        return original(connection, slug, payload, **kwargs)
    monkeypatch.setattr(query, 'execute_query', checked)
    result = query.run_query(service[0], SLUG, QueryRequest.model_validate(request(service[3])))
    assert result['total'] == 8 and observed == ['on']
    response = direct(service, start_date=(service[3] - timedelta(days=1)).isoformat())
    assert response.status_code == 409
    assert direct(service).status_code == 200


def test_final_rounding_retains_intermediate_precision_for_large_valid_constant(service):
    multiplier = '1000000000000000000000000000000000000000'
    response = direct(service, formulas=[{'alias': 'rate', 'expression': '(1/3)*' + multiplier,
                                          'decimal_places': 6}], limit=1)
    assert response.status_code == 200, response.text
    with localcontext() as context:
        context.prec = 120
        expected = (Decimal(multiplier) / 3).quantize(Decimal('0.000001'), rounding=ROUND_HALF_UP)
    assert Decimal(response.json()['data']['rows'][0]['rate']) == expected


@pytest.mark.parametrize('expression,expected', [
    ('0*(p/0)', None), ('(p/0)/(p/0)', None), ('(r/p)-(r/p)', '0.000000'),
    ('(1/3)+(1/3)+(1/3)', '1.000000'), ('(-1/6)*3', '-0.500000'),
    ('1/(-8)', '-0.125000'), ('1/(1/3)', '3.000000'), ('1/(1/0)', None),
    ('0.1234564999999', '0.123456'), ('-0.1234564999999', '-0.123456'), ('0.1234565', '0.123457'),
])
def test_rational_operations_preserve_null_and_exact_final_rounding(service, expression, expected):
    response = direct(service, filters=[{'field': 'shop', 'op': 'eq', 'value': 'A'}],
                      formulas=[{'alias': 'rate', 'expression': expression}])
    assert response.status_code == 200, response.text
    assert response.json()['data']['rows'][0]['rate'] == expected


def test_bounded_complex_fraction_expression_keeps_parameters_and_result_consistent(service):
    parts = ['(1/2)'] * 16
    while len(parts) > 1:
        parts = ['(' + parts[index] + '+' + parts[index + 1] + ')' for index in range(0, len(parts), 2)]
    response = direct(service, formulas=[{'alias': 'rate', 'expression': parts[0]}])
    assert response.status_code == 200, response.text
    assert all(row['rate'] == '8.000000' for row in response.json()['data']['rows'])
    assert direct(service, limit=1).status_code == 200
