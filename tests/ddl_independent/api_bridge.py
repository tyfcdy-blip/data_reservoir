"""D4浏览器API桥：真实FastAPI与隔离PG，规避宿主回环网络限制。"""

import json
from pathlib import Path
import sys
from urllib.parse import urlsplit
from fastapi.testclient import TestClient
from data_service import auth, db
from data_service.api import create_app
from data_service.config import Settings

root = Path(__file__).resolve().parents[2]
settings = Settings(_env_file=root / ".local/test-admin-independent.env")
assert urlsplit(settings.database_url).path == "/admin_independent_test", "拒绝非隔离库"
settings = settings.model_copy(
    update={
        "database_url": settings.database_url.replace("127.0.0.1:15433", "db:5432"),
        "source_config_path": "/tmp/ddl-independent-empty.json",
        "schedule_enabled": False,
        "admin_username": "ddl_independent",
        "admin_password": "ddl-independent-local-only-2026!",
        "allowed_source_hosts": [],
    }
)
Path(settings.source_config_path).write_text('{"sources":[]}', encoding="utf-8")
with db.connect(settings) as connection:
    connection.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
db.migrate(settings)
auth.initialize_admin(settings)
with TestClient(create_app(settings), raise_server_exceptions=False) as client:
    print(json.dumps({"ready": True}), flush=True)
    for line in sys.stdin:
        request = json.loads(line)
        assert request["path"].startswith("/api/"), "只允许已知管理API"
        response = client.request(
            request["method"], request["path"], headers=request.get("headers", {}), json=request.get("body")
        )
        print(
            json.dumps(
                {
                    "status": response.status_code,
                    "body": response.text,
                    "headers": {"content-type": response.headers["content-type"]},
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
