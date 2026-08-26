"""Materialize observed scenario evidence for development records.

This is a bounded SQLite development stage.  It runs each canonical query
against the independent Gold Oracle's generated worlds, then records compact
execution evidence used by the capability matrix.  It never reads a hidden
partition and it never changes implementation code from an execution result.

The output keeps at most one representative world per record.  Each table is
bounded by the oracle's 32-row limit; no native vendor claim is made here.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parents[1]
sys.path.insert(0, str(SCRIPT_DIR))

from phase1_gold_oracle import (  # noqa: E402
    ENGINE_GAP,
    EQUIVALENT,
    INPUT_GAP,
    NOT_EQUIVALENT,
    UNDECIDED,
    run_gold_oracle,
)


NULL_SENSITIVE = re.compile(
    r"(?is)\b(?:IS\s+(?:NOT\s+)?NULL|NOT\s+IN|NOT\s+EXISTS|COALESCE|NULLIF)\b"
)
BOUNDARY = re.compile(
    r"(?is)\b(?:BETWEEN|LIMIT|OFFSET|HAVING|CASE|WHEN)\b|(?:<=|>=|<>|!=|=|<|>)"
)


def _rows_have_duplicate_values(rows: Any) -> tuple[int, list[str]]:
    if not isinstance(rows, list):
        return 0, []
    values: dict[str, list[str]] = defaultdict(list)
    for row in rows:
        if not isinstance(row, dict):
            continue
        for key, value in row.items():
            values[str(key)].append(json.dumps(value, ensure_ascii=False, sort_keys=True, default=repr))
    duplicates = 0
    columns: list[str] = []
    for column, encoded in values.items():
        count = len(encoded) - len(set(encoded))
        if count > 0:
            duplicates += count
            columns.append(column)
    return duplicates, sorted(columns)


def _duplicate_result_rows(rows: Any) -> int:
    if not isinstance(rows, list):
        return 0
    encoded = [json.dumps(row, ensure_ascii=False, sort_keys=True, default=repr) for row in rows]
    return max(0, len(encoded) - len(set(encoded)))


def _catalog_has_constraint(catalog: Any) -> bool:
    if not isinstance(catalog, dict):
        return False
    return any(
        isinstance(table, dict)
        and (table.get("primary_key") or table.get("foreign_keys") or table.get("unique_constraints"))
        for table in catalog.get("tables") or ()
    )


def _physical_table_count(sql: str, schema: str, catalog: Any) -> int:
    physical_names: set[str] = set()
    if isinstance(catalog, dict):
        physical_names.update(
            str(table.get("name") or "").lower()
            for table in catalog.get("tables") or ()
            if isinstance(table, dict) and table.get("name")
        )
    if not physical_names:
        for chunk in str(schema or "").split(";"):
            head, separator, _tail = chunk.partition("(")
            if separator and head.strip():
                physical_names.add(head.strip().strip('"`[]').lower())
    names = re.findall(r"(?is)\b(?:FROM|JOIN)\s+([A-Za-z_][A-Za-z0-9_$]*)", sql)
    return len({name.lower() for name in names if name.lower() in physical_names})


def _safe_world(trial: dict[str, Any]) -> dict[str, Any]:
    return {
        "world_id": trial.get("world_id"),
        "seed": trial.get("seed"),
        "row_scale": trial.get("row_scale"),
        "row_flavour": trial.get("row_flavour"),
        "literal_layout": trial.get("literal_layout"),
        "database": trial.get("database") or {},
        "standard_columns": trial.get("standard_columns") or [],
        "standard_rows": trial.get("standard_rows") or [],
        "standard_digest": trial.get("standard_digest"),
    }


def _materialize_record(
    record: dict[str, Any],
    *,
    seeds: tuple[int, ...],
    scales: tuple[int, ...],
) -> tuple[dict[str, Any], str]:
    sql = str(record.get("sql") or "")
    oracle = run_gold_oracle(
        record.get("schema"),
        sql,
        sql,
        schema_catalog=record.get("schema_catalog"),
        dialect=record.get("dialect"),
        expected=EQUIVALENT,
        seeds=seeds,
        row_scales=scales,
        max_rows_per_table=32,
    )
    trials = oracle.get("trials") or []
    chosen = next((trial for trial in trials if trial.get("database") is not None), None)
    if chosen is None:
        return {
            "executed": False,
            "execution_evidence": {
                "sandbox_executed": False,
                "engine_verdict": oracle.get("verdict") or UNDECIDED,
                "reason": oracle.get("reason") or "no completed bounded SQLite world",
                "trial_count": len(trials),
            },
            "observed_scenario_axes": ["base"],
        }, str(oracle.get("verdict") or UNDECIDED)

    database = chosen.get("database") or {}
    standard_rows = chosen.get("standard_rows") or []
    null_rows = sum(
        1
        for rows in database.values()
        if isinstance(rows, list)
        for row in rows
        if isinstance(row, dict) and any(value is None for value in row.values())
    )
    duplicate_values = 0
    duplicate_columns: set[str] = set()
    for rows in database.values():
        count, columns = _rows_have_duplicate_values(rows)
        duplicate_values += count
        duplicate_columns.update(columns)
    duplicate_result_rows = _duplicate_result_rows(standard_rows)
    null_sensitive = bool(NULL_SENSITIVE.search(sql))
    empty_worlds = sum(1 for trial in trials if not (trial.get("standard_rows") or []))
    referenced_table_count = _physical_table_count(
        sql, str(record.get("schema") or ""), record.get("schema_catalog")
    )
    multi_table = referenced_table_count >= 2
    schema_constraint = _catalog_has_constraint(record.get("schema_catalog")) or bool(
        re.search(
            r"(?is)\b(?:PRIMARY\s+KEY|FOREIGN\s+KEY|UNIQUE|NOT\s+NULL)\b",
            str(record.get("schema") or ""),
        )
    )
    boundary = bool(BOUNDARY.search(sql))
    axes = {"base"}
    evidence: dict[str, Any] = {
        "sandbox_executed": True,
        "engine": "sqlite_bounded_gold_world",
        "engine_verdict": oracle.get("verdict") or UNDECIDED,
        "trial_count": len(trials),
        "representative_world": _safe_world(chosen),
        "null_row_count": null_rows,
        "standard_row_count": len(standard_rows),
        "standard_duplicate_row_count": duplicate_result_rows,
        "duplicate_input_value_count": duplicate_values,
        "duplicate_input_columns": sorted(duplicate_columns),
        "empty_result_world_count": empty_worlds,
        "referenced_table_count": referenced_table_count,
        "constraint_evidence": schema_constraint,
        "boundary_expression_present": boundary,
    }
    if null_sensitive and null_rows:
        axes.add("null")
        evidence["null_evidence"] = {"query_null_sensitive": True, "database_null_rows": null_rows}
    if empty_worlds:
        axes.add("empty_result")
        evidence["empty_result_evidence"] = {"world_count": empty_worlds}
    if duplicate_values or duplicate_result_rows:
        axes.add("duplicate_candidate")
        evidence["duplicate_evidence"] = {
            "input_value_duplicates": duplicate_values,
            "result_row_duplicates": duplicate_result_rows,
        }
    if multi_table:
        axes.add("multi_table")
    if boundary:
        axes.add("boundary_candidate")
        evidence["boundary_evidence"] = {"query_boundary_expression_executed": True}
    if schema_constraint:
        axes.add("schema_constraint")
    return {
        "executed": True,
        "test_database": database,
        "standard_rows": standard_rows,
        "standard_row_count": len(standard_rows),
        "execution_evidence": evidence,
        "boundary_evidence": evidence.get("boundary_evidence"),
        "observed_scenario_axes": sorted(axes),
    }, str(oracle.get("verdict") or UNDECIDED)


def materialize(source: Path, destination: Path, *, seeds: tuple[int, ...], scales: tuple[int, ...]) -> dict[str, Any]:
    if source.name.lower() == "hidden.jsonl" or "hidden" in source.name.lower():
        raise ValueError("scenario materializer refuses hidden input paths")
    counters = Counter()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with source.open("r", encoding="utf-8") as reader, destination.open("w", encoding="utf-8", newline="\n") as writer:
        for line_number, line in enumerate(reader, start=1):
            if not line.strip():
                continue
            record = json.loads(line)
            if not isinstance(record, dict):
                continue
            if str(record.get("partition") or "").lower() == "hidden":
                raise ValueError(f"hidden record at {source}:{line_number}")
            evidence, verdict = _materialize_record(record, seeds=seeds, scales=scales)
            record.update(evidence)
            counters[verdict] += 1
            writer.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    return {
        "source": str(source),
        "destination": str(destination),
        "oracle_seeds": list(seeds),
        "row_scales": list(scales),
        "max_rows_per_table": 32,
        "records": sum(counters.values()),
        "verdicts": dict(sorted(counters.items())),
        "hidden_partition_read": False,
        "engine": "SQLite bounded development materializer",
    }


def _ints(value: str) -> tuple[int, ...]:
    result = tuple(int(item.strip()) for item in value.split(",") if item.strip())
    if not result:
        raise ValueError("at least one integer is required")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--oracle-seeds", default="0")
    parser.add_argument("--row-scales", default="8")
    args = parser.parse_args()
    print(json.dumps(materialize(args.input, args.output, seeds=_ints(args.oracle_seeds), scales=_ints(args.row_scales)), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
