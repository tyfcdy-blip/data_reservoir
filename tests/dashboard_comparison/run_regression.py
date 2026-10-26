"""读取原有专用测试库配置，执行受覆盖读取抽取影响的既有查询/权限回归。"""

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from urllib.parse import unquote, urlsplit, urlunsplit
import xml.etree.ElementTree as ET

from dotenv import dotenv_values

from data_service import db
from data_service.config import Settings


root = Path(__file__).resolve().parents[2]
jobs = [
    ("test-query.env", "standalone_query_test", ["tests/test_query.py", "tests/test_query_formulas.py", "tests/test_formula_models.py"]),
    ("test-admin-api.env", "admin_api_test", ["tests/test_query_services.py", "tests/test_key_permissions.py"]),
]
results = []
for env_file, expected_database, tests in jobs:
    values = {key: value for key, value in dotenv_values(root / ".local" / env_file).items() if value is not None}
    secrets = list(values.values())
    for key in ("DS_DATABASE_URL", "DS_TEST_DATABASE_URL"):
        parsed = urlsplit(values[key])
        assert parsed.path == "/" + expected_database, "拒绝非指定隔离库"
        secrets.append(unquote(parsed.password or ""))
        values[key] = urlunsplit(parsed._replace(netloc=parsed.netloc.rsplit("@", 1)[0] + "@db:5432"))
        secrets.append(values[key])
    settings = Settings(database_url=values["DS_TEST_DATABASE_URL"])
    with db.connect(settings) as connection:
        connection.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
    cleanup = None
    try:
        with tempfile.TemporaryDirectory(prefix="period-regression-") as directory:
            report = Path(directory) / "junit.xml"
            command = [sys.executable, "-m", "pytest", *tests, "-q", "--tb=short", "--junitxml=" + str(report)]
            result = subprocess.run(command, cwd=root, env={**os.environ, **values}, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True, encoding="utf-8")
            output = re.sub(r"ds_[A-Za-z0-9_-]{43}", "[REDACTED_TEST_KEY]", result.stdout)
            for secret in sorted(set(secrets), key=len, reverse=True):
                if len(secret) >= 8:
                    output = output.replace(secret, "[REDACTED]")
            counts = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
            cases = []
            if report.exists():
                tree = ET.parse(report)
                for suite in tree.findall("testsuite"):
                    for key in counts:
                        counts[key] += int(suite.attrib.get(key, 0))
                for case in tree.findall(".//testcase"):
                    status = "failed" if case.find("failure") is not None or case.find("error") is not None else "skipped" if case.find("skipped") is not None else "passed"
                    cases.append({"name": case.attrib["name"], "status": status, "seconds": float(case.attrib.get("time", 0))})
            results.append({"command": "python -m pytest " + " ".join(tests) + " -q --tb=short --junitxml=TEMP/junit.xml",
                            "database": expected_database, "exit_code": result.returncode, "counts": counts,
                            "cases": cases, "output": output})
    finally:
        # 仅清理上述白名单专库，保留schema5空结构，既有测试迁移不能遗留降级状态。
        with db.connect(settings) as connection:
            connection.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
        db.migrate(settings)
        with db.connect(settings) as connection:
            cleanup = connection.execute("SELECT (SELECT max(version) FROM schema_versions) AS schema_version, (SELECT count(*) FROM datasets) AS datasets, (SELECT count(*) FROM query_services) AS services, (SELECT count(*) FROM api_keys) AS api_keys").fetchone()
        assert cleanup == {"schema_version": 5, "datasets": 0, "services": 0, "api_keys": 0}
        if results and results[-1]["database"] == expected_database:
            results[-1]["cleanup"] = cleanup
counts = {key: sum(result["counts"][key] for result in results) for key in ("tests", "failures", "errors", "skipped")}
evidence = {"command": "docker run --rm --network muyaju-data-service_default -e PYTHONPATH=/work -v PROJECT:/work -v EVIDENCE:/dashboard-evidence -w /work muyaju-data-service:key-tests python tests/dashboard_comparison/run_regression.py",
            "exit_code": max(result["exit_code"] for result in results), "counts": counts,
            "passed": counts["tests"] - counts["failures"] - counts["errors"] - counts["skipped"],
            "results": results, "real_postgresql": True, "production_touched": False}
Path("/dashboard-evidence/backend-regression.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(json.dumps({key: evidence[key] for key in ("exit_code", "counts", "passed")}, ensure_ascii=False))
for result in results:
    print(result["output"])
raise SystemExit(evidence["exit_code"])
