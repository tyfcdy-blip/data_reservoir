"""独立UI传输桥；真实FastAPI/隔离PG；无外部服务网络请求。"""

import json
from pathlib import Path
import sys
from fastapi.testclient import TestClient
from data_service.api import create_app
from fixtures import initialize

settings, period, aggregate = initialize(Path("/tmp"))
with TestClient(create_app(settings), raise_server_exceptions=False) as client:
    print(json.dumps({"ready": True, "period": period}), flush=True)
    for line in sys.stdin:
        request = json.loads(line)
        assert request["path"].startswith("/api/"), "只允许本地API"
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
