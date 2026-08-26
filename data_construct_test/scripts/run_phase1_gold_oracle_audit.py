"""Run a bounded independent Gold Oracle audit on train/public corpus families.

The audit is deliberately separate from the production judge and witness
generator.  It never reads a hidden partition, keeps only a bounded stable
reservoir of paired records, and writes one compact JSON object per pair plus
an aggregate summary.  ``UNDECIDED`` is preserved as a first-class outcome.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import contextlib
import hashlib
import io
import json
from pathlib import Path
import random
import re
import sys
from typing import Any, Iterable, Iterator


SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parents[1]
BACKEND_DIR = PROJECT_ROOT / "sql-edu-backend"
sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(BACKEND_DIR))

from phase1_gold_oracle import (  # noqa: E402
    ENGINE_GAP,
    EQUIVALENT,
    INPUT_GAP,
    NOT_EQUIVALENT,
    UNDECIDED,
    run_gold_oracle,
)
from core.parseval_data_generator import extract_ast_diffs, parse_schema_text  # noqa: E402
from core.witness_generation.obligations import (  # noqa: E402
    compile_obligations,
    is_redundant_summary_diff,
    stable_diff_id,
)


def _records(paths: Iterable[Path]) -> Iterator[dict[str, Any]]:
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


def _paired(record: dict[str, Any]) -> bool:
    standard = record.get("sql") or record.get("standard")
    student = record.get("student_sql") or record.get("student")
    return isinstance(standard, str) and bool(standard.strip()) and isinstance(student, str) and bool(student.strip())


def _stable_key(record: dict[str, Any]) -> str:
    family = str(record.get("family_id") or record.get("record_id") or "")
    return hashlib.sha256(family.encode("utf-8")).hexdigest()


def _reservoir(records: Iterable[dict[str, Any]], limit: int, seed: int) -> list[dict[str, Any]]:
    if limit <= 0:
        return []
    rng = random.Random(seed)
    selected: list[dict[str, Any]] = []
    for record in records:
        if len(selected) < limit:
            selected.append(record)
            continue
        index = rng.randrange(len(selected) + 1)
        if index < limit:
            selected[index] = record
    return sorted(selected, key=_stable_key)


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


def _stratified_records(
    records: Iterable[dict[str, Any]],
    *,
    families_per_category: int,
    equivalence_target: int,
) -> list[dict[str, Any]]:
    """Select a deterministic family-stratified public audit set.

    The old bounded reservoir is representative of the corpus, but it can
    spend nearly all of its budget on the two huge flat-query categories and
    leave CASE/CTE/JOIN families below the statistical denominator.  This
    selector reserves mutation families for the rarest categories first,
    then adds an independent equivalence-control stratum.  A family is still
    emitted at most once, so category counts remain unique-family counts.
    """
    if families_per_category <= 0 or equivalence_target < 0:
        raise ValueError("stratified targets must be non-negative")
    buckets: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    equivalence: list[dict[str, Any]] = []
    for record in records:
        if not _paired(record):
            continue
        expectation = _expected(record) or ""
        if expectation not in {NOT_EQUIVALENT, EQUIVALENT}:
            continue
        categories = {
            str(value)
            for value in (record.get("categories") or [])
            if str(value).strip() in CORE_CATEGORIES
        }
        for category in categories:
            buckets[(category, expectation)].append(record)
        if expectation == EQUIVALENT:
            equivalence.append(record)

    selected: list[dict[str, Any]] = []
    selected_families: set[str] = set()

    # Rare categories first prevents SELECT/WHERE overlap from consuming the
    # same family IDs before CASE/CTE/window strata are reserved.
    category_order = sorted(
        CORE_CATEGORIES,
        key=lambda category: (
            len(buckets.get((category, NOT_EQUIVALENT), [])),
            CORE_CATEGORIES.index(category),
        ),
    )
    for category in category_order:
        kept = 0
        for record in sorted(
            buckets.get((category, NOT_EQUIVALENT), []), key=_stable_key
        ):
            if kept >= families_per_category:
                break
            family_id = str(record.get("family_id") or record.get("record_id") or "")
            if not family_id or family_id in selected_families:
                continue
            selected.append(record)
            selected_families.add(family_id)
            kept += 1

    kept_controls = 0
    for record in sorted(equivalence, key=_stable_key):
        if kept_controls >= equivalence_target:
            break
        family_id = str(record.get("family_id") or record.get("record_id") or "")
        if not family_id or family_id in selected_families:
            continue
        selected.append(record)
        selected_families.add(family_id)
        kept_controls += 1
    return sorted(
        selected,
        key=lambda record: (
            str(record.get("family_id") or record.get("record_id") or ""),
            str(record.get("mutation_layer_role") or ""),
        ),
    )


def _labels(record: dict[str, Any]) -> list[str]:
    values = record.get("categories") or record.get("cfg_labels") or []
    return sorted({str(value) for value in values if str(value).strip()})


def _expected(record: dict[str, Any]) -> str | None:
    value = str(record.get("expectation") or record.get("intent") or "").strip().lower()
    if value in {"equivalent", "eq"}:
        return EQUIVALENT
    if value in {"not_equivalent", "not equivalent", "wrong", "neq"}:
        return NOT_EQUIVALENT
    return None


def _safe_record(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "family_id": record.get("family_id"),
        "lineage_family_id": record.get("lineage_family_id"),
        "family_identity": record.get("family_identity"),
        "record_id": record.get("record_id"),
        "mutation_layer_role": record.get("mutation_layer_role"),
        "attack_kind": record.get("attack_kind"),
        "mutation_operator_family": record.get("mutation_operator_family"),
        "source_id": record.get("source_id"),
        "partition": record.get("partition"),
        "dialect": record.get("dialect"),
        "categories": _labels(record),
        "expectation": _expected(record),
        "schema_trust": record.get("schema_trust"),
        "replay_eligible": bool(record.get("replay_eligible")),
        "standard_sql": record.get("sql") or record.get("standard"),
        "student_sql": record.get("student_sql") or record.get("student"),
        "schema": record.get("schema"),
        "schema_catalog": record.get("schema_catalog"),
        "source_scenario_axes": record.get("scenario_axes") or [],
        "scenario_candidates": record.get("scenario_candidates") or [],
    }


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _compact_oracle(oracle: dict[str, Any]) -> dict[str, Any]:
    """Keep verdict evidence without duplicating every generated world.

    A 10-seed x 3-scale audit can contain 120 trial rows per family.  The
    full trials remain available to the in-process axis classifier; public
    acceptance artifacts only need the verdict, selected distinguishing world,
    stable result digests and bounded trial count.  This keeps the audit
    restartable and avoids multi-gigabyte JSONL files while preserving enough
    information to reproduce a failing family from its source fields.
    """
    keep = {
        "verdict",
        "status",
        "equivalence_conclusion",
        "expected",
        "reason",
        "distinguishing_world_id",
        "native_adapter",
    }
    compact = {key: oracle[key] for key in keep if key in oracle}
    trials = oracle.get("trials") or []
    compact["trial_count"] = len(trials)
    if trials:
        compact["trial_digests"] = [
            {
                "world_id": trial.get("world_id"),
                "same_result": trial.get("same_result"),
                "standard_digest": trial.get("standard_digest"),
                "student_digest": trial.get("student_digest"),
            }
            for trial in trials
        ]
    return compact


def _structure_evidence(item: dict[str, Any]) -> dict[str, Any]:
    """Compile the structural half of the evidence chain only."""
    standard_sql = str(item["standard_sql"] or "")
    student_sql = str(item["student_sql"] or "")
    dialect = str(item.get("dialect") or "generic").lower()
    parse_dialect = None if dialect in {"generic", "standard"} else dialect
    try:
        # Some legacy sqlglot compatibility paths print a vendor warning while
        # still returning a valid AST.  Keep the JSONL audit machine-readable;
        # the structured status below remains the source of truth.
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            diffs = extract_ast_diffs(
                standard_sql,
                student_sql,
                dialect=parse_dialect,
            )
        schema = parse_schema_text(str(item.get("schema") or ""))
        obligations = compile_obligations(diffs, schema=schema)
    except Exception as exc:  # noqa: BLE001 - audit must preserve a bounded failure row.
        return {
            "status": "STRUCTURE_GAP",
            "error": f"{type(exc).__name__}: {exc}",
            "ast_diffs": [],
            "obligations": [],
            "evidence_chain": [],
        }

    diff_rows = []
    for index, diff in enumerate(diffs):
        diff_id = stable_diff_id(diff, index)
        summary_only = is_redundant_summary_diff(diff, diffs)
        diff_rows.append({
            "diff_id": diff_id,
            "clause": diff.clause_category,
            "diff_type": diff.diff_type,
            "knowledge_point_id": diff.knowledge_point_id,
            "table": diff.target_table,
            "column": diff.target_column,
            "severity": diff.severity,
            "extra": _json_safe(diff.extra),
            "summary_only": summary_only,
        })
    obligation_rows = []
    for obligation in obligations:
        obligation_rows.append({
            "obligation_id": obligation.id,
            "diff_id": obligation.diff_id,
            "diff_type": obligation.diff_type,
            "clause": obligation.clause,
            "knowledge_point_id": obligation.knowledge_point_id,
            "required_tables": sorted(obligation.required_tables),
            "required_columns": _json_safe(obligation.required_columns),
            "minimum_rows": dict(obligation.minimum_rows),
            "hard_constraints": _json_safe(obligation.hard_constraints),
            "estimated_cost": obligation.estimated_cost,
        })
    diff_ids = {
        row["diff_id"] for row in diff_rows if not row["summary_only"]
    }
    obligation_diff_ids = {row["diff_id"] for row in obligation_rows}
    missing_obligations = sorted(diff_ids - obligation_diff_ids)
    chain = []
    for diff in diff_rows:
        matches = [row for row in obligation_rows if row["diff_id"] == diff["diff_id"]]
        chain.append({
            "diff_id": diff["diff_id"],
            "obligation_ids": [row["obligation_id"] for row in matches],
            "witness": (
                "SUMMARY_ONLY"
                if diff["summary_only"]
                else "GOLD_WORLD_EXECUTED_SEPARATELY" if matches
                else "NO_OBLIGATION"
            ),
            "validator": "NOT_RUN_IN_GOLD_AUDIT",
            "mutation": "NOT_RUN_IN_GOLD_AUDIT",
            "attribution": "NOT_RUN_IN_GOLD_AUDIT",
        })
    return {
        "status": "BOUND" if not missing_obligations else "OBLIGATION_GAP",
        "ast_diffs": diff_rows,
        "obligations": obligation_rows,
        "missing_obligation_diff_ids": missing_obligations,
        "evidence_chain": chain,
    }


def _observed_axes(
    item: dict[str, Any],
    structure: dict[str, Any],
    oracle: dict[str, Any],
) -> list[str]:
    """Derive only axes evidenced by this independent audit run."""
    axes = {"base", "paired_mutation"}
    dialect = str(item.get("dialect") or "generic").lower()
    if dialect not in {"generic", "standard"} and oracle.get("verdict") not in {
        ENGINE_GAP,
        INPUT_GAP,
    }:
        axes.add("dialect_feature")
    schema_text = str(item.get("schema") or "")
    if item.get("schema_catalog") or re.search(
        r"(?is)\b(?:PRIMARY\s+KEY|FOREIGN\s+KEY|UNIQUE|NOT\s+NULL)\b",
        schema_text,
    ):
        axes.add("schema_constraint")
    diffs = structure.get("ast_diffs") or []
    diff_types = {str(diff.get("diff_type") or "") for diff in diffs}
    sql_pair = f"{item.get('standard_sql') or ''} {item.get('student_sql') or ''}"
    null_sensitive_query = bool(
        re.search(
            r"(?is)\b(?:IS\s+NULL|IS\s+NOT\s+NULL|NOT\s+IN|NOT\s+EXISTS|"
            r"COALESCE|NULLIF|LEFT\s+JOIN|RIGHT\s+JOIN|FULL\s+JOIN)\b",
            sql_pair,
        )
    )
    referenced_tables = {
        match.group(1).lower()
        for match in re.finditer(
            r"(?is)\b(?:FROM|JOIN)\s+([A-Za-z_][A-Za-z0-9_$]*)",
            sql_pair,
        )
    }
    if len(referenced_tables) >= 2 or any(
        "join" in diff_type or "from_source" in diff_type for diff_type in diff_types
    ):
        axes.add("multi_table")
    if (
        item.get("expectation") == NOT_EQUIVALENT
        and structure.get("status") == "BOUND"
        and oracle.get("verdict") == NOT_EQUIVALENT
    ):
        axes.add("mutation_ready")
    trials = oracle.get("trials") or []
    for trial in trials:
        database = trial.get("database") or {}
        if null_sensitive_query and any(
            isinstance(row, dict) and any(value is None for value in row.values())
            for rows in database.values() if isinstance(rows, list)
            for row in rows
        ):
            axes.add("null")
        if not trial.get("standard_rows") or not trial.get("student_rows"):
            axes.add("empty_result")
        for key in ("standard_rows", "student_rows"):
            rows = trial.get(key) or []
            encoded = [json.dumps(row, ensure_ascii=False, sort_keys=True, default=repr) for row in rows]
            if len(encoded) != len(set(encoded)):
                axes.add("duplicate_candidate")
        if any(
            diff_type in {
                "comparison_operator_changed",
                "logical_operator_changed",
                "limit_changed",
                "offset_changed",
                "having_changed",
                "order_direction_changed",
                "case_changed",
                "case_when_missing",
                "case_else_missing",
            }
            for diff_type in diff_types
        ) or re.search(
            r"(?is)\b(?:BETWEEN|LIMIT|OFFSET|HAVING|CASE|WHEN)\b|(?:<=|>=|<>|!=|=|<|>)",
            sql_pair,
        ):
            axes.add("boundary_candidate")
    return sorted(axes)


def audit(
    inputs: Iterable[Path],
    output: Path,
    summary_output: Path,
    *,
    max_pairs: int,
    sample_seed: int,
    oracle_seeds: tuple[int, ...],
    row_scales: tuple[int, ...],
    max_rows_per_table: int,
    families_per_category: int | None = None,
    equivalence_target: int = 0,
    compact_output: bool = False,
) -> dict[str, Any]:
    if families_per_category is not None:
        selected = _stratified_records(
            _records(inputs),
            families_per_category=families_per_category,
            equivalence_target=equivalence_target,
        )
    else:
        selected = _reservoir(
            (record for record in _records(inputs) if _paired(record)),
            max_pairs,
            sample_seed,
        )
    counters = Counter()
    structure_counters = Counter()
    expectation_pairs = Counter()
    atomic_diff_count = 0
    atomic_obligation_count = 0
    missing_obligation_count = 0
    category_counters: dict[str, Counter[str]] = defaultdict(Counter)
    dialect_counters: dict[str, Counter[str]] = defaultdict(Counter)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="\n") as handle:
        for record in selected:
            item = _safe_record(record)
            oracle = run_gold_oracle(
                item["schema"],
                item["standard_sql"],
                item["student_sql"],
                schema_catalog=item["schema_catalog"],
                dialect=item["dialect"],
                expected=item["expectation"],
                seeds=oracle_seeds,
                row_scales=row_scales,
                max_rows_per_table=max_rows_per_table,
            )
            structure = _structure_evidence(item)
            structure["gold_execution"] = {
                "verdict": oracle.get("verdict"),
                "distinguishing_world_id": oracle.get("distinguishing_world_id"),
                "trial_count": len(oracle.get("trials") or []),
            }
            item["observed_scenario_axes"] = _observed_axes(item, structure, oracle)
            verdict = str(oracle.get("verdict") or UNDECIDED)
            counters[verdict] += 1
            structure_counters[str(structure.get("status") or "STRUCTURE_GAP")] += 1
            atomic_diffs = [
                row for row in structure.get("ast_diffs") or ()
                if not row.get("summary_only")
            ]
            atomic_diff_count += len(atomic_diffs)
            atomic_obligation_count += len(structure.get("obligations") or ())
            missing_obligation_count += len(structure.get("missing_obligation_diff_ids") or ())
            expected = item.get("expectation")
            if expected:
                expectation_pairs[(str(expected), verdict)] += 1
            dialect = str(item.get("dialect") or "generic")
            dialect_counters[dialect][verdict] += 1
            for category in item["categories"]:
                category_counters[category][verdict] += 1
            serialised_item = {**item, "structure": structure, "oracle": oracle}
            if compact_output:
                serialised_item["oracle"] = _compact_oracle(oracle)
            handle.write(
                json.dumps(
                    _json_safe(serialised_item),
                    ensure_ascii=False,
                    sort_keys=True,
                )
                + "\n"
            )
    summary = {
        "schema_version": 1,
        "input_partitions": [str(path) for path in inputs],
        "hidden_partition_read": False,
        "sample_seed": sample_seed,
        "max_pairs": max_pairs,
        "families_per_category": families_per_category,
        "equivalence_target": equivalence_target if families_per_category is not None else None,
        "compact_output": compact_output,
        "selected_pairs": len(selected),
        "oracle_seeds": list(oracle_seeds),
        "row_scales": list(row_scales),
        "max_rows_per_table": max_rows_per_table,
        "verdicts": dict(sorted(counters.items())),
        "structure_statuses": dict(sorted(structure_counters.items())),
        "expectation_verdict_pairs": {
            f"{expected}->{verdict}": count
            for (expected, verdict), count in sorted(expectation_pairs.items())
        },
        "quality": {
            "structure_bound_rate": (
                structure_counters["BOUND"] / len(selected) if selected else 0.0
            ),
            "atomic_diff_count": atomic_diff_count,
            "atomic_obligation_count": atomic_obligation_count,
            "missing_obligation_diff_count": missing_obligation_count,
            "atomic_obligation_coverage_rate": (
                atomic_obligation_count / atomic_diff_count if atomic_diff_count else 1.0
            ),
            "labelled_pair_count": sum(expectation_pairs.values()),
            "labelled_verdict_match_count": sum(
                count
                for (expected, verdict), count in expectation_pairs.items()
                if expected == verdict
            ),
        },
        "by_dialect": {key: dict(sorted(value.items())) for key, value in sorted(dialect_counters.items())},
        "by_category": {key: dict(sorted(value.items())) for key, value in sorted(category_counters.items())},
        "interpretation": {
            "equivalent": "only an explicitly trusted equivalent expectation can produce EQUIVALENT",
            "not_equivalent": "a bounded execution witness differs",
            "undecided": "no bounded witness or no trusted equivalence label",
            "engine_gap": "native dialect/SQLite feature is unavailable",
            "input_gap": "schema or query pair is not replayable",
        },
    }
    summary_output.parent.mkdir(parents=True, exist_ok=True)
    summary_output.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return summary


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", action="append", type=Path, dest="inputs", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--summary", type=Path, required=True)
    parser.add_argument("--max-pairs", type=int, default=100)
    parser.add_argument("--sample-seed", type=int, default=20260820)
    parser.add_argument("--oracle-seeds", default="0,1")
    parser.add_argument("--row-scales", default="4,8")
    parser.add_argument("--max-rows-per-table", type=int, default=32)
    parser.add_argument(
        "--families-per-category",
        type=int,
        default=None,
        help="reserve this many NOT_EQUIVALENT families per core category instead of a global reservoir",
    )
    parser.add_argument(
        "--equivalence-target",
        type=int,
        default=0,
        help="additional family-stratified EQUIVALENT controls for --families-per-category mode",
    )
    parser.add_argument(
        "--compact-output",
        action="store_true",
        help="omit per-world database rows from JSONL while retaining verdict/digest evidence",
    )
    return parser.parse_args(argv)


def _parse_ints(value: str) -> tuple[int, ...]:
    values = tuple(int(item.strip()) for item in value.split(",") if item.strip())
    if not values:
        raise ValueError("at least one integer is required")
    return values


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.max_pairs <= 0 or args.max_rows_per_table <= 0:
        raise SystemExit("max-pairs and max-rows-per-table must be positive")
    if args.families_per_category is not None and args.families_per_category <= 0:
        raise SystemExit("families-per-category must be positive")
    if args.equivalence_target < 0:
        raise SystemExit("equivalence-target must not be negative")
    summary = audit(
        args.inputs,
        args.output,
        args.summary,
        max_pairs=args.max_pairs,
        sample_seed=args.sample_seed,
        oracle_seeds=_parse_ints(args.oracle_seeds),
        row_scales=_parse_ints(args.row_scales),
        max_rows_per_table=min(args.max_rows_per_table, 32),
        families_per_category=args.families_per_category,
        equivalence_target=args.equivalence_target,
        compact_output=args.compact_output,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
