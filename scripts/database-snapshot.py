"""二进制备份和只创建新库的恢复；不会覆盖或自动切换应用库。"""

import argparse
from datetime import datetime, timezone
from pathlib import Path
import re
import subprocess

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description="独立数据服务备份恢复")
    parser.add_argument("--test-environment", action="store_true", help="仅使用专用E2E容器项目")
    sub = parser.add_subparsers(dest="operation", required=True)
    backup = sub.add_parser("backup")
    backup.add_argument("--output", type=Path)
    restore = sub.add_parser("restore")
    restore.add_argument("--input", type=Path, required=True)
    restore.add_argument("--database", required=True)
    arguments = parser.parse_args()
    if arguments.test_environment:
        prefix = ["docker", "compose", "--env-file", ".local/e2e.env", "-f", "tests/e2e/compose.yml"]
        username, database = "data_service", "standalone_e2e_test"
    else:
        prefix = ["docker", "compose"]
        config = dotenv_values(ROOT / ".env")
        username, database = config.get("POSTGRES_USER"), config.get("POSTGRES_DB")
        if not username or not database:
            print("本地配置缺少数据库身份。")
            return 2
    command = prefix + ["exec", "-T", "db"]
    if arguments.operation == "backup":
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        destination = arguments.output or ROOT / ".local/backups" / f"data-service-{stamp}.dump"
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            with destination.open("xb") as handle:
                process = subprocess.run(command + ["pg_dump", "-U", username, "-d", database, "-Fc", "--no-owner", "--no-privileges"],
                                         cwd=ROOT, stdout=handle, stderr=subprocess.PIPE, check=False)
        except FileExistsError:
            print("目标文件已存在，拒绝覆盖。")
            return 2
        if process.returncode:
            print("备份失败，已生成的文件不能用于恢复。")
            return 3
        print("备份成功：" + str(destination.resolve()))
        return 0
    if not re.fullmatch(r"ds_restore_[a-z0-9_]{1,40}", arguments.database) or not arguments.input.is_file():
        print("恢复必须提供已有备份，库名使用ds_restore_加小写字母、数字、下划线。")
        return 2
    # createdb自身原子拒绝已有库；不做DROP、不覆盖既有数据，也不拼接用户SQL。
    created = subprocess.run(command + ["createdb", "-U", username, arguments.database], cwd=ROOT,
                             stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, check=False)
    if created.returncode:
        print("无法创建独立恢复库；已有库拒绝覆盖，请使用新名称并检查权限。")
        return 2
    with arguments.input.open("rb") as handle:
        process = subprocess.run(command + ["pg_restore", "-U", username, "-d", arguments.database,
                                           "--exit-on-error", "--no-owner", "--no-privileges"],
                                 cwd=ROOT, stdin=handle, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, check=False)
    if process.returncode:
        print("恢复失败，独立恢复库保留供排查，未切换应用。")
        return 3
    print("恢复成功：" + arguments.database + "，未切换应用连接。")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except OSError:
        print("文件或Docker操作失败，请检查本机环境。")
        raise SystemExit(3) from None
