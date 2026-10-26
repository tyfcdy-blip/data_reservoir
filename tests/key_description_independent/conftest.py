"""主键与说明独立验收：仅操作专用隔离库，源接口由 HTTP 边界替身提供。"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from data_service import db
from tests.key_description_independent.environment import isolated_configuration


@pytest.fixture
def isolated_settings(tmp_path):
    settings = isolated_configuration()
    source = tmp_path / "sources.json"
    source.write_text('{"sources":[]}', encoding="utf-8")
    settings = settings.model_copy(update={
        "source_config_path": str(source), "allowed_source_hosts": ["source.test"],
        "schedule_enabled": False,
    })
    with db.connect(settings) as connection:
        connection.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
    db.migrate(settings)
    return settings


@pytest.fixture
def business_day():
    return datetime.now(ZoneInfo("Asia/Shanghai")).date() - timedelta(days=1)
