"""自然语言草稿严格校验，保留手工协议；模型边界替身不替代查询编译。"""

import copy
import json

import pytest

from data_service import db, llm_client, query_drafts
from data_service.errors import ServiceError
from data_service.query_drafts import DraftRequest
from test_llm_config import KEY, configuration, put, service as service


def draft(**changes):
    return {'status': 'ready', 'name': '店铺类目渠道负责人汇总', 'slug': 'sales_summary', 'definition': {
        'fields': None, 'group_by': ['shop', 'category', 'channel', 'owner'],
        'aggregates': [{'field': 'paid', 'op': 'sum', 'alias': 'paid_total'},
                       {'field': 'refund', 'op': 'sum', 'alias': 'refund_total'}],
        'formulas': [{'alias': 'refund_rate', 'expression': 'refund_total / paid_total', 'decimal_places': 6}],
        'order_by': [{'field': 'refund_rate', 'direction': 'desc'}]},
        'allowed_filter_fields': ['shop', 'category', 'channel', 'owner'], 'max_limit': 100,
        'questions': [], 'notes': ['先分组汇总退款金额与支付金额，再计算退款率。'], **changes}


def generate(service, prompt='按店铺、类目、渠道、负责人汇总支付金额和退款金额，计算退款率降序'):
    return service[1].post('/api/v1/admin/query-services/draft', headers=service[2],
                           json={'dataset_slug': 'sales', 'prompt': prompt})


def test_ready_metadata_minimization_no_write_and_manual_compatible(service, monkeypatch):
    assert put(service).status_code == 200
    captured = []
    def complete(config, messages, token_budget=6000):
        captured.append((config, messages))
        return draft()
    monkeypatch.setattr(llm_client, 'complete', complete)
    result = generate(service)
    assert result.status_code == 200, result.text
    document = result.json()['data']
    assert document['definition']['formulas'][0]['expression'] == 'refund_total / paid_total'
    metadata = json.loads(captured[0][1][1]['content'])['dataset']
    assert set(metadata) == {'name', 'slug', 'date_field', 'fields'}
    assert set(metadata['fields'][0]) == {'name', 'type', 'dimension', 'description'}
    encoded = json.dumps(captured[0][1], ensure_ascii=False)
    assert KEY not in encoded and 'models.example.test' not in encoded and 'table_name' not in encoded
    with db.connect(service[0]) as connection:
        assert connection.execute('SELECT count(*) AS n FROM query_services').fetchone()['n'] == 0
        assert connection.execute('SELECT count(*) AS n FROM batches').fetchone()['n'] == 0
    # 草稿由管理员应用后仍走原保存接口；没有新增另一套查询协议。
    saved = service[1].put('/api/v1/admin/query-services/sales_summary', headers=service[2], json={
        'name': document['name'], 'dataset_slug': 'sales', 'definition': document['definition'],
        'allowed_filter_fields': document['allowed_filter_fields'], 'max_limit': document['max_limit'],
        'enabled': False, 'revision': 0})
    assert saved.status_code == 200 and not saved.json()['data']['enabled']


def test_unconfigured_disabled_and_invalid_request(service):
    assert generate(service).status_code == 409
    assert put(service, enabled=False).status_code == 200
    assert generate(service).status_code == 409
    for body in ({'dataset_slug': 'sales', 'prompt': ' '}, {'dataset_slug': 'sales', 'prompt': 'x' * 4001},
                 {'dataset_slug': 'sales', 'prompt': '\ud800'}, {'dataset_slug': 'sales', 'prompt': 'ok', 'sql': 'select 1'}):
        response = service[1].post('/api/v1/admin/query-services/draft',
                                   headers={**service[2], 'Content-Type': 'application/json'},
                                   content=json.dumps(body).encode())
        assert response.status_code == 422


def test_nonexistent_dataset(service):
    response = service[1].post('/api/v1/admin/query-services/draft', headers=service[2],
                               json={'dataset_slug': 'missing', 'prompt': '汇总金额'})
    assert response.status_code == 404


@pytest.mark.parametrize('replacement', [
    {'definition': {'fields': ['id']}},
    {'definition': {'group_by': ['id'], 'aggregates': [{'op': 'count', 'alias': 'n'}]}},
    {'definition': {'aggregates': [{'field': 'missing', 'op': 'sum', 'alias': 'paid_total'}]}},
    {'definition': {'aggregates': [{'field': 'shop', 'op': 'sum', 'alias': 'paid_total'}]}},
    {'definition': {'aggregates': [{'field': 'paid', 'op': 'sum', 'alias': 'x'}],
                    'formulas': [{'alias': 'rate', 'expression': "__import__('os')"}]}},
    {'definition': {'aggregates': [{'field': 'paid', 'op': 'sum', 'alias': 'x'}],
                    'formulas': [{'alias': 'rate', 'expression': 'other / x'}]}},
    {'definition': {'aggregates': [{'field': 'paid', 'op': 'sum', 'alias': 'x'}],
                    'order_by': [{'field': 'unknown', 'direction': 'desc'}]}},
    {'definition': {'aggregates': [{'field': 'paid', 'op': 'sum', 'alias': 'x'}],
                    'formulas': [{'alias': 'rate', 'expression': 'x / 2'},
                                 {'alias': 'chained', 'expression': 'rate * 100'}]}},
    {'allowed_filter_fields': ['paid_total']}, {'allowed_filter_fields': ['shop', 'shop']},
    {'max_limit': 501}, {'enabled': True}, {'dataset_slug': 'other'}, {'slug': 'SELECT 1'},
    {'status': 'ready', 'questions': ['尚未明确']}, {'name': ''}, {'notes': ['x\x00']},
    {'status': 'needs_clarification', 'questions': ['请选择字段']},
])
def test_invalid_model_output_never_becomes_usable(service, monkeypatch, replacement):
    put(service)
    monkeypatch.setattr(llm_client, 'complete', lambda *_args, **_kwargs: draft(**replacement))
    response = generate(service)
    assert response.status_code == 502 and response.json()['error_code'] == 'LLM_DRAFT_INVALID'
    assert KEY not in response.text
    assert service[1].get('/api/v1/admin/query-services', headers=service[2]).json()['data'] == []


def test_model_clarification_and_upstream_error(service, monkeypatch):
    put(service)
    monkeypatch.setattr(llm_client, 'complete', lambda *_args, **_kwargs:
        draft(status='needs_clarification', name='', slug='', definition=None, questions=['请确认退款口径']))
    response = generate(service)
    assert response.status_code == 200 and response.json()['data']['definition'] is None
    def fail(*_args, **_kwargs):
        raise ServiceError(504, 'LLM_TIMEOUT', '模型响应超时')
    monkeypatch.setattr(llm_client, 'complete', fail)
    assert generate(service).status_code == 504
    # 上游失败后锁必须释放。
    monkeypatch.setattr(llm_client, 'complete', lambda *_args, **_kwargs: draft())
    assert generate(service).status_code == 200


def test_unsaved_connection_test_uses_only_control_prompt(service, monkeypatch):
    observed = []
    def controlled(config, messages, token_budget=6000):
        observed.append((config, messages, token_budget))
        return {'connection': 'ok'}
    monkeypatch.setattr(llm_client, 'complete', controlled)
    response = service[1].post('/api/v1/admin/llm/test', headers=service[2], json=configuration(enabled=False))
    assert response.status_code == 200 and response.json()['data']['ok']
    assert observed[0][0].api_key == KEY and observed[0][2] == 100
    assert 'dataset' not in json.dumps(observed[0][1])
    assert service[1].get('/api/v1/admin/llm', headers=service[2]).json()['data']['revision'] == 0


@pytest.fixture
def refunds():
    return {'fields': [
        {'name': 'all_refund', 'type': 'decimal', 'description': '退款成功日全部退款，含无效单'},
        {'name': 'valid_refund', 'type': 'decimal', 'description': '有效成功退款，保留代发'},
        {'name': 'pre_refund', 'type': 'decimal', 'description': '有效发货前成功退款'},
        {'name': 'post_refund', 'type': 'decimal', 'description': '有效发货后成功退款'}]}


@pytest.mark.parametrize('prompt', ['汇总退款金额', '退款率从高到低', '统计有效订单退款金额'])
def test_refund_ambiguity_guard(refunds, prompt):
    result = query_drafts.refund_clarification(refunds, prompt)
    assert result['status'] == 'needs_clarification' and result['definition'] is None
    assert 'all_refund' in result['questions'][0] and '有效成功退款' in result['questions'][0]


@pytest.mark.parametrize('prompt', ['退款金额采用all_refund', '退款金额采用全部退款', '退款率用有效成功退款',
                                    '汇总发货前退款金额', '退款金额采用有效发货后成功退款', '汇总支付金额'])
def test_explicit_refund_wording_not_blocked(refunds, prompt):
    assert query_drafts.refund_clarification(refunds, prompt) is None


def test_ambiguity_before_any_provider_call(service, monkeypatch):
    with db.connect(service[0]) as connection:
        dataset = connection.execute("SELECT definition FROM datasets WHERE slug='sales'").fetchone()['definition']
    fields = copy.deepcopy(dataset['fields'])
    fields.append({'name': 'extra_refund', 'type': 'decimal', 'description': '其他退款'})
    monkeypatch.setattr(query_drafts, '_metadata', lambda _dataset: {'fields': fields})
    monkeypatch.setattr(llm_client, 'complete', lambda *_args: pytest.fail('歧义提示不应请求模型'))
    result = generate(service)
    assert result.status_code == 200 and result.json()['data']['status'] == 'needs_clarification'


def test_prompt_boundaries():
    assert DraftRequest(dataset_slug='sales', prompt='\n按店铺汇总\t金额\n').prompt == '按店铺汇总\t金额'

