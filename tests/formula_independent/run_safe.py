"""专库安全执行与脱敏证据。"""
import json
from pathlib import Path
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from urllib.parse import urlsplit

from data_service.config import Settings

root = Path(__file__).resolve().parents[2]
settings = Settings(_env_file=root / '.local/test-admin-independent.env')
parsed = urlsplit(settings.database_url)
assert parsed.path == '/admin_independent_test'
targets = sys.argv[1:] or ['tests/formula_independent']
command = [sys.executable, '-m', 'pytest', *targets, '-q', '--tb=short',
           '--junitxml=evidence/formula-independent-results.xml']
result = subprocess.run(command, cwd=root, capture_output=True, text=True, encoding='utf-8', errors='replace')
secrets = [settings.database_url, parsed.password or '', settings.config_encryption_key or '', settings.admin_password or '']

def redact(value):
    for secret in sorted((value for value in secrets if value), key=len, reverse=True):
        value = value.replace(secret, '<redacted>')
    return re.sub(r'postgres(?:ql)?://[^\s\x27\x22<>]+', '<redacted-dsn>', value)

output = redact(result.stdout + result.stderr)
(root / 'evidence/formula-independent-output.log').write_text(output, encoding='utf-8')
xml = root / 'evidence/formula-independent-results.xml'
counts = {}
if xml.exists():
    xml.write_text(redact(xml.read_text(encoding='utf-8')), encoding='utf-8')
    suite = ET.parse(xml).getroot().find('testsuite')
    if suite is not None:
        counts = {key: suite.attrib[key] for key in ('tests', 'failures', 'errors', 'skipped', 'time')}
(root / 'evidence/formula-independent-run.json').write_text(json.dumps({
    'command': ' '.join(command), 'exit_code': result.returncode, 'counts': counts,
    'environment': 'Python 3.12 / real PostgreSQL / isolated admin_independent_test',
}, ensure_ascii=False, indent=2), encoding='utf-8')
print(output)
raise SystemExit(result.returncode)
