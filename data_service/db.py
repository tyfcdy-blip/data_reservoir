"""独立数据库连接与显式初始化，禁止自动降级。"""

from importlib.resources import files

import psycopg
from psycopg.rows import dict_row

from data_service.config import Settings

SCHEMA_VERSION = 5
MIGRATION_LOCK = 706223001


def connect(settings: Settings) -> psycopg.Connection:
    return psycopg.connect(
        settings.database_url, row_factory=dict_row, connect_timeout=5,
        application_name="muyaju-data-service",
        options=f"-c statement_timeout={settings.statement_timeout_ms} -c lock_timeout=5000",
    )


def migrate(settings: Settings) -> None:
    with connect(settings) as connection:
        connection.execute("SELECT pg_advisory_xact_lock(%s)", (MIGRATION_LOCK,))
        version = 0
        if connection.execute("SELECT to_regclass('schema_versions') AS name").fetchone()["name"]:
            version = connection.execute("SELECT MAX(version) AS version FROM schema_versions").fetchone()["version"]
            if version is not None and version > SCHEMA_VERSION:
                raise RuntimeError("数据库版本高于程序，拒绝降级初始化")
            if version == SCHEMA_VERSION:
                return
        if not version:
            connection.execute(files("data_service").joinpath("schema.sql").read_text(encoding="utf-8"))
            connection.execute("INSERT INTO schema_versions(version) VALUES (1)")
            version = 1
        # 迁移与版本记录在同一事务内完成，失败时保留旧版本和原数据。
        for target in range(version + 1, SCHEMA_VERSION + 1):
            connection.execute(files("data_service").joinpath(f"migration_{target}.sql").read_text(encoding="utf-8"))
            connection.execute("INSERT INTO schema_versions(version) VALUES (%s)", (target,))


def ready(settings: Settings) -> bool:
    with connect(settings) as connection:
        if not connection.execute("SELECT to_regclass('schema_versions') AS name").fetchone()["name"]:
            return False
        version = connection.execute("SELECT MAX(version) AS version FROM schema_versions").fetchone()["version"]
        admin = connection.execute("SELECT id FROM auth_admin WHERE id=1").fetchone()
        return version == SCHEMA_VERSION and admin is not None

