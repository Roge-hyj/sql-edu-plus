"""Audit Phase 1 corpus coverage by teaching category and scenario axis.

This report is intentionally a family-level report.  Duplicate SQL records,
multiple source copies, and multiple mutations of one source question do not
inflate the denominator.  The script reads only development partitions and
rejects hidden input paths so optimization jobs cannot silently train on the
frozen holdout.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
from typing import Any, Iterable, Iterator


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_UNIVERSE = PROJECT_ROOT / "data_construct_test/outputs/phase1_corpus_universe"
SCENARIO_AXES = (
    "base",
    "mutation_ready",
    "null",
    "empty_result",
    "duplicate_candidate",
    "multi_table",
    "boundary_candidate",
    "schema_constraint",
    "paired_mutation",
    "dialect_feature",
)
TARGET_FAMILIES_PER_CATEGORY = 300
TARGET_FAMILIES_PER_SCENARIO_AXIS = 30


def _read_records(paths: Iterable[Path]) -> Iterator[dict[str, Any]]:
    for path in paths:
        if "hidden" in path.name.lower():
            raise ValueError(f"hidden partition is not allowed in capability matrix: {path}")
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"invalid JSON at {path}:{line_number}") from exc
                if not isinstance(record, dict):
                    continue
                yield record


def _record_categories(record: dict[str, Any]) -> list[str]:
    values = record.get("categories") or record.get("cfg_labels") or []
    return sorted({str(value) for value in values if str(value).strip()})


def _record_axes(record: dict[str, Any]) -> list[str]:
    values = (
        record["observed_scenario_axes"]
        if "observed_scenario_axes" in record
        else record.get("scenario_axes") or []
    )
    return sorted({str(value) for value in values if str(value).strip()})


def _record_candidate_axes(record: dict[str, Any]) -> list[str]:
    values = record.get("scenario_axes") or record.get("scenario_candidates") or []
    return sorted({str(value) for value in values if str(value).strip()})


def _record_candidates(record: dict[str, Any]) -> list[str]:
    values = record.get("scenario_candidates") or []
    return sorted({str(value) for value in values if str(value).strip()})


def build_capability_matrix(
    paths: Iterable[Path],
    output: Path,
    *,
    target_families_per_category: int = TARGET_FAMILIES_PER_CATEGORY,
    target_families_per_axis: int = TARGET_FAMILIES_PER_SCENARIO_AXIS,
) -> dict[str, Any]:
    family_records: dict[str, dict[str, Any]] = {}
    duplicate_records = 0
    invalid_records = 0
    for record in _read_records(paths):
        family_id = str(record.get("family_id") or "").strip()
        if not family_id:
            invalid_records += 1
            continue
        if family_id in family_records:
            duplicate_records += 1
            existing = family_records[family_id]
            # A mutation layer intentionally carries one mutation and one
            # equivalence row for the same family.  Keep one family in the
            # denominator, but union execution evidence from both roles so a
            # mutation-only axis is not discarded because the equivalence row
            # happened to be read first.
            for field in ("scenario_axes", "observed_scenario_axes", "verified_scenario_axes", "scenario_candidates"):
                merged = {
                    str(value)
                    for value in (existing.get(field) or [])
                    if str(value).strip()
                }
                merged.update(
                    str(value)
                    for value in (record.get(field) or [])
                    if str(value).strip()
                )
                if merged:
                    existing[field] = sorted(merged)
            existing_categories = {
                str(value) for value in (existing.get("categories") or []) if str(value).strip()
            }
            existing_categories.update(
                str(value) for value in (record.get("categories") or []) if str(value).strip()
            )
            if existing_categories:
                existing["categories"] = sorted(existing_categories)
            continue
        family_records[family_id] = record

    category_families: dict[str, set[str]] = defaultdict(set)
    category_axes: dict[str, Counter[str]] = defaultdict(Counter)
    category_dialects: dict[str, Counter[str]] = defaultdict(Counter)
    category_expectations: dict[str, Counter[str]] = defaultdict(Counter)
    category_sources: dict[str, Counter[str]] = defaultdict(Counter)
    category_replay: dict[str, Counter[str]] = defaultdict(Counter)
    category_candidates: dict[str, Counter[str]] = defaultdict(Counter)
    category_candidate_axes: dict[str, Counter[str]] = defaultdict(Counter)
    all_axes = Counter()
    all_candidates = Counter()
    all_candidate_axes = Counter()
    all_dialects = Counter()
    all_sources = Counter()
    all_partitions = Counter()
    for family_id, record in family_records.items():
        partition = str(record.get("partition") or "unknown")
        all_partitions[partition] += 1
        dialect = str(record.get("dialect") or "generic")
        all_dialects[dialect] += 1
        source = str(record.get("source_id") or "unknown")
        all_sources[source] += 1
        categories = _record_categories(record)
        axes = _record_axes(record)
        candidate_axes = _record_candidate_axes(record)
        candidates = _record_candidates(record)
        for axis in axes:
            all_axes[axis] += 1
        for candidate in candidates:
            all_candidates[candidate] += 1
        for axis in candidate_axes:
            all_candidate_axes[axis] += 1
        for category in categories:
            category_families[category].add(family_id)
            category_expectations[category][str(record.get("expectation") or "unpaired")] += 1
            category_sources[category][source] += 1
            category_dialects[category][dialect] += 1
            category_replay[category]["replay_eligible"] += bool(record.get("replay_eligible"))
            category_replay[category]["replay_ineligible"] += not bool(record.get("replay_eligible"))
            for axis in axes:
                category_axes[category][axis] += 1
            for candidate in candidates:
                category_candidates[category][candidate] += 1
            for axis in candidate_axes:
                category_candidate_axes[category][axis] += 1

    categories = sorted(category_families)
    matrix: dict[str, Any] = {}
    for category in categories:
        count = len(category_families[category])
        axes = {
            axis: {
                "families": category_axes[category][axis],
                "target": target_families_per_axis,
                "met": category_axes[category][axis] >= target_families_per_axis,
            }
            for axis in SCENARIO_AXES
        }
        matrix[category] = {
            "families": count,
            "target_families": target_families_per_category,
            "target_met": count >= target_families_per_category,
            "shortfall": max(0, target_families_per_category - count),
            "scenario_axes": axes,
            "candidate_scenario_axes": {
                axis: {
                    "families": category_candidate_axes[category][axis],
                    "target": target_families_per_axis,
                    "met": category_candidate_axes[category][axis] >= target_families_per_axis,
                }
                for axis in SCENARIO_AXES
            },
            "scenario_candidates": {
                candidate: {
                    "families": category_candidates[category][candidate],
                    "target": target_families_per_axis,
                    "met": category_candidates[category][candidate] >= target_families_per_axis,
                }
                for candidate in sorted(category_candidates[category])
            },
            "dialects": dict(sorted(category_dialects[category].items())),
            "expectations": dict(sorted(category_expectations[category].items())),
            "sources": dict(sorted(category_sources[category].items())),
            "replay": dict(category_replay[category]),
        }

    report = {
        "schema_version": 2,
        "metric_unit": "unique_question_family",
        "target_families_per_category": target_families_per_category,
        "target_families_per_scenario_axis": target_families_per_axis,
        "input_partitions": [str(path) for path in paths],
        "hidden_partition_read": False,
        "unique_families": len(family_records),
        "duplicate_records_ignored": duplicate_records,
        "invalid_records_ignored": invalid_records,
        "partitions": dict(sorted(all_partitions.items())),
        "dialects": dict(sorted(all_dialects.items())),
        "sources": dict(sorted(all_sources.items())),
        "scenario_axes": {
            axis: {"families": all_axes[axis], "target": target_families_per_axis}
            for axis in SCENARIO_AXES
        },
        "candidate_scenario_axes": {
            axis: {"families": all_candidate_axes[axis], "target": target_families_per_axis}
            for axis in SCENARIO_AXES
        },
        "scenario_candidates": {
            candidate: {"families": all_candidates[candidate], "target": target_families_per_axis}
            for candidate in sorted(all_candidates)
        },
        "categories": matrix,
        "all_categories_target_met": bool(categories) and all(
            item["target_met"] for item in matrix.values()
        ),
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return report


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--universe-dir", type=Path, default=DEFAULT_UNIVERSE)
    parser.add_argument("--input", action="append", type=Path, dest="inputs")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--target-families", type=int, default=TARGET_FAMILIES_PER_CATEGORY)
    parser.add_argument("--target-families-per-axis", type=int, default=TARGET_FAMILIES_PER_SCENARIO_AXIS)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    inputs = args.inputs or [
        args.universe_dir / "train.jsonl",
        args.universe_dir / "public.jsonl",
    ]
    report = build_capability_matrix(
        inputs,
        args.output,
        target_families_per_category=args.target_families,
        target_families_per_axis=args.target_families_per_axis,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
