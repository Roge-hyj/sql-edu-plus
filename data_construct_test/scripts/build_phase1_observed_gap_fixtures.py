"""Build reproducible public SQLite fixtures for observed-axis gaps.

These are explicitly labelled synthetic families.  They supplement only the
small public gaps left by the external snapshot and remain a separate source
stratum in every report.  Candidate axes describe why a row was selected; only
the independent Gold audit may populate observed axes later.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT = PROJECT_ROOT / "data_construct_test/outputs/phase1_observed_gap_fixtures_v7.jsonl"
SEED = 20260821
PUBLIC_LOW = 0.70
PUBLIC_HIGH = 0.85

ALL_CANDIDATE_AXES = [
    "base",
    "null",
    "empty_result",
    "duplicate_candidate",
    "multi_table",
    "boundary_candidate",
    "schema_constraint",
    "dialect_feature",
    "mutation_ready",
    "paired_mutation",
]

KINDS = (
    "case_sqlite",
    "cte_sqlite",
    "set_sqlite",
    "window_sqlite",
    "group_duplicate",
    "select_projection",
    "where_logic_null",
    "in_between_like",
    "join_outer_on",
    "distinct_order_limit",
    "subqueries_correlation",
    "dialect_features",
)

CATEGORIES = {
    "case_sqlite": ["case"],
    "cte_sqlite": ["cte_recursive"],
    "set_sqlite": ["set_operations"],
    "window_sqlite": ["window_functions"],
    "group_duplicate": ["group_having_aggregate"],
    "select_projection": ["select_projection"],
    "where_logic_null": ["where_logic_null"],
    "in_between_like": ["in_between_like"],
    "join_outer_on": ["join_outer_on"],
    "distinct_order_limit": ["distinct_order_limit"],
    "subqueries_correlation": ["subqueries_correlation"],
    "dialect_features": ["dialect_features"],
}


def _family_id(lineage: str) -> str:
    return hashlib.sha256(f"lineage\0{lineage}".encode("utf-8")).hexdigest()


def _is_public(family_id: str) -> bool:
    digest = hashlib.sha256(f"{SEED}:{family_id}".encode("utf-8")).digest()
    bucket = int.from_bytes(digest[:8], "big") / float(1 << 64)
    return PUBLIC_LOW <= bucket < PUBLIC_HIGH


def _query_pair(kind: str, index: int, table: str) -> tuple[str, str, str]:
    threshold = 5 + index % 23
    if kind == "case_sqlite":
        standard = f"SELECT id, CASE WHEN score >= {threshold} THEN printf('%02d', score) ELSE 'low' END AS bucket FROM {table}"
        student = f"SELECT id, CASE WHEN score > {threshold} THEN printf('%02d', score) ELSE 'low' END AS bucket FROM {table}"
        return standard, student, f"{table}(id INT PRIMARY KEY, score INT NOT NULL)"
    if kind == "cte_sqlite":
        limit = 3 + index % 19
        standard = f"WITH RECURSIVE nums(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM nums WHERE n < {limit}) SELECT n FROM nums"
        student = f"WITH RECURSIVE nums(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM nums WHERE n <= {limit}) SELECT n FROM nums"
        return standard, student, f"{table}(id INT PRIMARY KEY, marker INT)"
    if kind == "set_sqlite":
        standard = f"SELECT name COLLATE NOCASE FROM {table} UNION ALL SELECT name COLLATE NOCASE FROM {table}"
        student = f"SELECT name COLLATE NOCASE FROM {table} UNION SELECT name COLLATE NOCASE FROM {table}"
        return standard, student, f"{table}(id INT PRIMARY KEY, name TEXT)"
    if kind == "window_sqlite":
        standard = f"SELECT grp, score, ROW_NUMBER() OVER (PARTITION BY grp ORDER BY score) AS rn FROM {table} WHERE score IS NULL OR score IS NOT NULL"
        student = f"SELECT grp, score, ROW_NUMBER() OVER (ORDER BY score) AS rn FROM {table} WHERE score IS NULL OR score IS NOT NULL"
        return standard, student, f"{table}(id INT PRIMARY KEY, grp TEXT, score INT)"
    if kind == "group_duplicate":
        standard = f"SELECT 1 AS bucket FROM {table} GROUP BY score HAVING COUNT(*) >= 1"
        student = f"SELECT 1 AS bucket FROM {table} GROUP BY score HAVING COUNT(*) >= 2"
        return standard, student, f"{table}(id INT PRIMARY KEY, score INT, marker TEXT)"
    if kind == "select_projection":
        # The generated primary-key sentinel is 1, so this boundary is
        # guaranteed to expose the removed predicate in the bounded Gold world.
        standard = f"SELECT value FROM {table} WHERE id >= 1"
        student = f"SELECT value FROM {table} WHERE id > 1"
        return standard, student, f"{table}(id INT PRIMARY KEY, value TEXT)"
    if kind == "where_logic_null":
        standard = f"SELECT id FROM {table} WHERE value IS NULL AND value IS NOT NULL"
        student = f"SELECT id FROM {table} WHERE value IS NULL OR value IS NOT NULL"
        return standard, student, f"{table}(id INT PRIMARY KEY, value TEXT)"
    if kind == "in_between_like":
        # Primary-key rows 1 and 2 are always present; narrowing the BETWEEN
        # upper bound therefore remains observable without relying on NULL
        # placement in a randomly generated world.
        standard = f"SELECT id FROM {table} WHERE id BETWEEN 1 AND 2"
        student = f"SELECT id FROM {table} WHERE id BETWEEN 1 AND 1"
        return standard, student, f"{table}(id INT PRIMARY KEY, value TEXT)"
    if kind == "join_outer_on":
        # Join on a non-key column so the independent oracle can plant an
        # unmatched driving row, then project the nullable side to expose the
        # LEFT-vs-INNER difference.
        standard = f"SELECT a.id, b.id FROM {table}_a AS a LEFT JOIN {table}_b AS b ON a.value = b.value"
        student = f"SELECT a.id, b.id FROM {table}_a AS a INNER JOIN {table}_b AS b ON a.value = b.value"
        return standard, student, f"{table}_a(id INT PRIMARY KEY, value TEXT); {table}_b(id INT PRIMARY KEY, value TEXT)"
    if kind == "distinct_order_limit":
        standard = f"SELECT value FROM {table} WHERE value IS NULL OR value IS NOT NULL ORDER BY value LIMIT 1"
        student = f"SELECT value FROM {table} WHERE value IS NULL OR value IS NOT NULL ORDER BY value LIMIT 2"
        return standard, student, f"{table}(id INT PRIMARY KEY, value TEXT)"
    if kind == "subqueries_correlation":
        standard = f"SELECT id FROM {table} WHERE id IN (SELECT id FROM {table}_lookup)"
        student = f"SELECT id FROM {table} WHERE id NOT IN (SELECT id FROM {table}_lookup)"
        return standard, student, f"{table}(id INT PRIMARY KEY, value TEXT); {table}_lookup(id INT PRIMARY KEY, value TEXT)"
    standard = f"SELECT printf('%02d', id) FROM {table} WHERE id >= 0"
    student = f"SELECT printf('%03d', id) FROM {table} WHERE id >= 0"
    return standard, student, f"{table}(id INT PRIMARY KEY, value TEXT)"


def _record(kind: str, index: int) -> dict[str, object]:
    table = f"gap_{kind}_{index:04d}"
    lineage = f"phase1.observed_gap.{kind}.family_{index:04d}"
    family_id = _family_id(lineage)
    standard, student, schema = _query_pair(kind, index, table)
    return {
        "record_id": f"observed_gap_{kind}_{index:04d}",
        "family_id": family_id,
        "lineage_family_id": lineage,
        "family_identity": "explicit_lineage",
        "partition": "public",
        "source_id": "phase1_observed_gap_synthetic_fixture",
        "source_name": "Phase 1 public observed-axis gap fixtures",
        "source_kind": "hand_authored_synthetic_fixture",
        "source_url": "repo://phase1/observed-axis-gap-fixtures",
        "captured_at": "2026-08-21T00:00:00Z",
        "dialect": "sqlite",
        "schema_trust": "source_declared",
        "replay_eligible": True,
        "categories": CATEGORIES[kind],
        "labels": ["select-basic"],
        "scenario_candidates": list(ALL_CANDIDATE_AXES),
        "expectation": "not_equivalent",
        "attack_kind": "hand_authored_observed_axis_gap",
        "schema": schema,
        "sql": standard,
        "student_sql": student,
        "raw_text": standard,
    }


def build(output: Path, target_per_kind: int = 40) -> dict[str, object]:
    if target_per_kind <= 0:
        raise ValueError("target_per_kind must be positive")
    records: list[dict[str, object]] = []
    counts = {kind: 0 for kind in KINDS}
    indices = {kind: 0 for kind in KINDS}
    while any(count < target_per_kind for count in counts.values()):
        for kind in KINDS:
            if counts[kind] >= target_per_kind:
                continue
            index = indices[kind]
            indices[kind] += 1
            row = _record(kind, index)
            if _is_public(str(row["family_id"])):
                records.append(row)
                counts[kind] += 1
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="\n") as handle:
        for row in records:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    return {
        "output": str(output),
        "total_records": len(records),
        "target_per_kind": target_per_kind,
        "kinds": list(KINDS),
        "counts": counts,
        "partition_policy": "same deterministic public hash as build_phase1_corpus_universe",
        "hidden_partition_read": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--target-per-kind", type=int, default=40)
    args = parser.parse_args(argv)
    print(json.dumps(build(args.output, args.target_per_kind), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
