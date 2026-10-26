"""隔离测试的双运行环境；容器模式只替换测试库网络地址。"""

import os
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from data_service.config import Settings


def isolated_configuration():
    root = Path(__file__).resolve().parents[2]
    settings = Settings(_env_file=root / ".local" / "test-admin-independent.env")
    parsed = urlsplit(settings.database_url)
    assert parsed.path == "/admin_independent_test", "拒绝连接非隔离数据库"
    if os.environ.get("KEY_REVIEW_CONTAINER") == "1":
        assert parsed.hostname == "127.0.0.1" and parsed.port == 15433
        userinfo = parsed.netloc.rsplit("@", 1)[0]
        settings = settings.model_copy(update={
            "database_url": urlunsplit(parsed._replace(netloc=userinfo + "@db:5432"))
        })
    return settings
