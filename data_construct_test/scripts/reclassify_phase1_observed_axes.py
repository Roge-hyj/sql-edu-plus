"""Recompute cheap observed-axis labels from already executed public audits."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re


def _axes(row: dict) -> set[str]:
    axes = set(str(value) for value in row.get("observed_scenario_axes") or [])
    sql = f"{row.get('standard_sql') or ''} {row.get('student_sql') or ''}"
    structure = row.get("structure") or {}
    diff_types = {
        str(item.get("diff_type") or "")
        for item in structure.get("ast_diffs") or []
        if isinstance(item, dict)
    }
    if re.search(
        r"(?is)\b(?:BETWEEN|LIMIT|OFFSET|HAVING|CASE|WHEN)\b|(?:<=|>=|<>|!=|=|<|>)",
        sql,
    ) or diff_types & {
        "comparison_operator_changed",
        "logical_operator_changed",
        "limit_changed",
        "offset_changed",
        "having_changed",
        "order_direction_changed",
        "case_changed",
        "case_when_missing",
        "case_else_missing",
    }:
        axes.add("boundary_candidate")
    tables = {
        match.group(1).lower()
        for match in re.finditer(
            r"(?is)\b(?:FROM|JOIN)\s+([A-Za-z_][A-Za-z0-9_$]*)",
            sql,
        )
    }
    if len(tables) >= 2:
        axes.add("multi_table")
    schema = str(row.get("schema") or "")
    if re.search(r"(?is)\b(?:PRIMARY\s+KEY|FOREIGN\s+KEY|UNIQUE|NOT\s+NULL)\b", schema):
        axes.add("schema_constraint")
    verdict = str((row.get("oracle") or {}).get("verdict") or "")
    dialect = str(row.get("dialect") or "generic").lower()
    if dialect not in {"generic", "standard"} and verdict not in {"ENGINE_GAP", "INPUT_GAP"}:
        axes.add("dialect_feature")
    return axes


def reclassify(source: Path, output: Path) -> dict:
    rows = []
    with source.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            row["observed_scenario_axes"] = sorted(_axes(row))
            rows.append(row)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    return {"source": str(source), "output": str(output), "rows": len(rows), "hidden_partition_read": False}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    print(json.dumps(reclassify(args.input, args.output), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
