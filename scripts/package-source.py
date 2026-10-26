"""按源码白名单打包；不携带运行配置、凭据、业务数据或历史验收产物。"""

import hashlib
import json
from pathlib import Path
import re
import tomllib
import zipfile

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
ROOT_FILES = (
    "README.md", "Dockerfile", "docker-compose.yml", "pyproject.toml", "uv.lock",
    "requirements.lock", ".env.example", ".gitignore", ".gitattributes", ".dockerignore",
)
CODE_SUFFIXES = {".py", ".sql", ".html", ".css", ".js", ".mjs", ".cjs", ".yml", ".yaml", ".svg"}


def source_files(root: Path) -> list[Path]:
    selected = [root / name for name in ROOT_FILES if (root / name).is_file()]
    for folder in ("data_service", "scripts", "tests"):
        selected.extend(path for path in (root / folder).rglob("*")
                        if path.is_file() and path.suffix in CODE_SUFFIXES
                        and "__pycache__" not in path.parts)
    selected.extend(path for path in (root / "docs").glob("*")
                    if path.is_file() and path.suffix in {".md", ".sql", ".json"})
    selected.extend((root / "config").glob("*.example.json"))
    definition = root / "config/product_day.dataset.json"
    if definition.is_file():
        selected.append(definition)
    for path in selected:
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            raise ValueError("源码白名单不接受链接到外部的文件：" + path.relative_to(root).as_posix())
    return sorted(set(selected))


def local_secrets(root: Path) -> list[bytes]:
    secrets = []
    for env in (root / ".env", root / ".local/e2e.env"):
        if env.exists():
            secrets.extend(value.encode() for key, value in dotenv_values(env).items()
                           if value and len(value) >= 12 and re.search("PASSWORD|SECRET|TOKEN|KEY|URL", key))
    consumer_key = root / ".local/dashboard-consumer-key.json"
    if consumer_key.exists():
        raw_key = json.loads(consumer_key.read_text("utf-8")).get("api_key")
        if isinstance(raw_key, str) and raw_key:
            secrets.append(raw_key.encode())
    return secrets


def build_package(root: Path) -> dict:
    version = tomllib.loads((root / "pyproject.toml").read_text("utf-8"))["project"]["version"]
    if not isinstance(version, str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?", version):
        raise ValueError("项目版本不是安全的版本号")
    secrets = local_secrets(root)
    members = {}
    for path in source_files(root):
        payload = path.read_bytes()
        if any(secret in payload for secret in secrets):
            raise ValueError("候选文件包含本地凭据，拒绝打包：" + path.relative_to(root).as_posix())
        members[path.relative_to(root).as_posix()] = payload
    # 打包始终使用空运行配置，不读取管理员本机的接入设置。
    members["config/datasets.json"] = b"[]\n"
    members["config/sources.json"] = b'{"sources": []}\n'
    members["evidence/.gitkeep"] = b""
    target = root / "dist" / f"muyaju-data-service-{version}-source.zip"
    target.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in sorted(members.items()):
            archive.writestr("muyaju-data-service/" + name, payload)
    with zipfile.ZipFile(target) as archive:
        if archive.testzip() is not None:
            raise ValueError("源码包完整性检查失败")
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    target.with_suffix(".zip.sha256").write_text(digest + "  " + target.name + "\n", encoding="utf-8")
    report = {
        "archive": target.name, "bytes": target.stat().st_size, "members": len(members), "sha256": digest,
        "known_local_credentials_absent": True, "runtime_configuration": "empty",
        "historical_evidence_included": False, "zip_integrity": "passed",
    }
    evidence = root / "evidence"
    evidence.mkdir(exist_ok=True)
    (evidence / "source-package.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def main() -> None:
    print(json.dumps(build_package(ROOT)))


if __name__ == "__main__":
    main()
