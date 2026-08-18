"""Run the strict Phase 1 structure, data, and mutation acceptance gate."""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


PROJECT_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = PROJECT_ROOT / "sql-edu-backend"
STRUCTURE_OUTPUTS = PROJECT_ROOT / "test" / "phase1_structure" / "outputs"
DATA_OUTPUTS = PROJECT_ROOT / "data_construct_test" / "outputs"
TEACHING_DIALECT_MATRIX = DATA_OUTPUTS / "phase1_teaching_dialect_matrix.json"

EXPECTED_IR_BOUNDARIES = {
    "grouping_sets",
    "boundary_lateral",
    "boundary_rollup",
    "boundary_cube",
    "set_operation_intersect_all",
    "set_operation_except_all",
}
EXPECTED_AST_BOUNDARIES = {
    "rollup_changed",
    "grouping_sets_changed",
    "cube_changed",
    "intersect_all_modifier_changed",
    "except_all_modifier_changed",
    "lateral_changed",
}
EXPECTED_IR_TO_AST_BOUNDARIES = {
    f"from_ir__{case_id}" for case_id in EXPECTED_IR_BOUNDARIES
}

# A repaired result proves that a replacement was behaviorally useful.  It is
# localization evidence only when the replacement is a single-clause mutation
# whose KP and clause agree with the case's declared error cause.
_KP_EQUIVALENCE_GROUPS = (
    frozenset({"aggregate", "agg-count"}),
    frozenset({"comp-null", "null-handling"}),
)
_EXPECTED_CLAUSES_BY_KP = {
    "aggregate": frozenset({"AGGREGATE"}),
    "agg-count": frozenset({"AGGREGATE"}),
    "case": frozenset({"CASE"}),
    "comp-null": frozenset({"WHERE"}),
    "cte": frozenset({"CTE", "WHERE"}),
    "cte-recursive": frozenset({"RECURSIVE CTE", "WHERE"}),
    "distinct": frozenset({"DISTINCT", "DISTINCT ON"}),
    "except": frozenset({"EXCEPT"}),
    "group-by": frozenset({"GROUP BY"}),
    "having": frozenset({"HAVING"}),
    "intersect": frozenset({"INTERSECT"}),
    "join-full": frozenset({"JOIN", "JOIN TYPE"}),
    "join-inner": frozenset({"JOIN", "FROM"}),
    "join-left": frozenset({"JOIN", "JOIN TYPE"}),
    "join-on": frozenset({"JOIN ON"}),
    "join-right": frozenset({"JOIN", "JOIN TYPE"}),
    "limit": frozenset({"LIMIT", "OFFSET"}),
    "null-handling": frozenset({"WHERE"}),
    "order-by": frozenset({"ORDER BY"}),
    "select-basic": frozenset({"SELECT"}),
    "subquery-correlated": frozenset({"SUBQUERY", "WHERE"}),
    "subquery-exists": frozenset({"SUBQUERY", "WHERE"}),
    "subquery-in": frozenset({"SUBQUERY", "WHERE"}),
    "subquery-scalar": frozenset({"SUBQUERY", "WHERE"}),
    "union": frozenset({"UNION"}),
    "where": frozenset({"WHERE"}),
    "window-row-number": frozenset({"WINDOW"}),
}
_DATA_EXPECTED_KPS_BY_STRUCTURE = {
    "aggregate": ("aggregate",),
    "between": ("where",),
    "case": ("case",),
    "cte": ("cte", "cte-recursive"),
    "comparison": ("where",),
    "correlated-subquery": ("subquery-correlated",),
    "distinct": ("distinct",),
    "group-by": ("group-by",),
    "having": ("having",),
    "in": ("where",),
    "join": ("join-on", "join-left", "join-right", "join-full", "join-inner"),
    "join-on": ("join-on",),
    "like": ("where",),
    "limit-offset": ("limit", "order-by"),
    "logic": ("where",),
    "null": ("comp-null", "null-handling"),
    "order-by": ("order-by",),
    "recursive-cte": ("cte-recursive",),
    "select": ("select-basic",),
    "set-operation": ("union", "intersect", "except"),
    "subquery": ("subquery-scalar", "subquery-in", "subquery-exists"),
    "where": ("where",),
    "window": ("window-row-number",),
}
_DATA_EXPECTED_KPS_BY_TARGET = {
    "aggregate-argument-changed": ("aggregate",),
    "aggregate-condition-in-where": ("having",),
    "aggregate-distinct-changed": ("distinct",),
    "correlated-subquery-predicate-change": ("subquery-correlated",),
    "cte-body-predicate-change": ("cte",),
    "grouping-grain-too-fine": ("group-by",),
    "join-missing": ("join-inner",),
    "limit-count-change": ("limit",),
    "null-comparison-misuse": ("comp-null",),
    "null-sensitive-antijoin-equivalence": ("null-handling",),
    "recursive-cte-changed": ("cte-recursive",),
    "recursive-step-expression-changed": ("cte-recursive",),
    "subquery-comparison-operator-change": ("subquery-scalar",),
    "top-n-ordering-missing": ("order-by",),
}
_DATA_EXPECTED_KPS_BY_DIFF_TYPE = {
    "aggregate-argument-changed": ("aggregate",),
    "aggregate-condition-in-where": ("having",),
    "aggregate-distinct-changed": ("distinct",),
    "aggregate-function-changed": ("aggregate",),
    "case-changed": ("case",),
    "case-else-missing": ("case",),
    "correlated-predicate-changed": ("subquery-correlated",),
    "cte-changed": ("cte",),
    "distinct-changed": ("distinct",),
    "group-by-changed": ("group-by",),
    "grouping-grain-too-fine": ("group-by",),
    "having-changed": ("having",),
    "in-list-member-removed": ("where",),
    "join-key-column-changed": ("join-on",),
    "join-missing": ("join-inner",),
    "join-on-changed": ("join-on",),
    "join-type-changed": ("join-left", "join-right", "join-full"),
    "limit-changed": ("limit",),
    "null-equality-changed": ("comp-null",),
    "null-sensitive-antijoin-equivalence": ("null-handling",),
    "order-by-changed": ("order-by",),
    "order-by-tiebreaker-missing": ("order-by",),
    "projection-changed": ("select-basic",),
    "recursive-cte-changed": ("cte-recursive",),
    "recursive-step-expression-changed": ("cte-recursive",),
    "set-all-modifier-changed": ("union", "intersect", "except"),
    "set-operator-changed": ("union", "intersect", "except"),
    "top-n-ordering-missing": ("order-by",),
    "where-changed": ("where",),
    "window-function-changed": ("window-row-number",),
    "window-over-changed": ("window-row-number",),
}
_RECOVERY_ONLY_CLAUSES = frozenset({"AGGREGATE PLACEMENT", "JOIN STRUCTURE"})
_RECOVERY_ONLY_ACTIONS = frozenset({"restore_standard_join_structure_and_dependent_query_shape"})


class GateFailure(RuntimeError):
    """Raised when a stage command or its strict report checks fail."""


@dataclass(frozen=True)
class Stage:
    name: str
    command: tuple[str, ...]
    cwd: Path
    validate: Callable[[], str] | None = None


def _load_summary(path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GateFailure(f"cannot read report {path.relative_to(PROJECT_ROOT)}: {exc}") from exc
    summary = payload.get("summary")
    if not isinstance(summary, dict):
        raise GateFailure(f"report {path.relative_to(PROJECT_ROOT)} has no summary object")
    return payload, summary


def _require(condition: bool, detail: str) -> None:
    if not condition:
        raise GateFailure(detail)


def _require_sqlite_compatibility_scope(
    summary: dict[str, Any],
    *,
    total: int,
    label: str,
) -> None:
    _require(
        summary.get("validation_mode") == "sqlite_compatibility",
        f"{label}: report does not declare SQLite compatibility scope",
    )
    _require(
        summary.get("native_semantics_verified") is False,
        f"{label}: offline compatibility results must not claim native verification",
    )
    _require(
        summary.get("execution_backend_counts") == {"sqlite": total},
        f"{label}: expected all {total} cases on SQLite, got "
        f"{summary.get('execution_backend_counts')}",
    )


def _normalize_token(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().lower()).strip("-")


def _normalize_clause(value: Any) -> str:
    return re.sub(r"[^A-Z0-9]+", " ", str(value or "").strip().upper()).strip()


def _kp_matches(actual: str, expected: str) -> bool:
    actual_norm = _normalize_token(actual)
    expected_norm = _normalize_token(expected)
    if actual_norm == expected_norm:
        return True
    return any(
        actual_norm in group and expected_norm in group
        for group in _KP_EQUIVALENCE_GROUPS
    )


def _mutation_tests(item: dict[str, Any]) -> list[dict[str, Any]]:
    """Read both report layouts without accepting summary-only evidence."""
    candidates = [item.get("mutation_tests")]
    for container_name in ("mutation_evidence", "mutation_detail"):
        container = item.get(container_name)
        if isinstance(container, dict):
            candidates.append(container.get("tests"))
    for candidate in candidates:
        if isinstance(candidate, list) and candidate:
            return [test for test in candidate if isinstance(test, dict)]
    return []


def _mutation_summary(item: dict[str, Any]) -> dict[str, Any]:
    summary = item.get("mutation_summary")
    if isinstance(summary, dict):
        return summary
    for container_name in ("mutation_evidence", "mutation_detail"):
        container = item.get(container_name)
        if isinstance(container, dict) and isinstance(container.get("summary"), dict):
            return container["summary"]
    return {}


def _is_recovery_only_mutation(test: dict[str, Any]) -> bool:
    clause = _normalize_clause(test.get("clause"))
    action = str(test.get("action") or "").strip()
    if clause in _RECOVERY_ONLY_CLAUSES or action in _RECOVERY_ONLY_ACTIONS:
        return True
    scope = test.get("mutation_scope")
    if isinstance(scope, (list, tuple, set)):
        normalized_scope = {
            _normalize_clause(scope_item)
            for scope_item in scope
            if _normalize_clause(scope_item)
        }
        if len(normalized_scope) > 1:
            return True
    return False


def _mutation_localizes_expected_kp(
    test: dict[str, Any], expected_kps: tuple[str, ...]
) -> bool:
    if not test.get("fixed_by_replacement") or _is_recovery_only_mutation(test):
        return False
    actual_kp = _normalize_token(test.get("knowledge_point_id"))
    actual_clause = _normalize_clause(test.get("clause"))
    if not actual_kp or not actual_clause:
        return False
    for expected_kp in expected_kps:
        expected_norm = _normalize_token(expected_kp)
        expected_clauses = _EXPECTED_CLAUSES_BY_KP.get(expected_norm)
        if (
            expected_clauses
            and actual_clause in expected_clauses
            and _kp_matches(actual_kp, expected_norm)
        ):
            return True
    return False


def _case_label(item: dict[str, Any], index: int) -> str:
    if item.get("id"):
        return str(item["id"])
    operator = str(item.get("operator") or item.get("structure") or "case")
    tactic = str(item.get("tactic") or "")
    suffix = f"/{tactic}" if tactic else ""
    return f"{operator}{suffix}#{index}"


def _format_fixed_mutations(tests: list[dict[str, Any]]) -> str:
    if not tests:
        return "none"
    formatted = []
    for test in tests:
        kp = str(test.get("knowledge_point_id") or "?")
        clause = str(test.get("clause") or "?")
        recovery_only = ":recovery-only" if _is_recovery_only_mutation(test) else ""
        formatted.append(f"{kp}@{clause}{recovery_only}")
    return ",".join(formatted)


def _require_repair_localization(
    cases: list[dict[str, Any]],
    *,
    label: str,
    expected_kps: Callable[[dict[str, Any]], tuple[str, ...]],
) -> None:
    failures: list[str] = []
    for index, item in enumerate(cases, start=1):
        case_expected_kps = tuple(
            _normalize_token(kp) for kp in expected_kps(item) if _normalize_token(kp)
        )
        summary = _mutation_summary(item)
        fixed_tests = [
            test for test in _mutation_tests(item) if test.get("fixed_by_replacement")
        ]
        prefix = _case_label(item, index)
        if not case_expected_kps:
            failures.append(f"{prefix}: no declared/derived expected KP")
        elif not any(kp in _EXPECTED_CLAUSES_BY_KP for kp in case_expected_kps):
            failures.append(
                f"{prefix}: expected KPs have no mutation-clause contract "
                f"{list(case_expected_kps)}"
            )
        elif not (summary.get("fixed_by_replacement") or 0):
            failures.append(f"{prefix}: no repairing mutation")
        elif not fixed_tests:
            failures.append(f"{prefix}: repairing summary has no per-test mutation detail")
        elif not any(
            _mutation_localizes_expected_kp(test, case_expected_kps)
            for test in fixed_tests
        ):
            failures.append(
                f"{prefix}: expected={list(case_expected_kps)} "
                f"fixed={_format_fixed_mutations(fixed_tests)}"
            )
    if failures:
        preview = "; ".join(failures[:8])
        remaining = len(failures) - 8
        if remaining > 0:
            preview += f"; ... and {remaining} more"
        raise GateFailure(
            f"{label}: {len(failures)}/{len(cases)} negative cases lack a "
            f"single-point repairing mutation localized to the expected cause: {preview}"
        )


def _explicit_expected_kps(item: dict[str, Any]) -> tuple[str, ...]:
    values = item.get("expected_kps")
    if not isinstance(values, list):
        return ()
    return tuple(str(value) for value in values if value)


def _data_expected_kps(item: dict[str, Any]) -> tuple[str, ...]:
    explicit = _explicit_expected_kps(item)
    if explicit:
        return explicit
    target = _normalize_token(item.get("strict_target"))
    if target in _DATA_EXPECTED_KPS_BY_TARGET:
        return _DATA_EXPECTED_KPS_BY_TARGET[target]
    structure = _normalize_token(item.get("structure"))
    structure_kps = _DATA_EXPECTED_KPS_BY_STRUCTURE.get(structure, ())
    expected = item.get("expected")
    declared_diff_types = expected.get("diff_types") if isinstance(expected, dict) else None
    diff_types = declared_diff_types if declared_diff_types else item.get("ast_diff_types")
    diff_kps: set[str] = set()
    if isinstance(diff_types, list):
        for diff_type in diff_types:
            diff_kps.update(
                _DATA_EXPECTED_KPS_BY_DIFF_TYPE.get(_normalize_token(diff_type), ())
            )
    narrowed = tuple(kp for kp in structure_kps if kp in diff_kps)
    return narrowed or structure_kps


def _self_check_mutation_contract() -> None:
    expected = ("join-on",)
    _require(
        _mutation_localizes_expected_kp(
            {
                "clause": "JOIN ON",
                "knowledge_point_id": "join-on",
                "mutation_scope": ["JOIN ON"],
                "fixed_by_replacement": True,
            },
            expected,
        ),
        "internal mutation contract rejected a valid single-clause repair",
    )
    _require(
        not _mutation_localizes_expected_kp(
            {
                "clause": "JOIN STRUCTURE",
                "knowledge_point_id": "join-on",
                "mutation_scope": ["FROM", "JOIN", "WHERE", "SELECT"],
                "fixed_by_replacement": True,
            },
            expected,
        ),
        "internal mutation contract accepted a compound recovery as localization",
    )
    _require(
        not _mutation_localizes_expected_kp(
            {
                "clause": "WHERE",
                "knowledge_point_id": "join-on",
                "mutation_scope": ["WHERE"],
                "fixed_by_replacement": True,
            },
            expected,
        ),
        "internal mutation contract accepted a KP with the wrong clause",
    )
    _require(
        _mutation_localizes_expected_kp(
            {
                "clause": "AGGREGATE",
                "knowledge_point_id": "aggregate",
                "mutation_scope": ["AGGREGATE"],
                "fixed_by_replacement": True,
            },
            ("agg-count",),
        ),
        "internal mutation contract rejected a supported KP alias",
    )


def _require_exact_boundaries(summary: dict[str, Any], expected: set[str], label: str) -> None:
    actual = set(summary.get("execution_boundary_ids") or [])
    _require(
        summary.get("execution_boundary_count") == len(expected) and actual == expected,
        f"{label}: expected SQLite boundaries {sorted(expected)}, got {sorted(actual)}",
    )


def _validate_ir() -> str:
    _, summary = _load_summary(STRUCTURE_OUTPUTS / "phase1_ir_structure_capability.json")
    _require(summary.get("total") == 77, f"IR: expected 77 cases, got {summary.get('total')}")
    _require(summary.get("parse_success") == 77, "IR: not all 77 cases parsed")
    _require(summary.get("ir_build_success") == 77, "IR: not all 77 IR objects built")
    _require(summary.get("buckets") == {"first_class": 77}, f"IR: unexpected buckets {summary.get('buckets')}")
    _require_exact_boundaries(summary, EXPECTED_IR_BOUNDARIES, "IR")
    return "77/77 typed structures; 6 SQLite execution boundaries"


def _validate_ast() -> str:
    _, summary = _load_summary(STRUCTURE_OUTPUTS / "phase1_ast_diff_capability.json")
    _require(summary.get("total") == 53, f"AST Diff: expected 53 cases, got {summary.get('total')}")
    _require(summary.get("buckets") == {"supported": 53}, f"AST Diff: unexpected buckets {summary.get('buckets')}")
    _require(summary.get("unexpected_failure_count") == 0, "AST Diff: unexpected failures present")
    _require_exact_boundaries(summary, EXPECTED_AST_BOUNDARIES, "AST Diff")
    return "53/53 independent AST differences; 6 SQLite execution boundaries"


def _validate_ir_to_ast() -> str:
    payload, summary = _load_summary(STRUCTURE_OUTPUTS / "phase1_ast_diff_from_ir_capability.json")
    _require(summary.get("total") == 77, f"IR -> AST: expected 77 cases, got {summary.get('total')}")
    _require(summary.get("buckets") == {"supported": 77}, f"IR -> AST: unexpected buckets {summary.get('buckets')}")
    _require(summary.get("unexpected_failure_count") == 0, "IR -> AST: unexpected failures present")
    _require_exact_boundaries(summary, EXPECTED_IR_TO_AST_BOUNDARIES, "IR -> AST")
    source_ids = [item.get("source_ir_case_id") for item in payload.get("results") or []]
    _require(
        len(source_ids) == 77 and None not in source_ids and len(set(source_ids)) == 77,
        "IR -> AST: expected 77 unique source_ir_case_id links",
    )
    return "77/77 linked IR-to-AST differences; 6 SQLite execution boundaries"


def _validate_capability_samples() -> str:
    payload, summary = _load_summary(DATA_OUTPUTS / "phase1_capability_samples.json")
    _require(summary.get("total_cases") == 47, f"Capability samples: expected 47 cases, got {summary.get('total_cases')}")
    _require(summary.get("supported_cases") == 46, "Capability samples: expected 46 fully supported cases")
    _require(summary.get("semantic_boundary_cases") == 1, "Capability samples: expected one semantic boundary")
    _require(
        summary.get("semantic_boundary_ids") == ["limit_large_vs_unbounded"],
        "Capability samples: unexpected semantic boundary set",
    )
    _require(summary.get("known_gap_cases") == 0, "Capability samples: known gaps present")
    _require(summary.get("engine_gap_cases") == 0, "Capability samples: engine gaps present")
    _require_sqlite_compatibility_scope(
        summary,
        total=47,
        label="Capability samples",
    )
    _require(not summary.get("spurious_attribution_ids"), "Capability samples: equivalent cases have attribution noise")
    expected_stage_passes = {
        "parse": 47,
        "structure": 47,
        "data": 46,
        "mutation": 46,
        "attribution": 47,
    }
    for stage_name, expected_passed in expected_stage_passes.items():
        stage = (summary.get("stage_pass") or {}).get(stage_name) or {}
        _require(
            stage.get("passed") == expected_passed and stage.get("total") == 47,
            f"Capability samples: {stage_name} stage is not {expected_passed}/47",
        )
    negative_cases = [
        item for item in payload.get("results") or []
        if item.get("expectation") == "not_equivalent"
    ]
    _require(len(negative_cases) == 40, "Capability samples: expected 40 negative cases")
    supported_negative_cases = [
        item for item in negative_cases
        if item.get("capability_bucket") == "supported"
    ]
    _require(len(supported_negative_cases) == 39, "Capability samples: expected 39 supported negative cases")
    _require(
        all(item.get("mutation_stage_met") is True for item in supported_negative_cases),
        "Capability samples: a supported negative case did not pass its mutation stage",
    )
    _require_repair_localization(
        supported_negative_cases,
        label="Capability samples",
        expected_kps=_explicit_expected_kps,
    )
    return "46 full-flow cases plus one declared finite-cardinality semantic boundary"


def _validate_data_generation() -> str:
    payload, summary = _load_summary(DATA_OUTPUTS / "data_generation_boundary_report.json")
    expected = {
        "total": 195,
        "pass": 195,
        "fail": 0,
        "row_value_pass": 195,
        "row_value_fail": 0,
        "expected_counterexamples": 158,
        "row_value_counterexamples": 158,
        "observable_counterexamples": 158,
        "column_only_counterexamples": 0,
    }
    for key, value in expected.items():
        _require(
            summary.get(key) == value,
            f"Data generation: expected {key}={value}, got {summary.get(key)}",
        )
    _require_sqlite_compatibility_scope(
        summary,
        total=195,
        label="Data generation",
    )
    negative_cases = [
        item for item in payload.get("results") or []
        if item.get("expected_equivalent") is False
    ]
    _require(len(negative_cases) == 158, "Data generation: expected 158 negative cases")
    _require(
        all(
            item.get("row_equivalent") is False
            and item.get("row_value_ok") is True
            for item in negative_cases
        ),
        "Data generation: a negative case lacks a row-value counterexample",
    )
    _require_repair_localization(
        negative_cases,
        label="Data generation",
        expected_kps=_data_expected_kps,
    )
    return "195/195 strict PASS; 158/158 localized row-value counterexamples"


def _validate_fuzzer() -> str:
    payload, summary = _load_summary(DATA_OUTPUTS / "e2e_robustness_fuzzer_report.json")
    _require(summary.get("seed") == 20260722, f"Fuzzer: unexpected seed {summary.get('seed')}")
    _require(summary.get("cases_per_operator") == 20, "Fuzzer: cases_per_operator is not 20")
    _require(summary.get("positive_cases") == 50, "Fuzzer: positive_cases is not 50")
    _require(summary.get("total") == 430, f"Fuzzer: expected 430 cases, got {summary.get('total')}")
    _require(summary.get("status_counts") == {"PASS": 430}, f"Fuzzer: unexpected statuses {summary.get('status_counts')}")
    _require_sqlite_compatibility_scope(
        summary,
        total=430,
        label="Fuzzer",
    )
    results = payload.get("results") or []
    negative_cases = [item for item in results if item.get("expect_equiv") is False]
    positive_cases = [item for item in results if item.get("expect_equiv") is True]
    _require(
        len(negative_cases) == 380 and len(positive_cases) == 50,
        "Fuzzer: expected 380 negative and 50 positive cases",
    )
    _require(
        all(
            item.get("is_equivalent") is False
            and item.get("kp_hit") is True
            for item in negative_cases
        ),
        "Fuzzer: a negative case lacks a counterexample or attribution KP hit",
    )
    _require_repair_localization(
        negative_cases,
        label="Fuzzer",
        expected_kps=_explicit_expected_kps,
    )
    _require(
        all(item.get("is_equivalent") is True for item in positive_cases),
        "Fuzzer: an equivalent positive case was rejected",
    )
    return "430/430 deterministic cases with localized mutation repairs"


def _validate_teaching_dialect_matrix() -> str:
    payload, summary = _load_summary(TEACHING_DIALECT_MATRIX)
    _require(summary.get("total") == 16, "Teaching dialect matrix: expected 16 cases")
    for stage in ("structure", "ast_diff"):
        stage_summary = summary.get("stage_status", {}).get(stage) or {}
        _require(
            stage_summary.get("PASS") == 16,
            f"Teaching dialect matrix: {stage} is not 16/16",
        )
    execution = summary.get("stage_status", {}).get("execution") or {}
    _require(
        execution.get("PASS") == 2 and execution.get("PENDING_NATIVE") == 14,
        f"Teaching dialect matrix: unexpected execution statuses {execution}",
    )
    full_flow = summary.get("stage_status", {}).get("full_flow") or {}
    _require(
        full_flow.get("PASS") == 2 and full_flow.get("NOT_RUN") == 14,
        f"Teaching dialect matrix: unexpected full-flow statuses {full_flow}",
    )
    _require(
        payload.get("mode") == "offline",
        "Teaching dialect matrix: offline gate must not silently connect native engines",
    )
    return "16/16 structure + AST; standard 2/2 full flow; 14 vendor cases pending native engines"


def _stages() -> list[Stage]:
    python = sys.executable
    return [
        Stage(
            "IR structure capability",
            (python, "test/phase1_structure/run_phase1_ir_structure_capability.py"),
            PROJECT_ROOT,
            _validate_ir,
        ),
        Stage(
            "Independent AST Diff capability",
            (python, "test/phase1_structure/run_phase1_ast_diff_capability.py"),
            PROJECT_ROOT,
            _validate_ast,
        ),
        Stage(
            "IR-to-AST Diff continuity",
            (python, "test/phase1_structure/run_phase1_ast_diff_from_ir_cases.py"),
            PROJECT_ROOT,
            _validate_ir_to_ast,
        ),
        Stage(
            "Curated full-flow capability samples",
            (
                python,
                "data_construct_test/scripts/run_phase1_capability_samples.py",
                "--fail-on-non-pass",
            ),
            PROJECT_ROOT,
            _validate_capability_samples,
        ),
        Stage(
            "Strict counterexample data generation",
            (
                python,
                "data_construct_test/scripts/run_data_generation_boundary_tests.py",
                "--max-rows",
                "10",
                "--print-summary",
                "--fail-on-non-pass",
            ),
            PROJECT_ROOT,
            _validate_data_generation,
        ),
        Stage(
            "Strict deterministic robustness fuzzer",
            (
                python,
                "data_construct_test/scripts/run_e2e_robustness_fuzzer.py",
                "--seed",
                "20260722",
                "--cases-per-operator",
                "20",
                "--positive-cases",
                "50",
                "--fail-on-non-pass",
            ),
            PROJECT_ROOT,
            _validate_fuzzer,
        ),
        Stage(
            "Five teaching dialect matrix",
            (
                python,
                "data_construct_test/scripts/run_phase1_teaching_dialect_matrix.py",
                "--fail-on-gap",
            ),
            PROJECT_ROOT,
            _validate_teaching_dialect_matrix,
        ),
        Stage(
            "Backend Phase 1 regression tests",
            (
                python,
                "-m",
                "pytest",
                "-q",
                "tests/test_parseval_data_generator.py",
                "tests/test_phase1_advanced_structure.py",
                "tests/test_phase1_advanced_structure_ir.py",
                "tests/test_phase1_diff_gate_regressions.py",
                "tests/test_phase1_query_block_scope.py",
            ),
            BACKEND_ROOT,
        ),
    ]


def _display_command(stage: Stage) -> str:
    try:
        cwd = stage.cwd.relative_to(PROJECT_ROOT)
    except ValueError:
        cwd = stage.cwd
    cwd_text = str(cwd) if str(cwd) != "." else "project root"
    return f"({cwd_text}) {' '.join(stage.command)}"


def main() -> None:
    _self_check_mutation_contract()
    stages = _stages()
    started = time.monotonic()
    completed: list[tuple[str, float, str]] = []

    print("Phase 1 SQLite compatibility full-flow gate", flush=True)
    print(f"Python: {sys.executable}", flush=True)
    print(
        "Mode: fail-fast SQLite compatibility only; native dialect semantics are not verified",
        flush=True,
    )

    for index, stage in enumerate(stages, start=1):
        print(f"\n[{index}/{len(stages)}] RUN  {stage.name}", flush=True)
        print(f"         {_display_command(stage)}", flush=True)
        stage_started = time.monotonic()
        result = subprocess.run(stage.command, cwd=stage.cwd, check=False)
        elapsed = time.monotonic() - stage_started
        if result.returncode != 0:
            print(f"[{index}/{len(stages)}] FAIL {stage.name} ({elapsed:.1f}s, exit {result.returncode})", flush=True)
            print(f"\nGate failed after {time.monotonic() - started:.1f}s; {len(completed)}/{len(stages)} stages passed.")
            raise SystemExit(result.returncode or 1)
        try:
            detail = stage.validate() if stage.validate else "command exited successfully"
        except GateFailure as exc:
            print(f"[{index}/{len(stages)}] FAIL {stage.name} ({elapsed:.1f}s)", flush=True)
            print(f"         {exc}", flush=True)
            print(f"\nGate failed after {time.monotonic() - started:.1f}s; {len(completed)}/{len(stages)} stages passed.")
            raise SystemExit(1) from exc
        completed.append((stage.name, elapsed, detail))
        print(f"[{index}/{len(stages)}] PASS {stage.name} ({elapsed:.1f}s)", flush=True)
        print(f"         {detail}", flush=True)

    print("\nPhase 1 SQLite compatibility full-flow gate: PASS", flush=True)
    for name, elapsed, detail in completed:
        print(f"- {name}: {detail} ({elapsed:.1f}s)")
    print(f"Total: {time.monotonic() - started:.1f}s", flush=True)


if __name__ == "__main__":
    main()
