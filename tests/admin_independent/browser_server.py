"""只用于独立浏览器验收的本机空库、合成来源与真实服务进程。"""

from contextlib import asynccontextmanager
from pathlib import Path
import threading
from urllib.parse import urlsplit

from fastapi import Request
import uvicorn

from data_service import auth, db
from data_service.api import create_app
from data_service.config import Settings
from data_service.worker import run_worker


def build_test_app():
    root = Path(__file__).resolve().parents[2]
    settings = Settings(_env_file=root / ".local" / "test-admin-independent.env")
    assert urlsplit(settings.database_url).path == "/admin_independent_test", "拒绝浏览器测试使用非隔离库"
    source_path = root / "evidence" / "admin-independent" / "browser-sources.json"
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_text('{"sources":[]}', encoding="utf-8")
    settings = settings.model_copy(update={"admin_username": "independent_admin",
        "admin_password": "independent-admin-local-test-only-2026!", "source_config_path": str(source_path),
        "allowed_source_hosts": [], "schedule_enabled": False})
    with db.connect(settings) as connection:
        connection.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
    db.migrate(settings)
    auth.initialize_admin(settings)
    app = create_app(settings)

    @asynccontextmanager
    async def lifespan(_app):
        stop = threading.Event()
        worker = threading.Thread(target=run_worker, args=(settings,), kwargs={"stop": stop}, daemon=True)
        worker.start()
        yield
        stop.set()
        worker.join(timeout=15)

    app.router.lifespan_context = lifespan

    @app.post("/synthetic-source")
    async def synthetic_source(request: Request):
        payload = await request.json()
        return {"data": [] if payload["pageIndex"] else [
            {"dt": payload["dt"], "id": "0001", "shop": "独立验收店铺", "amount": "12.500001"},
            {"dt": payload["dt"], "id": "0002", "shop": "独立验收店铺", "amount": "7.499999"},
        ]}

    return app


if __name__ == "__main__":
    uvicorn.run(build_test_app(), host="127.0.0.1", port=18992, access_log=False, log_level="warning")
