"""独立经营看板使用的开放接口合同、金额与权限集成测试。

只连接明确命名的 standalone_query_test 隔离库。真实 FastAPI、PostgreSQL、入库和保存
服务流程均不 Mock；唯一不存在的外部边界是供应商，改用可重复合成行。
"""

from copy import deepcopy
from datetime import datetime, timedelta
from decimal import Decimal
import json
import os
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient
import pytest

from data_service import auth, db, datasets, ingest
from data_service.api import create_app
from data_service.config import Settings
from data_service.models import DatasetDefinition
from data_service.query_services import QueryServiceSave


ROOT = Path(__file__).resolve().parents[2]
TEMPLATES = json.loads((ROOT / 'config/dashboard-services.example.json').read_text(encoding='utf-8'))
DATASET = 'ads_product_dashboard_daily'
NUMBERS = ['pay_amount', 'net_sales_amount', 'gross_profit', 'profit_amount', 'goods_cost', 'net_goods_cost',
           'promotion_cost', 'platform_cost', 'refund_amount', 'sales_qty', 'ship_fee', 'sales_fee',
           'custom_fee', 'refund_rate', 'profit_rate', 'profit_pay_est']


def synthetic_row(identifier, **changes):
    row = {field: None for field in NUMBERS}
    row.update(daily_id=identifier, product_id=identifier, channel='淘宝', shop_name='共同店',
               category='家居', owner_person='甲', refund_rate='0.99', profit_rate='0.99',
               profit_pay_est='999999')
    row.update(changes)
    row["net_goods_cost"] = row["goods_cost"]
    row["goods_cost"] = "999999"  # 显著不同的旧字段，防止成本映射回退。
    return row


@pytest.fixture(scope='module')
def cockpit(tmp_path_factory):
    address = os.environ['DS_TEST_DATABASE_URL']
    assert urlsplit(address).path == '/standalone_query_test', '拒绝清理非 standalone_query_test 隔离库'
    directory = tmp_path_factory.mktemp('dashboard_sources')
    source_file = directory / 'sources.json'
    source_file.write_text('{"sources": []}', encoding='utf-8')
    settings = Settings(database_url=address, admin_username='dashboard_test',
                        admin_password='dashboard-isolated-test-only!', source_config_path=str(source_file))
    with db.connect(settings) as connection:
        connection.execute('DROP SCHEMA public CASCADE; CREATE SCHEMA public')
    db.migrate(settings)
    auth.initialize_admin(settings)
    definition = DatasetDefinition.model_validate({
        'slug': DATASET, 'name': '经营看板合成测试', 'date_field': 'dt', 'key_fields': ['daily_id'],
        'fields': [{'name': 'daily_id', 'type': 'string', 'required': True},
                   {'name': 'dt', 'type': 'date', 'required': True, 'dimension': True},
                   *[{'name': field, 'type': 'string', 'dimension': True}
                     for field in ['product_id', 'channel', 'shop_name', 'category', 'owner_person']],
                   *[{'name': field, 'type': 'decimal'} for field in NUMBERS]]})
    datasets.register_dataset(settings, definition)
    day = datetime.now(ZoneInfo('Asia/Shanghai')).date() - timedelta(days=3)
    rows = [
        synthetic_row('001', pay_amount='100', net_sales_amount='80', gross_profit='40', profit_amount='16',
                      goods_cost='30', promotion_cost='10', platform_cost='2', refund_amount='20',
                      sales_qty='1', ship_fee='4', sales_fee='3', custom_fee='1'),
        synthetic_row('002', pay_amount='900', net_sales_amount='720', gross_profit='144', profit_amount='72',
                      goods_cost='200', promotion_cost='100', platform_cost='18', refund_amount='90',
                      sales_qty='9', ship_fee='36', sales_fee='27'),
        synthetic_row('004', channel='京东', pay_amount='500', net_sales_amount='400', gross_profit='200',
                      profit_amount='80', refund_amount='100', category='数码', owner_person='乙'),
        synthetic_row('zero', shop_name='零店', category='零类目', pay_amount='0', net_sales_amount='0',
                      gross_profit='1', profit_amount='-1', refund_amount='2', goods_cost='0'),
        synthetic_row('null', shop_name='空值店', category='空值类目'),
        synthetic_row('negative', shop_name='负数店', category='负数类目', pay_amount='-10',
                      net_sales_amount='-20', gross_profit='-2', profit_amount='-3', refund_amount='-1'),
        *[synthetic_row(f'rank-{number}', shop_name=f'榜单店{number}', category=f'类目{number}',
                       pay_amount=str(number * 100), net_sales_amount=str(number * 100),
                       profit_amount=str(number * 10)) for number in range(1, 7)],
    ]
    later_rows = [synthetic_row('003', pay_amount='100', net_sales_amount='50', gross_profit='-10',
                               profit_amount='-5', goods_cost='20', promotion_cost='5', platform_cost='1',
                               refund_amount='50', sales_qty='1', ship_fee='2', sales_fee='1', custom_fee='2')]
    null_dimensions = [synthetic_row('null-dimensions', channel=None, shop_name=None, owner_person=None,
                                     category=None, pay_amount='123.000001', net_sales_amount='100', profit_amount='10')]
    for publish_day, records in [(day - timedelta(days=2), null_dimensions),
                                 (day - timedelta(days=1), []), (day, rows), (day + timedelta(days=1), later_rows)]:
        batch = ingest.create_batch(settings, DATASET, publish_day, len(records), 'dashboard-synthetic-' + publish_day.isoformat())
        if records:
            ingest.append_chunk(settings, batch['id'], 0, [dict(record, dt=publish_day.isoformat()) for record in records])
        ingest.commit_batch(settings, batch['id'])
    with TestClient(create_app(settings), raise_server_exceptions=False) as client:
        response = client.post('/api/v1/auth/login', json={'username': settings.admin_username,
                                                          'password': settings.admin_password})
        assert response.status_code == 200
        admin = {'Authorization': 'Bearer ' + response.json()['data']['token']}
        for template in TEMPLATES:
            payload = {key: value for key, value in template.items() if key != 'slug'}
            saved = client.put('/api/v1/admin/query-services/' + template['slug'], headers=admin, json=payload)
            assert saved.status_code == 200, saved.text
        response = client.post('/api/v1/admin/keys', headers=admin, json={
            'name': '看板合成数据测试专用', 'grants': {}, 'requests_per_minute': 600,
            'policy': {'services': {template['slug']: DATASET for template in TEMPLATES},
                       'row_scopes': {DATASET: {'mode': 'all', 'groups': []}}}})
        assert response.status_code == 201
        yield {'settings': settings, 'client': client, 'admin': admin, 'day': day,
               'headers': {'X-API-Key': response.json()['data']['api_key']}}


def call(cockpit, name, **changes):
    body = {'start_date': cockpit['day'].isoformat(),
            'end_date': (cockpit['day'] + timedelta(days=1)).isoformat(), **changes}
    response = cockpit['client'].post('/api/v1/services/ds_dashboard_' + name + '/query',
                                      headers=cockpit['headers'], json=body)
    assert response.status_code == 200, response.text
    return response.json()['data']


def selected_shop():
    return [{'field': 'channel', 'op': 'eq', 'value': '淘宝'},
            {'field': 'shop_name', 'op': 'eq', 'value': '共同店'}]


@pytest.mark.parametrize('template', TEMPLATES, ids=lambda template: template['slug'])
def test_template_model_contract_and_api(cockpit, template):
    payload = {key: value for key, value in template.items() if key != 'slug'}
    validated = QueryServiceSave.model_validate(payload)
    assert validated.dataset_slug == DATASET
    assert validated.revision == 0 and validated.enabled
    assert validated.definition.require_complete is False
    assert validated.allowed_filter_fields == ['channel', 'shop_name', 'owner_person']
    assert all(formula.decimal_places == 6 for formula in validated.definition.formulas)
    result = call(cockpit, template['slug'].removeprefix('ds_dashboard_'), limit=validated.max_limit)
    assert result['dataset'] == DATASET
    assert result['coverage']['complete']


def test_weighted_money_formulas_use_aggregate_bill_profit(cockpit):
    row = call(cockpit, 'summary', filters=selected_shop())['rows'][0]
    assert {name: row[name] for name in ['totalSalesAmount', 'netSalesAmount', 'grossProfit', 'netProfit',
                                       'totalRefundAmount', 'goodsCost', 'unitsSold']} == {
        'totalSalesAmount': '1100.000000', 'netSalesAmount': '850.000000', 'grossProfit': '174.000000',
        'netProfit': '83.000000', 'totalRefundAmount': '160.000000', 'goodsCost': '250.000000',
        'unitsSold': '11.000000'}
    assert row['recordCount'] == 3
    assert row['listedCostTotal'] == '462.000000'
    expected = {'grossMargin': '0.204706', 'netMargin': '0.097647', 'refundRate': '0.145455',
                'goodsCostShare': '0.294118', 'adSpendRatio': '0.135294', 'platformFeesShare': '0.024706',
                'shipFeeShare': '0.049412', 'salesFeeShare': '0.036471', 'customFeeShare': '0.003529'}
    assert {key: row[key] for key in expected} == expected
    assert row['refundRate'] != '0.990000' and row['netProfit'] != '2999997.000000'


@pytest.mark.parametrize('shop', ['零店', '空值店'])
def test_null_and_zero_denominator_stay_null(cockpit, shop):
    row = call(cockpit, 'summary', filters=[{'field': 'shop_name', 'op': 'eq', 'value': shop}])['rows'][0]
    ratios = ['grossMargin', 'netMargin', 'refundRate', 'goodsCostShare', 'adSpendRatio',
              'platformFeesShare', 'shipFeeShare', 'salesFeeShare', 'customFeeShare']
    assert all(row[name] is None for name in ratios)
    if shop == '空值店':
        assert row['totalSalesAmount'] is None and row['netProfit'] is None
    else:
        assert row['totalSalesAmount'] == '0.000000' and row['netProfit'] == '-1.000000'


def test_negative_amounts_are_not_clamped(cockpit):
    row = call(cockpit, 'summary', filters=[{'field': 'shop_name', 'op': 'eq', 'value': '负数店'}])['rows'][0]
    assert row['totalSalesAmount'] == '-10.000000' and row['netSalesAmount'] == '-20.000000'
    assert row['netProfit'] == '-3.000000' and row['netMargin'] == '0.150000'
    assert row['refundRate'] == '0.100000'


def test_quality_counts_expose_partial_null(cockpit):
    row = call(cockpit, 'quality', filters=selected_shop())['rows'][0]
    assert row['recordCount'] == 3 and row['customFeeCount'] == 2
    assert row['totalSalesAmountCount'] == 3 and row['netProfitCount'] == 3
    assert len(row) == 13


def test_trend_sorted_and_daily_weighted(cockpit):
    result = call(cockpit, 'trend', filters=selected_shop(), limit=180)
    assert [row['dt'] for row in result['rows']] == [cockpit['day'].isoformat(),
                                                    (cockpit['day'] + timedelta(days=1)).isoformat()]
    assert [row['netProfit'] for row in result['rows']] == ['88.000000', '-5.000000']
    assert [row['netMargin'] for row in result['rows']] == ['0.110000', '-0.100000']


def test_same_shop_name_across_channels_stays_separate(cockpit):
    result = call(cockpit, 'shops', filters=[{'field': 'shop_name', 'op': 'eq', 'value': '共同店'}])
    assert result['total'] == 2
    assert [(row['channel'], row['shop_name'], row['netSalesAmount']) for row in result['rows']] == [
        ('淘宝', '共同店', '850.000000'), ('京东', '共同店', '400.000000')]


@pytest.mark.parametrize('name', ['shops', 'categories'])
def test_top5_ranked_by_net_sales_with_total_and_offset(cockpit, name):
    result = call(cockpit, name)
    assert len(result['rows']) == 5 and result['total'] == 11
    amounts = [Decimal(row['netSalesAmount']) for row in result['rows']]
    assert amounts == sorted(amounts, reverse=True) and amounts[0] == Decimal('850')
    next_page = call(cockpit, name, offset=5)
    assert len(next_page['rows']) == 5
    assert Decimal(next_page['rows'][0]['netSalesAmount']) <= amounts[-1]


@pytest.mark.parametrize(('name', 'field'), [('filter_channels', 'channel'), ('filter_shops', 'shop_name'),
                                           ('filter_owners', 'owner_person')])
def test_dictionary_distinct_sorted_and_unfiltered(cockpit, name, field):
    result = call(cockpit, name, limit=500)
    expected_columns = ['channel', 'shop_name'] if name == 'filter_shops' else [field]
    assert result['columns'] == expected_columns
    values = [tuple(row[column] for column in expected_columns) for row in result['rows']]
    assert len(values) == len(set(values)) and result['total'] == len(values)
    filtered = call(cockpit, name, filters=[{'field': 'channel', 'op': 'eq', 'value': '不存在'}], limit=500)
    assert filtered['rows'] == []


def test_published_empty_day_and_missing_day_remain_distinct(cockpit):
    empty_day = (cockpit['day'] - timedelta(days=1)).isoformat()
    result = call(cockpit, 'summary', start_date=empty_day, end_date=empty_day)
    assert result['coverage'] == {'complete': True, 'missing_dates': []}
    assert result['rows'][0]['recordCount'] == 0 and result['rows'][0]['netProfit'] is None
    missing_day = (cockpit['day'] + timedelta(days=2)).isoformat()
    missing = call(cockpit, 'trend', end_date=missing_day, limit=180)
    assert missing['coverage'] == {'complete': False, 'missing_dates': [missing_day]}
    assert missing_day not in [row['dt'] for row in missing['rows']]


def test_no_match_keeps_empty_summary_and_quality_counts(cockpit):
    filters = [{'field': 'owner_person', 'op': 'eq', 'value': '不存在'}]
    summary = call(cockpit, 'summary', filters=filters)['rows'][0]
    assert summary['recordCount'] == 0 and summary['netSalesAmount'] is None and summary['netMargin'] is None
    quality = call(cockpit, 'quality', filters=filters)['rows'][0]
    assert all(value == 0 for value in quality.values())


@pytest.mark.parametrize('change', [
    {'fields': ['profit_pay_est']}, {'group_by': []}, {'formulas': []}, {'require_complete': True},
    {'filters': [{'field': 'category', 'op': 'eq', 'value': '家居'}]}, {'limit': 2},
])
def test_fixed_template_and_filter_whitelist_cannot_be_overridden(cockpit, change):
    response = cockpit['client'].post('/api/v1/services/ds_dashboard_summary/query',
                                      headers=cockpit['headers'], json={
        'start_date': cockpit['day'].isoformat(), 'end_date': cockpit['day'].isoformat(), **change})
    assert response.status_code == 422


def test_service_only_key_denies_direct_access_and_mutation(cockpit):
    client, headers = cockpit['client'], cockpit['headers']
    assert client.get('/api/v1/datasets/' + DATASET, headers=headers).status_code == 403
    response = client.post('/api/v1/datasets/' + DATASET + '/query', headers=headers,
                           json={'start_date': cockpit['day'].isoformat(), 'end_date': cockpit['day'].isoformat()})
    assert response.status_code == 403
    template = deepcopy(TEMPLATES[0])
    template.pop('slug')
    assert client.put('/api/v1/admin/query-services/ds_dashboard_summary', headers=headers,
                      json=template).status_code == 401


def test_row_scope_filters_before_summary_and_dictionaries(cockpit):
    response = cockpit['client'].post('/api/v1/admin/keys', headers=cockpit['admin'], json={
        'name': '甲店限定合成测试', 'grants': {}, 'requests_per_minute': 600,
        'policy': {'services': {template['slug']: DATASET for template in TEMPLATES},
                   'row_scopes': {DATASET: {'mode': 'filtered', 'groups': [{'conditions': [
                       {'field': 'channel', 'values': ['淘宝']}, {'field': 'shop_name', 'values': ['共同店']}
                   ]}]}}}})
    assert response.status_code == 201
    limited = dict(cockpit, headers={'X-API-Key': response.json()['data']['api_key']})
    row = call(limited, 'summary')['rows'][0]
    assert row['totalSalesAmount'] == '1100.000000' and row['recordCount'] == 3
    assert call(limited, 'filter_channels', limit=500)['rows'] == [{'channel': '淘宝'}]
    denied = call(limited, 'summary', filters=[{'field': 'channel', 'op': 'eq', 'value': '京东'}])
    assert denied['rows'][0]['recordCount'] == 0


@pytest.mark.parametrize(('name', 'columns'), [
    ('filter_channels', ['channel']), ('filter_shops', ['channel', 'shop_name']),
    ('filter_owners', ['owner_person']),
])
def test_null_dimension_dictionary_and_is_null_filter(cockpit, name, columns):
    day = (cockpit['day'] - timedelta(days=2)).isoformat()
    result = call(cockpit, name, start_date=day, end_date=day, limit=500)
    assert result['rows'] == [{column: None for column in columns}]
    summary = call(cockpit, 'summary', start_date=day, end_date=day, filters=[
        {'field': 'channel', 'op': 'is_null', 'value': True},
        {'field': 'shop_name', 'op': 'is_null', 'value': True}])
    assert summary['rows'][0]['totalSalesAmount'] == '123.000001'
    assert summary['rows'][0]['recordCount'] == 1
