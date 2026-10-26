"""源码包覆盖真实白名单、运行信息排除、空配置与凭据泄露拒绝边界。"""

import importlib.util
import json
from pathlib import Path
import zipfile

import pytest

spec = importlib.util.spec_from_file_location("package_source", Path(__file__).resolve().parents[1] / "scripts/package-source.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def write(root, name, text):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


@pytest.fixture
def project(tmp_path):
    write(tmp_path, "pyproject.toml", '[project]\nversion = "1.6.0"\n')
    write(tmp_path, "data_service/app.py", "VALUE = 1\n")
    write(tmp_path, "tests/test_sample.py", "def test_sample():\n    assert True\n")
    write(tmp_path, "config/source.example.json", '{"url":"https://source.invalid"}')
    return tmp_path


def archive_contents(root):
    with zipfile.ZipFile(root / "dist/muyaju-data-service-1.6.0-source.zip") as archive:
        assert archive.testzip() is None
        return {name.removeprefix("muyaju-data-service/"): archive.read(name) for name in archive.namelist()}


def test_fresh_package_creates_directories_and_contains_code(project):
    report = module.build_package(project)
    contents = archive_contents(project)
    assert contents["data_service/app.py"] == b"VALUE = 1\n"
    assert "tests/test_sample.py" in contents and "config/source.example.json" in contents
    assert report["zip_integrity"] == "passed" and report["members"] == len(contents)
    assert json.loads((project / "evidence/source-package.json").read_text())["sha256"] == report["sha256"]


@pytest.mark.parametrize("name", [
    ".env", ".local/consumer-key.json", "config/source.json", "evidence/report.json",
    "tests/check/browser-result.json", "tests/check/output.log", "tests/check/result.xml",
    "tests/check/screen.png", "docs/部署文档.docx", "backups/database.dump",
    "deploy/cloud/.env", "deploy/cloud/config/sources.json", "deploy/cloud/final.dump",
    "deploy/cloud/nginx-backup.conf",
])
def test_runtime_files_and_generated_results_are_excluded(project, name):
    write(project, name, "private runtime data")
    module.build_package(project)
    assert name not in archive_contents(project)


def test_runtime_configuration_replaced_in_archive_only(project):
    write(project, "config/datasets.json", '[{"name":"real_dataset"}]')
    write(project, "config/sources.json", '{"sources":[{"url":"https://private.invalid"}]}')
    original = (project / "config/sources.json").read_bytes()
    module.build_package(project)
    contents = archive_contents(project)
    assert json.loads(contents["config/datasets.json"]) == []
    assert json.loads(contents["config/sources.json"]) == {"sources": []}
    assert (project / "config/sources.json").read_bytes() == original


@pytest.mark.parametrize("ending", [b"\n", b"\r\n"])
def test_source_bytes_are_preserved_for_both_line_endings(project, ending):
    original = b"VALUE = 1" + ending
    (project / "data_service/app.py").write_bytes(original)
    module.build_package(project)
    assert archive_contents(project)["data_service/app.py"] == original


def test_known_secret_in_source_refused_before_archive_is_created(project):
    secret = "synthetic-secret-for-package-validation"
    write(project, ".env", "DS_ADMIN_PASSWORD=" + secret)
    write(project, "data_service/leaked.py", "PASSWORD = " + repr(secret))
    with pytest.raises(ValueError, match="data_service/leaked.py") as failure:
        module.build_package(project)
    assert secret not in str(failure.value)
    assert not (project / "dist").exists()


def test_cloud_templates_included_but_known_cloud_secret_rejected(project):
    for name in module.DEPLOY_FILES:
        write(project, name, "# empty template\n")
    module.build_package(project)
    assert all(name in archive_contents(project) for name in module.DEPLOY_FILES)
    secret = "synthetic-cloud-only-secret-for-validation"
    write(project, "deploy/cloud/.env", "POSTGRES_PASSWORD=" + secret)
    write(project, "deploy/cloud/compose.yml", "password: " + secret)
    with pytest.raises(ValueError, match="deploy/cloud/compose.yml") as failure:
        module.build_package(project)
    assert secret not in str(failure.value)


@pytest.mark.parametrize("version", ["../../outside", "", "1.6.0/evil"])
def test_unsafe_version_is_rejected(project, version):
    write(project, "pyproject.toml", '[project]\nversion = "' + version + '"\n')
    with pytest.raises(ValueError, match="版本号"):
        module.build_package(project)
