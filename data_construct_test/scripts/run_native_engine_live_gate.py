"""Strict live Phase 1 gate for all four native Phase 1 judge engines.

This command runs only ``test_native_engine_live_e2e.py``.  It does not invoke
Docker Compose and therefore cannot start any database. The test module skips
unavailable engines during ordinary pytest runs; strict mode turns any missing
or unreachable MySQL, PostgreSQL, SQL Server, or Oracle engine into a non-zero
gate result.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = PROJECT_ROOT / "sql-edu-backend"
TEST_FILE = "tests/test_native_engine_live_e2e.py"


def main() -> int:
    environment = os.environ.copy()
    environment["PARSEVAL_NATIVE_LIVE_STRICT"] = "1"
    environment["PYTHONUNBUFFERED"] = "1"
    # A release gate must not inherit selectors such as -k or --lf that can
    # silently narrow the required engine matrix.
    environment.pop("PYTEST_ADDOPTS", None)
    command = (
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "--strict-config",
        "--strict-markers",
        TEST_FILE,
    )
    print("Phase 1 native four-engine live gate", flush=True)
    print(
        "Scope: MySQL, PostgreSQL, SQL Server, and Oracle; no engine is started by this command",
        flush=True,
    )
    result = subprocess.run(command, cwd=BACKEND_ROOT, env=environment, check=False)
    if result.returncode == 0:
        print("Phase 1 native four-engine live gate: PASS", flush=True)
    else:
        print(
            f"Phase 1 native four-engine live gate: FAIL (exit {result.returncode})",
            flush=True,
        )
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
