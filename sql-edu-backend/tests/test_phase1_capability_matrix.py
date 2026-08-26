from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = (
    PROJECT_ROOT / "data_construct_test/scripts/build_phase1_capability_matrix.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "build_phase1_capability_matrix",
        SCRIPT_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write(path: Path, records: list[dict[str, object]]) -> None:
    path.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )


def test_capability_matrix_counts_families_and_axes_without_hidden_input(tmp_path):
    module = _load_module()
    train = tmp_path / "train.jsonl"
    public = tmp_path / "public.jsonl"
    records = [
        {
            "family_id": "family-a",
            "partition": "train",
            "source_id": "book",
            "dialect": "sqlite",
            "categories": ["where_logic_null"],
            "scenario_axes": ["base", "null", "mutation_ready"],
            "observed_scenario_axes": ["base"],
            "expectation": "equivalent",
            "replay_eligible": True,
        },
        {
            "family_id": "family-b",
            "partition": "train",
            "source_id": "spider",
            "dialect": "postgres",
            "categories": ["join_outer_on"],
            "scenario_axes": ["base", "multi_table", "schema_constraint"],
            "expectation": "not_equivalent",
            "replay_eligible": False,
        },
        {
            "family_id": "family-a",
            "partition": "public",
            "source_id": "duplicate",
            "dialect": "sqlite",
            "categories": ["where_logic_null"],
            "scenario_axes": ["base"],
        },
    ]
    _write(train, records[:2])
    _write(public, [records[2]])

    report = module.build_capability_matrix(
        [train, public],
        tmp_path / "matrix.json",
        target_families_per_category=2,
    )

    assert report["metric_unit"] == "unique_question_family"
    assert report["unique_families"] == 2
    assert report["duplicate_records_ignored"] == 1
    assert report["hidden_partition_read"] is False
    where = report["categories"]["where_logic_null"]
    assert where["families"] == 1
    assert where["shortfall"] == 1
    assert where["scenario_axes"]["null"]["families"] == 0
    assert where["scenario_axes"]["base"]["families"] == 1
    assert report["categories"]["join_outer_on"]["replay"]["replay_ineligible"] == 1


def test_capability_matrix_rejects_hidden_partition(tmp_path):
    module = _load_module()
    hidden = tmp_path / "hidden.jsonl"
    _write(hidden, [{"family_id": "hidden-family", "categories": ["case"]}])

    try:
        module.build_capability_matrix([hidden], tmp_path / "matrix.json")
    except ValueError as exc:
        assert "hidden partition" in str(exc)
    else:
        raise AssertionError("hidden input must be rejected")
