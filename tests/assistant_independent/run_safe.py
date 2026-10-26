"""运行独立验收并保存脱敏的真实执行证据。"""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from urllib.parse import urlsplit
from data_service.config import Settings

root = Path(__file__).resolve().parents[2]
configured = Settings(_env_file=root / ".local/test-admin-independent.env")
parsed = urlsplit(configured.database_url)
assert parsed.path == "/admin_independent_test"
assert os.environ.get("ASSISTANT_REVIEW_CONTAINER") == "1"
suffix = os.environ.get("ASSISTANT_REVIEW_RUN", "initial")
assert re.fullmatch(r"[a-z0-9-]+", suffix)
prefix = root / ("evidence/assistant-independent-" + suffix)
command = [sys.executable, "-m", "pytest", *(sys.argv[1:] or ["tests/assistant_independent"]), "-q",
           "--tb=short", "--junitxml=" + str(prefix) + ".xml"]
result = subprocess.run(command, cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace")
secrets = [configured.database_url, parsed.password or "", configured.config_encryption_key or "", configured.admin_password or ""]
def redact(value):
    for secret in sorted((s for s in secrets if s), key=len, reverse=True):
        value = value.replace(secret, "<redacted>")
    return re.sub(r"postgres(?:ql)?://[^\s\x27\x22<>]+", "<redacted-dsn>", value)
output = redact(result.stdout + result.stderr)
Path(str(prefix) + ".log").write_text(output, encoding="utf-8")
xml = Path(str(prefix) + ".xml")
counts = {}
if xml.exists():
    xml.write_text(redact(xml.read_text(encoding="utf-8")), encoding="utf-8")
    suite = ET.parse(xml).getroot().find("testsuite")
    if suite is not None:
        counts = {key: suite.attrib[key] for key in ("tests", "failures", "errors", "skipped", "time")}
Path(str(prefix) + ".json").write_text(json.dumps({
    "command": " ".join(command), "exit_code": result.returncode, "counts": counts,
    "environment": "Python 3.12 / PostgreSQL admin_independent_test / local controlled real HTTP only",
    "boundaries": "No real provider key, no real model semantic accuracy claim, no production data"}, ensure_ascii=False, indent=2), encoding="utf-8")
print(output)
raise SystemExit(result.returncode)

