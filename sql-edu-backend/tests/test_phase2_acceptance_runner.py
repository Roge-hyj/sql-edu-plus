from __future__ import annotations

import json
import subprocess
import socket
from types import SimpleNamespace

import pytest

from scripts import run_phase2_acceptance as runner
from scripts import phase2_acceptance_guard


def test_pytest_summary_parsing_is_structured_and_does_not_retain_text():
    counts = runner._parse_test_counts(
        "reference SQL must never be copied\n2 failed, 17 passed, 1 skipped in 0.42s"
    )

    assert counts == {
        "passed": 17,
        "failed": 2,
        "errors": 0,
        "skipped": 1,
        "xfailed": 0,
        "xpassed": 0,
        "deselected": 0,
        "executed": 20,
    }
    safe = runner._safe_command_result(
        runner.CommandResult(("python", "-m", "pytest"), 1, "hidden SQL")
    )
    assert "output" not in safe
    assert safe["raw_output_retained"] is False


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("63 tests collected in 0.10s", 63),
        ("tests/a.py::test_one\ntests/a.py::test_two[x]\n", 2),
        ("no tests collected in 0.01s", 0),
    ],
)
def test_collection_count_is_stable_across_pytest_summary_shapes(output, expected):
    assert runner._parse_collected_count(output) == expected


def test_subprocess_runner_uses_no_shell_and_applies_limits(monkeypatch):
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    result = runner._run_command(
        ("python", "-m", "pytest"),
        memory_mib=1024,
        timeout_seconds=30,
    )

    assert result.exit_code == 0
    assert captured["kwargs"]["shell"] is False
    assert captured["kwargs"]["stdin"] is subprocess.DEVNULL
    assert captured["kwargs"]["env"]["PHASE2_ACCEPTANCE_OFFLINE"] == "1"
    assert "preexec_fn" in captured["kwargs"]


def test_allowlist_does_not_expand_to_database_or_corpus_suites():
    targets = tuple(
        target
        for group_targets in runner.TEST_GROUPS.values()
        for target in group_targets
    )

    assert "tests/test_check_sql_flow.py" not in targets
    assert not any("integration" in target for target in targets)
    assert not any("corpus" in target for target in targets)
    assert not any("native_engine_live" in target for target in targets)


@pytest.mark.parametrize(
    "guard",
    [
        phase2_acceptance_guard._guarded_socket_connect,
        phase2_acceptance_guard._guarded_socket_connect_ex,
    ],
)
def test_offline_guard_rejects_internet_socket_without_echoing_address(guard):
    fake_socket = type("FakeSocket", (), {"family": socket.AF_INET})()

    with pytest.raises(OSError) as caught:
        guard(
            fake_socket,
            ("private.example", 443),
        )

    assert "private.example" not in str(caught.value)
    assert "outbound network access was blocked" in str(caught.value)


def test_offline_guard_blocks_unconnected_udp_sendto():
    fake_socket = type("FakeSocket", (), {"family": socket.AF_INET})()

    with pytest.raises(OSError) as caught:
        phase2_acceptance_guard._guarded_socket_sendto(
            fake_socket,
            b"probe",
            ("private.example", 53),
        )

    assert "private.example" not in str(caught.value)


def test_offline_guard_configure_and_unconfigure_restore_all_entrypoints(
    monkeypatch,
):
    fake_socket_type = type(
        "FakeSocketType",
        (),
        {
            "connect": object(),
            "connect_ex": object(),
            "sendto": object(),
        },
    )
    fake_socket_module = SimpleNamespace(
        socket=fake_socket_type,
        create_connection=object(),
    )
    monkeypatch.setattr(phase2_acceptance_guard, "socket", fake_socket_module)
    monkeypatch.setenv("PHASE2_ACCEPTANCE_OFFLINE", "1")

    phase2_acceptance_guard.pytest_configure()

    assert fake_socket_type.connect is phase2_acceptance_guard._guarded_socket_connect
    assert fake_socket_type.connect_ex is phase2_acceptance_guard._guarded_socket_connect_ex
    assert fake_socket_type.sendto is phase2_acceptance_guard._guarded_socket_sendto
    assert (
        fake_socket_module.create_connection
        is phase2_acceptance_guard._guarded_create_connection
    )

    phase2_acceptance_guard.pytest_unconfigure()

    assert fake_socket_type.connect is phase2_acceptance_guard._ORIGINAL_SOCKET_CONNECT
    assert fake_socket_type.connect_ex is phase2_acceptance_guard._ORIGINAL_SOCKET_CONNECT_EX
    assert fake_socket_type.sendto is phase2_acceptance_guard._ORIGINAL_SOCKET_SENDTO
    assert (
        fake_socket_module.create_connection
        is phase2_acceptance_guard._ORIGINAL_CREATE_CONNECTION
    )


def test_acceptance_fails_closed_and_aggregates_counts(monkeypatch):
    monkeypatch.setattr(
        runner,
        "_rule_catalog_snapshot",
        lambda: {
            "version": "phase2.rules.mvp20.v1",
            "expected_rule_count": 20,
            "declared_rule_count": 20,
            "matrix_covered_rule_count": 20,
            "exact_matrix_match": True,
            "rule_ids": [],
            "missing_matrix_rule_ids": [],
            "extra_matrix_rule_ids": [],
            "matrix_load_error": None,
        },
    )
    monkeypatch.setattr(runner, "_missing_target_files", lambda: [])

    def fake_group(name, targets, **_kwargs):
        failed = name == "error_diagnosis"
        return {
            "name": name,
            "targets": list(targets),
            "collection": {"collected": 2},
            "execution": {
                "counts": {
                    "passed": 1 if failed else 2,
                    "failed": 1 if failed else 0,
                    "errors": 0,
                    "skipped": 0,
                    "xfailed": 0,
                    "xpassed": 0,
                    "deselected": 0,
                    "executed": 2,
                }
            },
            "passed": not failed,
        }

    monkeypatch.setattr(runner, "_run_test_group", fake_group)
    report = runner.run_acceptance(
        python_executable="python",
        memory_mib=1024,
        timeout_seconds=30,
    )

    assert report["result"] == "FAIL"
    assert report["exit_code"] == 1
    assert report["totals"]["groups"] == len(runner.TEST_GROUPS)
    assert report["totals"]["failed"] == 1
    assert report["policy"]["commands_use_shell"] is False


def test_rule_catalog_snapshot_requires_exact_twenty_rule_matrix():
    snapshot = runner._rule_catalog_snapshot()

    assert snapshot["expected_rule_count"] == 20
    assert snapshot["declared_rule_count"] == 20
    assert snapshot["matrix_covered_rule_count"] == 20
    assert snapshot["exact_matrix_match"] is True
    assert len(snapshot["rule_ids"]) == 20


def test_main_writes_report_and_propagates_gate_exit(monkeypatch, tmp_path, capsys):
    report_path = tmp_path / "phase2.json"
    monkeypatch.setattr(
        runner,
        "run_acceptance",
        lambda **_kwargs: {
            "schema_version": runner.REPORT_SCHEMA_VERSION,
            "result": "FAIL",
            "exit_code": 1,
        },
    )

    exit_code = runner.main(["--output", str(report_path), "--memory-mib", "1024"])

    assert exit_code == 1
    assert json.loads(report_path.read_text(encoding="utf-8"))["result"] == "FAIL"
    assert "phase2.json" in capsys.readouterr().out
    assert report_path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("memory_mib", [511, 8193])
def test_memory_budget_outside_safe_range_is_rejected(memory_mib):
    with pytest.raises(ValueError, match="memory_mib"):
        runner.run_acceptance(
            python_executable="python",
            memory_mib=memory_mib,
            timeout_seconds=30,
        )
