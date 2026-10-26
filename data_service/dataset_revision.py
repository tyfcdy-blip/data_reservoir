"""仅对无记录且无活动批次的数据集修订业务键及字段说明，拒绝隐式搬迁存量数据。"""

from psycopg import sql
from psycopg.types.json import Jsonb

from data_service.config import Settings
from data_service.datasets import apply_field_comments, get_dataset
from data_service.db import connect
from data_service.errors import ServiceError
from data_service.models import DatasetDefinition


def _check_revision_scope(expected: DatasetDefinition, revised: DatasetDefinition) -> None:
    before = expected.model_dump(mode="json")
    after = revised.model_dump(mode="json")
    for payload in (before, after):
        payload.pop("key_fields")
        payload.pop("fields")
    if before != after or len(expected.fields) != len(revised.fields):
        raise ServiceError(422, "REVISION_SCOPE_INVALID", "此修订只允许更换唯一键、将新键设为必填和修改字段说明")
    for original, replacement in zip(expected.fields, revised.fields, strict=True):
        previous = original.model_dump()
        updated = replacement.model_dump()
        for payload in (previous, updated):
            payload.pop("description")
            payload.pop("required")
        required_change_allowed = (original.required == replacement.required
                                   or (replacement.required and replacement.name in revised.key_fields))
        if previous != updated or not required_change_allowed:
            raise ServiceError(422, "REVISION_SCOPE_INVALID", "禁止通过空表修订增删字段、改类型或放宽必填约束")


def revise_empty_dataset(settings: Settings, expected: DatasetDefinition,
                         revised: DatasetDefinition) -> dict:
    """锁定写入边界、核对旧定义后原子修订；任何异常均回滚DDL与元数据。"""
    expected = DatasetDefinition.model_validate(expected)
    revised = DatasetDefinition.model_validate(revised)
    _check_revision_scope(expected, revised)
    with connect(settings) as connection:
        connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", ("dataset:" + expected.slug,))
        # 维护时应停止API和worker。锁仍防止其他写入者在检查空表之后插入批次或发布数据。
        connection.execute("LOCK TABLE batches, published_days IN SHARE ROW EXCLUSIVE MODE")
        connection.execute("SELECT slug FROM datasets WHERE slug=%s FOR UPDATE", (expected.slug,))
        dataset = get_dataset(connection, expected.slug)
        current = DatasetDefinition.model_validate(dataset["definition"])
        if current == revised:
            return dataset
        if current != expected:
            raise ServiceError(409, "DATASET_REVISION_CONFLICT", "当前表定义已变化，请重新核对修订基准")
        published = connection.execute("SELECT 1 FROM published_days WHERE dataset_slug=%s LIMIT 1",
                                       (expected.slug,)).fetchone()
        active = connection.execute("SELECT 1 FROM batches WHERE dataset_slug=%s "
                                    "AND status IN ('pending','running','uploading') LIMIT 1",
                                    (expected.slug,)).fetchone()
        if published or active:
            raise ServiceError(409, "DATASET_REVISION_BLOCKED", "已有发布数据或进行中的批次，禁止更换表键")
        connection.execute(sql.SQL("LOCK TABLE {} IN ACCESS EXCLUSIVE MODE").format(
            sql.Identifier(dataset["table_name"])))
        if connection.execute(sql.SQL("SELECT 1 FROM {} LIMIT 1").format(
                sql.Identifier(dataset["table_name"]))).fetchone():
            raise ServiceError(409, "DATASET_NOT_EMPTY", "数据表已有记录，必须另行制定数据迁移方案")
        for original, replacement in zip(current.fields, revised.fields, strict=True):
            if not original.required and replacement.required:
                connection.execute(sql.SQL("ALTER TABLE {} ALTER COLUMN {} SET NOT NULL").format(
                    sql.Identifier(dataset["table_name"]), sql.Identifier(replacement.name)))
        apply_field_comments(connection, dataset["table_name"], revised.fields)
        connection.execute("UPDATE datasets SET definition=%s WHERE slug=%s",
                           (Jsonb(revised.model_dump(mode="json")), expected.slug))
        return get_dataset(connection, expected.slug)
