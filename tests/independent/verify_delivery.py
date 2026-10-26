"""独立复核归档、最终镜像中的代码摘要和运行服务，不接触业务配置秘密。"""

import hashlib
import json
from pathlib import Path
import subprocess
import tarfile

import httpx


ROOT = Path(__file__).resolve().parents[2]


def main():
    manifest = json.loads((ROOT / "evidence/delivery-manifest.json").read_text(encoding="utf-8-sig"))
    final = json.loads((ROOT / "evidence/independent/final-summary.json").read_text(encoding="utf-8-sig"))
    differences = []
    for name, digest in manifest["source_sha256"].items():
        current = hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
        tested = final["product_after"].get(name.replace("/", "\\"), final["product_after"].get(name))
        if current != digest or tested != digest:
            differences.append(name)
    archive = ROOT / manifest["archive"]["path"]
    with archive.open("rb") as stream:
        archive_sha = hashlib.file_digest(stream, "sha256").hexdigest()
    with tarfile.open(archive) as package:
        entries = json.load(package.extractfile("manifest.json"))
        tags = sorted(tag for entry in entries for tag in entry.get("RepoTags", []))
    code = """import hashlib,json,os
from pathlib import Path
root=Path('/app')
files=[*root.joinpath('data_service').glob('*.py'),*root.joinpath('data_service').glob('*.sql')]
print(json.dumps({'uid':os.getuid(),'paths':[name for name in ['.env','.local','tests'] if (root/name).exists()],
'sha256':{str(path.relative_to(root)):hashlib.sha256(path.read_bytes()).hexdigest() for path in files}}))
"""
    inspected = subprocess.run(["docker", "run", "--rm", "--network", "none", "--entrypoint", "python",
                                manifest["image"]["tag"], "-c", code], cwd=ROOT, capture_output=True, text=True, check=True)
    image = json.loads(inspected.stdout)
    image_differences = [name for name, digest in image["sha256"].items() if manifest["source_sha256"].get(name) != digest]
    with httpx.Client(base_url="http://127.0.0.1:8090", timeout=10, trust_env=False) as client:
        statuses = {path: client.get(path).status_code for path in ["/health/ready", "/api/v1/datasets", "/api/business/dashboard"]}
        standalone = client.get("/").json()["data"]["dashboard_included"] is False
    report = {"product_differences_from_tested_candidate": differences, "image_code_differences": image_differences,
              "image_code_files": len(image["sha256"]), "uid": image["uid"], "secret_or_test_paths": image["paths"],
              "archive_hash_matches": archive_sha == manifest["archive"]["sha256"], "archive_sha256": archive_sha,
              "archive_bytes": archive.stat().st_size, "archive_tags": tags, "live_http": statuses,
              "standalone": standalone, "docker_image_inspection_exit": inspected.returncode}
    report["passed"] = (not differences and not image_differences and image["uid"] == 10001 and not image["paths"]
                        and report["archive_hash_matches"] and set(tags) == set(manifest["archive"]["images"])
                        and statuses == {"/health/ready": 200, "/api/v1/datasets": 401, "/api/business/dashboard": 404} and standalone)
    (ROOT / "evidence/independent/delivery-review.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
