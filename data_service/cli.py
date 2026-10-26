"""显式初始化、增量注册配置和管理员密码重置。"""

import argparse
import json
from pathlib import Path

from pydantic import ValidationError
import psycopg

from data_service import auth, datasets, db
from data_service.config import get_settings
from data_service.errors import ServiceError
from data_service.models import DatasetDefinition


def main() -> int:
    parser = argparse.ArgumentParser(description="慕雅居独立数据服务")
    parser.add_argument("command", choices=["init", "register-config", "reset-password"])
    command = parser.parse_args().command
    try:
        settings = get_settings()
        if command == "reset-password":
            auth.reset_password(settings)
        else:
            path = Path(settings.dataset_config_path)
            definitions = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(definitions, list) or len(definitions) > 100:
                raise ValueError("数据集配置必须为不超过100项的列表")
            parsed = [DatasetDefinition.model_validate(value) for value in definitions]
            if command == "init":
                db.migrate(settings)
                auth.initialize_admin(settings)
            for definition in parsed:
                datasets.register_dataset(settings, definition)
        print("操作完成。")
        return 0
    except (ValidationError, ValueError, OSError, psycopg.Error, ServiceError):
        # 启动日志不能回显配置/DSN/密码/源响应。
        print("操作失败，请检查配置格式、数据库状态与字段定义；已有数据未被自动覆盖。")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
