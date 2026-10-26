"""独立用例只连接明确命名的隔离库；不会读取旧项目配置。"""

from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest

from data_service.auth import initialize_admin
from data_service.config import Settings
from data_service.db import migrate


@pytest.fixture(scope="session")
def independent_settings():
    configuration = Path(__file__).resolve().parents[2] / ".local" / "test-independent.env"
    settings = Settings(_env_file=configuration, _env_file_encoding="utf-8")
    database_name = urlsplit(settings.database_url).path.lstrip("/")
    assert database_name == "standalone_independent_test", "拒绝对非独立测试库执行用例"
    migrate(settings)
    initialize_admin(settings)
    return settings


@pytest.fixture
def day():
    return datetime.now(ZoneInfo("Asia/Shanghai")).date() - timedelta(days=1)


@pytest.fixture
def dataset_definitions():
    suffix = uuid4().hex[:12]
    return [
        {
            "slug": "ind_sales_" + suffix,
            "name": "独立测试：商品金额",
            "date_field": "dt",
            "key_fields": ["product_id"],
            "fields": [
                {"name": "dt", "type": "date", "required": True},
                {"name": "product_id", "type": "string", "required": True, "max_length": 80},
                {"name": "shop", "type": "string", "dimension": True, "max_length": 120},
                {"name": "amount", "type": "decimal"},
                {"name": "units", "type": "integer"},
            ],
        },
        {
            "slug": "ind_sensor_" + suffix,
            "name": "独立测试：设备采样",
            "date_field": "sample_day",
            "key_fields": ["device", "sequence"],
            "fields": [
                {"name": "sample_day", "type": "date", "required": True},
                {"name": "device", "type": "string", "required": True, "dimension": True},
                {"name": "sequence", "type": "integer", "required": True},
                {"name": "captured_at", "type": "datetime"},
                {"name": "ok", "type": "boolean", "dimension": True},
                {"name": "temperature", "type": "decimal"},
            ],
        },
    ]
