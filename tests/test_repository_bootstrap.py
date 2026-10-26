"""在临时目录验证首次初始化及 Git 排除规则；不连接数据库或外部服务。"""

import base64
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def project(tmp_path):
    (tmp_path / "scripts").mkdir()
    shutil.copyfile(ROOT / "scripts/configure.py", tmp_path / "scripts/configure.py")
    shutil.copyfile(ROOT / ".env.example", tmp_path / ".env.example")
    return tmp_path


def initialize(project):
    return subprocess.run([sys.executable, "scripts/configure.py"], cwd=project,
                          capture_output=True, check=True)


def test_fresh_clone_creates_empty_config_and_valid_key(project):
    initialize(project)
    assert json.loads((project / "config/datasets.json").read_text()) == []
    assert json.loads((project / "config/sources.json").read_text()) == {"sources": []}
    values = dict(line.split("=", 1) for line in (project / ".env").read_text().splitlines()
                  if "=" in line and not line.startswith("#"))
    assert len(base64.urlsafe_b64decode(values["DS_CONFIG_ENCRYPTION_KEY"])) == 32
    assert values["DS_ADMIN_PASSWORD"] and values["POSTGRES_PASSWORD"]


def test_repeated_initialization_preserves_credentials_and_runtime_config(project):
    initialize(project)
    (project / "config/sources.json").write_text('{"sources":[{"dataset_slug":"existing"}]}')
    paths = [project / name for name in (".env", ".local/admin-credentials.txt", "config/sources.json", "config/datasets.json")]
    original = {path: path.read_bytes() for path in paths}
    initialize(project)
    assert all(path.read_bytes() == original[path] for path in paths)


def test_existing_environment_still_creates_missing_configuration(project):
    (project / ".env").write_text("DS_CONFIG_ENCRYPTION_KEY=synthetic-existing-key\n")
    initialize(project)
    assert (project / ".env").read_text() == "DS_CONFIG_ENCRYPTION_KEY=synthetic-existing-key\n"
    assert json.loads((project / "config/sources.json").read_text()) == {"sources": []}
    assert not (project / ".local").exists()


def test_git_ignores_runtime_material_but_keeps_source_and_templates(tmp_path):
    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True, capture_output=True)
    shutil.copyfile(ROOT / ".gitignore", tmp_path / ".gitignore")
    ignored = [".env", ".env.production", ".local/key.json", "config/datasets.json", "config/sources.json",
               "config/product_day.mapping.json", "evidence/report.json", "tests/review/screen.png",
               "tests/review/pytest-output.txt", "backups/database.dump", "docs/部署文档.docx"]
    kept = [".env.example", "data_service/query.py", "config/sources.example.json",
            "config/product_day.dataset.json", "evidence/.gitkeep", "tests/test_package_source.py"]
    for name, expected in [(name, 0) for name in ignored] + [(name, 1) for name in kept]:
        result = subprocess.run(["git", "check-ignore", "--quiet", name], cwd=tmp_path, capture_output=True)
        assert result.returncode == expected, name
