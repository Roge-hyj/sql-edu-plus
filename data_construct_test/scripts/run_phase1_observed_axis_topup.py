"""Top up observed Phase 1 scenario axes from the public mutation layer.

The ordinary evaluation audit is intentionally bounded.  This command fills
only capability-matrix axis shortfalls using train/public records whose source
metadata already marks an axis as a candidate, then verifies the candidate by
running the independent Gold Oracle.  Candidate metadata is never copied into
``observed_scenario_axes`` without an execution result.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import contextlib
import json
import io
from pathlib import Path
import sys
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parents[1]
sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(PROJECT_ROOT / "sql-edu-backend"))

from phase1_gold_oracle import run_gold_oracle  # noqa: E402
from run_phase1_gold_oracle_audit import (  # noqa: E402
    _observed_axes,
    _safe_record,
    _structure_evidence,
)


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
AXES = (
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


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if "hidden" in path.name.lower():
        raise ValueError(f"hidden input is forbidden: {path}")
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            record = json.loads(line)
            if not isinstance(record, dict):
                continue
            if str(record.get("partition") or "").lower() == "hidden":
                raise ValueError(f"hidden record is forbidden: {path}:{line_number}")
            rows.append(record)
    return rows


def _role(record: dict[str, Any]) -> str:
    return str(record.get("mutation_layer_role") or "")


def _candidate_axes(record: dict[str, Any]) -> set[str]:
    values = record.get("scenario_candidates") or record.get("scenario_axes") or []
    return {str(value) for value in values if str(value).strip() in AXES}


def _key(record: dict[str, Any]) -> tuple[str, str]:
    return (str(record.get("family_id") or ""), _role(record))


def _load_existing(path: Path) -> tuple[list[dict[str, Any]], dict[str, set[str]]]:
    rows = _read_jsonl(path)
    observed: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        family = str(row.get("family_id") or "")
        for category in row.get("categories") or []:
            for axis in row.get("observed_scenario_axes") or []:
                observed[f"{category}\0{axis}"].add(family)
    return rows, observed


def _deficits(observed: dict[str, set[str]], target: int) -> set[tuple[str, str]]:
    return {
        (category, axis)
        for category in CORE_CATEGORIES
        for axis in AXES
        if len(observed.get(f"{category}\0{axis}", set())) < target
    }


def _select_candidates(
    candidates: list[dict[str, Any]],
    observed: dict[str, set[str]],
    existing_keys: set[tuple[str, str]],
    *,
    target: int,
) -> list[dict[str, Any]]:
    # Work on a private optimistic view.  Candidate rows are only a scheduling
    # hint; the caller must update the real ``observed`` map after Gold has
    # produced an execution-backed axis.  Mutating the caller's map here can
    # make an UNDECIDED candidate appear verified and prevent a retry.
    working_observed = {
        key: set(values)
        for key, values in observed.items()
    }
    deficits = _deficits(working_observed, target)
    selected: list[dict[str, Any]] = []
    selected_keys: set[tuple[str, str]] = set()
    remaining = list(candidates)
    while deficits:
        best: dict[str, Any] | None = None
        best_score: tuple[int, int, str] | None = None
        for row in remaining:
            key = _key(row)
            if not key[0] or key in existing_keys or key in selected_keys:
                continue
            role = _role(row)
            candidate_axes = _candidate_axes(row)
            categories = {str(value) for value in row.get("categories") or []}
            gain = sum(
                1
                for category, axis in deficits
                if category in categories and axis in candidate_axes
            )
            if role == "mutation" and "mutation_ready" in candidate_axes:
                gain += 1
            if gain <= 0:
                continue
            # Prefer mutation rows for semantic mutation evidence, then rows
            # that cover more currently missing category/axis cells.
            role_bias = 1 if role == "mutation" else 0
            score = (gain, role_bias, key[0])
            if best_score is None or score > best_score:
                best, best_score = row, score
        if best is None:
            break
        selected.append(best)
        selected_keys.add(_key(best))
        categories = {str(value) for value in best.get("categories") or []}
        for category, axis in list(deficits):
            if category in categories and axis in _candidate_axes(best):
                # Reserve the family optimistically.  The actual result is
                # checked below; failed candidates are not counted.
                working_observed[f"{category}\0{axis}"].add(str(best.get("family_id")))
        deficits = _deficits(working_observed, target)
    return selected


def _audit_record(record: dict[str, Any], seeds: tuple[int, ...], scales: tuple[int, ...], max_rows: int) -> dict[str, Any]:
    item = _safe_record(record)
    oracle = run_gold_oracle(
        item["schema"],
        item["standard_sql"],
        item["student_sql"],
        schema_catalog=item["schema_catalog"],
        dialect=item["dialect"],
        expected=item["expectation"],
        seeds=seeds,
        row_scales=scales,
        max_rows_per_table=min(max_rows, 32),
    )
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        structure = _structure_evidence(item)
    item["observed_scenario_axes"] = _observed_axes(item, structure, oracle)
    item["structure"] = structure
    item["oracle"] = oracle
    return item


def topup(
    source: Path,
    existing_path: Path,
    output: Path,
    summary_path: Path,
    *,
    target: int,
    seeds: tuple[int, ...],
    scales: tuple[int, ...],
    max_rows: int,
) -> dict[str, Any]:
    existing, observed = _load_existing(existing_path)
    source_rows = _read_jsonl(source)
    candidates = [
        row for row in source_rows
        if _role(row) in {"mutation", "equivalence"}
        and row.get("sql") and row.get("student_sql")
    ]
    # The source is normally the same mutation layer that was used to build
    # ``existing_path``.  A candidate can therefore already have a row in the
    # observed layer while still lacking an executed axis (candidate metadata
    # is not evidence).  Do not reject those keys: top-up is explicitly a
    # bounded re-audit of public rows whose candidate axis remains deficient.
    verified: list[dict[str, Any]] = []
    counters = Counter()
    selected: list[dict[str, Any]] = []
    selected_keys: set[tuple[str, str]] = set()
    # A candidate can be syntactically valid but still yield UNDECIDED or an
    # ENGINE_GAP.  Retry boundedly until the actual observed deficit closes;
    # this keeps candidate metadata from being mistaken for evidence while
    # avoiding an unbounded public audit.
    max_attempts = max(8, target * 4)
    for _attempt in range(max_attempts):
        if not _deficits(observed, target):
            break
        next_candidates = _select_candidates(
            candidates,
            observed,
            selected_keys,
            target=target,
        )
        if not next_candidates:
            break
        row = next_candidates[0]
        key = _key(row)
        selected.append(row)
        selected_keys.add(key)
        audited = _audit_record(row, seeds, scales, max_rows)
        verified.append(audited)
        axes = set(audited.get("observed_scenario_axes") or [])
        for category in audited.get("categories") or []:
            for axis in axes:
                observed[f"{category}\0{axis}"].add(str(audited.get("family_id")))
        counters[str((audited.get("oracle") or {}).get("verdict") or "UNDECIDED")] += 1

    merged: dict[tuple[str, str], dict[str, Any]] = {_key(row): row for row in existing}
    for row in verified:
        key = _key(row)
        previous = merged.get(key)
        if previous is None:
            merged[key] = row
            continue
        # Keep the original mutation-layer fields (including SQL text and
        # lineage) and union old/new observed axes.  The re-audit may be
        # replacing an existing public row, so setdefault would silently
        # discard the newly verified evidence.
        combined = dict(previous)
        combined.update(row)
        combined["observed_scenario_axes"] = sorted(
            {
                str(axis)
                for axis in (previous.get("observed_scenario_axes") or [])
                + (row.get("observed_scenario_axes") or [])
                if str(axis).strip()
            }
        )
        combined["observed_evidence_source"] = "public_gold_oracle_axis_topup"
        combined["observed_evidence_rows"] = int(previous.get("observed_evidence_rows") or 0) + 1
        merged[key] = combined
    rows = [merged[key] for key in sorted(merged)]
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    final_counts = {
        category: {
            axis: len({
                str(row.get("family_id"))
                for row in rows
                if category in (row.get("categories") or [])
                and axis in (row.get("observed_scenario_axes") or [])
            })
            for axis in AXES
        }
        for category in CORE_CATEGORIES
    }
    summary = {
        "schema_version": 1,
        "source": str(source),
        "existing": str(existing_path),
        "output": str(output),
        "hidden_partition_read": False,
        "target_families_per_axis": target,
        "selected_candidates": len(selected),
        "verified_topup_rows": len(verified),
        "topup_verdicts": dict(sorted(counters.items())),
        "rows_after_merge": len(rows),
        "by_category_axis_families": final_counts,
        "shortfalls": {
            category: {
                axis: max(0, target - count)
                for axis, count in values.items()
                if count < target
            }
            for category, values in final_counts.items()
        },
    }
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return summary


def _ints(value: str) -> tuple[int, ...]:
    result = tuple(int(item.strip()) for item in value.split(",") if item.strip())
    if not result:
        raise ValueError("at least one integer is required")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--existing", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--summary", type=Path, required=True)
    parser.add_argument("--target", type=int, default=30)
    parser.add_argument("--oracle-seeds", default="0,1,2")
    parser.add_argument("--row-scales", default="4,8,16")
    parser.add_argument("--max-rows-per-table", type=int, default=32)
    args = parser.parse_args(argv)
    if args.target <= 0 or args.max_rows_per_table <= 0:
        raise SystemExit("target and max-rows-per-table must be positive")
    result = topup(
        args.source,
        args.existing,
        args.output,
        args.summary,
        target=args.target,
        seeds=_ints(args.oracle_seeds),
        scales=_ints(args.row_scales),
        max_rows=min(args.max_rows_per_table, 32),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
