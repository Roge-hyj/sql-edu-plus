"""Constraint extraction and atomic predicate validation helpers."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any
from collections import Counter, defaultdict
from itertools import product
import json
import re
from sqlglot import exp
from core.ast_schema import ASTDiffNode
from core.witness_generation.schema_scope import SchemaCatalog, extract_physical_table_names
from core.witness_generation.obligations import DistinguishingObligation
from core.witness_generation.planner import write_owner
from core.witness_generation.regex_support import (
    RegexEvaluationError,
    first_regex_non_match,
    glob_separating_values,
    like_candidate_domain,
    like_matches,
    like_separating_values,
    regex_separating_values,
)

from core.phase1_foundation import (
    _AGG_FUNC_TYPES,
    _changed_having_aggregate_spec,
    _changed_having_aggregate_spec_for_diffs,
    _clause_ast_diffs,
    _comparison_node_types,
    _direct_from_table,
    _distinct_having_count_requirement,
    _extract_column_name,
    _extract_having_aggregate_specs,
    _function_args,
    _function_name,
    _function_sql,
    _group_by_items,
    _has_diff,
    _is_cross_table_condition,
    _is_inside_join,
    _is_inside_subquery,
    _join_on_standard_assignments,
    _join_type_signature,
    _like_counter_value,
    _like_render_node,
    _limit_offset_required_rows,
    _literal_value,
    _nearest_select,
    _outer_distinct_signature,
    _parse_sql,
    _positive_probe_value,
    _predicate_assignment_truth,
    _predicate_leaf_map,
    _projection_is_true_inner,
    _projection_label,
    _result_order_clause,
    _select_projection_repr,
    _set_operator_modifier,
    _set_operator_signature,
    _sql_of,
    _strip_alias,
    _top_select,
    _unqualified_sql,
    _unwrap_paren,
    _window_signature,
)

from core.phase1_sql_semantics import (
    _apply_count_group_probe,
    _comparison_descriptor,
    _comparison_truth_value,
    _counter_probe_value,
    _expression_static_value,
    _extract_join_graph,
    _extract_literal_constraints,
    _group_probe_value,
    _is_date_column,
    _is_key_column,
    _is_numeric_column,
    _logical_leaf_nodes,
    _norm_name,
    _positive_group_filter_value,
    _seed_value,
)



def _materialize_null_sensitive_limit_order_witness(
    data: dict[str, list[dict[str, Any]]],
    standard_sql: str,
    student_sql: str,
) -> bool:
    """Keep NULL from masking a direction difference in a limited result."""
    standard_ast = _parse_sql(standard_sql)
    student_ast = _parse_sql(student_sql)
    if not isinstance(standard_ast, exp.Expression) or not isinstance(student_ast, exp.Expression):
        return False
    standard_select = _top_select(standard_ast)
    student_select = _top_select(student_ast)
    if not isinstance(standard_select, exp.Select) or not isinstance(student_select, exp.Select):
        return False
    if not (standard_select.args.get("limit") or student_select.args.get("limit")):
        return False
    standard_order = _result_order_clause(standard_ast)
    student_order = _result_order_clause(student_ast)
    if not isinstance(standard_order, exp.Order) or not isinstance(student_order, exp.Order):
        return False
    has_null_case = any(
        isinstance(item, exp.Case) and item.find(exp.Is) is not None
        for order in (standard_order, student_order)
        for ordered in (order.expressions or ())
        for item in [ordered.this if isinstance(ordered, exp.Ordered) else ordered]
    )
    if not has_null_case:
        return False
    order_column = next(
        (
            item.this if isinstance(item, exp.Ordered) else item
            for item in (standard_order.expressions or ())
            if isinstance(item.this if isinstance(item, exp.Ordered) else item, exp.Column)
        ),
        None,
    )
    if not isinstance(order_column, exp.Column):
        return False
    source = _direct_from_table(standard_select)
    if not isinstance(source, exp.Table):
        return False
    table_name = next(
        (name for name in data if _norm_name(name) == _norm_name(source.name)),
        None,
    )
    rows = data.get(table_name or "")
    if not rows or len(rows) < 2:
        return False
    lookup = _column_lookup(list(rows[0]))
    actual_order = lookup.get(_norm_name(order_column.name))
    if actual_order is None:
        return False
    projected_columns: list[str] = []
    for item in standard_select.expressions or ():
        expression = item.this if isinstance(item, exp.Alias) else item
        if isinstance(expression, exp.Column):
            actual = lookup.get(_norm_name(expression.name))
            if actual and actual != actual_order:
                projected_columns.append(actual)
    with write_owner("materializer:null_sensitive_limit_order"):
        # The standard ASC/DESC discriminator is strongest when every order
        # value is non-NULL.  This leaves the NULL CASE branch syntactically
        # present but prevents both queries from selecting the same null row.
        for index, row in enumerate(rows):
            row[actual_order] = 100 + index
            for column in projected_columns:
                value = row.get(column)
                if isinstance(value, str):
                    row[column] = f"__order_row_{index:03d}__"
                elif isinstance(value, (int, float, Decimal)):
                    row[column] = 10000 + index
    return True


def _repair_known_unsafe_division_paths(
    data: dict[str, list[dict[str, Any]]],
    standard_sql: str,
    student_sql: str,
    *,
    schema_catalog: SchemaCatalog | None = None,
) -> bool:
    """Avoid an accidental zero denominator in a bounded SQLite fixture.

    The public facility-cost teaching query can receive seed rows that make
    ``SUM(...) / 3 -
    monthlymaintenance`` exactly zero.  Adjust only the maintenance cell that
    creates that accidental zero, preserving the query's intended join and
    aggregate structure.  Other arithmetic shapes remain fail-closed rather
    than receiving guessed data.
    """
    combined = f"{standard_sql}\n{student_sql}".upper()
    required = ("SUM(", "MONTHLYMAINTENANCE", "INITIALOUTLAY", "/")
    if not all(token in combined for token in required):
        return False
    ast = _parse_sql(standard_sql)
    if ast is None or ast.find(exp.Div) is None:
        return False
    facilities_table = next(
        (
            table
            for table, rows in data.items()
            if rows
            and _column_lookup(list(rows[0])).get("monthlymaintenance") is not None
        ),
        None,
    )
    bookings_table = next(
        (
            table
            for table, rows in data.items()
            if rows
            and {"facid", "memid", "slots"}.issubset(
                _column_lookup(list(rows[0]))
            )
        ),
        None,
    )
    if facilities_table is None or bookings_table is None:
        return False
    facility_rows = data[facilities_table]
    booking_rows = data[bookings_table]
    facility_lookup = _column_lookup(list(facility_rows[0]))
    booking_lookup = _column_lookup(list(booking_rows[0]))
    maintenance_column = facility_lookup.get("monthlymaintenance")
    facility_key = facility_lookup.get("facid")
    booking_key = booking_lookup.get("facid")
    member_cost_column = facility_lookup.get("membercost")
    guest_cost_column = facility_lookup.get("guestcost")
    slots_column = booking_lookup.get("slots")
    memid_column = booking_lookup.get("memid")
    if not maintenance_column or not facility_key or not booking_key:
        return False
    if not member_cost_column or not guest_cost_column or not slots_column or not memid_column:
        return False

    def number(value: Any) -> Decimal | None:
        if isinstance(value, bool) or value is None:
            return None
        try:
            return Decimal(str(value))
        except (InvalidOperation, ValueError, TypeError):
            return None

    changed = False
    with write_owner("materializer:division_zero_guard"):
        for facility in facility_rows:
            key = facility.get(facility_key)
            total = Decimal("0")
            has_booking = False
            for booking in booking_rows:
                if booking.get(booking_key) != key:
                    continue
                slots = number(booking.get(slots_column))
                member_cost = number(facility.get(member_cost_column))
                guest_cost = number(facility.get(guest_cost_column))
                memid = booking.get(memid_column)
                rate = guest_cost if memid == 0 else member_cost
                if slots is None or rate is None:
                    continue
                total += slots * rate
                has_booking = True
            if not has_booking:
                continue
            maintenance = number(facility.get(maintenance_column))
            if maintenance is None:
                continue
            # The source query divides by 3.  Keep the guard narrow to the
            # documented PGExercises shape; only rows whose current value is
            # exactly the aggregate boundary are changed.
            denominator = total / Decimal("3") - maintenance
            if denominator != 0:
                continue
            replacement = maintenance + Decimal("1")
            if total / Decimal("3") - replacement == 0:
                replacement += Decimal("1")
            if isinstance(facility.get(maintenance_column), int):
                facility[maintenance_column] = int(replacement)
            else:
                facility[maintenance_column] = replacement
            changed = True
    return changed


def _materialize_aggregate_filter_witness(
    data: dict[str, list[dict[str, Any]]],
    obligations: list[DistinguishingObligation],
) -> None:
    """Materialize bounded true/false and divergent paths for FILTER."""
    for obligation in obligations:
        spec = next(
            (
                item
                for item in obligation.hard_constraints
                if item.kind == "aggregate_filter_paths"
            ),
            None,
        )
        if spec is None or not spec.relation:
            continue
        table_name = next(
            (name for name in data if _norm_name(name) == _norm_name(spec.relation)),
            None,
        )
        rows = data.get(table_name or "", [])
        if not rows:
            continue
        standard_text = str(dict(spec.metadata).get("standard_filter_predicate") or "")
        student_text = str(dict(spec.metadata).get("student_filter_predicate") or "")
        standard = _parse_sql(standard_text) if standard_text else None
        student = _parse_sql(student_text) if student_text else None
        if standard is None and student is None:
            continue

        leaves: dict[str, exp.Expression] = {}
        for predicate in (standard, student):
            if predicate is None:
                continue
            for leaf in _logical_leaf_nodes(predicate):
                leaf = _unwrap_paren(leaf)
                if not isinstance(
                    leaf,
                    (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE),
                ):
                    leaves = {}
                    break
                if not isinstance(leaf.left, exp.Column) or not isinstance(
                    leaf.right,
                    exp.Literal,
                ):
                    leaves = {}
                    break
                leaves.setdefault(_sql_of(leaf), leaf)
        if not leaves or len(leaves) > 6:
            continue

        candidates: list[tuple[dict[str, Any], dict[str, Any], bool, bool]] = []
        keys = list(leaves)
        for truth_values in product((False, True), repeat=len(keys)):
            assignment = dict(zip(keys, truth_values))
            standard_truth = _predicate_assignment_truth(standard, assignment)
            student_truth = _predicate_assignment_truth(student, assignment)
            if standard_truth is None or student_truth is None:
                continue
            values: dict[str, Any] = {}
            compatible = True
            for key, desired in assignment.items():
                leaf = leaves[key]
                value = _comparison_truth_value(leaf, desired)
                column = _norm_name(leaf.left.name)
                if value is None or (
                    column in values and values[column] != value
                ):
                    compatible = False
                    break
                values[column] = value
            if compatible:
                candidates.append((
                    assignment,
                    values,
                    bool(standard_truth),
                    bool(student_truth),
                ))
        candidates.sort(key=lambda item: item[2] == item[3])
        selected: list[tuple[dict[str, Any], dict[str, Any], bool, bool]] = []
        for candidate in candidates:
            if len(selected) >= min(6, len(rows)):
                break
            selected.append(candidate)
            standard_paths = {item[2] for item in selected}
            student_paths = {item[3] for item in selected}
            divergent = any(item[2] != item[3] for item in selected)
            if divergent and standard_paths == {True, False} and student_paths == {True, False}:
                break
            if divergent and (standard is None or standard_paths == {True, False}) and (
                student is None or student_paths == {True, False}
            ):
                break
        if not selected:
            continue

        group_columns = dict(spec.metadata).get("standard_group_columns") or ()
        column_lookup = _column_lookup(rows[0])
        with write_owner(f"materializer:{obligation.id}"):
            for row, (_assignment, values, _standard_truth, _student_truth) in zip(
                rows,
                selected,
            ):
                for column, value in values.items():
                    actual = column_lookup.get(_norm_name(column))
                    if actual is not None:
                        row[actual] = value
                for group_column in group_columns:
                    actual = column_lookup.get(_norm_name(str(group_column).split(".")[-1]))
                    if actual is not None and selected:
                        row[actual] = rows[0][actual]


def _materialize_regex_pattern_witness(
    data: dict[str, list[dict[str, Any]]],
    obligations: list[DistinguishingObligation],
) -> None:
    """Write a bounded string that separates two REGEXP predicates."""
    table_lookup = {_norm_name(name): name for name in data}
    for obligation in obligations:
        spec = next(
            (
                item
                for item in obligation.hard_constraints
                if item.kind == "regex_pattern_separation"
            ),
            None,
        )
        if spec is None or not spec.relation or not spec.column:
            continue
        table_name = table_lookup.get(_norm_name(spec.relation))
        rows = data.get(table_name or "", [])
        if not rows:
            continue
        column = _column_lookup(rows[0]).get(_norm_name(spec.column))
        if column is None:
            continue
        metadata = dict(spec.metadata)
        standard_pattern = metadata.get("standard_pattern")
        student_pattern = metadata.get("student_pattern")
        if not isinstance(standard_pattern, str) or not isinstance(
            student_pattern, str
        ):
            continue
        try:
            separated = regex_separating_values(
                standard_pattern,
                student_pattern,
            )
            if not separated:
                continue
            non_match = first_regex_non_match(
                (standard_pattern, student_pattern)
            )
        except RegexEvaluationError:
            continue

        with write_owner(f"materializer:{obligation.id}"):
            rows[0][column] = separated[0][0]
            reverse = next(
                (
                    value
                    for value, standard, student in separated[1:]
                    if standard != separated[0][1]
                    and student != separated[0][2]
                ),
                None,
            )
            if len(rows) > 1:
                rows[1][column] = reverse or separated[-1][0]
            if len(rows) > 2 and non_match is not None:
                rows[2][column] = non_match


def _materialize_like_pattern_witness(
    data: dict[str, list[dict[str, Any]]],
    obligations: list[DistinguishingObligation],
) -> None:
    """Write bounded values that separate two constant LIKE predicates."""
    table_lookup = {_norm_name(name): name for name in data}
    for obligation in obligations:
        spec = next(
            (
                item
                for item in obligation.hard_constraints
                if item.kind == "like_pattern_separation"
            ),
            None,
        )
        if spec is None or not spec.relation or not spec.column:
            continue
        table_name = table_lookup.get(_norm_name(spec.relation))
        rows = data.get(table_name or "", [])
        if not rows:
            continue
        column = _column_lookup(rows[0]).get(_norm_name(spec.column))
        if column is None:
            continue
        metadata = dict(spec.metadata)
        standard_pattern = metadata.get("standard_pattern")
        student_pattern = metadata.get("student_pattern")
        if not isinstance(standard_pattern, str) or not isinstance(
            student_pattern, str
        ):
            continue
        try:
            standard_escape = metadata.get("standard_escape")
            student_escape = metadata.get("student_escape")
            if not isinstance(standard_escape, str):
                standard_escape = "\\"
            if not isinstance(student_escape, str):
                student_escape = "\\"
            separated = like_separating_values(
                standard_pattern,
                student_pattern,
                standard_escape=standard_escape,
                student_escape=student_escape,
                case_insensitive=bool(metadata.get("case_insensitive")),
            )
        except RegexEvaluationError:
            continue
        if not separated:
            continue
        with write_owner(f"materializer:{obligation.id}"):
            for row, item in zip(rows[:3], separated[:3]):
                row[column] = item[0]


def _materialize_glob_pattern_witness(
    data: dict[str, list[dict[str, Any]]],
    obligations: list[DistinguishingObligation],
) -> None:
    """Write bounded values that separate two constant GLOB predicates."""
    table_lookup = {_norm_name(name): name for name in data}
    for obligation in obligations:
        spec = next(
            (
                item
                for item in obligation.hard_constraints
                if item.kind == "glob_pattern_separation"
            ),
            None,
        )
        if spec is None or not spec.relation or not spec.column:
            continue
        table_name = table_lookup.get(_norm_name(spec.relation))
        rows = data.get(table_name or "", [])
        if not rows:
            continue
        column = _column_lookup(rows[0]).get(_norm_name(spec.column))
        if column is None:
            continue
        metadata = dict(spec.metadata)
        standard_pattern = metadata.get("standard_pattern")
        student_pattern = metadata.get("student_pattern")
        if not isinstance(standard_pattern, str) or not isinstance(
            student_pattern, str
        ):
            continue
        try:
            separated = glob_separating_values(
                standard_pattern,
                student_pattern,
            )
        except RegexEvaluationError:
            continue
        if not separated:
            continue
        with write_owner(f"materializer:{obligation.id}"):
            for row, item in zip(rows[:3], separated[:3]):
                row[column] = item[0]


def _materialize_predicate_presence_obligation_witness(
    data: dict[str, list[dict[str, Any]]],
    standard_sql: str,
    student_sql: str,
    ast_diffs: list[ASTDiffNode],
) -> None:
    """Create one row where adding/removing a predicate changes filtering."""
    presence_diffs = [
        diff for diff in ast_diffs
        if diff.diff_type in {"predicate_missing", "predicate_added"}
        and not diff.extra.get("subquery_depth")
    ]
    if not presence_diffs:
        return
    if _materialize_like_presence_witness(data, presence_diffs):
        return
    standard_ast = _parse_sql(standard_sql)
    student_ast = _parse_sql(student_sql)
    standard_select = _top_select(standard_ast) if standard_ast is not None else None
    student_select = _top_select(student_ast) if student_ast is not None else None
    if not isinstance(standard_select, exp.Select) or not isinstance(student_select, exp.Select):
        return
    standard_where = standard_select.args.get("where")
    student_where = student_select.args.get("where")
    standard_predicate = standard_where.this if isinstance(standard_where, exp.Where) else None
    student_predicate = student_where.this if isinstance(student_where, exp.Where) else None

    leaves: dict[str, exp.Expression] = {}
    for predicate in (standard_predicate, student_predicate):
        if predicate is None:
            continue
        for leaf in _logical_leaf_nodes(predicate):
            leaf = _unwrap_paren(leaf)
            if not isinstance(leaf, (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE)):
                return
            if not isinstance(leaf.left, exp.Column) or not isinstance(leaf.right, exp.Literal):
                return
            leaves.setdefault(_sql_of(leaf), leaf)
    if not leaves or len(leaves) > 6:
        return

    keys = list(leaves)
    candidates: list[tuple[dict[str, bool], dict[str, Any], bool, bool]] = []
    for truth_values in product((False, True), repeat=len(keys)):
        assignment = dict(zip(keys, truth_values))
        standard_truth = _predicate_assignment_truth(standard_predicate, assignment)
        student_truth = _predicate_assignment_truth(student_predicate, assignment)
        if standard_truth is None or student_truth is None:
            continue
        values: dict[str, Any] = {}
        compatible = True
        for key, desired in assignment.items():
            leaf = leaves[key]
            column = _norm_name(leaf.left.name)
            value = _comparison_truth_value(leaf, desired)
            if value is None or (column in values and values[column] != value):
                compatible = False
                break
            values[column] = value
        if compatible:
            candidates.append(
                (assignment, values, bool(standard_truth), bool(student_truth))
            )
    if not candidates:
        return

    # A presence obligation needs both a real positive path for the predicate
    # it owns and a divergent path.  Selecting only the first divergent truth
    # assignment made ``x = c`` versus no WHERE produce an all-negative table,
    # so the validator could not prove that the predicate itself was active.
    def reference_truth(item: tuple[dict[str, bool], dict[str, Any], bool, bool]) -> bool:
        _assignment, _values, standard_truth, student_truth = item
        return standard_truth if standard_predicate is not None else student_truth

    positive = [item for item in candidates if reference_truth(item)]
    divergent = [item for item in candidates if item[2] != item[3]]
    selected: list[tuple[dict[str, Any], bool, bool]] = []
    seen_values: set[str] = set()
    for item in [*positive, *divergent, *candidates]:
        values, standard_truth, student_truth = item[1], item[2], item[3]
        signature = json.dumps(values, sort_keys=True, default=str)
        if signature in seen_values:
            continue
        seen_values.add(signature)
        selected.append((values, standard_truth, student_truth))
        if len(selected) >= min(3, len(candidates)):
            break

    candidate_tables = []
    for table_name, rows in data.items():
        if not rows:
            continue
        lookup = _column_lookup(rows[0].keys())
        if all(column in lookup for values, _standard_truth, _student_truth in selected for column in values):
            candidate_tables.append((table_name, rows, lookup))
    if len(candidate_tables) != 1:
        return
    _table_name, rows, lookup = candidate_tables[0]
    with write_owner("materializer:predicate_positive_negative"):
        for row_index, (values, _standard_truth, _student_truth) in enumerate(selected):
            if row_index >= len(rows):
                break
            row = rows[row_index]
            for column, value in values.items():
                row[lookup[column]] = value


def _materialize_like_presence_witness(
    data: dict[str, list[dict[str, Any]]],
    presence_diffs: list[ASTDiffNode],
) -> bool:
    """Materialize a positive/negative row for a missing or added LIKE."""
    for diff in presence_diffs:
        if diff.diff_type == "predicate_missing":
            active_sql = str(diff.extra.get("standard_sql") or "")
            query_sql = str(diff.extra.get("standard_query_sql") or "")
        else:
            active_sql = str(diff.extra.get("student_sql") or "")
            query_sql = str(diff.extra.get("student_query_sql") or "")
        if "LIKE" not in active_sql.upper() or not active_sql.strip():
            continue
        try:
            parsed = _parse_sql(
                f"SELECT * FROM __phase1_like_presence WHERE {active_sql}"
            )
            like = parsed.find(exp.Like) if parsed is not None else None
        except Exception:
            continue
        if not isinstance(like, exp.Like) or not isinstance(like.this, exp.Column):
            continue
        pattern = _expression_static_value(like.expression)
        if not isinstance(pattern, str):
            continue
        try:
            candidates = like_candidate_domain(pattern)
            counter_value = _like_counter_value(pattern)
            candidates = [counter_value, *candidates]
            matching = next(
                (
                    value
                    for value in candidates
                    if like_matches(pattern, value) is True
                ),
                None,
            )
            non_matching = next(
                (
                    value
                    for value in candidates
                    if like_matches(pattern, value) is False
                ),
                None,
            )
        except RegexEvaluationError:
            continue
        if matching is None or non_matching is None:
            continue
        negated = isinstance(like.parent, exp.Not)
        active_value = non_matching if negated else matching
        inactive_value = matching if negated else non_matching

        source_names: set[str] = set()
        if query_sql:
            try:
                query_ast = _parse_sql(query_sql)
                source_names = {
                    _norm_name(table.name)
                    for table in query_ast.find_all(exp.Table)
                    if table.name
                }
            except Exception:
                source_names = set()
        requested_column = _norm_name(like.this.name)
        candidates: list[tuple[str, list[dict[str, Any]], str]] = []
        for table_name, rows in data.items():
            if not rows:
                continue
            actual = _column_lookup(rows[0]).get(requested_column)
            if actual is None:
                continue
            if source_names and _norm_name(table_name) not in source_names:
                continue
            candidates.append((table_name, rows, actual))
        if not candidates and source_names:
            for table_name, rows in data.items():
                if not rows:
                    continue
                actual = _column_lookup(rows[0]).get(requested_column)
                if actual is not None:
                    candidates.append((table_name, rows, actual))
        if len(candidates) != 1:
            continue
        _table_name, rows, actual = candidates[0]
        with write_owner("materializer:predicate_like_presence"):
            rows[0][actual] = active_value
            for row in rows[1:]:
                row[actual] = inactive_value
        return True
    return False


def _materialize_aggregate_obligation_witness(
    data: dict[str, list[dict[str, Any]]],
    standard_sql: str,
    ast_diffs: list[ASTDiffNode],
) -> None:
    """Re-establish one aggregate boundary owned by the current witness world."""
    aggregate_diffs = []
    for diff in ast_diffs:
        if diff.diff_type not in {"comparison_operator_changed", "literal_changed", "aggregate_function_changed", "aggregate_argument_changed"}:
            continue
        expression = _parse_sql(str(diff.extra.get("standard_sql") or ""))
        if expression is not None and expression.find(*_AGG_FUNC_TYPES) is not None:
            aggregate_diffs.append(diff)
    if not aggregate_diffs:
        return
    target_diff = aggregate_diffs[0]
    aggregate = _parse_sql(str(target_diff.extra.get("standard_sql") or ""))
    if aggregate is None:
        return
    agg_node = aggregate.find(*_AGG_FUNC_TYPES)
    if agg_node is None:
        return
    comparison = aggregate.find(exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE)
    boundary = target_diff.extra.get("value")
    if boundary is None or not isinstance(boundary, (int, float, Decimal)):
        return
    source = _direct_from_table(_parse_sql(standard_sql) or aggregate)
    if source is None:
        return
    table_name = _norm_name(source.name)
    actual_table = next((name for name in data if _norm_name(name) == table_name), None)
    rows = data.get(actual_table or "")
    if not rows:
        return
    query_ast = _parse_sql(standard_sql)
    select = None
    if query_ast is not None:
        for candidate in query_ast.find_all(exp.Select):
            if agg_node in list(candidate.find_all(*_AGG_FUNC_TYPES)):
                select = candidate
                break
        select = select or _top_select(query_ast)
    group = select.args.get("group") if isinstance(select, exp.Select) else None
    if not isinstance(group, exp.Group):
        return
    group_columns = [
        _norm_name(item.name)
        for item in (group.expressions or ())
        if isinstance(item, exp.Column)
    ]
    argument = agg_node.this
    argument_column = argument.find(exp.Column) if isinstance(argument, exp.Expression) else None
    value_name = _norm_name(argument_column.name) if isinstance(argument_column, exp.Column) else ""
    distinct = bool(agg_node.args.get("distinct") or isinstance(agg_node.this, exp.Distinct))
    function = type(agg_node).__name__.upper()
    lookup = _column_lookup(list(rows[0]))
    group_actual = [lookup.get(column) for column in group_columns]
    value_table_ref = _norm_name(argument_column.table) if isinstance(argument_column, exp.Column) else ""
    value_table = value_table_ref
    if value_table_ref and query_ast is not None:
        aliases = _table_aliases(query_ast)
        value_table = aliases.get(value_table_ref, value_table_ref)
    if value_table and value_table != table_name:
        return
    value_actual = lookup.get(value_name) if value_name else None
    if not group_actual or any(item is None for item in group_actual):
        return
    if function == "COUNT" and not value_actual and value_name:
        return
    group_size = max(2, min(len(rows), int(boundary) if function == "COUNT" else 2))
    anchor = rows[0]
    for row in rows[:group_size]:
        for column in group_actual:
            row[column] = anchor[column]
    if function == "COUNT":
        if not value_actual:
            return
        for index, row in enumerate(rows[:group_size]):
            row[value_actual] = (900000 + index if distinct else 1)
    elif function == "SUM" and value_actual:
        share = boundary / group_size
        for row in rows[:group_size]:
            row[value_actual] = share
    elif function == "AVG" and value_actual:
        for index, row in enumerate(rows[:group_size]):
            row[value_actual] = boundary if index == 0 else boundary
    elif function in {"MIN", "MAX"} and value_actual:
        for row in rows[:group_size]:
            row[value_actual] = boundary


def _mask_sql_literals_identifiers_and_comments(sql: str) -> str:
    """Return SQL code with quoted/comment text replaced by spaces.

    The unsupported-feature boundary must not mistake teaching data such as
    ``'use DATEADD here'`` or a quoted column named ``"pivot"`` for syntax.
    Keeping the original length also makes the result safe for regex checks
    that span whitespace.
    """

    masked = list(sql)
    index = 0
    length = len(sql)
    while index < length:
        if sql.startswith("--", index):
            end = sql.find("\n", index + 2)
            end = length if end < 0 else end
            for position in range(index, end):
                masked[position] = " "
            index = end
            continue
        if sql.startswith("/*", index):
            end = sql.find("*/", index + 2)
            end = length if end < 0 else end + 2
            for position in range(index, end):
                masked[position] = " "
            index = end
            continue

        quote = sql[index]
        closing = "]" if quote == "[" else quote
        if quote not in {"'", '"', "`", "["}:
            index += 1
            continue
        masked[index] = " "
        index += 1
        while index < length:
            masked[index] = " "
            if sql[index] != closing:
                index += 1
                continue
            if closing != "]" and index + 1 < length and sql[index + 1] == closing:
                masked[index + 1] = " "
                index += 2
                continue
            index += 1
            break
    return "".join(masked)


def _detect_unsupported_features(*sql_items: str) -> list[str]:
    """Reject syntax outside the bounded SQLite research contract.

    These patterns are a fail-closed input boundary, not alternate dialect
    implementations.  A matched query is never parsed, lowered, or executed.
    """
    checks: tuple[tuple[str, str], ...] = (
        (
            "LIMIT_WITH_TIES",
            r"(?is)(?:\bTOP\s*(?:\([^)]*\)|\d+)\s+WITH\s+TIES\b|"
            r"\bFETCH\s+(?:FIRST|NEXT)\s+[^;]*?\s+WITH\s+TIES\b|"
            r"\bLIMIT\s+\d+\s+WITH\s+TIES\b)",
        ),
        (
            "LIMIT_PERCENT",
            r"(?is)(?:\bTOP\s*(?:\([^)]*\)|\d+)\s+PERCENT\b|"
            r"\bFETCH\s+(?:FIRST|NEXT)\s+[^;]*?\s+PERCENT\b)",
        ),
        (
            "TOP_LIMIT",
            r"(?is)\bSELECT\s+(?:DISTINCT\s+)?TOP\s*(?:\([^)]*\)|\d+)"
            r"(?!\s+(?:WITH\s+TIES|PERCENT)\b)",
        ),
        (
            "FETCH_LIMIT",
            r"(?is)\bFETCH\s+(?:FIRST|NEXT)\b(?![^;]*?\b(?:WITH\s+TIES|PERCENT)\b)",
        ),
        (
            "GROUP_CONCAT_ORDERING",
            r"(?is)\bGROUP_CONCAT\s*\([^)]*\bORDER\s+BY\b",
        ),
        (
            "GROUP_CONCAT_SEPARATOR",
            r"(?is)\bGROUP_CONCAT\s*\([^)]*\bSEPARATOR\b",
        ),
        ("ROWNUM_PSEUDOCOLUMN", r"(?is)\bROWNUM\b"),
        ("HIERARCHICAL_CONNECT_BY", r"(?is)\bCONNECT\s+BY\b"),
        ("HIERARCHICAL_START_WITH", r"(?is)\bSTART\s+WITH\b"),
        ("LISTAGG", r"(?is)\bLISTAGG\s*\("),
        ("PIVOT", r"(?is)\bPIVOT\b"),
        ("UNPIVOT", r"(?is)\bUNPIVOT\b"),
        ("LATERAL", r"(?is)\bLATERAL\b"),
        ("APPLY", r"(?is)\b(?:CROSS|OUTER)\s+APPLY\b"),
        ("ROLLUP", r"(?is)\bROLLUP\s*\("),
        ("WITH_ROLLUP", r"(?is)\bWITH\s+ROLLUP\b"),
        ("CUBE", r"(?is)\bCUBE\s*\("),
        ("GROUPING_SETS", r"(?is)\bGROUPING\s+SETS\s*\("),
        ("GROUPING", r"(?is)\bGROUPING\s*\("),
        ("INTERSECT_ALL", r"(?is)\bINTERSECT\s+ALL\b"),
        ("EXCEPT_ALL", r"(?is)\bEXCEPT\s+ALL\b"),
        ("JSON_ARRAY_TABLE_FUNCTION", r"(?is)\bjsonb?_array_elements(?:_text)?\s*\("),
        ("DISTINCT_ON", r"(?is)\bDISTINCT\s+ON\s*\("),
        ("TYPE_CAST_OPERATOR", r"(?is)::\s*[A-Za-z_]"),
        ("ILIKE", r"(?is)\bILIKE\b"),
        ("INTERVAL_LITERAL", r"(?is)\bINTERVAL\s+'"),
        ("GENERATE_SERIES", r"(?is)\bGENERATE_SERIES\s*\("),
        ("SELECT_VARIABLE_ASSIGNMENT", r"(?is)\bSELECT\s+@[A-Z_][A-Z0-9_$]*\s*="),
        ("OPTION_HINT", r"(?is)\bOPTION\s*\("),
        ("RECURSIVE_SEARCH", r"(?is)\bSEARCH\s+(?:DEPTH|BREADTH)\s+FIRST\b"),
        ("RECURSIVE_CYCLE", r"(?is)\bCYCLE\b[^;]*\bUSING\b"),
        ("QUALIFY", r"(?is)\bQUALIFY\b"),
        ("TABLE_SAMPLE", r"(?is)\bTABLESAMPLE\b"),
        ("FROM_ONLY", r"(?is)\b(?:FROM|JOIN)\s+ONLY\b"),
        ("LOCKING_CLAUSE", r"(?is)\bFOR\s+(?:UPDATE|SHARE)\b"),
        ("SIMILAR_TO", r"(?is)\bSIMILAR\s+TO\b"),
        ("SQL_SERVER_ISNULL", r"(?is)\bISNULL\s*\("),
        ("EXTRACT", r"(?is)\bEXTRACT\s*\("),
        ("DATE_TRUNC", r"(?is)\b(?:DATE_TRUNC|TIMESTAMP_TRUNC)\s*\("),
        (
            "NON_SQLITE_DATE_FUNCTION",
            r"(?is)\b(?:DATEADD|DATEDIFF|DATEPART|TIMESTAMPDIFF|STR_TO_DATE|"
            r"STR_TO_TIME|YEAR|MONTH|DAY|GETDATE|NOW)\s*\(",
        ),
        (
            "NON_SQLITE_SCALAR_FUNCTION",
            r"(?is)\b(?:FIND_IN_SET|SUBSTRING_INDEX|WIDTH_BUCKET|REGEXP_LIKE)\s*\(",
        ),
        ("BIND_PARAMETER", r"(?is)(?<!:):[A-Z_][A-Z0-9_$]*|@[A-Z_][A-Z0-9_$]*"),
    )
    found: list[str] = []
    seen: set[str] = set()
    combined = "\n".join(
        _mask_sql_literals_identifiers_and_comments(item)
        for item in sql_items
        if item
    )
    for feature, pattern in checks:
        if feature not in seen and re.search(pattern, combined):
            found.append(feature)
            seen.add(feature)
    if re.search(
        r"(?is)(?:<=|>=|<>|!=|=|<|>)\s*(?:ALL|ANY|SOME)\s*\(",
        combined,
    ):
        found.append("QUANTIFIED_SUBQUERY_COMPARISON")
    return found


def _is_likely_sqlite_capability_error(error: str, sql: str | None) -> bool:
    # Runtime classification uses the explicit SQLite boundary catalog; an
    # error message never selects or emulates a different SQL dialect.
    del error
    return bool(_detect_unsupported_features(sql or ""))


def _outer_join_predicate_placement_ast_diffs(
    standard_ast: exp.Expression,
    student_ast: exp.Expression,
) -> list[ASTDiffNode]:
    """Recognize one predicate moved between LEFT JOIN ON and WHERE.

    The bounded Phase 1 contract deliberately handles the common teaching
    form where this movement is the only ON/WHERE leaf difference.  More
    complex simultaneous edits remain separate obligations instead of being
    over-collapsed into an allegedly atomic repair.
    """
    standard_select = _top_select(standard_ast)
    student_select = _top_select(student_ast)
    if not isinstance(standard_select, exp.Select) or not isinstance(
        student_select, exp.Select
    ):
        return []
    standard_joins = list(standard_select.args.get("joins") or ())
    student_joins = list(student_select.args.get("joins") or ())
    if len(standard_joins) != len(student_joins):
        return []

    standard_where = _predicate_leaf_map(standard_select.args.get("where"))
    student_where = _predicate_leaf_map(student_select.args.get("where"))
    results: list[ASTDiffNode] = []
    for join_index, (standard_join, student_join) in enumerate(
        zip(standard_joins, student_joins)
    ):
        if _join_type_signature(standard_join) != _join_type_signature(
            student_join
        ):
            continue
        side = str(standard_join.args.get("side") or "").upper()
        if side != "LEFT":
            continue
        standard_right = _sql_of(standard_join.this)
        student_right = _sql_of(student_join.this)
        if standard_right.lower() != student_right.lower():
            continue
        standard_on = _predicate_leaf_map(standard_join.args.get("on"))
        student_on = _predicate_leaf_map(student_join.args.get("on"))
        on_to_where = (set(standard_on) - set(student_on)) & (
            set(student_where) - set(standard_where)
        )
        where_to_on = (set(student_on) - set(standard_on)) & (
            set(standard_where) - set(student_where)
        )
        if len(on_to_where) == 1:
            moved_keys = on_to_where
            movement = "ON_TO_WHERE"
            moved = standard_on[next(iter(moved_keys))]
        elif len(where_to_on) == 1:
            moved_keys = where_to_on
            movement = "WHERE_TO_ON"
            moved = student_on[next(iter(moved_keys))]
        else:
            continue
        if (
            set(standard_on) ^ set(student_on) != moved_keys
            or set(standard_where) ^ set(student_where) != moved_keys
        ):
            continue

        right_table = (
            str(standard_join.this.name)
            if isinstance(standard_join.this, exp.Table)
            else standard_right
        )
        target = next(iter(moved.find_all(exp.Column)), None)
        results.append(ASTDiffNode(
            clause_category="JOIN ON",
            diff_type="join_predicate_placement_changed",
            target_table=right_table,
            target_column=(str(target.name) if isinstance(target, exp.Column) else None),
            standard_node=moved,
            student_node=(
                student_where[next(iter(moved_keys))]
                if movement == "ON_TO_WHERE"
                else standard_where[next(iter(moved_keys))]
            ),
            knowledge_point_id="join-on",
            extra={
                "movement": movement,
                "join_index": join_index,
                "standard_side": side,
                "right_table": right_table,
                "moved_predicate_sql": _sql_of(moved),
                "standard_on_sql": _sql_of(standard_join.args.get("on")),
                "student_on_sql": _sql_of(student_join.args.get("on")),
                "standard_where_sql": _sql_of(standard_select.args.get("where")),
                "student_where_sql": _sql_of(student_select.args.get("where")),
                "standard_query_sql": _sql_of(standard_select),
                "student_query_sql": _sql_of(student_select),
                "standard_join_pairs": _join_on_column_pairs(
                    _sql_of(standard_select)
                ),
                "student_join_pairs": _join_on_column_pairs(
                    _sql_of(student_select)
                ),
                "query_scope": "root",
            },
        ))
    return results


def _strict_monotonic_recursive_union_modifier_equivalent(
    standard_ast: exp.Expression,
    student_ast: exp.Expression,
) -> bool:
    """Prove UNION/UNION ALL equal for one bounded monotonic recurrence."""
    if not (_is_recursive_ast(standard_ast) and _is_recursive_ast(student_ast)):
        return False
    standard_unions = list(standard_ast.find_all(exp.Union))
    student_unions = list(student_ast.find_all(exp.Union))
    if len(standard_unions) != 1 or len(student_unions) != 1:
        return False
    standard_union = standard_unions[0]
    student_union = student_unions[0]
    if {
        _set_operator_modifier(standard_union),
        _set_operator_modifier(student_union),
    } != {"ALL", "DISTINCT"}:
        return False
    normalized = standard_ast.copy()
    normalized_union = next(iter(normalized.find_all(exp.Union)), None)
    if not isinstance(normalized_union, exp.Union):
        return False
    normalized_union.set("distinct", student_union.args.get("distinct"))
    if _sql_of(normalized) != _sql_of(student_ast):
        return False

    cte = standard_union.find_ancestor(exp.CTE)
    if not isinstance(cte, exp.CTE) or not cte.alias:
        return False
    cte_name = _norm_name(cte.alias)

    def direct_tables(select: exp.Select) -> list[exp.Table]:
        return [
            table
            for table in select.find_all(exp.Table)
            if table.find_ancestor(exp.Select) is select
        ]

    left = standard_union.this
    right = standard_union.expression
    if not isinstance(left, exp.Select) or not isinstance(right, exp.Select):
        return False
    left_tables = direct_tables(left)
    right_tables = direct_tables(right)
    left_recursive = any(_norm_name(table.name) == cte_name for table in left_tables)
    right_recursive = any(_norm_name(table.name) == cte_name for table in right_tables)
    if left_recursive == right_recursive:
        return False
    anchor, recursive = (right, left) if left_recursive else (left, right)
    anchor_tables = direct_tables(anchor)
    recursive_tables = direct_tables(recursive)
    if anchor_tables or len(recursive_tables) != 1:
        return False
    recursive_source = recursive_tables[0]
    if _norm_name(recursive_source.name) != cte_name:
        return False
    if any(
        select.args.get(key)
        for select in (anchor, recursive)
        for key in (
            "joins", "group", "having", "order", "limit", "offset",
            "distinct", "with", "with_",
        )
    ):
        return False
    if len(anchor.expressions or ()) != 1 or len(recursive.expressions or ()) != 1:
        return False
    if any(
        nested is not select
        for select in (anchor, recursive)
        for nested in select.find_all(exp.Select)
    ):
        return False

    anchor_projection = anchor.expressions[0]
    anchor_value_node = (
        anchor_projection.this
        if isinstance(anchor_projection, exp.Alias)
        else anchor_projection
    )
    if not isinstance(anchor_value_node, exp.Literal) or anchor_value_node.is_string:
        return False
    anchor_value = _literal_value(anchor_value_node)
    if not isinstance(anchor_value, (int, float, Decimal)) or isinstance(anchor_value, bool):
        return False

    cte_alias = cte.args.get("alias")
    output_columns = (
        list(cte_alias.args.get("columns") or ())
        if isinstance(cte_alias, exp.TableAlias)
        else []
    )
    if len(output_columns) == 1 and isinstance(output_columns[0], exp.Identifier):
        state_name = _norm_name(output_columns[0].name)
    else:
        state_name = _norm_name(anchor_projection.alias_or_name)
    if not state_name:
        return False

    recursive_projection = recursive.expressions[0]
    step_expression = (
        recursive_projection.this
        if isinstance(recursive_projection, exp.Alias)
        else recursive_projection
    )
    state_column: exp.Column | None = None
    step: int | float | Decimal | None = None
    if isinstance(step_expression, exp.Add):
        operands = (step_expression.left, step_expression.right)
        state_column = next(
            (item for item in operands if isinstance(item, exp.Column)),
            None,
        )
        step_node = next(
            (item for item in operands if isinstance(item, exp.Literal)),
            None,
        )
        step = _literal_value(step_node) if isinstance(step_node, exp.Literal) else None
    elif (
        isinstance(step_expression, exp.Sub)
        and isinstance(step_expression.left, exp.Column)
        and isinstance(step_expression.right, exp.Literal)
    ):
        state_column = step_expression.left
        raw_step = _literal_value(step_expression.right)
        step = -raw_step if isinstance(raw_step, (int, float, Decimal)) else None
    if (
        not isinstance(state_column, exp.Column)
        or _norm_name(state_column.name) != state_name
        or not isinstance(step, (int, float, Decimal))
        or isinstance(step, bool)
        or step == 0
    ):
        return False
    source_refs = {
        cte_name,
        _norm_name(recursive_source.alias or ""),
    }
    if state_column.table and _norm_name(state_column.table) not in source_refs:
        return False

    where = recursive.args.get("where")
    predicate = _unwrap_paren(where.this) if isinstance(where, exp.Where) else None
    if not isinstance(predicate, (exp.LT, exp.LTE, exp.GT, exp.GTE)):
        return False
    if isinstance(predicate.left, exp.Column) and isinstance(predicate.right, exp.Literal):
        bound_column = predicate.left
        column_on_left = True
    elif isinstance(predicate.right, exp.Column) and isinstance(predicate.left, exp.Literal):
        bound_column = predicate.right
        column_on_left = False
    else:
        return False
    if _norm_name(bound_column.name) != state_name:
        return False
    if bound_column.table and _norm_name(bound_column.table) not in source_refs:
        return False
    increasing_bound = (
        isinstance(predicate, (exp.LT, exp.LTE))
        if column_on_left
        else isinstance(predicate, (exp.GT, exp.GTE))
    )
    decreasing_bound = (
        isinstance(predicate, (exp.GT, exp.GTE))
        if column_on_left
        else isinstance(predicate, (exp.LT, exp.LTE))
    )
    return bool((step > 0 and increasing_bound) or (step < 0 and decreasing_bound))


def _commutative_set_branch_permutation_equivalent(
    standard_ast: exp.Expression,
    student_ast: exp.Expression,
) -> bool:
    """Recognize pure branch permutations of UNION/INTERSECT trees.

    EXCEPT is intentionally excluded.  Result-level ordering/limits,
    recursive CTEs and mixed set operators retain their original structure
    because branch position can be observable or operationally significant.
    """
    allowed = (exp.Union, exp.Intersect)
    if not isinstance(standard_ast, allowed) or not isinstance(student_ast, allowed):
        return False
    if type(standard_ast) is not type(student_ast):
        return False
    if _set_operator_modifier(standard_ast) != _set_operator_modifier(student_ast):
        return False
    if _is_recursive_ast(standard_ast) or _is_recursive_ast(student_ast):
        return False
    if list(standard_ast.find_all(exp.CTE)) or list(student_ast.find_all(exp.CTE)):
        return False
    if any(
        ast.args.get(key) is not None
        for ast in (standard_ast, student_ast)
        for key in ("order", "limit", "offset")
    ):
        return False

    root_type = type(standard_ast)
    modifier = _set_operator_modifier(standard_ast)

    def branches(node: exp.Expression) -> list[exp.Expression] | None:
        if isinstance(node, (exp.Union, exp.Intersect, exp.Except)):
            if type(node) is not root_type or _set_operator_modifier(node) != modifier:
                return None
            left = branches(node.this)
            right = branches(node.expression)
            if left is None or right is None:
                return None
            return [*left, *right]
        return [node]

    standard_branches = branches(standard_ast)
    student_branches = branches(student_ast)
    if standard_branches is None or student_branches is None:
        return False
    standard_sql = [_sql_of(item) for item in standard_branches]
    student_sql = [_sql_of(item) for item in student_branches]
    if standard_sql == student_sql or Counter(standard_sql) != Counter(student_sql):
        return False

    projections = {
        _select_projection_repr(item)
        for item in [*standard_branches, *student_branches]
    }
    return bool(len(projections) == 1 and "" not in projections)


def _simple_cte_dependency_chain_inline_equivalent(
    cte_ast: exp.Expression,
    inline_ast: exp.Expression,
) -> bool:
    """Collapse one side-effect-free ``SELECT *`` CTE dependency.

    This only widens the existing single-CTE equivalence rule to the common
    teaching form ``physical table -> passthrough CTE -> filtered CTE``.  The
    passthrough must preserve every row and column and may only be consumed by
    the second CTE.  The resulting query is still checked by
    ``_simple_cte_inline_equivalent``; this helper does not independently
    declare arbitrary CTE chains equivalent.
    """
    if list(inline_ast.find_all(exp.CTE)):
        return False
    copied = cte_ast.copy()
    with_node = next(iter(copied.find_all(exp.With)), None)
    ctes = list(with_node.expressions or ()) if isinstance(with_node, exp.With) else []
    if len(ctes) != 2:
        return False
    passthrough, dependent = ctes
    passthrough_select = (
        passthrough.this
        if isinstance(passthrough.this, exp.Select)
        else None
    )
    dependent_select = (
        dependent.this
        if isinstance(dependent.this, exp.Select)
        else None
    )
    if not isinstance(passthrough_select, exp.Select) or not isinstance(
        dependent_select, exp.Select
    ):
        return False
    passthrough_alias = _norm_name(passthrough.alias or "")
    dependent_alias = _norm_name(dependent.alias or "")
    if not passthrough_alias or not dependent_alias:
        return False
    declared_alias = passthrough.args.get("alias")
    if isinstance(declared_alias, exp.TableAlias) and declared_alias.args.get("columns"):
        return False
    if (
        len(passthrough_select.expressions or ()) != 1
        or not isinstance(passthrough_select.expressions[0], exp.Star)
        or any(
            passthrough_select.args.get(key)
            for key in (
                "joins", "where", "group", "having", "order", "limit",
                "offset", "distinct", "with", "with_",
            )
        )
    ):
        return False
    physical_source = _direct_from_table(passthrough_select)
    dependent_source = _direct_from_table(dependent_select)
    outer = _top_select(copied)
    outer_source = _direct_from_table(outer)
    if not physical_source or not dependent_source or not outer_source:
        return False
    if _norm_name(dependent_source.name) != passthrough_alias:
        return False
    if _norm_name(outer_source.name) != dependent_alias:
        return False
    references = [
        table
        for table in copied.find_all(exp.Table)
        if _norm_name(table.name) == passthrough_alias
    ]
    if len(references) != 1 or references[0] is not dependent_source:
        return False

    replacement = physical_source.copy()
    reference_name = _norm_name(dependent_source.alias or passthrough.alias or "")
    if reference_name:
        replacement.set(
            "alias",
            exp.TableAlias(this=exp.to_identifier(reference_name)),
        )
    dependent_source.replace(replacement)
    with_node.set("expressions", [dependent])
    return _simple_cte_inline_equivalent(copied, inline_ast)


def _simple_derived_table_inline_equivalent(
    derived_ast: exp.Expression,
    inline_ast: exp.Expression,
) -> bool:
    """Recognize one projection-only derived table moved into its parent."""
    if not isinstance(derived_ast, exp.Select) or not isinstance(inline_ast, exp.Select):
        return False
    if any(
        derived_ast.args.get(key)
        for key in (
            "joins", "where", "group", "having", "order", "limit", "offset",
            "distinct", "with", "with_",
        )
    ):
        return False
    if any(
        inline_ast.args.get(key)
        for key in ("joins", "group", "having", "order", "limit", "offset", "distinct", "with", "with_")
    ):
        return False
    derived_from = derived_ast.args.get("from_") or derived_ast.args.get("from")
    derived_source = derived_from.this if isinstance(derived_from, exp.From) else None
    if not isinstance(derived_source, exp.Subquery) or not derived_source.alias:
        return False
    inner = derived_source.this
    if not isinstance(inner, exp.Select):
        return False
    if any(
        inner.args.get(key)
        for key in (
            "joins", "group", "having", "order", "limit", "offset",
            "distinct", "with", "with_",
        )
    ):
        return False
    if any(nested is not inner for nested in inner.find_all(exp.Select)):
        return False
    inner_source = _direct_from_table(inner)
    inline_source = _direct_from_table(inline_ast)
    if (
        not inner_source
        or not inline_source
        or inner_source.alias
        or inline_source.alias
        or _norm_name(inner_source.name) != _norm_name(inline_source.name)
    ):
        return False
    if _unqualified_sql(inner.args.get("where")) != _unqualified_sql(
        inline_ast.args.get("where")
    ):
        return False

    inner_projection: dict[str, exp.Column] = {}
    for expression in inner.expressions or ():
        if not isinstance(expression, exp.Column) or expression.table:
            return False
        name = _norm_name(expression.name)
        if not name or name in inner_projection:
            return False
        inner_projection[name] = expression
    if not inner_projection:
        return False

    derived_alias = _norm_name(derived_source.alias)
    mapped_projection: list[str] = []
    for expression in derived_ast.expressions or ():
        if not isinstance(expression, exp.Column):
            return False
        if expression.table and _norm_name(expression.table) != derived_alias:
            return False
        mapped = inner_projection.get(_norm_name(expression.name))
        if mapped is None:
            return False
        mapped_projection.append(_norm_name(mapped.name))
    inline_projection: list[str] = []
    for expression in inline_ast.expressions or ():
        if not isinstance(expression, exp.Column) or expression.table:
            return False
        inline_projection.append(_norm_name(expression.name))
    return bool(mapped_projection and mapped_projection == inline_projection)




def _named_window_inline_equivalent(
    named_ast: exp.Expression,
    inline_ast: exp.Expression,
) -> bool:
    """Expand one unmodified named WINDOW and compare the complete AST."""
    if not isinstance(named_ast, exp.Select) or not isinstance(inline_ast, exp.Select):
        return False
    copied = named_ast.copy()
    definitions = list(copied.args.get("windows") or ())
    if len(definitions) != 1 or inline_ast.args.get("windows"):
        return False
    definition = definitions[0]
    if not isinstance(definition, exp.Window) or not isinstance(
        definition.this, exp.Identifier
    ):
        return False
    window_name = _norm_name(definition.this.name)
    if not window_name or definition.args.get("alias") or definition.args.get("over"):
        return False
    references = [
        node
        for expression in copied.expressions or ()
        for node in expression.walk()
        if isinstance(node, exp.Window)
        and node.find_ancestor(exp.Select) is copied
    ]
    if not references:
        return False
    for reference in references:
        alias = reference.args.get("alias")
        if not isinstance(alias, exp.Identifier) or _norm_name(alias.name) != window_name:
            return False
        if any(
            reference.args.get(key)
            for key in ("partition_by", "order", "spec", "first")
        ):
            return False
        reference.set("alias", None)
        for key in ("partition_by", "order", "spec", "first"):
            value = definition.args.get(key)
            if isinstance(value, list):
                reference.set(key, [item.copy() for item in value])
            elif isinstance(value, exp.Expression):
                reference.set(key, value.copy())
            elif value is not None:
                reference.set(key, value)
    copied.set("windows", None)
    return _sql_of(copied) == _sql_of(inline_ast)


def _single_row_aggregate_cte_scalar_equivalent(
    cte_ast: exp.Expression,
    scalar_ast: exp.Expression,
) -> bool:
    """Recognize one scalar aggregate CTE consumed as a CROSS JOIN value.

    A SELECT containing one aggregate expression and no GROUP BY/HAVING is
    guaranteed to yield exactly one row, including on empty input.  Therefore
    one qualified reference to that value through an unconditional CROSS JOIN
    is equivalent to the same SELECT used as a scalar subquery.  The rule is
    intentionally implemented as an AST rewrite followed by full-query
    equality so unrelated projection, filter, join, and subquery changes are
    never hidden.
    """
    if not isinstance(cte_ast, exp.Select) or not isinstance(
        scalar_ast, exp.Select
    ):
        return False
    if list(scalar_ast.find_all(exp.CTE)):
        return False

    copied = cte_ast.copy()
    with_clause = copied.args.get("with_") or copied.args.get("with")
    if not isinstance(with_clause, exp.With) or with_clause.args.get("recursive"):
        return False
    ctes = list(with_clause.expressions or ())
    if len(ctes) != 1:
        return False
    cte = ctes[0]
    if cte.args.get("materialized") is not None:
        return False
    body = cte.this
    if not isinstance(body, exp.Select) or len(body.expressions or ()) != 1:
        return False
    if any(
        body.args.get(key)
        for key in (
            "group", "having", "order", "limit", "offset",
            "distinct", "with", "with_",
        )
    ):
        return False

    projected = body.expressions[0]
    aggregate = projected.this if isinstance(projected, exp.Alias) else projected
    if not isinstance(aggregate, exp.AggFunc) or aggregate.find(exp.Window):
        return False

    cte_alias = cte.args.get("alias")
    if not isinstance(cte_alias, exp.TableAlias):
        return False
    cte_name = _norm_name(cte_alias.name)
    declared_columns = list(cte_alias.args.get("columns") or ())
    if declared_columns:
        if len(declared_columns) != 1 or not isinstance(
            declared_columns[0], exp.Identifier
        ):
            return False
        output_name = _norm_name(declared_columns[0].name)
    else:
        output_name = _norm_name(projected.alias_or_name)
    if not cte_name or not output_name:
        return False

    joins = list(copied.args.get("joins") or ())
    matching_joins = [
        join
        for join in joins
        if isinstance(join.this, exp.Table)
        and _norm_name(join.this.name) == cte_name
    ]
    if len(matching_joins) != 1:
        return False
    cte_join = matching_joins[0]
    join_kind = str(cte_join.args.get("kind") or "").upper()
    if join_kind not in {"", "CROSS"} or any(
        cte_join.args.get(key)
        for key in ("side", "on", "using", "method")
    ):
        return False
    joined_table = cte_join.this
    reference_name = _norm_name(joined_table.alias or cte_name)
    if not reference_name:
        return False

    # The CTE must be consumed only by this direct outer join.  A second table
    # reference could multiply rows or place the value in another query scope.
    cte_table_references = [
        table
        for table in copied.find_all(exp.Table)
        if table.find_ancestor(exp.CTE) is None
        and _norm_name(table.name) == cte_name
    ]
    if len(cte_table_references) != 1 or cte_table_references[0] is not joined_table:
        return False

    value_references: list[exp.Column] = []
    for column in copied.find_all(exp.Column):
        if column.find_ancestor(exp.CTE) is not None:
            continue
        table_name = _norm_name(column.table or "")
        column_name = _norm_name(column.name)
        if table_name in {reference_name, cte_name}:
            if table_name != reference_name or column_name != output_name:
                return False
            value_references.append(column)
        elif not table_name and column_name == output_name:
            # An unqualified column could bind to either the physical source
            # or the CTE.  Decline rather than make a scope guess.
            return False
    if len(value_references) != 1:
        return False

    scalar_body = body.copy()
    scalar_body.set("expressions", [aggregate.copy()])
    value_references[0].replace(exp.Subquery(this=scalar_body))
    copied.set("joins", [join for join in joins if join is not cte_join])
    copied.set("with_", None)
    copied.set("with", None)
    return _sql_of(copied) == _sql_of(scalar_ast)


def _simple_cte_inline_equivalent(cte_ast: exp.Expression, inline_ast: exp.Expression) -> bool:
    # This helper intentionally permits the one supported CTE -> inline
    # rewrite, but still rejects unrelated set/window/distinct shape changes.
    if (
        _set_operator_signature(cte_ast) != _set_operator_signature(inline_ast)
        or _window_signature(cte_ast) != _window_signature(inline_ast)
        or _outer_distinct_signature(cte_ast) != _outer_distinct_signature(inline_ast)
    ):
        return False
    ctes = list(cte_ast.find_all(exp.CTE))
    if len(ctes) != 1 or list(inline_ast.find_all(exp.CTE)):
        return False
    outer = _top_select(cte_ast)
    inline = _top_select(inline_ast)
    cte_select = ctes[0].this if isinstance(ctes[0].this, exp.Select) else ctes[0].this.find(exp.Select)
    if not isinstance(outer, exp.Select) or not isinstance(inline, exp.Select) or not isinstance(cte_select, exp.Select):
        return False
    # Only allow a genuinely simple CTE body.  In particular, do not hide
    # changed GROUP/HAVING/ORDER/LIMIT/JOIN/DISTINCT semantics as an inline
    # rewrite merely because the projected labels happen to match.
    if any(
        cte_select.args.get(key)
        for key in ("joins", "group", "having", "order", "limit", "offset", "distinct", "with", "with_")
    ):
        return False
    outer_source = _direct_from_table(outer)
    cte_source = _direct_from_table(cte_select)
    inline_source = _direct_from_table(inline)
    if not outer_source or not cte_source or not inline_source:
        return False
    if _norm_name(outer_source.name) != _norm_name(ctes[0].alias or ""):
        return False
    if _norm_name(cte_source.name) != _norm_name(inline_source.name):
        return False
    unsupported = ("joins", "where", "group", "having", "order", "limit", "offset")
    if any(outer.args.get(key) for key in unsupported):
        return False
    if any(inline.args.get(key) for key in ("joins", "group", "having")):
        return False
    # The outer query's result shaping must be preserved by the inline form.
    # The CTE body WHERE is compared below as the filter that moves outward;
    # ORDER/LIMIT/OFFSET belong to the outer query and
    # therefore must match exactly on both sides.
    for key in ("order", "limit", "offset"):
        if _sql_of(outer.args.get(key)) != _sql_of(inline.args.get(key)):
            return False
    if _select_projection_repr(cte_ast) == "" or _select_projection_repr(inline_ast) == "":
        return False
    outer_projection = [_norm_name(_projection_label(item)) for item in outer.expressions or []]
    body_items = [
        item.this if isinstance(item, exp.Alias) else item
        for item in cte_select.expressions or []
    ]
    cte_alias = ctes[0].args.get("alias")
    declared_columns = (
        list(cte_alias.args.get("columns") or [])
        if isinstance(cte_alias, exp.TableAlias)
        else []
    )
    output_names = [
        _norm_name(item.name)
        for item in declared_columns
        if isinstance(item, exp.Identifier) and item.name
    ]
    if len(output_names) != len(body_items):
        output_names = [
            _norm_name(_projection_label(item))
            for item in body_items
        ]
    cte_projection = {
        name: item
        for name, item in zip(output_names, body_items)
        if name
    }
    inline_projection = [_norm_name(_projection_label(item)) for item in inline.expressions or []]
    mapped_outer_projection: list[str] = []
    for item in outer.expressions or []:
        expression = item.this if isinstance(item, exp.Alias) else item
        if not isinstance(expression, exp.Column):
            mapped_outer_projection = []
            break
        mapped = cte_projection.get(_norm_name(expression.name))
        if mapped is None:
            mapped_outer_projection = []
            break
        mapped_outer_projection.append(_norm_name(_projection_label(mapped)))
    return (
        bool(mapped_outer_projection)
        and mapped_outer_projection == inline_projection
        and _unqualified_sql(cte_select.args.get("where")) == _unqualified_sql(inline.args.get("where"))
    )


def _simple_in_join_equivalent(in_ast: exp.Expression, join_ast: exp.Expression) -> bool:
    """Handle the common PK-membership rewrite: x IN (SELECT id ...) -> INNER JOIN."""
    if (
        _set_operator_signature(in_ast) != _set_operator_signature(join_ast)
        or _window_signature(in_ast) != _window_signature(join_ast)
        or _outer_distinct_signature(in_ast) != _outer_distinct_signature(join_ast)
        or list(in_ast.find_all(exp.CTE))
        or list(join_ast.find_all(exp.CTE))
    ):
        return False
    in_select = _top_select(in_ast)
    join_select = _top_select(join_ast)
    if not isinstance(in_select, exp.Select) or not isinstance(join_select, exp.Select):
        return False
    in_nodes = [node for node in in_select.find_all(exp.In) if not _is_inside_subquery(node)]
    joins = list(join_select.args.get("joins") or [])
    if len(in_nodes) != 1 or len(joins) != 1:
        return False
    in_node = in_nodes[0]
    if isinstance(in_node.parent, exp.Not):
        return False
    query = in_node.args.get("query")
    inner = query.this if isinstance(query, exp.Subquery) else None
    join = joins[0]
    if not isinstance(in_node.this, exp.Column) or not isinstance(inner, exp.Select) or not isinstance(join, exp.Join):
        return False
    if str(join.args.get("side") or "").upper() not in {"", "INNER"}:
        return False
    inner_source = _direct_from_table(inner)
    join_source = join.this if isinstance(join.this, exp.Table) else None
    in_source = _direct_from_table(in_select)
    direct_join_source = _direct_from_table(join_select)
    if not all((inner_source, join_source, in_source, direct_join_source)):
        return False
    if _norm_name(inner_source.name) != _norm_name(join_source.name):
        return False
    if _norm_name(in_source.name) != _norm_name(direct_join_source.name):
        return False
    projected = inner.expressions[0] if len(inner.expressions or []) == 1 else None
    projected = projected.this if isinstance(projected, exp.Alias) else projected
    on = join.args.get("on")
    if not isinstance(projected, exp.Column) or not isinstance(on, exp.EQ):
        return False
    on_columns = list(on.find_all(exp.Column))
    if len(on_columns) != 2:
        return False
    expected_names = {_norm_name(in_node.this.name), _norm_name(projected.name)}
    if {_norm_name(column.name) for column in on_columns} != expected_names:
        return False
    outer_where = in_select.args.get("where")
    if not isinstance(outer_where, exp.Where) or _unwrap_paren(outer_where.this) is not in_node:
        return False
    return (
        _select_projection_repr(in_ast) == _select_projection_repr(join_ast)
        and _unqualified_sql(inner.args.get("where")) == _unqualified_sql(join_select.args.get("where"))
    )


def _simple_not_exists_antijoin_equivalent(exists_ast: exp.Expression, join_ast: exp.Expression) -> bool:
    if (
        _set_operator_signature(exists_ast) != _set_operator_signature(join_ast)
        or _window_signature(exists_ast) != _window_signature(join_ast)
        or _outer_distinct_signature(exists_ast) != _outer_distinct_signature(join_ast)
        or list(exists_ast.find_all(exp.CTE))
        or list(join_ast.find_all(exp.CTE))
    ):
        return False
    exists_select = _top_select(exists_ast)
    join_select = _top_select(join_ast)
    if not isinstance(exists_select, exp.Select) or not isinstance(join_select, exp.Select):
        return False
    not_exists = next(
        (node for node in exists_select.find_all(exp.Not) if isinstance(_unwrap_paren(node.this), exp.Exists)),
        None,
    )
    joins = list(join_select.args.get("joins") or [])
    if not not_exists or len(joins) != 1:
        return False
    join = joins[0]
    if not isinstance(join, exp.Join) or str(join.args.get("side") or "").upper() != "LEFT":
        return False
    exists = _unwrap_paren(not_exists.this)
    inner = exists.this if isinstance(exists, exp.Exists) else None
    inner_select = inner if isinstance(inner, exp.Select) else inner.find(exp.Select) if isinstance(inner, exp.Expression) else None
    inner_source = _direct_from_table(inner_select)
    join_source = join.this if isinstance(join.this, exp.Table) else None
    where = join_select.args.get("where")
    null_check = where.find(exp.Is) if isinstance(where, exp.Where) else None
    if not inner_source or not join_source or not isinstance(null_check, exp.Is) or not isinstance(null_check.expression, exp.Null):
        return False
    if _norm_name(inner_source.name) != _norm_name(join_source.name):
        return False
    inner_equalities = [node for node in inner_select.find_all(exp.EQ)] if inner_select else []
    join_equalities = [node for node in join.args.get("on").find_all(exp.EQ)] if join.args.get("on") else []
    inner_pairs = {frozenset(_norm_name(col.name) for col in node.find_all(exp.Column)) for node in inner_equalities}
    join_pairs = {frozenset(_norm_name(col.name) for col in node.find_all(exp.Column)) for node in join_equalities}
    return bool(inner_pairs & join_pairs) and _select_projection_repr(exists_ast) == _select_projection_repr(join_ast)


def _projection_truth_predicate_metadata(
    predicate: exp.Expression,
    select: exp.Select,
) -> dict[str, Any]:
    """Describe the bounded input domain needed for a three-valued path."""
    columns = (
        [predicate]
        if isinstance(predicate, exp.Column)
        else list(predicate.find_all(exp.Column))
    )
    unique_columns: dict[tuple[str, str], exp.Column] = {}
    for item in columns:
        if not isinstance(item, exp.Column) or not item.name:
            continue
        unique_columns.setdefault(
            (_norm_name(item.table or ""), _norm_name(item.name)),
            item,
        )
    target = next(iter(unique_columns.values())) if len(unique_columns) == 1 else None
    aliases = _table_aliases(select)
    source_table = ""
    target_column = ""
    if isinstance(target, exp.Column):
        target_column = _norm_name(target.name)
        qualifier = _norm_name(target.table or "")
        source_table = aliases.get(qualifier, qualifier)
        if not source_table:
            direct = _direct_from_table(select)
            source_table = _norm_name(direct.name) if direct is not None else ""

    operator = ""
    boundary: Any = None
    if isinstance(predicate, exp.Column):
        operator = "COLUMN"
    elif isinstance(predicate, (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE)):
        left, right = predicate.left, predicate.right
        if isinstance(left, exp.Column) and isinstance(right, exp.Literal):
            operator = type(predicate).__name__.upper()
            boundary = _literal_value(right)
        elif isinstance(right, exp.Column) and isinstance(left, exp.Literal):
            operator = {
                "GT": "LT",
                "GTE": "LTE",
                "LT": "GT",
                "LTE": "GTE",
                "EQ": "EQ",
                "NEQ": "NEQ",
            }.get(type(predicate).__name__.upper(), "")
            boundary = _literal_value(left)

    return {
        "standard_source_table": source_table,
        "predicate_column": target_column,
        "predicate_operator": operator,
        "predicate_value": boundary,
    }


def _boolean_projection_truth_test_diffs(
    standard_ast: exp.Expression,
    student_ast: exp.Expression,
    *,
    standard_sql: str,
    student_sql: str,
) -> list[ASTDiffNode]:
    """Detect one projection-only ``IS TRUE`` wrapper change.

    Requiring the complete normalized queries to match prevents this focused
    diff from hiding an independent projection, filter, join, or ordering
    error.  Multiple changed projection slots remain on the generic path so
    mutation-to-diff binding stays atomic and unambiguous.
    """
    standard_select = _top_select(standard_ast)
    student_select = _top_select(student_ast)
    if not isinstance(standard_select, exp.Select) or not isinstance(
        student_select, exp.Select
    ):
        return []
    standard_items = list(standard_select.expressions or ())
    student_items = list(student_select.expressions or ())
    if len(standard_items) != len(student_items):
        return []

    changed: list[tuple[int, exp.Expression, exp.Expression, exp.Expression, bool]] = []
    for position, (standard_item, student_item) in enumerate(
        zip(standard_items, student_items)
    ):
        standard_alias = standard_item.alias if isinstance(standard_item, exp.Alias) else ""
        student_alias = student_item.alias if isinstance(student_item, exp.Alias) else ""
        if _norm_name(standard_alias) != _norm_name(student_alias):
            return []
        standard_expression = (
            standard_item.this if isinstance(standard_item, exp.Alias) else standard_item
        )
        student_expression = (
            student_item.this if isinstance(student_item, exp.Alias) else student_item
        )
        if _sql_of(standard_expression) == _sql_of(student_expression):
            continue
        standard_inner = _projection_is_true_inner(standard_expression)
        student_inner = _projection_is_true_inner(student_expression)
        if (standard_inner is not None) == (student_inner is not None):
            return []
        predicate = (
            standard_inner if standard_inner is not None else student_inner
        )
        bare = student_expression if standard_inner is not None else standard_expression
        if not isinstance(predicate, exp.Expression) or (
            _sql_of(predicate) != _sql_of(_unwrap_paren(bare))
        ):
            return []
        changed.append((
            position,
            standard_expression,
            student_expression,
            predicate,
            standard_inner is not None,
        ))

    if len(changed) != 1:
        return []

    position, standard_node, student_node, predicate, standard_is_true = changed[0]

    def normalized(ast: exp.Expression, position: int) -> str:
        copied = ast.copy()
        select = _top_select(copied)
        if not isinstance(select, exp.Select):
            return ""
        expressions = list(select.expressions or ())
        item = expressions[position]
        expression = item.this if isinstance(item, exp.Alias) else item
        inner = _projection_is_true_inner(expression)
        if inner is not None:
            if isinstance(item, exp.Alias):
                item.set("this", inner.copy())
            else:
                expressions[position] = inner.copy()
                select.set("expressions", expressions)
        return _sql_of(copied)

    if normalized(standard_ast, position) != normalized(student_ast, position):
        return []

    metadata = _projection_truth_predicate_metadata(predicate, standard_select)
    return [ASTDiffNode(
        clause_category="SELECT",
        diff_type="boolean_projection_truth_test_changed",
        target_table=metadata.get("standard_source_table") or None,
        target_column=metadata.get("predicate_column") or None,
        standard_node=standard_node,
        student_node=student_node,
        knowledge_point_id="null-handling",
        severity=0.74,
        extra={
            **metadata,
            "position": position,
            "predicate_sql": _sql_of(predicate),
            "standard_is_true": standard_is_true,
            "student_is_true": not standard_is_true,
            "standard_sql": _sql_of(standard_node),
            "student_sql": _sql_of(student_node),
            "standard_query_sql": standard_sql,
            "student_query_sql": student_sql,
            "query_scope": "root",
        },
    )]


def _in_exists_rewrite(
    in_ast: exp.Expression,
    exists_ast: exp.Expression,
    *,
    allow_negated: bool = False,
) -> bool:
    in_node = in_ast.find(exp.In)
    exists = exists_ast.find(exp.Exists)
    if not isinstance(in_node, exp.In) or not isinstance(exists, exp.Exists):
        return False
    in_negated = isinstance(in_node.parent, exp.Not)
    exists_negated = isinstance(exists.parent, exp.Not)
    if allow_negated:
        if not (in_negated and exists_negated):
            return False
    elif in_negated or exists_negated:
        return False
    query = in_node.args.get("query")
    inner = query.this if isinstance(query, exp.Subquery) else None
    exists_inner = exists.this if isinstance(exists.this, exp.Select) else exists.this.find(exp.Select) if isinstance(exists.this, exp.Expression) else None
    if not isinstance(inner, exp.Select) or not isinstance(exists_inner, exp.Select) or not isinstance(in_node.this, exp.Column):
        return False
    projected = inner.expressions[0] if len(inner.expressions or []) == 1 else None
    projected = projected.this if isinstance(projected, exp.Alias) else projected
    if not isinstance(projected, exp.Column):
        return False
    correlation = next(
        (
            eq for eq in exists_inner.find_all(exp.EQ)
            if {_norm_name(col.name) for col in eq.find_all(exp.Column)} == {
                _norm_name(projected.name), _norm_name(in_node.this.name)
            }
        ),
        None,
    )
    return correlation is not None and {
        _norm_name(table.name) for table in inner.find_all(exp.Table)
    } == {
        _norm_name(table.name) for table in exists_inner.find_all(exp.Table)
    }










def _projection_alias_ast_diffs(
    standard_ast: exp.Expression,
    student_ast: exp.Expression,
) -> list[ASTDiffNode]:
    std_select = standard_ast.find(exp.Select)
    stu_select = student_ast.find(exp.Select)
    if not isinstance(std_select, exp.Select) or not isinstance(stu_select, exp.Select):
        return []
    diffs: list[ASTDiffNode] = []
    for position, (std_item, stu_item) in enumerate(zip(std_select.expressions, stu_select.expressions)):
        std_expr = std_item.this if isinstance(std_item, exp.Alias) else std_item
        stu_expr = stu_item.this if isinstance(stu_item, exp.Alias) else stu_item
        if _sql_of(_strip_alias(std_expr)) != _sql_of(_strip_alias(stu_expr)):
            continue
        std_alias = std_item.alias if isinstance(std_item, exp.Alias) else ""
        stu_alias = stu_item.alias if isinstance(stu_item, exp.Alias) else ""
        if _norm_name(std_alias) == _norm_name(stu_alias):
            continue
        diffs.append(ASTDiffNode(
            clause_category="SELECT",
            diff_type="alias_changed",
            target_column=_extract_column_name(std_expr),
            standard_node=std_item,
            student_node=stu_item,
            knowledge_point_id="select-alias",
            severity=0.35,
            extra={
                "position": position,
                "standard_alias": std_alias,
                "student_alias": stu_alias,
                "standard_sql": _sql_of(std_item),
                "student_sql": _sql_of(stu_item),
            },
        ))
    return diffs


def _function_argument_ast_diffs(
    standard_ast: exp.Expression,
    student_ast: exp.Expression,
    filter_subqueries: bool = True,
) -> list[ASTDiffNode]:
    skip = _is_inside_subquery if filter_subqueries else (lambda _: False)
    # sqlglot models AND/OR connectors as Func subclasses. They are boolean
    # structure, not function calls; treating their operands as arguments
    # creates a phantom function diff for every ordinary predicate change.
    std_funcs = [
        node for node in standard_ast.find_all(exp.Func)
        if not skip(node) and not isinstance(node, exp.Connector)
    ]
    stu_funcs = [
        node for node in student_ast.find_all(exp.Func)
        if not skip(node) and not isinstance(node, exp.Connector)
    ]
    diffs: list[ASTDiffNode] = []
    for std_func, stu_func in zip(std_funcs, stu_funcs):
        if _function_name(std_func) != _function_name(stu_func):
            continue
        std_args = _function_args(std_func)
        stu_args = _function_args(stu_func)
        if std_args == stu_args:
            continue
        if isinstance(std_func, exp.RegexpLike) and isinstance(
            stu_func, exp.RegexpLike
        ):
            standard_column = (
                std_func.this if isinstance(std_func.this, exp.Column) else None
            )
            student_column = (
                stu_func.this if isinstance(stu_func.this, exp.Column) else None
            )
            standard_pattern = (
                _literal_value(std_func.expression)
                if isinstance(std_func.expression, exp.Literal)
                else None
            )
            student_pattern = (
                _literal_value(stu_func.expression)
                if isinstance(stu_func.expression, exp.Literal)
                else None
            )
            if (
                standard_column is not None
                and student_column is not None
                and _sql_of(standard_column) == _sql_of(student_column)
                and isinstance(standard_pattern, str)
                and isinstance(student_pattern, str)
                and standard_pattern != student_pattern
            ):
                standard_select = _nearest_select(std_func)
                student_select = _nearest_select(stu_func)
                source = (
                    _direct_from_table(standard_select)
                    if isinstance(standard_select, exp.Select)
                    else None
                )
                target_table = standard_column.table or (
                    source.name if isinstance(source, exp.Table) else None
                )
                diffs.append(ASTDiffNode(
                    clause_category="PREDICATE",
                    diff_type="regex_pattern_changed",
                    target_table=target_table,
                    target_column=standard_column.name,
                    standard_node=std_func,
                    student_node=stu_func,
                    knowledge_point_id="regex",
                    severity=0.74,
                    extra={
                        "standard_pattern": standard_pattern,
                        "student_pattern": student_pattern,
                        "standard_sql": _function_sql(std_func),
                        "student_sql": _function_sql(stu_func),
                        "standard_query_sql": _sql_of(
                            standard_select or standard_ast
                        ),
                        "student_query_sql": _sql_of(
                            student_select or student_ast
                        ),
                        "standard_source_table": (
                            source.name if isinstance(source, exp.Table) else ""
                        ),
                        "query_scope": (
                            "subquery" if _is_inside_subquery(std_func) else "root"
                        ),
                    },
                ))
                continue
        is_aggregate = isinstance(std_func, exp.AggFunc) and isinstance(stu_func, exp.AggFunc)
        if is_aggregate:
            std_columns = sorted({_norm_name(column.name) for column in std_func.find_all(exp.Column)})
            stu_columns = sorted({_norm_name(column.name) for column in stu_func.find_all(exp.Column)})
            # Most same-column aggregate expression differences are covered by
            # the projection/expression passes. GROUP_CONCAT is different: its
            # internal ORDER BY direction and SEPARATOR are first-class result
            # semantics even though the referenced column set is unchanged.
            if std_columns == stu_columns and not isinstance(std_func, exp.GroupConcat):
                continue
        diff_type = "aggregate_argument_changed" if is_aggregate else "function_argument_changed"
        standard_select = _nearest_select(std_func)
        student_select = _nearest_select(stu_func)
        standard_source = (
            _direct_from_table(standard_select)
            if isinstance(standard_select, exp.Select)
            else None
        )
        student_source = (
            _direct_from_table(student_select)
            if isinstance(student_select, exp.Select)
            else None
        )
        aggregate_context = {
            "standard_query_sql": _sql_of(standard_select or standard_ast),
            "student_query_sql": _sql_of(student_select or student_ast),
            "standard_source_table": (
                standard_source.name if isinstance(standard_source, exp.Table) else ""
            ),
            "student_source_table": (
                student_source.name if isinstance(student_source, exp.Table) else ""
            ),
            "standard_aggregate_function": (
                _function_name(std_func).upper() if is_aggregate else ""
            ),
            "student_aggregate_function": (
                _function_name(stu_func).upper() if is_aggregate else ""
            ),
            "standard_aggregate_argument": (
                _sql_of(std_func.this) if is_aggregate and std_func.this is not None else "*"
            ),
            "student_aggregate_argument": (
                _sql_of(stu_func.this) if is_aggregate and stu_func.this is not None else "*"
            ),
            "standard_group_columns": (
                [sql for sql, _ in _group_by_items(standard_ast)] if is_aggregate else []
            ),
            "student_group_columns": (
                [sql for sql, _ in _group_by_items(student_ast)] if is_aggregate else []
            ),
            "standard_aggregate_distinct": (
                bool(std_func.args.get("distinct") or isinstance(std_func.this, exp.Distinct))
                if is_aggregate else False
            ),
            "student_aggregate_distinct": (
                bool(stu_func.args.get("distinct") or isinstance(stu_func.this, exp.Distinct))
                if is_aggregate else False
            ),
        }
        diffs.append(ASTDiffNode(
            clause_category="AGGREGATE" if is_aggregate else "FUNCTION",
            diff_type=diff_type,
            target_column=_extract_column_name(std_func),
            standard_node=std_func,
            student_node=stu_func,
            knowledge_point_id="aggregate" if is_aggregate else "function",
            severity=0.66,
            extra={
                "function": _function_name(std_func),
                "standard_args": std_args,
                "student_args": stu_args,
                "standard_sql": _function_sql(std_func),
                "student_sql": _function_sql(stu_func),
                **aggregate_context,
            },
        ))
    return diffs


def _comparison_ast_diffs(
    standard_ast: exp.Expression,
    student_ast: exp.Expression,
    filter_subqueries: bool = True,
) -> list[ASTDiffNode]:
    _skip = _is_inside_subquery if filter_subqueries else (lambda _: False)
    std_comparisons = [
        _comparison_descriptor(node)
        for node in standard_ast.find_all(*_comparison_node_types())
        if not _skip(node)
        and not _is_inside_join(node)
        and not _is_cross_table_condition(node)
    ]
    stu_comparisons = [
        _comparison_descriptor(node)
        for node in student_ast.find_all(*_comparison_node_types())
        if not _skip(node)
        and not _is_inside_join(node)
        and not _is_cross_table_condition(node)
    ]
    std_comparisons = [item for item in std_comparisons if item]
    stu_comparisons = [item for item in stu_comparisons if item]

    # Index student comparisons by normalised column name; track which have been matched.
    stu_by_col: dict[str, list[tuple[int, dict[str, Any]]]] = {}
    for idx, item in enumerate(stu_comparisons):
        stu_by_col.setdefault(_norm_name(item["column"]), []).append((idx, item))
    stu_matched: set[int] = set()  # indices of student comparisons already paired
    std_matches: dict[int, tuple[int, dict[str, Any]]] = {}
    # Reserve exact body matches globally before pairing any fallback.  This
    # prevents an earlier unmatched standard predicate from consuming a
    # later student predicate that belongs to an unchanged repeated column.
    for std_index, std in enumerate(std_comparisons):
        std_sql_key = re.sub(r"\s+", "", str(std.get("sql") or "").lower())
        for idx, cand in stu_by_col.get(_norm_name(std["column"]), []):
            if idx in stu_matched:
                continue
            cand_sql_key = re.sub(r"\s+", "", str(cand.get("sql") or "").lower())
            if cand_sql_key == std_sql_key:
                std_matches[std_index] = (idx, cand)
                stu_matched.add(idx)
                break

    diffs: list[ASTDiffNode] = []
    for std_index, std in enumerate(std_comparisons):
        matched = std_matches.get(std_index)
        stu: dict[str, Any] | None = matched[1] if matched else None
        stu_idx: int | None = matched[0] if matched else None
        if stu is None:
            # Positional fallback is safe only when both blocks have the same
            # number of comparable predicates.  With unequal counts, leave
            # the unmatched node as missing/added rather than pairing it with
            # a semantically different repeated column.
            remaining_student = [
                (idx, cand)
                for idx, cand in stu_by_col.get(_norm_name(std["column"]), [])
                if idx not in stu_matched
            ]
            if (
                len(std_comparisons) == len(stu_comparisons)
                and len(remaining_student) == 1
            ):
                stu_idx, stu = remaining_student[0]
                stu_matched.add(stu_idx)
            elif len(std_comparisons) == len(stu_comparisons):
                # Multiple predicates on the same column are common in
                # interval questions.  Pair the boundary with the same
                # literal before falling back to ``missing/added``; otherwise
                # ``salary > 50000 AND salary < 60000`` versus
                # ``salary >= 50000 AND salary <= 60000`` is decomposed into
                # four unrelated predicates and loses atomic operator
                # evidence.  An exact value is required here so repeated
                # columns with different constants remain conservative.
                same_value = [
                    (idx, cand)
                    for idx, cand in remaining_student
                    if cand.get("value") == std.get("value")
                ]
                if len(same_value) == 1:
                    stu_idx, stu = same_value[0]
                    stu_matched.add(stu_idx)
        if stu is None:
            diffs.append(ASTDiffNode(
                clause_category="PREDICATE",
                diff_type="predicate_missing",
                target_column=std["column"],
                standard_node=std.get("node"),
                student_node=None,
                knowledge_point_id="where",
                extra={
                    **std,
                    "standard_sql": std["sql"],
                    "student_sql": "",
                    "standard_query_sql": _sql_of(standard_ast),
                    "student_query_sql": _sql_of(student_ast),
                }
            ))
            continue
        stu_matched.add(stu_idx)
        std_values = std.get("values")
        stu_values = stu.get("values")
        values_changed = std_values is not None and stu_values is not None and std_values != stu_values
        expression_value_changed = (
            std["op"] == stu["op"]
            and std.get("value") != stu.get("value")
            and std.get("value_kind") == "expression"
            and stu.get("value_kind") == "expression"
        )
        if expression_value_changed:
            # The nested expression receives its own query-block/function/
            # aggregate diff. Calling its rendered SQL a changed *literal*
            # duplicates that obligation at the outer comparison.
            continue
        standard_node = std.get("node")
        student_node = stu.get("node")
        same_like_predicate = (
            isinstance(standard_node, exp.Like)
            and isinstance(student_node, exp.Like)
            and type(standard_node) is type(student_node)
        )
        if same_like_predicate and (
            std.get("value") != stu.get("value")
            or std.get("escape") != stu.get("escape")
        ):
            standard_select = _nearest_select(standard_node)
            source = (
                _direct_from_table(standard_select)
                if isinstance(standard_select, exp.Select)
                else None
            )
            target_table = standard_node.this.table or (
                source.name if isinstance(source, exp.Table) else None
            )
            standard_escape = std.get("escape")
            student_escape = stu.get("escape")
            if not isinstance(standard_escape, str):
                standard_escape = "\\"
            if not isinstance(student_escape, str):
                student_escape = "\\"
            diffs.append(ASTDiffNode(
                clause_category="PREDICATE",
                diff_type="like_pattern_changed",
                target_table=target_table,
                target_column=standard_node.this.name,
                standard_node=_like_render_node(standard_node),
                student_node=_like_render_node(student_node),
                knowledge_point_id="like",
                severity=0.72,
                extra={
                    "standard_pattern": std.get("value"),
                    "student_pattern": stu.get("value"),
                    "standard_escape": standard_escape,
                    "student_escape": student_escape,
                    "case_insensitive": False,
                    "standard_sql": _sql_of(_like_render_node(standard_node)),
                    "student_sql": _sql_of(_like_render_node(student_node)),
                    "standard_query_sql": _sql_of(standard_select or standard_ast),
                    "student_query_sql": _sql_of(
                        _nearest_select(student_node) or student_ast
                    ),
                    "standard_source_table": (
                        source.name if isinstance(source, exp.Table) else ""
                    ),
                    "query_scope": (
                        "subquery" if _is_inside_subquery(standard_node) else "root"
                    ),
                },
            ))
            continue
        same_glob_predicate = (
            isinstance(standard_node, exp.Glob)
            and isinstance(student_node, exp.Glob)
        )
        if same_glob_predicate and std.get("value") != stu.get("value"):
            standard_select = _nearest_select(standard_node)
            source = (
                _direct_from_table(standard_select)
                if isinstance(standard_select, exp.Select)
                else None
            )
            target_table = standard_node.this.table or (
                source.name if isinstance(source, exp.Table) else None
            )
            diffs.append(ASTDiffNode(
                clause_category="PREDICATE",
                diff_type="glob_pattern_changed",
                target_table=target_table,
                target_column=standard_node.this.name,
                standard_node=standard_node,
                student_node=student_node,
                knowledge_point_id="glob",
                severity=0.7,
                extra={
                    "standard_pattern": std.get("value"),
                    "student_pattern": stu.get("value"),
                    "standard_sql": std["sql"],
                    "student_sql": stu["sql"],
                    "standard_query_sql": _sql_of(standard_select or standard_ast),
                    "student_query_sql": _sql_of(
                        _nearest_select(student_node) or student_ast
                    ),
                    "standard_source_table": (
                        source.name if isinstance(source, exp.Table) else ""
                    ),
                    "query_scope": (
                        "subquery" if _is_inside_subquery(standard_node) else "root"
                    ),
                },
            ))
            continue
        # A comparison nested under HAVING is still discovered by the shared
        # predicate pass.  Preserve that clause context instead of labelling
        # every literal/operator edit as ``where``; otherwise an aggregate
        # threshold mutation receives a HAVING summary plus a WHERE atomic
        # obligation, breaking the one-mutation evidence chain and attribution.
        std_node_context = std.get("node")
        context_node = std_node_context
        while isinstance(context_node, exp.Expression) and context_node.parent is not None:
            if isinstance(context_node.parent, exp.Having):
                break
            context_node = context_node.parent
        predicate_kp = "having" if isinstance(context_node.parent, exp.Having) else "where"
        predicate_clause = "HAVING" if predicate_kp == "having" else "PREDICATE"
        if (std["op"] != stu["op"]
                or std.get("value") != stu.get("value")
                or std.get("high") != stu.get("high")
                or values_changed):
            if std["op"] != stu["op"]:
                diff_type = "comparison_operator_changed"
            elif values_changed:
                std_set = set(std_values or [])
                stu_set = set(stu_values or [])
                diff_type = "in_list_member_removed" if std_set - stu_set else "in_list_member_added"
            else:
                diff_type = "literal_changed"
            diffs.append(ASTDiffNode(
                clause_category=predicate_clause,
                diff_type=diff_type,
                target_column=std["column"],
                standard_node=std.get("node"),
                student_node=stu.get("node"),
                knowledge_point_id=predicate_kp,
                extra={
                    "column": std["column"],
                    "standard_op": std["op"],
                    "student_op": stu["op"],
                    "value": std.get("value"),
                    "student_value": stu.get("value"),
                    "standard_value_kind": std.get("value_kind"),
                    "student_value_kind": stu.get("value_kind"),
                    "standard_right_column": std.get("right_column"),
                    "student_right_column": stu.get("right_column"),
                    "standard_right_table": std.get("right_table"),
                    "student_right_table": stu.get("right_table"),
                    "values": std_values,
                    "student_values": stu_values,
                    "standard_sql": std["sql"],
                    "student_sql": stu["sql"],
                    "predicate_clause": predicate_clause,
                }
            ))
        if stu["op"] in {"EQ", "NEQ"} and stu.get("value_is_null"):
            diffs.append(ASTDiffNode(
                clause_category="NULL",
                diff_type="null_equality_changed",
                target_column=stu["column"],
                standard_node=std.get("node"),
                student_node=stu.get("node"),
                knowledge_point_id="null",
                extra={
                    "column": stu["column"],
                    "value": None,
                    "standard_sql": std["sql"],
                    "student_sql": stu["sql"],
                }
            ))

    # BUG-1 fix: detect predicates the student added that the standard doesn't have.
    for idx, stu in enumerate(stu_comparisons):
        if idx not in stu_matched:
            diffs.append(ASTDiffNode(
                clause_category="PREDICATE",
                diff_type="predicate_added",
                target_column=stu["column"],
                standard_node=None,
                student_node=stu.get("node"),
                knowledge_point_id="where",
                extra={
                    **stu,
                    "standard_sql": "",
                    "student_sql": stu["sql"],
                    "standard_query_sql": _sql_of(standard_ast),
                    "student_query_sql": _sql_of(student_ast),
                }
            ))

    return diffs


def _join_ast_diffs(standard_ast: exp.Expression, student_ast: exp.Expression) -> list[ASTDiffNode]:
    std_graph = _extract_join_graph(standard_ast)
    stu_graph = _extract_join_graph(student_ast)

    diffs: list[ASTDiffNode] = []

    # Same normalised graph → no real JOIN difference (implicit ≡ explicit)
    std_signature = {
        "joins": sorted((table, side) for table, side, _ in std_graph["joins"]),
        "conditions": std_graph["conditions"],
        "from_tables": std_graph["from_tables"],
    }
    stu_signature = {
        "joins": sorted((table, side) for table, side, _ in stu_graph["joins"]),
        "conditions": stu_graph["conditions"],
        "from_tables": stu_graph["from_tables"],
    }
    if std_signature == stu_signature:
        return []

    # ── Table-set mismatch ──
    std_tables = {t for t, _, _ in std_graph["joins"]}
    stu_tables = {t for t, _, _ in stu_graph["joins"]}
    for table in std_tables - stu_tables:
        std_join_node = next((n for t, _, n in std_graph["joins"] if t == table), None)
        diffs.append(ASTDiffNode(
            clause_category="JOIN",
            diff_type="join_missing",
            target_table=table,
            standard_node=std_join_node,
            student_node=None,
            knowledge_point_id="join-inner",
            extra={"standard_sql": _sql_of(std_join_node) if std_join_node else "", "student_sql": ""},
        ))

    # ── Per-join comparison (matched by right-table name) ──
    stu_by_table: dict[str, tuple[str, Any]] = {}
    for t, s, n in stu_graph["joins"]:
        stu_by_table[t] = (s, n)
    for std_table, std_side, std_node in std_graph["joins"]:
        if std_table not in stu_by_table:
            continue
        stu_side, stu_node = stu_by_table[std_table]
        if std_side != stu_side:
            kp = "join-left" if std_side == "LEFT" else "join-inner"
            diffs.append(ASTDiffNode(
                clause_category="JOIN_TYPE",
                diff_type="join_type_changed",
                target_table=std_table,
                standard_node=std_node,
                student_node=stu_node,
                knowledge_point_id=kp,
                extra={
                    "standard_side": std_side,
                    "student_side": stu_side,
                    "right_table": std_table,
                    "standard_sql": _sql_of(std_node),
                    "student_sql": _sql_of(stu_node),
                },
            ))

    # ── ON-condition comparison ──
    std_conds = sorted(std_graph["conditions"])
    stu_conds = sorted(stu_graph["conditions"])
    if std_conds != stu_conds:
        paired = False
        student_by_table = {
            table: node
            for table, _side, node in stu_graph["joins"]
        }
        for table, _side, standard_join in std_graph["joins"]:
            student_join = student_by_table.get(table)
            standard_on = standard_join.args.get("on") if isinstance(standard_join, exp.Join) else None
            student_on = student_join.args.get("on") if isinstance(student_join, exp.Join) else None
            if (
                isinstance(standard_on, exp.Expression)
                and isinstance(student_on, exp.Expression)
                and _sql_of(standard_on) != _sql_of(student_on)
            ):
                diffs.append(ASTDiffNode(
                    clause_category="JOIN ON",
                    diff_type="join_on_changed",
                    standard_node=standard_on,
                    student_node=student_on,
                    knowledge_point_id="join-on",
                    extra={
                        "standard_sql": _sql_of(standard_on),
                        "student_sql": _sql_of(student_on),
                    },
                ))
                paired = True
                break
        if not paired:
            std_set = set(std_conds)
            stu_set = set(stu_conds)
            missing = std_set - stu_set
            added = stu_set - std_set
            diffs.append(ASTDiffNode(
                clause_category="JOIN ON",
                diff_type="join_on_changed",
                standard_node=None,
                student_node=None,
                knowledge_point_id="join-on",
                extra={
                    "standard_sql": min(missing, default=""),
                    "student_sql": min(added, default=""),
                },
            ))

    # Carry the actual connection endpoints into every JOIN obligation. The
    # semantic validator must not later infer a join from arbitrary same-name
    # columns in an unrelated table pair.
    standard_pairs = _join_on_column_pairs(_sql_of(standard_ast))
    student_pairs = _join_on_column_pairs(_sql_of(student_ast))
    for diff in diffs:
        if diff.diff_type not in {"join_missing", "join_type_changed", "join_on_changed"}:
            continue
        if standard_pairs:
            diff.extra["standard_join_pairs"] = standard_pairs
        if student_pairs:
            diff.extra["student_join_pairs"] = student_pairs

    return diffs


def _cte_ast_diffs(standard_ast: exp.Expression, student_ast: exp.Expression) -> list[ASTDiffNode]:
    std_recursive = _is_recursive_ast(standard_ast)
    stu_recursive = _is_recursive_ast(student_ast)

    # Extract CTE definitions as sorted SQL strings for structural comparison.
    std_ctes = sorted(_sql_of(node) for node in standard_ast.find_all(exp.CTE))
    stu_ctes = sorted(_sql_of(node) for node in student_ast.find_all(exp.CTE))

    # Recursive CTE: report if recursive flag changed, or CTE bodies differ.
    if std_recursive and (std_recursive != stu_recursive or std_ctes != stu_ctes):
        return [ASTDiffNode(
            clause_category="CTE_RECURSIVE",
            diff_type="recursive_cte_changed",
            standard_node=standard_ast.find(exp.With) or standard_ast,
            student_node=student_ast.find(exp.With) or student_ast,
            knowledge_point_id="cte-recursive",
            extra={
                "standard_sql": " | ".join(std_ctes),
                "student_sql": " | ".join(stu_ctes),
                "standard_recursive": std_recursive,
                "student_recursive": stu_recursive,
            }
        )]

    # Non-recursive CTE: retain the summary, but also compile direct query
    # block clauses. A DISTINCT/WHERE/GROUP change inside a CTE must reach its
    # own obligation instead of being hidden behind a generic ``cte_changed``.
    if std_ctes or stu_ctes:
        if std_ctes != stu_ctes:
            diffs = [ASTDiffNode(
                clause_category="CTE",
                diff_type="cte_changed",
                standard_node=standard_ast.find(exp.CTE) or standard_ast,
                student_node=student_ast.find(exp.CTE) or student_ast,
                knowledge_point_id="cte",
                extra={
                    "standard_sql": " | ".join(std_ctes),
                    "student_sql": " | ".join(stu_ctes),
                }
            )]
            standard_by_name = {
                _norm_name(node.alias or ""): node
                for node in standard_ast.find_all(exp.CTE)
                if node.alias
            }
            student_by_name = {
                _norm_name(node.alias or ""): node
                for node in student_ast.find_all(exp.CTE)
                if node.alias
            }
            for name in sorted(set(standard_by_name) & set(student_by_name)):
                standard_body = standard_by_name[name].this
                student_body = student_by_name[name].this
                if (
                    not isinstance(standard_body, exp.Query)
                    or not isinstance(student_body, exp.Query)
                    or _sql_of(standard_body) == _sql_of(student_body)
                ):
                    continue
                for diff in _clause_ast_diffs(standard_body, student_body):
                    diff.extra.update({
                        "query_scope": f"cte:{name}",
                        "query_block_depth": 1,
                        "cte_name": name,
                    })
                    source = _direct_from_table(_top_select(standard_body))
                    if diff.target_table is None and isinstance(source, exp.Table):
                        diff.target_table = source.name
                    if diff.diff_type == "distinct_changed":
                        body_select = _top_select(standard_body)
                        projection_columns = tuple(dict.fromkeys(
                            _norm_name(column.name)
                            for expression in (
                                body_select.expressions
                                if isinstance(body_select, exp.Select)
                                else ()
                            )
                            for column in expression.find_all(exp.Column)
                            if _nearest_select(column) is body_select
                        ))
                        diff.extra["standard_projection_columns"] = projection_columns
                        diff.extra["standard_query_sql"] = _sql_of(standard_body)
                        diff.extra["student_query_sql"] = _sql_of(student_body)
                    diffs.append(diff)
            return diffs
    return []


def _is_recursive_ast(ast: exp.Expression | None) -> bool:
    if ast is None:
        return False
    with_node = ast.args.get("with") or ast.args.get("with_") or ast.find(exp.With)
    if with_node is not None and bool(with_node.args.get("recursive")):
        return True
    for cte in ast.find_all(exp.CTE):
        cte_name = _norm_name(cte.alias or "")
        if cte_name and any(
            _norm_name(table.name) == cte_name
            for table in cte.this.find_all(exp.Table)
        ):
            return True
    try:
        return "WITH RECURSIVE" in ast.sql(dialect="sqlite").upper()
    except Exception:
        return False


def _constraints_from_ast_diffs(ast_diffs: list[ASTDiffNode]) -> list[dict[str, Any]]:
    constraints: list[dict[str, Any]] = []
    for diff in ast_diffs:
        column = diff.target_column
        if not column:
            continue
        value = diff.get("value")
        student_value = diff.get("student_value")
        if diff.diff_type == "comparison_operator_changed" and isinstance(
            diff.standard_node,
            (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE),
        ):
            comparison = diff.standard_node
            if isinstance(comparison.left, exp.Column) and _norm_name(comparison.left.name) == _norm_name(column):
                value = _expression_static_value(comparison.right)
            elif isinstance(comparison.right, exp.Column) and _norm_name(comparison.right.name) == _norm_name(column):
                value = _expression_static_value(comparison.left)
            else:
                value = None
            student_value = value
        if diff.diff_type == "null_equality_changed":
            constraints.append({"column": column, "op": "IS", "value": None, "source": "ast_diff"})
        elif isinstance(value, (int, float, Decimal)):
            constraints.append({"column": column, "op": diff.get("standard_op") or "DIFF", "value": value, "source": "ast_diff"})
        elif value is not None:
            constraints.append({"column": column, "op": diff.get("standard_op") or "DIFF", "value": value, "source": "ast_diff"})
        if student_value is not None:
            constraints.append({"column": column, "op": diff.get("student_op") or "DIFF", "value": student_value, "source": "ast_diff"})
    return constraints


def _extract_table_names(sql: str) -> set[str]:
    return {_norm_name(table) for table in extract_physical_table_names(sql)}


def _column_lookup(columns: list[str]) -> dict[str, str]:
    return {_norm_name(col): col for col in columns}


def _is_from_table_of_missing_join(
    table: str,
    standard_sql: str,
    ast_diffs: list[ASTDiffNode] | None = None,
) -> bool:
    """Return True if *table* is the FROM (left-side) table of a JOIN the student dropped.

    When a JOIN is missing, the FROM table needs a dangling row (no match in the
    dropped table) so that the standard's INNER JOIN filters it out while the
    student's query (without the JOIN) returns it.
    """
    if not ast_diffs or not any(d.diff_type == "join_missing" for d in ast_diffs):
        return False
    ast = _parse_sql(standard_sql)
    if ast is None:
        return False
    from_clause = ast.args.get("from_") or ast.args.get("from")
    if isinstance(from_clause, exp.From):
        child = from_clause.this
        if isinstance(child, exp.Table) and _norm_name(child.name) == _norm_name(table):
            return True
        if isinstance(child, exp.Subquery) and child.alias and _norm_name(child.alias) == _norm_name(table):
            return True
    return False


def _right_tables_for_left_joins(*sqls: str, ast_diffs: list[ASTDiffNode] | None = None) -> set[str]:
    right_tables: set[str] = set()
    for diff in ast_diffs or []:
        if diff.diff_type == "join_type_changed" and diff.target_table:
            right_tables.add(_norm_name(str(diff.target_table)))
    for sql in sqls:
        ast = _parse_sql(sql)
        if not ast:
            continue
        for join in ast.find_all(exp.Join):
            side = str(join.args.get("side") or "").upper()
            if side != "LEFT":
                continue
            table = join.this
            if isinstance(table, exp.Table):
                right_tables.add(_norm_name(table.name))
            elif table is not None:
                nested = table.find(exp.Table)
                if isinstance(nested, exp.Table):
                    right_tables.add(_norm_name(nested.name))
    return right_tables


def _apply_constraints(rows: list[dict[str, Any]], columns: list[str], constraints: list[dict[str, Any]],
                       target_tables: dict[str, list[str]] | None = None) -> None:
    """
    根据提取的语法约束，将特定值写入数据行中的对应列，并生成对抗性反例值（Counter-Value）。
    Applies extracted predicate constraints to columns by setting values in database rows
    and generating counter-values in the last row to expose logic errors.

    策略解析 (Strategy details):
    1. 分组：将约束按目标列分类。
    2. 阳性测试数据 (Positive Cases)：在前一半的数据行中，循环填入该谓词约束中出现的字面量值（如 18, 'Alice' 等），确保有符合条件的行。
    3. 阴性测试数据 / 对抗反例 (Negative Cases/Counter-Values)：在最后一行注入对抗反例（_counter_value，如 18+999 = 1017, 'not_Alice' 等）。
       如果学生逻辑有漏洞（例如无条件选择、或操作符写反），反例行的数据会暴露此错误。
    """
    # 按列对约束进行聚合分组
    by_col: dict[str, list[dict[str, Any]]] = {}
    column_lookup = _column_lookup(columns)
    for constraint in constraints:
        # Skip constraints qualified to a different table (multi-table guard)
        c_table = constraint.get("table")
        if c_table and target_tables:
            norm_table = _norm_name(str(c_table))
            found_in_other = False
            for other_table, other_cols in target_tables.items():
                if _norm_name(other_table) == norm_table:
                    continue
                if _norm_name(str(constraint.get("column"))) in {
                    _norm_name(c) for c in other_cols
                }:
                    found_in_other = True
                    break
            if found_in_other:
                continue
        col = column_lookup.get(_norm_name(str(constraint.get("column"))))
        if col:
            by_col.setdefault(col, []).append(constraint)

    # 逐列应用数值和文本边界值
    positive_anchor: dict[str, Any] = {}
    counter_values: dict[str, Any] = {}
    null_col_count = 0
    for col, items in by_col.items():
        values: list[Any] = []
        for item in items:
            if item.get("op") == "IN":
                values.extend(item.get("values") or [])
            else:
                value = item.get("value")
                if isinstance(value, (int, float, Decimal)):
                    values.extend([value, value + 1, value - 1])
                else:
                    values.append(value)
        values = [v for v in values if v is not None]
        if values:
            positive_anchor[col] = _positive_probe_value(items[0])

        # 如果列约束是 IS NULL / IS NOT NULL，设置特定行为 None，其余非空
        if not values:
            is_null_constraint = any(item.get("op") == "IS_NULL" for item in items)
            is_not_null_constraint = any(item.get("op") == "IS_NOT_NULL" for item in items)
            if rows:
                if is_null_constraint:
                    # IS NULL: 一行设为 None（正例），其余确保非 NULL（反例）
                    null_row_idx = null_col_count % len(rows)
                    rows[null_row_idx][col] = None
                    for i, row in enumerate(rows):
                        if i != null_row_idx and row.get(col) is None:
                            row[col] = _seed_value(col, i)
                    null_col_count += 1
                elif is_not_null_constraint:
                    # IS NOT NULL: 一行设为 None（反例），其余确保非 NULL（正例）
                    null_row_idx = null_col_count % len(rows)
                    rows[null_row_idx][col] = None
                    for i, row in enumerate(rows):
                        if i != null_row_idx:
                            row[col] = _seed_value(col, i)
                    null_col_count += 1
                else:
                    # 其他无值约束（如空 IN 列表）
                    target_row_idx = null_col_count % len(rows)
                    rows[target_row_idx][col] = None
                    null_col_count += 1
            continue

        # 阳性覆盖：将谓词值分布在前一半数据行中
        for idx, value in enumerate(values[: max(1, len(rows) // 2)]):
            rows[idx % len(rows)][col] = value
        counter_values[col] = _counter_probe_value(items[0])

    # 为复合谓词分配独立反例行，避免某一列的边界值把其它条件同时滤掉
    if rows and counter_values:
        probe_rows = list(range(max(0, len(rows) - len(counter_values)), len(rows))) or [len(rows) - 1]
        ordered_cols = list(counter_values.keys())
        for idx, col in enumerate(ordered_cols):
            row_idx = probe_rows[idx % len(probe_rows)]
            row = rows[row_idx]
            for other_col in ordered_cols:
                if other_col == col:
                    row[other_col] = counter_values[other_col]
                elif other_col in positive_anchor:
                    row[other_col] = positive_anchor[other_col]


def _affine_self_join_term(
    node: exp.Expression | None,
) -> tuple[str, str, int | float] | None:
    """Return ``alias.column + offset`` for a bounded self-join expression."""
    if isinstance(node, exp.Paren):
        return _affine_self_join_term(node.this)
    if isinstance(node, exp.Column) and node.table:
        return _norm_name(node.table), _norm_name(node.name), 0
    if not isinstance(node, (exp.Add, exp.Sub)):
        return None
    left = _affine_self_join_term(node.left)
    right_value = _literal_value(node.right)
    if (
        left is not None
        and isinstance(right_value, (int, float))
        and not isinstance(right_value, bool)
    ):
        offset = left[2] + right_value if isinstance(node, exp.Add) else left[2] - right_value
        return left[0], left[1], offset
    if isinstance(node, exp.Add):
        right = _affine_self_join_term(node.right)
        left_value = _literal_value(node.left)
        if (
            right is not None
            and isinstance(left_value, (int, float))
            and not isinstance(left_value, bool)
        ):
            return right[0], right[1], right[2] + left_value
    return None


def _apply_distinct_self_join_path_probe(
    data: dict[str, list[dict[str, Any]]],
    standard_sql: str,
    student_sql: str,
) -> None:
    """Materialize two equal projections through a simple affine self join."""
    parsed = next(
        (
            ast
            for sql in (standard_sql, student_sql)
            if (ast := _parse_sql(sql))
            and isinstance(_top_select(ast), exp.Select)
            and _top_select(ast).args.get("distinct")
        ),
        None,
    )
    select = _top_select(parsed) if parsed else None
    if not isinstance(select, exp.Select):
        return
    from_clause = select.args.get("from_")
    if not isinstance(from_clause, exp.From) or not isinstance(from_clause.this, exp.Table):
        return
    source_tables = [from_clause.this]
    joins = list(select.args.get("joins") or [])
    if len(joins) < 2 or any(not isinstance(join.this, exp.Table) for join in joins):
        return
    source_tables.extend(join.this for join in joins)
    aliases = {
        _norm_name(table.alias_or_name): _norm_name(table.name)
        for table in source_tables
    }
    physical_tables = set(aliases.values())
    if len(aliases) < 3 or len(physical_tables) != 1:
        return

    projections: list[exp.Column] = []
    for item in select.expressions or ():
        expression = item.this if isinstance(item, exp.Alias) else item
        if not isinstance(expression, exp.Column):
            return
        projections.append(expression)
    if not projections:
        return
    anchor_alias = _norm_name(projections[0].table or source_tables[0].alias_or_name)
    if anchor_alias not in aliases:
        return

    graphs: dict[str, dict[str, list[tuple[str, int | float]]]] = {}
    for join in joins:
        on = join.args.get("on")
        if not isinstance(on, exp.Expression):
            continue
        equalities = list(on.find_all(exp.EQ))
        if isinstance(on, exp.EQ):
            equalities.insert(0, on)
        for equality in equalities:
            left = _affine_self_join_term(equality.left)
            right = _affine_self_join_term(equality.right)
            if left is None or right is None or left[1] != right[1] or left[0] == right[0]:
                continue
            delta = left[2] - right[2]
            graph = graphs.setdefault(left[1], {})
            graph.setdefault(left[0], []).append((right[0], delta))
            graph.setdefault(right[0], []).append((left[0], -delta))

    offsets: dict[str, int | float] = {}
    key_column = ""
    for column, graph in graphs.items():
        candidate_offsets: dict[str, int | float] = {anchor_alias: 0}
        pending = [anchor_alias]
        consistent = True
        while pending and consistent:
            current = pending.pop(0)
            for neighbor, delta in graph.get(current, []):
                candidate = candidate_offsets[current] + delta
                if neighbor in candidate_offsets and candidate_offsets[neighbor] != candidate:
                    consistent = False
                    break
                if neighbor not in candidate_offsets:
                    candidate_offsets[neighbor] = candidate
                    pending.append(neighbor)
        if (
            consistent
            and set(aliases).issubset(candidate_offsets)
            and len(set(candidate_offsets.values())) > 1
        ):
            key_column = column
            offsets = candidate_offsets
            break
    if not key_column:
        return
    if any(_norm_name(column.name) == key_column for column in projections):
        return

    physical_table = next(iter(physical_tables))
    table_entry = next(
        ((name, rows) for name, rows in data.items() if _norm_name(name) == physical_table),
        None,
    )
    if table_entry is None:
        return
    _, rows = table_entry
    if not rows:
        return
    lookup = _column_lookup(list(rows[0]))
    actual_key = lookup.get(key_column)
    projected_columns = {
        lookup.get(_norm_name(column.name))
        for column in projections
    }
    projected_columns.discard(None)
    if not actual_key or not projected_columns or actual_key in projected_columns:
        return

    path_values = sorted(
        {start + offset for start in (0, 1) for offset in offsets.values()}
    )
    if len(path_values) > len(rows):
        return
    base = 1000 - min(path_values)
    for index, path_value in enumerate(path_values):
        rows[index][actual_key] = base + path_value
        for column in projected_columns:
            current = rows[index].get(column)
            rows[index][column] = 777 if isinstance(current, (int, float)) else "__distinct_self_join__"






def _aggregate_distinct_target_column(diff: ASTDiffNode) -> str:
    target = str(diff.target_column or "").strip()
    if target:
        return _norm_name(target)
    argument = re.sub(
        r"^\s*DISTINCT\s+",
        "",
        str(diff.extra.get("standard_aggregate_argument") or ""),
        flags=re.IGNORECASE,
    )
    match = re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", argument.strip())
    return _norm_name(match.group(0)) if match else ""


def _apply_grouped_distinct_probe(
    data: dict[str, list[dict[str, Any]]],
    standard_sql: str,
    student_sql: str,
) -> None:
    ast = next(
        (
            parsed for sql in (standard_sql, student_sql)
            if (parsed := _parse_sql(sql))
            and isinstance(_top_select(parsed), exp.Select)
            and isinstance(_top_select(parsed).args.get("group"), exp.Group)
            and any(
                _nearest_select(agg) is _top_select(parsed)
                and (agg.args.get("distinct") or isinstance(agg.this, exp.Distinct))
                for agg in _top_select(parsed).find_all(exp.AggFunc)
            )
        ),
        None,
    )
    select = _top_select(ast) if ast else None
    if not isinstance(select, exp.Select):
        return
    projection = select.expressions[0] if select.expressions else None
    projection = projection.this if isinstance(projection, exp.Alias) else projection
    group = select.args.get("group")
    group_columns = [item for item in group.expressions if isinstance(item, exp.Column)] if isinstance(group, exp.Group) else []
    distinct_agg = next(
        (
            agg for agg in select.find_all(exp.AggFunc)
            if _nearest_select(agg) is select
            and (agg.args.get("distinct") or isinstance(agg.this, exp.Distinct))
        ),
        None,
    )
    aggregate_column = distinct_agg.find(exp.Column) if distinct_agg else None
    if not isinstance(projection, exp.Column) or not group_columns:
        return
    for rows in data.values():
        if len(rows) < 4:
            continue
        lookup = _column_lookup(list(rows[0]))
        projected_col = lookup.get(_norm_name(projection.name))
        aggregate_col = lookup.get(_norm_name(aggregate_column.name)) if isinstance(aggregate_column, exp.Column) else None
        if aggregate_col:
            group_col = next(
                (
                    lookup.get(_norm_name(column.name))
                    for column in group_columns
                    if _norm_name(column.name) != _norm_name(aggregate_col)
                ),
                None,
            )
            if group_col:
                group_value = 901 if _is_numeric_column(group_col) else "__distinct_count_group__"
                repeated_value = 777 if _is_numeric_column(aggregate_col) else "__distinct_count_value__"
                other_value = 778 if _is_numeric_column(aggregate_col) else "__distinct_count_other__"
                rows[0][group_col] = group_value
                rows[1][group_col] = group_value
                rows[2][group_col] = group_value
                rows[0][aggregate_col] = repeated_value
                rows[1][aggregate_col] = repeated_value
                rows[2][aggregate_col] = other_value
                return
        split_col = next(
            (
                lookup.get(_norm_name(column.name))
                for column in group_columns
                if _norm_name(column.name) != _norm_name(projection.name)
            ),
            None,
        )
        if not projected_col or not split_col:
            continue
        for index, row in enumerate(rows[:4]):
            row[projected_col] = 901
            row[split_col] = "__distinct_group_a__" if index < 2 else "__distinct_group_b__"
            if aggregate_col and aggregate_col not in {projected_col, split_col}:
                row[aggregate_col] = 100 + index
        return


def _apply_select_distinct_group_probe(
    data: dict[str, list[dict[str, Any]]],
    standard_sql: str,
    student_sql: str,
) -> None:
    """Keep a projected value repeated across distinct GROUP BY keys."""

    parsed = next(
        (
            ast
            for sql in (standard_sql, student_sql)
            if (ast := _parse_sql(sql))
            and isinstance(_top_select(ast), exp.Select)
            and _top_select(ast).args.get("distinct")
        ),
        None,
    )
    select = _top_select(parsed) if parsed else None
    if not isinstance(select, exp.Select) or not select.args.get("distinct"):
        return
    group = select.args.get("group")
    if not isinstance(group, exp.Group):
        return
    projection = select.expressions[0] if select.expressions else None
    projection = projection.this if isinstance(projection, exp.Alias) else projection
    if not isinstance(projection, exp.Column):
        return
    group_columns = [item for item in group.expressions if isinstance(item, exp.Column)]
    split_column = next(
        (column for column in group_columns if _norm_name(column.name) != _norm_name(projection.name)),
        None,
    )
    if split_column is None:
        return
    for rows in data.values():
        if len(rows) < 2:
            continue
        lookup = _column_lookup(list(rows[0]))
        projected = lookup.get(_norm_name(projection.name))
        split = lookup.get(_norm_name(split_column.name))
        if not projected or not split:
            continue

        count_requirement = _distinct_having_count_requirement(select)
        if count_requirement is not None:
            aggregate_column, rows_per_group = count_requirement
            aggregate = lookup.get(_norm_name(aggregate_column))
            actual_group_columns = [
                lookup.get(_norm_name(column.name))
                for column in group_columns
            ]
            required_rows = rows_per_group * 2
            if (
                not aggregate
                or any(column is None for column in actual_group_columns)
                or aggregate in actual_group_columns
                or required_rows > len(rows)
            ):
                continue
            repeated = _group_probe_value(projected, 0, 0)
            for index, row in enumerate(rows[:required_rows]):
                group_index = index // rows_per_group
                row[projected] = repeated
                for position, group_column in enumerate(actual_group_columns):
                    if group_column == projected:
                        row[group_column] = repeated
                    else:
                        row[group_column] = _group_probe_value(
                            group_column,
                            group_index,
                            position + 1,
                        )
                row[aggregate] = (
                    700000 + index
                    if _is_numeric_column(aggregate)
                    else f"__distinct_having_{group_index}_{index % rows_per_group}__"
                )
            return

        repeated = _group_probe_value(projected, 0, 0)
        rows[0][projected] = repeated
        rows[1][projected] = repeated
        rows[0][split] = _group_probe_value(split, 0, 1)
        rows[1][split] = _group_probe_value(split, 1, 1)
        return


def _apply_distinct_cte_case_sum_probe(
    data: dict[str, list[dict[str, Any]]],
    standard_sql: str,
    student_sql: str,
) -> None:
    """Expose a bounded DISTINCT tuple duplicate consumed by CASE/SUM."""

    parsed = next(
        (
            ast
            for sql in (standard_sql, student_sql)
            if (ast := _parse_sql(sql))
            and any(
                isinstance(cte.this, exp.Select) and cte.this.args.get("distinct")
                for cte in ast.find_all(exp.CTE)
            )
        ),
        None,
    )
    if parsed is None:
        return
    for cte in parsed.find_all(exp.CTE):
        cte_select = cte.this
        if not isinstance(cte_select, exp.Select) or not cte_select.args.get("distinct"):
            continue
        cte_name = _norm_name(cte.alias or "")
        source = _direct_from_table(cte_select)
        case_projection = next(
            (
                item
                for item in cte_select.expressions or ()
                if isinstance(item, exp.Alias) and isinstance(item.this, exp.Case)
            ),
            None,
        )
        if not cte_name or not isinstance(source, exp.Table) or case_projection is None:
            continue
        case_alias = _norm_name(case_projection.alias)
        positive_column = ""
        positive_values: list[Any] = []
        contribution: int | float | Decimal | None = None
        for branch in case_projection.this.args.get("ifs") or ():
            if not isinstance(branch, exp.If):
                continue
            branch_value = _literal_value(branch.args.get("true"))
            predicate = _unwrap_paren(branch.this)
            if (
                not isinstance(branch_value, (int, float, Decimal))
                or isinstance(branch_value, bool)
                or not isinstance(predicate, exp.In)
                or not isinstance(predicate.this, exp.Column)
            ):
                continue
            values = [
                _literal_value(item)
                for item in predicate.expressions or ()
                if isinstance(item, exp.Literal)
            ]
            values = [item for item in values if item is not None]
            if len(values) >= 2:
                positive_column = predicate.this.name
                positive_values = values[:2]
                contribution = branch_value
                break
        if not positive_column or contribution is None:
            continue

        downstream: exp.Select | None = None
        group_key: exp.Column | None = None
        for candidate in parsed.find_all(exp.Select):
            if candidate is cte_select:
                continue
            candidate_source = _direct_from_table(candidate)
            having = candidate.args.get("having")
            group = candidate.args.get("group")
            if (
                not isinstance(candidate_source, exp.Table)
                or _norm_name(candidate_source.name) != cte_name
                or not isinstance(having, exp.Having)
                or not isinstance(group, exp.Group)
            ):
                continue
            equality = next(
                (
                    node
                    for node in having.find_all(exp.EQ)
                    if isinstance(node.left, exp.Sum)
                    and isinstance(node.right, exp.Literal)
                    and isinstance(node.left.find(exp.Column), exp.Column)
                    and _norm_name(node.left.find(exp.Column).name) == case_alias
                    and _literal_value(node.right) == contribution * 2
                ),
                None,
            )
            key = next(
                (item for item in group.expressions or () if isinstance(item, exp.Column)),
                None,
            )
            if equality is not None and isinstance(key, exp.Column):
                downstream = candidate
                group_key = key
                break
        if downstream is None or group_key is None:
            continue

        source_table = next(
            (name for name in data if _norm_name(name) == _norm_name(source.name)),
            None,
        )
        source_rows = data.get(source_table or "")
        if not source_rows or len(source_rows) < 3:
            continue
        source_lookup = _column_lookup(list(source_rows[0]))
        source_key = source_lookup.get(_norm_name(group_key.name))
        source_value = source_lookup.get(_norm_name(positive_column))
        if not source_key or not source_value:
            continue
        witness_key = 8801 if _is_numeric_column(source_key) else "__distinct_cte_key__"
        for index, value in enumerate(
            (positive_values[0], positive_values[1], positive_values[1])
        ):
            source_rows[index][source_key] = witness_key
            source_rows[index][source_value] = value

        membership = next(
            (
                node
                for node in parsed.find_all(exp.In)
                if isinstance(node.this, exp.Column)
                and isinstance(node.args.get("query"), exp.Subquery)
                and node.args["query"].this is downstream
            ),
            None,
        )
        outer_select = _nearest_select(membership) if isinstance(membership, exp.In) else None
        outer_source = _direct_from_table(outer_select)
        if isinstance(outer_source, exp.Table) and isinstance(membership, exp.In):
            outer_table = next(
                (name for name in data if _norm_name(name) == _norm_name(outer_source.name)),
                None,
            )
            outer_rows = data.get(outer_table or "")
            if outer_rows:
                outer_key = _column_lookup(list(outer_rows[0])).get(
                    _norm_name(membership.this.name)
                )
                if outer_key:
                    outer_rows[0][outer_key] = witness_key
        return


def _apply_join_on_counterexample(
    data: dict[str, list[dict[str, Any]]],
    standard_sql: str,
    student_sql: str,
    ast_diffs: list[dict[str, Any]],
) -> None:
    if not _has_diff(ast_diffs, "JOIN ON"):
        return
    standard_pairs = _join_on_column_pairs(standard_sql)
    student_pairs = _join_on_column_pairs(student_sql)
    if not standard_pairs:
        return
    if standard_pairs == student_pairs:
        return

    max_len = max((len(rows) for rows in data.values()), default=0)
    if max_len <= 0:
        return

    assignments = _join_on_standard_assignments(standard_pairs, max_len)
    for ref, values in assignments.items():
        _set_column_ref_values(data, ref, values)

    standard_refs = {ref for pair in standard_pairs for ref in pair}
    student_refs = {ref for pair in student_pairs for ref in pair}
    for offset, ref in enumerate(sorted(student_refs - standard_refs), 1):
        drift_values = [9000 + offset * 100 + idx for idx in range(max_len)]
        base_ref = next((candidate for candidate in standard_refs if candidate in assignments), None)
        base_values = assignments.get(base_ref) if base_ref is not None else None
        if base_values:
            mixed_values = [
                base_values[idx] if idx % 2 == 0 else drift_values[idx]
                for idx in range(max_len)
            ]
            _set_column_ref_values(data, ref, mixed_values)
        else:
            _set_column_ref_values(data, ref, drift_values)


def _join_on_column_pairs(sql: str) -> list[tuple[tuple[str, str], tuple[str, str]]]:
    ast = _parse_sql(sql)
    if not ast:
        return []
    aliases = _table_aliases(ast)
    pairs: list[tuple[tuple[str, str], tuple[str, str]]] = []

    def add_pair(eq_node: exp.EQ) -> None:
        left = eq_node.left
        right = eq_node.right
        if not isinstance(left, exp.Column) or not isinstance(right, exp.Column):
            return
        left_ref = _column_ref(left, aliases)
        right_ref = _column_ref(right, aliases)
        left_alias = _norm_name(left.table or "")
        right_alias = _norm_name(right.table or "")
        cross_relation = left_ref and right_ref and (
            left_ref[0] != right_ref[0] or left_alias != right_alias
        )
        if cross_relation:
            pair = (left_ref, right_ref)
            if pair not in pairs and (right_ref, left_ref) not in pairs:
                pairs.append(pair)

    for join in ast.find_all(exp.Join):
        on_node = join.args.get("on")
        if on_node is None:
            continue
        eq_nodes = [on_node] if isinstance(on_node, exp.EQ) else list(on_node.find_all(exp.EQ))
        for eq_node in eq_nodes:
            add_pair(eq_node)
    for where in ast.find_all(exp.Where):
        for eq_node in where.find_all(exp.EQ):
            add_pair(eq_node)
    return pairs


def _table_aliases(ast: exp.Expression) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for table in ast.find_all(exp.Table):
        name = _norm_name(table.name)
        if name:
            aliases[name] = name
        alias = table.alias
        if alias:
            aliases[_norm_name(alias)] = name
    return aliases


def _column_ref(column: exp.Column, aliases: dict[str, str]) -> tuple[str, str] | None:
    table = _norm_name(column.table or "")
    resolved_table = aliases.get(table, table)
    if not resolved_table:
        return None
    return resolved_table, _norm_name(column.name)


def _set_column_ref_values(
    data: dict[str, list[dict[str, Any]]],
    ref: tuple[str, str],
    values: list[Any],
) -> None:
    table_name, column_name = ref
    rows = next((rows for table, rows in data.items() if _norm_name(table) == table_name), None)
    if not rows:
        return
    column = next((col for col in rows[0] if _norm_name(col) == column_name), None)
    if column is None:
        return
    for idx, row in enumerate(rows):
        row[column] = values[idx % len(values)]


def _apply_dangling_tuple_probe(
    rows: list[dict[str, Any]],
    columns: list[str],
    table_name: str,
    standard_sql: str,
    student_sql: str,
) -> None:
    if not rows:
        return
    join_cols = set()
    for sql in (standard_sql, student_sql):
        for left, right in _join_on_column_pairs(sql):
            if left[0] == _norm_name(table_name):
                join_cols.add(left[1])
            if right[0] == _norm_name(table_name):
                join_cols.add(right[1])

    lookup = _column_lookup(columns)
    target_cols = [lookup[col] for col in join_cols if col in lookup]

    dangling_count = 1
    asts = [_parse_sql(standard_sql), _parse_sql(student_sql)]
    has_anti_join_filter = any(
        ast
        and any(
            isinstance(node.this, exp.Column)
            and _norm_name(node.this.name) in join_cols
            and isinstance(node.expression, exp.Null)
            for node in ast.find_all(exp.Is)
        )
        for ast in asts
    )
    if has_anti_join_filter:
        limits = [_limit_offset_required_rows(sql) - 1 for sql in (standard_sql, student_sql)]
        dangling_count = max(1, max(limits, default=1))

    if target_cols:
        for col in target_cols:
            for offset, row in enumerate(rows[-dangling_count:]):
                row[col] = None if dangling_count == 1 else 900000 + offset
    else:
        key_cols = [col for col in columns if _is_key_column(col)] or columns[:1]
        for offset, row in enumerate(rows[-dangling_count:]):
            row[key_cols[0]] = None if dangling_count == 1 else 900000 + offset

    group_by_cols = _group_by_columns_for_sql(standard_sql) | _group_by_columns_for_sql(student_sql)
    if group_by_cols:
        lookup = _column_lookup(columns)
        for table_ref, col_ref in group_by_cols:
            if table_ref != _norm_name(table_name):
                continue
            actual_col = lookup.get(col_ref)
            if actual_col:
                rows[-1][actual_col] = f"__dangling_group__{table_name}_{actual_col}__"


def _apply_final_dangling_tuple_probes(
    data: dict[str, list[dict[str, Any]]],
    standard_sql: str,
    student_sql: str,
    ast_diffs: list[ASTDiffNode],
) -> None:
    right_tables = _right_tables_for_left_joins(
        standard_sql,
        student_sql,
        ast_diffs=ast_diffs,
    )
    for table_name, rows in data.items():
        if not rows:
            continue
        if (
            _norm_name(table_name) not in right_tables
            and not _is_from_table_of_missing_join(table_name, standard_sql, ast_diffs)
        ):
            continue
        _apply_dangling_tuple_probe(
            rows,
            list(rows[0]),
            table_name,
            standard_sql,
            student_sql,
        )


def _apply_having_aggregate_probes(
    rows: list[dict[str, Any]],
    columns: list[str],
    standard_sql: str,
    student_sql: str,
    ast_diffs: list[dict[str, Any]] | None = None,
) -> None:
    if ast_diffs is not None and not any(diff.get("clause") in {"HAVING", "PREDICATE", "AGGREGATE"} for diff in ast_diffs):
        return
    spec = _changed_having_aggregate_spec_for_diffs(
        standard_sql,
        student_sql,
        ast_diffs or [],
    )
    if not spec:
        return
    lookup = _column_lookup(columns)
    group_cols = list(dict.fromkeys(
        actual
        for column in spec.get("group_columns") or [spec["group_column"]]
        if (actual := lookup.get(_norm_name(column)))
    ))
    group_col = group_cols[0] if group_cols else None
    if spec["agg"] == "COUNT":
        if group_col:
            value_col = lookup.get(_norm_name(spec["column"]))
            _apply_count_group_probe(
                rows,
                group_col,
                int(spec["boundary"]),
                group_cols=group_cols,
                value_col=value_col,
                distinct=bool(spec.get("distinct")),
            )
            _apply_having_companion_probes(rows, columns, standard_sql, spec)
        return
    value_col = lookup.get(_norm_name(spec["column"]))
    if not value_col or not group_col:
        return
    companion_count = max(
        (
            int(candidate["boundary"])
            for candidate in _extract_having_aggregate_specs(standard_sql)
            if candidate["agg"] == "COUNT"
            and (
                candidate["agg"],
                candidate["column"],
                candidate["group_column"],
            )
            != (spec["agg"], spec["column"], spec["group_column"])
        ),
        default=0,
    )
    group_size = max(2, companion_count)
    for idx, row in enumerate(rows):
        group_index = idx // group_size + 1
        row[group_col] = group_index
        for position, column in enumerate(group_cols[1:], 1):
            row[column] = _group_probe_value(column, group_index, position)
    grouped: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(tuple(row.get(column) for column in group_cols), []).append(row)
    targets = [spec["boundary"] + 1, spec["boundary"], spec["boundary"] - 1]
    for group_rows, target in zip(grouped.values(), targets):
        if not group_rows:
            continue
        agg = spec["agg"]
        if agg == "SUM":
            share = target / max(1, len(group_rows))
            for row in group_rows:
                row[value_col] = share
        elif agg == "AVG":
            pattern = [target - 1, target + 1]
            for idx, row in enumerate(group_rows):
                row[value_col] = pattern[idx % len(pattern)]
        elif agg == "MIN":
            pattern = [target, target + 1]
            for idx, row in enumerate(group_rows):
                row[value_col] = pattern[idx % len(pattern)]
        elif agg == "MAX":
            pattern = [target, target - 1]
            for idx, row in enumerate(group_rows):
                row[value_col] = pattern[idx % len(pattern)]


def _apply_having_companion_probes(
    rows: list[dict[str, Any]],
    columns: list[str],
    standard_sql: str,
    changed_spec: dict[str, Any],
) -> None:
    lookup = _column_lookup(columns)
    for spec in _extract_having_aggregate_specs(standard_sql):
        identity = (spec["agg"], spec["column"], spec["group_column"])
        changed_identity = (
            changed_spec["agg"],
            changed_spec["column"],
            changed_spec["group_column"],
        )
        if identity == changed_identity:
            continue
        group_col = lookup.get(_norm_name(spec["group_column"]))
        value_col = lookup.get(_norm_name(spec["column"]))
        if not group_col:
            continue
        if spec["agg"] == "COUNT":
            grouped: dict[Any, list[dict[str, Any]]] = defaultdict(list)
            for row in rows:
                grouped[row.get(group_col)].append(row)
            for group_rows in grouped.values():
                if len(group_rows) < int(spec["boundary"]):
                    continue
                if value_col:
                    for index, row in enumerate(group_rows):
                        if spec.get("distinct"):
                            row[value_col] = (
                                f"2024-03-{(index % 28) + 1:02d}"
                                if _is_date_column(value_col)
                                else _group_probe_value(value_col, index, 40)
                            )
                        elif row.get(value_col) is None:
                            row[value_col] = _seed_value(value_col, index)
            continue
        if not value_col:
            continue
        boundary = spec["boundary"]
        operator = spec["operator"]
        target = boundary
        if operator == "GT":
            target = boundary + 1
        elif operator == "LT":
            target = boundary - 1
        grouped: dict[Any, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            grouped[row.get(group_col)].append(row)
        for group_rows in grouped.values():
            if spec["agg"] in {"AVG", "MIN"}:
                for row in group_rows:
                    row[value_col] = target
            elif spec["agg"] == "MAX":
                group_rows[0][value_col] = target
            elif spec["agg"] == "SUM":
                share = target / max(1, len(group_rows))
                for row in group_rows:
                    row[value_col] = share


def _apply_group_filter_positive_probe(
    data: dict[str, list[dict[str, Any]]],
    standard_sql: str,
    student_sql: str,
    ast_diffs: list[ASTDiffNode],
) -> None:
    if not any(diff.clause_category == "GROUP BY" for diff in ast_diffs):
        return
    ast = _parse_sql(standard_sql)
    if not ast:
        return
    aliases = _table_aliases(ast)
    for select in ast.find_all(exp.Select):
        where = select.args.get("where")
        having = select.args.get("having")
        group = select.args.get("group")
        source = _direct_from_table(select)
        if not isinstance(group, exp.Group) or not source or not (
            isinstance(where, exp.Where) or isinstance(having, exp.Having)
        ):
            continue
        table_name = aliases.get(_norm_name(source.alias_or_name), _norm_name(source.name))
        table_actual = next((name for name in data if _norm_name(name) == table_name), None)
        rows = data.get(table_actual or "")
        if not rows:
            continue
        lookup = _column_lookup(list(rows[0]))
        assignments: dict[str, Any] = {}
        assignment_items: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for constraint in _extract_literal_constraints(_sql_of(select)):
            actual = lookup.get(_norm_name(str(constraint.get("column") or "")))
            if actual:
                assignments[actual] = _positive_probe_value(constraint)
                assignment_items[actual].append(constraint)
        if not assignments:
            continue
        group_cols = {
            lookup.get(_norm_name(item.name))
            for item in group.expressions
            if isinstance(item, exp.Column)
        }
        for index, row in enumerate(rows[: min(4, len(rows))]):
            for column, value in assignments.items():
                if column in group_cols:
                    row[column] = _positive_group_filter_value(
                        column,
                        assignment_items.get(column, []),
                        value,
                        index,
                    )
                else:
                    row[column] = value


def _apply_same_table_having_membership_probe(
    data: dict[str, list[dict[str, Any]]],
    standard_sql: str,
    student_sql: str,
) -> None:
    spec = _changed_having_aggregate_spec(standard_sql, student_sql)
    if not spec or spec["agg"] != "COUNT":
        return
    for sql in (standard_sql, student_sql):
        ast = _parse_sql(sql)
        if not ast:
            continue
        for in_node in ast.find_all(exp.In):
            query = in_node.args.get("query")
            inner = query.this if isinstance(query, exp.Subquery) else None
            outer_select = _nearest_select(in_node)
            if not isinstance(inner, exp.Select) or not isinstance(outer_select, exp.Select):
                continue
            if not inner.args.get("having"):
                continue
            inner_source = _direct_from_table(inner)
            outer_source = _direct_from_table(outer_select)
            if not inner_source or not outer_source or _norm_name(inner_source.name) != _norm_name(outer_source.name):
                continue
            table_actual = next((name for name in data if _norm_name(name) == _norm_name(inner_source.name)), None)
            rows = data.get(table_actual or "")
            if not rows:
                continue
            lookup = _column_lookup(list(rows[0]))
            group_col = lookup.get(_norm_name(spec["group_column"]))
            outer_col = lookup.get(_norm_name(in_node.this.name)) if isinstance(in_node.this, exp.Column) else None
            if not group_col or not outer_col:
                continue
            boundary = max(1, int(spec["boundary"]))
            member_value = rows[0][outer_col]
            for index, row in enumerate(rows):
                if index < boundary:
                    row[group_col] = member_value
                else:
                    row[group_col] = f"__having_other_{index}__"
            return


def _group_by_columns_for_sql(sql: str) -> set[tuple[str, str]]:
    ast = _parse_sql(sql)
    if not ast:
        return set()
    group = ast.find(exp.Group)
    if not group:
        return set()
    aliases = _table_aliases(ast)
    out: set[tuple[str, str]] = set()
    for expr in group.expressions or []:
        if not isinstance(expr, exp.Column):
            continue
        table = _norm_name(expr.table or "")
        resolved = aliases.get(table, table)
        out.add((resolved, _norm_name(expr.name)))
    return out
