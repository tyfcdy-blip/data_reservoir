"""记录本地交付证据，不输出凭据，也不修改业务数据。"""

from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
from pathlib import Path
import subprocess

from dotenv import dotenv_values
import httpx

ROOT = Path(__file__).resolve().parents[1]


def command(arguments):
    result = subprocess.run(arguments, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if result.returncode:
        raise RuntimeError("本地交付核对命令失败")
    return result.stdout.decode("utf-8")


def main():
    config = dotenv_values(ROOT / ".env")
    with httpx.Client(base_url="http://127.0.0.1:8090", timeout=10, trust_env=False) as client:
        home = client.get("/")
        assert home.status_code == 200 and home.json()["data"]["dashboard_included"] is False
        assert client.get("/", headers={"Accept": "text/html"}).headers["location"] == "/admin/"
        page = client.get("/admin/")
        assert page.status_code == 200 and "管理控制台" in page.text
        assert page.headers["X-Frame-Options"] == "DENY"
        assert "script-src 'self'" in page.headers["Content-Security-Policy"]
        for static_file in ("app.js", "core.js", "operations.js", "services.js", "sources.js", "tables.js", "styles.css"):
            assert client.get("/admin/" + static_file).status_code == 200
        assert client.get("/api/v1/datasets").status_code == 401
        assert client.get("/api/business/dashboard").status_code == 404
        login = client.post("/api/v1/auth/login", json={"username": config["DS_ADMIN_USERNAME"], "password": config["DS_ADMIN_PASSWORD"]})
        assert login.status_code == 200
        headers = {"Authorization": "Bearer " + login.json()["data"]["token"]}
        datasets = client.get("/api/v1/datasets", headers=headers).json()["data"]
        status = client.get("/api/v1/admin/status", headers=headers).json()["data"]
        assert datasets == [] and status["configured_sources"] == [] and status["coverage"] == []
        assert client.get("/api/v1/admin/sources", headers=headers).json()["data"] == []
        assert client.get("/api/v1/admin/query-services", headers=headers).json()["data"] == []
        assert any(worker["healthy"] for worker in status["workers"])
        schema = client.get("/openapi.json").json()
        (ROOT / "docs/openapi.json").write_text(json.dumps(schema, ensure_ascii=False, indent=2), encoding="utf-8")
        client.post("/api/v1/auth/logout", headers=headers).raise_for_status()
    image_check = command(["docker", "run", "--rm", "--entrypoint", "python", "muyaju-data-service:1.1.1", "-c",
        "import os,pathlib,json;print(json.dumps({'uid':os.getuid(),'secret_paths_present':[str(p) for p in [pathlib.Path('/app/.env'),pathlib.Path('/app/.local'),pathlib.Path('/app/tests')] if p.exists()]}))"])
    image_status = json.loads(image_check)
    assert image_status == {"uid": 10001, "secret_paths_present": []}
    code_paths = [*(p for p in (ROOT / "data_service").rglob("*") if p.is_file() and p.suffix in {".py", ".sql", ".html", ".js", ".css", ".svg"}), ROOT / "Dockerfile",
                  ROOT / "docker-compose.yml", ROOT / "requirements.lock", ROOT / "uv.lock", ROOT / ".dockerignore"]
    hashes = {path.relative_to(ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest() for path in code_paths}
    archive = ROOT / "dist/muyaju-data-service-1.1.1-linux-amd64.tar"
    with archive.open("rb") as handle:
        archive_hash = hashlib.file_digest(handle, "sha256").hexdigest()
    archive.with_suffix(".tar.sha256").write_text(archive_hash + "  " + archive.name + "\n", encoding="utf-8")
    containers = [json.loads(line) for line in command(["docker", "compose", "ps", "--format", "json"]).splitlines()]
    image_id = command(["docker", "image", "inspect", "muyaju-data-service:1.1.1", "--format", "{{.Id}}"]).strip()
    report = {"recorded_at_utc": datetime.now(timezone.utc).isoformat(), "api_port": 8090, "dashboard_included": False,
              "application_datasets": 0, "application_published_days": 0, "configured_sources": 0, "worker_healthy": True,
              "anonymous_data_http": 401, "dashboard_route_http": 404, "real_admin_login_http": 200,
              "admin_console_http": 200, "admin_assets_http": 200, "browser_root_redirect": "/admin/",
              "containers": [{"service": item["Service"], "state": item["State"], "health": item["Health"]} for item in containers],
              "image": {"tag": "muyaju-data-service:1.1.1", "id": image_id, **image_status},
              "archive": {"path": str(archive.relative_to(ROOT)), "bytes": archive.stat().st_size, "sha256": archive_hash,
                          "images": ["muyaju-data-service:1.1.1", "postgres:17-bookworm"]}, "source_sha256": hashes}
    (ROOT / "evidence/admin-delivery-manifest.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    dependencies = []
    for line in (ROOT / "requirements.lock").read_text(encoding="utf-8").splitlines():
        if "==" in line and not line.lstrip().startswith("#"):
            name = line.split("==")[0]
            distribution = importlib.metadata.distribution(name)
            metadata = distribution.metadata
            dependencies.append({"name": name, "version": distribution.version,
                                 "license_metadata": metadata.get("License-Expression") or metadata.get("License") or "metadata unavailable",
                                 "source": "https://pypi.org/project/" + name + "/" + distribution.version + "/"})
    (ROOT / "evidence/dependencies.json").write_text(json.dumps(dependencies, ensure_ascii=False, indent=2), encoding="utf-8")
    print("交付状态已记录；主应用无业务数据，镜像不含凭据，SHA256已生成。")


if __name__ == "__main__":
    main()
