"""记录独立最终回归命令、退出码和被测代码前后摘要；不修改产品文件。"""

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
from time import perf_counter
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = ROOT / "evidence" / "independent"


def snapshot():
    candidates = [*ROOT.joinpath("data_service").glob("*.py"), *ROOT.joinpath("data_service").glob("*.sql")]
    candidates.extend(ROOT / name for name in ["Dockerfile", "docker-compose.yml", "pyproject.toml", "requirements.lock", "uv.lock", ".dockerignore"])
    return {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(candidates) if path.is_file()}


def main():
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    before = snapshot()
    environment = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    executions = []
    for name, command in [
        ("final-ruff", ["uv", "run", "ruff", "check", "data_service", "tests/independent"]),
        ("final-pytest", [sys.executable, "-m", "pytest", "tests/independent", "-q", "--junitxml=evidence/independent/final-suite.xml"]),
    ]:
        tick = perf_counter()
        result = subprocess.run(command, cwd=ROOT, env=environment, capture_output=True, text=True, encoding="utf-8")
        (EVIDENCE / (name + ".log")).write_text(result.stdout + result.stderr, encoding="utf-8")
        executions.append({"command": command, "exit_code": result.returncode, "seconds": perf_counter() - tick})
    after = snapshot()
    junit = ET.parse(EVIDENCE / "final-suite.xml").getroot()
    suites = list(junit) if junit.tag == "testsuites" else [junit]
    counts = {name: sum(int(suite.get(name, 0)) for suite in suites) for name in ["tests", "failures", "errors", "skipped"]}
    report = {"finished_at_utc": datetime.now(timezone.utc).isoformat(), "python": platform.python_version(),
              "platform": platform.platform(), "database": "standalone_independent_test",
              "executions": executions, "counts": counts, "product_before": before, "product_after": after,
              "product_unchanged": before == after,
              "passed": all(item["exit_code"] == 0 for item in executions) and before == after}
    (EVIDENCE / "final-summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"passed": report["passed"], "counts": counts, "product_unchanged": before == after}, ensure_ascii=False))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
