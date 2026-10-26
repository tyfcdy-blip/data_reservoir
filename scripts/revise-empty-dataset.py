"""需先备份并停止API/worker；只支持受控空表定义修订，不删除表或批次历史。"""

import argparse
from pathlib import Path
import sys

import psycopg
from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    sys.path.insert(0, str(ROOT))
    from data_service.config import Settings
    from data_service.dataset_revision import revise_empty_dataset
    from data_service.errors import ServiceError
    from data_service.models import DatasetDefinition

    parser = argparse.ArgumentParser(description="备份后修订无数据的表键及字段说明")
    parser.add_argument("--expected", type=Path, required=True, help="修订前表定义JSON")
    parser.add_argument("--revised", type=Path, required=True, help="修订后表定义JSON")
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    args = parser.parse_args()
    try:
        previous = DatasetDefinition.model_validate_json(args.expected.read_text(encoding="utf-8"))
        revised = DatasetDefinition.model_validate_json(args.revised.read_text(encoding="utf-8"))
        revise_empty_dataset(Settings(_env_file=args.env_file), previous, revised)
        print("空表定义修订完成；历史任务保留。")
        return 0
    except ServiceError as error:
        print("修订被拒绝：" + error.code + "，" + error.message)
    except (OSError, ValidationError, psycopg.Error):
        print("修订失败，请检查配置、数据库连接和维护状态；本次事务未提交。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
