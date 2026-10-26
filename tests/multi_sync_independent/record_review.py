"""聚合独立实测证据，保留首轮缺陷及复验，不把重跑重复计数。"""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import xml.etree.ElementTree as ET

root = Path(__file__).resolve().parents[2]
folder = Path(__file__).parent
cases = {}
runs = []
for name in ['pytest-initial-result.xml', 'pytest-result.xml']:
    document = ET.parse(folder / name)
    raw = []
    for case in document.iter('testcase'):
        key = case.attrib['classname'] + '::' + case.attrib['name']
        status = '失败' if case.find('failure') is not None or case.find('error') is not None else '跳过' if case.find('skipped') is not None else '通过'
        row = {'用例': key, '结果': status, '耗时秒': float(case.attrib.get('time', '0')), '证据': 'tests/multi_sync_independent/' + name}
        raw.append(row)
        cases[key] = row
    runs.append({'证据': name, '通过': sum(case['结果'] == '通过' for case in raw),
                 '失败': sum(case['结果'] == '失败' for case in raw), '跳过': sum(case['结果'] == '跳过' for case in raw)})
assert len(cases) == 41 and all(case['结果'] == '通过' for case in cases.values())
reviewed = [
    'data_service/ingest.py', 'data_service/db.py', 'data_service/migration_3.sql',
    'data_service/source_registry.py', 'data_service/worker.py', 'data_service/source.py',
    'data_service/static/sources.js', 'data_service/static/styles.css',
]
report = {
    '生成时间': datetime.now(timezone.utc).isoformat(),
    '独立性与边界': {
        '执行者': 'multi_sync_independent 独立测试与审查代理',
        '实现修改': False,
        '写入范围': ['tests/multi_sync_independent/', 'evidence/multi-sync-review.json'],
        '数据库': '仅 admin_independent_test（Docker 网络 db:5432）',
        '第三方调用': '无；上游仅分页/异常边界替身，核心校验、事务和SQL均真实执行',
        '临时服务': '127.0.0.1:18995，验收后停止并删除，不启动worker',
        '测试数据': '可重复生成的合成行与合成管理员；每个数据库用例结束后重建空schema，最后无业务数据',
    },
    '任务闭环': {
        '模式': '独立审查', '等级': 'L3：数据库迁移、按主键合并、定时任务',
        '基准': '主代理本轮工作树；无Git仓库，按文件SHA256记录审查对象',
        '主线': [
            {'要求': '每日1至24个分钟时点', '实现': 'source_registry/worker/sources.js', '用例': 'test_schedule.py + browser.cjs', '结果': '通过'},
            {'要求': '同键整行覆盖含NULL、新增键、未返回保留、日期隔离', '实现': 'ingest._retain_missing_rows/commit_batch', '用例': 'test_merge.py + test_scale.py', '结果': '通过'},
            {'要求': '失败整批不发布、原子可见、同批并发幂等', '实现': 'ingest.commit_batch/worker.process_job', '用例': 'test_merge.py', '结果': '通过'},
            {'要求': 'schema2无损升级与失败回滚', '实现': 'db.migrate/migration_3.sql', '用例': 'test_migration.py', '结果': '通过'},
            {'要求': '模式入队冻结、旧小时/旧日级键/文件来源/push兼容', '实现': 'source_registry/worker/ingest', '用例': 'test_schedule.py + test_merge.py', '结果': '通过'},
        ],
        '审查SHA256': {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in reviewed},
    },
    '测试证据': {
        '命令': [
            {'内容': 'docker run --rm --network muyaju-data-service_default -v "D:/codex/慕雅居数据服务:/work" -w /work -e PYTHONPATH=/work -e MULTI_SYNC_REVIEW_CONTAINER=1 muyaju-data-service:key-tests python tests/multi_sync_independent/run_safe.py', '退出码': 1, '结果': '首轮39通过、2失败：显式null缺口'},
            {'内容': 'docker run --rm --network muyaju-data-service_default -v "D:/codex/慕雅居数据服务:/work" -w /work -e PYTHONPATH=/work -e MULTI_SYNC_REVIEW_CONTAINER=1 muyaju-data-service:key-tests python tests/multi_sync_independent/run_safe.py tests/multi_sync_independent/test_schedule.py', '退出码': 0, '结果': '修复后调度/契约26项通过'},
            {'内容': 'node tests/multi_sync_independent/browser.cjs', '退出码': 0, '结果': '真实Edge + API + PostgreSQL 8项通过'},
            {'内容': '.venv/Scripts/python.exe -m ruff check tests/multi_sync_independent', '退出码': 0, '结果': '测试代码静态检查通过'},
        ],
        'Python运行记录': runs, 'Python唯一用例数': 41, '最终通过': 41, '最终失败': 0, '最终跳过': 0,
        '逐用例': list(cases.values()),
        '浏览器': json.loads((folder / 'browser-result.json').read_text(encoding='utf-8')),
        '规模样本': json.loads((folder / 'scale-result.json').read_text(encoding='utf-8')),
        '画面检查': '已查看 narrow-schedule.png；390px宽度下三个时点、删除/添加与保存按钮均可见，无横向溢出',
        '日志安全': 'stdout/stderr/XML在落盘前按已知测试配置脱敏，短回溯；没有读取或输出真实来源凭据',
    },
    '问题风险': {
        '已修复': [
            {'编号': 'MS-IND-01', '级别': 'P2', '问题': '显式 schedule_times:null 被当作省略接受', '修复者': '调度后端代理', '复验': 'test_invalid_schedule_times_rejected[None]通过，保留原失败日志'},
            {'编号': 'MS-IND-02', '级别': 'P2', '问题': '显式 write_mode:null 被当作省略接受', '修复者': '调度后端代理', '复验': 'test_invalid_write_mode_rejected[None]通过，保留原失败日志'},
        ],
        '未关闭P0P1': [],
        '未验证与限制': [
            '不独立部署或修改业务库；本地正式容器升级、32107条存量保留、重启与恢复由主代理另行实测。',
            '规模样本仅4列、6万发布行，不代表32列商品表、180天历史或生产硬件容量/吞吐保证。',
            '未进行真实供应商请求、长时间运行或跨午夜持续调度观测。',
            'Windows主机直连数据库测试路径未使用；容器真实PostgreSQL路径通过。',
        ],
    },
    '质量状态': {
        'Codex开发完成': '由主代理整合确认；独立代理不修改实现',
        '测试计划用例已生成': True, '自动化测试已执行': True,
        '人工验证已执行': False, '独立复核通过': True,
        'CI通过': '未执行', '开发最终确认': '未获得人工确认', '人工代码评审通过': '未执行',
        '允许发布': '本报告不授予发布审批；本地升级门禁由主代理结合运行证据判断',
        'AI自检结论': '有条件通过：独立专项无未关闭P0/P1；本地交付运行证据由主代理补齐',
    },
    '责任边界': 'Codex自检通过不等于开发自测确认、人工代码评审、CI验证或发布审批通过。',
}
(root / 'evidence/multi-sync-review.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps({'unique_python_passed': len(cases), 'browser_passed': report['测试证据']['浏览器']['passed'], 'open_P0_P1': 0}, ensure_ascii=False))
