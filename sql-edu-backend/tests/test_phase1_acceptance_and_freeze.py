from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _load(name: str, relative: str):
    path = PROJECT_ROOT / relative
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_statistical_report_excludes_unknown_verdicts_and_warns_on_small_n():
    module = _load(
        "phase1_statistical_acceptance_test",
        "data_construct_test/scripts/report_phase1_statistical_acceptance.py",
    )
    rows = [
        {"expectation": "NOT_EQUIVALENT", "oracle": {"verdict": "NOT_EQUIVALENT"}},
        {"expectation": "NOT_EQUIVALENT", "oracle": {"verdict": "UNDECIDED"}},
        {"expectation": "EQUIVALENT", "oracle": {"verdict": "EQUIVALENT"}},
        {"expectation": "EQUIVALENT", "oracle": {"verdict": "ENGINE_GAP"}},
    ]
    result = module._oracle_metric(rows)
    assert result["not_equivalent_detection"]["trials"] == 1
    assert result["not_equivalent_detection"]["successes"] == 1
    assert result["equivalent_false_positive_control"]["trials"] == 1
    assert result["excluded"] == {"ENGINE_GAP": 1, "UNDECIDED": 1}
    assert module._warnings(result)


def test_split_leakage_audit_detects_cross_partition_family_overlap(tmp_path):
    module = _load(
        "phase1_split_leakage_test",
        "data_construct_test/scripts/audit_phase1_split_leakage.py",
    )
    universe = tmp_path / "universe"
    universe.mkdir()
    records = {
        "train": [{"partition": "train", "family_id": "same", "sql": "SELECT 1", "schema": ""}],
        "public": [{"partition": "public", "family_id": "same", "sql": "SELECT 1", "schema": ""}],
        "hidden": [{"partition": "hidden", "family_id": "hidden", "sql": "SELECT 2", "schema": ""}],
    }
    for name, values in records.items():
        (universe / f"{name}.jsonl").write_text(
            "".join(json.dumps(value) + "\n" for value in values),
            encoding="utf-8",
        )
    result = module.audit(universe, tmp_path / "leakage.json")
    assert not result["pass"]
    assert any("overlap:train_vs_public:family" in item for item in result["hard_failures"])
    assert "hidden_family_digest" in result["hidden_digest_only"]


def test_split_leakage_reports_normalized_template_overlap_without_hard_failure(tmp_path):
    module = _load(
        "phase1_split_template_overlap_test",
        "data_construct_test/scripts/audit_phase1_split_leakage.py",
    )
    universe = tmp_path / "universe"
    universe.mkdir()
    records = {
        "train": [{
            "partition": "train",
            "family_id": "train-family",
            "sql": "SELECT name FROM t WHERE id = 1",
            "schema": "t(id INT, name TEXT)",
        }],
        "public": [{
            "partition": "public",
            "family_id": "public-family",
            "sql": "SELECT name FROM t WHERE id = 2",
            "schema": "t(id INT, name TEXT, extra TEXT)",
        }],
        "hidden": [],
    }
    for name, values in records.items():
        (universe / f"{name}.jsonl").write_text(
            "".join(json.dumps(value) + "\n" for value in values),
            encoding="utf-8",
        )
    result = module.audit(universe, tmp_path / "leakage.json")
    assert result["pass"] is True
    assert result["template_overlaps"]
    assert result["pairwise_overlap_counts"]["train_vs_public"]["normalized_sql"] == 1


def test_freeze_selector_is_deterministic():
    module = _load(
        "phase1_freeze_test",
        "data_construct_test/scripts/run_phase1_freeze_verification.py",
    )
    rows = [{"family_id": str(index)} for index in range(20)]
    first = [row["family_id"] for row in module._select(rows, 5, 17)]
    second = [row["family_id"] for row in module._select(rows, 5, 17)]
    assert first == second


def test_hidden_pair_controls_use_each_record_schema():
    module = _load(
        "phase1_freeze_schema_scope_test",
        "data_construct_test/scripts/run_phase1_freeze_verification.py",
    )
    # The first query needs its numeric-leading schema identifier repaired by
    # the parser.  Passing the second row's schema would make its equivalence
    # control look unparsable and incorrectly remove the family from scope.
    records = [
        {
            "family_id": "first-family",
            "partition": "hidden",
            "dialect": "generic",
            "schema": "scores(2006_07, name)",
            "sql": "SELECT name FROM scores WHERE 2006_07 = 'yes'",
            "categories": ["where_logic_null"],
        },
        {
            "family_id": "second-family",
            "partition": "hidden",
            "dialect": "generic",
            "schema": "other(id, name)",
            "sql": "SELECT name FROM other WHERE id = 1",
            "categories": ["where_logic_null"],
        },
    ]
    pairs, summary = module._hidden_pairs(records)
    controls = [row for row in pairs if row.get("freeze_role") == "equivalence"]
    assert summary["families_with_equivalence_control"] == 2
    assert {row["family_id"] for row in controls} == {"first-family", "second-family"}


def test_public_freeze_reader_rejects_hidden_partition(tmp_path):
    module = _load(
        "phase1_public_freeze_partition_test",
        "data_construct_test/scripts/run_phase1_public_freeze_pair_regression.py",
    )
    public_path = tmp_path / "public.jsonl"
    public_path.write_text(
        json.dumps(
            {
                "family_id": "hidden-family",
                "partition": "hidden",
                "sql": "SELECT 1",
                "schema": "",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="non-public partition"):
        module._read_public_records(public_path)
