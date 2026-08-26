"""Build deterministic, explicitly labelled Phase 1 hard-case fixtures.

These fixtures are not presented as web or textbook observations.  They fill
small capability strata that the public sources do not contain in sufficient
quantity, while keeping every generated question family and mutation pair
reproducible.  They must remain a separate source stratum in reports.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT = PROJECT_ROOT / "data_construct_test/outputs/phase1_hand_authored_fixtures.jsonl"


def _record(index: int, kind: str) -> dict[str, object]:
    table = f"phase1_{kind}_{index:04d}"
    threshold = 10 + (index % 17)
    # The table suffix and numeric threshold are execution parameters of one
    # mutation template, not independent teaching questions.  Keep them in
    # the raw record for replay, while assigning one explicit lineage key per
    # semantic template so the corpus builder can collapse the variants.
    lineage_family_id = (
        "phase1.case.score_bucket_boundary"
        if kind == "case"
        else "phase1.cte.recursive_integer_sequence"
    )
    base = {
        "record_id": f"hand_authored_{kind}_{index:04d}",
        "source_id": "phase1_hand_authored_synthetic_fixture",
        "source_name": "Phase 1 hand-authored synthetic hard cases",
        "source_kind": "hand_authored_synthetic_fixture",
        "source_url": "repo://phase1/hand-authored-synthetic-fixtures",
        "captured_at": "2026-08-21T00:00:00Z",
        "lineage_family_id": lineage_family_id,
        "lineage_policy": "parameterized_execution_variant_of_one_semantic_template",
        "lineage_parameters": {
            "template_index": index,
            "threshold_or_limit": threshold if kind == "case" else 3 + (index % 7),
        },
        "dialect": "sqlite",
        "schema_trust": "source_declared",
        "replay_eligible": True,
        "expectation": "not_equivalent",
        "attack_kind": "hand_authored_boundary_mutation",
        "verified_scenario_axes": ["base", "boundary_candidate", "paired_mutation", "mutation_ready", "schema_constraint"],
    }
    if kind == "case":
        standard = (
            f"SELECT id, CASE WHEN score >= {threshold} THEN 'pass' ELSE 'fail' END AS bucket "
            f"FROM {table}"
        )
        student = (
            f"SELECT id, CASE WHEN score > {threshold} THEN 'pass' ELSE 'fail' END AS bucket "
            f"FROM {table}"
        )
        return {
            **base,
            "schema": f"{table}(id INT PRIMARY KEY, score INT NOT NULL)",
            "sql": standard,
            "student_sql": student,
            "raw_text": standard,
            "cfg_labels": ["select-basic", "case", "where-comp"],
            "scenario_candidates": ["boundary_candidate", "schema_constraint", "paired_mutation"],
        }
    recursive_limit = 3 + (index % 7)
    standard = (
        f"WITH RECURSIVE nums(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM nums WHERE n < {recursive_limit}) "
        "SELECT n FROM nums"
    )
    student = (
        f"WITH RECURSIVE nums(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM nums WHERE n <= {recursive_limit}) "
        "SELECT n FROM nums"
    )
    return {
        **base,
        "schema": f"{table}(id INT PRIMARY KEY, marker INT)",
        "sql": standard,
        "student_sql": student,
        "raw_text": standard,
        "cfg_labels": ["select-basic", "cte", "cte-recursive", "union", "where-comp"],
        "scenario_candidates": ["boundary_candidate", "schema_constraint", "paired_mutation"],
    }


def build(output: Path, *, count: int = 400) -> dict[str, object]:
    if count <= 0:
        raise ValueError("count must be positive")
    output.parent.mkdir(parents=True, exist_ok=True)
    records = [_record(index, kind) for kind in ("case", "cte") for index in range(count)]
    with output.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    return {
        "output": str(output),
        "source_id": "phase1_hand_authored_synthetic_fixture",
        "count_per_kind": count,
        "total_records": len(records),
        "kinds": ["case", "cte"],
        "policy": "synthetic fixtures are reported separately from external source families",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--count", type=int, default=400)
    args = parser.parse_args()
    print(json.dumps(build(args.output, count=args.count), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
