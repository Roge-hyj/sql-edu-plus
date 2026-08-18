from __future__ import annotations

from collections import Counter
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = (
    PROJECT_ROOT
    / "data_construct_test"
    / "scripts"
    / "run_phase1_teaching_dialect_matrix.py"
)


def _load_matrix_module():
    spec = importlib.util.spec_from_file_location("phase1_teaching_dialect_matrix", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_matrix_covers_all_five_teaching_dialects_and_native_expectations():
    matrix = _load_matrix_module()
    cases = matrix.build_cases()

    assert len(cases) == 16
    assert Counter(case.dialect for case in cases) == {
        "standard": 2,
        "mysql": 3,
        "postgres": 3,
        "tsql": 4,
        "oracle": 4,
    }
    assert Counter(case.execution_expectation for case in cases) == {
        matrix.FULL_FLOW_EXPECTATION: 14,
        matrix.SEMANTIC_BOUNDARY_EXPECTATION: 2,
    }
    assert {case.backend for case in cases} == {
        "sqlite",
        "mysql",
        "postgres",
        "tsql",
        "oracle",
    }


def test_offline_matrix_keeps_native_evidence_pending():
    matrix = _load_matrix_module()
    report = matrix.build_report()
    summary = report["summary"]

    assert report["mode"] == "offline"
    assert summary["total"] == 16
    assert summary["stage_status"]["structure"] == {"PASS": 16}
    assert summary["stage_status"]["ast_diff"] == {"PASS": 16}
    assert summary["stage_status"]["execution"] == {
        "PASS": 2,
        "PENDING_NATIVE": 14,
    }
    for stage in ("data", "mutation", "full_flow"):
        assert summary["stage_status"][stage] == {"PASS": 2, "NOT_RUN": 14}

    for item in report["results"]:
        case = item["case"]
        assert item["resolution"]["status"] == "RESOLVED"
        assert item["structure"]["status"] == "PASS"
        assert item["ast_diff"]["status"] == "PASS"
        if case["native_required"]:
            assert item["execution"]["status"] == "PENDING_NATIVE"
            assert "data" not in item
            assert "mutation" not in item
            assert "full_flow" not in item
        else:
            assert case["backend"] == "sqlite"
            assert item["execution"]["status"] == "PASS"
            assert item["full_flow"]["status"] == "PASS"


def test_matrix_declares_only_real_semantic_boundaries():
    matrix = _load_matrix_module()
    cases = {case.case_id: case for case in matrix.build_cases()}

    assert {
        case_id
        for case_id, case in cases.items()
        if case.execution_expectation == matrix.SEMANTIC_BOUNDARY_EXPECTATION
    } == {"postgres_from_only", "oracle_sample_rate"}
    assert "inheritance" in cases["postgres_from_only"].boundary_reason.lower()
    assert "probabilistic" in cases["oracle_sample_rate"].boundary_reason.lower()
    assert cases["mysql_if_function"].expected_clause == "FUNCTION"
    assert cases["mysql_if_function"].expected_kp == "function"


def test_native_cli_loads_backend_env_without_overriding_shell(tmp_path, monkeypatch):
    matrix = _load_matrix_module()
    monkeypatch.setattr(matrix, "BACKEND_ROOT", tmp_path)
    (tmp_path / ".env").write_text(
        "PARSEVAL_MYSQL_URL=mysql://from-dotenv\n"
        "PARSEVAL_POSTGRES_URL=postgresql://from-dotenv\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("PARSEVAL_MYSQL_URL", raising=False)
    monkeypatch.setenv("PARSEVAL_POSTGRES_URL", "postgresql://from-shell")

    matrix._load_native_environment()

    assert os.environ["PARSEVAL_MYSQL_URL"] == "mysql://from-dotenv"
    assert os.environ["PARSEVAL_POSTGRES_URL"] == "postgresql://from-shell"


def test_native_and_offline_default_reports_use_separate_paths():
    matrix = _load_matrix_module()

    offline_json, offline_md = matrix._default_output_paths(native=False)
    native_json, native_md = matrix._default_output_paths(native=True)

    assert offline_json.name == "phase1_teaching_dialect_matrix.json"
    assert offline_md.name == "phase1_teaching_dialect_matrix.md"
    assert native_json.name == "phase1_teaching_dialect_matrix_native.json"
    assert native_md.name == "phase1_teaching_dialect_matrix_native.md"


def test_report_rendering_and_cli_outputs_are_deterministic(tmp_path):
    matrix = _load_matrix_module()
    first = matrix.build_report()
    second = matrix.build_report()
    assert first == second

    markdown = matrix.render_markdown(first)
    assert "Vendor cases remain `PENDING_NATIVE`" in markdown
    assert "Explicit semantic boundaries" in markdown
    assert "postgres_from_only" in markdown
    assert "oracle_sample_rate" in markdown

    json_output = tmp_path / "matrix.json"
    markdown_output = tmp_path / "matrix.md"
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--output-json",
            str(json_output),
            "--output-md",
            str(markdown_output),
        ],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(json_output.read_text(encoding="utf-8")) == first
    assert markdown_output.read_text(encoding="utf-8") == markdown
