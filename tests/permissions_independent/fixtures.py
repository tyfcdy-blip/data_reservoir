"""独立权限验收数据；仅admin_independent_test合成数据，禁止业务库。"""

from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from data_service import auth, db, datasets, ingest, query_services
from data_service.config import Settings
from data_service.models import DatasetDefinition
from data_service.query_services import QueryServiceSave

ROOT = Path(__file__).resolve().parents[2]
USERNAME = "permissions_independent"
PASSWORD = "permissions-independent-test-only-2026!"


def initialize(directory: Path):
    settings = Settings(_env_file=ROOT / ".local/test-admin-independent.env")
    assert urlsplit(settings.database_url).path == "/admin_independent_test", "拒绝非专用隔离库"
    source_file = directory / "empty-sources.json"
    source_file.write_text('{"sources":[]}', encoding="utf-8")
    settings = settings.model_copy(
        update={
            "database_url": settings.database_url.replace("127.0.0.1:15433", "db:5432"),
            "admin_username": USERNAME,
            "admin_password": PASSWORD,
            "source_config_path": str(source_file),
            "schedule_enabled": False,
            "allowed_source_hosts": [],
        }
    )
    with db.connect(settings) as connection:
        connection.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
    db.migrate(settings)
    auth.initialize_admin(settings)
    day2 = datetime.now(ZoneInfo("Asia/Shanghai")).date() - timedelta(days=1)
    day1 = day2 - timedelta(days=1)
    fields = [
        {"name": "id", "type": "string", "required": True, "description": "稳定行标识"},
        {"name": "dt", "type": "date", "required": True, "description": "业务日期"},
        *[
            {"name": name, "type": "string", "dimension": True, "description": label}
            for name, label in [
                ("channel", "渠道"),
                ("shop_name", "店铺"),
                ("owner_person", "负责人"),
                ("category", "类目"),
            ]
        ],
        {"name": "pay_amount", "type": "decimal", "description": "支付金额"},
        {"name": "refund_amount", "type": "decimal", "description": "退款金额"},
        {"name": "private_note", "type": "string", "description": "非维度备注"},
    ]
    for slug in ["sales", "other_sales"]:
        datasets.register_dataset(
            settings,
            DatasetDefinition.model_validate(
                {
                    "slug": slug,
                    "name": "独立销售测试" if slug == "sales" else "未授权销售测试",
                    "date_field": "dt",
                    "key_fields": ["id"],
                    "fields": fields,
                }
            ),
        )
    source_rows = [
        ("001", "C1", "A", "甲", "X", "100", "10"),
        ("002", "C1", "A", "甲", "Y", "300", "90"),
        ("003", "C1", "B", "乙", "X", "200", "180"),
        ("004", "C2", "A", "丙", "X", "900", "810"),
        ("005", "C2", "B", "乙", "Y", "50", "5"),
        ("006", "C1", None, "甲", "X", "700", "600"),
        ("007", "C1", "", "甲", "X", "20", "20"),
        ("008", "C1", "A' OR 1=1 --", "甲", "X", "7", "1"),
        ("009", "C1", "A", "乙", "X", "50", "5"),
    ]
    for slug in ["sales", "other_sales"]:
        for business_day, records in [
            (day1, source_rows),
            (
                day2,
                [
                    ("010", "C1", "A", "甲", "Z", "0", "5"),
                    ("011", "C1", "B", "乙", "X", "1000", "500"),
                ],
            ),
        ]:
            rows = [
                {
                    "id": identifier,
                    "dt": business_day.isoformat(),
                    "channel": channel,
                    "shop_name": shop,
                    "owner_person": owner,
                    "category": category,
                    "pay_amount": pay,
                    "refund_amount": refund,
                    "private_note": "不可经仅服务密钥读取",
                }
                for identifier, channel, shop, owner, category, pay, refund in records
            ]
            batch = ingest.create_batch(
                settings, slug, business_day, len(rows), slug + business_day.isoformat()
            )
            ingest.append_chunk(settings, batch["id"], 0, rows)
            ingest.commit_batch(settings, batch["id"])
    aggregate = {
        "group_by": ["category"],
        "aggregates": [
            {"op": "sum", "field": "pay_amount", "alias": "payments"},
            {"op": "sum", "field": "refund_amount", "alias": "refunds"},
        ],
        "formulas": [{"alias": "refund_rate", "expression": "refunds / payments", "decimal_places": 6}],
        "order_by": [{"field": "refund_rate", "direction": "desc"}],
    }
    for slug, definition, dataset in [
        ("sales_rate", aggregate, "sales"),
        ("sales_rows", {"fields": ["id", "pay_amount"]}, "sales"),
        ("unselected", {"fields": ["private_note"]}, "sales"),
        ("other_service", {"fields": ["private_note"]}, "other_sales"),
        (
            "fixed_b",
            {"fields": ["id"], "filters": [{"field": "shop_name", "op": "eq", "value": "B"}]},
            "sales",
        ),
    ]:
        query_services.save_service(
            settings,
            slug,
            QueryServiceSave.model_validate(
                {
                    "name": "合成接口 " + slug,
                    "dataset_slug": dataset,
                    "definition": definition,
                    "allowed_filter_fields": ["shop_name", "channel", "owner_person"],
                    "max_limit": 20,
                    "enabled": True,
                    "revision": 0,
                }
            ),
        )
    return settings, {"start_date": day1.isoformat(), "end_date": day2.isoformat()}, aggregate
