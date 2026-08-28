"""Run the deterministic, offline Phase 2 acceptance gate.

This runner deliberately executes a small allowlist of unit, bounded local
contract, and pure-route tests.  It does not load a model, inspect generated
corpora, connect to an external database service, or retain pytest failure
text.  Every child process has a bounded address space and file size, and
Python-level Internet sockets are blocked by ``phase2_acceptance_guard``.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import resource
import subprocess
import sys
import tempfile
from typing import Any, Mapping, Sequence


REPORT_SCHEMA_VERSION = "phase2.acceptance-report.v1"
EXPECTED_RULE_COUNT = 20
DEFAULT_MEMORY_MIB = 4096
MIN_MEMORY_MIB = 512
MAX_MEMORY_MIB = 8192
DEFAULT_TIMEOUT_SECONDS = 240
MAX_TIMEOUT_SECONDS = 1800
MAX_CAPTURE_BYTES = 512 * 1024
MAX_OUTPUT_FILE_MIB = 8

SCRIPT_DIR = Path(__file__).resolve().parent
BACKEND_ROOT = SCRIPT_DIR.parent
PROJECT_ROOT = BACKEND_ROOT.parent
DEFAULT_OUTPUT = (
    PROJECT_ROOT
    / "data_construct_test"
    / "outputs"
    / "phase2_acceptance_report.json"
)

# Static allowlist: no external database service, online engines, corpus scans,
# or model calls.  The Phase 1 scope-contract test uses only its bounded local
# sandbox.  Node IDs in ``route_guards`` select dependency-free route tests.
TEST_GROUPS: Mapping[str, tuple[str, ...]] = {
    "phase1_scope_contract": ("tests/test_phase1_scope_contract.py",),
    "schema_catalog": ("tests/test_phase2_schema_catalog.py",),
    "scoped_query_graph": ("tests/test_scoped_query_graph.py",),
    "rule_matrix_mvp20": ("tests/test_phase2_rule_matrix.py",),
    "error_diagnosis": ("tests/test_error_diagnosis.py",),
    "public_contracts": (
        "tests/test_public_schema_preview_security.py",
        "tests/test_question_dialect_metadata.py",
        "tests/test_ai_question_generator_dialect.py",
    ),
    "route_guards": (
        "tests/test_check_sql_flow.py::test_check_sql_request_rejects_unbounded_sql_payload",
        "tests/test_check_sql_flow.py::test_phase1_capacity_stays_held_until_timed_out_thread_finishes",
        "tests/test_check_sql_flow.py::test_check_sql_undecided_never_becomes_correct_or_writes_learning_state",
        "tests/test_check_sql_flow.py::test_phase1_no_counterexample_requires_clean_supported_contract",
        "tests/test_check_sql_flow.py::test_public_diagnostic_gate_binds_verdict_and_rejects_reference_fragments",
    ),
}

_COUNT_LABELS = {
    "passed": "passed",
    "failed": "failed",
    "error": "errors",
    "errors": "errors",
    "skipped": "skipped",
    "xfailed": "xfailed",
    "xpassed": "xpassed",
    "deselected": "deselected",
}


@dataclass(frozen=True)
class CommandResult:
    command: tuple[str, ...]
    exit_code: int
    output: str
    timed_out: bool = False


def _bounded_environment() -> dict[str, str]:
    """Build a small environment without forwarding credentials."""
    allowed_names = (
        "PATH",
        "LANG",
        "LC_ALL",
        "TZ",
        "TMPDIR",
        "TEMP",
        "TMP",
        "SYSTEMROOT",
    )
    environment = {
        name: os.environ[name]
        for name in allowed_names
        if name in os.environ
    }
    environment.update(
        {
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONHASHSEED": "0",
            "PHASE2_ACCEPTANCE_OFFLINE": "1",
            "OMP_NUM_THREADS": "1",
            "OPENBLAS_NUM_THREADS": "1",
            "MKL_NUM_THREADS": "1",
            "NUMEXPR_NUM_THREADS": "1",
            "TOKENIZERS_PARALLELISM": "false",
            "TRANSFORMERS_OFFLINE": "1",
            "HF_HUB_OFFLINE": "1",
        }
    )
    return environment


def _resource_limiter(memory_mib: int, timeout_seconds: int):
    address_space_bytes = memory_mib * 1024 * 1024
    output_file_bytes = MAX_OUTPUT_FILE_MIB * 1024 * 1024

    def apply_limits() -> None:
        resource.setrlimit(
            resource.RLIMIT_AS,
            (address_space_bytes, address_space_bytes),
        )
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        resource.setrlimit(
            resource.RLIMIT_FSIZE,
            (output_file_bytes, output_file_bytes),
        )
        # CPU time is a final fail-safe; the parent wall-clock timeout normally
        # fires first.  The hard limit gives Python time to report a soft signal.
        resource.setrlimit(
            resource.RLIMIT_CPU,
            (timeout_seconds, min(timeout_seconds + 5, MAX_TIMEOUT_SECONDS + 5)),
        )

    return apply_limits


def _tail(handle: Any, limit: int = MAX_CAPTURE_BYTES) -> str:
    handle.flush()
    size = handle.tell()
    handle.seek(max(0, size - limit))
    return handle.read(limit).decode("utf-8", errors="replace")


def _run_command(
    command: Sequence[str],
    *,
    memory_mib: int,
    timeout_seconds: int,
) -> CommandResult:
    normalized = tuple(str(item) for item in command)
    with tempfile.TemporaryFile(mode="w+b") as stdout_handle, tempfile.TemporaryFile(
        mode="w+b"
    ) as stderr_handle:
        try:
            completed = subprocess.run(
                list(normalized),
                cwd=BACKEND_ROOT,
                env=_bounded_environment(),
                stdin=subprocess.DEVNULL,
                stdout=stdout_handle,
                stderr=stderr_handle,
                check=False,
                shell=False,
                timeout=timeout_seconds,
                preexec_fn=_resource_limiter(memory_mib, timeout_seconds),
            )
            exit_code = int(completed.returncode)
            timed_out = False
        except subprocess.TimeoutExpired:
            exit_code = 124
            timed_out = True
        output = f"{_tail(stdout_handle)}\n{_tail(stderr_handle)}"
    return CommandResult(normalized, exit_code, output, timed_out)


def _parse_test_counts(output: str) -> dict[str, int]:
    counts = {value: 0 for value in _COUNT_LABELS.values()}
    for amount, label in re.findall(
        r"(?<![\w.])(\d+)\s+"
        r"(passed|failed|errors?|skipped|xfailed|xpassed|deselected)\b",
        output.lower(),
    ):
        counts[_COUNT_LABELS[label]] = max(
            counts[_COUNT_LABELS[label]], int(amount)
        )
    counts["executed"] = sum(
        counts[name]
        for name in ("passed", "failed", "errors", "skipped", "xfailed", "xpassed")
    )
    return counts


def _parse_collected_count(output: str) -> int:
    matches = re.findall(r"\b(\d+)\s+(?:tests?|items?)\s+collected\b", output.lower())
    if matches:
        return int(matches[-1])
    if "no tests collected" in output.lower():
        return 0
    # ``pytest -q --collect-only`` prints one node ID per line.  This fallback
    # is useful across pytest versions whose final wording differs.
    return len(
        {
            line.strip()
            for line in output.splitlines()
            if "::test_" in line and not line.lstrip().startswith("=")
        }
    )


def _pytest_command(
    python_executable: str,
    targets: Sequence[str],
    *,
    collect_only: bool,
) -> tuple[str, ...]:
    command = [
        python_executable,
        "-m",
        "pytest",
        "-q",
        "--disable-warnings",
        "--tb=short",
        "-p",
        "pytest_asyncio.plugin",
        "-p",
        "scripts.phase2_acceptance_guard",
    ]
    if collect_only:
        command.append("--collect-only")
    command.extend(targets)
    return tuple(command)


def _safe_command_result(result: CommandResult) -> dict[str, Any]:
    """Exclude captured test text because assertions may contain hidden SQL."""
    return {
        "command": list(result.command),
        "exit_code": result.exit_code,
        "timed_out": result.timed_out,
        "raw_output_retained": False,
    }


def _run_test_group(
    name: str,
    targets: Sequence[str],
    *,
    python_executable: str,
    memory_mib: int,
    timeout_seconds: int,
) -> dict[str, Any]:
    collect_result = _run_command(
        _pytest_command(python_executable, targets, collect_only=True),
        memory_mib=memory_mib,
        timeout_seconds=timeout_seconds,
    )
    collected = _parse_collected_count(collect_result.output)
    execution_result = _run_command(
        _pytest_command(python_executable, targets, collect_only=False),
        memory_mib=memory_mib,
        timeout_seconds=timeout_seconds,
    )
    counts = _parse_test_counts(execution_result.output)
    passed = (
        collect_result.exit_code == 0
        and execution_result.exit_code == 0
        and collected > 0
        and counts["passed"] == collected
        and all(
            counts[name] == 0
            for name in ("failed", "errors", "skipped", "xfailed", "xpassed")
        )
    )
    return {
        "name": name,
        "targets": list(targets),
        "collection": {
            **_safe_command_result(collect_result),
            "collected": collected,
        },
        "execution": {
            **_safe_command_result(execution_result),
            "counts": counts,
        },
        "passed": passed,
    }


def _rule_catalog_snapshot() -> dict[str, Any]:
    sys.path.insert(0, str(BACKEND_ROOT))
    try:
        try:
            from core.error_diagnosis import RULE_CATALOG, RULE_CATALOG_VERSION

            catalog_ids = sorted(str(rule.rule_id) for rule in RULE_CATALOG)
            catalog_version = str(RULE_CATALOG_VERSION)
            catalog_error = None
        except Exception as exc:  # pragma: no cover - exercised via gate failure
            catalog_ids = []
            catalog_version = "unavailable"
            catalog_error = type(exc).__name__
        try:
            from tests.test_phase2_rule_matrix import RULE_CASES

            matrix_ids = sorted(str(rule_id) for rule_id in RULE_CASES)
            matrix_error = None
        except Exception as exc:  # pragma: no cover - exercised via gate failure
            matrix_ids = []
            matrix_error = type(exc).__name__
    finally:
        if sys.path and sys.path[0] == str(BACKEND_ROOT):
            sys.path.pop(0)

    catalog_set = set(catalog_ids)
    matrix_set = set(matrix_ids)
    exact_match = (
        catalog_error is None
        and matrix_error is None
        and len(catalog_ids) == EXPECTED_RULE_COUNT
        and len(catalog_ids) == len(catalog_set)
        and matrix_set == catalog_set
    )
    return {
        "version": catalog_version,
        "expected_rule_count": EXPECTED_RULE_COUNT,
        "declared_rule_count": len(catalog_ids),
        "matrix_covered_rule_count": len(catalog_set & matrix_set),
        "exact_matrix_match": exact_match,
        "rule_ids": catalog_ids,
        "missing_matrix_rule_ids": sorted(catalog_set - matrix_set),
        "extra_matrix_rule_ids": sorted(matrix_set - catalog_set),
        "catalog_load_error": catalog_error,
        "matrix_load_error": matrix_error,
    }


def _missing_target_files() -> list[str]:
    missing: set[str] = set()
    for targets in TEST_GROUPS.values():
        for target in targets:
            relative_file = target.split("::", 1)[0]
            if not (BACKEND_ROOT / relative_file).is_file():
                missing.add(relative_file)
    return sorted(missing)


def run_acceptance(
    *,
    python_executable: str,
    memory_mib: int,
    timeout_seconds: int,
) -> dict[str, Any]:
    if not MIN_MEMORY_MIB <= memory_mib <= MAX_MEMORY_MIB:
        raise ValueError(
            f"memory_mib must be between {MIN_MEMORY_MIB} and {MAX_MEMORY_MIB}"
        )
    if not 1 <= timeout_seconds <= MAX_TIMEOUT_SECONDS:
        raise ValueError(
            f"timeout_seconds must be between 1 and {MAX_TIMEOUT_SECONDS}"
        )

    rule_catalog = _rule_catalog_snapshot()
    missing_targets = _missing_target_files()
    groups = [
        _run_test_group(
            name,
            targets,
            python_executable=python_executable,
            memory_mib=memory_mib,
            timeout_seconds=timeout_seconds,
        )
        for name, targets in TEST_GROUPS.items()
    ]
    total_collected = sum(group["collection"]["collected"] for group in groups)
    aggregate_counts = {
        key: sum(group["execution"]["counts"][key] for group in groups)
        for key in (
            "passed",
            "failed",
            "errors",
            "skipped",
            "xfailed",
            "xpassed",
            "deselected",
            "executed",
        )
    }
    accepted = (
        not missing_targets
        and bool(rule_catalog["exact_matrix_match"])
        and all(group["passed"] for group in groups)
    )
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "result": "PASS" if accepted else "FAIL",
        "exit_code": 0 if accepted else 1,
        "policy": {
            "network": "PYTHON_INET_SOCKETS_BLOCKED",
            "model_loading": "NOT_REFERENCED_BY_STATIC_ALLOWLIST_AND_OFFLINE_ENV",
            "large_corpus_reads": "NONE",
            "database_integration": "EXTERNAL_SERVICE_AND_ASYNC_ROUTE_DB_OUT_OF_SCOPE",
            "raw_pytest_output_retained": False,
            "address_space_limit_mib_per_process": memory_mib,
            "output_file_limit_mib_per_process": MAX_OUTPUT_FILE_MIB,
            "wall_timeout_seconds_per_command": timeout_seconds,
            "thread_environment_limit": 1,
            "max_concurrent_test_processes": 1,
            "commands_use_shell": False,
        },
        "preflight": {
            "missing_test_target_files": missing_targets,
            "passed": not missing_targets,
        },
        "rule_catalog": rule_catalog,
        "totals": {
            "groups": len(groups),
            "groups_passed": sum(bool(group["passed"]) for group in groups),
            "collected": total_collected,
            **aggregate_counts,
        },
        "groups": groups,
    }


def _atomic_write_json(path: Path, report: Mapping[str, Any]) -> None:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        report,
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
    ) + "\n"
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the bounded, offline Phase 2 acceptance gate."
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="JSON report path (default: %(default)s)",
    )
    parser.add_argument(
        "--python",
        default=sys.executable,
        help="Python interpreter used for pytest (default: current interpreter)",
    )
    parser.add_argument(
        "--memory-mib",
        type=int,
        default=DEFAULT_MEMORY_MIB,
        help=f"per-process address-space limit ({MIN_MEMORY_MIB}-{MAX_MEMORY_MIB} MiB)",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=DEFAULT_TIMEOUT_SECONDS,
        help=f"per-command wall timeout (1-{MAX_TIMEOUT_SECONDS} seconds)",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        report = run_acceptance(
            python_executable=str(Path(args.python).expanduser()),
            memory_mib=args.memory_mib,
            timeout_seconds=args.timeout_seconds,
        )
    except Exception as exc:
        report = {
            "schema_version": REPORT_SCHEMA_VERSION,
            "result": "FAIL",
            "exit_code": 2,
            "configuration_error": type(exc).__name__,
        }
    _atomic_write_json(args.output, report)
    print(
        json.dumps(
            {
                "result": report["result"],
                "exit_code": report["exit_code"],
                "report": str(args.output.resolve()),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return int(report["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())
