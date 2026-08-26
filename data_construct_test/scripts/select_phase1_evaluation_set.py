"""Stratified selection of the Phase 1 evaluation set.

The acceptance plan requires, per core category, at least 300 independent
question families for the NEQ detection lower bound and roughly 2,995
equivalence-preserved rows for the false-positive ceiling, plus >=30
families per scenario axis.  This script reads the mutation-layer evaluation
rows and the synthesized teaching families, then emits a bounded stratified
selection that satisfies those targets where the corpus allows.

Selection is family-stratified: one mutation row and one equivalence row per
family, so the family denominator is never inflated.  Hidden discipline is
preserved: hidden partitions are forbidden on input and never emitted.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable, Iterator


PROJECT_ROOT = Path(__file__).resolve().parents[2]

TARGET_FAMILIES_PER_CATEGORY = 300
TARGET_EQUIVALENCE_ROWS = 2995
TARGET_FAMILIES_PER_AXIS = 30

CORE_CATEGORIES = (
    "select_projection",
    "where_logic_null",
    "in_between_like",
    "join_outer_on",
    "group_having_aggregate",
    "distinct_order_limit",
    "set_operations",
    "subqueries_correlation",
    "cte_recursive",
    "case",
    "window_functions",
    "dialect_features",
)

SCENARIO_AXES = (
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
)


def _iter_rows(paths: Iterable[Path]) -> Iterator[dict[str, Any]]:
    for path in paths:
        if "hidden" in path.name.lower():
            raise ValueError(f"hidden input is forbidden: {path}")
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                record = json.loads(line)
                if not isinstance(record, dict):
                    continue
                if str(record.get("partition") or "").lower() == "hidden":
                    raise ValueError(f"hidden record is forbidden: {path}:{line_number}")
                yield record


def _categories(record: dict[str, Any]) -> list[str]:
    values = record.get("categories") or []
    return sorted({str(value) for value in values if str(value).strip()})


def _axes(record: dict[str, Any]) -> list[str]:
    # Axis stratification is an observed-coverage claim.  Candidate/template
    # axes remain available in the row but cannot satisfy this denominator.
    values = record.get("observed_scenario_axes") or []
    return sorted({str(value) for value in values if str(value).strip()})


def _pick_index(family_id: str, salt: str, size: int) -> int:
    digest = hashlib.sha256(f"{salt}\0{family_id}".encode("utf-8")).hexdigest()
    return int(digest[:16], 16) % size


def select(
    inputs: list[Path],
    output: Path,
    *,
    target_per_category: int = TARGET_FAMILIES_PER_CATEGORY,
    target_equivalence: int = TARGET_EQUIVALENCE_ROWS,
    target_per_axis: int = TARGET_FAMILIES_PER_AXIS,
    salt: str = "phase1-evaluation-set-v1",
) -> dict[str, Any]:
    # Bucket by (category, role) so we can pull families per category while
    # keeping one mutation + one equivalence row per family.
    by_category_role: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    by_axis: dict[str, list[dict[str, Any]]] = defaultdict(list)
    equivalence_pool: list[dict[str, Any]] = []
    seen_families: set[str] = set()
    total = 0

    for record in _iter_rows(inputs):
        if not record.get("family_id") or not record.get("sql") or not record.get("student_sql"):
            continue
        family_id = str(record["family_id"])
        role = str(record.get("mutation_layer_role") or "")
        if role not in {"mutation", "equivalence"}:
            continue
        total += 1
        seen_families.add(family_id)
        for category in _categories(record):
            by_category_role[(category, role)].append(record)
        for axis in _axes(record):
            by_axis[axis].append(record)
        if role == "equivalence":
            equivalence_pool.append(record)

    selected: list[dict[str, Any]] = []
    selected_families: set[str] = set()
    category_stats: dict[str, dict[str, int]] = {}
    per_axis_kept: dict[str, int] = {}

    # Primary stratum: reserve rare categories first.  A family may belong to
    # several categories, so the old fixed order could consume all dialect
    # families while selecting an earlier, much larger category.
    category_order = sorted(
        CORE_CATEGORIES,
        key=lambda category: (
            len(by_category_role.get((category, "mutation"), [])),
            CORE_CATEGORIES.index(category),
        ),
    )
    for category in category_order:
        candidates = by_category_role.get((category, "mutation"), [])
        # Deterministic order so re-runs are reproducible.
        ordered = sorted(candidates, key=lambda r: str(r.get("family_id") or ""))
        kept = 0
        for record in ordered:
            if kept >= target_per_category:
                break
            family_id = str(record["family_id"])
            if family_id in selected_families:
                continue
            selected.append(record)
            selected_families.add(family_id)
            kept += 1
        category_stats[category] = {"mutation_families": kept, "target": target_per_category}

    # Equivalence stratum: a bounded pool across all categories for the FP
    # ceiling.  Draw deterministically until the target is met.
    eq_ordered = sorted(equivalence_pool, key=lambda r: str(r.get("family_id") or ""))
    eq_kept = 0
    for record in eq_ordered:
        if eq_kept >= target_equivalence:
            break
        family_id = str(record["family_id"])
        if family_id in selected_families:
            continue
        selected.append(record)
        selected_families.add(family_id)
        eq_kept += 1

    # Scenario-axis top-up: ensure >= target_per_axis families per axis where
    # the corpus has enough candidates.  Axis rows are pulled from the full
    # candidate pool, not just the primary stratum.
    for axis in SCENARIO_AXES:
        candidates = by_axis.get(axis, [])
        ordered = sorted(candidates, key=lambda r: str(r.get("family_id") or ""))
        kept = sum(1 for r in ordered if str(r["family_id"]) in selected_families)
        for record in ordered:
            if kept >= target_per_axis:
                break
            family_id = str(record["family_id"])
            if family_id in selected_families:
                continue
            selected.append(record)
            selected_families.add(family_id)
            kept += 1
        per_axis_kept[axis] = kept

    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="\n") as handle:
        for record in sorted(selected, key=lambda r: (str(r.get("family_id") or ""), str(r.get("mutation_layer_role") or ""))):
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")

    category_shortfalls = {
        category: max(
            0,
            target_per_category
            - category_stats[category]["mutation_families"],
        )
        for category in CORE_CATEGORIES
    }
    if any(category_shortfalls.values()):
        print(
            "warning: evaluation category shortfalls: "
            + json.dumps(category_shortfalls, sort_keys=True),
        )
    return {
        "schema_version": 1,
        "generator": "select_phase1_evaluation_set",
        "inputs": [str(path) for path in inputs],
        "output": str(output),
        "hidden_partition_read": False,
        "total_input_rows": total,
        "total_input_families": len(seen_families),
        "selected_rows": len(selected),
        "selected_families": len(selected_families),
        "target_per_category": target_per_category,
        "target_equivalence_rows": target_equivalence,
        "target_per_axis": target_per_axis,
        "by_category": category_stats,
        "category_shortfalls": category_shortfalls,
        "by_axis": {axis: {"families": per_axis_kept[axis], "target": target_per_axis} for axis in SCENARIO_AXES},
        "equivalence_rows_selected": eq_kept,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", action="append", type=Path, dest="inputs", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--target-per-category", type=int, default=TARGET_FAMILIES_PER_CATEGORY)
    parser.add_argument("--target-equivalence", type=int, default=TARGET_EQUIVALENCE_ROWS)
    parser.add_argument("--target-per-axis", type=int, default=TARGET_FAMILIES_PER_AXIS)
    parser.add_argument("--salt", default="phase1-evaluation-set-v1")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    summary = select(
        args.inputs,
        args.output,
        target_per_category=args.target_per_category,
        target_equivalence=args.target_equivalence,
        target_per_axis=args.target_per_axis,
        salt=args.salt,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
