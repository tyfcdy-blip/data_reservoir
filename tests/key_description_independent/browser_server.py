"""独立浏览器检查入口：专用空库、合成账号，不启动 worker 或请求上游。"""

import json
from pathlib import Path

import uvicorn

from data_service import auth, db
from data_service.api import create_app
from data_service.datasets import register_dataset
from data_service.models import DatasetDefinition
from tests.key_description_independent.environment import isolated_configuration


def build_app():
    root = Path(__file__).resolve().parents[2]
    settings = isolated_configuration()
    settings = settings.model_copy(update={
        "admin_username": "key_review", "admin_password": "independent-description-check-2026!",
        "allowed_source_hosts": [], "schedule_enabled": False,
        "source_config_path": str(root / "config/sources.json"),
    })
    with db.connect(settings) as connection:
        connection.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
    db.migrate(settings)
    auth.initialize_admin(settings)
    payload = json.loads((root / "config/product_day.dataset.json").read_text(encoding="utf-8"))
    payload["slug"] = "ind_browser_product"
    for field in payload["fields"]:
        if field["name"] == "product_name":
            field["description"] = '<img src=x onerror="window.__descriptionXss=1">商品名称'
    register_dataset(settings, DatasetDefinition.model_validate(payload))
    return create_app(settings)


if __name__ == "__main__":
    import os
    bind = "0.0.0.0" if os.environ.get("KEY_REVIEW_CONTAINER") == "1" else "127.0.0.1"
    uvicorn.run(build_app(), host=bind, port=18994, access_log=False, log_level="warning")
