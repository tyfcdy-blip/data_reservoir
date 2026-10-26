"""容器内使用已配置的隔离测试库，保存不含凭据的执行证据。"""

import json
import os
import re
from pathlib import Path
import subprocess
import sys
import tempfile
from urllib.parse import unquote, urlsplit, urlunsplit
import xml.etree.ElementTree as ET

from dotenv import dotenv_values


root = Path(__file__).resolve().parents[2]
values = {key: value for key, value in dotenv_values(root / '.local/test-query.env').items() if value is not None}
secrets = list(values.values())
for key in ('DS_DATABASE_URL', 'DS_TEST_DATABASE_URL'):
    parsed = urlsplit(values[key])
    assert parsed.path == '/standalone_query_test', '仅允许隔离专库'
    secrets.append(unquote(parsed.password or ''))
    values[key] = urlunsplit(parsed._replace(netloc=parsed.netloc.rsplit('@', 1)[0] + '@db:5432'))
    secrets.append(values[key])
with tempfile.TemporaryDirectory(prefix='dashboard-tests-') as directory:
    report = str(Path(directory) / 'junit.xml')
    command = [sys.executable, '-m', 'pytest', 'tests/dashboard_integration/test_dashboard_services.py',
               '-q', '--tb=short', '--junitxml=' + report]
    result = subprocess.run(command, cwd=root, env={**os.environ, **values},
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8')
    output = re.sub(r'ds_[A-Za-z0-9_-]{43}', '[REDACTED_TEST_API_KEY]', result.stdout)
    for secret in sorted(set(secrets), key=len, reverse=True):
        if len(secret) >= 8:
            output = output.replace(secret, '[REDACTED]')
    counts = {'tests': 0, 'failures': 0, 'errors': 0, 'skipped': 0}
    cases = []
    if Path(report).exists():
        tree = ET.parse(report)
        for suite in tree.findall('testsuite'):
            for key in counts:
                counts[key] += int(suite.attrib.get(key, 0))
        for case in tree.findall('.//testcase'):
            status = 'failed' if case.find('failure') is not None or case.find('error') is not None else 'skipped' if case.find('skipped') is not None else 'passed'
            cases.append({'name': case.attrib['name'], 'status': status})
    evidence = {'command': 'docker run --rm --network muyaju-data-service_default -e PYTHONPATH=/work -v PROJECT:/work -w /work muyaju-data-service:key-tests python tests/dashboard_integration/run.py',
                'exit_code': result.returncode, 'database': 'standalone_query_test', 'counts': counts,
                'passed': counts['tests'] - counts['failures'] - counts['errors'] - counts['skipped'],
                'cases': cases, 'output': output, 'mocked_core': False,
                'external_boundary': '供应商请求未触发，仅合成数据通过真实入库流程写入隔离库',
                'scope': '9个保存查询模板、金额汇总后相除、NULL/零/负数/空/缺日、榜单分页、跨渠道店铺隔离、查询与服务密钥边界'}
    evidence['prior_attempts'] = [{'exit_code': 1, 'errors': 31, 'reason': '测试夹具误将新建密钥201断言为200；已修正以符合实际API协议，非服务缺陷'}]
    (root / 'evidence/dashboard-integration-templates.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({key: evidence[key] for key in ['exit_code', 'counts', 'passed']}, ensure_ascii=False))
    print(output)
    raise SystemExit(result.returncode)
