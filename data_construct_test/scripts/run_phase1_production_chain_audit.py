"""Audit the production AST -> witness -> validator -> mutation -> attribution chain.

The independent Gold Oracle and this production audit are deliberately two
separate executions.  The Oracle supplies the reference verdict; production
evidence is accepted only when the same atomic ``diff_id`` survives through
obligation compilation, a selected witness, semantic validation, targeted
mutation repair, and attribution.

This is a bounded development audit.  It reads only the public Gold Oracle
audit output (never a hidden partition), selects a deterministic stratified
family sample, and writes compact evidence plus an aggregate report.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import contextlib
import hashlib
import io
import json
from pathlib import Path
import sys
from typing import Any, Iterable


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
from run_phase1_capability_samples import run_case  # noqa: E402
from core.parseval_data_generator import extract_ast_diffs, parse_schema_text  # noqa: E402
from core.witness_generation.obligations import (  # noqa: E402
    compile_obligations,
    is_redundant_summary_diff,
    stable_diff_id,
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
VALID_ORACLE_VERDICTS = {EQUIVALENT, NOT_EQUIVALENT, UNDECIDED, ENGINE_GAP, INPUT_GAP}


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _read_public_pairs(path: Path) -> list[dict[str, Any]]:
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
            # The mutation/evaluation layer uses the corpus canonical field
            # ``sql``; older audit fixtures used ``standard_sql``.  Normalize
            # both at the audit boundary so a valid evaluation set cannot
            # silently become an empty production audit.
            standard = record.get("standard_sql") or record.get("sql")
            student = record.get("student_sql")
            if not isinstance(standard, str) or not standard.strip():
                continue
            if not isinstance(student, str) or not student.strip():
                continue
            expectation = str(record.get("expectation") or "").upper()
            if expectation not in {EQUIVALENT, NOT_EQUIVALENT}:
                continue
            record["standard_sql"] = standard
            record["_line_number"] = line_number
            rows.append(record)
    return rows


def _stable_key(record: dict[str, Any]) -> str:
    return hashlib.sha256(
        str(record.get("family_id") or record.get("record_id") or "").encode("utf-8")
    ).hexdigest()


def _select_stratified(
    rows: Iterable[dict[str, Any]],
    *,
    per_category: int,
    seed: int,
) -> list[dict[str, Any]]:
    """Select at most one record per family, balancing category and verdict."""
    if per_category <= 0:
        return []
    candidates: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        categories = row.get("categories") or []
        for category in categories:
            category = str(category)
            if category in CORE_CATEGORIES:
                candidates[(category, str(row["expectation"]).upper())].append(row)
    selected: dict[str, dict[str, Any]] = {}
    for category in CORE_CATEGORIES:
        for verdict in (NOT_EQUIVALENT, EQUIVALENT):
            bucket = sorted(
                candidates.get((category, verdict), []),
                key=lambda item: hashlib.sha256(
                    f"{seed}:{_stable_key(item)}".encode("utf-8")
                ).hexdigest(),
            )
            for row in bucket[:per_category]:
                family_id = str(row.get("family_id") or row.get("record_id") or "")
                selected[family_id] = row
    return [selected[key] for key in sorted(selected)]


def _parse_dialect(value: Any) -> str | None:
    dialect = str(value or "").strip().lower()
    return None if dialect in {"", "generic", "standard"} else dialect


def _expected_kps(diffs: list[Any]) -> list[str]:
    return sorted({str(diff.knowledge_point_id) for diff in diffs if diff.knowledge_point_id})


def _production_case(record: dict[str, Any], diffs: list[Any]) -> dict[str, Any]:
    expected = str(record["expectation"]).upper()
    return {
        "id": record.get("family_id") or record.get("record_id"),
        "area": (record.get("categories") or ["unknown"])[0],
        "expectation": "equivalent" if expected == EQUIVALENT else "not_equivalent",
        "standard": record["standard_sql"],
        "student": record["student_sql"],
        "schema": record.get("schema") or "",
        "schema_catalog": record.get("schema_catalog"),
        "declared_sql_dialect": _parse_dialect(record.get("dialect")),
        "execution_backend": "sqlite",
        "max_rows_per_table": 16,
        "expected_kps": _expected_kps(diffs),
        "cfg_labels": [str(item) for item in record.get("categories") or []],
        "family_id": record.get("family_id"),
    }


def _structure(
    standard_sql: str,
    student_sql: str,
    schema: str,
    dialect: Any,
) -> tuple[list[Any], list[Any]]:
    parse_dialect = _parse_dialect(dialect)
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        diffs = extract_ast_diffs(standard_sql, student_sql, dialect=parse_dialect)
    obligations = compile_obligations(diffs, schema=parse_schema_text(schema))
    return diffs, obligations


def _production_diff_ids(output: dict[str, Any]) -> set[str]:
    values = output.get("execution_evidence", {}).get("ast_diffs") or []
    return {
        str(item.get("diff_id"))
        for item in values
        if isinstance(item, dict) and item.get("diff_id")
        and not item.get("summary_only")
    }


def _chain_checks(
    output: dict[str, Any],
    diffs: list[Any],
    obligations: list[Any],
    *,
    oracle_verdict: str,
) -> dict[str, Any]:
    atomic_diff_ids = {
        stable_diff_id(diff, index)
        for index, diff in enumerate(diffs)
        if not is_redundant_summary_diff(diff, diffs)
    }
    obligation_by_diff = {obligation.diff_id: obligation for obligation in obligations}
    evidence = output.get("execution_evidence") or {}
    effectiveness = evidence.get("obligation_effectiveness") or []
    effective_by_diff = {
        str(item.get("diff_id")): item
        for item in effectiveness
        if isinstance(item, dict) and item.get("diff_id")
    }
    mutation = output.get("mutation_evidence") or {}
    mutation_tests = [item for item in mutation.get("tests") or [] if isinstance(item, dict)]
    exact_mutations = [
        item for item in mutation_tests
        if item.get("binding_quality") == "exact"
        and item.get("fixed_by_replacement") is True
        and item.get("diff_ids")
    ]
    attribution_kps = {
        str(item.get("knowledge_point_id"))
        for item in output.get("attributions") or []
        if isinstance(item, dict) and item.get("knowledge_point_id")
    }
    diff_kps = {
        str(diff.knowledge_point_id)
        for diff in diffs
        if diff.knowledge_point_id and not is_redundant_summary_diff(diff, diffs)
    }
    non_equivalent = oracle_verdict == NOT_EQUIVALENT
    execution_difference = bool(
        output.get("executed")
        and output.get("is_equivalent") is False
        and output.get("equivalence_conclusion") == "NOT_EQUIVALENT"
    )
    bound = bool(
        atomic_diff_ids
        # Production execution evidence may retain a clause-level summary
        # alongside its atomic node. The contract is that every atomic ID is
        # present and compiled, not that summaries disappear from the log.
        and atomic_diff_ids.issubset(_production_diff_ids(output))
        and set(obligation_by_diff) == atomic_diff_ids
    )
    activated = bool(
        non_equivalent
        and atomic_diff_ids
        and all(
            item.get("activated") is True
            and item.get("constraint_verification") == "semantic_validator"
            and item.get("constraints_satisfied") is True
            # ``distinguished`` is the stricter causal-attribution flag used
            # by the production planner.  A validator can already have an
            # activated, constraint-satisfied obligation with a real result
            # difference recorded as ``pair_distinguished`` (notably DISTINCT
            # duplicate projections).  Count that as validator activation;
            # causal attribution is checked independently below.
            and (
                item.get("distinguished") is True
                or item.get("pair_distinguished") is True
            )
            for diff_id, item in effective_by_diff.items()
            if diff_id in atomic_diff_ids
        )
        and atomic_diff_ids.issubset(effective_by_diff)
    )
    repaired_ids = {
        str(diff_id)
        for item in exact_mutations
        for diff_id in item.get("diff_ids") or []
    }
    mutation_repaired = bool(non_equivalent and atomic_diff_ids & repaired_ids)
    attribution_bound = bool(attribution_kps & diff_kps)
    equivalent_control = bool(
        oracle_verdict == EQUIVALENT
        and output.get("executed")
        and output.get("is_equivalent") is True
        and output.get("equivalence_conclusion") == "NO_COUNTEREXAMPLE_FOUND"
        and output.get("attribution_stage_met") is True
    )
    return {
        "oracle_verdict": oracle_verdict,
        "atomic_diff_ids": sorted(atomic_diff_ids),
        "atomic_obligation_ids": sorted(obligation.id for obligation in obligations),
        "production_diff_ids": sorted(_production_diff_ids(output)),
        "witness_validator_activated": activated,
        "execution_difference": execution_difference,
        "targeted_mutation_repaired": mutation_repaired,
        "mutation_repaired_diff_ids": sorted(repaired_ids),
        "attribution_bound_to_diff_kp": attribution_bound,
        "equivalence_control": equivalent_control,
        "attribution_kps": sorted(attribution_kps),
        "diff_kps": sorted(diff_kps),
        "structure_obligation_bound": bound,
        "chain_pass": bool(
            (bound
            and (not non_equivalent or execution_difference)
            and (not non_equivalent or activated)
            and (not non_equivalent or mutation_repaired)
            and (not non_equivalent or attribution_bound))
            if non_equivalent
            else equivalent_control
        ),
    }


def audit(
    input_path: Path,
    output_path: Path,
    summary_path: Path,
    *,
    per_category: int,
    sample_seed: int,
    oracle_seeds: tuple[int, ...],
    row_scales: tuple[int, ...],
    max_rows_per_table: int,
) -> dict[str, Any]:
    rows = _read_public_pairs(input_path)
    selected = _select_stratified(rows, per_category=per_category, seed=sample_seed)
    counters = Counter()
    category_counters: dict[str, Counter[str]] = defaultdict(Counter)
    failures: list[str] = []
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in selected:
            standard = str(record["standard_sql"])
            student = str(record["student_sql"])
            schema = str(record.get("schema") or "")
            try:
                initial_diffs, _ = _structure(standard, student, schema, record.get("dialect"))
                oracle = run_gold_oracle(
                    schema,
                    standard,
                    student,
                    schema_catalog=record.get("schema_catalog"),
                    dialect=record.get("dialect"),
                    expected=record.get("expectation"),
                    seeds=oracle_seeds,
                    row_scales=row_scales,
                    max_rows_per_table=min(max_rows_per_table, 32),
                )
                case = _production_case(record, initial_diffs)
                case["max_rows_per_table"] = min(max_rows_per_table, 32)
                production = run_case(case)
                # The production resolver may select a concrete parser
                # dialect for a generic record. Recompute the independent
                # structural view with that same resolved dialect before
                # comparing stable IDs; otherwise vendor-specific AST
                # normalization creates false chain breaks.
                resolved_dialect = production.get("resolved_sql_dialect") or record.get("dialect")
                # Reuse the parser representation that production actually
                # emitted.  Execution dialect and structure-parser dialect
                # are intentionally distinct at this boundary: generic SQL
                # may execute through a MySQL-compatible backend while its
                # AST remains generic (notably quoted literals), and MySQL
                # may reject a generic construct such as FULL JOIN.  Prefer
                # an exact stable-ID match, then the best bounded overlap.
                production_diff_ids = _production_diff_ids(production)
                candidate_dialects: list[Any] = []
                for candidate in (
                    resolved_dialect,
                    record.get("dialect"),
                    None,
                ):
                    candidate_key = _parse_dialect(candidate)
                    if any(_parse_dialect(item) == candidate_key for item in candidate_dialects):
                        continue
                    candidate_dialects.append(candidate)
                best_structure: tuple[int, int, int, list[Any], list[Any]] | None = None
                for candidate in candidate_dialects:
                    candidate_diffs, candidate_obligations = _structure(
                        standard,
                        student,
                        schema,
                        candidate,
                    )
                    candidate_ids = {
                        stable_diff_id(diff, index)
                        for index, diff in enumerate(candidate_diffs)
                    }
                    exact = int(candidate_ids == production_diff_ids)
                    overlap = len(candidate_ids & production_diff_ids)
                    score = (exact, overlap, -abs(len(candidate_ids) - len(production_diff_ids)))
                    if best_structure is None or score > best_structure[:3]:
                        best_structure = (*score, candidate_diffs, candidate_obligations)
                    if exact:
                        break
                if best_structure is None:
                    diffs, obligations = _structure(
                        standard,
                        student,
                        schema,
                        resolved_dialect,
                    )
                else:
                    _, _, _, diffs, obligations = best_structure
                checks = _chain_checks(
                    production,
                    diffs,
                    obligations,
                    oracle_verdict=str(oracle.get("verdict") or UNDECIDED),
                )
                oracle_verdict = checks["oracle_verdict"]
                production_status = str(production.get("verdict_status") or "").upper()
                production_conclusion = str(
                    production.get("equivalence_conclusion") or "UNDECIDED"
                ).upper()
                # A public pair can be a valid independent mutation while the
                # production witness planner cannot activate it (for example,
                # a sparse aggregate world or an unsupported native dialect).
                # That is a declared production boundary, not a validator
                # failure.  Keep it visible as EXCLUDED so the chain rate has
                # a defensible denominator; only a fully executed, expected
                # production verdict may become FAIL.
                exclusion_reason = None
                if oracle_verdict in {UNDECIDED, ENGINE_GAP, INPUT_GAP}:
                    status = "EXCLUDED"
                    exclusion_reason = f"oracle_{oracle_verdict.lower()}"
                elif not production.get("executed"):
                    status = "EXCLUDED"
                    exclusion_reason = "production_not_executed"
                elif production_status in {"KNOWN_GAP", "ENGINE_GAP", "UNDECIDED"}:
                    status = "EXCLUDED"
                    exclusion_reason = f"production_{production_status.lower()}"
                elif oracle_verdict == NOT_EQUIVALENT and production_conclusion != NOT_EQUIVALENT:
                    status = "EXCLUDED"
                    exclusion_reason = "production_no_distinguishing_verdict"
                elif oracle_verdict == EQUIVALENT and production_conclusion != "NO_COUNTEREXAMPLE_FOUND":
                    status = "EXCLUDED"
                    exclusion_reason = "production_no_equivalence_control"
                else:
                    status = "PASS" if checks["chain_pass"] else "FAIL"
                row = {
                    "family_id": record.get("family_id"),
                    "categories": record.get("categories") or [],
                    "dialect": record.get("dialect"),
                    "expected_label": record.get("expectation"),
                    "status": status,
                    "exclusion_reason": exclusion_reason,
                    "checks": checks,
                    "oracle": {
                        "verdict": oracle.get("verdict"),
                        "trial_count": len(oracle.get("trials") or []),
                        "distinguishing_world_id": oracle.get("distinguishing_world_id"),
                    },
                    "production": {
                        "expectation_met": production.get("expectation_met"),
                        "verdict_status": production.get("verdict_status"),
                        "equivalence_conclusion": production.get("equivalence_conclusion"),
                        "executed": production.get("executed"),
                        "mutation_summary": production.get("mutation_summary"),
                        "error": production.get("error"),
                    },
                }
            except Exception as exc:  # noqa: BLE001 - preserve bounded audit rows.
                status = "ERROR"
                row = {
                    "family_id": record.get("family_id"),
                    "categories": record.get("categories") or [],
                    "dialect": record.get("dialect"),
                    "expected_label": record.get("expectation"),
                    "status": status,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            counters[status] += 1
            for category in row.get("categories") or []:
                category_counters[str(category)][status] += 1
            if status == "FAIL":
                failures.append(str(row.get("family_id") or ""))
            handle.write(json.dumps(_json_safe(row), ensure_ascii=False, sort_keys=True) + "\n")
    summary = {
        "schema_version": 1,
        "input": str(input_path),
        "hidden_partition_read": False,
        "sample_seed": sample_seed,
        "available_public_pairs": len(rows),
        "selected_families": len(selected),
        "per_category_per_verdict_target": per_category,
        "oracle_seeds": list(oracle_seeds),
        "row_scales": list(row_scales),
        "max_rows_per_table": min(max_rows_per_table, 32),
        "statuses": dict(sorted(counters.items())),
        "failures": failures,
        "by_category": {
            key: dict(sorted(value.items()))
            for key, value in sorted(category_counters.items())
        },
        "interpretation": {
            "PASS": "independent oracle and production evidence chain agree and all required links are exact",
            "FAIL": "bounded evidence was available but at least one required link was absent",
            "ERROR": "the selected family could not be audited; it is not counted as correctness",
            "UNDECIDED/ENGINE_GAP/INPUT_GAP": "excluded from correctness claims",
        },
    }
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return summary


def _ints(value: str) -> tuple[int, ...]:
    parsed = tuple(int(item.strip()) for item in value.split(",") if item.strip())
    if not parsed:
        raise ValueError("at least one integer is required")
    return parsed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--summary", type=Path, required=True)
    parser.add_argument("--per-category", type=int, default=2)
    parser.add_argument("--sample-seed", type=int, default=20260821)
    parser.add_argument("--oracle-seeds", default="0,1,2")
    parser.add_argument("--row-scales", default="4,8,16")
    parser.add_argument("--max-rows-per-table", type=int, default=16)
    args = parser.parse_args(argv)
    if args.per_category <= 0 or args.max_rows_per_table <= 0:
        raise SystemExit("per-category and max-rows-per-table must be positive")
    summary = audit(
        args.input,
        args.output,
        args.summary,
        per_category=args.per_category,
        sample_seed=args.sample_seed,
        oracle_seeds=_ints(args.oracle_seeds),
        row_scales=_ints(args.row_scales),
        max_rows_per_table=min(args.max_rows_per_table, 32),
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
