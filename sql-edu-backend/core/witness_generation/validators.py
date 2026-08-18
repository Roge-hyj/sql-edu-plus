"""Semantic validation of materialized witness worlds."""

from __future__ import annotations

from dataclasses import dataclass, field
from collections import Counter, defaultdict
import re
import sqlite3
from typing import Any

from sqlglot import exp, parse_one

from .obligations import DistinguishingObligation
from .regex_support import (
    RegexEvaluationError,
    glob_matches,
    like_matches,
    regex_matches,
    similar_to_matches,
)


@dataclass
class ObligationValidation:
    obligation_id: str
    activated: bool
    constraints_satisfied: bool
    execution_distinguished: bool = False
    diagnostics: list[str] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "obligation_id": self.obligation_id,
            "activated": self.activated,
            "constraints_satisfied": self.constraints_satisfied,
            "execution_distinguished": self.execution_distinguished,
            "diagnostics": list(self.diagnostics),
            "evidence": dict(self.evidence),
        }


def _values(world: Any, table: str, column: str) -> list[Any]:
    for name, rows in world.database.items():
        if name.lower() == table.lower():
            if not rows:
                return []
            actual = next(
                (name for name in rows[0] if name.lower() == column.lower()),
                None,
            )
            return [row.get(actual) for row in rows] if actual else []
    return []


def _table_rows(world: Any, table: str) -> list[dict[str, Any]]:
    for name, rows in world.database.items():
        if name.lower() == table.lower():
            return rows
    return []


def _column_name(rows: list[dict[str, Any]], requested: str) -> str | None:
    if not rows:
        return None
    return next(
        (name for name in rows[0] if name.lower() == requested.lower()),
        None,
    )


def _sql_extreme_order_key(value: Any) -> tuple[Any, ...]:
    """Return a total, SQLite-compatible order for heterogeneous fixtures.

    A valid typed database normally gives MIN/MAX one storage class. Legacy
    witness probes can temporarily leave a string in a numeric-looking column,
    however, and Python cannot directly order that string against an integer.
    SQLite orders non-NULL values by storage class (number, text, blob). Use a
    deterministic approximation here so validation reports the malformed
    mixture instead of aborting the entire Phase 1 run.
    """

    if isinstance(value, (bool, int, float)):
        return 0, float(value)
    if isinstance(value, str):
        return 1, value
    if isinstance(value, (bytes, bytearray, memoryview)):
        return 2, bytes(value)
    return 3, type(value).__name__, repr(value)


def _join_candidate_columns(left_rows: list[dict[str, Any]], right_rows: list[dict[str, Any]], preferred: str = "") -> list[tuple[str, str]]:
    if not left_rows or not right_rows:
        return []
    left_names = {name.lower(): name for name in left_rows[0]}
    right_names = {name.lower(): name for name in right_rows[0]}
    common = set(left_names) & set(right_names)
    ordered = []
    if preferred.lower() in common:
        ordered.append(preferred.lower())
    ordered.extend(sorted(common - {item[0].lower() for item in ordered}))
    return [(left_names[name], right_names[name]) for name in ordered]


def _validate_join_paths(world: Any, obligation: DistinguishingObligation) -> tuple[bool, dict[str, Any], list[str]]:
    spec = next(
        (item for item in obligation.hard_constraints
         if item.kind in {"matched_and_dangling_join_rows", "standard_join_equal_student_join_unequal"}),
        None,
    )
    if spec is None:
        return False, {}, ["join_constraint_missing"]
    metadata = dict(spec.metadata)
    declared_pairs = metadata.get("standard_join_pairs") or metadata.get("student_join_pairs")
    if not declared_pairs:
        return False, {}, ["join_metadata_missing"]
    tables = [(name, rows) for name, rows in world.database.items() if rows]

    def _pair_parts(pair: Any) -> tuple[str, str, str, str] | None:
        if len(pair) == 2 and all(isinstance(item, (tuple, list)) for item in pair):
            (left_table, left_column), (right_table, right_column) = pair
            return str(left_table), str(left_column), str(right_table), str(right_column)
        if len(pair) == 4:
            return tuple(str(item) for item in pair)  # type: ignore[return-value]
        return None

    if spec.kind == "standard_join_equal_student_join_unequal":
        def _edge_key(parts: tuple[str, str, str, str]) -> tuple[str, str]:
            left_table, _, right_table, _ = parts
            return tuple(sorted((left_table.lower(), right_table.lower())))

        def _pairs_by_edge(raw_pairs: Any) -> dict[tuple[str, str], list[tuple[str, str, str, str]]]:
            grouped: dict[tuple[str, str], list[tuple[str, str, str, str]]] = defaultdict(list)
            for raw_pair in raw_pairs or ():
                parts = _pair_parts(raw_pair)
                if parts is not None:
                    grouped[_edge_key(parts)].append(parts)
            return grouped

        def _orient_pair(
            parts: tuple[str, str, str, str],
            left_table: str,
            right_table: str,
        ) -> tuple[str, str] | None:
            pair_left_table, pair_left_column, pair_right_table, pair_right_column = parts
            if (
                pair_left_table.lower() == left_table.lower()
                and pair_right_table.lower() == right_table.lower()
            ):
                return pair_left_column, pair_right_column
            if (
                pair_left_table.lower() == right_table.lower()
                and pair_right_table.lower() == left_table.lower()
            ):
                return pair_right_column, pair_left_column
            return None

        def _predicate_evidence(
            pairs: list[tuple[str, str, str, str]],
            left_table: str,
            right_table: str,
            left_rows: list[dict[str, Any]],
            right_rows: list[dict[str, Any]],
            left_index: int,
            right_index: int,
        ) -> tuple[bool, list[dict[str, Any]]] | None:
            comparisons: list[dict[str, Any]] = []
            for pair in pairs:
                oriented = _orient_pair(pair, left_table, right_table)
                if oriented is None:
                    return None
                left_column = _column_name(left_rows, oriented[0])
                right_column = _column_name(right_rows, oriented[1])
                if left_column is None or right_column is None:
                    return None
                left_value = left_rows[left_index].get(left_column)
                right_value = right_rows[right_index].get(right_column)
                equal = (
                    left_value is not None
                    and right_value is not None
                    and left_value == right_value
                )
                comparisons.append({
                    "left_column": left_column,
                    "right_column": right_column,
                    "left_value": left_value,
                    "right_value": right_value,
                    "equal": equal,
                })
            return all(item["equal"] for item in comparisons), comparisons

        standard_by_edge = _pairs_by_edge(metadata.get("standard_join_pairs"))
        student_by_edge = _pairs_by_edge(metadata.get("student_join_pairs"))
        for edge in sorted(set(standard_by_edge) | set(student_by_edge)):
            standard_pairs = standard_by_edge.get(edge, [])
            student_pairs = student_by_edge.get(edge, [])
            declared = standard_pairs or student_pairs
            if not declared:
                continue
            declared_left, _, declared_right, _ = declared[0]
            left_entry = next(
                ((name, rows) for name, rows in tables if name.lower() == declared_left.lower()),
                None,
            )
            right_entry = next(
                ((name, rows) for name, rows in tables if name.lower() == declared_right.lower()),
                None,
            )
            if not left_entry or not right_entry:
                continue
            left_name, left_rows = left_entry
            right_name, right_rows = right_entry
            for left_index, _left_row in enumerate(left_rows[:32]):
                for right_index, _right_row in enumerate(right_rows[:32]):
                    standard_result = _predicate_evidence(
                        standard_pairs,
                        left_name,
                        right_name,
                        left_rows,
                        right_rows,
                        left_index,
                        right_index,
                    )
                    student_result = _predicate_evidence(
                        student_pairs,
                        left_name,
                        right_name,
                        left_rows,
                        right_rows,
                        left_index,
                        right_index,
                    )
                    if standard_result is None or student_result is None:
                        continue
                    standard_truth, standard_values = standard_result
                    student_truth, student_values = student_result
                    if standard_truth == student_truth:
                        continue
                    standard_matches = [
                        item["left_value"] for item in standard_values if item["equal"]
                    ]
                    student_matches = [
                        item["left_value"] for item in student_values if item["equal"]
                    ]
                    return True, {
                        "left_table": left_name,
                        "right_table": right_name,
                        "left_row_index": left_index,
                        "right_row_index": right_index,
                        "standard_truth": standard_truth,
                        "student_truth": student_truth,
                        "divergence_direction": (
                            "standard_only" if standard_truth else "student_only"
                        ),
                        "standard_pair_values": standard_values,
                        "student_pair_values": student_values,
                        # Preserve the legacy report keys while the consumers
                        # migrate to row-pair predicate evidence.
                        "standard_matched_values": standard_matches,
                        "student_matched_values": student_matches,
                    }, []
        return False, {}, ["standard_join_path_or_student_drift_missing"]

    best: dict[str, Any] | None = None
    for pair in declared_pairs:
        parts = _pair_parts(pair)
        if parts is None:
            continue
        left_table, left_col_name, right_table, right_col_name = parts
        left_entry = next(((name, rows) for name, rows in tables if name.lower() == left_table.lower()), None)
        right_entry = next(((name, rows) for name, rows in tables if name.lower() == right_table.lower()), None)
        if not left_entry or not right_entry:
            continue
        left_name, left_rows = left_entry
        right_name, right_rows = right_entry
        left_col = _column_name(left_rows, left_col_name)
        right_col = _column_name(right_rows, right_col_name)
        if not left_col or not right_col:
            continue
        for _ in (0,):
                left_values = {row.get(left_col) for row in left_rows}
                right_values = {row.get(right_col) for row in right_rows}
                matched = left_values & right_values
                dangling_left = left_values - right_values
                dangling_right = right_values - left_values
                candidate = {
                    "left_table": left_name,
                    "right_table": right_name,
                    "left_column": left_col,
                    "right_column": right_col,
                    "matched_values": sorted(matched, key=str)[:8],
                    "dangling_left_values": sorted(dangling_left, key=str)[:8],
                    "dangling_right_values": sorted(dangling_right, key=str)[:8],
                }
                if matched and (dangling_left or dangling_right):
                    return True, candidate, []
                best = candidate
    return False, best or {}, ["matched_and_dangling_join_paths_missing"]


def _validate_group_grain(world: Any, obligation: DistinguishingObligation) -> tuple[bool, dict[str, Any], list[str]]:
    spec = next((item for item in obligation.hard_constraints if item.kind == "group_grain_split"), None)
    if spec is None or not spec.column:
        return False, {}, ["group_constraint_missing_column"]
    metadata = dict(spec.metadata)
    if "standard_group_columns" in metadata:
        standard_keys = metadata["standard_group_columns"]
    elif "standard_keys" in metadata:
        standard_keys = metadata["standard_keys"]
    else:
        standard_keys = None
    if "student_group_columns" in metadata:
        student_keys = metadata["student_group_columns"]
    elif "student_keys" in metadata:
        student_keys = metadata["student_keys"]
    else:
        student_keys = None
    if standard_keys is None:
        return False, {}, ["group_metadata_missing"]
    rows = _table_rows(world, spec.relation)
    if not rows:
        return False, {}, ["group_table_missing"]
    standard_columns = [
        _column_name(rows, str(key).split(".")[-1])
        for key in standard_keys
    ]
    if any(item is None for item in standard_columns):
        return False, {}, ["group_column_missing"]
    standard_columns = [str(item) for item in standard_columns]
    counts = Counter(
        tuple(row.get(column) for column in standard_columns)
        for row in rows
    )
    if student_keys is not None:
        student_columns = [
            _column_name(rows, str(key).split(".")[-1])
            for key in student_keys
        ]
        if any(item is None for item in student_columns):
            return False, {}, ["student_group_column_missing"]
        student_columns = [str(item) for item in student_columns]
        standard_to_student: dict[tuple[Any, ...], set[tuple[Any, ...]]] = defaultdict(set)
        student_to_standard: dict[tuple[Any, ...], set[tuple[Any, ...]]] = defaultdict(set)
        student_counts: Counter[tuple[Any, ...]] = Counter()
        for row in rows:
            standard_key = tuple(row.get(column) for column in standard_columns)
            student_key = tuple(row.get(column) for column in student_columns)
            standard_to_student[standard_key].add(student_key)
            student_to_standard[student_key].add(standard_key)
            student_counts[student_key] += 1
        standard_splits = [
            key for key, partitions in standard_to_student.items()
            if len(partitions) > 1
        ]
        student_splits = [
            key for key, partitions in student_to_standard.items()
            if len(partitions) > 1
        ]
        if obligation.diff_type == "grouping_grain_too_fine":
            satisfied = bool(standard_splits)
        elif obligation.diff_type == "grouping_grain_too_coarse":
            satisfied = bool(student_splits)
        else:
            satisfied = bool(standard_splits or student_splits)
    else:
        student_columns = []
        student_counts = Counter()
        standard_splits = []
        student_splits = []
        # Compatibility validation for legacy/manual obligations that do not
        # yet carry both grouping definitions.
        satisfied = (
            len(rows) >= 3
            and any(count > 1 for count in counts.values())
            and len(counts) > 1
        )
    duplicate_keys = [key for key, count in counts.items() if count > 1]
    singleton_keys = [key for key, count in counts.items() if count == 1]
    return satisfied, {
        "group_columns": list(standard_keys),
        "student_group_columns": list(student_keys or ()),
        "group_key_counts": {str(key): count for key, count in counts.items()},
        "student_group_key_counts": {
            str(key): count for key, count in student_counts.items()
        },
        "standard_groups_split_by_student": [list(key) for key in standard_splits],
        "student_groups_split_by_standard": [list(key) for key in student_splits],
        "duplicate_group_keys": [list(key) for key in duplicate_keys],
        "singleton_group_keys": [list(key) for key in singleton_keys],
        # These aliases make the two semantic paths explicit for consumers
        # that use the common overlap/outer-only evidence vocabulary.
        "overlap_values": [key[0] if len(key) == 1 else list(key) for key in duplicate_keys],
        "outer_only_values": [key[0] if len(key) == 1 else list(key) for key in singleton_keys],
    }, [] if satisfied else ["group_grain_split_not_materialized"]


_AGGREGATE_TYPES = (exp.Count, exp.Sum, exp.Avg, exp.Min, exp.Max)
_MAX_JOIN_VALIDATION_PRODUCT = 4096
_MAX_JOIN_VALIDATION_GROUPS = 1025


def _quote_sqlite_identifier(value: str) -> str:
    return '"' + str(value).replace('"', '""') + '"'


def _sqlite_fixture_type(values: list[Any]) -> str:
    non_null = [value for value in values if value is not None]
    if non_null and all(isinstance(value, (bool, int)) for value in non_null):
        return "INTEGER"
    if non_null and all(isinstance(value, (bool, int, float)) for value in non_null):
        return "REAL"
    if non_null and all(
        isinstance(value, (bytes, bytearray, memoryview))
        for value in non_null
    ):
        return "BLOB"
    return "TEXT"


def _nearest_select(node: exp.Expression) -> exp.Select | None:
    current = node.parent
    while current is not None:
        if isinstance(current, exp.Select):
            return current
        current = current.parent
    return None


def _execute_sqlite_diagnostic(
    world: Any,
    diagnostic_sql: str,
) -> tuple[list[tuple[Any, ...]] | None, str | None]:
    """Execute one bounded, read-only semantic diagnostic over a witness."""
    connection = sqlite3.connect(":memory:")
    progress_calls = 0

    def _abort_large_diagnostic() -> int:
        nonlocal progress_calls
        progress_calls += 1
        return int(progress_calls > 250)

    connection.set_progress_handler(_abort_large_diagnostic, 1000)
    try:
        for table_name, rows in world.database.items():
            columns = list(dict.fromkeys(
                column for row in rows for column in row
            ))
            if not columns:
                continue
            declarations = []
            for column in columns:
                column_values = [row.get(column) for row in rows]
                declarations.append(
                    f"{_quote_sqlite_identifier(column)} "
                    f"{_sqlite_fixture_type(column_values)}"
                )
            connection.execute(
                f"CREATE TABLE {_quote_sqlite_identifier(table_name)} "
                f"({', '.join(declarations)})"
            )
            placeholders = ", ".join("?" for _ in columns)
            connection.executemany(
                f"INSERT INTO {_quote_sqlite_identifier(table_name)} "
                f"VALUES ({placeholders})",
                [tuple(row.get(column) for column in columns) for row in rows],
            )
        return connection.execute(diagnostic_sql).fetchall(), None
    except Exception as exc:  # noqa: BLE001 - validator failures are evidence.
        return None, type(exc).__name__
    finally:
        connection.close()


def _validate_outer_join_predicate_placement(
    world: Any,
    obligation: DistinguishingObligation,
) -> tuple[bool, dict[str, Any], list[str]]:
    spec = next(
        (
            item
            for item in obligation.hard_constraints
            if item.kind == "outer_join_predicate_placement_path"
        ),
        None,
    )
    if spec is None:
        return False, {}, ["join_predicate_placement_constraint_missing"]
    metadata = dict(spec.metadata)
    context = getattr(world, "execution", {}).get("validation_context", {})
    standard_sql = str(
        metadata.get("standard_query_sql")
        or context.get("standard_sql")
        or ""
    ).strip().rstrip(";")
    student_sql = str(
        metadata.get("student_query_sql")
        or context.get("student_sql")
        or ""
    ).strip().rstrip(";")
    if not standard_sql or not student_sql:
        return False, {
            "movement": metadata.get("movement"),
        }, ["join_predicate_placement_validation_context_missing"]

    def execute_bounded(sql: str) -> tuple[list[tuple[Any, ...]] | None, str | None]:
        return _execute_sqlite_diagnostic(
            world,
            f'SELECT * FROM ({sql}) AS "__placement_path" LIMIT 65',
        )

    standard_rows, standard_error = execute_bounded(standard_sql)
    student_rows, student_error = execute_bounded(student_sql)
    if standard_rows is None or student_rows is None:
        return False, {
            "movement": metadata.get("movement"),
            "standard_execution_error": standard_error,
            "student_execution_error": student_error,
        }, ["join_predicate_placement_validator_execution_failed"]

    standard_counter = Counter(standard_rows)
    student_counter = Counter(student_rows)
    only_standard = list((standard_counter - student_counter).elements())
    only_student = list((student_counter - standard_counter).elements())
    satisfied = bool(only_standard or only_student)
    return satisfied, {
        "source": "full_outer_join_query_path",
        "movement": metadata.get("movement"),
        "join_side": metadata.get("standard_side"),
        "right_table": metadata.get("right_table"),
        "moved_predicate_sql": metadata.get("moved_predicate_sql"),
        "standard_row_count": len(standard_rows),
        "student_row_count": len(student_rows),
        "only_standard_rows": [list(row) for row in only_standard[:8]],
        "only_student_rows": [list(row) for row in only_student[:8]],
    }, [] if satisfied else ["join_predicate_placement_path_missing"]


def _scalar_aggregate_comparison_parts(
    select: exp.Select,
) -> tuple[exp.Expression, exp.Column, exp.Subquery] | None:
    for comparison in select.find_all(
        exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE
    ):
        if _nearest_select(comparison) is not select:
            continue
        if isinstance(comparison.left, exp.Column) and isinstance(
            comparison.right, exp.Subquery
        ):
            outer_column = comparison.left
            subquery = comparison.right
        elif isinstance(comparison.right, exp.Column) and isinstance(
            comparison.left, exp.Subquery
        ):
            outer_column = comparison.right
            subquery = comparison.left
        else:
            continue
        if subquery.find(*_AGGREGATE_TYPES) is not None:
            return comparison, outer_column, subquery
    return None


def _validate_scalar_subquery_boundary(
    world: Any,
    obligation: DistinguishingObligation,
) -> tuple[bool, dict[str, Any], list[str]]:
    spec = next(
        (
            item
            for item in obligation.hard_constraints
            if item.kind == "scalar_subquery_boundary_path"
        ),
        None,
    )
    if spec is None:
        return False, {}, ["scalar_subquery_constraint_missing"]
    execution = getattr(world, "execution", {}) or {}
    context = execution.get("validation_context", {})
    standard_sql = str(context.get("standard_sql") or "")
    if not standard_sql:
        return False, {
            "scalar_boundary_scope": "query_path",
        }, ["scalar_subquery_validation_context_missing"]
    try:
        ast = parse_one(standard_sql, read="sqlite")
    except Exception as exc:  # noqa: BLE001 - report, do not raise.
        return False, {
            "scalar_boundary_scope": "query_path",
        }, [f"scalar_subquery_validator_parse_failed:{type(exc).__name__}"]

    selected: exp.Select | None = None
    for candidate in ast.find_all(exp.Select):
        if _scalar_aggregate_comparison_parts(candidate) is not None:
            selected = candidate
            break
    if selected is None:
        return False, {
            "scalar_boundary_scope": "query_path",
        }, ["scalar_subquery_comparison_missing"]

    diagnostic = selected.copy()
    parts = _scalar_aggregate_comparison_parts(diagnostic)
    if parts is None:
        return False, {
            "scalar_boundary_scope": "query_path",
        }, ["scalar_subquery_comparison_copy_failed"]
    comparison, outer_column, subquery = parts
    outer_projection = outer_column.copy()
    boundary_projection = subquery.copy()
    comparison.replace(exp.EQ(
        this=outer_column.copy(),
        expression=subquery.copy(),
    ))
    diagnostic.set("expressions", [
        exp.alias_(outer_projection, "__outer_boundary_value"),
        exp.alias_(boundary_projection, "__scalar_boundary_value"),
    ])
    for key in ("order", "limit", "offset", "qualify", "distinct"):
        diagnostic.set(key, None)
    diagnostic.set(
        "limit",
        exp.Limit(expression=exp.Literal.number(9)),
    )
    diagnostic_sql = diagnostic.sql(dialect="sqlite")
    result_rows, execution_error = _execute_sqlite_diagnostic(
        world,
        diagnostic_sql,
    )
    if result_rows is None:
        return False, {
            "scalar_boundary_scope": "query_path",
            "diagnostic_sql": diagnostic_sql,
        }, [f"scalar_subquery_validator_execution_failed:{execution_error}"]

    boundary_rows = [
        row
        for row in result_rows
        if len(row) >= 2 and row[0] is not None and row[0] == row[1]
    ]
    satisfied = bool(boundary_rows)
    metadata = dict(spec.metadata)
    return satisfied, {
        "scalar_boundary_scope": "query_path",
        "scalar_aggregate_function": metadata.get(
            "standard_scalar_aggregate_function"
        ),
        "scalar_source_table": metadata.get("standard_scalar_source_table"),
        "scalar_source_column": metadata.get("standard_scalar_source_column"),
        "boundary_path_rows": [list(row) for row in boundary_rows[:8]],
        "diagnostic_sql": diagnostic_sql,
    }, [] if satisfied else ["scalar_subquery_boundary_path_missing"]


def _filtered_aggregate_diagnostic_sql(sql: str) -> tuple[str, list[str]] | None:
    """Build a bounded GROUP/HAVING diagnostic while retaining its query path."""
    try:
        ast = parse_one(sql, read="sqlite")
    except Exception:
        return None
    selected: exp.Select | None = None
    aggregate: exp.Count | None = None
    for candidate in ast.find_all(exp.Select):
        having = candidate.args.get("having")
        group = candidate.args.get("group")
        if not isinstance(having, exp.Having) or not isinstance(group, exp.Group):
            continue
        count = next(iter(having.find_all(exp.Count)), None)
        if isinstance(count, exp.Count):
            selected = candidate
            aggregate = count
            break
    if selected is None or aggregate is None:
        return None
    group = selected.args.get("group")
    if not isinstance(group, exp.Group) or not group.expressions:
        return None
    diagnostic = selected.copy()
    diagnostic.set(
        "expressions",
        [item.copy() for item in group.expressions]
        + [exp.alias_(aggregate.copy(), "__witness_count")],
    )
    for key in ("order", "limit", "offset", "qualify", "distinct"):
        diagnostic.set(key, None)
    diagnostic.set(
        "limit",
        exp.Limit(expression=exp.Literal.number(_MAX_JOIN_VALIDATION_GROUPS)),
    )
    table_names = [
        str(table.name)
        for table in selected.find_all(exp.Table)
        if _nearest_select(table) is selected
    ]
    return diagnostic.sql(dialect="sqlite"), list(dict.fromkeys(table_names))


def _validate_filtered_aggregate_boundary(
    world: Any,
    obligation: DistinguishingObligation,
) -> tuple[bool, dict[str, Any], list[str]]:
    spec = next(
        (
            item
            for item in obligation.hard_constraints
            if item.kind == "filtered_aggregate_boundary_path"
        ),
        None,
    )
    if spec is None:
        return False, {}, ["filtered_aggregate_constraint_missing"]
    metadata = dict(spec.metadata)
    execution = getattr(world, "execution", {}) or {}
    context = execution.get("validation_context", {})
    standard_sql = str(
        context.get("standard_sql") or metadata.get("standard_query_sql") or ""
    )
    student_sql = str(
        context.get("student_sql") or metadata.get("student_query_sql") or ""
    )
    if not standard_sql or not student_sql:
        return False, {}, ["filtered_aggregate_validation_context_missing"]

    rows = _table_rows(world, spec.relation)
    column = _column_name(rows, spec.column)
    values = [row.get(column) for row in rows] if column is not None else []
    if spec.value not in values:
        return False, {
            "filtered_aggregate_scope": "query_path",
            "boundary": spec.value,
            "boundary_values_sample": values[:8],
        }, ["filtered_aggregate_boundary_value_missing"]

    standard_diagnostic = _filtered_aggregate_diagnostic_sql(standard_sql)
    student_diagnostic = _filtered_aggregate_diagnostic_sql(student_sql)
    if standard_diagnostic is None or student_diagnostic is None:
        return False, {
            "filtered_aggregate_scope": "query_path",
        }, ["filtered_aggregate_diagnostic_unavailable"]

    standard_sql_diagnostic, standard_tables = standard_diagnostic
    student_sql_diagnostic, student_tables = student_diagnostic
    physical_names = list(dict.fromkeys(standard_tables + student_tables))
    candidate_product = 1
    for table_name in physical_names:
        table_rows = _table_rows(world, table_name)
        if not table_rows:
            return False, {
                "filtered_aggregate_scope": "query_path",
                "post_join_tables": physical_names,
            }, ["filtered_aggregate_table_missing"]
        candidate_product *= len(table_rows)
        if candidate_product > _MAX_JOIN_VALIDATION_PRODUCT:
            return False, {
                "filtered_aggregate_scope": "query_path",
                "candidate_row_product": candidate_product,
                "candidate_row_product_limit": _MAX_JOIN_VALIDATION_PRODUCT,
            }, ["filtered_aggregate_product_limit"]

    standard_rows, standard_error = _execute_sqlite_diagnostic(
        world, standard_sql_diagnostic
    )
    student_rows, student_error = _execute_sqlite_diagnostic(
        world, student_sql_diagnostic
    )
    if standard_rows is None or student_rows is None:
        return False, {
            "filtered_aggregate_scope": "query_path",
            "standard_diagnostic_sql": standard_sql_diagnostic,
            "student_diagnostic_sql": student_sql_diagnostic,
        }, [
            "filtered_aggregate_validator_execution_failed:"
            f"{standard_error or student_error}"
        ]

    standard_groups = [list(row) for row in standard_rows[:8]]
    student_groups = [list(row) for row in student_rows[:8]]
    standard_counter = Counter(tuple(row) for row in standard_rows)
    student_counter = Counter(tuple(row) for row in student_rows)
    distinguished = standard_counter != student_counter
    truncated = (
        len(standard_rows) >= _MAX_JOIN_VALIDATION_GROUPS
        or len(student_rows) >= _MAX_JOIN_VALIDATION_GROUPS
    )
    satisfied = distinguished and not truncated
    diagnostics = []
    if truncated:
        diagnostics.append("filtered_aggregate_diagnostic_group_limit")
    if not distinguished:
        diagnostics.append("filtered_aggregate_boundary_path_missing")
    return satisfied, {
        "filtered_aggregate_scope": "query_path",
        "boundary": spec.value,
        "standard_path_groups": standard_groups,
        "student_path_groups": student_groups,
        "standard_diagnostic_sql": standard_sql_diagnostic,
        "student_diagnostic_sql": student_sql_diagnostic,
        "candidate_row_product": candidate_product,
        "candidate_row_product_limit": _MAX_JOIN_VALIDATION_PRODUCT,
        "path_results_distinguished": distinguished,
    }, diagnostics


def _validate_joined_aggregate_boundary(
    world: Any,
    metadata: dict[str, Any],
    boundary: Any,
) -> tuple[bool, dict[str, Any], list[str]] | None:
    """Validate the aggregate over the joined relation, not a base table.

    Only bounded SQLite-compatible worlds enter this diagnostic path.  The
    candidate Cartesian product is capped before execution, and SQLite's VM
    progress handler supplies a second hard stop.  Returning ``None`` means
    the query has no direct multi-table aggregate and the base-table validator
    remains appropriate.
    """
    execution = getattr(world, "execution", {}) or {}
    context = execution.get("validation_context", {})
    standard_sql = str(context.get("standard_sql") or "")
    if not standard_sql:
        return None
    try:
        ast = parse_one(standard_sql, read="sqlite")
    except Exception as exc:  # noqa: BLE001 - validator evidence, never fatal.
        return False, {
            "aggregate_cardinality_scope": "post_join",
        }, [f"post_join_validator_parse_failed:{type(exc).__name__}"]

    function = str(metadata.get("standard_aggregate_function") or "COUNT").upper()
    selected: exp.Select | None = None
    aggregate: exp.Expression | None = None
    for having in ast.find_all(exp.Having):
        candidate_select = _nearest_select(having)
        if not isinstance(candidate_select, exp.Select):
            continue
        candidate = next(
            (
                item
                for item in having.find_all(*_AGGREGATE_TYPES)
                if type(item).__name__.upper() == function
            ),
            None,
        )
        if candidate is not None:
            selected = candidate_select
            aggregate = candidate
            break
    if selected is None or aggregate is None:
        return None

    direct_tables = [
        table
        for table in selected.find_all(exp.Table)
        if _nearest_select(table) is selected
    ]
    physical_names = list(dict.fromkeys(str(table.name) for table in direct_tables))
    if len(physical_names) <= 1:
        return None
    world_tables = {
        str(name).lower(): (str(name), rows)
        for name, rows in world.database.items()
    }
    resolved_tables: list[tuple[str, list[dict[str, Any]]]] = []
    for name in physical_names:
        resolved = world_tables.get(name.lower())
        if resolved is None:
            return False, {
                "aggregate_cardinality_scope": "post_join",
                "post_join_tables": physical_names,
            }, ["post_join_validator_table_missing"]
        resolved_tables.append(resolved)

    candidate_product = 1
    for _name, rows in resolved_tables:
        candidate_product *= len(rows)
        if candidate_product > _MAX_JOIN_VALIDATION_PRODUCT:
            return False, {
                "aggregate_cardinality_scope": "post_join",
                "post_join_tables": physical_names,
                "candidate_row_product": candidate_product,
                "candidate_row_product_limit": _MAX_JOIN_VALIDATION_PRODUCT,
            }, ["post_join_validator_product_limit"]

    group = selected.args.get("group")
    if not isinstance(group, exp.Group):
        group_expressions: list[exp.Expression] = []
    else:
        group_expressions = [item.copy() for item in group.expressions]
    diagnostic = selected.copy()
    diagnostic.set(
        "expressions",
        group_expressions
        + [exp.alias_(aggregate.copy(), "__witness_aggregate")],
    )
    for key in ("having", "order", "limit", "offset", "qualify", "distinct"):
        diagnostic.set(key, None)
    diagnostic.set(
        "limit",
        exp.Limit(expression=exp.Literal.number(_MAX_JOIN_VALIDATION_GROUPS)),
    )
    diagnostic_sql = diagnostic.sql(dialect="sqlite")

    connection = sqlite3.connect(":memory:")
    progress_calls = 0

    def _abort_large_diagnostic() -> int:
        nonlocal progress_calls
        progress_calls += 1
        return int(progress_calls > 250)

    connection.set_progress_handler(_abort_large_diagnostic, 1000)
    try:
        for table_name, rows in world.database.items():
            columns = list(dict.fromkeys(
                column for row in rows for column in row
            ))
            if not columns:
                continue
            declarations = []
            for column in columns:
                column_values = [row.get(column) for row in rows]
                declarations.append(
                    f"{_quote_sqlite_identifier(column)} "
                    f"{_sqlite_fixture_type(column_values)}"
                )
            connection.execute(
                f"CREATE TABLE {_quote_sqlite_identifier(table_name)} "
                f"({', '.join(declarations)})"
            )
            placeholders = ", ".join("?" for _ in columns)
            connection.executemany(
                f"INSERT INTO {_quote_sqlite_identifier(table_name)} "
                f"VALUES ({placeholders})",
                [tuple(row.get(column) for column in columns) for row in rows],
            )
        result_rows = connection.execute(diagnostic_sql).fetchall()
    except Exception as exc:  # noqa: BLE001 - convert failure into evidence.
        return False, {
            "aggregate_cardinality_scope": "post_join",
            "post_join_tables": physical_names,
            "candidate_row_product": candidate_product,
            "diagnostic_sql": diagnostic_sql,
        }, [f"post_join_validator_execution_failed:{type(exc).__name__}"]
    finally:
        connection.close()

    truncated = len(result_rows) >= _MAX_JOIN_VALIDATION_GROUPS
    aggregate_values = {
        str(tuple(row[:-1])): row[-1]
        for row in result_rows[: _MAX_JOIN_VALIDATION_GROUPS - 1]
    }
    satisfied = not truncated and any(
        value == boundary for value in aggregate_values.values()
    )
    diagnostics = []
    if truncated:
        diagnostics.append("post_join_validator_group_limit")
    if not satisfied:
        diagnostics.append("aggregate_boundary_group_missing_after_join")
    return satisfied, {
        "aggregate_cardinality_scope": "post_join",
        "post_join_tables": physical_names,
        "candidate_row_product": candidate_product,
        "candidate_row_product_limit": _MAX_JOIN_VALIDATION_PRODUCT,
        "post_join_aggregate_values": aggregate_values,
        "aggregate_function": function,
        "boundary": boundary,
        "diagnostic_sql": diagnostic_sql,
    }, diagnostics


def _validate_aggregate_boundary(world: Any, obligation: DistinguishingObligation) -> tuple[bool, dict[str, Any], list[str]]:
    spec = next((item for item in obligation.hard_constraints if item.kind == "aggregate_boundary_group"), None)
    if spec is None:
        return False, {}, ["aggregate_constraint_missing"]
    rows = _table_rows(world, spec.relation)
    if not rows:
        return False, {}, ["aggregate_table_missing"]
    metadata = dict(spec.metadata)
    joined_validation = _validate_joined_aggregate_boundary(
        world,
        metadata,
        spec.value,
    )
    if joined_validation is not None:
        return joined_validation
    keys = metadata.get("standard_group_columns") or metadata.get("student_group_columns")
    resolved = [_column_name(rows, str(key).split(".")[-1]) for key in keys or ()]
    if keys and any(item is None for item in resolved):
        return False, {}, ["aggregate_group_column_missing"]
    groups: dict[tuple[Any, ...], list[dict[str, Any]]] = {(): list(rows)} if not keys else {}
    if keys:
        for row in rows:
            key = tuple(row.get(column) for column in resolved)
            groups.setdefault(key, []).append(row)
    counts = Counter({key: len(items) for key, items in groups.items()})
    boundary = spec.value
    function = str(metadata.get("standard_aggregate_function") or "COUNT").upper()
    argument = str(metadata.get("standard_aggregate_argument") or "*").strip()
    distinct = bool(metadata.get("standard_aggregate_distinct", False))
    if argument.upper().startswith("DISTINCT "):
        distinct = True
        argument = argument[9:].strip()
    argument_column = _column_name(rows, argument.split(".")[-1]) if argument != "*" else None
    aggregate_values: dict[tuple[Any, ...], Any] = {}
    heterogeneous_extreme_groups: list[tuple[Any, ...]] = []
    for key, items in groups.items():
        values = [item.get(argument_column) for item in items] if argument_column else [1] * len(items)
        non_null = [value for value in values if value is not None]
        if function == "COUNT":
            aggregate_values[key] = (
                len(set(non_null)) if distinct and argument != "*"
                else len(non_null) if argument != "*"
                else len(items)
            )
        elif function == "SUM":
            source = list(dict.fromkeys(non_null)) if distinct else non_null
            aggregate_values[key] = sum(value for value in source if isinstance(value, (int, float)))
        elif function == "AVG":
            source = list(dict.fromkeys(non_null)) if distinct else non_null
            numeric = [value for value in source if isinstance(value, (int, float))]
            aggregate_values[key] = sum(numeric) / len(numeric) if numeric else None
        elif function == "MIN":
            if not non_null:
                aggregate_values[key] = None
            else:
                try:
                    aggregate_values[key] = min(non_null)
                except TypeError:
                    heterogeneous_extreme_groups.append(key)
                    aggregate_values[key] = min(
                        non_null,
                        key=_sql_extreme_order_key,
                    )
        elif function == "MAX":
            if not non_null:
                aggregate_values[key] = None
            else:
                try:
                    aggregate_values[key] = max(non_null)
                except TypeError:
                    heterogeneous_extreme_groups.append(key)
                    aggregate_values[key] = max(
                        non_null,
                        key=_sql_extreme_order_key,
                    )
        else:
            return False, {"aggregate_function": function}, ["aggregate_function_not_supported"]
    satisfied = any(value == boundary for value in aggregate_values.values())
    diagnostics = (
        ["aggregate_mixed_types_ordered_deterministically"]
        if heterogeneous_extreme_groups
        else []
    )
    if not satisfied:
        diagnostics.append("aggregate_boundary_group_missing")
    return satisfied, {
        "aggregate_group_columns": list(keys or ()),
        "aggregate_group_counts": {str(key): count for key, count in counts.items()},
        "aggregate_values": {str(key): value for key, value in aggregate_values.items()},
        "aggregate_function": function,
        "aggregate_argument": argument,
        "aggregate_distinct": distinct,
        "boundary": boundary,
        "heterogeneous_extreme_groups": [
            str(key) for key in heterogeneous_extreme_groups
        ],
    }, diagnostics


def _aggregate_function_value(
    function: str,
    items: list[dict[str, Any]],
    argument_column: str | None,
    *,
    distinct: bool,
) -> tuple[bool, Any]:
    function = function.upper()
    if argument_column is None:
        values = [1] * len(items)
    else:
        values = [item.get(argument_column) for item in items]
    non_null = [value for value in values if value is not None]
    if distinct and argument_column is not None:
        non_null = list(dict.fromkeys(non_null))
    if function == "COUNT":
        return True, len(items) if argument_column is None else len(non_null)
    if function == "SUM":
        numeric = [value for value in non_null if isinstance(value, (int, float))]
        return True, sum(numeric) if numeric else None
    if function == "AVG":
        numeric = [value for value in non_null if isinstance(value, (int, float))]
        return True, sum(numeric) / len(numeric) if numeric else None
    if function == "MIN":
        return True, min(non_null, key=_sql_extreme_order_key) if non_null else None
    if function == "MAX":
        return True, max(non_null, key=_sql_extreme_order_key) if non_null else None
    return False, None


def _validate_aggregate_function_separation(
    world: Any,
    obligation: DistinguishingObligation,
) -> tuple[bool, dict[str, Any], list[str]]:
    spec = next(
        (
            item
            for item in obligation.hard_constraints
            if item.kind == "aggregate_function_separation"
        ),
        None,
    )
    if spec is None:
        return False, {}, ["aggregate_function_constraint_missing"]
    rows = _table_rows(world, spec.relation)
    if not rows:
        return False, {}, ["aggregate_function_table_missing"]
    metadata = dict(spec.metadata)
    standard_function = str(
        metadata.get("standard_aggregate_function") or ""
    ).upper()
    student_function = str(
        metadata.get("student_aggregate_function") or ""
    ).upper()
    if not standard_function or not student_function:
        return False, {}, ["aggregate_function_metadata_missing"]

    keys = metadata.get("standard_group_columns") or ()
    group_columns = [
        _column_name(rows, str(key).split(".")[-1]) for key in keys
    ]
    if any(column is None for column in group_columns):
        return False, {}, ["aggregate_function_group_column_missing"]
    resolved_groups = [str(column) for column in group_columns]
    groups: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        key = tuple(row.get(column) for column in resolved_groups)
        groups[key].append(row)

    def _argument(label: str) -> tuple[str | None, bool]:
        raw = str(metadata.get(f"{label}_aggregate_argument") or "*").strip()
        distinct = bool(metadata.get(f"{label}_aggregate_distinct", False))
        if raw.upper().startswith("DISTINCT "):
            distinct = True
            raw = raw[9:].strip()
        if raw == "*":
            return None, distinct
        return _column_name(rows, raw.split(".")[-1].strip('`"[] ')), distinct

    standard_column, standard_distinct = _argument("standard")
    student_column, student_distinct = _argument("student")
    standard_raw_argument = str(
        metadata.get("standard_aggregate_argument") or "*"
    ).strip()
    student_raw_argument = str(
        metadata.get("student_aggregate_argument") or "*"
    ).strip()
    if standard_raw_argument != "*" and standard_column is None:
        return False, {}, ["standard_aggregate_argument_missing"]
    if student_raw_argument != "*" and student_column is None:
        return False, {}, ["student_aggregate_argument_missing"]

    values: dict[str, dict[str, Any]] = {}
    supported = True
    for key, items in groups.items():
        standard_supported, standard_value = _aggregate_function_value(
            standard_function,
            items,
            standard_column,
            distinct=standard_distinct,
        )
        student_supported, student_value = _aggregate_function_value(
            student_function,
            items,
            student_column,
            distinct=student_distinct,
        )
        supported = supported and standard_supported and student_supported
        values[str(key)] = {
            "standard": standard_value,
            "student": student_value,
        }
    if not supported:
        return False, {
            "standard_aggregate_function": standard_function,
            "student_aggregate_function": student_function,
        }, ["aggregate_function_not_supported"]
    separated_groups = [
        key
        for key, pair in values.items()
        if pair["standard"] != pair["student"]
    ]
    satisfied = bool(separated_groups)
    return satisfied, {
        "aggregate_group_columns": list(keys),
        "standard_aggregate_function": standard_function,
        "student_aggregate_function": student_function,
        "aggregate_function_values": values,
        "separated_groups": separated_groups,
    }, [] if satisfied else ["aggregate_function_results_not_separated"]


def _window_order_items(
    metadata: dict[str, Any],
    side: str,
) -> list[tuple[str, bool, bool]]:
    """Return ``(expression, descending, nulls_first)`` for one window side."""
    declared = metadata.get(f"{side}_window_order_items") or ()
    result: list[tuple[str, bool, bool]] = []
    for item in declared:
        if not isinstance(item, (tuple, list)) or len(item) < 3:
            continue
        result.append((str(item[0]), bool(item[1]), bool(item[2])))
    if result:
        return result

    # Keep hand-built validator tests and older serialized obligations
    # compatible.  The compiler now supplies order_items, but a legacy
    # obligation may only contain ``score ASC`` or ``ORDER BY score``.
    order_sql = str(metadata.get(f"{side}_window_order") or "").strip()
    if not order_sql:
        return []
    fragment = order_sql
    if not fragment.upper().startswith("ORDER BY"):
        fragment = f"ORDER BY {fragment}"
    try:
        parsed = parse_one(f"SELECT * FROM _window_probe {fragment}", read="sqlite")
        order = parsed.args.get("order")
        if isinstance(order, exp.Order):
            for item in order.expressions or ():
                ordered = item if isinstance(item, exp.Ordered) else None
                expression = ordered.this if ordered is not None else item
                if not isinstance(expression, exp.Expression):
                    continue
                descending = bool(ordered.args.get("desc")) if ordered else False
                nulls_first = (
                    bool(ordered.args.get("nulls_first"))
                    if ordered and ordered.args.get("nulls_first") is not None
                    else not descending
                )
                result.append((expression.sql(), descending, nulls_first))
    except Exception:  # noqa: BLE001 - validator must fail closed, not raise.
        return []
    return result


def _window_column_names(
    metadata: dict[str, Any],
    side: str,
    items: list[tuple[str, bool, bool]],
) -> list[str]:
    declared = metadata.get(f"{side}_window_order_columns") or ()
    if declared:
        return [str(item).split(".")[-1].strip('`" ') for item in declared]
    return [str(item[0]).split(".")[-1].strip('`" ') for item in items]


def _window_partition_names(metadata: dict[str, Any], side: str) -> list[str]:
    return [
        str(item).split(".")[-1].strip('`" ')
        for item in (metadata.get(f"{side}_window_partition") or ())
    ]


def _partition_relation_diff(
    rows: list[dict[str, Any]],
    standard_columns: list[str],
    student_columns: list[str],
) -> bool:
    """Check whether two partition layouts classify any row pair differently."""
    standard_resolved = [_column_name(rows, item) for item in standard_columns]
    student_resolved = [_column_name(rows, item) for item in student_columns]
    if any(item is None for item in standard_resolved + student_resolved):
        return False
    standard_keys = [
        tuple(row.get(item) for item in standard_resolved)
        if standard_resolved else ()
        for row in rows
    ]
    student_keys = [
        tuple(row.get(item) for item in student_resolved)
        if student_resolved else ()
        for row in rows
    ]
    return any(
        (standard_keys[left] == standard_keys[right])
        != (student_keys[left] == student_keys[right])
        for left in range(len(rows))
        for right in range(left + 1, len(rows))
    )


def _validate_window_paths(world: Any, obligation: DistinguishingObligation) -> tuple[bool, dict[str, Any], list[str]]:
    spec = next((item for item in obligation.hard_constraints if item.kind == "window_partitions_and_ties"), None)
    if spec is None:
        return False, {}, ["window_constraint_missing"]
    metadata = dict(spec.metadata)
    rows = _table_rows(
        world,
        str(
            metadata.get("standard_window_source_table")
            or metadata.get("student_window_source_table")
            or spec.relation
        ),
    )
    if not rows:
        return False, {}, ["window_table_missing"]

    standard_partition = _window_partition_names(metadata, "standard")
    student_partition = _window_partition_names(metadata, "student")
    standard_items = _window_order_items(metadata, "standard")
    student_items = _window_order_items(metadata, "student")
    standard_order_columns = _window_column_names(metadata, "standard", standard_items)
    standard_resolved_partition = [_column_name(rows, item) for item in standard_partition]
    resolved_order = [_column_name(rows, item) for item in standard_order_columns]
    if any(item is None for item in standard_resolved_partition + resolved_order):
        return False, {
            "partition_columns": standard_partition,
            "order_columns": standard_order_columns,
        }, ["window_column_missing"]

    partitioned_order_keys: dict[tuple[Any, ...], list[tuple[Any, ...]]] = {}
    for row in rows:
        partition_key = (
            tuple(row.get(item) for item in standard_resolved_partition)
            if standard_resolved_partition
            else ()
        )
        order_key = tuple(row.get(item) for item in resolved_order)
        partitioned_order_keys.setdefault(partition_key, []).append(order_key)
    partition_keys = Counter({
        key: len(values) for key, values in partitioned_order_keys.items()
    })
    peer_group_count = sum(
        1
        for values in partitioned_order_keys.values()
        for count in Counter(values).values()
        if count >= 2
    )
    distinct_order_partition_count = sum(
        1 for values in partitioned_order_keys.values() if len(set(values)) >= 2
    )

    standard_function = str(metadata.get("standard_window_function") or "").upper()
    student_function = str(metadata.get("student_window_function") or "").upper()
    nulls_changed = any(
        len(standard_item) >= 3
        and len(student_item) >= 3
        and bool(standard_item[2]) != bool(student_item[2])
        for standard_item, student_item in zip(standard_items, student_items)
    )
    partition_changed = (
        "student_window_partition" in metadata
        and tuple(standard_partition) != tuple(student_partition)
    )
    partition_relation_changed = (
        _partition_relation_diff(rows, standard_partition, student_partition)
        if partition_changed
        else False
    )
    has_multiple_partitions = len(partition_keys) >= 2 if standard_resolved_partition else True
    has_order = bool(standard_items or metadata.get("standard_window_order"))
    ranking_functions = {
        "RANK", "DENSE_RANK", "ROW_NUMBER", "NTILE", "PERCENT_RANK", "CUME_DIST",
    }
    requires_tie = bool(
        has_order
        and not nulls_changed
        and (
            bool({standard_function, student_function} & ranking_functions)
            or (not standard_function and not student_function)
        )
    )
    has_tie = peer_group_count > 0 if has_order else True
    requires_distinct_order_path = bool(
        {standard_function, student_function} & {"FIRST_VALUE", "LAST_VALUE"}
        or (
            "student_window_order" in metadata
            and metadata.get("standard_window_order") != metadata.get("student_window_order")
        )
        or (
            "student_window_frame" in metadata
            and metadata.get("standard_window_frame") != metadata.get("student_window_frame")
        )
    )
    has_distinct_order_path = distinct_order_partition_count > 0 if has_order else True

    null_order_path = True
    if nulls_changed:
        null_order_path = any(
            row.get(item) is None
            for row in rows
            for item in resolved_order
        ) and len({
            row.get(item)
            for item in resolved_order
            for row in rows
            if row.get(item) is not None
        }) >= 2

    satisfied = (
        (partition_relation_changed if partition_changed else has_multiple_partitions)
        and (not requires_tie or has_tie)
        and (not requires_distinct_order_path or has_distinct_order_path)
        and null_order_path
    )
    evidence = {
        "partition_columns": standard_partition,
        "student_partition_columns": student_partition,
        "order_columns": standard_order_columns,
        "partition_count": len(partition_keys),
        "order_tie_count": peer_group_count,
        "requires_tie": requires_tie,
        "requires_distinct_order_path": requires_distinct_order_path,
        "distinct_order_partition_count": distinct_order_partition_count,
        "partition_layout_changed": partition_changed,
        "partition_relation_changed": partition_relation_changed,
        "nulls_first_changed": nulls_changed,
        "null_order_path": null_order_path,
    }
    diagnostics: list[str] = []
    if partition_changed and not partition_relation_changed:
        diagnostics.append("window_partition_relation_missing")
    if requires_tie and not has_tie:
        diagnostics.append("window_tie_path_missing")
    if requires_distinct_order_path and not has_distinct_order_path:
        diagnostics.append("window_distinct_order_path_missing")
    if nulls_changed and not null_order_path:
        diagnostics.append("window_null_order_path_missing")
    return satisfied, evidence, diagnostics


def _order_keys(value: Any) -> list[tuple[str, bool]]:
    """Normalize serialized planner metadata without guessing missing keys."""
    result: list[tuple[str, bool]] = []
    for item in value or ():
        if not isinstance(item, (tuple, list)) or len(item) < 2:
            continue
        expression = str(item[0] or "").strip()
        if expression:
            result.append((expression, bool(item[1])))
    return result


def _simple_order_column(expression: str) -> str | None:
    """Return the physical column for a simple ORDER BY key.

    Function calls, arithmetic expressions, ordinals, and projection aliases
    need expression-aware materialization.  Treating an arbitrary identifier
    found inside those expressions as the key would create false validator
    positives, so the first migration stage deliberately fails closed.
    """
    try:
        node = parse_one(expression, read="sqlite")
    except Exception:
        return None
    if isinstance(node, exp.Ordered):
        node = node.this
    if not isinstance(node, exp.Column) or not node.name:
        return None
    return str(node.name)


def _same_order_expression(left: str, right: str) -> bool:
    try:
        return parse_one(left, read="sqlite") == parse_one(right, read="sqlite")
    except Exception:
        return "".join(left.lower().split()) == "".join(right.lower().split())


def _order_pair_with_prefix_tie(
    rows: list[dict[str, Any]],
    prefix_columns: list[str],
    discriminator_column: str,
) -> tuple[int, int] | None:
    grouped: dict[tuple[Any, ...], list[tuple[int, Any]]] = {}
    for index, row in enumerate(rows):
        prefix = tuple(row.get(column) for column in prefix_columns)
        grouped.setdefault(prefix, []).append((index, row.get(discriminator_column)))
    for candidates in grouped.values():
        for left_position, (left_index, left_value) in enumerate(candidates):
            if left_value is None:
                continue
            for right_index, right_value in candidates[left_position + 1:]:
                if right_value is not None and right_value != left_value:
                    return left_index, right_index
    return None


def _validate_order_paths(world: Any, obligation: DistinguishingObligation) -> tuple[bool, dict[str, Any], list[str]]:
    spec = next(
        (item for item in obligation.hard_constraints if item.kind == "order_key_separation"),
        None,
    )
    if spec is None:
        return False, {}, ["order_constraint_missing"]
    metadata = dict(spec.metadata)
    standard_keys = _order_keys(metadata.get("standard_order_keys"))
    student_keys = _order_keys(metadata.get("student_order_keys"))
    relation = str(spec.relation or metadata.get("standard_source_table") or "")
    if not relation:
        return False, {}, ["order_table_metadata_missing"]
    rows = _table_rows(world, relation)
    if not rows:
        return False, {"relation": relation}, ["order_table_missing"]
    if not standard_keys and not student_keys:
        return False, {"relation": relation}, ["order_key_metadata_missing"]

    diff_type = obligation.diff_type
    prefix_keys: list[tuple[str, bool]]
    discriminator_key: tuple[str, bool] | None = None
    changed_index: int | None = None
    if diff_type == "order_direction_changed":
        if len(standard_keys) != len(student_keys):
            return False, {}, ["order_direction_metadata_inconsistent"]
        changed_indexes = [
            index
            for index, (standard, student) in enumerate(zip(standard_keys, student_keys))
            if _same_order_expression(standard[0], student[0]) and standard[1] != student[1]
        ]
        if not changed_indexes or any(
            not _same_order_expression(standard[0], student[0])
            for standard, student in zip(standard_keys, student_keys)
        ):
            return False, {}, ["order_direction_metadata_inconsistent"]
        changed_index = changed_indexes[0]
        prefix_keys = standard_keys[:changed_index]
        discriminator_key = standard_keys[changed_index]
    elif diff_type == "order_by_tiebreaker_missing":
        prefix_length = len(student_keys)
        if len(standard_keys) <= prefix_length or any(
            not _same_order_expression(standard[0], student[0])
            for standard, student in zip(standard_keys[:prefix_length], student_keys)
        ):
            return False, {}, ["order_tiebreaker_metadata_inconsistent"]
        changed_index = prefix_length
        prefix_keys = standard_keys[:prefix_length]
        discriminator_key = standard_keys[prefix_length]
    elif diff_type == "order_by_key_added":
        prefix_length = len(standard_keys)
        if len(student_keys) <= prefix_length or any(
            not _same_order_expression(standard[0], student[0])
            for standard, student in zip(standard_keys, student_keys[:prefix_length])
        ):
            return False, {}, ["order_added_key_metadata_inconsistent"]
        changed_index = prefix_length
        prefix_keys = standard_keys
        discriminator_key = student_keys[prefix_length]
    else:
        return False, {"diff_type": diff_type}, ["order_diff_type_not_supported"]

    requested_columns = [
        _simple_order_column(expression)
        for expression, _descending in (*prefix_keys, discriminator_key)
    ]
    if any(column is None for column in requested_columns):
        return False, {
            "relation": relation,
            "standard_order_keys": standard_keys,
            "student_order_keys": student_keys,
        }, ["order_expression_not_supported"]
    resolved_columns = [_column_name(rows, str(column)) for column in requested_columns]
    if any(column is None for column in resolved_columns):
        return False, {
            "relation": relation,
            "requested_columns": requested_columns,
        }, ["order_column_missing"]
    prefix_columns = [str(column) for column in resolved_columns[:-1]]
    discriminator_column = str(resolved_columns[-1])
    pair = _order_pair_with_prefix_tie(rows, prefix_columns, discriminator_column)
    evidence = {
        "relation": relation,
        "diff_type": diff_type,
        "standard_order_keys": standard_keys,
        "student_order_keys": student_keys,
        "changed_key_index": changed_index,
        "prefix_columns": prefix_columns,
        "discriminator_column": discriminator_column,
        "distinguishing_row_indexes": list(pair) if pair else [],
    }
    return bool(pair), evidence, [] if pair else ["order_key_separation_not_materialized"]


_UNSUPPORTED_TRUTH_VALUE = object()


def _predicate_expression(sql: str) -> exp.Expression | None:
    body = re.sub(r"(?is)^\s*WHERE\s+", "", str(sql or "")).strip()
    if not body:
        return None
    try:
        select = parse_one(f"SELECT 1 WHERE {body}", read="sqlite")
    except Exception:
        return None
    where = select.args.get("where") if isinstance(select, exp.Select) else None
    return where.this if isinstance(where, exp.Where) else None


def _literal_value(node: exp.Expression) -> Any:
    if isinstance(node, exp.Null):
        return None
    if isinstance(node, exp.Boolean):
        return bool(node.this)
    if not isinstance(node, exp.Literal):
        return _UNSUPPORTED_TRUTH_VALUE
    if node.is_string:
        return str(node.this)
    text = str(node.this)
    try:
        return int(text)
    except ValueError:
        try:
            return float(text)
        except ValueError:
            return text


def _row_column_value(row: dict[str, Any], column: exp.Column) -> Any:
    actual = next(
        (name for name in row if name.lower() == str(column.name).lower()),
        None,
    )
    return row.get(actual) if actual else _UNSUPPORTED_TRUTH_VALUE


def _sql_and(left: Any, right: Any) -> bool | None:
    if left is False or right is False:
        return False
    if left is True and right is True:
        return True
    return None


def _sql_or(left: Any, right: Any) -> bool | None:
    if left is True or right is True:
        return True
    if left is False and right is False:
        return False
    return None


def _evaluate_predicate(node: exp.Expression, row: dict[str, Any]) -> Any:
    while isinstance(node, exp.Paren):
        node = node.this
    if isinstance(node, exp.And):
        left = _evaluate_predicate(node.left, row)
        right = _evaluate_predicate(node.right, row)
        if _UNSUPPORTED_TRUTH_VALUE in (left, right):
            return _UNSUPPORTED_TRUTH_VALUE
        return _sql_and(left, right)
    if isinstance(node, exp.Or):
        left = _evaluate_predicate(node.left, row)
        right = _evaluate_predicate(node.right, row)
        if _UNSUPPORTED_TRUTH_VALUE in (left, right):
            return _UNSUPPORTED_TRUTH_VALUE
        return _sql_or(left, right)
    if isinstance(node, exp.Not):
        value = _evaluate_predicate(node.this, row)
        if value is _UNSUPPORTED_TRUTH_VALUE or value is None:
            return value
        return not value
    if isinstance(node, exp.Column):
        value = _row_column_value(row, node)
        return value if value is _UNSUPPORTED_TRUTH_VALUE else bool(value) if value is not None else None
    if isinstance(
        node,
        (
            exp.EQ,
            exp.NEQ,
            exp.GT,
            exp.GTE,
            exp.LT,
            exp.LTE,
            exp.NullSafeEQ,
            exp.NullSafeNEQ,
        ),
    ):
        left = _row_column_value(row, node.left) if isinstance(node.left, exp.Column) else _literal_value(node.left)
        right = _row_column_value(row, node.right) if isinstance(node.right, exp.Column) else _literal_value(node.right)
        if _UNSUPPORTED_TRUTH_VALUE in (left, right):
            return _UNSUPPORTED_TRUTH_VALUE
        if isinstance(node, exp.NullSafeEQ):
            return left == right
        if isinstance(node, exp.NullSafeNEQ):
            return left != right
        if left is None or right is None:
            return None
        try:
            if isinstance(node, exp.EQ):
                return left == right
            if isinstance(node, exp.NEQ):
                return left != right
            if isinstance(node, exp.GT):
                return left > right
            if isinstance(node, exp.GTE):
                return left >= right
            if isinstance(node, exp.LT):
                return left < right
            return left <= right
        except TypeError:
            return _UNSUPPORTED_TRUTH_VALUE
    if isinstance(node, exp.Is):
        left = _row_column_value(row, node.this) if isinstance(node.this, exp.Column) else _literal_value(node.this)
        right = _literal_value(node.expression)
        if _UNSUPPORTED_TRUTH_VALUE in (left, right):
            return _UNSUPPORTED_TRUTH_VALUE
        return left is None if right is None else left is right
    if isinstance(node, exp.Between):
        value = _row_column_value(row, node.this) if isinstance(node.this, exp.Column) else _literal_value(node.this)
        low = _literal_value(node.args.get("low"))
        high = _literal_value(node.args.get("high"))
        if _UNSUPPORTED_TRUTH_VALUE in (value, low, high):
            return _UNSUPPORTED_TRUTH_VALUE
        if value is None or low is None or high is None:
            return None
        try:
            return low <= value <= high
        except TypeError:
            return _UNSUPPORTED_TRUTH_VALUE
    if isinstance(node, exp.In) and node.args.get("query") is None:
        value = _row_column_value(row, node.this) if isinstance(node.this, exp.Column) else _literal_value(node.this)
        candidates = [_literal_value(item) for item in node.expressions or ()]
        if value is _UNSUPPORTED_TRUTH_VALUE or _UNSUPPORTED_TRUTH_VALUE in candidates:
            return _UNSUPPORTED_TRUTH_VALUE
        if value is None:
            return None
        if value in candidates:
            return True
        return None if None in candidates else False
    if isinstance(node, (exp.Like, exp.ILike)):
        value = _row_column_value(row, node.this) if isinstance(node.this, exp.Column) else _literal_value(node.this)
        pattern = _literal_value(node.expression)
        if _UNSUPPORTED_TRUTH_VALUE in (value, pattern):
            return _UNSUPPORTED_TRUTH_VALUE
        if value is None or pattern is None:
            return None
        escape = _literal_value(node.args.get("escape"))
        if escape is _UNSUPPORTED_TRUTH_VALUE or escape is None:
            escape = "\\"
        try:
            return like_matches(
                pattern,
                value,
                escape=str(escape),
                case_insensitive=isinstance(node, exp.ILike),
            )
        except RegexEvaluationError:
            return _UNSUPPORTED_TRUTH_VALUE
    if isinstance(node, exp.RegexpLike):
        value = (
            _row_column_value(row, node.this)
            if isinstance(node.this, exp.Column)
            else _literal_value(node.this)
        )
        pattern = _literal_value(node.expression)
        flag = _literal_value(node.args.get("flag"))
        if _UNSUPPORTED_TRUTH_VALUE in (value, pattern):
            return _UNSUPPORTED_TRUTH_VALUE
        try:
            return regex_matches(pattern, value, flags=str(flag or ""))
        except RegexEvaluationError:
            return _UNSUPPORTED_TRUTH_VALUE
    return _UNSUPPORTED_TRUTH_VALUE


def _logical_leaves(node: exp.Expression) -> list[exp.Expression]:
    while isinstance(node, exp.Paren):
        node = node.this
    if isinstance(node, (exp.And, exp.Or)):
        return _logical_leaves(node.left) + _logical_leaves(node.right)
    return [node]


def _truth_label(value: Any) -> str:
    if value is True:
        return "T"
    if value is False:
        return "F"
    return "U"


def _validate_boolean_truth_table(world: Any, obligation: DistinguishingObligation) -> tuple[bool, dict[str, Any], list[str]]:
    spec = next(
        (item for item in obligation.hard_constraints if item.kind == "boolean_truth_table"),
        None,
    )
    if spec is None:
        return False, {}, ["boolean_constraint_missing"]
    metadata = dict(spec.metadata)
    standard = _predicate_expression(str(metadata.get("standard_predicate_sql") or ""))
    student = _predicate_expression(str(metadata.get("student_predicate_sql") or ""))
    if standard is None or student is None:
        return False, {}, ["boolean_predicate_metadata_missing"]
    relation = str(spec.relation or metadata.get("standard_source_table") or "")
    rows = _table_rows(world, relation)
    if not relation or not rows:
        return False, {"relation": relation}, ["boolean_table_missing"]
    leaves = _logical_leaves(standard)
    assignments: set[tuple[str, ...]] = set()
    distinguishing_rows: list[int] = []
    unsupported_rows: list[int] = []
    for index, row in enumerate(rows):
        leaf_values = [_evaluate_predicate(leaf, row) for leaf in leaves]
        standard_value = _evaluate_predicate(standard, row)
        student_value = _evaluate_predicate(student, row)
        if _UNSUPPORTED_TRUTH_VALUE in (*leaf_values, standard_value, student_value):
            unsupported_rows.append(index)
            continue
        assignments.add(tuple(_truth_label(value) for value in leaf_values))
        if (standard_value is True) != (student_value is True):
            distinguishing_rows.append(index)

    leaf_columns = [
        {str(column.name).lower() for column in leaf.find_all(exp.Column)}
        for leaf in leaves
    ]
    require_binary_truth_table = (
        len(leaves) == 2
        and all(len(columns) == 1 for columns in leaf_columns)
        and len(set().union(*leaf_columns)) == 2
    )
    required_assignments = {("T", "T"), ("T", "F"), ("F", "T"), ("F", "F")}
    full_truth_table = required_assignments <= assignments
    satisfied = bool(distinguishing_rows) and (
        full_truth_table if require_binary_truth_table else bool(assignments)
    )
    evidence = {
        "relation": relation,
        "leaf_sql": [leaf.sql(dialect="sqlite") for leaf in leaves],
        "truth_assignments": [list(item) for item in sorted(assignments)],
        "requires_binary_truth_table": require_binary_truth_table,
        "full_binary_truth_table": full_truth_table,
        "distinguishing_row_indexes": distinguishing_rows,
        "unsupported_row_indexes": unsupported_rows,
    }
    diagnostics = [] if satisfied else [
        "boolean_truth_table_not_materialized"
        if require_binary_truth_table and not full_truth_table
        else "boolean_distinguishing_path_missing"
    ]
    return satisfied, evidence, diagnostics


def _set_projection_values(
    world: Any,
    table: str,
    columns: Any,
) -> set[tuple[Any, ...]] | None:
    rows = _table_rows(world, str(table or ""))
    if not rows or not columns:
        return None
    resolved = [_column_name(rows, str(column).split(".")[-1]) for column in columns]
    if any(column is None for column in resolved):
        return None
    return {
        tuple(row.get(str(column)) for column in resolved)
        for row in rows
    }


def _set_operation_node(ast: exp.Expression | None) -> exp.Expression | None:
    if isinstance(ast, (exp.Union, exp.Intersect, exp.Except)):
        return ast
    return ast.find(exp.Union, exp.Intersect, exp.Except) if ast is not None else None


def _set_operation_name(node: exp.Expression | None) -> str:
    if isinstance(node, exp.Union):
        return "UNION"
    if isinstance(node, exp.Intersect):
        return "INTERSECT"
    if isinstance(node, exp.Except):
        return "EXCEPT"
    return ""


def _set_counter_result(
    operator: str,
    left: Counter,
    right: Counter,
    *,
    all_rows: bool,
) -> Counter:
    if operator == "UNION":
        return left + right if all_rows else Counter(set(left) | set(right))
    if operator == "INTERSECT":
        if all_rows:
            return left & right
        return Counter(set(left) & set(right))
    if operator == "EXCEPT":
        if all_rows:
            return left - right
        return Counter(set(left) - set(right))
    return Counter()


def _ordered_counter_rows(counter: Counter) -> list[list[Any]]:
    expanded: list[tuple[Any, ...]] = []
    for row, count in counter.items():
        expanded.extend([row] * count)
    expanded.sort(key=repr)
    return [list(row) for row in expanded]


def _execute_set_branch(
    world: Any,
    branch: exp.Expression,
) -> tuple[list[tuple[Any, ...]] | None, str | None, int]:
    branch_sql = branch.sql(dialect="sqlite")
    try:
        parsed = parse_one(branch_sql, read="sqlite")
    except Exception as exc:  # noqa: BLE001 - diagnostic boundary only.
        return None, f"parse:{type(exc).__name__}", 0
    owner_select = (
        parsed if isinstance(parsed, exp.Select) else parsed.find(exp.Select)
    )
    direct_tables = list(dict.fromkeys(
        str(table.name)
        for table in parsed.find_all(exp.Table)
        if owner_select is not None and _nearest_select(table) is owner_select
    ))
    candidate_product = 1
    for table_name in direct_tables:
        rows = _table_rows(world, table_name)
        if not rows:
            return None, "table_missing", candidate_product
        candidate_product *= len(rows)
        if candidate_product > _MAX_JOIN_VALIDATION_PRODUCT:
            return None, "product_limit", candidate_product
    result, error = _execute_sqlite_diagnostic(world, branch_sql)
    return result, error, candidate_product


def _validate_set_query_paths(
    world: Any,
    obligation: DistinguishingObligation,
) -> tuple[bool, dict[str, Any], list[str]] | None:
    execution = getattr(world, "execution", {}) or {}
    context = execution.get("validation_context", {})
    standard_sql = str(context.get("standard_sql") or "")
    student_sql = str(context.get("student_sql") or "")
    if not standard_sql or not student_sql:
        return None
    try:
        standard_ast = parse_one(standard_sql, read="sqlite")
        student_ast = parse_one(student_sql, read="sqlite")
    except Exception:
        return None
    standard_node = _set_operation_node(standard_ast)
    student_node = _set_operation_node(student_ast)
    if not isinstance(standard_node, (exp.Union, exp.Intersect, exp.Except)) or not isinstance(
        student_node, (exp.Union, exp.Intersect, exp.Except)
    ):
        return None
    if standard_node.find_ancestor(exp.CTE) is not None or student_node.find_ancestor(
        exp.CTE
    ) is not None:
        # A recursive member is not an executable standalone query because it
        # references the CTE being defined.  The legacy fallback below uses
        # the bounded full-query execution and duplicate counts instead.
        return None

    standard_branches = (standard_node.this, standard_node.expression)
    student_branches = (student_node.this, student_node.expression)
    standard_results: list[list[tuple[Any, ...]]] = []
    student_results: list[list[tuple[Any, ...]]] = []
    products: list[int] = []
    for branch in (*standard_branches, *student_branches):
        result, error, product = _execute_set_branch(world, branch)
        products.append(product)
        if result is None:
            if error == "product_limit":
                return False, {
                    "source": "query_branches",
                    "candidate_row_product": product,
                    "candidate_row_product_limit": _MAX_JOIN_VALIDATION_PRODUCT,
                }, ["set_branch_product_limit"]
            return False, {
                "source": "query_branches",
                "branch_error": error,
            }, ["set_branch_execution_failed"]
        if len(standard_results) < 2:
            standard_results.append(result)
        else:
            student_results.append(result)

    standard_left = Counter(tuple(row) for row in standard_results[0])
    standard_right = Counter(tuple(row) for row in standard_results[1])
    student_left = Counter(tuple(row) for row in student_results[0])
    student_right = Counter(tuple(row) for row in student_results[1])
    standard_modifier = standard_node.args.get("distinct") is False
    student_modifier = student_node.args.get("distinct") is False
    standard_simulated = _set_counter_result(
        _set_operation_name(standard_node),
        standard_left,
        standard_right,
        all_rows=standard_modifier,
    )
    student_simulated = _set_counter_result(
        _set_operation_name(student_node),
        student_left,
        student_right,
        all_rows=student_modifier,
    )
    branch_paths_same = (
        standard_left == student_left and standard_right == student_right
    )
    distinguished = standard_simulated != student_simulated
    standard_operator = _set_operation_name(standard_node)
    student_operator = _set_operation_name(student_node)
    overlap = set(standard_left) & set(standard_right)
    left_only = set(standard_left) - set(standard_right)
    right_only = set(standard_right) - set(standard_left)
    if standard_operator == student_operator and standard_modifier != student_modifier:
        required_paths = {"overlap"}
    elif {standard_operator, student_operator} == {"INTERSECT", "UNION"}:
        required_paths = {"overlap", "left_only", "right_only"}
    elif {standard_operator, student_operator} == {"EXCEPT", "INTERSECT"}:
        required_paths = {"overlap", "left_only"}
    elif {standard_operator, student_operator} == {"EXCEPT", "UNION"}:
        required_paths = {"overlap", "left_only", "right_only"}
    else:
        required_paths = {"overlap"}
    available_paths = {
        label
        for label, values in (
            ("overlap", overlap),
            ("left_only", left_only),
            ("right_only", right_only),
        )
        if values
    }
    evidence = {
        "source": "query_branches",
        "standard_operator": standard_operator,
        "student_operator": student_operator,
        "standard_modifier": "ALL" if standard_modifier else "DISTINCT",
        "student_modifier": "ALL" if student_modifier else "DISTINCT",
        "branch_paths_same": branch_paths_same,
        "left_branch_rows": _ordered_counter_rows(standard_left),
        "right_branch_rows": _ordered_counter_rows(standard_right),
        "standard_branch_row_counts": [len(result) for result in standard_results],
        "student_branch_row_counts": [len(result) for result in student_results],
        "simulated_standard_result": _ordered_counter_rows(standard_simulated),
        "simulated_student_result": _ordered_counter_rows(student_simulated),
        "required_paths": sorted(required_paths),
        "available_paths": sorted(available_paths),
        "path_requirement_mode": "executed_operator_result",
        "candidate_row_product": max(products, default=0),
        "candidate_row_product_limit": _MAX_JOIN_VALIDATION_PRODUCT,
    }
    satisfied = branch_paths_same and distinguished
    return satisfied, evidence, [] if satisfied else [
        "set_operator_query_path_not_distinguished"
    ]


def _validate_set_paths(world: Any, obligation: DistinguishingObligation) -> tuple[bool, dict[str, Any], list[str]]:
    spec = next(
        (item for item in obligation.hard_constraints if item.kind == "set_left_right_overlap"),
        None,
    )
    if spec is None:
        return False, {}, ["set_constraint_missing"]
    query_path = _validate_set_query_paths(world, obligation)
    if query_path is not None:
        return query_path
    metadata = dict(spec.metadata)
    standard_columns = metadata.get("standard_projection_columns") or ()
    left = _set_projection_values(
        world,
        metadata.get("standard_left_source_table") or "",
        standard_columns,
    )
    right = _set_projection_values(
        world,
        metadata.get("standard_right_source_table") or "",
        standard_columns,
    )
    if left is None or right is None:
        attempt = _latest_execution_attempt(world)
        standard_rows = attempt.get("standard_result") or []
        student_rows = attempt.get("student_result") or []
        standard_counts = Counter(_freeze_result_value(row) for row in standard_rows)
        student_counts = Counter(_freeze_result_value(row) for row in student_rows)
        standard_duplicates = sum(count - 1 for count in standard_counts.values())
        student_duplicates = sum(count - 1 for count in student_counts.values())
        execution_satisfied = bool(
            set(standard_counts) == set(student_counts)
            and bool(standard_duplicates) != bool(student_duplicates)
        )
        if execution_satisfied:
            return True, {
                "source": "executed_recursive_set",
                "standard_row_count": len(standard_rows),
                "student_row_count": len(student_rows),
                "standard_duplicate_count": standard_duplicates,
                "student_duplicate_count": student_duplicates,
            }, []
        return False, {
            "source": "executed_recursive_set" if attempt else "set_branch_metadata",
            "standard_duplicate_count": standard_duplicates,
            "student_duplicate_count": student_duplicates,
        }, ["set_branch_metadata_missing"]
    standard_op = str(metadata.get("standard_op") or "").upper()
    student_op = str(metadata.get("student_op") or "").upper()
    standard_modifier = str(metadata.get("standard_modifier") or "").upper()
    student_modifier = str(metadata.get("student_modifier") or "").upper()
    overlap = left & right
    left_only = left - right
    right_only = right - left
    if standard_op == student_op and standard_modifier != student_modifier:
        required = {"overlap"}
    elif {standard_op, student_op} == {"INTERSECT", "UNION"}:
        required = {"overlap", "left_only", "right_only"}
    elif {standard_op, student_op} == {"EXCEPT", "INTERSECT"}:
        required = {"overlap", "left_only"}
    elif {standard_op, student_op} == {"EXCEPT", "UNION"}:
        required = {"overlap", "left_only", "right_only"}
    else:
        required = {"overlap"}
    available = {
        "overlap" if overlap else "",
        "left_only" if left_only else "",
        "right_only" if right_only else "",
    } - {""}
    satisfied = required <= available
    evidence = {
        "standard_operator": standard_op,
        "student_operator": student_op,
        "standard_modifier": standard_modifier,
        "student_modifier": student_modifier,
        "left_tuple_count": len(left),
        "right_tuple_count": len(right),
        "overlap_count": len(overlap),
        "left_only_count": len(left_only),
        "right_only_count": len(right_only),
        "required_paths": sorted(required),
        "available_paths": sorted(available),
    }
    return satisfied, evidence, [] if satisfied else ["set_branch_paths_not_materialized"]


def _validate_case_paths(world: Any, obligation: DistinguishingObligation) -> tuple[bool, dict[str, Any], list[str]]:
    spec = next(
        (item for item in obligation.hard_constraints if item.kind == "case_unmatched_and_branch_rows"),
        None,
    )
    if spec is None:
        return False, {}, ["case_constraint_missing"]
    metadata = dict(spec.metadata)
    predicates = [
        _predicate_expression(str(value))
        for value in metadata.get("standard_case_when_predicates") or ()
    ]
    if not predicates or any(predicate is None for predicate in predicates):
        return False, {}, ["case_predicate_metadata_missing"]
    relation = str(spec.relation or metadata.get("standard_source_table") or "")
    rows = _table_rows(world, relation)
    if not relation or not rows:
        return False, {"relation": relation}, ["case_table_missing"]
    branch_hits = [0 for _ in predicates]
    unmatched_rows: list[int] = []
    unsupported_rows: list[int] = []
    for index, row in enumerate(rows):
        values = [_evaluate_predicate(predicate, row) for predicate in predicates]
        if any(value is _UNSUPPORTED_TRUTH_VALUE for value in values):
            unsupported_rows.append(index)
            continue
        for position, value in enumerate(values):
            if value is True:
                branch_hits[position] += 1
        if not any(value is True for value in values):
            unmatched_rows.append(index)
    satisfied = all(count > 0 for count in branch_hits) and bool(unmatched_rows)
    evidence = {
        "relation": relation,
        "branch_count": len(predicates),
        "branch_hit_counts": branch_hits,
        "unmatched_row_indexes": unmatched_rows,
        "unsupported_row_indexes": unsupported_rows,
    }
    return satisfied, evidence, [] if satisfied else ["case_branch_or_unmatched_path_missing"]


def _validate_membership_paths(world: Any, obligation: DistinguishingObligation) -> tuple[bool, dict[str, Any], list[str]]:
    spec = next(
        (item for item in obligation.hard_constraints if item.kind == "subquery_membership_paths"),
        None,
    )
    if spec is None:
        return False, {}, ["subquery_constraint_missing"]
    metadata = dict(spec.metadata)
    outer_table = str(metadata.get("standard_source_table") or spec.relation or "")
    inner_table = str(metadata.get("standard_membership_table") or "")
    outer_column = str(metadata.get("standard_outer_column") or spec.column or "")
    inner_column = str(metadata.get("standard_membership_column") or "")
    outer_values = _values(world, outer_table, outer_column)
    inner_values = _values(world, inner_table, inner_column)
    if not outer_table or not inner_table or not outer_column or not inner_column:
        return False, {}, ["subquery_membership_metadata_missing"]
    if not outer_values or not inner_values:
        return False, {
            "outer_table": outer_table,
            "inner_table": inner_table,
        }, ["subquery_membership_table_missing"]
    outer_non_null = {value for value in outer_values if value is not None}
    inner_non_null = {value for value in inner_values if value is not None}
    overlap = outer_non_null & inner_non_null
    outer_only = outer_non_null - inner_non_null
    student_inner_table = str(
        metadata.get("student_membership_table") or inner_table
    )
    student_outer_table = str(
        metadata.get("student_source_table") or outer_table
    )
    student_outer_column = str(
        metadata.get("student_outer_column") or outer_column
    )
    student_inner_column = str(
        metadata.get("student_membership_column") or ""
    )
    standard_only_overlap: set[Any] = set()
    student_only_overlap: set[Any] = set()
    key_drift_required = bool(
        (
            student_inner_column
            and (
                student_inner_table.lower() != inner_table.lower()
                or student_inner_column.lower() != inner_column.lower()
            )
        )
        or (
            student_outer_column
            and (
                student_outer_table.lower() != outer_table.lower()
                or student_outer_column.lower() != outer_column.lower()
            )
        )
    )
    if key_drift_required:
        student_outer_values = _values(
            world,
            student_outer_table,
            student_outer_column,
        )
        student_outer_non_null = {
            value for value in student_outer_values if value is not None
        }
        student_inner_values = _values(
            world,
            student_inner_table,
            student_inner_column or inner_column,
        )
        student_inner_non_null = {
            value for value in student_inner_values if value is not None
        }
        student_overlap = student_outer_non_null & student_inner_non_null
        standard_only_overlap = overlap - student_overlap
        student_only_overlap = student_overlap - overlap
    require_inner_null = bool(metadata.get("require_inner_null"))
    inner_null_count = sum(value is None for value in inner_values)
    satisfied = bool(overlap) and bool(outer_only) and (
        not require_inner_null or inner_null_count > 0
    ) and (
        not key_drift_required
        or bool(standard_only_overlap or student_only_overlap)
    )
    evidence = {
        "outer_table": outer_table,
        "inner_table": inner_table,
        "outer_column": outer_column,
        "inner_column": inner_column,
        "overlap_values": sorted(overlap, key=str)[:8],
        "outer_only_values": sorted(outer_only, key=str)[:8],
        "inner_null_count": inner_null_count,
        "requires_inner_null": require_inner_null,
        "key_drift_required": key_drift_required,
        "student_inner_table": student_inner_table,
        "student_inner_column": student_inner_column,
        "student_outer_table": student_outer_table,
        "student_outer_column": student_outer_column,
        "standard_only_overlap_values": sorted(
            standard_only_overlap,
            key=str,
        )[:8],
        "student_only_overlap_values": sorted(
            student_only_overlap,
            key=str,
        )[:8],
    }
    diagnostics = [] if satisfied else [
        "subquery_membership_null_path_missing"
        if require_inner_null and inner_null_count == 0
        else "correlated_key_drift_path_missing"
        if key_drift_required
        and not (standard_only_overlap or student_only_overlap)
        else "subquery_membership_paths_missing"
    ]
    return satisfied, evidence, diagnostics


def _validate_in_list_paths(world: Any, obligation: DistinguishingObligation) -> tuple[bool, dict[str, Any], list[str]]:
    spec = next(
        (item for item in obligation.hard_constraints if item.kind == "in_list_membership_paths"),
        None,
    )
    if spec is None or not spec.relation or not spec.column:
        return False, {}, ["in_list_constraint_missing"]
    values = _values(world, spec.relation, spec.column)
    metadata = dict(spec.metadata)
    listed = set(metadata.get("standard_in_values") or ())
    distinguishing = set(metadata.get("distinguishing_values") or ())
    matching = {value for value in values if value in listed}
    outside = {value for value in values if value not in listed and value is not None}
    distinguishing_matches = {value for value in values if value in distinguishing}
    satisfied = (
        bool(distinguishing_matches)
        if distinguishing
        else bool(matching) and bool(outside)
    )
    evidence = {
        "relation": spec.relation,
        "column": spec.column,
        "listed_values": sorted(listed, key=str),
        "matching_values": sorted(matching, key=str),
        "outside_values": sorted(outside, key=str),
        "distinguishing_values": sorted(distinguishing, key=str),
        "materialized_distinguishing_values": sorted(distinguishing_matches, key=str),
    }
    return satisfied, evidence, [] if satisfied else ["in_list_membership_paths_missing"]


def _validate_predicate_paths(world: Any, obligation: DistinguishingObligation) -> tuple[bool, dict[str, Any], list[str]]:
    spec = next(
        (item for item in obligation.hard_constraints if item.kind == "predicate_positive_negative_paths"),
        None,
    )
    if spec is None or not spec.relation:
        return False, {}, ["predicate_path_constraint_missing"]
    metadata = dict(spec.metadata)
    predicate_sql = str(metadata.get("standard_sql") or metadata.get("student_sql") or "")
    try:
        predicate = parse_one(predicate_sql, read="sqlite")
    except Exception:
        return False, {"predicate_sql": predicate_sql}, ["predicate_path_parse_failed"]
    rows = _table_rows(world, spec.relation)
    if not rows:
        return False, {"relation": spec.relation}, ["predicate_path_table_missing"]
    values = [_evaluate_predicate(predicate, row) for row in rows]
    true_rows = [index for index, value in enumerate(values) if value is True]
    non_true_rows = [index for index, value in enumerate(values) if value is not True]
    unsupported_rows = [
        index for index, value in enumerate(values)
        if value is _UNSUPPORTED_TRUTH_VALUE
    ]
    divergent_rows: list[int] = []
    query_parse_failed = False
    standard_query_sql = str(metadata.get("standard_query_sql") or "")
    student_query_sql = str(metadata.get("student_query_sql") or "")
    if standard_query_sql and student_query_sql:
        try:
            standard_query = parse_one(standard_query_sql, read="sqlite")
            student_query = parse_one(student_query_sql, read="sqlite")
            standard_where = standard_query.find(exp.Where)
            student_where = student_query.find(exp.Where)
            standard_predicate = standard_where.this if isinstance(standard_where, exp.Where) else None
            student_predicate = student_where.this if isinstance(student_where, exp.Where) else None
            for index, row in enumerate(rows):
                standard_truth = (
                    True if standard_predicate is None
                    else _evaluate_predicate(standard_predicate, row)
                )
                student_truth = (
                    True if student_predicate is None
                    else _evaluate_predicate(student_predicate, row)
                )
                if _UNSUPPORTED_TRUTH_VALUE in (standard_truth, student_truth):
                    query_parse_failed = True
                    break
                if (standard_truth is True) != (student_truth is True):
                    divergent_rows.append(index)
        except Exception:
            query_parse_failed = True
    context_satisfied = bool(divergent_rows) if standard_query_sql and student_query_sql else True
    satisfied = bool(
        true_rows
        and non_true_rows
        and not unsupported_rows
        and not query_parse_failed
        and context_satisfied
    )
    evidence = {
        "relation": spec.relation,
        "predicate_sql": predicate_sql,
        "true_row_indexes": true_rows,
        "non_true_row_indexes": non_true_rows,
        "unsupported_row_indexes": unsupported_rows,
        "query_context_checked": bool(standard_query_sql and student_query_sql),
        "query_context_parse_failed": query_parse_failed,
        "divergent_row_indexes": divergent_rows,
    }
    return satisfied, evidence, [] if satisfied else ["predicate_positive_negative_paths_missing"]


def _validate_aggregate_filter_paths(
    world: Any,
    obligation: DistinguishingObligation,
) -> tuple[bool, dict[str, Any], list[str]]:
    """Validate inclusion paths for a top-level aggregate FILTER change."""
    spec = next(
        (
            item
            for item in obligation.hard_constraints
            if item.kind == "aggregate_filter_paths"
        ),
        None,
    )
    if spec is None or not spec.relation:
        return False, {}, ["aggregate_filter_constraint_missing"]
    metadata = dict(spec.metadata)

    def parse_predicate(value: Any) -> exp.Expression | None:
        text = str(value or "").strip()
        if not text:
            return None
        return _predicate_expression(text)

    standard = parse_predicate(metadata.get("standard_filter_predicate"))
    student = parse_predicate(metadata.get("student_filter_predicate"))
    if standard is None and student is None:
        return False, {}, ["aggregate_filter_predicate_missing"]
    rows = _table_rows(world, spec.relation)
    if not rows:
        return False, {"relation": spec.relation}, ["aggregate_filter_table_missing"]

    evaluations: list[dict[str, Any]] = []
    standard_true: list[int] = []
    standard_false: list[int] = []
    student_true: list[int] = []
    student_false: list[int] = []
    divergent: list[int] = []
    unsupported: list[int] = []
    for index, row in enumerate(rows):
        standard_value = True if standard is None else _evaluate_predicate(standard, row)
        student_value = True if student is None else _evaluate_predicate(student, row)
        if _UNSUPPORTED_TRUTH_VALUE in (standard_value, student_value):
            unsupported.append(index)
            continue
        standard_included = standard_value is True
        student_included = student_value is True
        (standard_true if standard_included else standard_false).append(index)
        (student_true if student_included else student_false).append(index)
        if standard_included != student_included:
            divergent.append(index)
        evaluations.append({
            "row_index": index,
            "standard_value": standard_value,
            "student_value": student_value,
            "standard_included": standard_included,
            "student_included": student_included,
            "distinguishes": standard_included != student_included,
        })

    standard_paths = standard is None or bool(standard_true and standard_false)
    student_paths = student is None or bool(student_true and student_false)
    satisfied = bool(divergent) and not unsupported and standard_paths and student_paths
    evidence = {
        "relation": spec.relation,
        "standard_filter_predicate": metadata.get("standard_filter_predicate", ""),
        "student_filter_predicate": metadata.get("student_filter_predicate", ""),
        "standard_true_row_indexes": standard_true,
        "standard_false_row_indexes": standard_false,
        "student_true_row_indexes": student_true,
        "student_false_row_indexes": student_false,
        "divergent_row_indexes": divergent,
        "unsupported_row_indexes": unsupported,
        "evaluations": evaluations[:8],
    }
    return satisfied, evidence, [] if satisfied else ["aggregate_filter_paths_missing"]


def _validate_regex_pattern_separation(
    world: Any,
    obligation: DistinguishingObligation,
) -> tuple[bool, dict[str, Any], list[str]]:
    spec = next(
        (
            item
            for item in obligation.hard_constraints
            if item.kind == "regex_pattern_separation"
        ),
        None,
    )
    if spec is None or not spec.relation or not spec.column:
        return False, {}, ["regex_pattern_constraint_missing"]
    metadata = dict(spec.metadata)
    standard_pattern = metadata.get("standard_pattern")
    student_pattern = metadata.get("student_pattern")
    if not isinstance(standard_pattern, str) or not isinstance(
        student_pattern, str
    ):
        return False, {}, ["regex_pattern_metadata_missing"]

    values = _values(world, spec.relation, spec.column)
    evaluations: list[dict[str, Any]] = []
    try:
        for index, value in enumerate(values):
            standard = regex_matches(standard_pattern, value)
            student = regex_matches(student_pattern, value)
            evaluations.append({
                "row_index": index,
                "value": value,
                "standard_matches": standard,
                "student_matches": student,
                "distinguishes": (
                    standard is not None
                    and student is not None
                    and standard != student
                ),
            })
    except RegexEvaluationError as exc:
        return False, {
            "relation": spec.relation,
            "column": spec.column,
            "standard_pattern": standard_pattern,
            "student_pattern": student_pattern,
        }, [f"regex_evaluation_failed:{exc}"]

    satisfied = any(item["distinguishes"] for item in evaluations)
    return satisfied, {
        "relation": spec.relation,
        "column": spec.column,
        "standard_pattern": standard_pattern,
        "student_pattern": student_pattern,
        "evaluations": evaluations[:8],
    }, [] if satisfied else ["regex_separating_value_missing"]


def _validate_like_pattern_separation(
    world: Any,
    obligation: DistinguishingObligation,
) -> tuple[bool, dict[str, Any], list[str]]:
    spec = next(
        (
            item
            for item in obligation.hard_constraints
            if item.kind == "like_pattern_separation"
        ),
        None,
    )
    if spec is None or not spec.relation or not spec.column:
        return False, {}, ["like_pattern_constraint_missing"]
    metadata = dict(spec.metadata)
    standard_pattern = metadata.get("standard_pattern")
    student_pattern = metadata.get("student_pattern")
    if not isinstance(standard_pattern, str) or not isinstance(
        student_pattern, str
    ):
        return False, {}, ["like_pattern_metadata_missing"]
    standard_escape = metadata.get("standard_escape")
    student_escape = metadata.get("student_escape")
    if not isinstance(standard_escape, str):
        standard_escape = "\\"
    if not isinstance(student_escape, str):
        student_escape = "\\"
    case_insensitive = bool(metadata.get("case_insensitive"))
    values = _values(world, spec.relation, spec.column)
    evaluations: list[dict[str, Any]] = []
    try:
        for index, value in enumerate(values):
            standard = like_matches(
                standard_pattern,
                value,
                escape=standard_escape,
                case_insensitive=case_insensitive,
            )
            student = like_matches(
                student_pattern,
                value,
                escape=student_escape,
                case_insensitive=case_insensitive,
            )
            evaluations.append({
                "row_index": index,
                "value": value,
                "standard_matches": standard,
                "student_matches": student,
                "distinguishes": (
                    standard is not None
                    and student is not None
                    and standard != student
                ),
            })
    except RegexEvaluationError as exc:
        return False, {
            "relation": spec.relation,
            "column": spec.column,
            "standard_pattern": standard_pattern,
            "student_pattern": student_pattern,
        }, [f"like_evaluation_failed:{exc}"]

    satisfied = any(item["distinguishes"] for item in evaluations)
    return satisfied, {
        "relation": spec.relation,
        "column": spec.column,
        "standard_pattern": standard_pattern,
        "student_pattern": student_pattern,
        "standard_escape": standard_escape,
        "student_escape": student_escape,
        "case_insensitive": case_insensitive,
        "evaluations": evaluations[:8],
    }, [] if satisfied else ["like_separating_value_missing"]


def _validate_glob_pattern_separation(
    world: Any,
    obligation: DistinguishingObligation,
) -> tuple[bool, dict[str, Any], list[str]]:
    spec = next(
        (
            item
            for item in obligation.hard_constraints
            if item.kind == "glob_pattern_separation"
        ),
        None,
    )
    if spec is None or not spec.relation or not spec.column:
        return False, {}, ["glob_pattern_constraint_missing"]
    metadata = dict(spec.metadata)
    standard_pattern = metadata.get("standard_pattern")
    student_pattern = metadata.get("student_pattern")
    if not isinstance(standard_pattern, str) or not isinstance(
        student_pattern, str
    ):
        return False, {}, ["glob_pattern_metadata_missing"]
    values = _values(world, spec.relation, spec.column)
    evaluations: list[dict[str, Any]] = []
    try:
        for index, value in enumerate(values):
            standard = glob_matches(standard_pattern, value)
            student = glob_matches(student_pattern, value)
            evaluations.append({
                "row_index": index,
                "value": value,
                "standard_matches": standard,
                "student_matches": student,
                "distinguishes": (
                    standard is not None
                    and student is not None
                    and standard != student
                ),
            })
    except RegexEvaluationError as exc:
        return False, {
            "relation": spec.relation,
            "column": spec.column,
            "standard_pattern": standard_pattern,
            "student_pattern": student_pattern,
        }, [f"glob_evaluation_failed:{exc}"]
    satisfied = any(item["distinguishes"] for item in evaluations)
    return satisfied, {
        "relation": spec.relation,
        "column": spec.column,
        "standard_pattern": standard_pattern,
        "student_pattern": student_pattern,
        "evaluations": evaluations[:8],
    }, [] if satisfied else ["glob_separating_value_missing"]


def _validate_similar_pattern_separation(
    world: Any,
    obligation: DistinguishingObligation,
) -> tuple[bool, dict[str, Any], list[str]]:
    spec = next(
        (
            item
            for item in obligation.hard_constraints
            if item.kind == "similar_pattern_separation"
        ),
        None,
    )
    if spec is None or not spec.relation or not spec.column:
        return False, {}, ["similar_pattern_constraint_missing"]
    metadata = dict(spec.metadata)
    standard_pattern = metadata.get("standard_pattern")
    student_pattern = metadata.get("student_pattern")
    if not isinstance(standard_pattern, str) or not isinstance(
        student_pattern, str
    ):
        return False, {}, ["similar_pattern_metadata_missing"]
    standard_escape = metadata.get("standard_escape")
    student_escape = metadata.get("student_escape")
    if not isinstance(standard_escape, str):
        standard_escape = "\\"
    if not isinstance(student_escape, str):
        student_escape = "\\"
    values = _values(world, spec.relation, spec.column)
    evaluations: list[dict[str, Any]] = []
    try:
        for index, value in enumerate(values):
            standard = similar_to_matches(
                standard_pattern,
                value,
                escape=standard_escape,
            )
            student = similar_to_matches(
                student_pattern,
                value,
                escape=student_escape,
            )
            evaluations.append({
                "row_index": index,
                "value": value,
                "standard_matches": standard,
                "student_matches": student,
                "distinguishes": (
                    standard is not None
                    and student is not None
                    and standard != student
                ),
            })
    except RegexEvaluationError as exc:
        return False, {
            "relation": spec.relation,
            "column": spec.column,
            "standard_pattern": standard_pattern,
            "student_pattern": student_pattern,
        }, [f"similar_evaluation_failed:{exc}"]
    satisfied = any(item["distinguishes"] for item in evaluations)
    return satisfied, {
        "relation": spec.relation,
        "column": spec.column,
        "standard_pattern": standard_pattern,
        "student_pattern": student_pattern,
        "standard_escape": standard_escape,
        "student_escape": student_escape,
        "evaluations": evaluations[:8],
    }, [] if satisfied else ["similar_separating_value_missing"]


def _validate_null_safe_comparison_paths(
    world: Any,
    obligation: DistinguishingObligation,
) -> tuple[bool, dict[str, Any], list[str]]:
    spec = next(
        (
            item
            for item in obligation.hard_constraints
            if item.kind == "null_safe_comparison_paths"
        ),
        None,
    )
    if spec is None or not spec.relation or not spec.column:
        return False, {}, ["null_safe_comparison_constraint_missing"]
    metadata = dict(spec.metadata)
    standard_op = str(metadata.get("standard_op") or "").upper()
    student_op = str(metadata.get("student_op") or "").upper()
    rows = _table_rows(world, spec.relation)
    left_column = _column_name(rows, spec.column)
    if not left_column:
        return False, {
            "relation": spec.relation,
            "column": spec.column,
        }, ["null_safe_comparison_left_column_missing"]

    standard_kind = str(
        metadata.get("standard_value_kind") or "literal"
    ).lower()
    student_kind = str(
        metadata.get("student_value_kind") or "literal"
    ).lower()
    standard_right_requested = str(
        metadata.get("standard_right_column") or ""
    )
    student_right_requested = str(
        metadata.get("student_right_column") or ""
    )
    standard_right_column = (
        _column_name(rows, standard_right_requested)
        if standard_kind == "column"
        else None
    )
    student_right_column = (
        _column_name(rows, student_right_requested)
        if student_kind == "column"
        else None
    )
    missing_right_columns = [
        requested
        for kind, requested, actual in (
            (standard_kind, standard_right_requested, standard_right_column),
            (student_kind, student_right_requested, student_right_column),
        )
        if kind == "column" and requested and not actual
    ]
    if missing_right_columns:
        return False, {
            "relation": spec.relation,
            "column": left_column,
            "missing_right_columns": missing_right_columns,
        }, ["null_safe_comparison_right_column_missing"]

    standard_literal = metadata.get("standard_value", spec.value)
    student_literal = metadata.get("student_value", spec.value)

    def right_value(
        row: dict[str, Any],
        kind: str,
        actual_column: str | None,
        literal: Any,
    ) -> Any:
        return row.get(actual_column) if kind == "column" and actual_column else literal

    def evaluate(operator: str, left: Any, right: Any) -> bool | None | object:
        if operator == "NULLSAFEEQ":
            return left == right
        if operator == "NULLSAFENEQ":
            return left != right
        if left is None or right is None:
            return None
        try:
            if operator in {"EQ", "="}:
                return left == right
            if operator in {"NEQ", "!=", "<>"}:
                return left != right
            if operator in {"GT", ">"}:
                return left > right
            if operator in {"GTE", ">="}:
                return left >= right
            if operator in {"LT", "<"}:
                return left < right
            if operator in {"LTE", "<="}:
                return left <= right
        except TypeError:
            return _UNSUPPORTED_TRUTH_VALUE
        return _UNSUPPORTED_TRUTH_VALUE

    evaluations: list[dict[str, Any]] = []
    null_rows: list[int] = []
    boundary_rows: list[int] = []
    other_rows: list[int] = []
    both_null_rows: list[int] = []
    one_null_rows: list[int] = []
    equal_non_null_rows: list[int] = []
    unequal_non_null_rows: list[int] = []
    divergent_rows: list[int] = []
    unsupported_rows: list[int] = []
    for index, row in enumerate(rows):
        value = row.get(left_column)
        standard_right = right_value(
            row, standard_kind, standard_right_column, standard_literal
        )
        student_right = right_value(
            row, student_kind, student_right_column, student_literal
        )
        if value is None:
            null_rows.append(index)
        if value == spec.value:
            boundary_rows.append(index)
        if value is not None and value != spec.value:
            other_rows.append(index)
        if standard_right_column and standard_right_column == student_right_column:
            if value is None and standard_right is None:
                both_null_rows.append(index)
            elif (value is None) != (standard_right is None):
                one_null_rows.append(index)
            elif value is not None and value == standard_right:
                equal_non_null_rows.append(index)
            elif value is not None and standard_right is not None:
                unequal_non_null_rows.append(index)
        standard = evaluate(standard_op, value, standard_right)
        student = evaluate(student_op, value, student_right)
        if _UNSUPPORTED_TRUTH_VALUE in (standard, student):
            unsupported_rows.append(index)
            continue
        distinguishes = (standard is True) != (student is True)
        if distinguishes:
            divergent_rows.append(index)
        evaluations.append({
            "row_index": index,
            "value": value,
            "standard_right_value": standard_right,
            "student_right_value": student_right,
            "standard_truth": standard,
            "student_truth": student,
            "distinguishes": distinguishes,
        })

    same_right_column = bool(
        standard_right_column
        and standard_right_column == student_right_column
    )
    if same_right_column and standard_right_column != left_column:
        satisfied = bool(
            both_null_rows
            and one_null_rows
            and equal_non_null_rows
            and unequal_non_null_rows
            and divergent_rows
            and not unsupported_rows
        )
    elif same_right_column:
        satisfied = bool(null_rows and other_rows and divergent_rows and not unsupported_rows)
    else:
        satisfied = bool(
            null_rows
            and boundary_rows
            and other_rows
            and divergent_rows
            and not unsupported_rows
        )
    evidence = {
        "relation": spec.relation,
        "column": left_column,
        "boundary": spec.value,
        "standard_op": standard_op,
        "student_op": student_op,
        "standard_value_kind": standard_kind,
        "student_value_kind": student_kind,
        "standard_right_column": standard_right_column,
        "student_right_column": student_right_column,
        "null_row_indexes": null_rows,
        "boundary_row_indexes": boundary_rows,
        "other_row_indexes": other_rows,
        "both_null_row_indexes": both_null_rows,
        "one_null_row_indexes": one_null_rows,
        "equal_non_null_row_indexes": equal_non_null_rows,
        "unequal_non_null_row_indexes": unequal_non_null_rows,
        "divergent_row_indexes": divergent_rows,
        "unsupported_row_indexes": unsupported_rows,
        "evaluations": evaluations[:8],
    }
    return satisfied, evidence, [] if satisfied else ["null_safe_comparison_paths_missing"]


def _validate_boundary(world: Any, obligation: DistinguishingObligation) -> tuple[bool, dict[str, Any], list[str]]:
    spec = next((item for item in obligation.hard_constraints if item.kind == "boundary_tristate"), None)
    if spec is None or not spec.column:
        return False, {}, ["boundary_constraint_missing_column"]
    values = _values(world, spec.relation, spec.column)
    source = "physical_table"
    if not values:
        attempt = _latest_execution_attempt(world)
        standard_rows = attempt.get("standard_result") or []
        if standard_rows and all(
            isinstance(row, (tuple, list)) and len(row) == 1
            for row in standard_rows
        ):
            values = [row[0] for row in standard_rows]
            source = "standard_single_column_result"
    satisfied = spec.value in values
    return satisfied, {
        "column": spec.column,
        "boundary": spec.value,
        "values_sample": values[:8],
        "source": source,
    }, [] if satisfied else ["boundary_value_not_materialized"]


def _validate_null(world: Any, obligation: DistinguishingObligation) -> tuple[bool, dict[str, Any], list[str]]:
    spec = next((item for item in obligation.hard_constraints if item.kind == "null_and_non_null_rows"), None)
    if spec is None or not spec.column:
        return False, {}, ["null_constraint_missing_column"]
    values = _values(world, spec.relation, spec.column)
    satisfied = any(value is None for value in values) and any(value is not None for value in values)
    return satisfied, {"null_count": sum(value is None for value in values), "non_null_count": sum(value is not None for value in values)}, [] if satisfied else ["null_and_non_null_paths_missing"]


def _validate_duplicate(world: Any, obligation: DistinguishingObligation) -> tuple[bool, dict[str, Any], list[str]]:
    spec = next((item for item in obligation.hard_constraints if item.kind == "duplicate_projected_tuple"), None)
    if spec is None:
        return False, {}, ["duplicate_constraint_missing"]
    if obligation.diff_type == "distinct_changed":
        metadata = dict(spec.metadata)
        query_scope = str(metadata.get("query_scope") or "root")
        attempt = _latest_execution_attempt(world)
        if attempt and query_scope == "root":
            standard_rows = attempt.get("standard_result") or []
            student_rows = attempt.get("student_result") or []

            standard_counts = Counter(_freeze_result_value(row) for row in standard_rows)
            student_counts = Counter(_freeze_result_value(row) for row in student_rows)
            standard_duplicate_count = sum(count - 1 for count in standard_counts.values())
            student_duplicate_count = sum(count - 1 for count in student_counts.values())
            same_projected_values = set(standard_counts) == set(student_counts)
            satisfied = bool(
                same_projected_values
                and bool(standard_duplicate_count) != bool(student_duplicate_count)
            )
            evidence = {
                "source": "executed_projection",
                "standard_row_count": len(standard_rows),
                "student_row_count": len(student_rows),
                "standard_duplicate_count": standard_duplicate_count,
                "student_duplicate_count": student_duplicate_count,
                "same_projected_values": same_projected_values,
            }
            return satisfied, evidence, [] if satisfied else ["duplicate_projection_not_observed"]

        if query_scope != "root":
            rows = _table_rows(world, spec.relation)
            requested = tuple(metadata.get("standard_projection_columns") or ())
            columns = [_column_name(rows, str(column)) for column in requested]
            if rows and requested and all(column is not None for column in columns):
                tuple_counts = Counter(
                    tuple(row.get(column) for column in columns if column is not None)
                    for row in rows
                )
                duplicate_tuples = {
                    repr(value): count
                    for value, count in tuple_counts.items()
                    if count > 1
                }
                satisfied = bool(duplicate_tuples)
                return satisfied, {
                    "source": "nested_query_input",
                    "query_scope": query_scope,
                    "projection_columns": list(requested),
                    "duplicate_tuples": duplicate_tuples,
                }, [] if satisfied else ["nested_duplicate_tuple_not_materialized"]

    if not spec.column:
        return False, {}, ["duplicate_constraint_missing_column"]

    # Aggregate DISTINCT needs duplicate input values rather than duplicate
    # output rows. This is also the pre-execution fallback for top-level
    # DISTINCT obligations.
    values = _values(world, spec.relation, spec.column)
    counts = Counter(values)
    duplicates = {str(value): count for value, count in counts.items() if count > 1}
    return bool(duplicates), {
        "source": "physical_table",
        "duplicate_values": duplicates,
    }, [] if duplicates else ["duplicate_projection_not_materialized"]


def _validate_distinct_on_competing_payload(
    world: Any,
    obligation: DistinguishingObligation,
) -> tuple[bool, dict[str, Any], list[str]]:
    spec = next(
        (
            item
            for item in obligation.hard_constraints
            if item.kind == "distinct_on_competing_payload"
        ),
        None,
    )
    if spec is None or not spec.relation or not spec.column:
        return False, {}, ["distinct_on_constraint_missing_columns"]
    rows = _table_rows(world, spec.relation)
    key_columns = tuple(dict(spec.metadata).get("key_columns") or ())
    if len(rows) < 2 or not key_columns:
        return False, {}, ["distinct_on_key_or_rows_missing"]
    payload = _column_name(rows, spec.column)
    keys = [_column_name(rows, str(column)) for column in key_columns]
    if payload is None or any(column is None for column in keys):
        return False, {}, ["distinct_on_columns_not_materialized"]
    groups: dict[tuple[Any, ...], set[Any]] = {}
    for row in rows:
        key = tuple(row.get(column) for column in keys if column is not None)
        groups.setdefault(key, set()).add(row.get(payload))
    competing = {
        repr(key): sorted(values, key=str)[:8]
        for key, values in groups.items()
        if len(values) >= 2
    }
    return bool(competing), {
        "key_columns": [str(column) for column in key_columns],
        "payload_column": payload,
        "competing_payloads": competing,
    }, [] if competing else ["distinct_on_competing_payload_missing"]


def _validate_projection_discriminator(world: Any, obligation: DistinguishingObligation) -> tuple[bool, dict[str, Any], list[str]]:
    spec = next(
        (item for item in obligation.hard_constraints if item.kind == "observable_projection_discriminator"),
        None,
    )
    if spec is None or not spec.relation or not spec.column:
        return False, {}, ["projection_constraint_missing_column"]
    values = _values(world, spec.relation, spec.column)
    distinct_values = {value for value in values if value is not None}
    satisfied = len(values) >= 2 and len(distinct_values) >= 2
    return satisfied, {
        "relation": spec.relation,
        "column": spec.column,
        "row_count": len(values),
        "distinct_value_count": len(distinct_values),
    }, [] if satisfied else ["projection_discriminator_not_materialized"]


def _latest_execution_attempt(world: Any) -> dict[str, Any]:
    execution = getattr(world, "execution", {}) if world is not None else {}
    attempts = execution.get("attempts", [])
    return attempts[-1] if attempts else {}


def _freeze_result_value(value: Any) -> Any:
    if isinstance(value, dict):
        return tuple(sorted((str(key), _freeze_result_value(item)) for key, item in value.items()))
    if isinstance(value, (tuple, list)):
        return tuple(_freeze_result_value(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return tuple(sorted((_freeze_result_value(item) for item in value), key=repr))
    try:
        hash(value)
    except TypeError:
        return repr(value)
    return value


def _validate_projection_shape(world: Any, obligation: DistinguishingObligation) -> tuple[bool, dict[str, Any], list[str]]:
    """Validate a star/non-star difference from executed result shape."""
    attempt = _latest_execution_attempt(world)
    standard = attempt.get("standard_result") or []
    student = attempt.get("student_result") or []
    standard_widths = sorted({len(row) for row in standard if isinstance(row, (tuple, list))})
    student_widths = sorted({len(row) for row in student if isinstance(row, (tuple, list))})
    satisfied = bool(standard_widths and student_widths and standard_widths != student_widths)
    evidence = {
        "standard_result_widths": standard_widths,
        "student_result_widths": student_widths,
        "standard_row_count": len(standard),
        "student_row_count": len(student),
    }
    return satisfied, evidence, [] if satisfied else ["projection_shape_not_materialized"]


def _validate_projection_values(world: Any, obligation: DistinguishingObligation) -> tuple[bool, dict[str, Any], list[str]]:
    """Validate a same-width projection change from executed tuples."""
    attempt = _latest_execution_attempt(world)
    standard = attempt.get("standard_result") or []
    student = attempt.get("student_result") or []
    satisfied = bool(standard or student) and standard != student
    evidence = {
        "standard_result_sample": standard[:5],
        "student_result_sample": student[:5],
        "standard_row_count": len(standard),
        "student_row_count": len(student),
    }
    return satisfied, evidence, [] if satisfied else ["projection_value_difference_not_materialized"]


def _validate_projection_boolean_tristate(
    world: Any,
    obligation: DistinguishingObligation,
) -> tuple[bool, dict[str, Any], list[str]]:
    """Verify TRUE/FALSE/UNKNOWN inputs and the projected NULL distinction."""
    spec = next(
        (
            item
            for item in obligation.hard_constraints
            if item.kind == "projection_boolean_tristate_paths"
        ),
        None,
    )
    if spec is None or not spec.relation:
        return False, {}, ["projection_boolean_constraint_missing"]
    metadata = dict(spec.metadata)
    predicate_sql = str(metadata.get("predicate_sql") or "")
    predicate = _predicate_expression(predicate_sql)
    rows = _table_rows(world, spec.relation)
    if predicate is None or not rows:
        return False, {
            "relation": spec.relation,
            "predicate_sql": predicate_sql,
        }, ["projection_boolean_predicate_or_table_missing"]

    evaluations = [_evaluate_predicate(predicate, row) for row in rows]
    true_rows = [index for index, value in enumerate(evaluations) if value is True]
    false_rows = [index for index, value in enumerate(evaluations) if value is False]
    unknown_rows = [index for index, value in enumerate(evaluations) if value is None]
    unsupported_rows = [
        index
        for index, value in enumerate(evaluations)
        if value is _UNSUPPORTED_TRUTH_VALUE
    ]

    attempt = _latest_execution_attempt(world)
    standard = attempt.get("standard_result") or []
    student = attempt.get("student_result") or []
    position = int(metadata.get("position") or 0)

    def projected_values(result: list[Any]) -> list[Any]:
        return [
            row[position]
            for row in result
            if isinstance(row, (tuple, list)) and position < len(row)
        ]

    standard_values = projected_values(standard)
    student_values = projected_values(student)
    truth_values = (
        standard_values
        if bool(metadata.get("standard_is_true"))
        else student_values
    )
    bare_values = (
        student_values
        if bool(metadata.get("standard_is_true"))
        else standard_values
    )
    output_distinguished = bool(
        standard_values != student_values
        and sum(value is None for value in bare_values)
        > sum(value is None for value in truth_values)
    )
    satisfied = bool(
        true_rows
        and false_rows
        and unknown_rows
        and not unsupported_rows
        and output_distinguished
    )
    evidence = {
        "relation": spec.relation,
        "column": spec.column,
        "predicate_sql": predicate_sql,
        "true_row_indexes": true_rows,
        "false_row_indexes": false_rows,
        "unknown_row_indexes": unknown_rows,
        "unsupported_row_indexes": unsupported_rows,
        "projection_position": position,
        "standard_projection_sample": standard_values[:8],
        "student_projection_sample": student_values[:8],
        "bare_null_count": sum(value is None for value in bare_values),
        "truth_test_null_count": sum(value is None for value in truth_values),
        "output_distinguished": output_distinguished,
    }
    return satisfied, evidence, [] if satisfied else [
        "projection_boolean_tristate_paths_missing"
    ]


def _validate_limit_paths(world: Any, obligation: DistinguishingObligation) -> tuple[bool, dict[str, Any], list[str]]:
    """Validate that a LIMIT/OFFSET difference reaches a row-count boundary."""
    attempt = _latest_execution_attempt(world)
    standard = attempt.get("standard_result") or []
    student = attempt.get("student_result") or []
    satisfied = standard != student
    evidence = {
        "standard_row_count": len(standard),
        "student_row_count": len(student),
        "standard_sql": dict(next(
            (item.metadata for item in obligation.hard_constraints if item.kind == "limit_row_count_paths"),
            (),
        )).get("standard_sql", ""),
        "student_sql": dict(next(
            (item.metadata for item in obligation.hard_constraints if item.kind == "limit_row_count_paths"),
            (),
        )).get("student_sql", ""),
    }
    return satisfied, evidence, [] if satisfied else ["limit_row_count_boundary_missing"]


def _validate_cte_base_paths(world: Any, obligation: DistinguishingObligation) -> tuple[bool, dict[str, Any], list[str]]:
    """Require an executable base/derived path for an ordinary CTE diff."""
    attempt = _latest_execution_attempt(world)
    standard = attempt.get("standard_result") or []
    student = attempt.get("student_result") or []
    base_tables = {
        name: len(rows)
        for name, rows in world.database.items()
        if rows
    }
    satisfied = bool(standard or base_tables)
    evidence = {
        "standard_row_count": len(standard),
        "student_row_count": len(student),
        "base_table_row_counts": base_tables,
    }
    return satisfied, evidence, [] if satisfied else ["cte_base_path_missing"]


def _validate_recursive_paths(world: Any, obligation: DistinguishingObligation) -> tuple[bool, dict[str, Any], list[str]]:
    """Validate root/child paths, including inline recursive CTE results.

    Recursive CTEs often have no physical table at all (for example a numeric
    sequence).  In that form the executed standard result is the only
    materialized witness and must be considered alongside base-table rows.
    """
    spec = next(
        (item for item in obligation.hard_constraints if item.kind == "cte_base_recursive_orphan_paths"),
        None,
    )
    if spec is None:
        return False, {}, ["recursive_cte_constraint_missing"]
    metadata = dict(spec.metadata)
    candidate_evidence: dict[str, Any] = {
        "standard_recursive": metadata.get("standard_recursive"),
        "student_recursive": metadata.get("student_recursive"),
    }
    for table_name, rows in world.database.items():
        if not rows:
            continue
        columns = list(rows[0])
        id_column = next(
            (column for column in columns if column.lower() in {"id", "emp_id", "node_id", "key"}),
            None,
        )
        parent_column = next(
            (
                column for column in columns
                if any(token in column.lower() for token in ("parent", "manager", "boss", "supervisor", "reports_to"))
            ),
            None,
        )
        if not id_column or not parent_column:
            continue
        identifiers = {row.get(id_column) for row in rows if row.get(id_column) is not None}
        roots = [index for index, row in enumerate(rows) if row.get(parent_column) is None]
        children = [
            index for index, row in enumerate(rows)
            if row.get(parent_column) in identifiers and row.get(parent_column) is not None
        ]
        orphans = [
            index for index, row in enumerate(rows)
            if row.get(parent_column) is not None and row.get(parent_column) not in identifiers
        ]
        candidate_evidence.update({
            "table": table_name,
            "id_column": id_column,
            "parent_column": parent_column,
            "root_row_indexes": roots,
            "child_row_indexes": children,
            "orphan_row_indexes": orphans,
        })
        satisfied = bool(roots and children)
        return satisfied, candidate_evidence, [] if satisfied else ["recursive_root_child_paths_missing"]

    attempt = _latest_execution_attempt(world)
    standard = attempt.get("standard_result") or []
    values = [tuple(row) if isinstance(row, (tuple, list)) else (row,) for row in standard]
    distinct_values = len(set(values))
    satisfied = len(values) >= 2 and distinct_values >= 2
    candidate_evidence.update({
        "inline_recursive": True,
        "standard_row_count": len(values),
        "standard_distinct_row_count": distinct_values,
        "standard_result_sample": values[:5],
    })
    return satisfied, candidate_evidence, [] if satisfied else ["inline_recursive_paths_missing"]


def validate_obligation(
    world: Any,
    obligation: DistinguishingObligation,
    *,
    execution_distinguished: bool = False,
) -> ObligationValidation:
    kinds = {item.kind for item in obligation.hard_constraints}
    validators = []
    if "boundary_tristate" in kinds:
        validators.append(_validate_boundary)
    if "null_safe_comparison_paths" in kinds:
        validators.append(_validate_null_safe_comparison_paths)
    if "regex_pattern_separation" in kinds:
        validators.append(_validate_regex_pattern_separation)
    if "like_pattern_separation" in kinds:
        validators.append(_validate_like_pattern_separation)
    if "glob_pattern_separation" in kinds:
        validators.append(_validate_glob_pattern_separation)
    if "similar_pattern_separation" in kinds:
        validators.append(_validate_similar_pattern_separation)
    if "boolean_truth_table" in kinds:
        validators.append(_validate_boolean_truth_table)
    if "set_left_right_overlap" in kinds:
        validators.append(_validate_set_paths)
    if "case_unmatched_and_branch_rows" in kinds:
        validators.append(_validate_case_paths)
    if "subquery_membership_paths" in kinds:
        validators.append(_validate_membership_paths)
    if "in_list_membership_paths" in kinds:
        validators.append(_validate_in_list_paths)
    if "predicate_positive_negative_paths" in kinds:
        validators.append(_validate_predicate_paths)
    if "aggregate_filter_paths" in kinds:
        validators.append(_validate_aggregate_filter_paths)
    if "null_and_non_null_rows" in kinds:
        validators.append(_validate_null)
    if "duplicate_projected_tuple" in kinds:
        validators.append(_validate_duplicate)
    if "distinct_on_competing_payload" in kinds:
        validators.append(_validate_distinct_on_competing_payload)
    if "observable_projection_discriminator" in kinds:
        validators.append(_validate_projection_discriminator)
    if "projection_shape_paths" in kinds:
        validators.append(_validate_projection_shape)
    if "projection_value_paths" in kinds:
        validators.append(_validate_projection_values)
    if "projection_boolean_tristate_paths" in kinds:
        validators.append(_validate_projection_boolean_tristate)
    if "limit_row_count_paths" in kinds:
        validators.append(_validate_limit_paths)
    if "cte_base_paths" in kinds:
        validators.append(_validate_cte_base_paths)
    if "cte_base_recursive_orphan_paths" in kinds:
        validators.append(_validate_recursive_paths)
    if kinds & {"matched_and_dangling_join_rows", "standard_join_equal_student_join_unequal"}:
        validators.append(_validate_join_paths)
    if "outer_join_predicate_placement_path" in kinds:
        validators.append(_validate_outer_join_predicate_placement)
    if "group_grain_split" in kinds:
        validators.append(_validate_group_grain)
    if "aggregate_boundary_group" in kinds:
        validators.append(_validate_aggregate_boundary)
    if "filtered_aggregate_boundary_path" in kinds:
        validators.append(_validate_filtered_aggregate_boundary)
    if "aggregate_function_separation" in kinds:
        validators.append(_validate_aggregate_function_separation)
    if "scalar_subquery_boundary_path" in kinds:
        validators.append(_validate_scalar_subquery_boundary)
    if "window_partitions_and_ties" in kinds:
        validators.append(_validate_window_paths)
    if "order_key_separation" in kinds:
        validators.append(_validate_order_paths)
    if not validators:
        return ObligationValidation(
            obligation.id,
            activated=bool(world),
            constraints_satisfied=False,
            execution_distinguished=execution_distinguished,
            diagnostics=["semantic_validator_not_implemented"],
        )
    evidence: dict[str, Any] = {}
    diagnostics: list[str] = []
    satisfied = True
    for validator in validators:
        current, current_evidence, current_diagnostics = validator(world, obligation)
        satisfied = satisfied and current
        evidence.update(current_evidence)
        diagnostics.extend(current_diagnostics)
    return ObligationValidation(
        obligation.id,
        activated=True,
        constraints_satisfied=satisfied,
        execution_distinguished=execution_distinguished,
        diagnostics=diagnostics,
        evidence=evidence,
    )


__all__ = ["ObligationValidation", "validate_obligation"]
