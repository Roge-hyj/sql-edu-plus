"""Scoped query-path and relational structure analysis helpers."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Any
from collections import defaultdict
import re
from sqlglot import exp
from core.ast_schema import ASTDiffNode
from core.witness_generation.schema_scope import SchemaCatalog
from core.witness_generation.obligations import DistinguishingObligation
from core.witness_generation.planner import write_owner

from core.phase1_foundation import (
    _AGG_FUNC_TYPES,
    _aggregate_function_discriminator_groups,
    _aggregate_probe_order_descending,
    _aggregate_probe_result,
    _alias_insensitive_sql,
    _ancestor_selects,
    _changed_having_aggregate_spec,
    _comparison_subquery_parts,
    _direct_from_table,
    _flatten_and,
    _is_inside_join,
    _is_like_negation_equivalence,
    _is_not_between_expansion,
    _limit_offset_required_rows,
    _limit_repr,
    _literal_value,
    _logical_connective_shape,
    _logical_tree_signature,
    _nearest_select,
    _parse_sql,
    _result_order_clause,
    _semantic_diff,
    _semantic_literal_value,
    _set_operator_kp,
    _set_operator_modifier,
    _set_operator_name,
    _set_operator_node,
    _sql_of,
    _top_select,
    _unqualified_sql,
    _unwrap_paren,
)

from core.phase1_sql_semantics import (
    _aggregate_group_probe_value,
    _catalog_has_unary_unique_key,
    _coerce_datetime,
    _comparison_matches,
    _comparison_truth_value,
    _counter_value,
    _expression_static_value,
    _extract_logical_skeleton,
    _group_probe_value,
    _is_key_column,
    _is_numeric_column,
    _norm_name,
    _rich_predicate_truth_value,
    _scalar_predicate_values,
    _seed_value,
    _subquery_predicate_context_sql,
    _temporal_value_for_comparison,
    _unique_key_value,
)

from core.phase1_constraints import (
    _boolean_projection_truth_test_diffs,
    _column_lookup,
    _group_by_columns_for_sql,
    _in_exists_rewrite,
    _is_recursive_ast,
    _join_on_column_pairs,
    _table_aliases,
)



def _materialize_not_in_reachable_path(
    data: dict[str, list[dict[str, Any]]],
    standard_sql: str,
    student_sql: str,
    ast_diffs: list[ASTDiffNode],
) -> bool:
    """Build one complete row path for a top-level ``NOT IN`` difference.

    A local membership witness is insufficient when the anti-membership
    predicate is the last term of a large outer conjunction.  This narrow
    materializer is intentionally limited to the common teaching shape where
    the standard query has ``NOT <outer-column> IN (SELECT <column> ...)``.
    It makes one outer row survive its direct join, every top-level EXISTS,
    and a scalar COUNT prerequisite, while keeping a second outer value in
    the membership overlap so the semantic validator remains meaningful.

    Only existing bounded rows are edited.  The pass is last in finalization
    so generic probes cannot undo the assembled path afterward.
    """
    boundary_diff = next(
        (
            diff
            for diff in ast_diffs
            if diff.diff_type == "in_predicate_negation_changed"
            and diff.extra.get("standard_membership_table")
        ),
        None,
    )
    if boundary_diff is None:
        return False
    standard_ast = _parse_sql(standard_sql)
    student_ast = _parse_sql(student_sql)
    if standard_ast is None or student_ast is None:
        return False
    standard_outer = _top_select(standard_ast)
    student_outer = _top_select(student_ast)
    if not isinstance(standard_outer, exp.Select) or not isinstance(student_outer, exp.Select):
        return False

    def select_body(node: exp.Expression | None) -> exp.Select | None:
        if isinstance(node, exp.Subquery):
            node = node.this
        if isinstance(node, exp.Select):
            return node
        if isinstance(node, exp.Expression):
            found = node.find(exp.Select)
            return found if isinstance(found, exp.Select) else None
        return None

    expected_predicate_sql = str(
        boundary_diff.extra.get("standard_sql") or ""
    )
    membership: tuple[
        exp.In,
        exp.Select,
        tuple[str, str],
        tuple[str, str],
        tuple[list[dict[str, Any]], str],
        tuple[list[dict[str, Any]], str],
    ] | None = None
    for in_node in standard_outer.find_all(exp.In):
        if in_node.find_ancestor(exp.Select) is not standard_outer:
            continue
        if not isinstance(in_node.parent, exp.Not) or not isinstance(in_node.this, exp.Column):
            continue
        if expected_predicate_sql and _sql_of(in_node.parent) != expected_predicate_sql:
            continue
        inner = select_body(in_node.args.get("query"))
        if inner is None or not inner.expressions:
            continue
        projected = inner.expressions[0]
        projected = projected.this if isinstance(projected, exp.Alias) else projected
        if not isinstance(projected, exp.Column):
            continue
        outer_ref = _column_ref_in_select_data(data, in_node.this, standard_outer)
        inner_ref = _column_ref_in_select_data(data, projected, inner)
        outer_actual = _actual_data_ref(data, outer_ref) if outer_ref else None
        inner_actual = _actual_data_ref(data, inner_ref) if inner_ref else None
        if outer_ref is None or inner_ref is None or outer_actual is None or inner_actual is None:
            continue
        membership = (
            in_node,
            inner,
            outer_ref,
            inner_ref,
            outer_actual,
            inner_actual,
        )
        break
    if membership is None:
        return False

    membership_in, membership_inner, outer_ref, inner_ref, outer_actual, inner_actual = membership
    outer_rows, outer_column = outer_actual
    inner_rows, inner_column = inner_actual
    if len(outer_rows) < 2 or len(inner_rows) < 2:
        return False

    def align_local_equalities(select: exp.Select, row_index: int) -> bool:
        changed = False
        for equality in select.find_all(exp.EQ):
            if equality.find_ancestor(exp.Select) is not select:
                continue
            if not isinstance(equality.left, exp.Column) or not isinstance(equality.right, exp.Column):
                continue
            left_ref = _column_ref_in_select_data(data, equality.left, select)
            right_ref = _column_ref_in_select_data(data, equality.right, select)
            if left_ref is None or right_ref is None or left_ref[0] == right_ref[0]:
                continue
            left_actual = _actual_data_ref(data, left_ref)
            right_actual = _actual_data_ref(data, right_ref)
            if left_actual is None or right_actual is None:
                continue
            left_rows, left_column = left_actual
            right_rows, right_column = right_actual
            if row_index >= len(left_rows) or row_index >= len(right_rows):
                continue
            value = left_rows[row_index].get(left_column)
            if value is None:
                value = right_rows[row_index].get(right_column)
            if value is None:
                value = _seed_value(left_column, row_index)
            left_rows[row_index][left_column] = value
            right_rows[row_index][right_column] = value
            changed = True
        return changed

    def copy_outer_to_inner(
        outer_ref: tuple[str, str],
        inner_ref: tuple[str, str],
    ) -> bool:
        source = _actual_data_ref(data, outer_ref)
        target = _actual_data_ref(data, inner_ref)
        if source is None or target is None:
            return False
        source_rows, source_column = source
        target_rows, target_column = target
        if not source_rows or not target_rows:
            return False
        value = source_rows[0].get(source_column)
        if value is None:
            value = _seed_value(source_column, 0)
            source_rows[0][source_column] = value
        target_rows[0][target_column] = value
        return True

    def satisfy_neighboring_antimembership(target: exp.In) -> bool:
        """Keep unchanged top-level NOT IN predicates true for row zero."""
        neighbor_changed = False
        for candidate in standard_outer.find_all(exp.In):
            if candidate is target:
                continue
            if candidate.find_ancestor(exp.Select) is not standard_outer:
                continue
            if not isinstance(candidate.parent, exp.Not) or not isinstance(
                candidate.this, exp.Column
            ):
                continue
            inner = select_body(candidate.args.get("query"))
            if inner is None or not inner.expressions:
                continue
            projected = inner.expressions[0]
            projected = projected.this if isinstance(projected, exp.Alias) else projected
            if not isinstance(projected, exp.Column):
                continue
            neighbor_outer_ref = _column_ref_in_select_data(
                data, candidate.this, standard_outer
            )
            neighbor_inner_ref = _column_ref_in_select_data(data, projected, inner)
            neighbor_outer = (
                _actual_data_ref(data, neighbor_outer_ref)
                if neighbor_outer_ref is not None
                else None
            )
            neighbor_inner = (
                _actual_data_ref(data, neighbor_inner_ref)
                if neighbor_inner_ref is not None
                else None
            )
            if neighbor_outer is None or neighbor_inner is None:
                continue
            neighbor_outer_rows, neighbor_outer_column = neighbor_outer
            neighbor_inner_rows, neighbor_inner_column = neighbor_inner
            if not neighbor_outer_rows or not neighbor_inner_rows:
                continue
            outer_value = neighbor_outer_rows[0].get(neighbor_outer_column)
            if outer_value is None:
                outer_value = _seed_value(neighbor_outer_column, 0)
                neighbor_outer_rows[0][neighbor_outer_column] = outer_value
                neighbor_changed = True
            used: set[Any] = set()
            for index, row in enumerate(neighbor_inner_rows):
                value = row.get(neighbor_inner_column)
                if value is not None and value != outer_value and value not in used:
                    used.add(value)
                    continue
                replacement = _counter_value(neighbor_inner_column, outer_value)
                while replacement is None or replacement == outer_value or replacement in used:
                    replacement = _unique_key_value(
                        neighbor_inner_column,
                        index + len(used) + 1,
                        used | {outer_value},
                        replacement,
                    )
                row[neighbor_inner_column] = replacement
                used.add(replacement)
                neighbor_changed = True
        return neighbor_changed

    # Find the physical key used by the top-level ``Pt.PCP = PhPCP.EmployeeID``
    # style join.  The target must be a real physician key; inventing one would
    # make the assembled outer row disappear before NOT IN is evaluated.
    join_ref: tuple[str, str] | None = None
    outer_where = standard_outer.args.get("where")
    if isinstance(outer_where, exp.Where):
        for equality in outer_where.find_all(exp.EQ):
            if equality.find_ancestor(exp.Select) is not standard_outer:
                continue
            if not isinstance(equality.left, exp.Column) or not isinstance(equality.right, exp.Column):
                continue
            left_ref = _column_ref_in_select_data(data, equality.left, standard_outer)
            right_ref = _column_ref_in_select_data(data, equality.right, standard_outer)
            if left_ref == outer_ref and right_ref and right_ref[0] != outer_ref[0]:
                join_ref = right_ref
                break
            if right_ref == outer_ref and left_ref and left_ref[0] != outer_ref[0]:
                join_ref = left_ref
                break
    if join_ref is None:
        return False
    join_actual = _actual_data_ref(data, join_ref)
    if join_actual is None or len(join_actual[0]) < 2:
        return False
    physician_rows, physician_column = join_actual
    # Earlier compatibility probes may have copied a foreign key into the
    # unique physician key column.  Normalize this small key domain first;
    # otherwise the final path can contain a duplicate EmployeeID and SQLite
    # may expose an accidental second physician row (or reject a stricter
    # engine fixture).  Keep row 0 as the selected path and reserve row 1 as
    # the validator's overlapping control value.
    target_value = physician_rows[0].get(physician_column)
    if target_value is None:
        target_value = _seed_value(physician_column, 0)
    seen_keys: set[Any] = {target_value}
    physician_rows[0][physician_column] = target_value
    for index, row in enumerate(physician_rows[1:], start=1):
        value = row.get(physician_column)
        if value is None or value in seen_keys:
            value = _unique_key_value(physician_column, index, seen_keys, value)
            row[physician_column] = value
        seen_keys.add(value)
    overlap_value = physician_rows[1].get(physician_column)
    if overlap_value is None or overlap_value == target_value:
        return False

    # Resolve the correlation links against this same AST.  Parsing the SQL a
    # second time and comparing object ids loses the links, which was the
    # reason the older generic path silently left Prescribes/Undergoes empty.
    links_by_inner: dict[int, list[tuple[tuple[str, str], tuple[str, str]]]] = defaultdict(list)
    for source_ref, target_ref, link_inner in _correlated_subquery_links(standard_ast):
        links_by_inner[id(link_inner)].append((source_ref, target_ref))

    changed = False
    with write_owner("materializer:not_in_reachable_path"):
        # Keep one anti-membership-only outer value and one overlapping value.
        # The remaining department heads are made non-target so the first row
        # is guaranteed to survive standard NOT IN.
        outer_rows[0][outer_column] = target_value
        outer_rows[1][outer_column] = overlap_value
        safe_heads = [
            row.get(physician_column)
            for row in physician_rows
            if row.get(physician_column) is not None and row.get(physician_column) != target_value
        ]
        for index, row in enumerate(inner_rows):
            value = overlap_value if index == 0 else safe_heads[(index - 1) % len(safe_heads)]
            if value == target_value:
                value = _counter_value(inner_column, target_value)
            row[inner_column] = value

        # Align the top-level row's direct equality without copying a new key
        # into the unique physician table.
        changed |= align_local_equalities(standard_outer, 0)
        outer_rows[0][outer_column] = target_value
        physician_rows[0][physician_column] = target_value
        changed |= satisfy_neighboring_antimembership(membership_in)

        # Make each direct EXISTS reachable from the selected Patient row.
        for exists in standard_outer.find_all(exp.Exists):
            if exists.find_ancestor(exp.Select) is not standard_outer:
                continue
            inner = select_body(exists.this)
            if inner is None:
                continue
            changed |= align_local_equalities(inner, 0)
            _set_select_local_literal_predicates(data, inner, 0)
            for source_ref, target_ref in links_by_inner.get(id(inner), ()):
                changed |= copy_outer_to_inner(source_ref, target_ref)

        # The scalar COUNT prerequisite is common in multi-condition teaching
        # exercises.  Two existing joined appointment/nurse rows are enough
        # for ``2 <= COUNT(...)`` and keep the fixture bounded.
        for subquery in standard_outer.find_all(exp.Subquery):
            if subquery.find_ancestor(exp.Select) is not standard_outer:
                continue
            inner = select_body(subquery)
            if inner is None or inner.find(exp.Count) is None:
                continue
            for row_index in range(2):
                changed |= align_local_equalities(inner, row_index)
                _set_select_local_literal_predicates(data, inner, row_index)

        changed = True
    return changed


def _materialize_select_literal_path(
    data: dict[str, list[dict[str, Any]]],
    select: exp.Select,
    row_index: int,
    *,
    protected: tuple[str, str] | None = None,
) -> None:
    """Make one bounded row satisfy simple SELECT-local literal predicates."""
    where = select.args.get("where")
    if not isinstance(where, exp.Where):
        return
    for node in where.find_all(exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE):
        if node.find_ancestor(exp.Select) is not select:
            continue
        column = node.left if isinstance(node.left, exp.Column) else node.right if isinstance(node.right, exp.Column) else None
        literal_node = node.right if column is node.left else node.left if column is node.right else None
        if not isinstance(column, exp.Column) or not isinstance(literal_node, (exp.Literal, exp.Boolean, exp.Null)):
            continue
        ref = _column_ref_in_select_data(data, column, select)
        actual = _actual_data_ref(data, ref) if ref else None
        if not actual:
            continue
        rows, actual_column = actual
        if row_index >= len(rows) or (protected is not None and ref == protected):
            continue
        literal = _semantic_literal_value(literal_node)
        value = _temporal_value_for_comparison(node, literal, true=True)
        if value is not None:
            rows[row_index][actual_column] = value
    for node in where.find_all(exp.In):
        if node.find_ancestor(exp.Select) is not select or not isinstance(node.this, exp.Column):
            continue
        values = [_semantic_literal_value(item) for item in node.expressions]
        if not values:
            continue
        ref = _column_ref_in_select_data(data, node.this, select)
        actual = _actual_data_ref(data, ref) if ref else None
        if actual and row_index < len(actual[0]) and (protected is None or ref != protected):
            actual[0][row_index][actual[1]] = values[0]


def _materialize_aggregate_filter_presence_witness(
    data: dict[str, list[dict[str, Any]]],
    standard_sql: str,
    student_sql: str,
    ast_diffs: list[ASTDiffNode],
) -> bool:
    """Make a top-level aggregate observe a present-vs-absent filter.

    Predicate path validation can prove that a row satisfies the filter and
    that another row does not, but MIN/MAX/SUM/AVG may still collapse those
    rows to the same aggregate result.  This narrow single-table adapter owns
    one simple top-level comparison and chooses aggregate inputs whose
    filtered and unfiltered results are different.  Grouped, joined, windowed,
    and multi-predicate shapes remain on their existing conservative paths.
    """
    if not any(
        diff.diff_type in {"predicate_missing", "predicate_added"}
        and not diff.extra.get("subquery_depth")
        for diff in ast_diffs
    ):
        return False

    standard_ast = _parse_sql(standard_sql)
    student_ast = _parse_sql(student_sql)
    standard_select = _top_select(standard_ast) if standard_ast is not None else None
    student_select = _top_select(student_ast) if student_ast is not None else None
    if not isinstance(standard_select, exp.Select) or not isinstance(
        student_select, exp.Select
    ):
        return False

    standard_where = standard_select.args.get("where")
    student_where = student_select.args.get("where")
    standard_has_where = isinstance(standard_where, exp.Where)
    student_has_where = isinstance(student_where, exp.Where)
    if standard_has_where == student_has_where:
        return False
    filtered_select = standard_select if standard_has_where else student_select
    filtered_where = standard_where if standard_has_where else student_where
    if not isinstance(filtered_where, exp.Where):
        return False
    predicate = _unwrap_paren(filtered_where.this)
    if isinstance(predicate, (exp.And, exp.Or, exp.Not)):
        return False
    if not isinstance(predicate, (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE)):
        return False

    if isinstance(predicate.left, exp.Column) and isinstance(
        predicate.right, exp.Literal
    ):
        filter_column = predicate.left
        filter_literal = predicate.right
        column_on_left = True
    elif isinstance(predicate.right, exp.Column) and isinstance(
        predicate.left, exp.Literal
    ):
        filter_column = predicate.right
        filter_literal = predicate.left
        column_on_left = False
    else:
        return False
    if _is_key_column(filter_column.name):
        # Duplicating a key-like filter value can violate an unstated but
        # common relational identity and would make this a fabricated world.
        return False

    standard_source = _direct_from_table(standard_select)
    student_source = _direct_from_table(student_select)
    if not isinstance(standard_source, exp.Table) or not isinstance(
        student_source, exp.Table
    ) or _norm_name(standard_source.name) != _norm_name(student_source.name):
        return False
    if standard_select.args.get("joins") or student_select.args.get("joins"):
        return False
    if any(
        select.args.get(key)
        for select in (standard_select, student_select)
        for key in ("group", "having", "distinct", "order", "limit", "offset")
    ):
        return False

    def aggregate_nodes(select: exp.Select) -> list[exp.Expression]:
        return [
            node
            for node in select.find_all(*_AGG_FUNC_TYPES)
            if _nearest_select(node) is select
        ]

    standard_aggregates = aggregate_nodes(standard_select)
    student_aggregates = aggregate_nodes(student_select)
    if len(standard_aggregates) != 1 or len(student_aggregates) != 1:
        return False
    standard_aggregate = standard_aggregates[0]
    student_aggregate = student_aggregates[0]
    if (
        type(standard_aggregate) is not type(student_aggregate)
        or _sql_of(standard_aggregate.this) != _sql_of(student_aggregate.this)
    ):
        return False

    table_name = _norm_name(standard_source.name)
    actual_table = next(
        (name for name in data if _norm_name(name) == table_name),
        None,
    )
    rows = data.get(actual_table or "", [])
    if len(rows) < 2:
        return False
    filter_ref = _column_ref_in_select_data(data, filter_column, filtered_select)
    filter_actual = _actual_data_ref(data, filter_ref) if filter_ref else None
    if filter_actual is None or _norm_name(filter_ref[0]) != table_name:
        return False
    filter_rows, filter_column_name = filter_actual

    aggregate_argument = standard_aggregate.this
    measure_column = (
        aggregate_argument
        if isinstance(aggregate_argument, exp.Column)
        else aggregate_argument.find(exp.Column)
        if isinstance(aggregate_argument, exp.Expression)
        else None
    )
    measure_ref = (
        _column_ref_in_select_data(data, measure_column, standard_select)
        if isinstance(measure_column, exp.Column)
        else None
    )
    measure_actual = _actual_data_ref(data, measure_ref) if measure_ref else None
    if measure_actual is not None and _norm_name(measure_ref[0]) != table_name:
        return False
    if measure_actual is not None and measure_actual[1] == filter_column_name:
        # The filter and aggregate argument would compete for the same cell;
        # leave that shape to the existing predicate/aggregate materializers.
        return False

    scalar = _literal_value(filter_literal)
    values = _scalar_predicate_values(
        predicate,
        scalar,
        filter_column.name,
        column_on_left=column_on_left,
    )
    if values is None or values[0] == values[1]:
        return False
    positive_value, negative_value = values
    function = type(standard_aggregate).__name__.upper()
    if function not in {"COUNT", "SUM", "AVG", "MIN", "MAX"}:
        return False

    with write_owner("materializer:aggregate_filter_presence"):
        for index, row in enumerate(filter_rows):
            row[filter_column_name] = positive_value if index == 0 else negative_value

        if measure_actual is None:
            # COUNT(*) already observes the extra unfiltered rows.  Other
            # aggregate arguments cannot be made distinct without a column.
            return function == "COUNT"
        measure_rows, measure_column_name = measure_actual
        if function == "COUNT":
            if isinstance(aggregate_argument, exp.Distinct):
                for index, row in enumerate(measure_rows):
                    row[measure_column_name] = index + 1
            else:
                for row in measure_rows:
                    row[measure_column_name] = 1
        elif function == "MIN":
            measure_rows[0][measure_column_name] = 10
            for row in measure_rows[1:]:
                row[measure_column_name] = 1
        elif function == "MAX":
            measure_rows[0][measure_column_name] = 1
            for row in measure_rows[1:]:
                row[measure_column_name] = 10
        elif function == "SUM":
            measure_rows[0][measure_column_name] = 10
            for row in measure_rows[1:]:
                row[measure_column_name] = 1
        elif function == "AVG":
            measure_rows[0][measure_column_name] = 10
            for row in measure_rows[1:]:
                row[measure_column_name] = 20
    return True


def _materialize_declared_aggregate_boundary(
    data: dict[str, list[dict[str, Any]]],
    obligations: list[DistinguishingObligation],
    standard_sql: str = "",
) -> None:
    """Materialize bounded aggregate constraints from obligation metadata."""

    for obligation in obligations:
        spec = next(
            (
                item for item in obligation.hard_constraints
                if item.kind == "aggregate_boundary_group"
            ),
            None,
        )
        if spec is None or not isinstance(spec.value, (int, float, Decimal)):
            continue
        actual_table = next(
            (name for name in data if _norm_name(name) == _norm_name(spec.relation)),
            None,
        )
        rows = data.get(actual_table or "")
        if not rows:
            continue
        lookup = _column_lookup(rows[0].keys())
        metadata = dict(spec.metadata)
        raw_group_columns = (
            metadata.get("standard_group_columns")
            or metadata.get("student_group_columns")
            or ()
        )
        group_columns = [
            lookup.get(_norm_name(str(item).split(".")[-1].strip('`"[] ')))
            for item in raw_group_columns
        ]
        if raw_group_columns and any(column is None for column in group_columns):
            continue

        function = str(metadata.get("standard_aggregate_function") or "COUNT").upper()
        argument = str(metadata.get("standard_aggregate_argument") or "*").strip()
        distinct = bool(metadata.get("standard_aggregate_distinct", False))
        if argument.upper().startswith("DISTINCT "):
            distinct = True
            argument = argument[9:].strip()
        argument_name = _norm_name(argument.split(".")[-1].strip('`"[] '))
        value_column = lookup.get(argument_name) if argument != "*" else None
        if argument != "*" and value_column is None:
            continue

        global_group = not raw_group_columns
        if function == "COUNT":
            if int(spec.value) != spec.value or spec.value < 1:
                continue
            group_size = int(spec.value)
        else:
            group_size = len(rows) if global_group else 2
        if group_size > len(rows):
            continue

        with write_owner(f"materializer:{obligation.id}:aggregate_boundary"):
            anchor_values = {column: rows[0].get(column) for column in group_columns}
            for row in rows[:group_size]:
                for column, value in anchor_values.items():
                    row[column] = value
            for row_index, row in enumerate(rows[group_size:], start=1):
                for position, column in enumerate(group_columns):
                    candidate = _group_probe_value(
                        column,
                        row_index,
                        70 + position,
                    )
                    if candidate == anchor_values[column]:
                        candidate = _group_probe_value(
                            column,
                            row_index + 1,
                            80 + position,
                        )
                    row[column] = candidate

            if function == "COUNT" and global_group:
                if argument == "*":
                    del rows[group_size:]
                elif value_column:
                    for index, row in enumerate(rows):
                        if index < group_size:
                            row[value_column] = 900000 + index if distinct else 1
                        else:
                            row[value_column] = None if not distinct else 900000
            elif function == "COUNT" and value_column:
                for index, row in enumerate(rows[:group_size]):
                    row[value_column] = 900000 + index if distinct else 1
            elif function == "SUM" and value_column:
                share = spec.value / group_size
                for row in rows[:group_size]:
                    row[value_column] = share
            elif function == "AVG" and value_column:
                for row in rows[:group_size]:
                    row[value_column] = spec.value
            elif function in {"MIN", "MAX"} and value_column:
                for row in rows[:group_size]:
                    row[value_column] = spec.value
            _materialize_aggregate_filter_rows(
                data,
                standard_sql,
                actual_table or "",
                len(rows),
            )


def _materialize_filtered_aggregate_boundary_path(
    data: dict[str, list[dict[str, Any]]],
    obligations: list[DistinguishingObligation],
    standard_sql: str,
    student_sql: str,
    *,
    schema_catalog: SchemaCatalog | None = None,
) -> bool:
    """Materialize a WHERE boundary through a bounded GROUP/HAVING path.

    The distinguishing row is deliberately placed on the many side of a
    simple equality JOIN.  The parent side stays one-row-per-key, so the
    post-join cardinality is ``common_rows + boundary_row`` rather than an
    accidental Cartesian multiplication.  Complex join graphs and unique
    predicate sides are left untouched; their obligations remain explicitly
    unverified instead of receiving an invalid fixture.
    """
    standard_ast = _parse_sql(standard_sql)
    student_ast = _parse_sql(student_sql)
    if standard_ast is None or student_ast is None:
        return False

    def where_comparison(
        select: exp.Select,
        column_name: str,
    ) -> exp.Expression | None:
        where = select.args.get("where")
        if not isinstance(where, exp.Where):
            return None
        for comparison in where.find_all(
            exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE
        ):
            if comparison.find_ancestor(exp.Select) is not select:
                continue
            if any(
                isinstance(item, exp.Column)
                and _norm_name(item.name) == _norm_name(column_name)
                for item in (comparison.left, comparison.right)
            ):
                return comparison
        return None

    def common_value(
        standard_comparison: exp.Expression,
        student_comparison: exp.Expression,
        boundary: Any,
    ) -> Any | None:
        candidates: list[Any] = [boundary]
        if isinstance(boundary, (int, float, Decimal)) and not isinstance(
            boundary, bool
        ):
            candidates.extend((boundary - 1, boundary + 1))
        else:
            candidates.extend((f"{boundary}__common", f"common_{boundary}"))
        for desired in (True, False):
            for comparison in (standard_comparison, student_comparison):
                candidate = _comparison_truth_value(comparison, desired)
                if candidate is not None:
                    candidates.append(candidate)
        seen: set[Any] = set()
        for candidate in candidates:
            try:
                if candidate in seen:
                    continue
                seen.add(candidate)
            except TypeError:
                pass
            if _comparison_matches(standard_comparison, candidate) and _comparison_matches(
                student_comparison, candidate
            ):
                return candidate
        return None

    for obligation in obligations:
        spec = next(
            (
                item
                for item in obligation.hard_constraints
                if item.kind == "filtered_aggregate_boundary_path"
            ),
            None,
        )
        if spec is None or spec.value is None:
            continue
        metadata = dict(spec.metadata)
        source_table = _norm_name(
            str(metadata.get("standard_source_table") or spec.relation or "")
        )
        source_column = _norm_name(
            str(metadata.get("standard_predicate_column") or spec.column or "")
        )
        common_rows = metadata.get("common_qualifying_rows")
        if not source_table or not source_column or not isinstance(common_rows, int):
            continue
        required_rows = common_rows + 1
        if required_rows <= 0:
            continue

        standard_select = next(
            (
                select
                for select in standard_ast.find_all(exp.Select)
                if where_comparison(select, source_column) is not None
                and isinstance(select.args.get("group"), exp.Group)
                and isinstance(select.args.get("having"), exp.Having)
            ),
            None,
        )
        student_select = next(
            (
                select
                for select in student_ast.find_all(exp.Select)
                if where_comparison(select, source_column) is not None
                and isinstance(select.args.get("group"), exp.Group)
                and isinstance(select.args.get("having"), exp.Having)
            ),
            None,
        )
        if not isinstance(standard_select, exp.Select) or not isinstance(
            student_select, exp.Select
        ):
            continue
        standard_comparison = where_comparison(standard_select, source_column)
        student_comparison = where_comparison(student_select, source_column)
        if standard_comparison is None or student_comparison is None:
            continue
        source_ref = _column_ref_in_select(
            next(
                item
                for item in (standard_comparison.left, standard_comparison.right)
                if isinstance(item, exp.Column)
            ),
            standard_select,
        )
        source_actual = _actual_data_ref(data, source_ref) if source_ref else None
        if source_actual is None:
            continue
        source_rows, actual_source_column = source_actual
        if len(source_rows) < required_rows:
            continue

        direct_tables = set(_direct_select_tables(standard_select).values())
        if len(direct_tables) > 2:
            # Keep this materializer bounded to one equality edge.  A later
            # specialized world can handle a join graph without guessing its
            # multiplicity.
            continue
        join_pairs = _join_on_column_pairs(standard_sql)
        source_pairs = [
            pair
            for pair in join_pairs
            if source_table in {pair[0][0], pair[1][0]}
            and pair[0][0] in direct_tables
            and pair[1][0] in direct_tables
        ]
        if len(direct_tables) == 2 and len(source_pairs) != 1:
            continue
        if any(
            ref[0] == source_table
            and _catalog_has_unary_unique_key(schema_catalog, ref)
            for pair in source_pairs
            for ref in pair
        ):
            # Repeating a declared unique source key would create an invalid
            # witness.  The opposite (many) side is handled by another world.
            continue

        boundary = spec.value
        shared_value = common_value(
            standard_comparison,
            student_comparison,
            boundary,
        )
        if shared_value is None:
            continue
        false_value = next(
            (
                candidate
                for candidate in (
                    [boundary - 1, boundary + 1]
                    if isinstance(boundary, (int, float, Decimal))
                    and not isinstance(boundary, bool)
                    else [f"{boundary}__false"]
                )
                if not _comparison_matches(standard_comparison, candidate)
                and not _comparison_matches(student_comparison, candidate)
            ),
            None,
        )
        if false_value is None:
            continue

        group_refs = [
            _column_ref_in_select(item, standard_select)
            for item in (standard_select.args["group"].expressions or ())
            if isinstance(item, exp.Column)
        ]
        group_refs = [item for item in group_refs if item is not None]
        if not group_refs:
            continue
        group_actuals = [
            (_actual_data_ref(data, ref), ref) for ref in group_refs
        ]
        if any(actual is None for actual, _ref in group_actuals):
            continue

        with write_owner(
            f"materializer:{obligation.id}:filtered_aggregate_boundary"
        ):
            # First make every local literal predicate reachable.  The
            # boundary column is assigned below, after this compatibility
            # helper has filled sibling filters.
            for row_index in range(required_rows):
                _set_select_local_literal_predicates(
                    data, standard_select, row_index
                )

            # Keep the parent side of the equality path at exactly one row
            # for the anchor key and make all other parent rows distinct.
            parent_domains: set[Any] = set()
            for left_ref, right_ref in source_pairs:
                source_join_ref, parent_ref = (
                    (left_ref, right_ref)
                    if left_ref[0] == source_table
                    else (right_ref, left_ref)
                )
                source_join_actual = _actual_data_ref(data, source_join_ref)
                parent_actual = _actual_data_ref(data, parent_ref)
                if source_join_actual is None or parent_actual is None:
                    continue
                source_join_rows, source_join_column = source_join_actual
                parent_rows, parent_column = parent_actual
                if not parent_rows:
                    continue
                anchor = parent_rows[0].get(parent_column)
                if anchor is None:
                    anchor = _seed_value(parent_column, 0)
                    parent_rows[0][parent_column] = anchor
                parent_domains = {
                    row.get(parent_column)
                    for row in parent_rows
                    if row.get(parent_column) is not None
                }
                parent_domains.add(anchor)
                parent_rows[0][parent_column] = anchor
                used_parent = {anchor}
                for index, row in enumerate(parent_rows[1:], start=1):
                    value = row.get(parent_column)
                    if value is None or value in used_parent:
                        value = _unique_key_value(
                            parent_column, index, used_parent, anchor
                        )
                        row[parent_column] = value
                    used_parent.add(value)
                for row in source_join_rows[:required_rows]:
                    row[source_join_column] = anchor
                # Rows outside the witness path must not create another
                # qualifying joined group.
                for index, row in enumerate(source_join_rows[required_rows:], start=required_rows):
                    replacement = _unique_key_value(
                        source_join_column,
                        index + len(parent_rows),
                        used_parent,
                        anchor,
                    )
                    while replacement in parent_domains:
                        replacement = _counter_value(source_join_column, replacement)
                    row[source_join_column] = replacement

            # Put all path rows in one GROUP BY key, without touching a
            # declared unique source key.
            for actual, ref in group_actuals:
                rows, column_name = actual
                if ref[0] == source_table:
                    anchor = rows[0].get(column_name)
                    for row in rows[:required_rows]:
                        row[column_name] = anchor

            for index, row in enumerate(source_rows):
                row[actual_source_column] = (
                    shared_value if index < common_rows else boundary
                    if index == common_rows
                    else false_value
                )

            # COUNT(column) must see a non-NULL argument on the participating
            # rows; COUNT(*) needs no additional action.
            having = standard_select.args.get("having")
            count_node = (
                next(iter(having.find_all(exp.Count)), None)
                if isinstance(having, exp.Having)
                else None
            )
            if isinstance(count_node, exp.Count) and count_node.this is not None:
                count_column = count_node.find(exp.Column)
                count_ref = (
                    _column_ref_in_select(count_column, standard_select)
                    if isinstance(count_column, exp.Column)
                    else None
                )
                count_actual = _actual_data_ref(data, count_ref) if count_ref else None
                if count_actual is not None:
                    count_rows, count_column_name = count_actual
                    for row in count_rows[:required_rows]:
                        if row.get(count_column_name) is None:
                            row[count_column_name] = _seed_value(
                                count_column_name, 0
                            )
        return True
    return False


def _materialize_aggregate_filter_rows(
    data: dict[str, list[dict[str, Any]]],
    standard_sql: str,
    target_table: str,
    row_count: int,
) -> None:
    """Keep the declared aggregate group inside its SELECT-local filter."""
    ast = _parse_sql(standard_sql)
    select = _top_select(ast) if ast is not None else None
    if not isinstance(select, exp.Select) or row_count <= 0:
        return
    for row_index in range(row_count):
        _set_select_local_literal_predicates(data, select, row_index)


def _materialize_simple_in_exists_membership_witness(
    data: dict[str, list[dict[str, Any]]],
    standard_sql: str,
    student_sql: str,
) -> bool:
    """Materialize a bounded witness for a simple uncorrelated IN/EXISTS pair.

    ``x IN (SELECT k FROM inner_table)`` and an uncorrelated
    ``EXISTS (SELECT k FROM inner_table)`` are not interchangeable: the former
    filters the outer rows by ``k`` while the latter only checks that the inner
    relation is non-empty.  The generic predicate-presence materializer cannot
    infer that distinction because the EXISTS branch has no comparable scalar
    predicate.  Keep this fallback deliberately narrow; correlated, filtered,
    joined, grouped, ordered, limited, negated, and same-table shapes remain
    on the ordinary bounded/UNDECIDED path.
    """

    def root_predicate(
        sql: str,
    ) -> tuple[exp.Select, exp.Expression] | None:
        ast = _parse_sql(sql)
        # A root SELECT is required.  This excludes set operators and derived
        # table wrappers whose row topology needs a different witness owner.
        if not isinstance(ast, exp.Select):
            return None
        where = ast.args.get("where")
        if not isinstance(where, exp.Where):
            return None
        predicate = _unwrap_paren(where.this)
        if not isinstance(predicate, (exp.In, exp.Exists)):
            return None
        if isinstance(predicate, exp.In) and isinstance(predicate.parent, exp.Not):
            return None
        if isinstance(predicate, exp.Exists) and isinstance(predicate.parent, exp.Not):
            return None
        return ast, predicate

    def simple_root(select: exp.Select) -> exp.Table | None:
        from_clause = select.args.get("from_") or select.args.get("from")
        if (
            not isinstance(from_clause, exp.From)
            or not isinstance(from_clause.this, exp.Table)
            or from_clause.expressions
        ):
            return None
        # The outer block must have one projected column and no other relational
        # operator.  The root WHERE is handled separately by this helper.
        if len(select.expressions or ()) != 1:
            return None
        projected = select.expressions[0]
        projected = projected.this if isinstance(projected, exp.Alias) else projected
        if not isinstance(projected, exp.Column):
            return None
        if any(
            select.args.get(key) is not None
            for key in (
                "joins",
                "group",
                "having",
                "order",
                "limit",
                "offset",
                "distinct",
                "with",
                "with_",
            )
        ):
            return None
        return from_clause.this

    def simple_inner(select: exp.Select) -> exp.Table | None:
        from_clause = select.args.get("from_") or select.args.get("from")
        if (
            not isinstance(from_clause, exp.From)
            or not isinstance(from_clause.this, exp.Table)
            or from_clause.expressions
        ):
            return None
        if len(select.expressions or ()) != 1:
            return None
        if any(
            select.args.get(key) is not None
            for key in (
                "where",
                "joins",
                "group",
                "having",
                "order",
                "limit",
                "offset",
                "distinct",
                "with",
                "with_",
            )
        ):
            return None
        return from_clause.this

    standard_pair = root_predicate(standard_sql)
    student_pair = root_predicate(student_sql)
    if standard_pair is None or student_pair is None:
        return False
    standard_select, standard_predicate = standard_pair
    student_select, student_predicate = student_pair
    if isinstance(standard_predicate, exp.In) == isinstance(student_predicate, exp.In):
        return False
    in_node = standard_predicate if isinstance(standard_predicate, exp.In) else student_predicate
    exists_node = standard_predicate if isinstance(standard_predicate, exp.Exists) else student_predicate
    if not isinstance(in_node, exp.In) or not isinstance(exists_node, exp.Exists):
        return False
    in_outer_select = standard_select if standard_predicate is in_node else student_select
    exists_outer_select = standard_select if standard_predicate is exists_node else student_select
    if in_node.find_ancestor(exp.Select) is not in_outer_select:
        return False
    if exists_node.find_ancestor(exp.Select) is not exists_outer_select:
        return False

    standard_outer_table = simple_root(standard_select)
    student_outer_table = simple_root(student_select)
    if standard_outer_table is None or student_outer_table is None:
        return False
    standard_outer = _norm_name(standard_outer_table.name)
    student_outer = _norm_name(student_outer_table.name)
    if not standard_outer or standard_outer != student_outer:
        return False

    # Once the root WHERE is removed, the two query blocks must be identical
    # modulo harmless alias/qualifier spelling.  This prevents the helper from
    # hiding a second projection, source, or ordering change.
    def without_root_where(select: exp.Select) -> str:
        copied = select.copy()
        copied.set("where", None)
        return _alias_insensitive_sql(copied)

    if without_root_where(standard_select) != without_root_where(student_select):
        return False

    in_query = in_node.args.get("query")
    in_inner = in_query.this if isinstance(in_query, exp.Subquery) else None
    exists_inner = exists_node.this if isinstance(exists_node.this, exp.Select) else None
    if not isinstance(in_inner, exp.Select) or not isinstance(exists_inner, exp.Select):
        return False
    in_table = simple_inner(in_inner)
    exists_table = simple_inner(exists_inner)
    if in_table is None or exists_table is None:
        return False
    inner_name = _norm_name(in_table.name)
    if not inner_name or inner_name != _norm_name(exists_table.name):
        return False
    # Same-table writes would make the outer and inner domains alias the same
    # physical rows.  Leave that more delicate shape to the dedicated probes.
    if inner_name == standard_outer:
        return False
    if _subquery_is_correlated(in_inner) or _subquery_is_correlated(exists_inner):
        return False

    in_projected = in_inner.expressions[0]
    in_projected = in_projected.this if isinstance(in_projected, exp.Alias) else in_projected
    exists_projected = exists_inner.expressions[0]
    exists_projected = (
        exists_projected.this
        if isinstance(exists_projected, exp.Alias)
        else exists_projected
    )
    if not isinstance(in_projected, exp.Column):
        return False
    if not isinstance(exists_projected, (exp.Column, exp.Literal, exp.Boolean, exp.Null)):
        return False
    outer_column_ref = _scope_column_ref(in_node.this, in_outer_select)
    inner_column_ref = _scope_column_ref(in_projected, in_inner)
    if outer_column_ref is None or inner_column_ref is None:
        return False
    if outer_column_ref[0] != standard_outer or inner_column_ref[0] != inner_name:
        return False
    if isinstance(exists_projected, exp.Column):
        exists_column_ref = _scope_column_ref(exists_projected, exists_inner)
        if exists_column_ref is None or exists_column_ref[0] != inner_name:
            return False

    outer_actual = _actual_data_ref(data, outer_column_ref)
    inner_actual = _actual_data_ref(data, inner_column_ref)
    if outer_actual is None or inner_actual is None:
        return False
    outer_rows, outer_column = outer_actual
    inner_rows, inner_column = inner_actual
    if len(outer_rows) < 2 or not inner_rows:
        return False

    outer_values = [row.get(outer_column) for row in outer_rows]
    inner_values = [row.get(inner_column) for row in inner_rows]
    non_null_inner = [value for value in inner_values if value is not None]

    # Prefer an already present inner value that is also present in the outer
    # relation.  This preserves primary-key uniqueness and avoids unnecessary
    # writes in ordinary generated worlds.
    match_value = next(
        (
            value
            for value in non_null_inner
            if value in outer_values
        ),
        None,
    )
    match_index = (
        next(
            (index for index, value in enumerate(outer_values) if value == match_value),
            None,
        )
        if match_value is not None
        else None
    )
    if match_value is None:
        candidate = non_null_inner[0] if non_null_inner else next(
            (value for value in outer_values if value is not None),
            _seed_value(outer_column, 0),
        )
        occupied_outer = {value for value in outer_values[1:] if value is not None}
        occupied_inner_tail = {
            value for value in inner_values[1:] if value is not None
        }
        while candidate is None or candidate in occupied_outer or candidate in occupied_inner_tail:
            candidate = _counter_value(outer_column, candidate)
        match_value = candidate
        match_index = 0
        inner_rows[0][inner_column] = match_value
    if match_index is None:
        # The selected inner key is not currently represented by the outer
        # table.  Pick a value that will not collide with the reserved negative
        # outer row or the remaining inner rows.
        occupied_outer = {value for value in outer_values[1:] if value is not None}
        occupied_inner_tail = {
            value for value in inner_values[1:] if value is not None
        }
        candidate = match_value
        while candidate in occupied_outer or candidate in occupied_inner_tail:
            candidate = _counter_value(outer_column, candidate)
        match_value = candidate
        match_index = 0
        inner_rows[0][inner_column] = match_value
    negative_index = next(
        (index for index in range(len(outer_rows)) if index != match_index),
        None,
    )
    if negative_index is None:
        return False
    occupied_inner = {
        row.get(inner_column)
        for row in inner_rows
        if row.get(inner_column) is not None
    }
    occupied_outer_other = {
        row.get(outer_column)
        for index, row in enumerate(outer_rows)
        if index != negative_index and row.get(outer_column) is not None
    }
    non_match = _counter_value(outer_column, match_value)
    while (
        non_match is None
        or non_match in occupied_inner
        or non_match in occupied_outer_other
        or non_match == match_value
    ):
        non_match = _counter_value(outer_column, non_match)

    with write_owner("materializer:simple_in_exists_membership"):
        outer_rows[match_index][outer_column] = match_value
        outer_rows[negative_index][outer_column] = non_match
    return True


def _materialize_conjunctive_in_exists_membership_witness(
    data: dict[str, list[dict[str, Any]]],
    standard_sql: str,
    student_sql: str,
) -> bool:
    """Reach one uncorrelated IN->EXISTS edit inside an AND predicate.

    The simple helper above intentionally handles only a single root
    predicate.  A common teaching query combines several membership filters;
    the generic presence probe can then make an unchanged sibling (for
    example ``IN('A')``) unreachable and leave both original queries empty.
    This bounded extension activates only when every non-target root leaf is
    textually identical and the target inner query is a single-table query.
    """
    standard_ast = _parse_sql(standard_sql)
    student_ast = _parse_sql(student_sql)
    if not isinstance(standard_ast, exp.Select) or not isinstance(student_ast, exp.Select):
        return False
    standard_where = standard_ast.args.get("where")
    student_where = student_ast.args.get("where")
    if not isinstance(standard_where, exp.Where) or not isinstance(student_where, exp.Where):
        return False
    standard_leaves = _flatten_and(standard_where.this)
    student_leaves = _flatten_and(student_where.this)
    standard_in_nodes = [
        node for node in standard_ast.find_all(exp.In)
        if node.find_ancestor(exp.Select) is standard_ast
    ]
    student_in_nodes = [
        node for node in student_ast.find_all(exp.In)
        if node.find_ancestor(exp.Select) is student_ast
    ]
    student_exists_nodes = [
        node for node in student_ast.find_all(exp.Exists)
        if node.find_ancestor(exp.Select) is student_ast
        and not isinstance(node.parent, exp.Not)
    ]
    if not standard_in_nodes or not student_exists_nodes:
        return False

    def inner_select(node: exp.Expression) -> exp.Select | None:
        if isinstance(node, exp.In):
            query = node.args.get("query")
            return query.this if isinstance(query, exp.Subquery) and isinstance(query.this, exp.Select) else None
        if isinstance(node, exp.Exists):
            return node.this if isinstance(node.this, exp.Select) else None
        return None

    def inner_key(node: exp.Expression) -> str:
        inner = inner_select(node)
        return re.sub(r"\s+", "", _alias_insensitive_sql(inner).lower()) if inner is not None else ""

    student_in_keys = {inner_key(node) for node in student_in_nodes}
    target_pair: tuple[exp.In, exp.Exists] | None = None
    for in_node in standard_in_nodes:
        key = inner_key(in_node)
        if not key or key in student_in_keys:
            continue
        exists_node = next(
            (node for node in student_exists_nodes if inner_key(node) == key),
            None,
        )
        if exists_node is not None:
            if target_pair is not None:
                return False
            target_pair = (in_node, exists_node)
    if target_pair is None:
        return False
    target_in, target_exists = target_pair

    def leaf_key(node: exp.Expression) -> str:
        return re.sub(r"\s+", "", _alias_insensitive_sql(node).lower())

    def without_target(
        leaves: list[exp.Expression],
        target: exp.Expression,
    ) -> list[str]:
        target_leaf = target
        if isinstance(target.parent, exp.Not):
            target_leaf = target.parent
        return sorted(
            leaf_key(node)
            for node in leaves
            if node is not target_leaf
        )

    if without_target(standard_leaves, target_in) != without_target(student_leaves, target_exists):
        return False

    outer_table = _direct_from_table(standard_ast)
    target_inner = inner_select(target_in)
    if not isinstance(outer_table, exp.Table) or target_inner is None:
        return False
    target_inner_table = _direct_from_table(target_inner)
    if not isinstance(target_inner_table, exp.Table):
        return False
    if _subquery_is_correlated(target_inner) or _norm_name(outer_table.name) == _norm_name(target_inner_table.name):
        return False
    target_projected = target_inner.expressions[0] if target_inner.expressions else None
    target_projected = target_projected.this if isinstance(target_projected, exp.Alias) else target_projected
    if not isinstance(target_in.this, exp.Column) or not isinstance(target_projected, exp.Column):
        return False
    outer_ref = _scope_column_ref(target_in.this, standard_ast)
    inner_ref = _scope_column_ref(target_projected, target_inner)
    outer_actual = _actual_data_ref(data, outer_ref) if outer_ref else None
    inner_actual = _actual_data_ref(data, inner_ref) if inner_ref else None
    if outer_actual is None or inner_actual is None:
        return False
    outer_rows, outer_column = outer_actual
    target_rows, target_column = inner_actual
    if len(outer_rows) < 2 or not target_rows:
        return False

    def equality_filter(select: exp.Select) -> tuple[str, Any] | None:
        where = select.args.get("where")
        if not isinstance(where, exp.Where):
            return None
        for leaf in _flatten_and(where.this):
            if not isinstance(leaf, exp.EQ):
                continue
            if isinstance(leaf.left, exp.Column):
                value = _literal_value(leaf.right)
                if value is not None:
                    return leaf.left.name, value
            if isinstance(leaf.right, exp.Column):
                value = _literal_value(leaf.left)
                if value is not None:
                    return leaf.right.name, value
        return None

    def ensure_filter(
        rows: list[dict[str, Any]],
        select: exp.Select,
        used: set[int],
    ) -> tuple[int | None, tuple[str, Any] | None]:
        spec = equality_filter(select)
        if spec is None:
            candidates = [index for index in range(len(rows)) if index not in used]
            return (candidates[0] if candidates else None), None
        column, value = spec
        actual = next((name for name in rows[0] if _norm_name(name) == _norm_name(column)), None)
        if actual is None:
            return None, spec
        existing = next(
            (index for index, row in enumerate(rows) if index not in used and row.get(actual) == value),
            None,
        )
        if existing is not None:
            return existing, spec
        candidates = [index for index in range(len(rows)) if index not in used]
        if not candidates:
            return None, spec
        rows[candidates[0]][actual] = value
        return candidates[0], spec

    match_value = next((row.get(outer_column) for row in outer_rows if row.get(outer_column) is not None), None)
    if match_value is None:
        match_value = _seed_value(outer_column, 0)
    occupied = {
        row.get(outer_column)
        for row in outer_rows
        if row.get(outer_column) is not None
    }
    non_match = _counter_value(outer_column, match_value)
    while non_match in occupied or non_match == match_value:
        non_match = _counter_value(outer_column, non_match)
    used_by_table: dict[str, set[int]] = defaultdict(set)
    writes: list[tuple[dict[str, Any], str, Any]] = []
    writes.append((outer_rows[0], outer_column, match_value))

    # Make one unchanged positive IN sibling reachable for the selected outer
    # row, while keeping negated siblings on their negative path.
    for node in standard_in_nodes:
        if node is target_in:
            continue
        inner = inner_select(node)
        if inner is None:
            return False
        projected = inner.expressions[0] if inner.expressions else None
        projected = projected.this if isinstance(projected, exp.Alias) else projected
        if not isinstance(node.this, exp.Column) or not isinstance(projected, exp.Column):
            return False
        outer_ref = _scope_column_ref(node.this, standard_ast)
        inner_ref = _scope_column_ref(projected, inner)
        outer_link = _actual_data_ref(data, outer_ref) if outer_ref else None
        inner_link = _actual_data_ref(data, inner_ref) if inner_ref else None
        if outer_link is None or inner_link is None:
            return False
        sibling_outer_rows, sibling_outer_column = outer_link
        sibling_rows, sibling_column = inner_link
        if sibling_outer_rows is not outer_rows or sibling_outer_column != outer_column:
            return False
        table_key = _norm_name(str(inner_table_name := _direct_from_table(inner).name if isinstance(_direct_from_table(inner), exp.Table) else ""))
        if not table_key:
            return False
        if isinstance(node.parent, exp.Not):
            for index, row in enumerate(sibling_rows):
                filter_spec = equality_filter(inner)
                filter_actual = (
                    next((name for name in row if _norm_name(name) == _norm_name(filter_spec[0])), None)
                    if filter_spec is not None
                    else None
                )
                if filter_spec is None or (filter_actual is not None and row.get(filter_actual) == filter_spec[1]):
                    writes.append((row, sibling_column, non_match))
            continue
        index, _ = ensure_filter(sibling_rows, inner, used_by_table[table_key])
        if index is None:
            return False
        used_by_table[table_key].add(index)
        writes.append((sibling_rows[index], sibling_column, match_value))

    target_table_key = _norm_name(target_inner_table.name)
    target_index, _ = ensure_filter(target_rows, target_inner, used_by_table[target_table_key])
    if target_index is None:
        return False
    writes.append((target_rows[target_index], target_column, non_match))
    with write_owner("materializer:conjunctive_in_exists_membership"):
        for row, column, value in writes:
            row[column] = value
    return True


def _subquery_is_correlated(node: exp.Expression) -> bool:
    if not isinstance(node, exp.Select):
        return False
    # A physical table name and its alias are not interchangeable for scope
    # resolution.  In ``FROM employee e`` the alias ``e`` is local and the
    # outer query's alias with the same spelling remains visible only when the
    # inner query uses a different alias (for example ``employee x``).
    local_qualifiers = _select_scope_qualifiers(node)
    for col in node.find_all(exp.Column):
        if col.find_ancestor(exp.Select) is not node:
            continue
        if col.table and _norm_name(col.table) not in local_qualifiers:
            return True
    return False


def _subquery_membership_key_ast_diffs(
    standard_ast: exp.Expression,
    student_ast: exp.Expression,
) -> list[ASTDiffNode]:
    """Detect one changed lhs column of an otherwise identical subquery IN."""
    standard_nodes = list(standard_ast.find_all(exp.In))
    student_nodes = list(student_ast.find_all(exp.In))
    if len(standard_nodes) != len(student_nodes):
        return []
    changed: list[tuple[int, exp.In, exp.In]] = []
    for index, (standard_in, student_in) in enumerate(
        zip(standard_nodes, student_nodes)
    ):
        if _sql_of(standard_in) == _sql_of(student_in):
            continue
        standard_query = standard_in.args.get("query")
        student_query = student_in.args.get("query")
        if _sql_of(standard_in.this) == _sql_of(student_in.this):
            # An enclosing IN naturally renders differently when a nested IN
            # changes.  It is a container, not a second membership-key diff.
            continue
        if not (
            isinstance(standard_in.this, exp.Column)
            and isinstance(student_in.this, exp.Column)
            and isinstance(standard_query, exp.Subquery)
            and isinstance(student_query, exp.Subquery)
            and isinstance(standard_query.this, exp.Select)
            and isinstance(student_query.this, exp.Select)
            and _sql_of(standard_query) == _sql_of(student_query)
        ):
            return []
        changed.append((index, standard_in, student_in))
    if len(changed) != 1:
        return []

    index, standard_in, student_in = changed[0]
    copied = standard_ast.copy()
    copied_nodes = list(copied.find_all(exp.In))
    copied_nodes[index].set("this", student_in.this.copy())
    if _sql_of(copied) != _sql_of(student_ast):
        return []

    standard_select = standard_in.find_ancestor(exp.Select)
    student_select = student_in.find_ancestor(exp.Select)
    standard_inner = standard_in.args["query"].this
    student_inner = student_in.args["query"].this
    if not isinstance(standard_select, exp.Select) or not isinstance(
        student_select, exp.Select
    ):
        return []
    standard_outer = _column_ref_in_select(standard_in.this, standard_select)
    student_outer = _column_ref_in_select(student_in.this, student_select)
    standard_projected = standard_inner.expressions[0] if standard_inner.expressions else None
    student_projected = student_inner.expressions[0] if student_inner.expressions else None
    standard_projected = (
        standard_projected.this
        if isinstance(standard_projected, exp.Alias)
        else standard_projected
    )
    student_projected = (
        student_projected.this
        if isinstance(student_projected, exp.Alias)
        else student_projected
    )
    if not isinstance(standard_projected, exp.Column) or not isinstance(
        student_projected, exp.Column
    ):
        return []
    standard_inner_ref = _column_ref_in_select(
        standard_projected,
        standard_inner,
    )
    student_inner_ref = _column_ref_in_select(
        student_projected,
        student_inner,
    )
    if (
        standard_outer is None
        or student_outer is None
        or standard_inner_ref is None
        or student_inner_ref is None
        or standard_inner_ref != student_inner_ref
    ):
        return []
    return [ASTDiffNode(
        clause_category="IN",
        diff_type="subquery_membership_key_changed",
        target_table=standard_outer[0],
        target_column=standard_outer[1],
        standard_node=standard_in.this,
        student_node=student_in.this,
        knowledge_point_id="subquery-in",
        severity=0.8,
        extra={
            "standard_sql": _sql_of(standard_in),
            "student_sql": _sql_of(student_in),
            "standard_query_sql": _sql_of(standard_ast),
            "student_query_sql": _sql_of(student_ast),
            "standard_source_table": standard_outer[0],
            "standard_outer_column": standard_outer[1],
            "standard_membership_table": standard_inner_ref[0],
            "standard_membership_column": standard_inner_ref[1],
            "student_source_table": student_outer[0],
            "student_outer_column": student_outer[1],
            "student_membership_table": student_inner_ref[0],
            "student_membership_column": student_inner_ref[1],
            "query_scope": "nested_membership",
        },
    )]


def _specialized_semantic_ast_diffs(
    standard_ast: exp.Expression,
    student_ast: exp.Expression,
    *,
    standard_sql: str = "",
    student_sql: str = "",
) -> list[ASTDiffNode]:
    """Add diagnostics that require comparing expression shape, not clause text."""
    diffs: list[ASTDiffNode] = _boolean_projection_truth_test_diffs(
        standard_ast,
        student_ast,
        standard_sql=standard_sql,
        student_sql=student_sql,
    )
    diffs.extend(_subquery_membership_key_ast_diffs(standard_ast, student_ast))
    std_select = _top_select(standard_ast)
    stu_select = _top_select(student_ast)

    # Projection and predicate arithmetic changes (for example x * 2 -> x + 2).
    arithmetic_types = (exp.Add, exp.Sub, exp.Mul, exp.Div, exp.Mod)
    if isinstance(std_select, exp.Select) and isinstance(stu_select, exp.Select):
        for std_item, stu_item in zip(std_select.expressions or [], stu_select.expressions or []):
            std_expr = std_item.this if isinstance(std_item, exp.Alias) else std_item
            stu_expr = stu_item.this if isinstance(stu_item, exp.Alias) else stu_item
            std_op = std_expr if isinstance(std_expr, arithmetic_types) else std_expr.find(*arithmetic_types)
            stu_op = stu_expr if isinstance(stu_expr, arithmetic_types) else stu_expr.find(*arithmetic_types)
            if std_op and stu_op and type(std_op) is not type(stu_op):
                diffs.append(_semantic_diff(
                    "expression_operator_changed", "SELECT", std_op, stu_op, "select-basic",
                    standard_operator=type(std_op).__name__.upper(),
                    student_operator=type(stu_op).__name__.upper(),
                ))
                break

        std_where = std_select.args.get("where")
        stu_where = stu_select.args.get("where")
        if isinstance(std_where, exp.Where) and isinstance(stu_where, exp.Where):
            std_ops = list(std_where.find_all(*arithmetic_types))
            stu_ops = list(stu_where.find_all(*arithmetic_types))
            if std_ops and stu_ops and type(std_ops[0]) is not type(stu_ops[0]):
                diffs.append(_semantic_diff(
                    "predicate_expression_operator_changed", "PREDICATE", std_ops[0], stu_ops[0], "where",
                    standard_operator=type(std_ops[0]).__name__.upper(),
                    student_operator=type(stu_ops[0]).__name__.upper(),
                ))

    # Same comparison and boundary, but a different left-hand column.
    std_comps = [node for node in standard_ast.find_all(exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE) if not _is_inside_join(node)]
    stu_comps = [node for node in student_ast.find_all(exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE) if not _is_inside_join(node)]
    std_where_for_pairing = std_select.args.get("where") if isinstance(std_select, exp.Select) else None
    stu_where_for_pairing = stu_select.args.get("where") if isinstance(stu_select, exp.Select) else None
    logical_shape_compatible = (
        isinstance(std_where_for_pairing, exp.Where)
        and isinstance(stu_where_for_pairing, exp.Where)
        and _logical_connective_shape(std_where_for_pairing.this)
        == _logical_connective_shape(stu_where_for_pairing.this)
    ) or (std_where_for_pairing is None and stu_where_for_pairing is None)
    for std_cmp, stu_cmp in zip(std_comps, stu_comps) if logical_shape_compatible else ():
        std_left = std_cmp.left if isinstance(std_cmp.left, exp.Column) else None
        stu_left = stu_cmp.left if isinstance(stu_cmp.left, exp.Column) else None
        if (
            std_left and stu_left
            and type(std_cmp) is type(stu_cmp)
            and _sql_of(std_cmp.right) == _sql_of(stu_cmp.right)
            and _norm_name(std_left.name) != _norm_name(stu_left.name)
        ):
            diffs.append(_semantic_diff(
                "comparison_left_column_changed", "PREDICATE", std_cmp, stu_cmp, "where",
                standard_column=std_left.name,
                student_column=stu_left.name,
            ))
            break

    # Aggregate DISTINCT belongs to the aggregate, not to SELECT DISTINCT.
    std_aggs = list(standard_ast.find_all(*_AGG_FUNC_TYPES))
    stu_aggs = list(student_ast.find_all(*_AGG_FUNC_TYPES))
    for std_agg, stu_agg in zip(std_aggs, stu_aggs):
        std_distinct = bool(std_agg.args.get("distinct") or isinstance(std_agg.this, exp.Distinct))
        stu_distinct = bool(stu_agg.args.get("distinct") or isinstance(stu_agg.this, exp.Distinct))
        if type(std_agg) is type(stu_agg) and std_distinct != stu_distinct:
            diffs.append(_semantic_diff(
                "aggregate_distinct_changed", "AGGREGATE", std_agg, stu_agg, "aggregate",
                standard_distinct=std_distinct,
                student_distinct=stu_distinct,
                standard_aggregate_distinct=std_distinct,
                student_aggregate_distinct=stu_distinct,
                standard_aggregate_function=type(std_agg).__name__.upper(),
                student_aggregate_function=type(stu_agg).__name__.upper(),
                standard_aggregate_argument=_sql_of(std_agg.this) if std_agg.this is not None else "*",
                student_aggregate_argument=_sql_of(stu_agg.this) if stu_agg.this is not None else "*",
            ))
            break

    std_where = std_select.args.get("where") if isinstance(std_select, exp.Select) else None
    stu_where = stu_select.args.get("where") if isinstance(stu_select, exp.Select) else None
    std_body = _unwrap_paren(std_where.this) if isinstance(std_where, exp.Where) else None
    stu_body = _unwrap_paren(stu_where.this) if isinstance(stu_where, exp.Where) else None

    if _is_not_between_expansion(std_body, stu_body) or _is_not_between_expansion(stu_body, std_body):
        diffs.append(_semantic_diff(
            "between_expansion_equivalence", "PREDICATE", std_body, stu_body, "between",
        ))
    if _is_like_negation_equivalence(std_body, stu_body):
        diffs.append(_semantic_diff(
            "like_negation_equivalence", "PREDICATE", std_body, stu_body, "like",
        ))

    std_tree = _logical_tree_signature(std_body)
    stu_tree = _logical_tree_signature(stu_body)
    if std_tree and stu_tree and std_tree != stu_tree:
        std_skeleton = _extract_logical_skeleton(std_body)
        stu_skeleton = _extract_logical_skeleton(stu_body)
        if (
            std_skeleton["operators"] == stu_skeleton["operators"]
            and std_skeleton["leaves"] == stu_skeleton["leaves"]
        ):
            diffs.append(_semantic_diff(
                "logical_precedence_tree_changed", "LOGICAL", std_where, stu_where, "where",
                standard_tree=std_tree,
                student_tree=stu_tree,
                standard_predicate_sql=_sql_of(std_where.this),
                student_predicate_sql=_sql_of(stu_where.this),
                standard_source_table=(
                    _direct_from_table(std_select).name
                    if isinstance(_direct_from_table(std_select), exp.Table)
                    else ""
                ),
            ))

    if _in_exists_rewrite(standard_ast, student_ast) or _in_exists_rewrite(student_ast, standard_ast):
        diffs.append(_semantic_diff(
            "in_exists_equivalence", "SUBQUERY", standard_ast.find(exp.In), student_ast.find(exp.Exists), "subquery-exists",
        ))
    antijoin_metadata = _strict_in_exists_filter_metadata(
        standard_ast,
        student_ast,
        allow_negated=True,
    )
    not_in_side = "standard"
    if antijoin_metadata is None:
        antijoin_metadata = _strict_in_exists_filter_metadata(
            student_ast,
            standard_ast,
            allow_negated=True,
        )
        not_in_side = "student"
    if antijoin_metadata is not None:
        standard_node = standard_ast.find(exp.In) or standard_ast.find(exp.Exists)
        student_node = student_ast.find(exp.In) or student_ast.find(exp.Exists)
        diffs.append(_semantic_diff(
            "null_sensitive_antijoin_equivalence", "NULL", standard_node, student_node, "null-handling",
            standard_query_sql=standard_sql,
            student_query_sql=student_sql,
            **antijoin_metadata,
            not_in_side=not_in_side,
        ))

    std_order = _result_order_clause(standard_ast)
    stu_order = _result_order_clause(student_ast)
    if std_order and not stu_order and _limit_repr(standard_ast) == _limit_repr(student_ast) and _limit_repr(standard_ast):
        diffs.append(_semantic_diff(
            "top_n_ordering_missing", "ORDER BY", std_order, stu_order, "order-by",
        ))

    std_joins = list(standard_ast.find_all(exp.Join))
    stu_joins = list(student_ast.find_all(exp.Join))
    for std_join, stu_join in zip(std_joins, stu_joins):
        std_on = std_join.args.get("on")
        stu_on = stu_join.args.get("on")
        if isinstance(std_on, exp.Expression) and isinstance(stu_on, exp.Expression):
            std_cols = [_norm_name(col.name) for col in std_on.find_all(exp.Column)]
            stu_cols = [_norm_name(col.name) for col in stu_on.find_all(exp.Column)]
            if std_cols != stu_cols:
                diffs.append(_semantic_diff(
                    "join_key_column_changed", "JOIN ON", std_on, stu_on, "join-on",
                    standard_columns=std_cols,
                    student_columns=stu_cols,
                ))
                break

    std_set = _set_operator_node(standard_ast)
    stu_set = _set_operator_node(student_ast)
    if (
        type(std_set) is type(stu_set)
        and isinstance(std_set, (exp.Union, exp.Intersect, exp.Except))
        and _set_operator_modifier(std_set) != _set_operator_modifier(stu_set)
    ):
        diffs.append(_semantic_diff(
            "set_all_modifier_changed", "UNION", std_set, stu_set, "union",
            standard_modifier=_set_operator_modifier(std_set),
            student_modifier=_set_operator_modifier(stu_set),
        ))

    std_set_nodes = [node for node in standard_ast.walk() if isinstance(node, (exp.Union, exp.Intersect, exp.Except))]
    stu_set_nodes = [node for node in student_ast.walk() if isinstance(node, (exp.Union, exp.Intersect, exp.Except))]
    for std_nested, stu_nested in zip(std_set_nodes, stu_set_nodes):
        if type(std_nested) is not type(stu_nested):
            diffs.append(_semantic_diff(
                "set_operator_changed", "UNION", std_nested, stu_nested, _set_operator_kp(_set_operator_name(std_nested)),
            ))
            break
        if _set_operator_modifier(std_nested) != _set_operator_modifier(stu_nested):
            diffs.append(_semantic_diff(
                "set_modifier_changed", "UNION", std_nested, stu_nested, _set_operator_kp(_set_operator_name(std_nested)),
                standard_modifier=_set_operator_modifier(std_nested),
                student_modifier=_set_operator_modifier(stu_nested),
            ))
            break

    if _is_recursive_ast(standard_ast) and _is_recursive_ast(student_ast):
        std_recursive_arithmetic = [node for node in standard_ast.find_all(*arithmetic_types)]
        stu_recursive_arithmetic = [node for node in student_ast.find_all(*arithmetic_types)]
        if std_recursive_arithmetic and stu_recursive_arithmetic:
            std_step = std_recursive_arithmetic[0]
            stu_step = stu_recursive_arithmetic[0]
            if _sql_of(std_step) != _sql_of(stu_step):
                diffs.append(_semantic_diff(
                    "recursive_step_expression_changed", "CTE_RECURSIVE", std_step, stu_step, "cte-recursive",
                ))
    return diffs


def _strict_in_exists_filter_metadata(
    in_ast: exp.Expression,
    exists_ast: exp.Expression,
    *,
    allow_negated: bool = False,
) -> dict[str, Any] | None:
    """Describe one strict IN/EXISTS rewrite and its physical membership key.

    Negated forms are deliberately opt-in: positive IN/EXISTS can be an
    equivalence fast path, while NOT IN/NOT EXISTS need a NULL witness.
    """
    in_nodes = list(in_ast.find_all(exp.In))
    exists_nodes = list(exists_ast.find_all(exp.Exists))
    if len(in_nodes) != 1 or len(exists_nodes) != 1:
        return None
    in_node = in_nodes[0]
    exists = exists_nodes[0]
    in_negated = isinstance(in_node.parent, exp.Not)
    exists_negated = isinstance(exists.parent, exp.Not)
    if allow_negated:
        if not (in_negated and exists_negated):
            return None
    elif in_negated or exists_negated:
        return None

    in_outer = in_node.find_ancestor(exp.Select)
    exists_outer = exists.find_ancestor(exp.Select)
    if (
        not isinstance(in_outer, exp.Select)
        or not isinstance(exists_outer, exp.Select)
        or in_outer is not _top_select(in_ast)
        or exists_outer is not _top_select(exists_ast)
        or in_node.find_ancestor(exp.Where) is None
        or exists.find_ancestor(exp.Where) is None
        or not isinstance(in_node.this, exp.Column)
    ):
        return None
    in_query = in_node.args.get("query")
    in_inner = in_query.this if isinstance(in_query, exp.Subquery) else None
    exists_inner = exists.this if isinstance(exists.this, exp.Select) else None
    if not isinstance(in_inner, exp.Select) or not isinstance(exists_inner, exp.Select):
        return None
    if any(
        select.args.get(key)
        for select in (in_inner, exists_inner)
        for key in (
            "joins", "group", "having", "order", "limit", "offset",
            "distinct", "with", "with_",
        )
    ):
        return None

    in_source = _direct_from_table(in_inner)
    exists_source = _direct_from_table(exists_inner)
    in_outer_source = _direct_from_table(in_outer)
    exists_outer_source = _direct_from_table(exists_outer)
    if not all((in_source, exists_source, in_outer_source, exists_outer_source)):
        return None
    if (
        _norm_name(in_source.name) != _norm_name(exists_source.name)
        or _norm_name(in_outer_source.name) != _norm_name(exists_outer_source.name)
    ):
        return None

    projected = in_inner.expressions[0] if len(in_inner.expressions or ()) == 1 else None
    projected = projected.this if isinstance(projected, exp.Alias) else projected
    exists_projection = (
        exists_inner.expressions[0]
        if len(exists_inner.expressions or ()) == 1
        else None
    )
    exists_projection = (
        exists_projection.this
        if isinstance(exists_projection, exp.Alias)
        else exists_projection
    )
    if not isinstance(projected, exp.Column) or not isinstance(
        exists_projection, (exp.Literal, exp.Boolean, exp.Null)
    ):
        return None

    in_outer_ref = _scope_column_ref(in_node.this, in_outer)
    in_projected_ref = _scope_column_ref(projected, in_inner)
    if in_outer_ref is None or in_projected_ref is None:
        return None

    inner_refs = _select_scope_qualifiers(exists_inner)
    outer_refs = _select_scope_qualifiers(exists_outer)

    def inner_ref(column: exp.Column) -> tuple[str, str] | None:
        qualifier = _norm_name(column.table or "")
        if qualifier and qualifier not in inner_refs:
            return None
        return _scope_column_ref(column, exists_inner)

    def outer_ref(column: exp.Column) -> tuple[str, str] | None:
        qualifier = _norm_name(column.table or "")
        if not qualifier or qualifier not in outer_refs or qualifier in inner_refs:
            return None
        return _scope_column_ref(column, exists_outer)

    where = exists_inner.args.get("where")
    leaves = _flatten_and(where.this) if isinstance(where, exp.Where) else []
    correlations: list[exp.EQ] = []
    for leaf in leaves:
        if not isinstance(leaf, exp.EQ):
            continue
        if not isinstance(leaf.left, exp.Column) or not isinstance(leaf.right, exp.Column):
            continue
        sides = (leaf.left, leaf.right)
        local_columns = [column for column in sides if inner_ref(column) == in_projected_ref]
        outer_columns = [column for column in sides if outer_ref(column) == in_outer_ref]
        if (
            len(local_columns) == 1
            and len(outer_columns) == 1
            and local_columns[0] is not outer_columns[0]
        ):
            correlations.append(leaf)
    if len(correlations) != 1:
        return None
    correlation = correlations[0]
    exists_remainder = tuple(
        sorted(_unqualified_sql(leaf) for leaf in leaves if leaf is not correlation)
    )
    in_where = in_inner.args.get("where")
    in_remainder = tuple(
        sorted(
            _unqualified_sql(leaf)
            for leaf in (
                _flatten_and(in_where.this)
                if isinstance(in_where, exp.Where)
                else []
            )
        )
    )
    if exists_remainder != in_remainder:
        return None

    require_inner_null = True
    require_outer_null = False
    in_where = in_inner.args.get("where")
    if isinstance(in_where, exp.Where):
        for leaf in _flatten_and(in_where.this):
            is_not_null = (
                isinstance(leaf, exp.Not)
                and isinstance(leaf.this, exp.Is)
                and isinstance(leaf.this.this, exp.Column)
            )
            is_null = isinstance(leaf, exp.Is) and isinstance(leaf.this, exp.Column)
            checked = leaf.this if is_not_null else leaf if is_null else None
            if not isinstance(checked, exp.Is):
                continue
            if _scope_column_ref(checked.this, in_inner) != in_projected_ref:
                continue
            if is_not_null:
                require_inner_null = False
                require_outer_null = True
                break

    in_copy = in_ast.copy()
    exists_copy = exists_ast.copy()
    copied_in = next(iter(in_copy.find_all(exp.In)), None)
    copied_exists = next(iter(exists_copy.find_all(exp.Exists)), None)
    if not isinstance(copied_in, exp.In) or not isinstance(copied_exists, exp.Exists):
        return None
    copied_in.replace(exp.Boolean(this=True))
    copied_exists.replace(exp.Boolean(this=True))
    if _alias_insensitive_sql(in_copy) != _alias_insensitive_sql(exists_copy):
        return None

    return {
        "standard_source_table": in_outer_ref[0],
        "standard_membership_table": in_projected_ref[0],
        "standard_outer_column": in_outer_ref[1],
        "standard_membership_column": in_projected_ref[1],
        "in_query_negated": in_negated,
        "exists_query_negated": exists_negated,
        "require_inner_null": require_inner_null,
        "require_outer_null": require_outer_null,
    }


def _strict_in_exists_filter_equivalent(
    in_ast: exp.Expression,
    exists_ast: exp.Expression,
) -> bool:
    """Prove one WHERE ``IN`` is the corresponding correlated ``EXISTS``."""
    return _strict_in_exists_filter_metadata(in_ast, exists_ast) is not None


def _correlated_subquery_context_ast_diffs(
    standard_ast: exp.Expression,
    student_ast: exp.Expression,
) -> list[ASTDiffNode]:
    """Detect changes in the outer predicate that wraps a correlated subquery.

    Example: ``x > 5 * (SELECT ... WHERE t.id = s.id)`` vs
    ``x > 4 * (SELECT ... WHERE t.id = s.id)``. The inner correlated SELECT is
    identical, but the correlated predicate's effective boundary changed.
    """
    std_contexts = _correlated_subquery_contexts(standard_ast)
    stu_contexts = _correlated_subquery_contexts(student_ast)
    if not std_contexts and not stu_contexts:
        return []
    if _subquery_membership_key_ast_diffs(standard_ast, student_ast):
        # The correlation itself is unchanged; only a surrounding IN lhs is
        # wrong.  Let the nested-membership obligation own that distinction.
        return []
    standard_links = _correlated_subquery_links(standard_ast)
    student_links = _correlated_subquery_links(student_ast)
    standard_pairs = {item[:2] for item in standard_links}
    student_pairs = {item[:2] for item in student_links}
    if std_contexts == stu_contexts and standard_pairs == student_pairs:
        # The complete inner body may differ (for example DISTINCT or ORDER)
        # while both the outer wrapper and correlation links remain intact.
        # Those changes belong to their own atomic rule, not to correlation.
        return []
    changed_standard = [
        item for item in standard_links if item[:2] not in student_pairs
    ]
    changed_student = [
        item for item in student_links if item[:2] not in standard_pairs
    ]
    if changed_standard and changed_student:
        standard_outer, standard_inner, standard_select = changed_standard[0]

        def pairing_score(
            candidate: tuple[tuple[str, str], tuple[str, str], exp.Select],
        ) -> int:
            student_outer, student_inner, _ = candidate
            return (
                (8 if student_inner == standard_inner else 0)
                + (8 if student_outer == standard_outer else 0)
                + (3 if student_inner[0] == standard_inner[0] else 0)
                + (3 if student_outer[0] == standard_outer[0] else 0)
            )

        student_outer, student_inner, student_select = max(
            changed_student,
            key=pairing_score,
        )
        standard_comparison = _correlation_comparison(
            standard_select,
            standard_outer,
            standard_inner,
        )
        student_comparison = _correlation_comparison(
            student_select,
            student_outer,
            student_inner,
        )
        if standard_comparison is not None and student_comparison is not None:
            return [ASTDiffNode(
                clause_category="CORRELATED SUBQUERY",
                diff_type="correlated_predicate_changed",
                target_table=standard_outer[0],
                target_column=standard_outer[1],
                standard_node=standard_comparison,
                student_node=student_comparison,
                knowledge_point_id="subquery-correlated",
                severity=0.82,
                extra={
                    "standard_sql": _sql_of(standard_comparison),
                    "student_sql": _sql_of(student_comparison),
                    "standard_query_sql": _sql_of(standard_ast),
                    "student_query_sql": _sql_of(student_ast),
                    "standard_source_table": standard_outer[0],
                    "standard_membership_table": standard_inner[0],
                    "standard_outer_column": standard_outer[1],
                    "standard_membership_column": standard_inner[1],
                    "student_source_table": student_outer[0],
                    "student_membership_table": student_inner[0],
                    "student_outer_column": student_outer[1],
                    "student_membership_column": student_inner[1],
                    "query_scope": "nested_correlation",
                },
            )]
    standard_node = next(
        (
            node for node in list(standard_ast.find_all(exp.Subquery))
            + list(standard_ast.find_all(exp.Exists))
            if isinstance(node.this, exp.Select) and _subquery_is_correlated(node.this)
        ),
        None,
    )
    inner_select = standard_node.this if isinstance(standard_node, exp.Expression) else None
    outer_select = standard_node.find_ancestor(exp.Select) if isinstance(standard_node, exp.Expression) else None
    inner_source = _direct_from_table(inner_select)
    outer_source = _direct_from_table(outer_select)
    inner_tables = {
        _norm_name(table.name)
        for table in inner_select.find_all(exp.Table)
    } if isinstance(inner_select, exp.Select) else set()
    inner_aliases = {
        _norm_name(table.alias)
        for table in inner_select.find_all(exp.Table)
        if table.alias
    } if isinstance(inner_select, exp.Select) else set()
    outer_tables = {
        _norm_name(table.name)
        for table in outer_select.find_all(exp.Table)
    } if isinstance(outer_select, exp.Select) else set()
    outer_aliases = {
        _norm_name(table.alias)
        for table in outer_select.find_all(exp.Table)
        if table.alias
    } if isinstance(outer_select, exp.Select) else set()
    inner_column = outer_column = ""
    if isinstance(inner_select, exp.Select):
        for predicate in inner_select.find_all(exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE):
            columns = list(predicate.find_all(exp.Column))
            if len(columns) != 2:
                continue
            for column in columns:
                table_ref = _norm_name(column.table)
                if table_ref in inner_tables or table_ref in inner_aliases:
                    inner_column = column.name
                elif table_ref in outer_tables or table_ref in outer_aliases:
                    outer_column = column.name
            if inner_column and outer_column:
                break
    student_outer_ref: tuple[str, str] = ("", "")
    student_inner_ref: tuple[str, str] = ("", "")
    if student_links:
        student_outer_ref, student_inner_ref, _student_inner_select = student_links[0]
    return [ASTDiffNode(
        clause_category="CORRELATED SUBQUERY",
        diff_type="correlated_predicate_changed",
        standard_node=standard_ast.find(exp.Subquery) or standard_ast.find(exp.Exists),
        student_node=student_ast.find(exp.Subquery) or student_ast.find(exp.Exists),
        knowledge_point_id="subquery-correlated",
        severity=0.78,
        extra={
            "standard_sql": " | ".join(std_contexts),
            "student_sql": " | ".join(stu_contexts),
            "standard_source_table": (
                outer_source.name if isinstance(outer_source, exp.Table) else ""
            ),
            "standard_membership_table": (
                inner_source.name if isinstance(inner_source, exp.Table) else ""
            ),
            "standard_outer_column": outer_column,
            "standard_membership_column": inner_column,
            "student_source_table": student_outer_ref[0],
            "student_membership_table": student_inner_ref[0],
            "student_outer_column": student_outer_ref[1],
            "student_membership_column": student_inner_ref[1],
        },
    )]


def _correlated_subquery_contexts(ast: exp.Expression) -> list[str]:
    contexts: list[str] = []
    candidates: list[exp.Expression] = list(ast.find_all(exp.Subquery)) + list(ast.find_all(exp.Exists))
    for node in candidates:
        inner = node.this
        if not isinstance(inner, exp.Select) or not _subquery_is_correlated(inner):
            continue
        contexts.append(_subquery_predicate_context_sql(node))
    return sorted(contexts)


def _materialize_limit_antijoin_path(
    data: dict[str, list[dict[str, Any]]],
    obligations: list[DistinguishingObligation],
    standard_sql: str,
    student_sql: str,
) -> bool:
    """Keep enough LEFT anti-join rows alive for LIMIT/OFFSET boundaries."""
    obligation = next(
        (
            item
            for item in obligations
            if any(
                constraint.kind == "limit_row_count_paths"
                for constraint in item.hard_constraints
            )
        ),
        None,
    )
    if obligation is None:
        return False
    required_rows = max(
        _limit_offset_required_rows(standard_sql) - 1,
        _limit_offset_required_rows(student_sql) - 1,
        0,
    )
    if required_rows < 1:
        return False

    ast = _parse_sql(standard_sql)
    select = _top_select(ast) if ast is not None else None
    if not isinstance(select, exp.Select):
        return False
    where = select.args.get("where")
    if not isinstance(where, exp.Where):
        return False

    for join in select.args.get("joins") or ():
        if str(join.args.get("side") or "").upper() != "LEFT":
            continue
        if not isinstance(join.this, exp.Table):
            continue
        right_table = _norm_name(join.this.name)
        anti_ref: tuple[str, str] | None = None
        for predicate in where.find_all(exp.Is):
            if not isinstance(predicate.this, exp.Column) or not isinstance(
                predicate.expression, exp.Null
            ):
                continue
            candidate = _column_ref_in_select_data(
                data,
                predicate.this,
                select,
            )
            if candidate is not None and candidate[0] == right_table:
                anti_ref = candidate
                break
        if anti_ref is None:
            continue

        pair = next(
            (
                candidate
                for candidate in _join_on_column_pairs(standard_sql)
                if anti_ref in candidate
            ),
            None,
        )
        if pair is None:
            continue
        left_ref = pair[1] if pair[0] == anti_ref else pair[0]
        right_ref = anti_ref
        left_actual = _actual_data_ref(data, left_ref)
        right_actual = _actual_data_ref(data, right_ref)
        if left_actual is None or right_actual is None:
            continue
        left_rows, left_column = left_actual
        right_rows, right_column = right_actual
        if len(left_rows) < required_rows or not right_rows:
            continue

        with write_owner(
            f"materializer:{obligation.id}:limit_antijoin_path"
        ):
            all_left_values = {
                row.get(left_column)
                for row in left_rows
                if row.get(left_column) is not None
            }
            target_rows = left_rows[-required_rows:]
            target_values: set[Any] = set()
            for index, row in enumerate(target_rows):
                value = row.get(left_column)
                if value is None or value in target_values:
                    value = _unique_key_value(
                        left_column,
                        len(left_rows) + index,
                        all_left_values | target_values,
                        min(
                            all_left_values,
                            key=lambda item: (type(item).__name__, repr(item)),
                            default=None,
                        ),
                    )
                    row[left_column] = value
                target_values.add(value)

            used_right = {
                row.get(right_column)
                for row in right_rows
                if row.get(right_column) not in target_values
                and row.get(right_column) is not None
            }
            for index, row in enumerate(right_rows):
                if row.get(right_column) not in target_values:
                    continue
                replacement = _unique_key_value(
                    right_column,
                    len(right_rows) + index,
                    used_right | target_values,
                    row.get(right_column),
                )
                row[right_column] = replacement
                used_right.add(replacement)
        return True
    return False


def _apply_aggregate_function_probe(
    rows: list[dict[str, Any]],
    columns: list[str],
    table_name: str,
    standard_sql: str,
    student_sql: str,
    ast_diffs: list[ASTDiffNode],
) -> None:
    """Give changed aggregates a bounded, row-scale-stable discriminator."""
    changed = [
        diff
        for diff in ast_diffs
        if diff.diff_type == "aggregate_function_changed" and diff.target_column
    ]
    if not changed or len(rows) < 2:
        return

    group_refs = _group_by_columns_for_sql(standard_sql) | _group_by_columns_for_sql(
        student_sql
    )
    aliases: dict[str, str] = {}
    for sql in (standard_sql, student_sql):
        ast = _parse_sql(sql)
        if ast is not None:
            aliases.update(_table_aliases(ast))
    table_norm = _norm_name(table_name)
    lookup = _column_lookup(columns)

    group_columns = [
        lookup[column]
        for table, column in group_refs
        if column in lookup and (not table or aliases.get(table, table) == table_norm)
    ]
    changed_columns = [
        (diff, lookup[_norm_name(str(diff.target_column))])
        for diff in changed
        if _norm_name(str(diff.target_column)) in lookup
    ]
    if not changed_columns:
        return

    for diff, value_column in changed_columns:
        standard_function = str(
            diff.extra.get("standard_aggregate_function")
            or diff.extra.get("standard_func")
            or ""
        ).upper()
        student_function = str(
            diff.extra.get("student_aggregate_function")
            or diff.extra.get("student_func")
            or ""
        ).upper()
        if not standard_function or not student_function:
            continue

        if not group_columns or value_column in group_columns:
            # One global group: these values keep COUNT, SUM, AVG, MIN and MAX
            # pairwise distinct for every bounded row count from 4 through
            # 32. Keep one NULL as well: AVG and SUM/COUNT(*) are only
            # semantically equivalent when COUNT counts the same non-NULL
            # measure rows.
            values = [1, 44] + [4] * max(0, len(rows) - 2)
            for row, value in zip(rows, values):
                row[value_column] = value
            rows[-1][value_column] = None
            continue

        group_values = _aggregate_function_discriminator_groups(
            standard_function,
            student_function,
        )
        if group_values is None:
            # Unsupported aggregate families still receive a non-degenerate
            # group, but are not falsely reported as a declared discriminator.
            group_values = ((1, 9), (7,))
        left_values, right_values = group_values
        required = len(left_values) + len(right_values)
        if required > len(rows):
            continue

        cursor = 0
        for group_index, values in enumerate((left_values, right_values)):
            for value in values:
                row = rows[cursor]
                for position, column in enumerate(group_columns):
                    row[column] = _aggregate_group_probe_value(
                        column,
                        group_index,
                        position,
                    )
                row[value_column] = value
                cursor += 1

        descending = _aggregate_probe_order_descending(diff)
        aggregate_results = [
            _aggregate_probe_result(function, values)
            for function in (standard_function, student_function)
            for values in (left_values, right_values)
        ]
        numeric_results = [
            float(value)
            for value in aggregate_results
            if isinstance(value, (int, float, Decimal))
        ]
        if descending is False:
            neutral = (max(numeric_results) if numeric_results else 100) + 1000
        else:
            neutral = (min(numeric_results) if numeric_results else 0) - 1000
        # Extra scale rows are isolated into unique groups whose singleton
        # value cannot outrank either discriminator group. This makes 8, 16
        # and 32-row requests preserve the same counterexample.
        for group_index, row in enumerate(rows[cursor:], start=2):
            for position, column in enumerate(group_columns):
                row[column] = _aggregate_group_probe_value(
                    column,
                    group_index,
                    position,
                )
            row[value_column] = neutral


def _apply_cross_table_having_count_probe(
    data: dict[str, list[dict[str, Any]]],
    standard_sql: str,
    student_sql: str,
) -> None:
    """Make COUNT over joined rows observable without duplicating parent keys.

    A grouped parent row usually has a unique primary key, while ``COUNT``
    over a joined child column is driven by repeated child foreign keys.  The
    per-table HAVING probe cannot see that relationship, so it must be applied
    after all tables are built and the standard join keys have been aligned.
    """
    spec = _changed_having_aggregate_spec(standard_sql, student_sql)
    if not spec or spec.get("agg") != "COUNT":
        return
    ast = _parse_sql(standard_sql)
    if not ast:
        return

    for having in ast.find_all(exp.Having):
        select = _nearest_select(having)
        group = select.args.get("group") if isinstance(select, exp.Select) else None
        if not isinstance(select, exp.Select) or not isinstance(group, exp.Group):
            continue
        group_column = next(
            (item for item in group.expressions if isinstance(item, exp.Column)),
            None,
        )
        if not group_column:
            continue
        group_ref = _column_ref_in_select(group_column, select)
        if not group_ref:
            continue

        count_node = next(
            (node for node in having.find_all(exp.Count)),
            None,
        )
        count_column = count_node.find(exp.Column) if count_node else None
        value_ref = _column_ref_in_select(count_column, select) if count_column else None
        if value_ref and value_ref[0] == group_ref[0]:
            continue

        # Resolve a child table from the COUNT argument when possible; for
        # COUNT(*) use the other side of the first join involving the group
        # table.
        join_pair: tuple[tuple[str, str], tuple[str, str]] | None = None
        for left, right in _join_on_column_pairs(standard_sql):
            if left[0] == group_ref[0] and (not value_ref or right[0] == value_ref[0]):
                join_pair = (left, right)
                break
            if right[0] == group_ref[0] and (not value_ref or left[0] == value_ref[0]):
                join_pair = (right, left)
                break
        if not join_pair:
            continue
        parent_ref, child_ref = join_pair
        if parent_ref[0] == child_ref[0]:
            continue
        parent_rows = next(
            (rows for table, rows in data.items() if _norm_name(table) == parent_ref[0]),
            None,
        )
        child_rows = next(
            (rows for table, rows in data.items() if _norm_name(table) == child_ref[0]),
            None,
        )
        if not parent_rows or not child_rows:
            continue
        parent_lookup = _column_lookup(list(parent_rows[0]))
        child_lookup = _column_lookup(list(child_rows[0]))
        parent_join_col = parent_lookup.get(parent_ref[1])
        child_join_col = child_lookup.get(child_ref[1])
        if not parent_join_col or not child_join_col:
            continue

        parent_values = list(dict.fromkeys(
            row.get(parent_join_col) for row in parent_rows
            if row.get(parent_join_col) is not None
        ))
        if not parent_values:
            continue
        boundary = max(1, int(spec["boundary"]))
        targets = [boundary, boundary + 1, max(1, boundary - 1)]
        child_index = 0
        for group_index, count in enumerate(targets):
            if group_index >= len(parent_values):
                break
            parent_value = parent_values[group_index]
            for member_index in range(count):
                if child_index >= len(child_rows):
                    break
                child_rows[child_index][child_join_col] = parent_value
                if (
                    spec.get("distinct")
                    and value_ref
                    and value_ref[0] == child_ref[0]
                    and value_ref[1] in child_lookup
                    and child_lookup[value_ref[1]] != child_join_col
                ):
                    child_rows[child_index][child_lookup[value_ref[1]]] = (
                        f"__having_distinct_{group_index}_{member_index}__"
                    )
                child_index += 1
        fallback = parent_values[-1]
        while child_index < len(child_rows):
            child_rows[child_index][child_join_col] = fallback
            child_index += 1
        return


def _apply_subquery_aggregate_probes(
    rows: list[dict[str, Any]],
    columns: list[str],
    table_name: str,
    standard_sql: str,
    student_sql: str,
) -> None:
    asts = [_parse_sql(standard_sql), _parse_sql(student_sql)]
    lookup = _column_lookup(columns)

    # Prefer a distribution probe for filtered-vs-global AVG subqueries.
    for ast in asts:
        if not ast:
            continue
        for subquery in ast.find_all(exp.Subquery):
            avg = subquery.find(exp.Avg)
            where = subquery.find(exp.Where)
            if not avg or not where:
                continue
            avg_col = avg.find(exp.Column)
            equality = where.find(exp.EQ)
            if not avg_col or not equality:
                continue
            filter_col = equality.left if isinstance(equality.left, exp.Column) else equality.right
            filter_value_node = equality.right if filter_col is equality.left else equality.left
            if not isinstance(filter_col, exp.Column) or not isinstance(filter_value_node, exp.Literal):
                continue
            measure = lookup.get(_norm_name(avg_col.name))
            category = lookup.get(_norm_name(filter_col.name))
            filter_value = _literal_value(filter_value_node)
            if not measure or not category or measure == category or len(rows) < 2:
                continue

            # Keep one filtered value below and one above the filtered AVG,
            # while all non-matching rows sit above the global AVG. This makes
            # the outer predicate distinguish filtered and global averages.
            rows[0][category] = filter_value
            rows[0][measure] = 10
            rows[1][category] = filter_value
            rows[1][measure] = 20
            for row in rows[2:]:
                if row.get(category) == filter_value:
                    if isinstance(filter_value, str):
                        row[category] = f"not_{filter_value}"
                    elif isinstance(filter_value, (int, float, Decimal)):
                        row[category] = filter_value + 1
                    else:
                        row[category] = "not_matching"
                row[measure] = 90
            return

    for ast in asts:
        if not ast:
            continue
        for subquery in ast.find_all(exp.Subquery):
            if not subquery.find(exp.Avg):
                continue
            parent = subquery.parent
            while parent is not None and not isinstance(
                parent,
                (exp.GT, exp.GTE, exp.LT, exp.LTE, exp.EQ, exp.NEQ),
            ):
                parent = parent.parent
            if parent is None:
                continue
            outer_col = (
                parent.left
                if isinstance(parent.left, exp.Column)
                else parent.right
                if isinstance(parent.right, exp.Column)
                else None
            )
            if not isinstance(outer_col, exp.Column):
                continue
            if _norm_name(outer_col.table or table_name) != _norm_name(table_name):
                continue
            actual_col = lookup.get(_norm_name(outer_col.name))
            if not actual_col or not rows:
                continue
            boundary_literal = (
                _literal_value(parent.right)
                if isinstance(parent.right, exp.Literal)
                else _literal_value(parent.left)
                if isinstance(parent.left, exp.Literal)
                else 50
            )
            if not isinstance(boundary_literal, (int, float, Decimal)):
                boundary_literal = 50
            equality_rows = 1 if len(rows) % 2 else 2
            side_rows = max(0, len(rows) - equality_rows)
            lower_rows = side_rows // 2
            for idx, row in enumerate(rows):
                if idx < lower_rows:
                    row[actual_col] = boundary_literal - 1
                elif idx < side_rows:
                    row[actual_col] = boundary_literal + 1
                else:
                    row[actual_col] = boundary_literal
            return


def _set_select_literal_predicates_false(
    data: dict[str, list[dict[str, Any]]],
    select: exp.Select,
    start_index: int,
) -> None:
    where = select.args.get("where")
    if not isinstance(where, exp.Where):
        return
    for comparison in where.find_all(exp.EQ):
        if comparison.find_ancestor(exp.Select) is not select:
            continue
        column = comparison.left if isinstance(comparison.left, exp.Column) else comparison.right
        literal = comparison.right if column is comparison.left else comparison.left
        if not isinstance(column, exp.Column) or not isinstance(literal, exp.Literal):
            continue
        ref = _column_ref_in_select_data(data, column, select)
        actual = _actual_data_ref(data, ref) if ref else None
        if not actual:
            continue
        rows, column_name = actual
        value = _literal_value(literal)
        counter = _counter_value(column_name, value)
        for row in rows[start_index:]:
            row[column_name] = counter


def _apply_scalar_aggregate_comparison_probe(
    data: dict[str, list[dict[str, Any]]],
    comparison: exp.Expression,
) -> bool:
    parts = _comparison_subquery_parts(comparison)
    if not parts:
        return False
    subquery, outer_column = parts
    inner_select = subquery.this if isinstance(subquery.this, exp.Select) else subquery.find(exp.Select)
    outer_select = comparison.find_ancestor(exp.Select)
    if not isinstance(inner_select, exp.Select) or not isinstance(outer_select, exp.Select):
        return False
    aggregate = next(
        (
            inner_select.find(kind)
            for kind in (exp.Avg, exp.Max, exp.Min, exp.Sum)
            if inner_select.find(kind) is not None
        ),
        None,
    )
    measure = aggregate.find(exp.Column) if aggregate is not None else None
    if aggregate is None or not isinstance(measure, exp.Column):
        return False
    inner_ref = _column_ref_in_select_data(data, measure, inner_select)
    outer_ref = _column_ref_in_select_data(data, outer_column, outer_select)
    inner_actual = _actual_data_ref(data, inner_ref) if inner_ref else None
    outer_actual = _actual_data_ref(data, outer_ref) if outer_ref else None
    if not inner_actual or not outer_actual:
        return False
    inner_rows, measure_column = inner_actual
    outer_rows, outer_column_name = outer_actual
    if not inner_rows or not outer_rows:
        return False

    boundary = 50
    filtered = isinstance(inner_select.args.get("where"), exp.Where)
    if filtered:
        matching = min(2, len(inner_rows))
        for index in range(matching):
            _set_select_local_literal_predicates(data, inner_select, index)
        _set_select_literal_predicates_false(data, inner_select, matching)
        target_rows = inner_rows[:matching]
    else:
        target_rows = inner_rows

    if isinstance(aggregate, exp.Avg):
        equality_rows = 1 if len(target_rows) % 2 else 2
        side_rows = max(0, len(target_rows) - equality_rows)
        lower_rows = side_rows // 2
        for index, row in enumerate(target_rows):
            row[measure_column] = (
                boundary - 1
                if index < lower_rows
                else boundary + 1
                if index < side_rows
                else boundary
            )
    elif isinstance(aggregate, exp.Max):
        for index, row in enumerate(target_rows):
            row[measure_column] = boundary if index == len(target_rows) - 1 else boundary - 1
    elif isinstance(aggregate, exp.Min):
        for index, row in enumerate(target_rows):
            row[measure_column] = boundary if index == 0 else boundary + 1
    elif isinstance(aggregate, exp.Sum):
        for row in target_rows:
            row[measure_column] = boundary / max(1, len(target_rows))

    if not (
        isinstance(aggregate, exp.Avg)
        and not filtered
        and outer_rows is inner_rows
        and outer_column_name == measure_column
    ):
        boundary_index = len(outer_rows) - 1
        _set_select_local_literal_predicates(
            data,
            outer_select,
            boundary_index,
        )
        outer_rows[boundary_index][outer_column_name] = boundary
        if len(outer_rows) > 1:
            positive_index = len(outer_rows) - 2
            _set_select_local_literal_predicates(
                data,
                outer_select,
                positive_index,
            )
            outer_rows[positive_index][outer_column_name] = boundary + 1
    return True


def _apply_scalar_lookup_comparison_probe(
    data: dict[str, list[dict[str, Any]]],
    comparison: exp.Expression,
) -> bool:
    parts = _comparison_subquery_parts(comparison)
    if not parts:
        return False
    subquery, outer_column = parts
    inner_select = subquery.this if isinstance(subquery.this, exp.Select) else subquery.find(exp.Select)
    outer_select = comparison.find_ancestor(exp.Select)
    if not isinstance(inner_select, exp.Select) or not isinstance(outer_select, exp.Select):
        return False
    if inner_select.find(exp.AggFunc) is not None or not inner_select.expressions:
        return False
    projected = inner_select.expressions[0]
    projected = projected.this if isinstance(projected, exp.Alias) else projected
    if not isinstance(projected, exp.Column):
        return False
    inner_ref = _column_ref_in_select(projected, inner_select)
    outer_ref = _column_ref_in_select(outer_column, outer_select)
    inner_actual = _actual_data_ref(data, inner_ref) if inner_ref else None
    outer_actual = _actual_data_ref(data, outer_ref) if outer_ref else None
    if not inner_actual or not outer_actual:
        return False
    inner_rows, projected_column = inner_actual
    outer_rows, outer_column_name = outer_actual
    if not inner_rows or not outer_rows:
        return False
    boundary: Any = 50 if _is_numeric_column(projected_column) else "__scalar_boundary__"
    _set_select_local_literal_predicates(data, inner_select, 0)
    _set_select_literal_predicates_false(data, inner_select, 1)
    inner_rows[0][projected_column] = boundary
    outer_rows[-1][outer_column_name] = boundary
    return True


def _apply_scalar_subquery_boundary_probes(
    data: dict[str, list[dict[str, Any]]],
    standard_sql: str,
    student_sql: str,
    ast_diffs: list[ASTDiffNode],
) -> None:
    if not any(diff.diff_type == "comparison_operator_changed" for diff in ast_diffs):
        return
    ast = _parse_sql(standard_sql)
    if ast is None:
        return
    for comparison in ast.find_all(exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE):
        if _apply_scalar_aggregate_comparison_probe(data, comparison):
            continue
        _apply_scalar_lookup_comparison_probe(data, comparison)


def _actual_column_for_expression(
    data: dict[str, list[dict[str, Any]]],
    ast: exp.Expression,
    column: exp.Column,
) -> tuple[list[dict[str, Any]], str] | None:
    aliases = _table_aliases(ast)
    table_ref = aliases.get(_norm_name(column.table or ""), _norm_name(column.table or ""))
    if table_ref:
        return _actual_data_ref(data, (table_ref, _norm_name(column.name)))
    matches = []
    for rows in data.values():
        if not rows:
            continue
        actual = _column_lookup(list(rows[0])).get(_norm_name(column.name))
        if actual:
            matches.append((rows, actual))
    return matches[0] if len(matches) == 1 else None


def _apply_expression_comparison_boundary_probes(
    data: dict[str, list[dict[str, Any]]],
    standard_sql: str,
    ast_diffs: list[ASTDiffNode],
) -> None:
    ast = _parse_sql(standard_sql)
    if ast is None:
        return
    for diff in ast_diffs:
        comparison = diff.standard_node
        if diff.diff_type != "comparison_operator_changed" or not isinstance(
            comparison,
            (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE),
        ):
            continue
        left, right = comparison.left, comparison.right
        arithmetic = left if isinstance(left, exp.Add) else right if isinstance(right, exp.Add) else None
        result_column = right if arithmetic is left and isinstance(right, exp.Column) else left if arithmetic is right and isinstance(left, exp.Column) else None
        if isinstance(arithmetic, exp.Add) and isinstance(result_column, exp.Column):
            operands = [node for node in (arithmetic.left, arithmetic.right) if isinstance(node, exp.Column)]
            if len(operands) == 2:
                first = _actual_column_for_expression(data, ast, operands[0])
                second = _actual_column_for_expression(data, ast, operands[1])
                result = _actual_column_for_expression(data, ast, result_column)
                if first and second and result and first[0] is second[0] is result[0] and first[0]:
                    rows = first[0]
                    rows[0][first[1]] = 1
                    rows[0][second[1]] = 2
                    rows[0][result[1]] = 3
                    continue

        if isinstance(left, exp.Column) and isinstance(right, exp.Column):
            left_actual = _actual_column_for_expression(data, ast, left)
            right_actual = _actual_column_for_expression(data, ast, right)
            if not left_actual or not right_actual:
                continue
            left_rows, left_column = left_actual
            right_rows, right_column = right_actual
            if left_rows is right_rows and left_column == right_column:
                continue
            boundary: Any = 50 if (
                _is_numeric_column(left_column) or _is_numeric_column(right_column)
            ) else "__comparison_boundary__"
            for row in right_rows:
                row[right_column] = boundary
            left_rows[-1][left_column] = boundary


def _self_join_select(ast: exp.Expression) -> tuple[exp.Select, exp.Join] | None:
    select = ast if isinstance(ast, exp.Select) else ast.find(exp.Select)
    if not isinstance(select, exp.Select):
        return None
    source = _direct_from_table(select)
    if not isinstance(source, exp.Table):
        return None
    for join in select.args.get("joins") or []:
        if isinstance(join.this, exp.Table) and _norm_name(join.this.name) == _norm_name(source.name):
            return select, join
    return None


def _apply_self_join_range_probe(
    data: dict[str, list[dict[str, Any]]],
    ast: exp.Expression,
    join: exp.Join,
) -> bool:
    on_node = join.args.get("on")
    if on_node is None:
        return False
    boundary_comparison = next(
        (
            node
            for node in on_node.find_all(exp.GT, exp.GTE, exp.LT, exp.LTE)
            if isinstance(node.left, exp.Column)
            and isinstance(node.right, exp.Add)
            and isinstance(node.right.left, exp.Column)
            and isinstance(node.right.right, exp.Literal)
        ),
        None,
    )
    if boundary_comparison is None:
        return False
    table = _direct_from_table(_nearest_select(join) or ast.find(exp.Select))
    if not isinstance(table, exp.Table):
        return False
    table_name = next((name for name in data if _norm_name(name) == _norm_name(table.name)), None)
    rows = data.get(table_name or "")
    if not rows or len(rows) < 4:
        return False
    lookup = _column_lookup(list(rows[0]))
    range_column = lookup.get(_norm_name(boundary_comparison.left.name))
    id_column = lookup.get("id")
    salary_column = lookup.get("salary")
    if not range_column or not id_column:
        return False
    values = [1, 3, 4, 5]
    for index, row in enumerate(rows[:4]):
        row[id_column] = 1
        row[range_column] = values[index]
        if salary_column:
            row[salary_column] = (index + 1) * 10
    return True


def _apply_self_join_count_probe(
    data: dict[str, list[dict[str, Any]]],
    ast: exp.Expression,
    join: exp.Join,
) -> bool:
    having = ast.find(exp.Having)
    count = having.find(exp.Count) if isinstance(having, exp.Having) else None
    comparison = next(
        (
            node
            for node in having.find_all(exp.GT, exp.GTE, exp.LT, exp.LTE)
            if node.find(exp.Count) is not None
            and isinstance(node.right, exp.Literal)
        ),
        None,
    ) if isinstance(having, exp.Having) else None
    if count is None or comparison is None:
        return False
    boundary = _literal_value(comparison.right)
    if not isinstance(boundary, (int, float, Decimal)):
        return False
    on_node = join.args.get("on")
    equality = on_node.find(exp.EQ) if on_node is not None else None
    if not isinstance(equality, exp.EQ):
        return False
    columns = [node for node in (equality.left, equality.right) if isinstance(node, exp.Column)]
    manager = next((node for node in columns if "manager" in _norm_name(node.name)), None)
    identifier = next((node for node in columns if node is not manager), None)
    source = _direct_from_table(_nearest_select(join) or ast.find(exp.Select))
    if not manager or not identifier or not isinstance(source, exp.Table):
        return False
    table_name = next((name for name in data if _norm_name(name) == _norm_name(source.name)), None)
    rows = data.get(table_name or "")
    if not rows or len(rows) < int(boundary) + 1:
        return False
    lookup = _column_lookup(list(rows[0]))
    manager_column = lookup.get(_norm_name(manager.name))
    id_column = lookup.get(_norm_name(identifier.name))
    name_column = lookup.get("name")
    if not manager_column or not id_column:
        return False
    manager_id = 900
    rows[0][id_column] = manager_id
    rows[0][manager_column] = -1
    if name_column:
        rows[0][name_column] = "__manager_boundary__"
    for index, row in enumerate(rows[1 : int(boundary) + 1], start=1):
        row[id_column] = manager_id + index
        row[manager_column] = manager_id
    for index, row in enumerate(rows[int(boundary) + 1 :], start=int(boundary) + 1):
        row[id_column] = manager_id + index
        row[manager_column] = manager_id + index
    return True


def _apply_self_join_boundary_probes(
    data: dict[str, list[dict[str, Any]]],
    standard_sql: str,
    student_sql: str,
    ast_diffs: list[ASTDiffNode],
) -> None:
    ast = _parse_sql(standard_sql)
    if ast is None or _self_join_select(ast) is None:
        return
    _, join = _self_join_select(ast) or (None, None)
    if not isinstance(join, exp.Join):
        return
    if any(diff.clause_category == "HAVING" for diff in ast_diffs):
        if _apply_self_join_count_probe(data, ast, join):
            return
    _apply_self_join_range_probe(data, ast, join)


def _apply_same_table_membership_probe(
    data: dict[str, list[dict[str, Any]]],
    standard_sql: str,
    student_sql: str,
) -> None:
    ast = _parse_sql(standard_sql)
    if ast is None:
        return
    for in_node in ast.find_all(exp.In):
        query = in_node.args.get("query")
        inner_select = query.this if isinstance(query, exp.Subquery) else None
        outer_select = in_node.find_ancestor(exp.Select)
        if not isinstance(in_node.this, exp.Column) or not isinstance(inner_select, exp.Select) or not isinstance(outer_select, exp.Select):
            continue
        if not inner_select.expressions:
            continue
        projected = inner_select.expressions[0]
        projected = projected.this if isinstance(projected, exp.Alias) else projected
        if not isinstance(projected, exp.Column):
            continue
        outer_ref = _column_ref_in_select(in_node.this, outer_select)
        inner_ref = _column_ref_in_select(projected, inner_select)
        if not outer_ref or not inner_ref or outer_ref[0] != inner_ref[0]:
            continue
        outer_actual = _actual_data_ref(data, outer_ref)
        inner_actual = _actual_data_ref(data, inner_ref)
        if not outer_actual or not inner_actual or len(outer_actual[0]) < 3:
            continue
        rows, outer_column = outer_actual
        _, inner_column = inner_actual
        rows[1][inner_column] = rows[2][outer_column]
        return


def _apply_nested_except_membership_probe(
    data: dict[str, list[dict[str, Any]]],
    standard_sql: str,
    student_sql: str,
) -> None:
    ast = _parse_sql(standard_sql)
    except_node = ast.find(exp.Except) if ast is not None else None
    in_node = except_node.find_ancestor(exp.In) if except_node is not None else None
    if not isinstance(except_node, exp.Except) or not isinstance(in_node, exp.In):
        return
    left = except_node.this if isinstance(except_node.this, exp.Select) else except_node.this.find(exp.Select)
    right = except_node.expression if isinstance(except_node.expression, exp.Select) else except_node.expression.find(exp.Select)
    outer_select = in_node.find_ancestor(exp.Select)
    if not isinstance(left, exp.Select) or not isinstance(right, exp.Select) or not isinstance(outer_select, exp.Select):
        return
    left_projection = left.expressions[0] if left.expressions else None
    left_projection = left_projection.this if isinstance(left_projection, exp.Alias) else left_projection
    if not isinstance(left_projection, exp.Column) or not isinstance(in_node.this, exp.Column):
        return
    inner_ref = _column_ref_in_select(left_projection, left)
    outer_ref = _column_ref_in_select(in_node.this, outer_select)
    inner_actual = _actual_data_ref(data, inner_ref) if inner_ref else None
    outer_actual = _actual_data_ref(data, outer_ref) if outer_ref else None
    if not inner_actual or not outer_actual or len(inner_actual[0]) < 2:
        return
    inner_rows, inner_column = inner_actual
    outer_rows, outer_column = outer_actual
    marker = outer_rows[0][outer_column]
    inner_rows[0][inner_column] = marker
    inner_rows[1][inner_column] = marker
    between = left.find(exp.Between)
    date_column = between.this if isinstance(between, exp.Between) and isinstance(between.this, exp.Column) else None
    if isinstance(date_column, exp.Column):
        date_ref = _column_ref_in_select(date_column, left)
        date_actual = _actual_data_ref(data, date_ref) if date_ref else None
        low = _expression_static_value(between.args.get("low"))
        high = _expression_static_value(between.args.get("high"))
        if date_actual and low is not None and high is not None:
            rows, column = date_actual
            rows[0][column] = low
            high_date = _coerce_datetime(high)
            rows[1][column] = (
                (high_date + timedelta(days=1)).strftime("%Y-%m-%d")
                if high_date is not None
                else _counter_value(column, high)
            )


def _apply_cte_set_overlap_probe(
    data: dict[str, list[dict[str, Any]]],
    standard_sql: str,
    student_sql: str,
    ast_diffs: list[ASTDiffNode],
) -> None:
    if not any(diff.diff_type in {"set_modifier_changed", "set_all_modifier_changed"} for diff in ast_diffs):
        return
    ast = _parse_sql(standard_sql)
    ctes = list(ast.find_all(exp.CTE)) if ast is not None else []
    if len(ctes) < 3:
        return
    first_select = ctes[0].this if isinstance(ctes[0].this, exp.Select) else ctes[0].this.find(exp.Select)
    if not isinstance(first_select, exp.Select) or not first_select.expressions:
        return
    source = _direct_from_table(first_select)
    projected = first_select.expressions[0]
    projected = projected.this if isinstance(projected, exp.Alias) else projected
    equality = first_select.find(exp.EQ)
    if not isinstance(source, exp.Table) or not isinstance(projected, exp.Column) or not isinstance(equality, exp.EQ):
        return
    filter_column = equality.left if isinstance(equality.left, exp.Column) else equality.right
    filter_literal = equality.right if filter_column is equality.left else equality.left
    if not isinstance(filter_column, exp.Column) or not isinstance(filter_literal, exp.Literal):
        return
    table_name = next((name for name in data if _norm_name(name) == _norm_name(source.name)), None)
    rows = data.get(table_name or "")
    if not rows or len(rows) < 3:
        return
    lookup = _column_lookup(list(rows[0]))
    value_column = lookup.get(_norm_name(projected.name))
    parent_column = lookup.get(_norm_name(filter_column.name))
    root = _literal_value(filter_literal)
    if not value_column or not parent_column or not isinstance(root, (int, float, Decimal)):
        return
    rows[0][value_column], rows[0][parent_column] = root + 1, root
    rows[1][value_column], rows[1][parent_column] = root + 2, root + 1
    rows[2][value_column], rows[2][parent_column] = root + 1, root + 2


def _apply_correlated_subquery_probe(
    data: dict[str, list[dict[str, Any]]],
    schema: dict[str, list[str]],
    standard_sql: str,
    student_sql: str,
) -> None:
    """
    相关子查询探针：确保外层表和内层表的关联列有交叉数据。
    Correlated subquery probe: ensures outer/inner table columns have overlapping values.
    """
    correlations = _correlated_subquery_column_pairs(standard_sql, student_sql)

    if not correlations:
        return

    # 对每个相关引用，确保内外层列有重叠值
    for (outer_table, outer_col), (inner_table, inner_col) in correlations:
        # 找到对应的实际表名（大小写归一化）
        outer_table_actual = next((t for t in schema if _norm_name(t) == outer_table), None)
        inner_table_actual = next((t for t in schema if _norm_name(t) == inner_table), None)
        if not outer_table_actual or not inner_table_actual:
            continue
        if outer_table_actual not in data or inner_table_actual not in data:
            continue

        outer_rows = data[outer_table_actual]
        inner_rows = data[inner_table_actual]
        if not outer_rows or not inner_rows:
            continue
        if outer_table_actual == inner_table_actual:
            # Same-table correlations need a different row layout and are
            # handled by the dedicated same-table probes below.
            continue

        # 找到实际列名
        outer_col_actual = next((c for c in schema[outer_table_actual] if _norm_name(c) == outer_col), None)
        inner_col_actual = next((c for c in schema[inner_table_actual] if _norm_name(c) == inner_col), None)
        if not outer_col_actual or not inner_col_actual:
            continue

        # Reuse non-NULL inner keys instead of overwriting them. A preceding
        # NOT IN probe may deliberately place NULL in this projected column;
        # replacing it would erase the three-valued-logic counterexample.
        inner_values = [
            row.get(inner_col_actual)
            for row in inner_rows
            if row.get(inner_col_actual) is not None
        ]
        overlap_limit = min(
            3,
            max(0, len(outer_rows) - 1),
            len(inner_values),
        )
        for index, value in enumerate(inner_values[:overlap_limit]):
            outer_rows[index][outer_col_actual] = value

        # Membership obligations require a negative outer path as well as an
        # overlap. Reserve the final row explicitly, including at the minimum
        # three-row witness scale.
        if len(outer_rows) > 1:
            inner_value_set = set(inner_values)
            non_match = _seed_value(outer_col_actual, len(outer_rows) + 100)
            while non_match is None or non_match in inner_value_set:
                non_match = _counter_value(outer_col_actual, non_match)
            outer_rows[-1][outer_col_actual] = non_match


def _correlated_subquery_column_pairs(
    standard_sql: str,
    student_sql: str,
) -> list[tuple[tuple[str, str], tuple[str, str]]]:
    """Return physical outer/inner column pairs from correlated predicates."""
    correlations: list[tuple[tuple[str, str], tuple[str, str]]] = []
    for sql in (standard_sql, student_sql):
        for outer_ref, inner_ref, _inner in _correlated_subquery_links(sql):
            pair = (outer_ref, inner_ref)
            if pair not in correlations:
                correlations.append(pair)
    return correlations


def _correlated_subquery_links(
    sql: str | exp.Expression,
) -> list[tuple[tuple[str, str], tuple[str, str], exp.Select]]:
    """Return scope-resolved correlation links for one SQL statement."""

    ast = sql if isinstance(sql, exp.Expression) else _parse_sql(sql)
    if ast is None:
        return []
    links: list[tuple[tuple[str, str], tuple[str, str], exp.Select]] = []
    seen_inner_nodes: set[int] = set()
    nested_nodes = list(ast.find_all(exp.Subquery)) + list(ast.find_all(exp.Exists))
    for nested in nested_nodes:
        inner = nested.this if isinstance(nested.this, exp.Select) else None
        outer = nested.find_ancestor(exp.Select)
        if not isinstance(inner, exp.Select) or not isinstance(outer, exp.Select):
            continue
        if id(inner) in seen_inner_nodes or not _subquery_is_correlated(inner):
            continue
        seen_inner_nodes.add(id(inner))
        for comparison in inner.find_all(
            exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE
        ):
            if comparison.find_ancestor(exp.Select) is not inner:
                continue
            refs = _correlation_refs(comparison, inner)
            if refs is None:
                continue
            outer_ref, inner_ref = refs
            link = (outer_ref, inner_ref, inner)
            if not any(existing[:2] == link[:2] for existing in links):
                links.append(link)
    return links


def _correlation_refs(
    comparison: exp.Expression,
    inner: exp.Select,
) -> tuple[tuple[str, str], tuple[str, str]] | None:
    """Resolve one local/outer column pair across every ancestor scope.

    The returned references intentionally use physical table names so they can
    address fixture rows.  Scope classification is performed separately using
    visible qualifiers; this is essential for self-correlations such as
    ``FROM employee x WHERE x.dept = employee.dept`` where both sides map to
    the same physical table but belong to different query blocks.
    """
    columns = [
        side
        for side in (comparison.left, comparison.right)
        if isinstance(side, exp.Column)
    ]
    if len(columns) != 2:
        return None
    ancestors = _ancestor_selects(inner)
    local_candidates: list[tuple[exp.Column, tuple[str, str]]] = []
    outer_candidates: list[tuple[exp.Column, tuple[str, str]]] = []
    for column in columns:
        local_ref = _scope_column_ref(column, inner)
        if local_ref is not None:
            local_candidates.append((column, local_ref))
            continue
        outer_ref = next(
            (
                ref
                for ancestor in ancestors
                if (ref := _scope_column_ref(column, ancestor)) is not None
            ),
            None,
        )
        if outer_ref is not None:
            outer_candidates.append((column, outer_ref))
    if len(local_candidates) == 1 and len(outer_candidates) == 1:
        return outer_candidates[0][1], local_candidates[0][1]
    return None


def _correlation_comparison(
    select: exp.Select,
    outer_ref: tuple[str, str],
    inner_ref: tuple[str, str],
) -> exp.Expression | None:
    for comparison in select.find_all(
        exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE
    ):
        if comparison.find_ancestor(exp.Select) is not select:
            continue
        if _correlation_refs(comparison, select) == (outer_ref, inner_ref):
            return comparison
    return None


def _materialize_correlated_key_drift_witness(
    data: dict[str, list[dict[str, Any]]],
    standard_sql: str,
    student_sql: str,
) -> bool:
    """Create a standard-only EXISTS path when the correlated key changed."""

    standard_links = _correlated_subquery_links(standard_sql)
    student_links = _correlated_subquery_links(student_sql)
    for standard_outer, standard_inner, standard_select in standard_links:
        student_match = next(
            (
                (student_outer, student_inner, student_select)
                for student_outer, student_inner, student_select in student_links
                if (
                    student_outer == standard_outer
                    and student_inner[0] == standard_inner[0]
                    and student_inner[1] != standard_inner[1]
                )
                or (
                    student_inner == standard_inner
                    and student_outer != standard_outer
                )
            ),
            None,
        )
        if student_match is None:
            continue
        student_outer, student_inner, student_select = student_match
        outer_actual = _actual_data_ref(data, standard_outer)
        standard_actual = _actual_data_ref(data, standard_inner)
        student_actual = _actual_data_ref(data, student_inner)
        if not outer_actual or not standard_actual or not student_actual:
            continue
        outer_rows, outer_column = outer_actual
        inner_rows, standard_column = standard_actual
        student_rows, student_column = student_actual
        if not outer_rows or not inner_rows or inner_rows is not student_rows:
            continue

        if student_outer != standard_outer and student_inner == standard_inner:
            student_outer_actual = _actual_data_ref(data, student_outer)
            if not student_outer_actual:
                continue
            wrong_outer_rows, wrong_outer_column = student_outer_actual
            if not wrong_outer_rows:
                continue
            parent_link = next(
                (
                    (parent_outer, parent_inner)
                    for parent_outer, parent_inner, _parent_select in standard_links
                    if parent_outer == student_outer
                    and parent_inner[0] == standard_outer[0]
                ),
                None,
            )
            if standard_outer[0] != student_outer[0] and parent_link is None:
                continue
            parent_outer_actual = (
                _actual_data_ref(data, parent_link[0]) if parent_link else None
            )
            parent_inner_actual = (
                _actual_data_ref(data, parent_link[1]) if parent_link else None
            )
            if parent_link and (not parent_outer_actual or not parent_inner_actual):
                continue

            used_inner = {
                row.get(standard_column)
                for row in inner_rows
                if row.get(standard_column) is not None
            }
            used_standard_outer = {
                row.get(outer_column)
                for row in outer_rows
                if row.get(outer_column) is not None
            }
            used_wrong_outer = {
                row.get(wrong_outer_column)
                for row in wrong_outer_rows
                if row.get(wrong_outer_column) is not None
            }

            anchor = _counter_value(
                outer_column,
                outer_rows[0].get(outer_column),
            )
            while anchor is None or anchor in used_inner or anchor in used_standard_outer:
                anchor = _counter_value(outer_column, anchor)
            bridge = _counter_value(
                wrong_outer_column,
                wrong_outer_rows[0].get(wrong_outer_column),
            )
            while (
                bridge is None
                or bridge == anchor
                or bridge in used_inner
                or bridge in used_wrong_outer
            ):
                bridge = _counter_value(wrong_outer_column, bridge)

            with write_owner("materializer:correlated_outer_key_drift"):
                outer_rows[0][outer_column] = anchor
                inner_rows[0][standard_column] = anchor
                wrong_outer_rows[0][wrong_outer_column] = bridge
                if parent_outer_actual and parent_inner_actual:
                    parent_outer_rows, parent_outer_column = parent_outer_actual
                    parent_inner_rows, parent_inner_column = parent_inner_actual
                    parent_outer_rows[0][parent_outer_column] = bridge
                    parent_inner_rows[0][parent_inner_column] = bridge
                _set_select_local_literal_predicates(data, standard_select, 0)
                _set_select_local_literal_predicates(data, student_select, 0)
            return True

        anchor = outer_rows[0].get(outer_column)
        if anchor is None:
            anchor = _seed_value(outer_column, 0)
        with write_owner("materializer:correlated_key_drift"):
            outer_rows[0][outer_column] = anchor
            inner_rows[0][standard_column] = anchor
            # Materialize all inner-local filters (for example Total > 10)
            # on the standard-only matching row before excluding the wrong
            # student key.
            _set_select_local_literal_predicates(data, standard_select, 0)
            _set_select_local_literal_predicates(data, student_select, 0)

            used_student_values = {
                row.get(student_column)
                for row in student_rows
                if row.get(student_column) is not None
                and row.get(student_column) != anchor
            }
            for row in student_rows:
                if row.get(student_column) != anchor:
                    continue
                candidate = _counter_value(student_column, anchor)
                while (
                    candidate is None
                    or candidate == anchor
                    or candidate in used_student_values
                ):
                    candidate = _counter_value(student_column, candidate)
                row[student_column] = candidate
                used_student_values.add(candidate)
        return True
    return False


def _materialize_correlated_scalar_aggregate_key_drift_witness(
    data: dict[str, list[dict[str, Any]]],
    standard_sql: str,
    student_sql: str,
    ast_diffs: list[ASTDiffNode],
    *,
    schema_catalog: SchemaCatalog | None = None,
) -> bool:
    """Create a standard-only scalar aggregate path for a wrong outer key.

    The ordinary correlation materializer proves key overlap, which is enough
    for EXISTS but not for ``salary > (SELECT AVG(...))``: a one-row group has
    the same value as the outer row.  This bounded form creates exactly two
    rows in the standard group and keeps the student's changed outer key
    absent from that group.  It accepts one direct aggregate, one correlation
    link and no grouped/limited inner query; broader shapes remain undecided.
    """
    diff = next(
        (
            item
            for item in ast_diffs
            if item.diff_type == "correlated_predicate_changed"
            and item.extra.get("query_scope") == "nested_correlation"
        ),
        None,
    )
    if diff is None:
        return False
    metadata = diff.extra
    standard_outer = (
        _norm_name(str(metadata.get("standard_source_table") or "")),
        _norm_name(str(metadata.get("standard_outer_column") or "")),
    )
    standard_inner = (
        _norm_name(str(metadata.get("standard_membership_table") or "")),
        _norm_name(str(metadata.get("standard_membership_column") or "")),
    )
    student_outer = (
        _norm_name(str(metadata.get("student_source_table") or "")),
        _norm_name(str(metadata.get("student_outer_column") or "")),
    )
    student_inner = (
        _norm_name(str(metadata.get("student_membership_table") or "")),
        _norm_name(str(metadata.get("student_membership_column") or "")),
    )
    if (
        not all((*standard_outer, *standard_inner, *student_outer, *student_inner))
        or standard_inner != student_inner
        or standard_outer == student_outer
        or _catalog_has_unary_unique_key(schema_catalog, standard_inner)
    ):
        return False

    standard_ast = _parse_sql(standard_sql)
    student_ast = _parse_sql(student_sql)
    if standard_ast is None or student_ast is None:
        return False
    standard_links = [
        item
        for item in _correlated_subquery_links(standard_ast)
        if item[0] == standard_outer and item[1] == standard_inner
    ]
    student_links = [
        item
        for item in _correlated_subquery_links(student_ast)
        if item[0] == student_outer and item[1] == student_inner
    ]
    if len(standard_links) != 1 or len(student_links) != 1:
        return False
    inner_select = standard_links[0][2]
    if any(
        inner_select.args.get(key) is not None
        for key in (
            "group", "having", "order", "limit", "offset",
            "distinct", "with", "with_",
        )
    ) or inner_select.args.get("joins"):
        return False
    aggregate_nodes = [
        node
        for node in inner_select.find_all(exp.Avg, exp.Sum, exp.Min, exp.Max)
        if node.find_ancestor(exp.Select) is inner_select
    ]
    if len(aggregate_nodes) != 1:
        return False
    aggregate = aggregate_nodes[0]
    if aggregate.find(exp.Window) is not None or aggregate.args.get("distinct"):
        return False
    measure = aggregate.find(exp.Column)
    if not isinstance(measure, exp.Column):
        return False

    subquery = next(
        (
            node
            for node in standard_ast.find_all(exp.Subquery)
            if node.this is inner_select
        ),
        None,
    )
    if not isinstance(subquery, exp.Subquery):
        return False
    comparison = subquery.find_ancestor(
        exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE
    )
    if not isinstance(comparison, (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE)):
        return False
    parts = _comparison_subquery_parts(comparison)
    if parts is None or parts[0] is not subquery:
        return False
    outer_column = parts[1]
    outer_select = comparison.find_ancestor(exp.Select)
    if not isinstance(outer_select, exp.Select):
        return False
    outer_measure_ref = _scope_column_ref(outer_column, outer_select)
    measure_ref = _scope_column_ref(measure, inner_select)
    if outer_measure_ref is None or measure_ref is None:
        return False

    standard_outer_actual = _actual_data_ref(data, standard_outer)
    standard_inner_actual = _actual_data_ref(data, standard_inner)
    student_outer_actual = _actual_data_ref(data, student_outer)
    outer_measure_actual = _actual_data_ref(data, outer_measure_ref)
    measure_actual = _actual_data_ref(data, measure_ref)
    if not all(
        (
            standard_outer_actual,
            standard_inner_actual,
            student_outer_actual,
            outer_measure_actual,
            measure_actual,
        )
    ):
        return False
    outer_rows, outer_key_column = standard_outer_actual
    inner_rows, inner_key_column = standard_inner_actual
    wrong_outer_rows, wrong_outer_column = student_outer_actual
    measure_outer_rows, outer_measure_column = outer_measure_actual
    measure_rows, measure_column = measure_actual
    if (
        not outer_rows
        or len(inner_rows) < 2
        or not wrong_outer_rows
        or not measure_outer_rows
        or len(measure_rows) < 2
        or outer_rows is not measure_outer_rows
        or inner_rows is not measure_rows
    ):
        return False
    if inner_key_column == measure_column:
        return False
    if outer_rows is wrong_outer_rows and wrong_outer_column == outer_measure_column:
        return False
    if outer_rows is inner_rows and outer_key_column == outer_measure_column:
        return False

    operator = type(comparison).__name__.upper()
    outer_on_left = comparison.left is outer_column
    if not outer_on_left:
        operator = {
            "GT": "LT",
            "GTE": "LTE",
            "LT": "GT",
            "LTE": "GTE",
            "EQ": "EQ",
            "NEQ": "NEQ",
        }[operator]
    shared_measure = outer_rows is inner_rows and outer_measure_column == measure_column

    def aggregate_values() -> tuple[Any, Any, Any] | None:
        """Return outer value and two aggregate-measure values."""
        if not shared_measure:
            outer_value = {
                "GT": 60, "GTE": 60, "LT": 40, "LTE": 40,
                "EQ": 50, "NEQ": 60,
            }[operator]
            if isinstance(aggregate, exp.Avg):
                return outer_value, 40, 60
            if isinstance(aggregate, exp.Sum):
                return outer_value, 20, 30
            if isinstance(aggregate, exp.Max):
                return outer_value, 50, 40
            return outer_value, 50, 60
        if isinstance(aggregate, exp.Avg):
            if operator == "EQ":
                return 50, 50, 50
            return (40, 40, 60) if operator in {"LT", "LTE"} else (60, 60, 40)
        if isinstance(aggregate, exp.Sum):
            if operator == "EQ":
                return 50, 50, 0
            if operator in {"LT", "LTE"}:
                return 40, 40, 20
            return 60, 60, -20 if operator in {"GT", "GTE"} else 10
        if isinstance(aggregate, exp.Max):
            if operator == "GT":
                return None
            if operator in {"GTE", "EQ"}:
                return 50, 50, 40
            return 40, 40, 60
        if operator == "LT":
            return None
        if operator in {"LTE", "EQ"}:
            return 50, 50, 60
        return 60, 60, 40

    values = aggregate_values()
    if values is None:
        return False
    outer_value, first_measure, second_measure = values

    wrong_value = wrong_outer_rows[0].get(wrong_outer_column)
    if wrong_value is None:
        wrong_value = _seed_value(wrong_outer_column, 0)
    anchor = _counter_value(inner_key_column, wrong_value)
    while anchor is None or anchor == wrong_value:
        anchor = _counter_value(inner_key_column, anchor)

    with write_owner("materializer:correlated_scalar_aggregate_key_drift"):
        _set_select_local_literal_predicates(data, inner_select, 0)
        _set_select_local_literal_predicates(data, inner_select, 1)
        _set_select_local_literal_predicates(data, outer_select, 0)
        outer_rows[0][outer_key_column] = anchor
        inner_rows[0][inner_key_column] = anchor
        inner_rows[1][inner_key_column] = anchor
        wrong_outer_rows[0][wrong_outer_column] = wrong_value
        for index, row in enumerate(inner_rows[2:], start=2):
            candidate = row.get(inner_key_column)
            while candidate is None or candidate in {anchor, wrong_value}:
                candidate = _counter_value(inner_key_column, candidate)
            row[inner_key_column] = candidate
        measure_outer_rows[0][outer_measure_column] = outer_value
        measure_rows[0][measure_column] = first_measure
        measure_rows[1][measure_column] = second_measure
    return True


def _correlated_local_true_value(
    comparison: exp.Expression,
    inner_select: exp.Select,
    inner_ref: tuple[str, str],
    outer_value: Any,
) -> Any | None:
    """Choose a local column value that makes a column correlation TRUE."""
    if not isinstance(
        comparison,
        (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE),
    ):
        return None
    left_local = (
        isinstance(comparison.left, exp.Column)
        and _column_ref_in_select(comparison.left, inner_select) == inner_ref
    )
    right_local = (
        isinstance(comparison.right, exp.Column)
        and _column_ref_in_select(comparison.right, inner_select) == inner_ref
    )
    if left_local == right_local:
        return None
    operator = type(comparison).__name__.upper()
    if right_local:
        operator = {
            "GT": "LT",
            "GTE": "LTE",
            "LT": "GT",
            "LTE": "GTE",
            "EQ": "EQ",
            "NEQ": "NEQ",
        }[operator]
    if operator == "EQ":
        return outer_value
    if operator == "NEQ":
        return _counter_value(inner_ref[1], outer_value)
    if not isinstance(outer_value, (int, float, Decimal)):
        return None
    if operator == "GT":
        return outer_value + 1
    if operator == "GTE":
        return outer_value
    if operator == "LT":
        return outer_value - 1
    if operator == "LTE":
        return outer_value
    return None


def _materialize_subquery_membership_key_drift_witness(
    data: dict[str, list[dict[str, Any]]],
    ast_diffs: list[ASTDiffNode],
    standard_sql: str,
) -> bool:
    """Create a standard-only path for a changed nested IN lhs column."""
    diff = next(
        (
            item
            for item in ast_diffs
            if item.diff_type == "subquery_membership_key_changed"
        ),
        None,
    )
    if diff is None:
        return False
    metadata = diff.extra
    standard_outer = (
        _norm_name(str(metadata.get("standard_source_table") or "")),
        _norm_name(str(metadata.get("standard_outer_column") or "")),
    )
    student_outer = (
        _norm_name(str(metadata.get("student_source_table") or "")),
        _norm_name(str(metadata.get("student_outer_column") or "")),
    )
    inner_ref = (
        _norm_name(str(metadata.get("standard_membership_table") or "")),
        _norm_name(str(metadata.get("standard_membership_column") or "")),
    )
    standard_actual = _actual_data_ref(data, standard_outer)
    student_actual = _actual_data_ref(data, student_outer)
    inner_actual = _actual_data_ref(data, inner_ref)
    if not standard_actual or not student_actual or not inner_actual:
        return False
    outer_rows, standard_column = standard_actual
    student_rows, student_column = student_actual
    inner_rows, inner_column = inner_actual
    if (
        not outer_rows
        or outer_rows is not student_rows
        or not inner_rows
    ):
        return False

    standard_ast = _parse_sql(standard_sql)
    if standard_ast is None:
        return False
    changed_in: exp.In | None = None
    membership_select: exp.Select | None = None
    membership_inner: exp.Select | None = None
    for node in standard_ast.find_all(exp.In):
        select = node.find_ancestor(exp.Select)
        query = node.args.get("query")
        inner = query.this if isinstance(query, exp.Subquery) else None
        if not (
            isinstance(node.this, exp.Column)
            and isinstance(select, exp.Select)
            and isinstance(inner, exp.Select)
        ):
            continue
        projected = inner.expressions[0] if inner.expressions else None
        projected = projected.this if isinstance(projected, exp.Alias) else projected
        if not isinstance(projected, exp.Column):
            continue
        if (
            _column_ref_in_select(node.this, select) == standard_outer
            and _column_ref_in_select(projected, inner) == inner_ref
        ):
            changed_in = node
            membership_select = select
            membership_inner = inner
            break
    if changed_in is None or membership_select is None or membership_inner is None:
        return False

    root_in: exp.In | None = None
    parent = changed_in.parent
    while isinstance(parent, exp.Expression):
        if isinstance(parent, exp.In):
            root_in = parent
            break
        parent = parent.parent
    root_actual: tuple[list[dict[str, Any]], str] | None = None
    if root_in is not None and isinstance(root_in.this, exp.Column):
        root_select = root_in.find_ancestor(exp.Select)
        if isinstance(root_select, exp.Select):
            root_ref = _column_ref_in_select(root_in.this, root_select)
            root_actual = _actual_data_ref(data, root_ref) if root_ref else None

    bridge = student_rows[0].get(student_column)
    if root_actual:
        root_rows, root_column = root_actual
        if root_rows:
            bridge = root_rows[0].get(root_column)
    if bridge is None:
        bridge = _seed_value(student_column, 0)
    used_inner = {
        row.get(inner_column)
        for row in inner_rows
        if row.get(inner_column) is not None
    }
    used_standard = {
        row.get(standard_column)
        for row in outer_rows
        if row.get(standard_column) is not None
    }
    anchor = _counter_value(standard_column, outer_rows[0].get(standard_column))
    while (
        anchor is None
        or anchor == bridge
        or anchor in used_inner
        or anchor in used_standard
    ):
        anchor = _counter_value(standard_column, anchor)

    with write_owner("materializer:subquery_membership_key_drift"):
        outer_rows[0][standard_column] = anchor
        student_rows[0][student_column] = bridge
        inner_rows[0][inner_column] = anchor
        if root_actual:
            root_rows, root_column = root_actual
            root_rows[0][root_column] = bridge
        for index, row in enumerate(inner_rows[1:], start=1):
            if row.get(inner_column) == bridge:
                candidate = _counter_value(inner_column, bridge)
                while candidate in {anchor, bridge} or candidate in used_inner:
                    candidate = _counter_value(inner_column, candidate)
                row[inner_column] = candidate
                used_inner.add(candidate)
        for comparison in membership_inner.find_all(
            exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE
        ):
            if comparison.find_ancestor(exp.Select) is not membership_inner:
                continue
            refs = _correlation_refs(comparison, membership_inner)
            if refs is None:
                continue
            correlation_outer, correlation_inner = refs
            outer_value_actual = _actual_data_ref(data, correlation_outer)
            local_actual = _actual_data_ref(data, correlation_inner)
            if not outer_value_actual or not local_actual:
                continue
            correlation_outer_rows, correlation_outer_column = outer_value_actual
            local_rows, local_column = local_actual
            if not correlation_outer_rows or not local_rows:
                continue
            true_value = _correlated_local_true_value(
                comparison,
                membership_inner,
                correlation_inner,
                correlation_outer_rows[0].get(correlation_outer_column),
            )
            if true_value is not None:
                local_rows[0][local_column] = true_value
    return True


def _materialize_subquery_comparison_boundary_witness(
    data: dict[str, list[dict[str, Any]]],
    standard_sql: str,
    student_sql: str,
) -> bool:
    """Keep an IN-subquery boundary key exclusive to one predicate result."""

    standard_ast = _parse_sql(standard_sql)
    student_ast = _parse_sql(student_sql)
    if standard_ast is None or student_ast is None:
        return False
    standard_in_nodes = list(standard_ast.find_all(exp.In))
    student_in_nodes = list(student_ast.find_all(exp.In))
    comparison_types = (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE)
    for standard_in, student_in in zip(standard_in_nodes, student_in_nodes):
        standard_query = standard_in.args.get("query")
        student_query = student_in.args.get("query")
        standard_inner = (
            standard_query.this
            if isinstance(standard_query, exp.Subquery)
            and isinstance(standard_query.this, exp.Select)
            else None
        )
        student_inner = (
            student_query.this
            if isinstance(student_query, exp.Subquery)
            and isinstance(student_query.this, exp.Select)
            else None
        )
        standard_outer = standard_in.find_ancestor(exp.Select)
        if (
            not isinstance(standard_in.this, exp.Column)
            or not isinstance(standard_inner, exp.Select)
            or not isinstance(student_inner, exp.Select)
            or not isinstance(standard_outer, exp.Select)
            or not standard_inner.expressions
        ):
            continue
        projected = standard_inner.expressions[0]
        projected = projected.this if isinstance(projected, exp.Alias) else projected
        if not isinstance(projected, exp.Column):
            continue

        standard_comparisons = [
            node
            for node in standard_inner.find_all(*comparison_types)
            if node.find_ancestor(exp.Select) is standard_inner
            and isinstance(node.left, exp.Column)
            and isinstance(node.right, exp.Literal)
        ]
        student_comparisons = [
            node
            for node in student_inner.find_all(*comparison_types)
            if node.find_ancestor(exp.Select) is student_inner
            and isinstance(node.left, exp.Column)
            and isinstance(node.right, exp.Literal)
        ]
        changed_pair = next(
            (
                (standard_comparison, student_comparison)
                for standard_comparison in standard_comparisons
                for student_comparison in student_comparisons
                if _norm_name(standard_comparison.left.name)
                == _norm_name(student_comparison.left.name)
                and _literal_value(standard_comparison.right)
                == _literal_value(student_comparison.right)
                and type(standard_comparison) is not type(student_comparison)
            ),
            None,
        )
        if changed_pair is None:
            continue
        standard_comparison, student_comparison = changed_pair
        candidate_values = [
            _comparison_truth_value(comparison, desired)
            for comparison in changed_pair
            for desired in (True, False)
        ]
        boundary_value = next(
            (
                value
                for value in candidate_values
                if value is not None
                and _comparison_matches(standard_comparison, value)
                != _comparison_matches(student_comparison, value)
            ),
            None,
        )
        if boundary_value is None:
            continue

        outer_ref = _column_ref_in_select(standard_in.this, standard_outer)
        projected_ref = _column_ref_in_select(projected, standard_inner)
        predicate_ref = _column_ref_in_select(
            standard_comparison.left,
            standard_inner,
        )
        outer_actual = _actual_data_ref(data, outer_ref) if outer_ref else None
        projected_actual = (
            _actual_data_ref(data, projected_ref) if projected_ref else None
        )
        predicate_actual = (
            _actual_data_ref(data, predicate_ref) if predicate_ref else None
        )
        if not outer_actual or not projected_actual or not predicate_actual:
            continue
        outer_rows, outer_column = outer_actual
        inner_rows, projected_column = projected_actual
        predicate_rows, predicate_column = predicate_actual
        if (
            not outer_rows
            or not inner_rows
            or inner_rows is not predicate_rows
            or outer_rows is inner_rows
        ):
            continue

        anchor = outer_rows[0].get(outer_column)
        if projected_column == predicate_column:
            anchor = boundary_value
        if anchor is None:
            anchor = _seed_value(outer_column, 0)
        with write_owner("materializer:subquery_comparison_boundary"):
            outer_rows[0][outer_column] = anchor
            _set_select_local_literal_predicates(data, standard_inner, 0)
            _set_select_local_literal_predicates(data, student_inner, 0)
            inner_rows[0][projected_column] = anchor
            inner_rows[0][predicate_column] = boundary_value

            used_projection_values = {
                row.get(projected_column)
                for row in inner_rows
                if row.get(projected_column) is not None
                and row.get(projected_column) != anchor
            }
            for row in inner_rows[1:]:
                if row.get(projected_column) != anchor:
                    continue
                replacement = _counter_value(projected_column, anchor)
                while (
                    replacement is None
                    or replacement == anchor
                    or replacement in used_projection_values
                ):
                    replacement = _counter_value(projected_column, replacement)
                row[projected_column] = replacement
                used_projection_values.add(replacement)
        return True
    return False


def _direct_select_tables(select: exp.Select) -> dict[str, str]:
    """Return aliases for physical tables owned by this SELECT scope."""
    aliases: dict[str, str] = {}
    for table in select.find_all(exp.Table):
        if table.find_ancestor(exp.Select) is not select:
            continue
        name = _norm_name(table.name)
        if not name:
            continue
        aliases[name] = name
        if table.alias:
            aliases[_norm_name(table.alias)] = name
    return aliases


def _select_scope_bindings(select: exp.Select) -> dict[str, str]:
    """Map SQL-visible table qualifiers to physical table names.

    ``_direct_select_tables`` is intentionally permissive for legacy fixture
    lookup and exposes both a table name and its alias.  Scope analysis needs
    the stricter SQL rule: once a table has an alias, that alias is the local
    qualifier and the original table name is no longer a local binding.
    """
    bindings: dict[str, str] = {}
    for table in select.find_all(exp.Table):
        if table.find_ancestor(exp.Select) is not select:
            continue
        physical = _norm_name(table.name)
        qualifier = _norm_name(table.alias or table.name)
        if physical and qualifier:
            bindings[qualifier] = physical
    return bindings


def _select_scope_qualifiers(select: exp.Select) -> set[str]:
    return set(_select_scope_bindings(select))


def _scope_column_ref(
    column: exp.Column,
    select: exp.Select,
) -> tuple[str, str] | None:
    """Resolve a column only against qualifiers local to ``select``."""
    bindings = _select_scope_bindings(select)
    qualifier = _norm_name(column.table or "")
    if qualifier:
        table_name = bindings.get(qualifier)
        return (table_name, _norm_name(column.name)) if table_name else None
    physical_tables = list(dict.fromkeys(bindings.values()))
    if len(physical_tables) != 1:
        return None
    return physical_tables[0], _norm_name(column.name)


def _column_ref_in_select(
    column: exp.Column,
    select: exp.Select,
) -> tuple[str, str] | None:
    aliases = _direct_select_tables(select)
    table_ref = _norm_name(column.table or "")
    if table_ref:
        table_name = aliases.get(table_ref)
    else:
        physical_tables = list(dict.fromkeys(aliases.values()))
        table_name = physical_tables[0] if len(physical_tables) == 1 else None
    if not table_name:
        return None
    return table_name, _norm_name(column.name)


def _column_ref_in_select_data(
    data: dict[str, list[dict[str, Any]]],
    column: exp.Column,
    select: exp.Select,
) -> tuple[str, str] | None:
    """Resolve a SELECT-local column against the materialized physical data.

    The legacy resolver intentionally refuses every unqualified reference in
    a multi-table block.  For witness generation we can safely do better when
    the authoritative table shapes prove that exactly one direct table owns
    the column.  Ambiguous and outer-scope references remain unresolved.
    """
    resolved = _column_ref_in_select(column, select)
    if resolved is not None:
        return resolved
    if column.table:
        return None

    column_name = _norm_name(column.name)
    direct_tables = set(_direct_select_tables(select).values())
    candidates: list[tuple[str, str]] = []
    for table_name, rows in data.items():
        normalized_table = _norm_name(table_name)
        if normalized_table not in direct_tables or not rows:
            continue
        if any(_norm_name(name) == column_name for name in rows[0]):
            candidates.append((normalized_table, column_name))
    return candidates[0] if len(candidates) == 1 else None


def _actual_data_ref(
    data: dict[str, list[dict[str, Any]]],
    ref: tuple[str, str],
) -> tuple[list[dict[str, Any]], str] | None:
    table_ref, column_ref = ref
    rows = next((rows for table, rows in data.items() if _norm_name(table) == table_ref), None)
    if not rows:
        return None
    column = next((name for name in rows[0] if _norm_name(name) == column_ref), None)
    if not column:
        return None
    return rows, column


def _set_select_local_literal_predicates(
    data: dict[str, list[dict[str, Any]]],
    select: exp.Select,
    row_index: int,
) -> None:
    where = select.args.get("where")
    if not isinstance(where, exp.Where):
        return
    for comparison in where.find_all(exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE):
        if comparison.find_ancestor(exp.Select) is not select:
            continue
        column = comparison.left if isinstance(comparison.left, exp.Column) else None
        literal = comparison.right if isinstance(comparison.right, exp.Literal) else None
        if not column or not literal:
            continue
        ref = _column_ref_in_select_data(data, column, select)
        actual = _actual_data_ref(data, ref) if ref else None
        if not actual:
            continue
        rows, column_name = actual
        if row_index >= len(rows):
            continue
        value = _comparison_truth_value(comparison, True)
        if value is not None:
            rows[row_index][column_name] = value


def _set_select_local_rich_predicates(
    data: dict[str, list[dict[str, Any]]],
    select: exp.Select,
    row_index: int,
) -> None:
    """Make a SELECT-local row satisfy simple wrapped predicates.

    This is used only by bounded DISTINCT/reachability adapters.  It does not
    claim that arbitrary expressions are writable; unresolved or multi-column
    predicates remain untouched and are reported by the normal evidence gate.
    """
    where = select.args.get("where")
    if not isinstance(where, exp.Where):
        return
    predicate_types = (
        exp.Not,
        exp.EQ,
        exp.NEQ,
        exp.GT,
        exp.GTE,
        exp.LT,
        exp.LTE,
        exp.Like,
        exp.Is,
        exp.In,
        exp.Between,
    )
    for predicate in where.find_all(*predicate_types):
        if predicate.find_ancestor(exp.Select) is not select:
            continue
        # A child of NOT is owned by the NOT node; processing both would
        # immediately overwrite the intended negative/positive assignment.
        if not isinstance(predicate, exp.Not) and predicate.find_ancestor(exp.Not) is not None:
            continue
        resolved = _rich_predicate_truth_value(predicate, True)
        if resolved is None:
            continue
        column, value = resolved
        ref = _column_ref_in_select_data(data, column, select)
        actual = _actual_data_ref(data, ref) if ref else None
        if actual is None or row_index >= len(actual[0]):
            continue
        actual[0][row_index][actual[1]] = value


def _query_cte_select(
    root_ast: exp.Expression,
    relation_name: str,
) -> exp.Select | None:
    relation = _norm_name(relation_name)
    if not relation:
        return None
    for cte in root_ast.find_all(exp.CTE):
        if _norm_name(cte.alias or "") != relation:
            continue
        body = cte.this
        return body if isinstance(body, exp.Select) else None
    return None


def _query_source_alias(source: exp.Expression) -> str:
    if isinstance(source, exp.Table):
        return _norm_name(source.alias or source.name)
    if isinstance(source, exp.Subquery):
        return _norm_name(source.alias or "")
    return ""


def _query_block_sources(select: exp.Select) -> list[tuple[str, exp.Expression]]:
    from_clause = select.args.get("from_") or select.args.get("from")
    sources: list[tuple[str, exp.Expression]] = []
    if isinstance(from_clause, exp.From) and isinstance(from_clause.this, exp.Expression):
        alias = _query_source_alias(from_clause.this)
        if alias:
            sources.append((alias, from_clause.this))
    for join in select.args.get("joins") or ():
        source = join.this
        if not isinstance(source, exp.Expression):
            continue
        alias = _query_source_alias(source)
        if alias:
            sources.append((alias, source))
    return sources


def _query_source_select(
    source: exp.Expression,
    root_ast: exp.Expression,
) -> exp.Select | None:
    if isinstance(source, exp.Subquery) and isinstance(source.this, exp.Select):
        return source.this
    if isinstance(source, exp.Table):
        return _query_cte_select(root_ast, source.name)
    return None
