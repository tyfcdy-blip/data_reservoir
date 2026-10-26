"""捕获测试输出后脱敏，避免第三方库回溯将连接串写入证据。"""

from pathlib import Path
import subprocess
import sys
from urllib.parse import unquote, urlsplit

from data_service.config import Settings


ROOT = Path(__file__).resolve().parents[2]
DIRECTORY = Path(__file__).resolve().parent


def sanitize(text):
    settings = Settings(_env_file=ROOT / ".local" / "test-admin-independent.env")
    values = [settings.database_url, unquote(urlsplit(settings.database_url).password or ""),
              settings.config_encryption_key, settings.admin_password]
    for value in values:
        if value:
            text = text.replace(value, "[REDACTED]")
    return text


if __name__ == "__main__":
    existing = DIRECTORY / "pytest-result.xml"
    if existing.exists():
        existing.write_text(sanitize(existing.read_text(encoding="utf-8")), encoding="utf-8")
    if "--sanitize-only" in sys.argv:
        print("已有测试证据已脱敏")
    else:
        outcome = subprocess.run([sys.executable, "-m", "pytest", "tests/key_description_independent", "-q",
                                  "--tb=short", "--junitxml=tests/key_description_independent/pytest-result.xml"],
                                 cwd=ROOT, text=True, encoding="utf-8", errors="replace", capture_output=True)
        safe = sanitize(outcome.stdout + outcome.stderr)
        (DIRECTORY / "pytest-output.txt").write_text(safe, encoding="utf-8")
        if existing.exists():
            existing.write_text(sanitize(existing.read_text(encoding="utf-8")), encoding="utf-8")
        print(safe)
        raise SystemExit(outcome.returncode)
