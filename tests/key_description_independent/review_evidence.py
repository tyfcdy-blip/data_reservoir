"""只读交叉核对主代理实测证据，再更新独立审查结论；不访问数据库或网络。"""

from datetime import datetime
import hashlib
import json
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = ROOT / "evidence"


def read(name):
    return json.loads((EVIDENCE / name).read_text(encoding="utf-8"))


applied = read("product-day-key-applied.json")
live = read("product-day-key-live.json")
ui = read("product-day-key-ui.json")
regression = read("key-regression-summary.json")
diagnosis = read("product-day-key-diagnosis.json")
restart = read("product-day-key-restart.json")
rollback = read("product-day-key-rollback.json")
source_regression = read("key-source-regression.json")
expected = json.loads((ROOT / "config/product_day.dataset.json").read_text(encoding="utf-8"))
mapping = json.loads((ROOT / "config/product_day.mapping.json").read_text(encoding="utf-8"))

assert applied["status"] == live["status"] == ui["status"] == "passed"
assert applied["definition"] == expected
assert expected["key_fields"] == ["product_day_id"] and len(expected["fields"]) == 32
assert applied["source_config"] == mapping and mapping["page_start"] == 1
assert applied["source_revision"] == 2 and applied["schedule_enabled"] is False
assert applied["historical_jobs_unchanged"] and applied["business_rows_at_revision"] == 0
columns = {column["name"]: column for column in applied["postgres_columns"]}
for field in expected["fields"]:
    assert field["description"] and columns[field["name"]]["description"] == field["description"]
    assert columns[field["name"]]["required"] == field["required"]

batch = live["latest_batch"]
assert batch["status"] == "succeeded" and batch["error_code"] is None
assert batch["id"] == live["batch_id"]
assert batch["business_date"] == live["business_date"] == "2026-10-21"
assert batch["row_count"] == live["row_count"] == live["distinct_product_day_id_count"] == 32107
assert batch["page_count"] == (live["row_count"] + mapping["page_size"] - 1) // mapping["page_size"] + 1
assert live["aggregate_amount_matches_database"] and live["sample_records_compared"] == 3
assert live["history_failed_batch_preserved"]
assert datetime.fromisoformat(batch["finished_at"]) > datetime.fromisoformat(batch["started_at"])
assert ui["business_writes"] is False and ui["real_service"] == "http://127.0.0.1:8090"
assert diagnosis["overlap_0_1"] == 200 and diagnosis["overlap_1_2"] == 0

count = 0
for suite in regression:
    assert suite["exit_code"] == suite["failed"] == suite["errors"] == 0
    log = (ROOT / suite["evidence"]).read_text(encoding="utf-8")
    assert re.search(r"\b" + str(suite["passed"]) + r" passed\b", log)
    count += suite["passed"]
assert count == 246
assert restart["status"] == "passed" and restart["api_ready"] and restart["worker_healthy"] and restart["coverage_complete"]
assert restart["key_fields"] == expected["key_fields"] and restart["descriptions"] == len(expected["fields"])
assert restart["source_page_start"] == mapping["page_start"] == 1 and restart["source_revision"] == 2
assert restart["published_row_count"] == live["row_count"] and restart["published_date"] == live["business_date"]
assert restart["schedule_enabled"] is False
assert rollback["status"] == "passed" and rollback["old_image_ready"] and rollback["old_schema_compatible"]
assert rollback["restored_database"] == "ds_restore_product_key_pre"
assert rollback["old_key_fields"] == ["dt", "product_id"] and rollback["old_page_start"] == 0
assert rollback["application_connection_switched"] is False and rollback["read_only"] is True
assert source_regression["passed"] == 62
assert source_regression["failed"] == source_regression["skipped"] == source_regression["exit_code"] == 0

report = read("product-day-key-review.json")
files = ["product-day-key-applied.json", "product-day-key-live.json", "product-day-key-ui.json",
         "product-day-key-schema.png", "key-regression-summary.json", "product-day-key-diagnosis.json",
         "product-day-key-restart.json", "product-day-key-rollback.json", "key-source-regression.json"]
report["real_environment_evidence_cross_review"] = {
    "status": "passed",
    "command": "uv run python -m tests.key_description_independent.review_evidence",
    "boundary": "本代理只读核对主代理产出的原始脚本、证据 JSON、测试日志和真实 UI 截图；不再次触达业务库或第三方来源。",
    "verified": [
        "实际保存的定义与当前配置逐字段相同；32 个 PostgreSQL 列说明及可空性与定义逐项一致",
        "单键 product_day_id 必填，实际来源 page_start=1、revision=2，自动同步仍关闭",
        "变更时无业务记录，原失败任务历史保留",
        "2026-10-21 整日同步 succeeded：32107 行、32107 个唯一 ID，162 页与 200 行分页加终止空页相符",
        "SQL 与 API 支付金额汇总一致；来源首 3 行全部 32 字段通过类型规范化后无损比对",
        "真实 8090 管理页 DOM 测试对 32 行名称、说明、唯一键逐项断言；截图可视范围与证据一致",
        "主代理 246 项回归汇总与各套件日志数量一致，均为退出码 0",
        "原同步重复由来源第 0 和第 1 页重复引起，不能归因为复合键逻辑天然失败",
        "重启后 32107 行、完整日期覆盖、单键、32 说明、起始页 1 和来源版本 2 持久化，worker 健康",
        "readiness 检查已改为等待成功的显式布尔值与循环后断言，复验报告 api_ready=true",
        "旧镜像在独立恢复库 ds_restore_product_key_pre 中读取旧 schema 与页码 0 通过；未切换业务连接",
        "source 62 项为主代理真实命令输出汇总，失败与跳过均为 0；本代理仅核对汇总，不冒充独立执行"
    ],
    "evidence_sha256": {name: hashlib.sha256((EVIDENCE / name).read_bytes()).hexdigest() for name in files},
    "main_agent_regression_passed": 246,
    "source_62_tests": "已读取 key-source-regression.json：主代理命令输出汇总 62 passed / 0 failed / 0 skipped，退出码 0。本代理未独立执行，亦不并入前述 246 项日志计数。"
}
report["real_environment_evidence_cross_review"]["verification_script_sha256"] = {
    name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
    for name in [".local/verify-product-day-key-live.py", ".local/verify-product-day-ui.cjs",
                 ".local/verify-product-key-restart.py", ".local/verify-product-key-restored.py"]
}
report["findings"]["test_evidence_defects"] = [{
    "id": "KEY-REVIEW-P3-01",
    "priority": "P3",
    "location": ".local/verify-product-key-restart.py",
    "problem": "原 readiness 等待循环耗尽后无最终断言，报告固定写 api_ready=true，存在证据误报路径。",
    "fix": "主代理增加 ready_ok=False，HTTP 200 后设 True，循环后 assert ready_ok，报告使用真实布尔值。",
    "retest": "主代理重新执行只读核验，退出码 0；本代理复核更新脚本及成功报告。",
    "retest_command": "docker run --rm --user root --network muyaju-data-service_default -v D:/codex/慕雅居数据服务:/work -w /work -e PYTHONPATH=/work muyaju-data-service:1.1.1 python .local/verify-product-key-restart.py",
    "execution_note": "首次复验未使用 --user root，容器落盘证据遇 PermissionError；改用仅此次测试容器的 root 用户后成功，未改变业务服务权限。该经过由主代理提供，本代理未重复执行。",
    "status": "closed"
}]
report["unverified_by_this_reviewer"] = [
    "主代理执行的真实来源、业务库写入和实际 UI 操作已做只读证据交叉核验，本代理未重复直接执行。",
    "重启与独立备份恢复库兼容性已核对主代理证据及脚本，本代理未重复执行操作；未将当前业务连接切回旧备份。",
    "此专项不代表千万级数据量性能验证；Windows 直连数据库的 10022 环境问题未修复。",
    "人工代码评审、开发自测确认、CI 和生产发布审批均未由本代理执行。"
]
report["conclusion"] = "当前本地 Docker 版本的独立审查通过：28 项隔离专项及 4 项浏览器检查通过；主代理真实部署、32107 行整日同步、32 字段 UI、重启持久化及独立恢复库旧镜像兼容证据交叉核验一致。发现的 1 项 P3 测试证据缺口已修复并复验关闭，无未关闭 P0/P1。Windows 直连测试网络问题及人工/CI/生产审批边界继续披露。"
(EVIDENCE / "product-day-key-review.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(json.dumps({"cross_review": "passed", "real_rows": live["row_count"], "main_regression_logs_passed": count,
                  "fields": len(expected["fields"])}, ensure_ascii=False))
