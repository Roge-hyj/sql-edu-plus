from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = PROJECT_ROOT / "data_construct_test/scripts/run_phase1_gold_oracle_audit.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("phase1_gold_oracle_audit", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write(path: Path, records: list[dict[str, object]]) -> None:
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")


def test_audit_is_bounded_and_keeps_undecided_separate(tmp_path):
    module = _load_module()
    train = tmp_path / "train.jsonl"
    public = tmp_path / "public.jsonl"
    _write(
        train,
        [
            {
                "family_id": "neq",
                "record_id": "neq",
                "partition": "train",
                "source_id": "fixture",
                "dialect": "generic",
                "categories": ["where_logic_null"],
                "expectation": "not_equivalent",
                "schema": "users(id INT PRIMARY KEY, salary INT)",
                "sql": "SELECT id FROM users WHERE salary > 3",
                "student_sql": "SELECT id FROM users WHERE salary >= 3",
            },
            {
                "family_id": "eq",
                "record_id": "eq",
                "partition": "public",
                "source_id": "fixture",
                "dialect": "generic",
                "categories": ["select_projection"],
                "expectation": "equivalent",
                "schema": "users(id INT PRIMARY KEY)",
                "sql": "SELECT id FROM users",
                "student_sql": "SELECT id FROM users",
            },
        ],
    )
    _write(public, [{"family_id": "unpaired", "partition": "public", "sql": "SELECT 1"}])
    output = tmp_path / "audit.jsonl"
    summary_path = tmp_path / "summary.json"

    summary = module.audit(
        [train, public],
        output,
        summary_path,
        max_pairs=2,
        sample_seed=3,
        oracle_seeds=(0,),
        row_scales=(4,),
        max_rows_per_table=8,
    )

    assert summary["hidden_partition_read"] is False
    assert summary["selected_pairs"] == 2
    assert summary["verdicts"][module.NOT_EQUIVALENT] == 1
    assert summary["verdicts"][module.EQUIVALENT] == 1
    assert summary["quality"]["structure_bound_rate"] == 1.0
    assert summary["quality"]["atomic_obligation_coverage_rate"] == 1.0
    assert summary["quality"]["labelled_verdict_match_count"] == 2
    assert len(output.read_text(encoding="utf-8").splitlines()) == 2
    audited = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    neq = next(item for item in audited if item["family_id"] == "neq")
    assert neq["structure"]["status"] == "BOUND"
    assert neq["structure"]["ast_diffs"]
    assert neq["structure"]["obligations"]
    assert {
        row["diff_id"] for row in neq["structure"]["ast_diffs"]
    } >= {
        row["diff_id"] for row in neq["structure"]["obligations"]
    }
    assert all(
        entry["validator"] == "NOT_RUN_IN_GOLD_AUDIT"
        for entry in neq["structure"]["evidence_chain"]
    )
    assert "observed_scenario_axes" in neq
    assert "paired_mutation" in neq["observed_scenario_axes"]
    assert "boundary_candidate" in neq["observed_scenario_axes"]
    assert "null" not in neq["observed_scenario_axes"]


def test_observed_null_axis_requires_null_sensitive_query():
    module = _load_module()
    structure = {"ast_diffs": [], "obligations": []}
    oracle = {
        "trials": [{"database": {"t": [{"value": None}]}, "standard_rows": [[1]], "student_rows": [[1]]}]
    }
    item = {
        "dialect": "generic",
        "standard_sql": "SELECT value FROM t",
        "student_sql": "SELECT value FROM t",
        "schema_catalog": None,
    }
    assert "null" not in module._observed_axes(item, structure, oracle)


def test_gold_oracle_materializes_public_aggregate_and_boundary_mutations():
    module = _load_module()
    cases = [
        (
            "wikisql_2_1031262_1(year, wins, losses, percentage, finish);",
            "SELECT MAX(losses) FROM wikisql_2_1031262_1 WHERE percentage > 0.461 AND wins > 86 AND finish = '2nd' AND year = 1953",
            "SELECT MAX(losses) FROM wikisql_2_1031262_1 WHERE percentage >= 0.461 AND wins > 86 AND finish = '2nd' AND year = 1953",
        ),
        (
            "wikisql_2_10640687_18(home_team, home_team_score, away_team, away_team_score, venue, crowd, date);",
            "SELECT SUM(crowd) FROM wikisql_2_10640687_18 WHERE venue = 'princes park'",
            "SELECT AVG(crowd) FROM wikisql_2_10640687_18 WHERE venue = 'princes park'",
        ),
        (
            "wikisql_2_13599687_24(driver, seasons, entries, poles, percentage);",
            "SELECT SUM(poles) FROM wikisql_2_13599687_24 WHERE percentage = '22.08%'",
            "SELECT AVG(poles) FROM wikisql_2_13599687_24 WHERE percentage = '22.08%'",
        ),
        (
            "wikisql_1_174266_6(year, total, less_than_a_year, one_year, two_years, three_years, four_years, 5_9_years, 10_14_years, 15_19_years, 20_24_years, 25_and_more, unknown);",
            "SELECT MAX(two_years) FROM wikisql_1_174266_6 WHERE unknown > 1.0",
            "SELECT MAX(two_years) FROM wikisql_1_174266_6 WHERE unknown >= 1.0",
        ),
    ]
    for schema, standard_sql, student_sql in cases:
        result = module.run_gold_oracle(
            schema,
            standard_sql,
            student_sql,
            dialect="generic",
            expected="not_equivalent",
            seeds=(0,),
            row_scales=(4,),
        )
        assert result["verdict"] == module.NOT_EQUIVALENT
        assert result["trials"]


def test_audit_rejects_hidden_input_before_execution(tmp_path):
    module = _load_module()
    hidden = tmp_path / "hidden.jsonl"
    _write(
        hidden,
        [{
            "family_id": "hidden",
            "partition": "hidden",
            "schema": "users(id INT)",
            "sql": "SELECT id FROM users",
            "student_sql": "SELECT id FROM users",
        }],
    )

    try:
        module.audit(
            [hidden],
            tmp_path / "audit.jsonl",
            tmp_path / "summary.json",
            max_pairs=1,
            sample_seed=0,
            oracle_seeds=(0,),
            row_scales=(4,),
            max_rows_per_table=8,
        )
    except ValueError as exc:
        assert "hidden" in str(exc).lower()
    else:
        raise AssertionError("hidden input must be rejected")
