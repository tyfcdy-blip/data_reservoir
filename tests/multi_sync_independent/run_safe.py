"""只保留短回溯并在落盘前脱敏，避免数据库地址进入交付日志。"""
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import urlsplit
from data_service.config import Settings

root = Path(__file__).resolve().parents[2]
settings = Settings(_env_file=root / '.local' / 'test-admin-independent.env')
parsed = urlsplit(settings.database_url)
assert parsed.path == '/admin_independent_test'
targets = sys.argv[1:] or ['tests/multi_sync_independent']
command = [sys.executable, '-m', 'pytest', *targets, '-q', '--tb=short',
           '--junitxml=tests/multi_sync_independent/pytest-result.xml']
result = subprocess.run(command, cwd=root, capture_output=True, text=True, encoding='utf-8', errors='replace')
secrets = [settings.database_url, parsed.password or '', settings.config_encryption_key or '']
def redact(text):
    for value in sorted((value for value in secrets if value), key=len, reverse=True):
        text = text.replace(value, '<redacted>')
    return re.sub(r'postgres(?:ql)?://[^\s\x27\x22<>]+', '<redacted-dsn>', text)
output = redact(result.stdout + result.stderr)
(root / 'tests/multi_sync_independent/pytest-output.txt').write_text(output, encoding='utf-8')
xml = root / 'tests/multi_sync_independent/pytest-result.xml'
if xml.exists():
    xml.write_text(redact(xml.read_text(encoding='utf-8')), encoding='utf-8')
print(output)
raise SystemExit(result.returncode)

