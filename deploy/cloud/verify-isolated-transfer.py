"""显式运行的真实恢复演练：临时PostgreSQL、无端口、无持久卷、无真实数据。"""

import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import uuid


def main() -> None:
    bash = ("C:/Program Files/Git/bin/bash.exe" if os.name == "nt" else "/bin/bash")
    project = "ds-cloud-transfer-test-" + uuid.uuid4().hex[:10]
    with tempfile.TemporaryDirectory(prefix=project) as directory:
        work = Path(directory)
        shutil.copyfile(Path(__file__).with_name("database-transfer.sh"), work / "database-transfer.sh")
        (work / ".env").write_text("", encoding="utf-8")
        (work / "compose.yml").write_text(
            f"name: {project}\nservices:\n  db:\n    image: postgres:17-bookworm\n"
            "    environment:\n      POSTGRES_USER: transfer_test\n"
            f"      POSTGRES_PASSWORD: {uuid.uuid4().hex}\n"
            "    tmpfs:\n      - /var/lib/postgresql/data\n"
            "    healthcheck:\n      test: [CMD-SHELL, 'pg_isready -U transfer_test']\n"
            "      interval: 1s\n      timeout: 2s\n      retries: 30\n",
            encoding="utf-8",
        )
        compose = ["docker", "compose", "--env-file", ".env", "-f", "compose.yml"]

        def run(arguments, *, expected=0, input_text=None):
            result = subprocess.run(arguments, cwd=work, input=input_text,
                                    text=True, encoding="utf-8", capture_output=True, check=False)
            if (result.returncode == 0) != (expected == 0):
                raise RuntimeError(f"测试命令退出码不符合预期：{result.returncode}\n{result.stderr}")
            return result.stdout

        def sql(database, statement):
            return run(compose + ["exec", "-T", "db", "psql", "-X", "-v", "ON_ERROR_STOP=1",
                                  "-U", "transfer_test", "-d", database, "-At"], input_text=statement)

        snapshot = work / "snapshot.dump"
        shell_snapshot = snapshot.as_posix()
        if os.name == "nt":
            shell_snapshot = "/" + shell_snapshot[0].lower() + shell_snapshot[2:]
        script = [bash, "database-transfer.sh"]
        try:
            run(compose + ["up", "-d", "--wait", "db"])
            run(compose + ["exec", "-T", "db", "createdb", "-U", "transfer_test", "ds_restore_source"])
            sql("ds_restore_source", "CREATE TABLE transfer_probe(id text PRIMARY KEY, amount numeric(18,2));"
                "INSERT INTO transfer_probe VALUES('001',12.34),('002',56.78);")
            run(script + ["backup", shell_snapshot, "ds_restore_source"])
            original_hash = hashlib.sha256(snapshot.read_bytes()).hexdigest()
            run(script + ["backup", shell_snapshot, "ds_restore_source"], expected=2)
            assert hashlib.sha256(snapshot.read_bytes()).hexdigest() == original_hash
            run(script + ["restore", shell_snapshot, "ds_restore_copy"])
            assert sql("ds_restore_copy", "SELECT count(*),sum(amount) FROM transfer_probe;").strip() == "2|69.12"
            sql("ds_restore_copy", "UPDATE transfer_probe SET amount=99 WHERE id='001';")
            run(script + ["restore", shell_snapshot, "ds_restore_copy"], expected=2)
            assert sql("ds_restore_copy", "SELECT amount FROM transfer_probe WHERE id='001';").strip() == "99.00"
            run(script + ["restore", shell_snapshot, "postgres"], expected=2)
            print("真实隔离恢复通过：备份、拒绝覆盖文件、恢复数值、拒绝覆盖已有库、拒绝保留库名，共5项。")
        finally:
            run(compose + ["down", "--remove-orphans"])


if __name__ == "__main__":
    main()
