"""通过管理员HTTP接口安装独立看板查询模板，可选签发专用只读密钥。"""
import argparse
import json
import os
from pathlib import Path
import sys
from uuid import uuid4
from urllib.parse import urlsplit

import httpx
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from data_service.query_services import QueryServiceSave  # noqa: E402


def install(base_url: str, username: str, password: str, key_file: Path | None) -> dict:
    endpoint = urlsplit(base_url)
    if (endpoint.scheme not in {'http', 'https'} or not endpoint.hostname or endpoint.username or endpoint.password
            or endpoint.path not in {'', '/'} or endpoint.query or endpoint.fragment
            or (endpoint.scheme == 'http' and endpoint.hostname not in {'127.0.0.1', 'localhost', '::1'})):
        raise ValueError('远程服务需要HTTPS；地址只填写协议、主机和端口。')
    templates = json.loads((ROOT/'config/dashboard-services.example.json').read_text('utf-8'))
    desired = {row['slug']: QueryServiceSave.model_validate({k:v for k,v in row.items() if k != 'slug'})
               for row in templates}
    if len(desired) != 9:
        raise ValueError('看板模板数量应为9个。')
    if key_file is not None and key_file.exists():
        raise ValueError('密钥文件已存在，禁止覆盖；已安装接口可不带 --key-file 重复检查。')
    created = []
    with httpx.Client(base_url=base_url.rstrip('/'), timeout=60, follow_redirects=False) as client:
        def call(method, path, **kwargs):
            response = client.request(method, path, **kwargs)
            if response.status_code not in {200, 201}:
                raise RuntimeError(f'配置请求失败，HTTP {response.status_code}，接口 {path}；已完成 {len(created)} 个配置，可重新执行续接。')
            payload = response.json()
            if payload.get('code') != 200:
                raise RuntimeError('配置接口返回异常状态。')
            return payload['data']
        session = call('POST', '/api/v1/auth/login', json={'username':username,'password':password})
        client.headers['Authorization'] = 'Bearer '+session['token']
        reserved = False
        key_id = None
        result_summary = None
        try:
            existing = {row['slug']:row for row in call('GET','/api/v1/admin/query-services')}
            # 先完整检查冲突，再保存；绝不自动改写管理员后来调整过的接口。
            for slug, template in desired.items():
                if slug in existing:
                    keys = QueryServiceSave.model_fields
                    current = QueryServiceSave.model_validate({k:v for k,v in existing[slug].items() if k in keys})
                    if current.model_dump(exclude={'revision'}) != template.model_dump(exclude={'revision'}):
                        raise ValueError(f'接口 {slug} 已存在且配置不同，请在管理员页面确认；未覆盖。')
            if key_file is not None:
                key_file.parent.mkdir(parents=True, exist_ok=True)
                descriptor = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                os.close(descriptor)
                reserved = True
            for slug, template in desired.items():
                if slug not in existing:
                    call('PUT','/api/v1/admin/query-services/'+slug,json=template.model_dump(mode='json'))
                    created.append(slug)
            key_metadata = None
            if key_file is not None:
                issuance_name = '独立经营看板本地Demo · ' + uuid4().hex[:12]
                try:
                    result = call('POST','/api/v1/admin/keys',json={
                        'name':issuance_name,'grants':{},'expires_in_days':90,'requests_per_minute':120,
                        'policy':{'services':{slug:'ads_product_dashboard_daily' for slug in desired},
                            'row_scopes':{'ads_product_dashboard_daily':{'mode':'all','groups':[]}}}})
                except (RuntimeError, ValueError, KeyError, httpx.HTTPError) as issuance_error:
                    # 网络异常可能发生在服务端提交之后；仅按本次随机名称查找并撤销，不触碰旧密钥。
                    recovered = []
                    try:
                        for candidate in call('GET', '/api/v1/admin/keys'):
                            if candidate['name'] == issuance_name and candidate.get('revoked_at') is None:
                                call('DELETE', '/api/v1/admin/keys/' + candidate['id'])
                                recovered.append(candidate['id'])
                    except (RuntimeError, ValueError, KeyError, httpx.HTTPError):
                        raise RuntimeError(f'密钥签发结果未确认且自动清理失败，请在调用密钥页面查找并撤销“{issuance_name}”后再执行。') from issuance_error
                    if recovered:
                        raise RuntimeError('密钥签发响应未确认，已找到并撤销本次签发的密钥，可以重新执行。') from issuance_error
                    raise RuntimeError(f'密钥签发结果未确认，请先在调用密钥页面检查“{issuance_name}”，确认没有有效密钥后再执行。') from issuance_error
                key_id = result['id']
                key_metadata = {k:result[k] for k in ['id','name','expires_at','requests_per_minute']}
                key_file.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
                reserved = False
            result_summary = {'created':created,'existing_reused':sorted(set(desired)-set(created)),
                    'services':sorted(desired),'key':key_metadata,'source_requested':False,
                    'scope':'只读已保存接口，全部已接入商品驾驶舱数据；禁止通用表和写入'}
            return result_summary
        except Exception as original_error:
            cleanup_failures = []
            if reserved:
                # 撤销失败不能阻止删除可能含部分密钥的文件，也不能掩盖需要人工撤销的身份。
                if key_id:
                    try:
                        call('DELETE','/api/v1/admin/keys/'+key_id)
                    except (RuntimeError, ValueError, KeyError, httpx.HTTPError):
                        cleanup_failures.append(f'自动撤销失败，请在调用密钥页面撤销密钥 ID {key_id}')
                try:
                    key_file.unlink(missing_ok=True)
                except OSError:
                    cleanup_failures.append('密钥文件清理失败，请检查所指定的本地文件')
            if cleanup_failures:
                raise RuntimeError('安装失败；' + '；'.join(cleanup_failures)) from original_error
            raise
        finally:
            try:
                call('POST','/api/v1/auth/logout')
            except (RuntimeError, ValueError, KeyError, httpx.HTTPError):
                # 保存成功后登出故障不应诱导重复签发；主操作失败时也保留其根因。
                warning = '管理员会话自动登出失败，请在管理页面检查会话；本次接口保存结果不受影响。'
                if result_summary is not None:
                    result_summary['warnings'] = [warning]
                else:
                    print(warning, file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url',required=True)
    parser.add_argument('--key-file',type=Path)
    args = parser.parse_args()
    config = {**dotenv_values(ROOT/'.env'), **os.environ}
    username=config.get('DS_ADMIN_USERNAME','admin')
    password=config.get('DS_ADMIN_PASSWORD')
    if not password:
        parser.error('请通过DS_ADMIN_PASSWORD环境变量或本地.env提供管理员密码。')
    try:
        result=install(args.url,username,password,args.key_file)
    except (ValueError,RuntimeError,httpx.HTTPError,OSError) as error:
        print(str(error),file=sys.stderr)
        raise SystemExit(1) from None
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':
    main()
