"""Higher-level witness materialization and stabilization."""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from collections import Counter, defaultdict
import re
from sqlglot import exp
from core.ast_schema import ASTDiffNode
from core.witness_generation.schema_scope import SchemaCatalog
from core.witness_generation.obligations import (
    ConstraintSpec,
    DistinguishingObligation,
)
from core.witness_generation.planner import write_owner

from core.phase1_foundation import (
    _AGG_FUNC_TYPES,
    _MAX_WITNESS_ROWS_PER_TABLE,
    _MISSING,
    _aggregate_distinct_probe_value,
    _comparison_node_from_diff,
    _direct_from_table,
    _distinct_shape_changed,
    _existing_order_pair_indexes,
    _has_diff,
    _has_set_operator,
    _literal_value,
    _materialized_order_keys,
    _nearest_select,
    _parse_sql,
    _predicate_source_column,
    _semantic_literal_value,
    _set_operator_node,
    _sql_of,
    _static_predicate_scalar,
    _strict_path_variant,
    _top_select,
    _unwrap_paren,
)

from core.phase1_sql_semantics import (
    _catalog_has_unary_unique_key,
    _comparison_matches,
    _comparison_truth_value,
    _counter_value,
    _group_probe_value,
    _is_date_column,
    _is_key_column,
    _is_numeric_column,
    _norm_name,
    _order_materializer_values,
    _ordered_distinct_pair,
    _rich_predicate_truth_value,
    _seed_value,
    _simple_materialized_order_column,
    _table_key_aliases,
    _temporal_value_for_comparison,
    _unique_key_value,
)

from core.phase1_constraints import (
    _aggregate_distinct_target_column,
    _apply_distinct_cte_case_sum_probe,
    _apply_distinct_self_join_path_probe,
    _apply_grouped_distinct_probe,
    _apply_select_distinct_group_probe,
    _column_lookup,
    _column_ref,
    _join_on_column_pairs,
    _table_aliases,
)

from core.phase1_query_paths import (
    _actual_data_ref,
    _column_ref_in_select,
    _column_ref_in_select_data,
    _correlated_subquery_links,
    _direct_select_tables,
    _query_block_sources,
    _query_cte_select,
    _query_source_select,
    _set_select_local_literal_predicates,
    _set_select_local_rich_predicates,
)

from core.phase1_witness_strategies import (
    _is_primary_key_candidate,
    _primary_key_candidate,
    _query_block_equality_edges,
    _query_block_predicate_values,
    _query_column_ref_in_data,
    _query_structural_lineage_refs,
    _select_local_scalar_predicates,
    _set_query_block_rich_predicates,
)



def _materialize_distinct_join_projection_witness(
    data: dict[str, list[dict[str, Any]]],
    standard_sql: str,
    student_sql: str,
    ast_diffs: list[ASTDiffNode],
) -> bool:
    """Create two valid join paths with the same DISTINCT projection.

    The old DISTINCT probe duplicated columns independently in every table.
    That works for a single source table, but it cannot make
    ``d.name, p.name`` equal when two fact rows still point at different
    dimension keys.  This bounded materializer chooses two rows from one
    direct source table, keeps their candidate keys unique, aligns the JOIN
    equalities to one dimension row, and then equalizes the projected payload.

    It deliberately skips self-joins, repeated physical aliases, and
    projections containing a varying primary key.  In those cases a generic
    duplicate would violate the declared relational shape and the existing
    specialized probes remain responsible for the witness.
    """
    if not any(diff.diff_type == "distinct_changed" for diff in ast_diffs):
        return False
    standard_ast = _parse_sql(standard_sql)
    student_ast = _parse_sql(student_sql)
    standard_select = _top_select(standard_ast) if standard_ast is not None else None
    student_select = _top_select(student_ast) if student_ast is not None else None
    if not isinstance(standard_select, exp.Select) or not isinstance(student_select, exp.Select):
        return False
    if not standard_select.args.get("distinct") or student_select.args.get("distinct"):
        return False
    distinct = standard_select.args.get("distinct")
    if isinstance(distinct, exp.Distinct) and distinct.args.get("on") is not None:
        return False

    source = _direct_from_table(standard_select)
    joins = list(standard_select.args.get("joins") or ())
    if not joins:
        return False
    direct_aliases = _direct_select_tables(standard_select)

    # A physical table used through two aliases needs a self-join-specific
    # path; using one row index for both aliases here can manufacture a false
    # duplicate or a Cartesian product.
    physical_aliases: dict[str, int] = defaultdict(int)
    for table_node in standard_select.find_all(exp.Table):
        if table_node.find_ancestor(exp.Select) is not standard_select:
            continue
        physical_aliases[_norm_name(table_node.name)] += 1
    if any(count > 1 for count in physical_aliases.values()):
        return False

    projected_refs: list[tuple[str, str]] = []
    for item in standard_select.expressions or ():
        expression = item.this if isinstance(item, exp.Alias) else item
        # A correlated scalar subquery is evaluated from the same joined
        # outer row.  It is not a physical cell that this adapter can write,
        # but it can still be carried by a duplicate outer path when the
        # direct projected columns are equal.  Keep those expressions in the
        # executable query and only omit them from the payload write set.
        if isinstance(expression, exp.Subquery):
            continue
        if not isinstance(expression, exp.Column):
            # Expressions may still be handled by the older probes.  This
            # materializer only claims the simple column projection shape.
            return False
        ref = _query_column_ref_in_data(
            data,
            expression,
            standard_select,
            standard_ast,
        )
        if ref is None:
            ref = _column_ref_in_select_data(data, expression, standard_select)
        if ref is None:
            return False
        projected_refs.append(ref)
    if not projected_refs:
        return False

    table_names = {
        _norm_name(name): name
        for name, rows in data.items()
        if rows
    }
    lineage_physical = {
        ref[0]
        for ref in projected_refs
        if ref[0] in table_names
    }

    projected_by_table: dict[str, set[str]] = defaultdict(set)
    for table, column in projected_refs:
        projected_by_table[table].add(column)

    edges: list[tuple[tuple[str, str], tuple[str, str]]] = []
    for join in joins:
        on = join.args.get("on")
        if not isinstance(on, exp.Expression):
            continue
        equalities = [on] if isinstance(on, exp.EQ) else list(on.find_all(exp.EQ))
        for equality in equalities:
            left_column = _predicate_source_column(equality.left)
            right_column = _predicate_source_column(equality.right)
            if left_column is None or right_column is None:
                continue
            left = _query_column_ref_in_data(
                data,
                left_column,
                standard_select,
                standard_ast,
            )
            right = _query_column_ref_in_data(
                data,
                right_column,
                standard_select,
                standard_ast,
            )
            if left is None:
                left = _column_ref_in_select_data(data, left_column, standard_select)
            if right is None:
                right = _column_ref_in_select_data(data, right_column, standard_select)
            if left is None or right is None:
                continue
            edges.append((left, right))
    if not edges:
        return False

    lineage_physical.update(ref[0] for edge in edges for ref in edge)
    lineage_physical = {
        table
        for table in lineage_physical
        if table in table_names
    }
    table_rows = {
        table: next(
            rows for name, rows in data.items() if _norm_name(name) == table
        )
        for table in lineage_physical
    }
    candidates = [
        _norm_name(source.name)
        if isinstance(source, exp.Table)
        else "",
        *sorted(lineage_physical - {
            _norm_name(source.name) if isinstance(source, exp.Table) else ""
        }),
    ]
    driver = None
    for candidate in candidates:
        actual_name = table_names.get(candidate)
        rows = data.get(actual_name or "", [])
        if len(rows) < 2:
            continue
        columns = list(rows[0])
        primary = _primary_key_candidate(columns, actual_name or candidate)
        if primary and _norm_name(primary) in projected_by_table.get(candidate, set()):
            continue
        driver = candidate
        break
    if driver is None:
        return False
    path_rows = {
        table: (0, 1) if table == driver else (0, 0)
        for table in lineage_physical
    }
    structural_refs = _query_structural_lineage_refs(data, standard_ast)

    # JOIN endpoint cells are structural, not projected payload.  Changing a
    # projected key to a marker after the equality pass can silently turn a
    # valid join into an empty result (for example ``d.BUILDING_KEY`` joined
    # through ``a`` and ``e``).  Keep the endpoint value stable and let the
    # joined path itself provide the duplicate.
    join_refs = {ref for edge in edges for ref in edge}

    def actual_column(ref: tuple[str, str]) -> tuple[list[dict[str, Any]], str] | None:
        return _actual_data_ref(data, ref)

    # Apply local literal predicates before aligning the join graph.  The
    # equality pass below is authoritative for the path keys.
    for row_index in (0, 1):
        _set_select_local_rich_predicates(data, standard_select, row_index)

    # Every equality gets one common value on both paths.  Existing typed
    # values are preferred; a small deterministic fallback handles NULL or
    # missing seed values without introducing an AST object into the row.
    with write_owner("materializer:distinct_join_projection"):
        # Resolve equality *components*, not edges independently.  In a chain
        # such as ``rooms.building_key = address.building_key`` and
        # ``buildings.building_key = address.building_key``, an edge-local
        # assignment lets the second edge overwrite the address key without
        # updating the first endpoint.  That creates a false empty join.
        parent: dict[tuple[str, str], tuple[str, str]] = {}

        def find(ref: tuple[str, str]) -> tuple[str, str]:
            parent.setdefault(ref, ref)
            if parent[ref] != ref:
                parent[ref] = find(parent[ref])
            return parent[ref]

        def union(left: tuple[str, str], right: tuple[str, str]) -> None:
            left_root, right_root = find(left), find(right)
            if left_root != right_root:
                parent[right_root] = left_root

        for left, right in edges:
            union(left, right)
        components: dict[tuple[str, str], list[tuple[str, str]]] = defaultdict(list)
        for ref in parent:
            components[find(ref)].append(ref)

        for path in (0, 1):
            for refs in components.values():
                current = None
                # A shared dimension row is the stable anchor for both
                # paths.  If the varying driver is examined first, path 1
                # would overwrite the shared row with its second key and
                # leave path 0 dangling (the classic fact/dimension witness
                # failure).
                ordered_refs = sorted(
                    refs,
                    key=lambda ref: 1 if path_rows[ref[0]] == (0, 1) else 0,
                )
                for ref in ordered_refs:
                    actual = actual_column(ref)
                    if actual is None:
                        continue
                    rows, column = actual
                    current = rows[path_rows[ref[0]][path]].get(column)
                    if current is not None:
                        break
                if current is None:
                    current = (
                        700000
                        if any(_is_numeric_column(column) for _table, column in refs)
                        else "__distinct_join_key__"
                    )
                for ref in refs:
                    actual = actual_column(ref)
                    if actual is None:
                        continue
                    rows, column = actual
                    rows[path_rows[ref[0]][path]][column] = current

        # Equalize the values that the SELECT actually observes.  A dimension
        # projection already uses the same row on both paths; a driver payload
        # needs a write to both distinct source rows.
        for table, column in projected_refs:
            actual = actual_column((table, column))
            if actual is None:
                return False
            rows, actual_column_name = actual
            first_value = rows[path_rows[table][0]].get(actual_column_name)
            if (table, column) in join_refs:
                for path in (0, 1):
                    rows[path_rows[table][path]][actual_column_name] = first_value
                continue
            if (table, column) in structural_refs:
                target_indexes = {path_rows[table][0], path_rows[table][1]}
                if len(rows) >= 2:
                    target_indexes.update({0, 1})
                for row_index in target_indexes:
                    rows[row_index][actual_column_name] = first_value
                continue
            marker = (
                777777
                if isinstance(first_value, (int, float, Decimal)) and not isinstance(first_value, bool)
                else "__distinct_join_projection__"
            )
            target_indexes = {path_rows[table][0], path_rows[table][1]}
            # A CTE/derived relation may re-materialize its physical source
            # with a different row pairing than the outer block.  Equalize a
            # non-key payload in the first two source rows as well, so the
            # duplicate survives that query-block boundary.  Structural keys
            # remain protected and are handled only through JOIN components.
            actual_table_name = next(
                (
                    name
                    for name, candidate_rows in data.items()
                    if candidate_rows is rows
                ),
                table,
            )
            if (
                len(rows) >= 2
                and not _is_key_column(actual_column_name)
                and not _is_primary_key_candidate(
                    actual_table_name,
                    actual_column_name,
                    list(rows[0]),
                )
            ):
                target_indexes.update({0, 1})
            for row_index in target_indexes:
                rows[row_index][actual_column_name] = marker
    return True


def _materialize_top_level_distinct_filter_witness(
    data: dict[str, list[dict[str, Any]]],
    standard_sql: str,
    student_sql: str,
    ast_diffs: list[ASTDiffNode],
) -> bool:
    """Duplicate two rows that actually survive a simple DISTINCT filter.

    The legacy duplicate adapter used physical rows 0 and 1.  A preceding
    predicate probe may instead make rows 0 and 3 the qualifying rows, so the
    duplicate was present in the table but absent from the executed result.
    This late, single-table path writes the filter-positive values first and
    then duplicates only the projected, non-key payload.
    """
    if not any(diff.diff_type == "distinct_changed" for diff in ast_diffs):
        return False
    standard_ast = _parse_sql(standard_sql)
    student_ast = _parse_sql(student_sql)
    standard_select = _top_select(standard_ast) if standard_ast is not None else None
    student_select = _top_select(student_ast) if student_ast is not None else None
    if not isinstance(standard_select, exp.Select) or not isinstance(
        student_select, exp.Select
    ):
        return False
    standard_distinct = standard_select.args.get("distinct")
    student_distinct = student_select.args.get("distinct")
    if bool(standard_distinct) == bool(student_distinct):
        return False
    distinct_select = standard_select if standard_distinct else student_select
    plain_select = student_select if standard_distinct else standard_select
    if isinstance(standard_distinct, exp.Distinct) and standard_distinct.args.get("on") is not None:
        return False
    if isinstance(student_distinct, exp.Distinct) and student_distinct.args.get("on") is not None:
        return False
    if any(
        select.find(exp.AggFunc) is not None
        or select.args.get(key)
        for select in (standard_select, student_select)
        for key in ("group", "having", "order", "limit", "offset")
    ):
        return False

    source = _direct_from_table(distinct_select)
    plain_source = _direct_from_table(plain_select)
    if not isinstance(source, exp.Table) or not isinstance(plain_source, exp.Table):
        return False
    if _norm_name(source.name) != _norm_name(plain_source.name):
        return False
    if distinct_select.args.get("joins") or plain_select.args.get("joins"):
        return False
    table_name = _norm_name(source.name)
    actual_table = next(
        (name for name in data if _norm_name(name) == table_name),
        None,
    )
    rows = data.get(actual_table or "", [])
    if len(rows) < 2:
        return False

    where = distinct_select.args.get("where")
    comparisons: list[exp.Expression] = []
    if isinstance(where, exp.Where):
        if isinstance(_unwrap_paren(where.this), (exp.And, exp.Or, exp.Not)):
            return False
        comparisons = [
            node
            for node in where.find_all(
                exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE
            )
            if node.find_ancestor(exp.Select) is distinct_select
        ]
        if not comparisons:
            return False

    projected_columns: list[str] = []
    for item in distinct_select.expressions or ():
        expression = item.this if isinstance(item, exp.Alias) else item
        if not isinstance(expression, exp.Column):
            return False
        ref = _column_ref_in_select_data(data, expression, distinct_select)
        actual = _actual_data_ref(data, ref) if ref else None
        if ref is None or actual is None or _norm_name(ref[0]) != table_name:
            return False
        column = actual[1]
        if _is_key_column(column) or _is_primary_key_candidate(
            actual_table or table_name,
            column,
            list(rows[0]),
        ):
            # A duplicate primary/business key would violate the bounded
            # relational shape and cannot prove a DISTINCT difference.
            return False
        projected_columns.append(column)
    if not projected_columns:
        return False

    filter_assignments: list[tuple[str, Any]] = []
    for comparison in comparisons:
        if not isinstance(comparison.left, exp.Column) or not isinstance(
            comparison.right, exp.Literal
        ):
            return False
        ref = _column_ref_in_select_data(data, comparison.left, distinct_select)
        actual = _actual_data_ref(data, ref) if ref else None
        value = _comparison_truth_value(comparison, True)
        if actual is None or value is None:
            return False
        filter_assignments.append((actual[1], value))

    with write_owner("materializer:distinct_filter_projection"):
        for column, value in filter_assignments:
            rows[0][column] = value
            rows[1][column] = value
        for column in projected_columns:
            rows[1][column] = rows[0][column]
    return True


def _materialize_correlated_exists_boundary_path(
    data: dict[str, list[dict[str, Any]]],
    standard_sql: str,
    student_sql: str,
    ast_diffs: list[ASTDiffNode],
) -> bool:
    """Make an inner comparison boundary reachable from the outer query.

    A comparison inside ``EXISTS`` is not sufficient by itself.  The outer
    block may also require a join, another EXISTS, a scalar COUNT, and an
    anti-membership predicate.  The generic probes correctly create local
    boundary values, but they can leave the conjunction with no surviving
    outer row.  This narrow pass constructs one row-0 path through those
    dependencies and keeps all writes bounded to existing rows.

    The pass is intentionally limited to a standard/student operator change
    whose standard comparison is inside EXISTS.  It does not rewrite arbitrary
    predicates or infer a complete relational model.
    """
    standard_ast = _parse_sql(standard_sql)
    student_ast = _parse_sql(student_sql)
    if standard_ast is None or student_ast is None:
        return False
    standard_outer = _top_select(standard_ast)
    student_outer = _top_select(student_ast)
    if not isinstance(standard_outer, exp.Select) or not isinstance(
        student_outer, exp.Select
    ):
        return False

    def subquery_select(node: exp.Expression) -> exp.Select | None:
        candidate = node.this if isinstance(node, exp.Subquery) else node
        if isinstance(candidate, exp.Select):
            return candidate
        if isinstance(candidate, exp.Expression):
            found = candidate.find(exp.Select)
            return found if isinstance(found, exp.Select) else None
        return None

    boundary_diff = next(
        (
            diff
            for diff in ast_diffs
            if diff.diff_type == "comparison_operator_changed"
            and isinstance(diff.standard_node, exp.Expression)
            and isinstance(diff.standard_node, (exp.GT, exp.GTE, exp.LT, exp.LTE, exp.EQ, exp.NEQ))
            and diff.standard_node.find_ancestor(exp.Exists) is not None
        ),
        None,
    )
    if boundary_diff is None:
        # A common teaching mutation replaces a correlated EXISTS predicate
        # with an ordinary outer predicate.  There is no paired scalar
        # comparison in that shape, so the original boundary-only path was a
        # no-op and both generated queries could legitimately return zero
        # rows.  Build the smallest asymmetric path: keep the standard EXISTS
        # false for one outer row, and make the student's direct predicate
        # true on that same row.  This is still evidence for the actual
        # subquery-removal rule, not an inferred knowledge point.
        standard_exists = [
            node
            for node in standard_outer.find_all(exp.Exists)
            if node.find_ancestor(exp.Select) is standard_outer
        ]
        student_exists = [
            node
            for node in student_outer.find_all(exp.Exists)
            if node.find_ancestor(exp.Select) is student_outer
        ]
        if not standard_exists or student_exists:
            return False
        changed = False
        with write_owner("materializer:correlated_exists_removed_path"):
            changed |= _materialize_select_row_path(
                data,
                standard_outer,
                row_index=0,
            )
            changed |= _materialize_select_row_path(
                data,
                student_outer,
                row_index=0,
            )
            # Ensure the replacement outer predicate admits the selected row.
            _set_select_local_literal_predicates(data, student_outer, 0)
            for exists in standard_exists:
                inner = subquery_select(exists.this)
                if inner is None:
                    continue
                changed |= _materialize_select_row_path(
                    data,
                    inner,
                    row_index=0,
                )
                # Make every direct inner literal predicate false on the
                # selected row.  Correlated key equality remains intact, but
                # the complete EXISTS conjunction is now absent.
                for comparison in inner.find_all(
                    exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE
                ):
                    if comparison.find_ancestor(exp.Select) is not inner:
                        continue
                    if not isinstance(comparison.left, exp.Column):
                        continue
                    if not isinstance(comparison.right, exp.Literal):
                        continue
                    ref = _column_ref_in_select_data(
                        data,
                        comparison.left,
                        inner,
                    )
                    actual = _actual_data_ref(data, ref) if ref else None
                    false_value = _comparison_truth_value(comparison, False)
                    if actual is None or false_value is None:
                        continue
                    rows, column = actual
                    if rows:
                        rows[0][column] = false_value
                        changed = True
        return changed
    standard_inner = boundary_diff.standard_node.find_ancestor(exp.Select)
    if not isinstance(standard_inner, exp.Select):
        return False
    if standard_inner is standard_outer:
        return False

    comparison = boundary_diff.standard_node
    if not isinstance(comparison.left, exp.Column) or not isinstance(comparison.right, exp.Literal):
        return False
    boundary = _literal_value(comparison.right)
    if not isinstance(boundary, (int, float, Decimal)) or isinstance(boundary, bool):
        return False
    boundary_ref = _column_ref_in_select_data(data, comparison.left, standard_inner)
    boundary_actual = _actual_data_ref(data, boundary_ref) if boundary_ref else None
    if boundary_actual is None or not boundary_actual[0]:
        return False

    outer_sources = _direct_select_tables(standard_outer)
    if not outer_sources:
        return False

    def set_ref_from_rows(
        outer_ref: tuple[str, str],
        inner_ref: tuple[str, str],
        outer_index: int = 0,
        inner_index: int = 0,
    ) -> bool:
        outer_actual = _actual_data_ref(data, outer_ref)
        inner_actual = _actual_data_ref(data, inner_ref)
        if outer_actual is None or inner_actual is None:
            return False
        outer_rows, outer_column = outer_actual
        inner_rows, inner_column = inner_actual
        if outer_index >= len(outer_rows) or inner_index >= len(inner_rows):
            return False
        value = outer_rows[outer_index].get(outer_column)
        if value is None:
            value = inner_rows[inner_index].get(inner_column)
        if value is None:
            value = _seed_value(outer_column, outer_index)
        outer_rows[outer_index][outer_column] = value
        inner_rows[inner_index][inner_column] = value
        return True

    changed = False
    with write_owner("materializer:correlated_exists_boundary_path"):
        # First make the top-level JOIN path executable.
        changed |= _materialize_select_row_path(data, standard_outer, row_index=0)

        # Correlation links are scope-resolved, so this also handles aliases
        # and nested blocks without comparing raw column names.
        links_by_inner: dict[int, list[tuple[tuple[str, str], tuple[str, str]]]] = defaultdict(list)
        for outer_ref, inner_ref, inner in _correlated_subquery_links(standard_sql):
            links_by_inner.setdefault(id(inner), []).append((outer_ref, inner_ref))

        # Every top-level EXISTS must have one reachable row.  Its local JOIN
        # equalities are materialized first, then correlated keys are copied
        # from the selected outer row.
        for exists in standard_outer.find_all(exp.Exists):
            if exists.find_ancestor(exp.Select) is not standard_outer:
                continue
            inner = subquery_select(exists.this)
            if inner is None:
                continue
            changed |= _materialize_select_row_path(data, inner, row_index=0)
            for outer_ref, inner_ref in links_by_inner.get(id(inner), []):
                changed |= set_ref_from_rows(outer_ref, inner_ref)

        # COUNT scalar subqueries in the outer WHERE need enough rows to make
        # their comparison true.  For a numeric lower bound, use exactly the
        # requested count where the source tables provide that many rows.
        for subquery in standard_outer.find_all(exp.Subquery):
            inner = subquery_select(subquery)
            if inner is None or inner.find(exp.Count) is None:
                continue
            parent_comparison = subquery.find_ancestor(
                exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE
            )
            requested = 2
            if isinstance(parent_comparison, exp.Expression):
                for side in (parent_comparison.left, parent_comparison.right):
                    value = _literal_value(side)
                    if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
                        requested = max(1, min(int(value), _MAX_WITNESS_ROWS_PER_TABLE))
            local_tables = list(_direct_select_tables(inner).values())
            for index in range(requested):
                changed |= _materialize_select_row_path(
                    data,
                    inner,
                    row_index=index,
                )
            # ``registered = 1`` is a common teaching COUNT condition.  Keep
            # precisely the requested joined nurse rows positive and make the
            # remaining rows non-positive so the scalar comparison is stable.
            for comparison_node in inner.find_all(exp.EQ):
                if not isinstance(comparison_node.left, exp.Column) or not isinstance(comparison_node.right, exp.Literal):
                    continue
                local_ref = _column_ref_in_select_data(data, comparison_node.left, inner)
                literal = _literal_value(comparison_node.right)
                actual = _actual_data_ref(data, local_ref) if local_ref else None
                if actual is None or not isinstance(literal, (int, float, Decimal)):
                    continue
                rows, column = actual
                for index, row in enumerate(rows):
                    row[column] = literal if index < requested else _counter_value(column, literal)

        # A NOT IN subquery must not contain a matching or NULL value when the
        # outer path is meant to survive.  This is safe only for a plain
        # direct-column membership source; complex anti-joins keep their
        # existing dedicated materializer.
        for in_node in standard_outer.find_all(exp.In):
            if in_node.find_ancestor(exp.Select) is not standard_outer:
                continue
            query = in_node.args.get("query")
            inner = subquery_select(query) if isinstance(query, exp.Subquery) else None
            if inner is None or not isinstance(in_node.this, exp.Column) or not inner.expressions:
                continue
            projected = inner.expressions[0]
            projected = projected.this if isinstance(projected, exp.Alias) else projected
            if not isinstance(projected, exp.Column):
                continue
            outer_ref = _column_ref_in_select_data(data, in_node.this, standard_outer)
            inner_ref = _column_ref_in_select_data(data, projected, inner)
            outer_actual = _actual_data_ref(data, outer_ref) if outer_ref else None
            inner_actual = _actual_data_ref(data, inner_ref) if inner_ref else None
            if outer_actual is None or inner_actual is None:
                continue
            outer_rows, outer_column = outer_actual
            inner_rows, inner_column = inner_actual
            if not outer_rows or not inner_rows:
                continue
            outer_value = outer_rows[0].get(outer_column)
            if outer_value is None:
                outer_value = _seed_value(outer_column, 0)
                outer_rows[0][outer_column] = outer_value
            for index, row in enumerate(inner_rows):
                row[inner_column] = _counter_value(inner_column, outer_value)
                if row[inner_column] is None or row[inner_column] == outer_value:
                    row[inner_column] = 100000 + index
            changed = True

        # Finally place the exact threshold in the target inner row.  The
        # student operator then includes/excludes that row while all outer
        # prerequisites remain satisfied.
        boundary_rows, boundary_column = boundary_actual
        boundary_rows[0][boundary_column] = boundary
        changed = True
    return changed


def _materialize_window_alias_cardinality_boundary(
    data: dict[str, list[dict[str, Any]]],
    standard_sql: str,
    student_sql: str,
    ast_diffs: list[ASTDiffNode],
    *,
    schema_catalog: SchemaCatalog | None = None,
) -> bool:
    """Push ``rn <= k``/``rn < k`` through a CTE window alias.

    The outer predicate references a derived column, so a normal comparison
    materializer cannot locate a physical cell.  For ROW_NUMBER/RANK teaching
    queries, make exactly ``k`` rows share one partition and give the window a
    deterministic order.  The pass is deliberately limited to a single
    direct source table plus one equality JOIN.
    """
    if not any(
        diff.diff_type in {"comparison_operator_changed", "literal_changed"}
        for diff in ast_diffs
    ):
        return False
    standard_ast = _parse_sql(standard_sql)
    if standard_ast is None:
        return False
    comparison_types = (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE)
    for diff in ast_diffs:
        if diff.diff_type not in {"comparison_operator_changed", "literal_changed"}:
            continue
        comparison = _comparison_node_from_diff(diff.standard_node, diff.extra.get("standard_sql"))
        if not isinstance(comparison, comparison_types):
            continue
        outer_column = next(
            (node for node in (comparison.left, comparison.right) if isinstance(node, exp.Column)),
            None,
        )
        literal_node = next(
            (node for node in (comparison.left, comparison.right) if isinstance(node, (exp.Literal, exp.Boolean))),
            None,
        )
        if not isinstance(outer_column, exp.Column) or literal_node is None:
            continue
        boundary = _semantic_literal_value(literal_node)
        if not isinstance(boundary, (int, float, Decimal)) or isinstance(boundary, bool):
            continue
        outer_select = _nearest_select(comparison)
        if not isinstance(outer_select, exp.Select):
            continue
        source = _direct_from_table(outer_select)
        if not isinstance(source, exp.Table):
            continue
        cte = next(
            (
                node for node in standard_ast.find_all(exp.CTE)
                if _norm_name(node.alias or "") == _norm_name(source.name)
            ),
            None,
        )
        if not isinstance(cte, exp.CTE) or not isinstance(cte.this, exp.Select):
            continue
        body = cte.this
        window_alias = next(
            (
                item for item in body.expressions or ()
                if isinstance(item, exp.Alias)
                and _norm_name(item.alias) == _norm_name(outer_column.name)
                and isinstance(item.this, exp.Window)
            ),
            None,
        )
        if not isinstance(window_alias, exp.Alias):
            continue
        window = window_alias.this
        if not isinstance(window, exp.Window) or not isinstance(window.this, (exp.RowNumber, exp.Rank, exp.DenseRank)):
            continue
        required = int(boundary) if isinstance(comparison, (exp.LTE, exp.GTE, exp.EQ)) else max(1, int(boundary))
        if required < 1:
            continue
        source_tables = _direct_select_tables(body)
        if not source_tables:
            continue
        partition_columns = [item for item in (window.args.get("partition_by") or []) if isinstance(item, exp.Column)]
        order_clause = window.args.get("order")
        order_items = list(order_clause.expressions or []) if isinstance(order_clause, exp.Order) else []
        order_columns = [
            (item.this if isinstance(item, exp.Ordered) else item, bool(item.args.get("desc")) if isinstance(item, exp.Ordered) else False)
            for item in order_items
            if isinstance(item.this if isinstance(item, exp.Ordered) else item, exp.Column)
        ]
        if not order_columns:
            continue
        order_ref = _column_ref_in_select_data(data, order_columns[0][0], body)
        if order_ref is None:
            continue
        order_actual = _actual_data_ref(data, order_ref)
        if order_actual is None or len(order_actual[0]) < required:
            continue
        source_table = order_ref[0]
        partition_refs = [
            _column_ref_in_select_data(data, column, body)
            for column in partition_columns
        ]
        partition_refs = [item for item in partition_refs if item is not None]
        join_pairs = _join_on_column_pairs(standard_sql)
        with write_owner("materializer:window_alias_cardinality_boundary"):
            _align_standard_join_equalities(data, standard_sql)
            # Route the source rows through one parent partition when the
            # partition key is on the other side of a simple equality JOIN.
            for partition_ref in partition_refs:
                if partition_ref[0] == source_table:
                    actual = _actual_data_ref(data, partition_ref)
                    if actual is None:
                        continue
                    rows, column = actual
                    anchor = rows[0].get(column)
                    for row in rows[:required]:
                        row[column] = anchor
                    for index, row in enumerate(rows[required:], start=required):
                        row[column] = _group_probe_value(column, index, 90)
                    continue
                edge = next(
                    (
                        pair for pair in join_pairs
                        if partition_ref[0] in {pair[0][0], pair[1][0]}
                        and source_table in {pair[0][0], pair[1][0]}
                    ),
                    None,
                )
                if edge is None:
                    continue
                child_ref = next((item for item in edge if item[0] == source_table), None)
                parent_join_ref = next((item for item in edge if item[0] != source_table), None)
                parent_actual = _actual_data_ref(data, parent_join_ref) if parent_join_ref else None
                child_join_actual = _actual_data_ref(data, child_ref) if child_ref else None
                if parent_actual is None or child_join_actual is None:
                    continue
                parent_rows, parent_column = parent_actual
                child_rows, child_column = child_join_actual
                anchor = parent_rows[0].get(parent_column)
                used = {row.get(parent_column) for row in parent_rows if row.get(parent_column) is not None}
                for index, row in enumerate(parent_rows[1:], start=1):
                    if row.get(parent_column) in {None, anchor}:
                        candidate = _unique_key_value(parent_column, index, used, anchor)
                        row[parent_column] = candidate
                        used.add(candidate)
                for row in child_rows[:required]:
                    row[child_column] = anchor
                for index, row in enumerate(child_rows[required:], start=required):
                    if len(parent_rows) > 1:
                        row[child_column] = parent_rows[1 + ((index - required) % (len(parent_rows) - 1))].get(parent_column)
                    else:
                        row[child_column] = _counter_value(child_column, anchor)
                partition_actual = _actual_data_ref(data, partition_ref)
                if partition_actual is not None:
                    partition_rows, partition_column = partition_actual
                    partition_anchor = partition_rows[0].get(partition_column)
                    used_partition = {
                        row.get(partition_column)
                        for row in partition_rows
                        if row.get(partition_column) is not None
                    }
                    for index, row in enumerate(partition_rows[1:], start=1):
                        candidate = _group_probe_value(partition_column, index, 90)
                        if candidate in used_partition or candidate == partition_anchor:
                            candidate = _group_probe_value(partition_column, index + 1, 91)
                        row[partition_column] = candidate

            rows, order_column = order_actual
            descending = bool(order_columns[0][1])
            for index, row in enumerate(rows[:required]):
                value = 1000 - index if descending else 1000 + index
                row[order_column] = value
        return True
    return False


def _materialize_having_ratio_boundary(
    data: dict[str, list[dict[str, Any]]],
    standard_sql: str,
    student_sql: str,
    ast_diffs: list[ASTDiffNode],
    *,
    schema_catalog: SchemaCatalog | None = None,
) -> bool:
    """Materialize exact percentage boundaries in simple HAVING clauses.

    Supported form (the common introductory analytics exercise):

    ``scale * SUM(CASE WHEN flag = 0 THEN 1 ELSE 0 END) /
    COUNT(measure) > threshold``

    The witness uses a small integer denominator and aligns only existing
    equality edges.  If the expression is not this shape, it returns without
    changing data; the caller will retain the explicit bounded/unknown state.
    """
    if not any(
        diff.clause_category.upper() in {"HAVING", "PREDICATE"}
        and diff.diff_type in {"having_changed", "comparison_operator_changed", "literal_changed"}
        for diff in ast_diffs
    ):
        return False
    standard_ast = _parse_sql(standard_sql)
    student_ast = _parse_sql(student_sql)
    if standard_ast is None or student_ast is None:
        return False

    def numeric_literal(node: exp.Expression | None) -> Any | None:
        value = _semantic_literal_value(node)
        return value if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool) else None

    def endpoint(node: exp.Expression) -> tuple[exp.Expression, Any] | None:
        if isinstance(node.left, (exp.Literal, exp.Boolean)):
            return node.right, _semantic_literal_value(node.left)
        if isinstance(node.right, (exp.Literal, exp.Boolean)):
            return node.left, _semantic_literal_value(node.right)
        return None

    for having in standard_ast.find_all(exp.Having):
        select = _nearest_select(having)
        if not isinstance(select, exp.Select):
            continue
        comparison = next(
            (
                item
                for item in having.find_all(exp.GT, exp.GTE, exp.LT, exp.LTE, exp.EQ, exp.NEQ)
                if item.find_ancestor(exp.Select) is select and endpoint(item) is not None
            ),
            None,
        )
        if comparison is None:
            continue
        pair = endpoint(comparison)
        if pair is None:
            continue
        aggregate_expression, boundary = pair
        if not isinstance(boundary, (int, float, Decimal)) or isinstance(boundary, bool):
            continue
        division = aggregate_expression.find(exp.Div)
        if not isinstance(division, exp.Div):
            continue
        sum_node = division.left.find(exp.Sum) if isinstance(division.left, exp.Expression) else None
        count_node = division.right.find(exp.Count) if isinstance(division.right, exp.Expression) else None
        if not isinstance(sum_node, exp.Sum) or not isinstance(count_node, exp.Count):
            continue
        case_node = sum_node.this if isinstance(sum_node.this, exp.Case) else None
        if not isinstance(case_node, exp.Case):
            continue
        ifs = case_node.args.get("ifs") or []
        condition = ifs[0].this if ifs and isinstance(ifs[0], exp.If) else None
        condition_parts = (
            condition
            if isinstance(condition, (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE))
            else None
        )
        if condition_parts is None:
            continue
        flag_column = condition_parts.left if isinstance(condition_parts.left, exp.Column) else None
        flag_literal = condition_parts.right if isinstance(condition_parts.right, (exp.Literal, exp.Boolean)) else None
        if not isinstance(flag_column, exp.Column) or flag_literal is None:
            continue
        count_column = count_node.find(exp.Column)
        if not isinstance(count_column, exp.Column):
            # COUNT(*) is intentionally left to the existing COUNT path.
            continue
        flag_ref = _column_ref_in_select_data(data, flag_column, select)
        count_ref = _column_ref_in_select_data(data, count_column, select)
        if flag_ref is None or count_ref is None or flag_ref[0] != count_ref[0]:
            continue
        fact_actual = _actual_data_ref(data, flag_ref)
        count_actual = _actual_data_ref(data, count_ref)
        if fact_actual is None or count_actual is None or fact_actual[0] is not count_actual[0]:
            continue
        fact_rows, flag_actual_column = fact_actual
        count_rows, count_actual_column = count_actual
        if len(fact_rows) < 2:
            continue

        # Extract the scale from the numerator.  The only accepted extra
        # arithmetic is a single numeric multiplier outside SUM.
        scale = Decimal("1")
        numerator = division.left
        if isinstance(numerator, exp.Mul):
            factors = [item for item in (numerator.left, numerator.right)]
            scale_value = next((numeric_literal(item) for item in factors if numeric_literal(item) is not None), None)
            if scale_value is None:
                continue
            scale = Decimal(str(scale_value))
        elif numerator is not sum_node:
            continue

        # Find the smallest bounded denominator that can represent the target
        # exactly as scale * errors / count.
        target = Decimal(str(boundary))
        denominator = error_count = None
        for candidate_count in range(2, min(len(fact_rows), 16) + 1):
            for candidate_errors in range(candidate_count + 1):
                if scale * Decimal(candidate_errors) / Decimal(candidate_count) == target:
                    denominator, error_count = candidate_count, candidate_errors
                    break
            if denominator is not None:
                break
        if denominator is None or error_count is None:
            continue

        group = select.args.get("group")
        group_refs = [
            _column_ref_in_select(item, select)
            for item in (group.expressions if isinstance(group, exp.Group) else ())
            if isinstance(item, exp.Column)
        ]
        group_refs = [item for item in group_refs if item is not None]
        if not group_refs:
            continue
        join_pairs = _join_on_column_pairs(standard_sql)
        if not join_pairs:
            continue

        def actual_ref(ref: tuple[str, str]) -> tuple[list[dict[str, Any]], str] | None:
            return _actual_data_ref(data, ref)

        def choose_unique(pair_ref: tuple[tuple[str, str], tuple[str, str]]) -> tuple[tuple[str, str], tuple[str, str]] | None:
            left_ref, right_ref = pair_ref
            left_unique = _catalog_has_unary_unique_key(schema_catalog, left_ref) or (
                (actual_ref(left_ref) is not None)
                and _is_primary_key_candidate(left_ref[0], actual_ref(left_ref)[1], list(actual_ref(left_ref)[0][0]))
            )
            right_unique = _catalog_has_unary_unique_key(schema_catalog, right_ref) or (
                (actual_ref(right_ref) is not None)
                and _is_primary_key_candidate(right_ref[0], actual_ref(right_ref)[1], list(actual_ref(right_ref)[0][0]))
            )
            if left_unique and not right_unique:
                return left_ref, right_ref
            if right_unique and not left_unique:
                return right_ref, left_ref
            left_score = _join_key_uniqueness_score(data, left_ref, schema_catalog)
            right_score = _join_key_uniqueness_score(data, right_ref, schema_catalog)
            if left_score == right_score:
                return None
            return (left_ref, right_ref) if left_score > right_score else (right_ref, left_ref)

        with write_owner("materializer:having_ratio_boundary"):
            _align_standard_join_equalities(data, standard_sql)
            # First establish a bounded parent/child path for every equality
            # edge.  Prefix rows all use parent row 0; the tail is routed to
            # later parent rows, whose group labels are split below.
            for pair_ref in join_pairs:
                selected = choose_unique(pair_ref)
                if selected is None:
                    continue
                unique_ref, repeated_ref = selected
                unique_actual = actual_ref(unique_ref)
                repeated_actual = actual_ref(repeated_ref)
                if unique_actual is None or repeated_actual is None:
                    continue
                unique_rows, unique_column = unique_actual
                repeated_rows, repeated_column = repeated_actual
                if not unique_rows or not repeated_rows:
                    continue
                anchor = unique_rows[0].get(unique_column)
                # Only the leaf fact relation needs several rows in the
                # anchor group.  Intermediate child relations (for example
                # questions between responses and topics) must keep just
                # their row 0 on the anchor key; otherwise unreferenced child
                # rows become additional anchor rows when the fact tail is
                # routed through them.
                anchor_row_count = (
                    denominator
                    if repeated_ref[0] == flag_ref[0]
                    else 1
                )
                for index, row in enumerate(repeated_rows):
                    if index < anchor_row_count:
                        row[repeated_column] = anchor
                    elif len(unique_rows) > 1:
                        parent_index = 1 + ((index - anchor_row_count) % (len(unique_rows) - 1))
                        row[repeated_column] = unique_rows[parent_index].get(unique_column)
                    else:
                        row[repeated_column] = _counter_value(repeated_column, anchor)

            # Keep non-join group labels unique after the anchor row.  This is
            # what prevents the rest of the bounded database from inflating
            # the exact percentage group.
            join_endpoints = {ref for pair_ref in join_pairs for ref in pair_ref}
            for group_ref in group_refs:
                group_actual = actual_ref(group_ref)
                if group_actual is None:
                    continue
                group_rows, group_column = group_actual
                anchor = group_rows[0].get(group_column)
                if group_ref in join_endpoints:
                    continue
                for index, row in enumerate(group_rows[1:], start=1):
                    candidate = _group_probe_value(group_column, index, 80)
                    if candidate == anchor:
                        candidate = _group_probe_value(group_column, index + 1, 81)
                    row[group_column] = candidate

            true_value = _temporal_value_for_comparison(
                condition_parts,
                _semantic_literal_value(flag_literal),
                true=True,
            )
            false_value = _temporal_value_for_comparison(
                condition_parts,
                _semantic_literal_value(flag_literal),
                true=False,
            )
            if true_value is None or false_value is None:
                continue
            for index, row in enumerate(fact_rows):
                row[flag_actual_column] = true_value if index < error_count else false_value
                row[count_actual_column] = 900000 + index
        return True
    return False


def _materialize_derived_sum_alias_boundary(
    data: dict[str, list[dict[str, Any]]],
    standard_sql: str,
    student_sql: str,
    ast_diffs: list[ASTDiffNode],
    *,
    schema_catalog: SchemaCatalog | None = None,
) -> bool:
    """Materialize a simple derived-table ``SUM(CASE ...)`` boundary."""
    if not any(
        diff.diff_type in {"comparison_operator_changed", "literal_changed"}
        for diff in ast_diffs
    ):
        return False
    standard_ast = _parse_sql(standard_sql)
    if standard_ast is None:
        return False
    for comparison in standard_ast.find_all(exp.GT, exp.GTE, exp.LT, exp.LTE, exp.EQ, exp.NEQ):
        outer_select = _nearest_select(comparison)
        if not isinstance(outer_select, exp.Select):
            continue
        outer_column = next(
            (node for node in (comparison.left, comparison.right) if isinstance(node, exp.Column)),
            None,
        )
        literal_node = next(
            (node for node in (comparison.left, comparison.right) if isinstance(node, (exp.Literal, exp.Boolean))),
            None,
        )
        if not isinstance(outer_column, exp.Column) or literal_node is None:
            continue
        boundary = _semantic_literal_value(literal_node)
        if not isinstance(boundary, (int, float, Decimal)) or isinstance(boundary, bool):
            continue
        from_clause = outer_select.args.get("from_") or outer_select.args.get("from")
        source_subquery = from_clause.this if isinstance(from_clause, exp.From) else None
        if not isinstance(source_subquery, exp.Subquery) or not isinstance(source_subquery.this, exp.Select):
            continue
        body = source_subquery.this
        aggregate_projection = next(
            (
                item for item in body.expressions or ()
                if isinstance(item, exp.Alias)
                and _norm_name(item.alias) == _norm_name(outer_column.name)
                and isinstance(item.this, exp.Sum)
            ),
            None,
        )
        if not isinstance(aggregate_projection, exp.Alias):
            continue
        aggregate = aggregate_projection.this
        group = body.args.get("group")
        group_column_node = next(
            (item for item in (group.expressions if isinstance(group, exp.Group) else ()) if isinstance(item, exp.Column)),
            None,
        )
        if not isinstance(group_column_node, exp.Column):
            continue
        source_table_node = _direct_from_table(body)
        if not isinstance(source_table_node, exp.Table):
            continue
        source_ref = _norm_name(source_table_node.name)
        source_table = next((name for name in data if _norm_name(name) == source_ref), None)
        if not source_table:
            continue
        group_ref = _column_ref_in_select_data(data, group_column_node, body)
        group_actual = _actual_data_ref(data, group_ref) if group_ref else None
        if group_actual is None:
            continue
        # The standard teaching revenue form has a single equality JOIN from
        # the fact table to the grouped dimension.  Resolve the join path and
        # retain one dimension row per non-anchor group.
        join_pair = next(
            (
                pair for pair in _join_on_column_pairs(standard_sql)
                if source_ref in {pair[0][0], pair[1][0]}
                and group_ref is not None
                and group_ref[0] in {pair[0][0], pair[1][0]}
            ),
            None,
        )
        if join_pair is None or group_ref is None:
            continue
        child_ref = next((item for item in join_pair if item[0] == source_ref), None)
        parent_join_ref = next((item for item in join_pair if item[0] != source_ref), None)
        child_actual = _actual_data_ref(data, child_ref) if child_ref else None
        parent_join_actual = _actual_data_ref(data, parent_join_ref) if parent_join_ref else None
        if child_actual is None or parent_join_actual is None:
            continue
        fact_rows, child_column = child_actual
        parent_rows, parent_join_column = parent_join_actual
        group_rows, group_column = group_actual
        if not fact_rows or not parent_rows:
            continue

        case_node = aggregate.this if isinstance(aggregate.this, exp.Case) else None
        if not isinstance(case_node, exp.Case):
            continue
        if_nodes = case_node.args.get("ifs") or []
        condition = if_nodes[0].this if if_nodes and isinstance(if_nodes[0], exp.If) else None
        if not isinstance(condition, (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE)):
            continue
        condition_column = condition.left if isinstance(condition.left, exp.Column) else None
        condition_literal = condition.right if isinstance(condition.right, (exp.Literal, exp.Boolean)) else None
        if not isinstance(condition_column, exp.Column) or condition_literal is None:
            continue
        condition_ref = _column_ref_in_select_data(data, condition_column, body)
        condition_actual = _actual_data_ref(data, condition_ref) if condition_ref else None
        if condition_actual is None or condition_ref[0] != source_ref:
            continue
        # Resolve the two multiplicative branches.  We intentionally require
        # a column from the fact side and a column from the grouped dimension.
        branch_nodes = []
        true_branch = if_nodes[0].args.get("true") if if_nodes else None
        if isinstance(true_branch, exp.Expression):
            branch_nodes.append(true_branch)
        default_node = case_node.args.get("default")
        if isinstance(default_node, exp.Expression):
            branch_nodes.append(default_node)
        if len(branch_nodes) != 2:
            continue
        branch_columns = [list(node.find_all(exp.Column)) for node in branch_nodes]
        all_columns = [column for columns in branch_columns for column in columns]
        if not all_columns:
            continue
        # Pick a fact measure (slots/quantity/amount) and a dimension price;
        # this covers the canonical PGExercises revenue exercise without
        # guessing arbitrary multi-column arithmetic.
        fact_measure_node = next(
            (
                column for column in all_columns
                if (_column_ref_in_select_data(data, column, body) or ("" , ""))[0] == source_ref
                and _norm_name(column.name) != _norm_name(condition_column.name)
            ),
            None,
        )
        default_columns = list(default_node.find_all(exp.Column)) if isinstance(default_node, exp.Expression) else []
        dimension_value_node = next(
            (
                column for column in [*default_columns, *all_columns]
                if group_ref[0] == (_column_ref_in_select_data(data, column, body) or ("", ""))[0]
            ),
            None,
        )
        if not isinstance(fact_measure_node, exp.Column) or not isinstance(dimension_value_node, exp.Column):
            continue
        fact_measure_ref = _column_ref_in_select_data(data, fact_measure_node, body)
        dimension_value_ref = _column_ref_in_select_data(data, dimension_value_node, body)
        fact_measure_actual = _actual_data_ref(data, fact_measure_ref) if fact_measure_ref else None
        dimension_value_actual = _actual_data_ref(data, dimension_value_ref) if dimension_value_ref else None
        if fact_measure_actual is None or dimension_value_actual is None:
            continue
        condition_rows, condition_column_actual = condition_actual
        fact_measure_rows, fact_measure_column = fact_measure_actual
        dimension_value_rows, dimension_value_column = dimension_value_actual
        with write_owner("materializer:derived_sum_alias_boundary"):
            _align_standard_join_equalities(data, standard_sql)
            anchor_key = parent_rows[0].get(parent_join_column)
            used_keys = {row.get(parent_join_column) for row in parent_rows if row.get(parent_join_column) is not None}
            for index, row in enumerate(parent_rows[1:], start=1):
                if row.get(parent_join_column) in {None, anchor_key}:
                    candidate = _unique_key_value(parent_join_column, index, used_keys, anchor_key)
                    row[parent_join_column] = candidate
                    used_keys.add(candidate)
            for row in fact_rows[:1]:
                row[child_column] = anchor_key
            for index, row in enumerate(fact_rows[1:], start=1):
                if len(parent_rows) > 1:
                    row[child_column] = parent_rows[1 + ((index - 1) % (len(parent_rows) - 1))].get(parent_join_column)
                else:
                    row[child_column] = _counter_value(child_column, anchor_key)
            # Give each dimension group a stable distinct label.
            anchor_group = group_rows[0].get(group_column)
            used_groups = {row.get(group_column) for row in group_rows if row.get(group_column) is not None}
            for index, row in enumerate(group_rows[1:], start=1):
                candidate = _group_probe_value(group_column, index, 95)
                if candidate in used_groups or candidate == anchor_group:
                    candidate = _group_probe_value(group_column, index + 1, 96)
                row[group_column] = candidate
                used_groups.add(candidate)
            # Select the ELSE branch (memid != 0 in the canonical form), then
            # make one row's revenue exactly the threshold.
            condition_value = _semantic_literal_value(condition_literal)
            false_value = _temporal_value_for_comparison(condition, condition_value, true=False)
            if false_value is None:
                continue
            condition_rows[0][condition_column_actual] = false_value
            fact_measure_rows[0][fact_measure_column] = 10
            dimension_value_rows[0][dimension_value_column] = boundary / 10
        return True
    return False


def _materialize_cte_aggregate_alias_boundary(
    data: dict[str, list[dict[str, Any]]],
    standard_sql: str,
    student_sql: str,
    ast_diffs: list[ASTDiffNode],
    *,
    schema_catalog: SchemaCatalog | None = None,
) -> bool:
    """Push an outer CTE-alias boundary down to its aggregate input rows.

    A comparison such as ``WHERE avg_value >= 22`` has no physical column in
    the outer query block.  The generic boundary probe therefore cannot
    resolve a cell to write.  For the common teaching form of a CTE exposing
    ``AVG/MIN/MAX/SUM(...) AS avg_value``, resolve that alias through the CTE,
    then create one small group whose aggregate is exactly the threshold.
    """
    if not any(
        diff.diff_type in {"comparison_operator_changed", "literal_changed"}
        for diff in ast_diffs
    ):
        return False
    standard_ast = _parse_sql(standard_sql)
    student_ast = _parse_sql(student_sql)
    if standard_ast is None or student_ast is None:
        return False

    def cte_for_outer_column(
        outer_select: exp.Select,
        column: exp.Column,
    ) -> tuple[exp.Select, exp.Alias, exp.Expression] | None:
        source = _direct_from_table(outer_select)
        if not isinstance(source, exp.Table):
            return None
        source_name = _norm_name(source.name)
        cte_node = next(
            (
                item
                for item in standard_ast.find_all(exp.CTE)
                if _norm_name(item.alias or "") == source_name
            ),
            None,
        )
        if not isinstance(cte_node, exp.CTE) or not isinstance(cte_node.this, exp.Select):
            return None
        body = cte_node.this
        wanted = _norm_name(column.name)
        for projection in body.expressions or ():
            if not isinstance(projection, exp.Alias) or _norm_name(projection.alias) != wanted:
                continue
            aggregate = projection.this.find(*_AGG_FUNC_TYPES)
            if aggregate is not None:
                return body, projection, aggregate
        return None

    changed = False
    for diff in ast_diffs:
        if diff.diff_type not in {"comparison_operator_changed", "literal_changed"}:
            continue
        standard_comparison = _comparison_node_from_diff(
            diff.standard_node,
            diff.extra.get("standard_sql"),
        )
        student_comparison = _comparison_node_from_diff(
            diff.student_node,
            diff.extra.get("student_sql"),
        )
        if not isinstance(standard_comparison, (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE)):
            continue
        if not isinstance(student_comparison, (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE)):
            continue
        standard_column = next(
            (item for item in (standard_comparison.left, standard_comparison.right) if isinstance(item, exp.Column)),
            None,
        )
        if not isinstance(standard_column, exp.Column):
            continue
        outer_select = _nearest_select(standard_comparison)
        if not isinstance(outer_select, exp.Select):
            continue
        definition = cte_for_outer_column(outer_select, standard_column)
        if definition is None:
            continue
        body, projection, aggregate = definition
        literal_node = (
            standard_comparison.right
            if isinstance(standard_comparison.right, (exp.Literal, exp.Boolean))
            else standard_comparison.left
            if isinstance(standard_comparison.left, (exp.Literal, exp.Boolean))
            else None
        )
        boundary = _semantic_literal_value(literal_node)
        if not isinstance(boundary, (int, float, Decimal)) or isinstance(boundary, bool):
            continue
        argument_column = aggregate.find(exp.Column)
        group = body.args.get("group")
        group_column = next(
            (item for item in (group.expressions if isinstance(group, exp.Group) else ()) if isinstance(item, exp.Column)),
            None,
        )
        if not isinstance(argument_column, exp.Column) or not isinstance(group_column, exp.Column):
            continue
        measure_ref = _column_ref_in_select_data(data, argument_column, body)
        group_ref = _column_ref_in_select_data(data, group_column, body)
        measure_actual = _actual_data_ref(data, measure_ref) if measure_ref else None
        group_actual = _actual_data_ref(data, group_ref) if group_ref else None
        if not measure_actual or not group_actual:
            continue
        measure_rows, measure_column = measure_actual
        group_rows, group_column_name = group_actual
        if not measure_rows or not group_rows:
            continue
        # Find the equality edge connecting the aggregate input and the group
        # table.  The common CTE form has exactly one such edge.
        join_pair = next(
            (
                pair
                for pair in _join_on_column_pairs(standard_sql)
                if {pair[0][0], pair[1][0]} == {measure_ref[0], group_ref[0]}
            ),
            None,
        )
        if join_pair is None:
            continue
        measure_join_ref = next((ref for ref in join_pair if ref[0] == measure_ref[0]), None)
        group_join_ref = next((ref for ref in join_pair if ref[0] == group_ref[0]), None)
        measure_join_actual = _actual_data_ref(data, measure_join_ref) if measure_join_ref else None
        group_join_actual = _actual_data_ref(data, group_join_ref) if group_join_ref else None
        if not measure_join_actual or not group_join_actual:
            continue
        measure_join_rows, measure_join_column = measure_join_actual
        group_join_rows, group_join_column = group_join_actual
        if len(measure_rows) < 2 or not group_join_rows:
            continue
        function = type(aggregate).__name__.upper()
        if function not in {"AVG", "SUM", "MIN", "MAX"}:
            continue
        with write_owner("materializer:cte_aggregate_alias_boundary"):
            _align_standard_join_equalities(data, standard_sql)
            anchor_key = group_join_rows[0].get(group_join_column)
            if anchor_key is None:
                anchor_key = _seed_value(group_join_column, 0)
                group_join_rows[0][group_join_column] = anchor_key
            used_keys = {row.get(group_join_column) for row in group_join_rows if row.get(group_join_column) is not None}
            for index, row in enumerate(group_join_rows[1:], start=1):
                candidate = row.get(group_join_column)
                if candidate is None or candidate in used_keys:
                    candidate = _unique_key_value(group_join_column, index, used_keys, anchor_key)
                    row[group_join_column] = candidate
                used_keys.add(candidate)
            # Two input rows are sufficient for AVG/SUM and preserve a valid
            # integer/date/text schema without expanding the witness world.
            for index, row in enumerate(measure_join_rows[:2]):
                row[measure_join_column] = anchor_key
            for row in measure_join_rows[2:]:
                if row.get(measure_join_column) == anchor_key:
                    row[measure_join_column] = _counter_value(measure_join_column, anchor_key)
            if function == "AVG":
                measure_rows[0][measure_column] = boundary - 1
                measure_rows[1][measure_column] = boundary + 1
            elif function == "SUM":
                measure_rows[0][measure_column] = boundary / 2
                measure_rows[1][measure_column] = boundary / 2
            elif function == "MIN":
                measure_rows[0][measure_column] = boundary
                measure_rows[1][measure_column] = boundary + 1
            elif function == "MAX":
                measure_rows[0][measure_column] = boundary
                measure_rows[1][measure_column] = boundary - 1
        changed = True
    return changed


def _materialize_literal_in_reachability(
    data: dict[str, list[dict[str, Any]]],
    standard_sql: str,
    *,
    schema_catalog: SchemaCatalog | None = None,
) -> bool:
    """Make simple literal ``IN`` predicates reach real source rows.

    The general predicate probes are intentionally conservative and may leave
    an unchanged ``IN ('a', 'b')`` filter unreachable when a generated text
    column contains only seed values.  That is especially visible in a
    DISTINCT-vs-non-DISTINCT mutation: the join path exists, but both queries
    return no rows.  This adapter handles only literal lists and writes a
    listed value (or an outside value for ``NOT IN``) to at most two source
    rows.  Subquery membership, expressions, and ambiguous lineage remain
    untouched and continue through the normal evidence-gap path.
    """
    root_ast = _parse_sql(standard_sql)
    if root_ast is None:
        return False
    changed = False
    assigned: set[tuple[str, str]] = set()
    selects = list(root_ast.find_all(exp.Select))
    if isinstance(root_ast, exp.Select) and root_ast not in selects:
        selects.append(root_ast)
    for select in selects:
        for in_node in select.find_all(exp.In):
            if in_node.find_ancestor(exp.Select) is not select:
                continue
            if in_node.args.get("query") is not None:
                continue
            values = [
                _semantic_literal_value(item)
                for item in in_node.expressions or ()
                if isinstance(item, (exp.Literal, exp.Boolean))
            ]
            values = [value for value in values if value is not None]
            if not values or not isinstance(in_node.this, exp.Column):
                continue
            ref = _query_column_ref_in_data(data, in_node.this, select, root_ast)
            if ref is None:
                ref = _column_ref_in_select_data(data, in_node.this, select)
            actual = _actual_data_ref(data, ref) if ref else None
            if actual is None or not actual[0] or ref in assigned:
                continue
            rows, column = actual
            listed_value = values[0]
            target_value = (
                _counter_value(column, listed_value)
                if isinstance(in_node.parent, exp.Not)
                else listed_value
            )
            unique_target = _catalog_has_unary_unique_key(schema_catalog, ref) or _is_primary_key_candidate(
                next((name for name, candidate_rows in data.items() if candidate_rows is rows), ref[0]),
                column,
                list(rows[0]),
            )
            if unique_target:
                # A unique/primary-key IN column already has to contain
                # distinct values.  Reusing the first listed value for two
                # rows would violate the authoritative uniqueness constraint.
                # Keep an existing matching row; if none exists, write one
                # unused row and leave the remaining key domain intact.
                if any(
                    row.get(column) == target_value
                    for row in rows
                ):
                    assigned.add(ref)
                    continue
                row_index = next(
                    (
                        index
                        for index, row in enumerate(rows)
                        if row.get(column) not in {target_value}
                    ),
                    None,
                )
                if row_index is None:
                    continue
                with write_owner("materializer:literal_in_reachability"):
                    _align_query_block_equality_row(
                        data,
                        select,
                        root_ast,
                        row_index,
                        schema_catalog=schema_catalog,
                    )
                    _materialize_select_row_path(
                        data,
                        select,
                        row_index=row_index,
                        schema_catalog=schema_catalog,
                    )
                    rows[row_index][column] = target_value
                assigned.add(ref)
                changed = True
                continue
            with write_owner("materializer:literal_in_reachability"):
                for row_index in range(min(2, len(rows))):
                    # Align JOIN endpoints before the final IN write.  The
                    # endpoint adapter deliberately does not own this cell.
                    _align_query_block_equality_row(
                        data,
                        select,
                        root_ast,
                        row_index,
                        schema_catalog=schema_catalog,
                    )
                    _materialize_select_row_path(
                        data,
                        select,
                        row_index=row_index,
                        schema_catalog=schema_catalog,
                    )
                    rows[row_index][column] = target_value
                    changed = True
            assigned.add(ref)
    return changed


def _materialize_derived_comparison_boundaries(
    data: dict[str, list[dict[str, Any]]],
    standard_sql: str,
    student_sql: str,
    ast_diffs: list[ASTDiffNode],
    *,
    schema_catalog: SchemaCatalog | None = None,
) -> bool:
    """Expose strict/inclusive boundaries over simple derived expressions.

    A predicate such as ``cost > 30`` versus ``cost >= 30`` often compares an
    alias from a derived table.  That alias is not a physical cell, so the
    generic boundary writer correctly refuses to assign ``30`` directly to
    it.  For the common bounded teaching shape where the alias is a CASE
    expression containing ``slots * membercost/guestcost``, set one reachable
    input row to the exact boundary.  Other derived expressions remain
    unresolved rather than receiving an arbitrary reverse-engineered value.
    """
    standard_ast = _parse_sql(standard_sql)
    student_ast = _parse_sql(student_sql)
    if standard_ast is None or student_ast is None:
        return False
    comparison_types = (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE)
    changed = False
    for diff in ast_diffs:
        if diff.diff_type not in {"comparison_operator_changed", "literal_changed"}:
            continue
        standard_comparison = _comparison_node_from_diff(
            diff.standard_node,
            diff.extra.get("standard_sql"),
        )
        student_comparison = _comparison_node_from_diff(
            diff.student_node,
            diff.extra.get("student_sql"),
        )
        if not isinstance(standard_comparison, comparison_types) or not isinstance(
            student_comparison, comparison_types
        ):
            continue
        standard_column = _predicate_source_column(standard_comparison.left)
        standard_scalar = _static_predicate_scalar(standard_comparison.right)
        if standard_column is None or standard_scalar is _MISSING:
            standard_column = _predicate_source_column(standard_comparison.right)
            standard_scalar = _static_predicate_scalar(standard_comparison.left)
        student_column = _predicate_source_column(student_comparison.left)
        student_scalar = _static_predicate_scalar(student_comparison.right)
        if student_column is None or student_scalar is _MISSING:
            student_column = _predicate_source_column(student_comparison.right)
            student_scalar = _static_predicate_scalar(student_comparison.left)
        if (
            standard_column is None
            or student_column is None
            or _norm_name(standard_column.name) != _norm_name(student_column.name)
            or standard_scalar is _MISSING
            or student_scalar is _MISSING
            or standard_scalar != student_scalar
            or isinstance(standard_scalar, bool)
            or not isinstance(standard_scalar, (int, float, Decimal))
        ):
            continue
        strict_types = {exp.GT, exp.LT}
        inclusive_types = {exp.GTE, exp.LTE}
        if not (
            type(standard_comparison) in strict_types
            and type(student_comparison) in inclusive_types
            or type(student_comparison) in strict_types
            and type(standard_comparison) in inclusive_types
        ):
            continue
        outer_select = _nearest_select(standard_comparison)
        if not isinstance(outer_select, exp.Select):
            continue
        outer_sources = _query_block_sources(outer_select)
        if standard_column.table:
            source = next(
                (
                    source
                    for alias, source in outer_sources
                    if alias == _norm_name(standard_column.table)
                ),
                None,
            )
        else:
            derived_sources = [
                source
                for _alias, source in outer_sources
                if isinstance(source, exp.Subquery)
            ]
            source = derived_sources[0] if len(derived_sources) == 1 else None
        if not isinstance(source, exp.Subquery) or not isinstance(source.this, exp.Select):
            continue
        inner_select = source.this
        alias_name = _norm_name(standard_column.name)
        derived_expression = next(
            (
                item.this
                for item in inner_select.expressions or ()
                if isinstance(item, exp.Alias)
                and _norm_name(item.alias) == alias_name
            ),
            None,
        )
        if not isinstance(derived_expression, exp.Case):
            continue
        columns = list(derived_expression.find_all(exp.Column))
        slots_column = next(
            (column for column in columns if _norm_name(column.name) == "slots"),
            None,
        )
        rate_columns = [
            column
            for column in columns
            if _norm_name(column.name) in {"membercost", "guestcost"}
        ]
        if slots_column is None or not rate_columns:
            continue
        slots_ref = _query_column_ref_in_data(
            data,
            slots_column,
            inner_select,
            standard_ast,
        )
        rate_refs = [
            _query_column_ref_in_data(data, column, inner_select, standard_ast)
            for column in rate_columns
        ]
        rate_refs = [ref for ref in rate_refs if ref is not None]
        slots_actual = _actual_data_ref(data, slots_ref) if slots_ref else None
        if slots_actual is None or not slots_actual[0] or not rate_refs:
            continue
        rate_actuals = [
            actual
            for ref in rate_refs
            if (actual := _actual_data_ref(data, ref)) is not None and actual[0]
        ]
        if not rate_actuals:
            continue
        # Establish a real joined row before writing the expression inputs.
        _align_query_block_equality_row(
            data,
            inner_select,
            standard_ast,
            0,
            schema_catalog=schema_catalog,
        )
        _materialize_select_row_path(
            data,
            inner_select,
            row_index=0,
            schema_catalog=schema_catalog,
        )
        boundary = standard_scalar
        if isinstance(boundary, float):
            boundary_value: Any = boundary
        elif isinstance(boundary, Decimal):
            boundary_value = boundary
        else:
            boundary_value = int(boundary)
        with write_owner("materializer:derived_comparison_boundary"):
            slots_rows, slots_actual_column = slots_actual
            slots_rows[0][slots_actual_column] = boundary_value
            for actual in rate_actuals:
                rows, column = actual
                rows[0][column] = 1
        changed = True
    return changed


def _join_key_uniqueness_score(
    data: dict[str, list[dict[str, Any]]],
    ref: tuple[str, str],
    catalog: SchemaCatalog | None,
) -> int:
    """Rank which side of a teaching JOIN must remain one-row-per-key."""
    if _catalog_has_unary_unique_key(catalog, ref):
        return 100
    actual = _actual_data_ref(data, ref)
    if actual is None:
        return -1
    rows, column = actual
    columns = list(rows[0])
    score = 0
    if _is_primary_key_candidate(ref[0], column, columns):
        score += 20
    normalized = _norm_name(column)
    if normalized == "id":
        score += 20
    elif normalized in _table_key_aliases(_norm_name(ref[0])):
        score += 10
    if columns and _norm_name(columns[0]) == normalized:
        score += 2
    return score


def _materialize_joined_having_count_boundary(
    data: dict[str, list[dict[str, Any]]],
    obligations: list[DistinguishingObligation],
    standard_sql: str,
    *,
    schema_catalog: SchemaCatalog | None = None,
) -> None:
    """Make one post-JOIN group contain exactly the declared COUNT boundary.

    A base-table group size is not the same thing as a joined group size.  In
    particular, repeating both sides of an equality JOIN turns a requested
    boundary ``b`` into ``b * b``.  This final materializer keeps the declared
    unique side one-row-per-key and repeats only the many side.

    The bounded implementation intentionally handles one two-table equality
    path.  More complex join graphs remain unverified instead of fabricating
    a base-table COUNT and reporting it as post-JOIN evidence.
    """
    ast = _parse_sql(standard_sql)
    if ast is None:
        return
    join_pairs = _join_on_column_pairs(standard_sql)
    if not join_pairs:
        return

    for obligation in obligations:
        spec = next(
            (
                item
                for item in obligation.hard_constraints
                if item.kind == "aggregate_boundary_group"
            ),
            None,
        )
        if spec is None or not isinstance(spec.value, (int, float, Decimal)):
            continue
        metadata = dict(spec.metadata)
        function = str(
            metadata.get("standard_aggregate_function") or "COUNT"
        ).upper()
        if function != "COUNT" or int(spec.value) != spec.value:
            continue
        boundary = int(spec.value)
        if boundary < 1:
            continue

        matching_select: exp.Select | None = None
        matching_count: exp.Count | None = None
        for having in ast.find_all(exp.Having):
            select = _nearest_select(having)
            if not isinstance(select, exp.Select):
                continue
            direct_tables = set(_direct_select_tables(select).values())
            if len(direct_tables) != 2:
                continue
            count_node = next(iter(having.find_all(exp.Count)), None)
            if isinstance(count_node, exp.Count):
                matching_select = select
                matching_count = count_node
                break
        if matching_select is None or matching_count is None:
            continue

        group = matching_select.args.get("group")
        if not isinstance(group, exp.Group):
            continue
        group_refs = [
            _column_ref_in_select(item, matching_select)
            for item in group.expressions
            if isinstance(item, exp.Column)
        ]
        group_refs = [item for item in group_refs if item is not None]
        group_relation = (
            group_refs[0][0]
            if group_refs
            else _norm_name(
                metadata.get("standard_source_table")
                or metadata.get("source_table")
                or spec.relation
            )
        )
        if not group_relation:
            continue

        count_column = matching_count.find(exp.Column)
        count_ref = (
            _column_ref_in_select(count_column, matching_select)
            if isinstance(count_column, exp.Column)
            else None
        )
        candidate_pairs = [
            pair
            for pair in join_pairs
            if group_relation in {pair[0][0], pair[1][0]}
            and (count_ref is None or count_ref[0] in {pair[0][0], pair[1][0]})
        ]
        direct_tables = set(_direct_select_tables(matching_select).values())
        candidate_pairs = [
            pair
            for pair in candidate_pairs
            if {pair[0][0], pair[1][0]} == direct_tables
        ]
        if len(candidate_pairs) != 1:
            continue
        left_ref, right_ref = candidate_pairs[0]

        left_declared_unique = _catalog_has_unary_unique_key(
            schema_catalog, left_ref
        )
        right_declared_unique = _catalog_has_unary_unique_key(
            schema_catalog, right_ref
        )
        if left_declared_unique and right_declared_unique:
            # A one-to-one equality path cannot produce COUNT > 1 without
            # violating the supplied schema.  Do not manufacture invalid data.
            continue
        if left_declared_unique != right_declared_unique:
            unique_ref, repeated_ref = (
                (left_ref, right_ref)
                if left_declared_unique
                else (right_ref, left_ref)
            )
        else:
            left_score = _join_key_uniqueness_score(
                data, left_ref, schema_catalog
            )
            right_score = _join_key_uniqueness_score(
                data, right_ref, schema_catalog
            )
            if left_score == right_score:
                unique_ref, repeated_ref = (
                    (left_ref, right_ref)
                    if left_ref[0] == group_relation
                    else (right_ref, left_ref)
                )
            elif left_score > right_score:
                unique_ref, repeated_ref = left_ref, right_ref
            else:
                unique_ref, repeated_ref = right_ref, left_ref

        unique_actual = _actual_data_ref(data, unique_ref)
        repeated_actual = _actual_data_ref(data, repeated_ref)
        if unique_actual is None or repeated_actual is None:
            continue
        unique_rows, unique_column = unique_actual
        repeated_rows, repeated_column = repeated_actual
        if not unique_rows or len(repeated_rows) < boundary:
            continue

        anchor = unique_rows[0].get(unique_column)
        if anchor is None:
            anchor = _seed_value(unique_column, 0)
        distinct = bool(metadata.get("standard_aggregate_distinct", False))
        if (
            distinct
            and count_ref is not None
            and count_ref[0] == unique_ref[0]
            and boundary > 1
        ):
            continue

        with write_owner(
            f"materializer:{obligation.id}:joined_aggregate_boundary"
        ):
            # Preserve one and only one matching row on the unique side.
            used_unique: set[Any] = {anchor}
            unique_rows[0][unique_column] = anchor
            for index, row in enumerate(unique_rows[1:], start=1):
                current = row.get(unique_column)
                if current is None or current in used_unique:
                    current = _unique_key_value(
                        unique_column,
                        index,
                        used_unique,
                        anchor,
                    )
                    row[unique_column] = current
                used_unique.add(current)

            # Exactly ``boundary`` rows match the unique anchor.  Every later
            # row receives a value outside the unique-side domain, so it
            # cannot silently create another joined group at the boundary.
            unique_domain = [
                row.get(unique_column)
                for row in unique_rows[1:]
                if row.get(unique_column) is not None
            ]
            domain_use_count: Counter[Any] = Counter()
            unmatched_values = set(used_unique)
            for index, row in enumerate(repeated_rows):
                if index < boundary:
                    row[repeated_column] = anchor
                    continue
                reusable_parent = next(
                    (
                        value
                        for value in unique_domain
                        if domain_use_count[value] < max(0, boundary - 1)
                    ),
                    None,
                )
                if reusable_parent is not None:
                    row[repeated_column] = reusable_parent
                    domain_use_count[reusable_parent] += 1
                    continue
                replacement = _unique_key_value(
                    repeated_column,
                    len(unique_rows) + index + 1,
                    unmatched_values,
                    anchor,
                )
                row[repeated_column] = replacement
                unmatched_values.add(replacement)

            if repeated_ref[0] == group_relation:
                for group_ref in group_refs:
                    if group_ref[0] != repeated_ref[0]:
                        continue
                    group_actual = _actual_data_ref(data, group_ref)
                    if group_actual is None:
                        continue
                    group_rows, group_column = group_actual
                    group_anchor = group_rows[0].get(group_column)
                    for row in group_rows[:boundary]:
                        row[group_column] = group_anchor

            # A generated parent table may have had its display/group column
            # normalized by an earlier generic HAVING probe (for example all
            # ``s.nome`` values become ``100``).  Reusing the parent's other
            # join keys is valid, but leaving that display column identical
            # makes every post-JOIN row part of the boundary group.  Keep the
            # anchor group intact and split only the non-participating group
            # keys.  Never rewrite a join endpoint here: changing it would
            # invalidate the cardinality witness we just established.
            join_endpoints = {
                (left_ref[0], left_ref[1]),
                (right_ref[0], right_ref[1]),
            }
            for group_ref in group_refs:
                if group_ref in join_endpoints:
                    continue
                group_actual = _actual_data_ref(data, group_ref)
                if group_actual is None:
                    continue
                group_rows, group_column = group_actual
                if not group_rows:
                    continue
                anchor_value = group_rows[0].get(group_column)
                for index, row in enumerate(group_rows[1:], start=1):
                    candidate = _group_probe_value(group_column, index, 60)
                    if candidate == anchor_value:
                        candidate = _group_probe_value(group_column, index + 1, 61)
                    row[group_column] = candidate
                if repeated_ref[0] == group_relation:
                    # The repeated-side group column must remain equal for the
                    # exact participating prefix; split its tail only.
                    for index, row in enumerate(group_rows[boundary:], start=boundary):
                        candidate = _group_probe_value(group_column, index, 62)
                        if candidate == anchor_value:
                            candidate = _group_probe_value(group_column, index + 1, 63)
                        row[group_column] = candidate

            if count_ref is not None:
                count_actual = _actual_data_ref(data, count_ref)
                if count_actual is not None:
                    count_rows, count_column_name = count_actual
                    participating = (
                        count_rows[:boundary]
                        if count_ref[0] == repeated_ref[0]
                        else count_rows[:1]
                    )
                    for index, row in enumerate(participating):
                        if distinct:
                            row[count_column_name] = 900000 + index
                        elif row.get(count_column_name) is None:
                            row[count_column_name] = _seed_value(
                                count_column_name, index
                            )

            for row_index in range(
                max((len(rows) for rows in data.values()), default=0)
            ):
                _set_select_local_literal_predicates(
                    data,
                    matching_select,
                    row_index,
                )


def _materialize_set_grouped_branch_path(
    data: dict[str, list[dict[str, Any]]],
    obligations: list[DistinguishingObligation],
    standard_sql: str,
    student_sql: str,
    *,
    schema_catalog: SchemaCatalog | None = None,
) -> bool:
    """Make a grouped right branch reachable for bounded EXCEPT/UNION worlds."""
    if not any(
        constraint.kind == "set_left_right_overlap"
        for obligation in obligations
        for constraint in obligation.hard_constraints
    ):
        return False
    standard_ast = _parse_sql(standard_sql)
    student_ast = _parse_sql(student_sql)
    standard_set = _set_operator_node(standard_ast)
    student_set = _set_operator_node(student_ast)
    if not isinstance(standard_set, (exp.Except, exp.Union)) or not isinstance(
        student_set, (exp.Except, exp.Union)
    ):
        return False
    if {type(standard_set), type(student_set)} != {exp.Except, exp.Union}:
        return False

    right = standard_set.expression
    select = right if isinstance(right, exp.Select) else right.find(exp.Select)
    if not isinstance(select, exp.Select):
        return False
    direct_tables = set(_direct_select_tables(select).values())
    if len(direct_tables) != 2:
        return False
    branch_sql = _sql_of(select)
    if len(_join_on_column_pairs(branch_sql)) != 1:
        return False
    group = select.args.get("group")
    having = select.args.get("having")
    if not isinstance(group, exp.Group) or not isinstance(having, exp.Having):
        return False

    comparison = next(
        (
            item
            for item in having.find_all(
                exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE
            )
            if item.find_ancestor(exp.Select) is select
            and isinstance(item.left, exp.Count)
            and isinstance(item.right, exp.Literal)
        ),
        None,
    )
    if comparison is None:
        return False
    count = comparison.left
    if bool(count.args.get("distinct") or isinstance(count.this, exp.Distinct)):
        return False
    required_count = next(
        (
            candidate
            for candidate in range(1, _MAX_WITNESS_ROWS_PER_TABLE + 1)
            if _comparison_matches(comparison, candidate)
        ),
        None,
    )
    if required_count is None:
        return False
    group_refs = [
        _column_ref_in_select(item, select)
        for item in group.expressions or ()
        if isinstance(item, exp.Column)
    ]
    group_refs = [item for item in group_refs if item is not None]
    if not group_refs:
        return False
    group_relation = group_refs[0][0]
    argument = count.this.sql(dialect="sqlite") if count.this is not None else "*"
    owner = next(
        (
            obligation
            for obligation in obligations
            if any(
                constraint.kind == "set_left_right_overlap"
                for constraint in obligation.hard_constraints
            )
        ),
        None,
    )
    if owner is None:
        return False
    synthetic = DistinguishingObligation(
        id=f"{owner.id}:grouped_right_branch",
        diff_id=owner.diff_id,
        diff_type=owner.diff_type,
        clause="HAVING",
        knowledge_point_id=owner.knowledge_point_id,
        required_tables=set(direct_tables),
        hard_constraints=[ConstraintSpec(
            "aggregate_boundary_group",
            group_relation,
            argument,
            required_count,
            metadata=(
                ("standard_aggregate_function", "COUNT"),
                ("standard_aggregate_argument", argument),
                ("standard_aggregate_distinct", False),
                (
                    "standard_group_columns",
                    tuple(item.sql(dialect="sqlite") for item in group.expressions),
                ),
                ("standard_source_table", group_relation),
            ),
        )],
    )
    _materialize_joined_having_count_boundary(
        data,
        [synthetic],
        branch_sql,
        schema_catalog=schema_catalog,
    )
    return True




def _materialize_grouped_order_obligation_witness(
    data: dict[str, list[dict[str, Any]]],
    standard_sql: str,
    student_sql: str,
    order_diff: ASTDiffNode,
    *,
    schema_catalog: SchemaCatalog | None = None,
) -> bool:
    """Create two reachable aggregate groups for an ORDER BY mutation.

    ``ORDER BY count_alias`` is a result-level operation: the sort key is not
    a physical source column and the old order probe consequently had no safe
    cell to write.  This adapter handles the bounded teaching shape where a
    direct grouped query orders by a COUNT alias.  It deliberately refuses
    derived/opaque blocks, unique grouping keys, and non-COUNT aggregates;
    those cases remain honest known boundaries instead of receiving a guessed
    source value.
    """
    standard_ast = _parse_sql(standard_sql)
    student_ast = _parse_sql(student_sql)
    standard_select = _top_select(standard_ast) if standard_ast is not None else None
    student_select = _top_select(student_ast) if student_ast is not None else None
    if not isinstance(standard_select, exp.Select) or not isinstance(
        student_select, exp.Select
    ):
        return False
    group = standard_select.args.get("group")
    standard_order = standard_select.args.get("order")
    student_order = student_select.args.get("order")
    if not isinstance(group, exp.Group) or not isinstance(standard_order, exp.Order):
        return False
    if not isinstance(student_order, exp.Order):
        return False

    # Only direct physical FROM/JOIN sources are safe here.  A CTE or derived
    # source needs its own cardinality/path materializer and must not be
    # approximated by editing an arbitrary base table.
    sources = _query_block_sources(standard_select)
    if not sources or any(not isinstance(source, exp.Table) for _, source in sources):
        return False
    if any(
        _query_source_select(source, standard_ast) is not None
        for _, source in sources
    ):
        return False

    standard_item = standard_order.expressions[0] if standard_order.expressions else None
    if not isinstance(standard_item, exp.Ordered):
        return False
    standard_key = standard_item.this
    if not isinstance(standard_key, exp.Column):
        return False
    alias_name = _norm_name(standard_key.name)
    aggregate: exp.Count | None = None
    for item in standard_select.expressions or ():
        if not isinstance(item, exp.Alias) or _norm_name(item.alias) != alias_name:
            continue
        candidate = item.this
        if isinstance(candidate, exp.Count):
            aggregate = candidate
        break
    if aggregate is None:
        return False

    group_refs: list[tuple[str, str]] = []
    for expression in group.expressions or ():
        if not isinstance(expression, exp.Column):
            return False
        ref = _query_column_ref_in_data(data, expression, standard_select, standard_ast)
        if ref is None or ref in group_refs:
            continue
        group_refs.append(ref)
    if not group_refs:
        return False

    count_ref: tuple[str, str] | None = None
    count_distinct = bool(
        aggregate.args.get("distinct")
        or isinstance(aggregate.this, exp.Distinct)
    )
    count_column = (
        (aggregate.this.expressions[0] if aggregate.this.expressions else None)
        if isinstance(aggregate.this, exp.Distinct)
        else aggregate.this
    )
    if count_column is not None and not isinstance(count_column, exp.Star):
        if not isinstance(count_column, exp.Column):
            return False
        count_ref = _query_column_ref_in_data(
            data,
            count_column,
            standard_select,
            standard_ast,
        )
        if count_ref is None:
            return False
        # COUNT(DISTINCT group_key) is one for every group in this shape and
        # cannot expose an ORDER BY change without a second independent
        # cardinality dimension.
        if count_ref in group_refs:
            return False

    actual_group_refs: list[tuple[list[dict[str, Any]], str, tuple[str, str]]] = []
    for ref in group_refs:
        actual = _actual_data_ref(data, ref)
        if actual is None or len(actual[0]) < 3:
            return False
        actual_group_refs.append((actual[0], actual[1], ref))

    # A unique GROUP BY key cannot be collapsed into two rows without
    # violating the schema's identity contract.  The adapter is allowed to
    # use descriptive dimensions (role/category, category/sponsor, ...), not
    # primary-key duplication.
    for rows, column, ref in actual_group_refs:
        if _catalog_has_unary_unique_key(schema_catalog, ref) or _is_primary_key_candidate(
            next((name for name, candidate in data.items() if candidate is rows), ""),
            column,
            list(rows[0]),
        ):
            return False

    join_edges = _query_block_equality_edges(data, standard_select, standard_ast)

    def bucket(index: int) -> int:
        return 0 if index < 2 else 1

    with write_owner("materializer:grouped_order_cardinality"):
        # First make the two logical group labels visible on every dimension.
        # Remaining rows join the second group, so the groups have cardinality
        # 2 and N-2 rather than relying on a coincidental seed distribution.
        for rows, column, ref in actual_group_refs:
            for index, row in enumerate(rows):
                row[column] = _group_probe_value(column, bucket(index), 210 + len(ref[1]))

        # Reconnect foreign-key sides after the group labels are assigned.
        # For each bucket, point a non-unique endpoint at one stable unique
        # endpoint.  This preserves person/session identities for COUNT(DISTINCT)
        # while coalescing dimensions such as category and sponsor.
        for left_ref, right_ref in join_edges:
            # COUNT(DISTINCT source_key) needs one distinct source identity
            # per representative row.  Coalescing its foreign-key edge would
            # silently turn the intended two-member group into COUNT=1 and
            # erase the ordering witness.  Descriptive group columns already
            # carry the shared bucket label, so this edge need not be merged.
            if count_distinct and count_ref in {left_ref, right_ref}:
                continue
            left_actual = _actual_data_ref(data, left_ref)
            right_actual = _actual_data_ref(data, right_ref)
            if left_actual is None or right_actual is None:
                continue
            left_rows, left_column = left_actual
            right_rows, right_column = right_actual
            left_table = next((name for name, rows in data.items() if rows is left_rows), "")
            right_table = next((name for name, rows in data.items() if rows is right_rows), "")
            left_unique = _catalog_has_unary_unique_key(schema_catalog, left_ref) or _is_primary_key_candidate(
                left_table, left_column, list(left_rows[0])
            )
            right_unique = _catalog_has_unary_unique_key(schema_catalog, right_ref) or _is_primary_key_candidate(
                right_table, right_column, list(right_rows[0])
            )
            if left_unique and right_unique:
                continue
            if right_unique and not left_unique:
                source_rows, source_column = right_rows, right_column
                target_rows, target_column = left_rows, left_column
            elif left_unique and not right_unique:
                source_rows, source_column = left_rows, left_column
                target_rows, target_column = right_rows, right_column
            else:
                source_rows, source_column = left_rows, left_column
                target_rows, target_column = right_rows, right_column
            if not source_rows or not target_rows:
                continue
            for index, row in enumerate(target_rows):
                source_index = 0 if bucket(index) == 0 else min(2, len(source_rows) - 1)
                row[target_column] = source_rows[source_index].get(source_column)

        # The selected COUNT argument must be non-NULL on the participating
        # rows.  DISTINCT arguments remain untouched so their source identity
        # can provide the intended 2-vs-N cardinality.
        if count_ref is not None:
            count_actual = _actual_data_ref(data, count_ref)
            if count_actual is not None:
                count_rows, count_column = count_actual
                for index, row in enumerate(count_rows):
                    if row.get(count_column) is None:
                        row[count_column] = _seed_value(count_column, index)

        # Re-apply only local literal predicates on the two representative
        # paths.  This does not fabricate a predicate value for opaque query
        # blocks and keeps an existing WHERE clause from erasing the witness.
        for index in (0, 1, 2):
            _materialize_select_row_path(
                data,
                standard_select,
                row_index=index,
                schema_catalog=schema_catalog,
            )
    return True


def _materialize_nested_grouped_order_obligation_witness(
    data: dict[str, list[dict[str, Any]]],
    standard_sql: str,
    student_sql: str,
    *,
    schema_catalog: SchemaCatalog | None = None,
) -> bool:
    """Create two reachable groups for an outer ORDER BY on a derived table."""
    standard_ast = _parse_sql(standard_sql)
    if standard_ast is None:
        return False
    outer = _top_select(standard_ast)
    if not isinstance(outer, exp.Select):
        return False
    order = outer.args.get("order")
    if not isinstance(order, exp.Order) or not order.expressions:
        return False
    first = order.expressions[0]
    order_expression = first.this if isinstance(first, exp.Ordered) else first
    if not isinstance(order_expression, exp.Column):
        return False
    order_alias = _norm_name(order_expression.name)
    candidate: tuple[exp.Select, exp.Subquery] | None = None
    for _alias, source in _query_block_sources(outer):
        if not isinstance(source, exp.Subquery):
            continue
        inner = _query_source_select(source, standard_ast)
        if not isinstance(inner, exp.Select):
            continue
        if not isinstance(inner.args.get("group"), exp.Group):
            continue
        projection_names = {
            _norm_name(item.alias_or_name)
            for item in inner.expressions or ()
            if _norm_name(item.alias_or_name)
        }
        if order_alias in projection_names or not order_expression.table:
            candidate = (inner, source)
            break
        if _norm_name(order_expression.table) == _norm_name(source.alias or ""):
            candidate = (inner, source)
            break
    if candidate is None:
        return False
    inner, _source = candidate
    group = inner.args.get("group")
    if not isinstance(group, exp.Group):
        return False

    # Bring CTE/derived input paths into the same two rows before assigning
    # group keys. This is intentionally limited to direct local predicates and
    # equality joins; aggregate/window outputs remain execution-owned.
    all_selects = list(standard_ast.find_all(exp.Select))
    if isinstance(standard_ast, exp.Select) and standard_ast not in all_selects:
        all_selects.append(standard_ast)
    global_join_refs = {
        ref
        for select in all_selects
        for edge in _query_block_equality_edges(data, select, standard_ast)
        for ref in edge
    }
    with write_owner("materializer:nested_grouped_order_path"):
        for select in reversed(all_selects):
            if select is outer:
                continue
            sources = _query_block_sources(select)
            is_cte_body = any(
                cte.this is select for cte in standard_ast.find_all(exp.CTE)
            )
            is_derived_body = any(
                subquery.this is select
                for subquery in standard_ast.find_all(exp.Subquery)
            )
            has_lineage_source = any(
                not isinstance(source, exp.Table)
                or _query_cte_select(standard_ast, source.name) is not None
                for _alias, source in sources
            )
            if not (is_cte_body or is_derived_body or has_lineage_source):
                continue
            for row_index in (0, 1):
                _set_query_block_rich_predicates(
                    data,
                    select,
                    standard_ast,
                    row_index,
                )
                _materialize_select_row_path(
                    data,
                    select,
                    row_index=row_index,
                    schema_catalog=schema_catalog,
                )
                _align_query_block_equality_row(
                    data,
                    select,
                    standard_ast,
                    row_index,
                    schema_catalog=schema_catalog,
                )

        group_refs: list[tuple[list[dict[str, Any]], str]] = []
        for expression in group.expressions or ():
            if not isinstance(expression, exp.Column):
                continue
            ref = _query_column_ref_in_data(data, expression, inner, standard_ast)
            actual = _actual_data_ref(data, ref) if ref else None
            if actual is not None and actual not in group_refs:
                group_refs.append(actual)
        if not group_refs:
            return False
        for rows, column in group_refs:
            if len(rows) < 2:
                return False
            if rows[0].get(column) == rows[1].get(column):
                rows[1][column] = _strict_path_variant(rows[0].get(column), 1)

        # Aggregates frequently order a CASE-derived measure, for example
        # ``SUM(CASE WHEN room.use IN (...) THEN room.area ELSE 0 END)``.
        # The rows may be joinable while the CASE condition is false for every
        # row, producing two equal zero-valued sort keys.  Materialize only
        # simple CASE predicates whose source cell is unambiguous.
        for case_node in inner.find_all(exp.Case):
            for if_node in case_node.args.get("ifs") or ():
                predicate = if_node.this if isinstance(if_node, exp.If) else None
                if not isinstance(predicate, exp.Expression):
                    continue
                for predicate_node in predicate.walk():
                    if not isinstance(predicate_node, exp.In):
                        continue
                    resolved = _rich_predicate_truth_value(predicate_node, True)
                    if resolved is None:
                        continue
                    column_node, value = resolved
                    ref = _query_column_ref_in_data(
                        data,
                        column_node,
                        inner,
                        standard_ast,
                    )
                    actual = _actual_data_ref(data, ref) if ref else None
                    if actual is None:
                        continue
                    rows, column = actual
                    for row_index in (0, 1):
                        if row_index < len(rows):
                            rows[row_index][column] = value

        # Prefer a numeric physical input to the ordered aggregate.  The
        # q30-style SUM(CASE ... area ...) shape then has 10/20 group values;
        # q87-style COUNT(CASE ...) is still separated by the group keys above.
        aggregate_input: tuple[list[dict[str, Any]], str] | None = None
        join_refs = global_join_refs
        for aggregate in inner.find_all(exp.AggFunc):
            for column_node in aggregate.find_all(exp.Column):
                ref = _query_column_ref_in_data(
                    data,
                    column_node,
                    inner,
                    standard_ast,
                )
                actual = _actual_data_ref(data, ref) if ref else None
                if actual is None or len(actual[0]) < 2:
                    continue
                # COUNT(DISTINCT join_key) is common in reporting SQL. Its
                # input is structural; changing it to create a numerical sort
                # gap would disconnect the very row path that the aggregate
                # needs (the q87 member-MIT-ID case).
                if ref in join_refs:
                    continue
                if _is_numeric_column(actual[1]) or all(
                    isinstance(row.get(actual[1]), (int, float, Decimal))
                    for row in actual[0][:2]
                    if row.get(actual[1]) is not None
                ):
                    aggregate_input = actual
                    break
            if aggregate_input is not None:
                break
        if aggregate_input is not None:
            rows, column = aggregate_input
            low, high = _order_materializer_values(column)
            rows[0][column], rows[1][column] = low, high

        for row_index in (0, 1):
            _materialize_select_row_path(
                data,
                outer,
                row_index=row_index,
                schema_catalog=schema_catalog,
            )
            _align_query_block_equality_row(
                data,
                outer,
                standard_ast,
                row_index,
                schema_catalog=schema_catalog,
            )
    return True


def _materialize_order_obligation_witness(
    data: dict[str, list[dict[str, Any]]],
    standard_sql: str,
    student_sql: str,
    ast_diffs: list[ASTDiffNode],
    *,
    schema_catalog: SchemaCatalog | None = None,
) -> None:
    """Materialize only the ordering topology owned by the current world."""
    if any(
        diff.diff_type in {"window_over_changed", "window_function_changed"}
        for diff in ast_diffs
    ):
        return
    order_diff = next(
        (
            diff for diff in ast_diffs
            if diff.diff_type in {
                "order_by_changed",
                "order_direction_changed",
                "order_by_tiebreaker_missing",
                "order_by_key_added",
                "order_nulls_changed",
            }
        ),
        None,
    )
    if order_diff is None:
        return

    # The web mutation layer commonly rewrites ``ORDER BY alias DESC`` to a
    # NULL-ranking CASE followed by ``alias ASC``.  It is still one semantic
    # ordering mutation, but it has no legacy ``standard_order_keys`` metadata.
    # Handle direct physical keys and grouped COUNT aliases from the actual AST
    # before falling back to the older metadata-driven forms.
    if order_diff.diff_type == "order_by_changed":
        if _materialize_grouped_order_obligation_witness(
            data,
            standard_sql,
            student_sql,
            order_diff,
            schema_catalog=schema_catalog,
        ):
            return
        if _materialize_nested_grouped_order_obligation_witness(
            data,
            standard_sql,
            student_sql,
            schema_catalog=schema_catalog,
        ):
            return
        standard_ast = _parse_sql(standard_sql)
        # ``order_by_changed`` stores clause fragments rather than full query
        # text.  The direct-key fallback only needs the standard AST and the
        # physical source cell, so it remains safe without reconstructing the
        # student query here.
        standard_select = _top_select(standard_ast) if standard_ast is not None else None
        if isinstance(standard_select, exp.Select):
            order = standard_select.args.get("order")
            first = order.expressions[0] if isinstance(order, exp.Order) and order.expressions else None
            first_expression = first.this if isinstance(first, exp.Ordered) else first
            if isinstance(first_expression, exp.Column):
                ref = _query_column_ref_in_data(data, first_expression, standard_select, standard_ast)
                actual = _actual_data_ref(data, ref) if ref is not None else None
                if actual is not None and len(actual[0]) >= 2:
                    rows, column = actual
                    for index in (0, 1):
                        _materialize_select_row_path(
                            data,
                            standard_select,
                            row_index=index,
                            schema_catalog=schema_catalog,
                        )
                    low, high = _order_materializer_values(column)
                    with write_owner("materializer:order_ast_key_separation"):
                        rows[0][column] = low
                        rows[1][column] = high
                    return
    standard_keys = _materialized_order_keys(
        order_diff.extra.get("standard_order_keys")
    )
    student_keys = _materialized_order_keys(
        order_diff.extra.get("student_order_keys")
    )
    prefix_keys: list[tuple[str, bool]]
    discriminator_key: tuple[str, bool] | None = None
    if order_diff.diff_type in {"order_direction_changed", "order_nulls_changed"}:
        changed = next(
            (
                index
                for index, (standard, student) in enumerate(zip(standard_keys, student_keys))
                if standard[0].lower() == student[0].lower()
                and (
                    standard[1] != student[1]
                    if order_diff.diff_type == "order_direction_changed"
                    else standard[1] == student[1]
                )
            ),
            None,
        )
        if order_diff.diff_type == "order_nulls_changed":
            standard_nulls = tuple(order_diff.extra.get("standard_nulls_first") or ())
            student_nulls = tuple(order_diff.extra.get("student_nulls_first") or ())
            changed = next(
                (
                    index
                    for index, (standard, student) in enumerate(zip(standard_keys, student_keys))
                    if standard[0].lower() == student[0].lower()
                    and standard[1] == student[1]
                    and index < len(standard_nulls)
                    and index < len(student_nulls)
                    and standard_nulls[index] != student_nulls[index]
                ),
                None,
            )
        if changed is None:
            return
        prefix_keys = standard_keys[:changed]
        discriminator_key = standard_keys[changed]
        reverse_reference_order = False
    elif order_diff.diff_type == "order_by_tiebreaker_missing":
        changed = len(student_keys)
        if len(standard_keys) <= changed:
            return
        prefix_keys = standard_keys[:changed]
        discriminator_key = standard_keys[changed]
        reverse_reference_order = True
    else:
        changed = len(standard_keys)
        if len(student_keys) <= changed:
            return
        prefix_keys = standard_keys
        discriminator_key = student_keys[changed]
        reverse_reference_order = True

    requested = [
        _simple_materialized_order_column(expression)
        for expression, _descending in (*prefix_keys, discriminator_key)
    ]
    if any(column is None for column in requested):
        return
    source_table = _norm_name(str(order_diff.extra.get("standard_source_table") or ""))
    table_name = next(
        (name for name in data if source_table and _norm_name(name) == source_table),
        None,
    )
    if not table_name:
        return
    rows = data.get(table_name) or []
    if len(rows) < 2:
        return
    lookup = _column_lookup(list(rows[0]))
    resolved = [lookup.get(str(column)) for column in requested]
    if any(column is None for column in resolved):
        return
    prefix_columns = [str(column) for column in resolved[:-1]]
    discriminator_column = str(resolved[-1])
    if order_diff.diff_type == "order_nulls_changed":
        with write_owner("materializer:order_null_separation"):
            rows[0][discriminator_column] = None
            non_null = next(
                (
                    row.get(discriminator_column)
                    for row in rows[1:]
                    if row.get(discriminator_column) is not None
                ),
                None,
            )
            if non_null is None:
                non_null, _ = _order_materializer_values(discriminator_column)
            rows[1][discriminator_column] = non_null
        return
    existing_pair = _existing_order_pair_indexes(
        rows,
        prefix_columns,
        discriminator_column,
    )
    left_index, right_index = existing_pair or (0, 1)
    left_row = rows[left_index]
    right_row = rows[right_index]
    with write_owner("materializer:order_key_separation"):
        if existing_pair is None:
            for column in prefix_columns:
                right_row[column] = left_row[column]
        low, high = _ordered_distinct_pair(
            left_row.get(discriminator_column),
            right_row.get(discriminator_column),
            discriminator_column,
        )
        descending = bool(discriminator_key[1])
        if reverse_reference_order:
            left_row[discriminator_column] = low if descending else high
            right_row[discriminator_column] = high if descending else low
        else:
            # Direction changes only require distinct values. Preserve the
            # generator's existing insertion order to avoid perturbing WHERE
            # predicates that already selected two valid rows.
            if left_row.get(discriminator_column) == right_row.get(discriminator_column):
                left_row[discriminator_column] = low
                right_row[discriminator_column] = high

        ast = _parse_sql(standard_sql)
        select = _top_select(ast) if ast is not None else None
        projected_columns = [
            lookup.get(_norm_name(node.name))
            for item in (select.expressions if isinstance(select, exp.Select) else ())
            for node in [item.this if isinstance(item, exp.Alias) else item]
            if isinstance(node, exp.Column)
        ]
        payload = next(
            (
                column for column in projected_columns
                if column
            ),
            None,
        )
        if (
            payload
            and payload != discriminator_column
            and left_row.get(payload) == right_row.get(payload)
        ):
            first, second = _order_materializer_values(str(payload))
            left_row[payload] = first
            right_row[payload] = second


def _materialize_in_list_obligation_witness(
    data: dict[str, list[dict[str, Any]]],
    standard_sql: str,
    ast_diffs: list[ASTDiffNode],
    *,
    schema_catalog: SchemaCatalog | None = None,
) -> None:
    """Make constant ``IN`` paths observable in the result projection.

    The membership validator only needs one listed and one outside value.  A
    bounded generator can still accidentally assign the same display value
    to both rows (the legacy title generator deliberately reuses a short
    cycle).  In that case ``IN`` and ``NOT IN`` select different source rows
    but produce equal projected tuples, so execution and atomic mutation both
    look equivalent.  This materializer changes only a directly projected
    payload column when the two paths are otherwise indistinguishable.
    """
    diff = next(
        (
            item for item in ast_diffs
            if (
                item.diff_type == "in_predicate_negation_changed"
                and not item.extra.get("standard_membership_table")
            )
            or item.diff_type in {"in_list_member_removed", "in_list_member_added"}
        ),
        None,
    )
    if diff is None:
        return
    source_table = _norm_name(str(diff.extra.get("standard_source_table") or ""))
    predicate_column = _norm_name(str(
        diff.extra.get("standard_outer_column")
        or diff.target_column
        or diff.extra.get("column")
        or ""
    ))
    listed = set(
        diff.extra.get("standard_in_values")
        or diff.extra.get("values")
        or ()
    )
    student_listed = set(diff.extra.get("student_values") or ())
    distinguishing = listed ^ student_listed if student_listed else set()
    if not predicate_column or not listed:
        return
    ast = _parse_sql(standard_sql)
    target_select = _top_select(ast) if ast is not None else None
    target_in = None
    if ast is not None:
        target_sql = re.sub(
            r"\s+",
            "",
            str(diff.extra.get("standard_sql") or ""),
        ).lower()
        for candidate in ast.find_all(exp.In):
            candidate_sql = re.sub(r"\s+", "", _sql_of(candidate)).lower()
            if target_sql and candidate_sql == target_sql:
                target_in = candidate
                break
        if target_in is not None:
            candidate_select = target_in.find_ancestor(exp.Select)
            if isinstance(candidate_select, exp.Select):
                target_select = candidate_select
            resolved_ref = _query_column_ref_in_data(
                data,
                target_in.this,
                target_select,
                ast,
            ) if isinstance(target_in.this, exp.Column) and isinstance(target_select, exp.Select) else None
            if resolved_ref is not None:
                source_table, predicate_column = resolved_ref
    if not source_table:
        candidates = [
            name for name, rows in data.items()
            if rows and predicate_column in _column_lookup(rows[0].keys())
        ]
        if len(candidates) != 1:
            return
        source_table = _norm_name(candidates[0])
    table_name = next(
        (name for name in data if _norm_name(name) == source_table),
        None,
    )
    rows = data.get(table_name or "") or []
    if not rows:
        return
    lookup = _column_lookup(list(rows[0]))
    predicate_actual = lookup.get(predicate_column)
    if not predicate_actual:
        return
    # ``in_predicate_negation_changed`` has no pre-existing listed/outside
    # pair in a freshly generated multi-table world.  Create the two values
    # first, then align the owning JOIN rows.  The old implementation returned
    # early in this case, leaving a valid ``IN`` query with an empty result and
    # turning an ordinary teaching mutation into a false known gap.
    if diff.diff_type == "in_predicate_negation_changed":
        listed_value = next(iter(sorted(listed, key=str)))
        control_values = (listed & student_listed) or listed
        control_value = next(iter(sorted(control_values, key=str)))
        outside_value = _counter_value(predicate_actual, listed_value)
        with write_owner("materializer:in_list_negation_paths"):
            rows[0][predicate_actual] = listed_value
            if len(rows) > 1:
                rows[1][predicate_actual] = outside_value
                if isinstance(target_select, exp.Select):
                    _align_query_block_equality_row(
                        data,
                        target_select,
                        ast,
                        0,
                        schema_catalog=schema_catalog,
                    )
                    _align_query_block_equality_row(
                        data,
                        target_select,
                        ast,
                        1,
                        schema_catalog=schema_catalog,
                    )
                    _materialize_select_row_path(
                        data,
                        target_select,
                        row_index=0,
                        schema_catalog=schema_catalog,
                    )
                    _materialize_select_row_path(
                        data,
                        target_select,
                        row_index=1,
                        schema_catalog=schema_catalog,
                    )
                # Keep the two semantic values owned by this adapter after
                # JOIN path alignment; the path helper does not understand IN
                # predicates, but future compatibility probes may touch the
                # same physical cell.
                rows[0][predicate_actual] = listed_value
                rows[1][predicate_actual] = outside_value
        return
    if distinguishing:
        with write_owner("materializer:in_list_membership_paths"):
            rows[0][predicate_actual] = next(iter(sorted(distinguishing, key=str)))
            if len(rows) > 1:
                control_values = (listed & student_listed) or listed
                rows[1][predicate_actual] = next(iter(sorted(control_values, key=str)))
        return
    matching = [
        index for index, row in enumerate(rows)
        if row.get(predicate_actual) in listed
    ]
    outside = [
        index for index, row in enumerate(rows)
        if row.get(predicate_actual) is not None
        and row.get(predicate_actual) not in listed
    ]
    if not matching or not outside:
        return

    select = target_select
    aliases = _table_aliases(ast) if ast is not None else {}
    projected_columns: list[str] = []
    if isinstance(select, exp.Select):
        for item in select.expressions or ():
            expression = item.this if isinstance(item, exp.Alias) else item
            if not isinstance(expression, exp.Column):
                continue
            qualifier = _norm_name(expression.table or "")
            if qualifier and aliases.get(qualifier, qualifier) != source_table:
                continue
            actual = lookup.get(_norm_name(expression.name))
            if actual and actual not in projected_columns:
                projected_columns.append(actual)
    if not projected_columns:
        return

    left_index, right_index = matching[0], outside[0]
    left_row, right_row = rows[left_index], rows[right_index]
    if any(left_row.get(column) != right_row.get(column) for column in projected_columns):
        return

    payload = next(
        (column for column in projected_columns if _norm_name(column) != predicate_column),
        projected_columns[0],
    )
    current = left_row.get(payload)
    if _is_numeric_column(payload):
        left_value, right_value = 910001, 910002
    elif _is_date_column(payload):
        left_value, right_value = "2099-01-01", "2099-01-02"
    else:
        suffix = _norm_name(payload) or "value"
        left_value = f"__in_list_match_{suffix}__"
        right_value = f"__in_list_outside_{suffix}__"
    if current is not None and isinstance(current, (int, float, Decimal)) and not _is_numeric_column(payload):
        return
    with write_owner("materializer:in_list_membership_paths"):
        left_row[payload] = left_value
        right_row[payload] = right_value


def _add_duplicate_probe(
    rows: list[dict[str, Any]],
    columns: list[str],
    table_name: str,
    standard_sql: str,
    student_sql: str,
    ast_diffs: list[dict[str, Any]] | None = None,
) -> None:
    """
    去重探测机制：在 Row 0 和 Row 1 的非主键字段上，复制生成完全重复的数据行。
    Distinct probe mechanism: clones values from Row 0 to Row 1 for non-key columns
    to trigger duplication mismatches if DISTINCT is missing in student SQL.
    """
    if len(rows) < 3 or not columns:
        return
    ast_diffs = ast_diffs or []
    probe_cols = _distinct_probe_columns_for_table(
        standard_sql,
        student_sql,
        table_name,
        columns,
    )
    if not probe_cols and not _has_diff(ast_diffs, "UNION") and not _has_set_operator(standard_sql, student_sql):
        return
    # A keyless single-table DISTINCT control is intentionally allowed to
    # remain a latent bounded case: without a declared identity column there
    # is no safe way to duplicate a source row without changing the intended
    # teaching fixture.  Keyed tables (and set-operation branches) still get
    # explicit duplicate witnesses.
    if not any(_is_key_column(col) for col in columns) and not _has_set_operator(standard_sql, student_sql):
        return
    # Set operators without a SELECT DISTINCT still use non-key payload columns.
    if not probe_cols:
        probe_cols = [col for col in columns if not _is_key_column(col)]
    # A DISTINCT over an ID-looking business key (product_id in a history table,
    # for example) explicitly needs duplicate source values. PK repair has already
    # run before this late probe, so keep the query-observable duplicate here.
    for col in probe_cols:
        rows[1][col] = rows[0][col]


def _distinct_probe_columns_for_table(
    standard_sql: str,
    student_sql: str,
    table_name: str,
    columns: list[str],
) -> list[str]:
    lookup = _column_lookup(columns)
    projected: list[str] = []
    for sql in (standard_sql, student_sql):
        ast = _parse_sql(sql)
        if not ast:
            continue
        cte_aliases = {_norm_name(cte.alias or "") for cte in ast.find_all(exp.CTE)}
        for select in ast.find_all(exp.Select):
            direct_tables: set[str] = set()
            source = _direct_from_table(select)
            if source:
                direct_tables.add(_norm_name(source.name))
            for join in select.args.get("joins") or []:
                if isinstance(join.this, exp.Table):
                    direct_tables.add(_norm_name(join.this.name))
            table_matches = not direct_tables or _norm_name(table_name) in direct_tables
            source_is_derived = bool(direct_tables & cte_aliases)
            if not table_matches and not source_is_derived:
                continue
            candidates: list[exp.Column] = []
            if select.args.get("distinct") and not select.args.get("group"):
                for item in select.expressions or []:
                    candidates.extend(
                        column for column in item.find_all(exp.Column)
                        if _nearest_select(column) is select
                    )
                    if isinstance(item, exp.Column):
                        candidates.append(item)
                where = select.args.get("where")
                if isinstance(where, exp.Where):
                    candidates.extend(
                        column for column in where.find_all(exp.Column)
                        if _nearest_select(column) is select
                    )
            for aggregate in select.find_all(exp.AggFunc):
                if _nearest_select(aggregate) is not select:
                    continue
                if not (aggregate.args.get("distinct") or isinstance(aggregate.this, exp.Distinct)):
                    continue
                column = aggregate.find(exp.Column)
                if (
                    isinstance(column, exp.Column)
                    and not _is_primary_key_candidate(table_name, column.name, columns)
                ):
                    candidates.append(column)
            for column in candidates:
                actual = lookup.get(_norm_name(column.name))
                if actual and actual not in projected:
                    projected.append(actual)
    return projected


def _apply_distinct_probes(
    data: dict[str, list[dict[str, Any]]],
    standard_sql: str,
    student_sql: str,
    ast_diffs: list[ASTDiffNode],
) -> None:
    distinct_shape_changed = any(
        diff.diff_type in {"distinct_changed", "aggregate_distinct_changed"}
        for diff in ast_diffs
    ) or _distinct_shape_changed(standard_sql, student_sql)
    if not distinct_shape_changed:
        return
    if distinct_shape_changed:
        aggregate_distinct_columns = {
            _aggregate_distinct_target_column(diff)
            for diff in ast_diffs
            if diff.diff_type == "aggregate_distinct_changed"
        }
        aggregate_distinct_columns.discard("")
        has_top_level_distinct_diff = any(
            diff.diff_type == "distinct_changed" for diff in ast_diffs
        )
        for table_name, rows in data.items():
            if not rows:
                continue
            if (
                aggregate_distinct_columns
                and not has_top_level_distinct_diff
                and all(
                    _is_primary_key_candidate(table_name, column, list(rows[0]))
                    for column in aggregate_distinct_columns
                    if column in {_norm_name(name) for name in rows[0]}
                )
                and any(
                    column in {_norm_name(name) for name in rows[0]}
                    for column in aggregate_distinct_columns
                )
            ):
                continue
            _add_duplicate_probe(
                rows,
                list(rows[0]),
                table_name,
                standard_sql,
                student_sql,
                ast_diffs,
            )

    _apply_distinct_self_join_path_probe(data, standard_sql, student_sql)
    _apply_grouped_distinct_probe(data, standard_sql, student_sql)
    _apply_select_distinct_group_probe(data, standard_sql, student_sql)
    _apply_distinct_cte_case_sum_probe(data, standard_sql, student_sql)
    asts = [_parse_sql(standard_sql), _parse_sql(student_sql)]
    lead_lag_ast = next(
        (ast for ast in asts if ast and ast.find(exp.Lead) and ast.find(exp.Lag)),
        None,
    )
    if not lead_lag_ast:
        return
    outer = _top_select(lead_lag_ast)
    projection = outer.expressions[0] if isinstance(outer, exp.Select) and outer.expressions else None
    projection = projection.this if isinstance(projection, exp.Alias) else projection
    order = lead_lag_ast.find(exp.Order)
    ordered = order.expressions[0] if isinstance(order, exp.Order) and order.expressions else None
    order_column = ordered.this if isinstance(ordered, exp.Ordered) else ordered
    if not isinstance(projection, exp.Column) or not isinstance(order_column, exp.Column):
        return
    for rows in data.values():
        if len(rows) < 5:
            continue
        lookup = _column_lookup(list(rows[0]))
        value_col = lookup.get(_norm_name(projection.name))
        order_col = lookup.get(_norm_name(order_column.name))
        if not value_col or not order_col:
            continue
        for index, row in enumerate(rows[:5]):
            row[value_col] = 777
            row[order_col] = index + 1
        return


def _apply_null_aggregate_probe(
    rows: list[dict[str, Any]],
    columns: list[str],
    standard_sql: str,
    student_sql: str,
) -> None:
    """Inject NULL when aggregate denominator/null semantics differ."""
    if not rows:
        return
    asts = [_parse_sql(standard_sql), _parse_sql(student_sql)]
    count_star = any(
        ast and any(not list(node.find_all(exp.Column)) for node in ast.find_all(exp.Count))
        for ast in asts
    )
    if not count_star:
        return
    candidate_columns: list[str] = []
    for ast in asts:
        if not ast:
            continue
        for node in ast.find_all(exp.Avg, exp.Sum, exp.Count):
            column = node.find(exp.Column)
            if column:
                candidate_columns.append(column.name)
    lookup = _column_lookup(columns)
    actual = next(
        (lookup[_norm_name(column)] for column in candidate_columns if _norm_name(column) in lookup),
        None,
    )
    if actual and not _is_primary_key_candidate("", actual, columns):
        rows[0][actual] = None


def _apply_subquery_membership_probe(
    rows: list[dict[str, Any]],
    columns: list[str],
    table_name: str,
    standard_sql: str,
    student_sql: str,
) -> None:
    asts = [_parse_sql(standard_sql), _parse_sql(student_sql)]
    membership_targets: set[str] = set()
    for ast in asts:
        if not ast:
            continue
        for in_node in ast.find_all(exp.In):
            subquery = in_node.args.get("query")
            if not isinstance(subquery, exp.Subquery):
                continue
            for table in subquery.find_all(exp.Table):
                membership_targets.add(_norm_name(table.name))
    if _norm_name(table_name) not in membership_targets:
        return
    if any(
        subquery.find(exp.Having)
        and any(_norm_name(table.name) == _norm_name(table_name) for table in subquery.find_all(exp.Table))
        for ast in asts if ast
        for subquery in ast.find_all(exp.Subquery)
    ):
        return

    lookup = _column_lookup(columns)
    member_col = next((lookup[col] for col in lookup if col in {"agent_id", "seller_id", "dept_id", "user_id", "customer_id"}), None)
    if member_col is None:
        member_col = next((lookup[col] for col in lookup if col.endswith("_id") and lookup[col] != "id"), None)
    if member_col is None:
        member_col = next((lookup[col] for col in lookup if col != "id" and (col.endswith("id") or col == "id")), None)
    measure_col = next((lookup[col] for col in lookup if col in {"amount", "salary", "score", "price"} or (_is_numeric_column(lookup[col]) and lookup[col] != member_col)), None)
    if not rows or not member_col or not measure_col:
        return

    # Extract boundary values from subquery WHERE clauses for dynamic thresholds
    thresholds: list[int | float] = []
    for ast in asts:
        if not ast:
            continue
        for subquery in ast.find_all(exp.Subquery):
            for cmp in subquery.find_all(exp.GT, exp.GTE, exp.LT, exp.LTE, exp.EQ):
                for side in (cmp.right, cmp.left):
                    if isinstance(side, exp.Literal):
                        val = _literal_value(side)
                        if isinstance(val, (int, float, Decimal)):
                            thresholds.append(val)
    T = max(thresholds) if thresholds else 1000
    lo = T - 1
    hi = T + 1

    pattern = [
        (1, hi), (1, T),    # both high and low
        (2, T), (2, lo),    # only low
        (3, hi), (3, T + 2), # only high
        (4, lo - 2), (4, lo),  # neither
    ]
    for idx, row in enumerate(rows):
        member_value, measure_value = pattern[idx % len(pattern)]
        if _is_primary_key_candidate(table_name, member_col, columns):
            member_value = _seed_value(member_col, idx)
        # Preserve NULL values injected by earlier probes (dangling tuple, join drift)
        if row.get(member_col) is None and member_col != measure_col:
            row[measure_col] = measure_value
            continue
        row[member_col] = member_value
        row[measure_col] = measure_value


def _align_query_block_equality_row(
    data: dict[str, list[dict[str, Any]]],
    select: exp.Select,
    root_ast: exp.Expression,
    row_index: int,
    *,
    schema_catalog: SchemaCatalog | None = None,
) -> bool:
    """Align one query-block JOIN path without collapsing unique endpoints."""
    edges = _query_block_equality_edges(data, select, root_ast)
    if not edges:
        return False
    parent: dict[tuple[str, str], tuple[str, str]] = {}

    def find(ref: tuple[str, str]) -> tuple[str, str]:
        parent.setdefault(ref, ref)
        if parent[ref] != ref:
            parent[ref] = find(parent[ref])
        return parent[ref]

    for left, right in edges:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[right_root] = left_root
    components: dict[tuple[str, str], list[tuple[str, str]]] = defaultdict(list)
    for ref in parent:
        components[find(ref)].append(ref)

    def is_unique(ref: tuple[str, str]) -> bool:
        actual = _actual_data_ref(data, ref)
        if actual is None or not actual[0]:
            return False
        rows, column = actual
        table_name = next(
            (name for name, candidate in data.items() if candidate is rows),
            ref[0],
        )
        return bool(
            _catalog_has_unary_unique_key(schema_catalog, ref)
            or _is_primary_key_candidate(table_name, column, list(rows[0]))
        )

    touched = False
    with write_owner("materializer:query_block_equality_row"):
        for refs in components.values():
            if not refs:
                continue
            unique_refs = [ref for ref in refs if is_unique(ref)]
            ordered_refs = unique_refs + [ref for ref in refs if ref not in unique_refs]
            anchor = None
            for ref in ordered_refs:
                actual = _actual_data_ref(data, ref)
                if actual is None or row_index >= len(actual[0]):
                    continue
                value = actual[0][row_index].get(actual[1])
                if value is not None:
                    anchor = value
                    break
            if anchor is None:
                anchor = _seed_value(refs[0][1], row_index)
            for ref in refs:
                actual = _actual_data_ref(data, ref)
                if actual is None or row_index >= len(actual[0]):
                    continue
                if is_unique(ref):
                    current = actual[0][row_index].get(actual[1])
                    if current is not None and current != anchor:
                        continue
                actual[0][row_index][actual[1]] = anchor
                touched = True
    return touched


def _materialize_query_block_reachability(
    data: dict[str, list[dict[str, Any]]],
    standard_sql: str,
    student_sql: str,
    ast_diffs: list[ASTDiffNode],
    *,
    schema_catalog: SchemaCatalog | None = None,
) -> bool:
    """Propagate bounded reachability through derived tables and CTEs.

    The database payload contains physical tables, while a real teaching
    query often filters and joins through several named query blocks.  This
    adapter resolves only simple column lineage and equality joins.  It makes
    a concrete source row reachable; it does not synthesize aggregate/window
    results or assume that an opaque expression has a physical cell.
    """
    if not any(
        diff.diff_type in {
            "distinct_changed",
            "subquery_added",
            "subquery_removed",
            "correlated_predicate_changed",
            "cte_changed",
            "predicate_missing",
            "predicate_added",
            "comparison_operator_changed",
            "literal_changed",
            "in_predicate_negation_changed",
            "in_list_member_removed",
            "in_list_member_added",
        }
        for diff in ast_diffs
    ):
        return False
    root_ast = _parse_sql(standard_sql)
    if root_ast is None:
        return False
    selects = list(root_ast.find_all(exp.Select))
    if isinstance(root_ast, exp.Select) and root_ast not in selects:
        selects.append(root_ast)
    has_nested_query = bool(
        root_ast.find(exp.Subquery)
        or root_ast.find(exp.CTE)
        or root_ast.args.get("with")
        or root_ast.args.get("with_")
    )
    strict_boundary = False
    for diff in ast_diffs:
        if diff.diff_type not in {"comparison_operator_changed", "literal_changed"}:
            continue
        comparison = _comparison_node_from_diff(
            diff.standard_node,
            diff.extra.get("standard_sql"),
        )
        if isinstance(comparison, (exp.GT, exp.LT)):
            strict_boundary = True
            break
    changed = False
    # Inner blocks are visited first so their physical lineage is in place
    # before an outer CTE/derived join asks for the same output column.
    for select in reversed(selects):
        sources = _query_block_sources(select)
        has_lineage_source = any(
            not isinstance(source, exp.Table)
            or _query_cte_select(root_ast, source.name) is not None
            for _alias, source in sources
        )
        is_cte_body = any(
            cte.this is select
            for cte in root_ast.find_all(exp.CTE)
        )
        is_derived_body = any(
            subquery.this is select
            for subquery in root_ast.find_all(exp.Subquery)
        )
        is_root_nested_query = select is root_ast and has_nested_query
        # Direct physical-table joins already have dedicated join, aggregate,
        # NULL and window materializers.  Applying this generic lineage pass
        # to them can overwrite primary keys or aggregate driver rows.  Keep
        # this adapter scoped to an actual derived/CTE query-block boundary.
        # A derived body can still be made only from physical tables, and a
        # root query with a scalar subquery can need its own outer WHERE/JOIN
        # path.  Those are explicit query-block boundaries too; plain
        # top-level physical queries remain outside this generic pass.
        if not (
            has_lineage_source
            or is_cte_body
            or is_derived_body
            or is_root_nested_query
        ):
            continue
        before = repr(data)
        row_indices = (
            (0, 1)
            if (
                (
                    bool(select.args.get("distinct"))
                    and any(diff.diff_type == "distinct_changed" for diff in ast_diffs)
                )
                or strict_boundary
            )
            else (0,)
        )
        preferred_values_by_row: dict[int, dict[tuple[str, str], Any]] = {}
        for row_index in row_indices:
            _set_query_block_rich_predicates(data, select, root_ast, row_index)
            preferred_values_by_row[row_index] = _query_block_predicate_values(
                data,
                select,
                root_ast,
                row_index,
            )
            if not has_lineage_source:
                # Direct equality syntax is cheaper and more conservative
                # than the lineage graph.  It also handles a derived/CTE
                # body whose source tables are all physical tables.
                _materialize_select_row_path(
                    data,
                    select,
                    row_index=row_index,
                    schema_catalog=schema_catalog,
                )
        # A root scalar-subquery path is aligned by the unique-aware direct
        # row-path helper above.  The lineage component pass intentionally
        # preserves every column it heuristically considers unique; on a
        # root filter such as ``mlo.owner_key = <literal>`` that would preserve
        # a stale foreign-key value and undo the just-materialized predicate.
        # CTE/derived blocks still use the component graph, including wrapped
        # equality expressions such as UPPER(a)=UPPER(b).
        edges = (
            _query_block_equality_edges(data, select, root_ast)
            if has_lineage_source or is_cte_body or is_derived_body
            else []
        )
        if edges:
            parent: dict[tuple[str, str], tuple[str, str]] = {}

            def find(ref: tuple[str, str]) -> tuple[str, str]:
                parent.setdefault(ref, ref)
                if parent[ref] != ref:
                    parent[ref] = find(parent[ref])
                return parent[ref]

            for left, right in edges:
                left_root, right_root = find(left), find(right)
                if left_root != right_root:
                    parent[right_root] = left_root
            components: dict[tuple[str, str], list[tuple[str, str]]] = defaultdict(list)
            for ref in parent:
                components[find(ref)].append(ref)

            def is_unique(ref: tuple[str, str]) -> bool:
                actual = _actual_data_ref(data, ref)
                if actual is None or not actual[0]:
                    return False
                rows, column = actual
                table_name = next(
                    (name for name, candidate in data.items() if candidate is rows),
                    ref[0],
                )
                return bool(
                    _catalog_has_unary_unique_key(schema_catalog, ref)
                    or _is_primary_key_candidate(
                        table_name,
                        column,
                        list(rows[0]),
                    )
                )

            with write_owner("materializer:query_block_reachability"):
                for row_index in row_indices:
                    preferred_values = preferred_values_by_row.get(row_index, {})
                    for refs in components.values():
                        values: list[Any] = []
                        for ref in refs:
                            actual = _actual_data_ref(data, ref)
                            if actual is not None and row_index < len(actual[0]):
                                values.append(actual[0][row_index].get(actual[1]))
                        unique_refs = [ref for ref in refs if is_unique(ref)]
                        anchor = next(
                            (
                                preferred_values[ref]
                                for ref in unique_refs
                                if ref in preferred_values
                            ),
                            None,
                        )
                        if anchor is None:
                            anchor = preferred_values.get(refs[0]) if refs else None
                        if anchor is None:
                            anchor = next(
                                (
                                    preferred_values[ref]
                                    for ref in refs
                                    if ref in preferred_values
                                ),
                                None,
                            )
                        if anchor is None:
                            anchor = next((value for value in values if value is not None), None)
                        if anchor is None:
                            anchor = "__query_block_join_key__"
                        for ref in refs:
                            actual = _actual_data_ref(data, ref)
                            if actual is not None and row_index < len(actual[0]):
                                # Never collapse two already populated unique
                                # endpoints merely to make a derived path
                                # look connected.  Non-unique foreign-key
                                # cells may follow the selected unique anchor;
                                # conflicting unique rows remain an honest
                                # unreachable path.
                                if is_unique(ref):
                                    current = actual[0][row_index].get(actual[1])
                                    if current is not None and current != anchor:
                                        continue
                                actual[0][row_index][actual[1]] = anchor
        if repr(data) != before:
            changed = True
    return changed


def _materialize_null_query_block_paths(
    data: dict[str, list[dict[str, Any]]],
    standard_sql: str,
    student_sql: str,
    ast_diffs: list[ASTDiffNode],
    *,
    schema_catalog: SchemaCatalog | None = None,
) -> bool:
    """Create non-NULL and NULL rows for a query-block NULL mutation."""
    diff = next(
        (
            item
            for item in ast_diffs
            if item.diff_type == "null_predicate_negation_changed"
        ),
        None,
    )
    if diff is None:
        return False
    standard_ast = _parse_sql(standard_sql)
    student_ast = _parse_sql(student_sql)
    if standard_ast is None or student_ast is None:
        return False
    target_sql = re.sub(
        r"\s+",
        "",
        str(diff.extra.get("standard_sql") or ""),
    ).lower()
    standard_is: exp.Is | None = None
    student_is: exp.Is | None = None
    for candidate in standard_ast.find_all(exp.Is):
        candidate_sql = re.sub(r"\s+", "", _sql_of(candidate)).lower()
        if target_sql and (
            candidate_sql == target_sql
            or re.sub(r"\s+", "", f"NOT{candidate_sql}").lower() == target_sql
        ):
            standard_is = candidate
            break
    for candidate in student_ast.find_all(exp.Is):
        candidate_sql = re.sub(r"\s+", "", _sql_of(candidate)).lower()
        student_target = re.sub(
            r"\s+",
            "",
            str(diff.extra.get("student_sql") or ""),
        ).lower()
        if student_target and candidate_sql == student_target:
            student_is = candidate
            break
    if standard_is is None or student_is is None:
        return False
    standard_select = _nearest_select(standard_is)
    student_select = _nearest_select(student_is)
    if not isinstance(standard_select, exp.Select) or not isinstance(
        student_select, exp.Select
    ):
        return False
    standard_column = _predicate_source_column(standard_is.this)
    student_column = _predicate_source_column(student_is.this)
    if standard_column is None or student_column is None:
        return False
    standard_ref = _query_column_ref_in_data(
        data,
        standard_column,
        standard_select,
        standard_ast,
    )
    student_ref = _query_column_ref_in_data(
        data,
        student_column,
        student_select,
        student_ast,
    )
    if standard_ref is None or standard_ref != student_ref:
        return False
    actual = _actual_data_ref(data, standard_ref)
    if actual is None or len(actual[0]) < 2:
        return False
    rows, column = actual

    # A direct top-level physical NULL predicate is already handled by the
    # legacy NULL adapter.  This adapter owns only a nested/derived/CTE block,
    # where the source row must also pass its local JOIN and filter path.
    root_has_boundary = bool(
        standard_select.find_ancestor(exp.Subquery)
        or standard_select.find_ancestor(exp.CTE)
        or any(
            _query_cte_select(standard_ast, source.name) is not None
            for _alias, source in _query_block_sources(standard_select)
            if isinstance(source, exp.Table)
        )
    )
    if not root_has_boundary:
        return False
    with write_owner("materializer:null_query_block_paths"):
        for row_index in (0, 1):
            _set_query_block_rich_predicates(
                data,
                standard_select,
                standard_ast,
                row_index,
            )
            _materialize_select_row_path(
                data,
                standard_select,
                row_index=row_index,
                schema_catalog=schema_catalog,
            )
            _align_query_block_equality_row(
                data,
                standard_select,
                standard_ast,
                row_index,
                schema_catalog=schema_catalog,
            )
        original_non_null = rows[0].get(column)
        if original_non_null is None:
            original_non_null = _seed_value(column, 0)
        rows[0][column] = original_non_null
        rows[1][column] = None
    return True


def _materialize_query_block_aggregate_paths(
    data: dict[str, list[dict[str, Any]]],
    standard_sql: str,
    *,
    schema_catalog: SchemaCatalog | None = None,
) -> bool:
    """Satisfy bounded GROUP BY/HAVING prerequisites in CTE/derived blocks.

    A comparison such as ``HAVING COUNT(DISTINCT member) = 10`` is not a
    scalar cell constraint: it requires ten rows in one physical group before
    the outer query can even produce a row.  The ordinary predicate materializer
    correctly refuses to write an aggregate alias, but without this narrow
    prerequisite a valid outer ``> 0``/``>= 0`` teaching example remains
    empty.  Only literal COUNT thresholds within the witness cap are handled;
    arbitrary aggregate expressions remain unresolved rather than guessed.
    """
    root_ast = _parse_sql(standard_sql)
    if root_ast is None:
        return False
    changed = False
    selects = list(root_ast.find_all(exp.Select))
    if isinstance(root_ast, exp.Select) and root_ast not in selects:
        selects.append(root_ast)
    for select in reversed(selects):
        group = select.args.get("group")
        having = select.args.get("having")
        if not isinstance(group, exp.Group) or not isinstance(having, exp.Having):
            continue
        sources = _query_block_sources(select)
        is_cte_body = any(cte.this is select for cte in root_ast.find_all(exp.CTE))
        is_derived_body = any(
            subquery.this is select for subquery in root_ast.find_all(exp.Subquery)
        )
        has_lineage_source = any(
            not isinstance(source, exp.Table)
            or _query_cte_select(root_ast, source.name) is not None
            for _alias, source in sources
        )
        if not (is_cte_body or is_derived_body or has_lineage_source):
            continue

        requirement: tuple[exp.Expression, int] | None = None
        for comparison in having.find_all(exp.EQ, exp.GTE, exp.GT, exp.LTE, exp.LT):
            aggregate = next(
                (node for node in comparison.find_all(exp.Count)),
                None,
            )
            scalar = (
                _static_predicate_scalar(comparison.right)
                if aggregate is not None
                else _MISSING
            )
            if aggregate is None or scalar is _MISSING:
                aggregate = next(
                    (node for node in comparison.find_all(exp.Count)),
                    None,
                )
                scalar = _static_predicate_scalar(comparison.left)
            if aggregate is None or not isinstance(scalar, (int, float, Decimal)):
                continue
            if isinstance(scalar, bool):
                continue
            threshold = int(scalar)
            if threshold < 0 or threshold > _MAX_WITNESS_ROWS_PER_TABLE:
                continue
            requirement = (comparison, threshold)
            break
        if requirement is None:
            continue
        comparison, threshold = requirement
        aggregate = next((node for node in comparison.find_all(exp.Count)), None)
        if aggregate is None:
            continue
        count_expression = aggregate.this
        if isinstance(count_expression, exp.Distinct):
            count_expression = (
                count_expression.expressions[0]
                if count_expression.expressions
                else None
            )
        count_ref = (
            _query_column_ref_in_data(data, count_expression, select, root_ast)
            if isinstance(count_expression, exp.Column)
            else None
        )
        if count_expression is not None and not isinstance(count_expression, exp.Star) and count_ref is None:
            continue

        group_refs: list[tuple[str, str]] = []
        for expression in group.expressions or ():
            if not isinstance(expression, exp.Column):
                continue
            ref = _query_column_ref_in_data(data, expression, select, root_ast)
            if ref is not None and ref not in group_refs:
                group_refs.append(ref)
        if not group_refs:
            continue
        group_actuals = [(_actual_data_ref(data, ref), ref) for ref in group_refs]
        if any(actual is None or not actual[0] for actual, _ref in group_actuals):
            continue
        if any(
            _catalog_has_unary_unique_key(schema_catalog, ref)
            or _is_primary_key_candidate(
                next((name for name, rows in data.items() if rows is actual[0]), ref[0]),
                actual[1],
                list(actual[0][0]),
            )
            for actual, ref in group_actuals
        ):
            # A unique GROUP BY key cannot be duplicated safely in this
            # bounded adapter.  The normal aggregate planner may still prove
            # a different shape through a dedicated obligation.
            continue

        count_actual = _actual_data_ref(data, count_ref) if count_ref else None
        if count_ref is not None and count_actual is None:
            continue
        if count_ref is not None and any(ref == count_ref for ref in group_refs):
            continue
        available = min(
            len(actual[0]) for actual, _ref in group_actuals
        )
        if count_actual is not None:
            available = min(available, len(count_actual[0]))
        if threshold > available:
            continue

        with write_owner("materializer:query_block_aggregate_prerequisite"):
            # First make all local filters and JOINs reachable for the rows
            # that will form the group.  This path is deliberately bounded by
            # the requested threshold, not by an arbitrary table-wide rewrite.
            for row_index in range(max(1, threshold)):
                _set_query_block_rich_predicates(
                    data,
                    select,
                    root_ast,
                    row_index,
                )
                _materialize_select_row_path(
                    data,
                    select,
                    row_index=row_index,
                    schema_catalog=schema_catalog,
                )
                _align_query_block_equality_row(
                    data,
                    select,
                    root_ast,
                    row_index,
                    schema_catalog=schema_catalog,
                )
            group_values = []
            for actual, _ref in group_actuals:
                group_value = actual[0][0].get(actual[1])
                if group_value is None:
                    group_value = _seed_value(actual[1], 0)
                group_values.append((actual[0], actual[1], group_value))
            for rows, column, group_value in group_values:
                for row_index in range(threshold):
                    rows[row_index][column] = group_value
                for row in rows[threshold:]:
                    if row.get(column) == group_value:
                        row[column] = _counter_value(column, group_value)
            if count_actual is not None:
                count_rows, count_column = count_actual
                existing = [
                    row.get(count_column)
                    for row in count_rows[:threshold]
                ]
                for row_index in range(threshold):
                    current = existing[row_index] if row_index < len(existing) else None
                    candidate = _aggregate_distinct_probe_value(current, row_index)
                    while candidate in existing[:row_index]:
                        candidate = _counter_value(count_column, candidate)
                    count_rows[row_index][count_column] = candidate
            # The HAVING group is often only the first CTE in a longer
            # pipeline.  Its repeated rows must be able to flow through the
            # next CTE's JOINs as well; otherwise the aggregate is satisfied
            # locally but the outer result is still empty.  Reuse the same
            # bounded row-path alignment for dependent blocks, excluding the
            # root query where physical identity/cardinality has its own
            # materializers.
            for dependent in reversed(selects):
                if dependent is root_ast:
                    continue
                dependent_sources = _query_block_sources(dependent)
                dependent_is_cte = any(
                    cte.this is dependent for cte in root_ast.find_all(exp.CTE)
                )
                dependent_is_derived = any(
                    subquery.this is dependent
                    for subquery in root_ast.find_all(exp.Subquery)
                )
                dependent_has_lineage = any(
                    not isinstance(source, exp.Table)
                    or _query_cte_select(root_ast, source.name) is not None
                    for _alias, source in dependent_sources
                )
                if not (
                    dependent_is_cte
                    or dependent_is_derived
                    or dependent_has_lineage
                ):
                    continue
                for row_index in range(max(1, threshold)):
                    _set_query_block_rich_predicates(
                        data,
                        dependent,
                        root_ast,
                        row_index,
                    )
                    _materialize_select_row_path(
                        data,
                        dependent,
                        row_index=row_index,
                        schema_catalog=schema_catalog,
                    )
                    _align_query_block_equality_row(
                        data,
                        dependent,
                        root_ast,
                        row_index,
                        schema_catalog=schema_catalog,
                    )
        changed = True
    return changed


def _materialize_select_row_path(
    data: dict[str, list[dict[str, Any]]],
    select: exp.Select,
    *,
    row_index: int = 0,
    exclude_other_rows: bool = False,
    schema_catalog: SchemaCatalog | None = None,
) -> bool:
    """Make one bounded row combination satisfy local JOIN/WHERE conditions."""
    touched = False
    for join in select.find_all(exp.Join):
        if join.find_ancestor(exp.Select) is not select:
            continue
        on = join.args.get("on")
        if on is None:
            continue
        equalities = [on] if isinstance(on, exp.EQ) else list(on.find_all(exp.EQ))
        for equality in equalities:
            if not isinstance(equality.left, exp.Column) or not isinstance(
                equality.right, exp.Column
            ):
                continue
            left_ref = _column_ref_in_select_data(data, equality.left, select)
            right_ref = _column_ref_in_select_data(data, equality.right, select)
            if left_ref is None or right_ref is None or left_ref == right_ref:
                continue
            left_actual = _actual_data_ref(data, left_ref)
            right_actual = _actual_data_ref(data, right_ref)
            if left_actual is None or right_actual is None:
                continue
            left_rows, left_column = left_actual
            right_rows, right_column = right_actual
            if row_index >= len(left_rows) or row_index >= len(right_rows):
                continue
            left_table = next(
                (name for name, rows in data.items() if rows is left_rows),
                left_ref[0],
            )
            right_table = next(
                (name for name, rows in data.items() if rows is right_rows),
                right_ref[0],
            )
            left_unique = _catalog_has_unary_unique_key(schema_catalog, left_ref) or _is_primary_key_candidate(
                left_table,
                left_column,
                list(left_rows[0]),
            )
            right_unique = _catalog_has_unary_unique_key(schema_catalog, right_ref) or _is_primary_key_candidate(
                right_table,
                right_column,
                list(right_rows[0]),
            )
            if right_unique and not left_unique:
                anchor = right_rows[row_index].get(right_column)
            else:
                anchor = left_rows[row_index].get(left_column)
            if anchor is None:
                anchor = _seed_value(
                    right_column if right_unique and not left_unique else left_column,
                    row_index,
                )
            left_rows[row_index][left_column] = anchor
            right_rows[row_index][right_column] = anchor
            touched = True

    for rows, column, true_value, false_value in _select_local_scalar_predicates(
        data, select
    ):
        if row_index >= len(rows):
            continue
        rows[row_index][column] = true_value
        if exclude_other_rows:
            for index, row in enumerate(rows):
                if index != row_index:
                    row[column] = false_value
        touched = True
    return touched


def _align_standard_join_equalities(
    data: dict[str, list[dict[str, Any]]],
    standard_sql: str,
) -> None:
    ast = _parse_sql(standard_sql)
    if not ast:
        return
    aliases = _table_aliases(ast)
    for comparison in ast.find_all(exp.EQ):
        if not isinstance(comparison.left, exp.Column) or not isinstance(comparison.right, exp.Column):
            continue
        left_ref = _column_ref(comparison.left, aliases)
        right_ref = _column_ref(comparison.right, aliases)
        if not left_ref or not right_ref or left_ref[0] == right_ref[0]:
            continue
        left_table = next((name for name in data if _norm_name(name) == left_ref[0]), None)
        right_table = next((name for name in data if _norm_name(name) == right_ref[0]), None)
        if not left_table or not right_table or not data[left_table] or not data[right_table]:
            continue
        left_lookup = _column_lookup(list(data[left_table][0]))
        right_lookup = _column_lookup(list(data[right_table][0]))
        left_column = left_lookup.get(left_ref[1])
        right_column = right_lookup.get(right_ref[1])
        if not left_column or not right_column:
            continue
        left_is_pk = _is_primary_key_candidate(left_table, left_column, list(data[left_table][0]))
        right_is_pk = _is_primary_key_candidate(right_table, right_column, list(data[right_table][0]))
        if right_is_pk and not left_is_pk:
            source_rows, source_column = data[right_table], right_column
            target_rows, target_column = data[left_table], left_column
        else:
            source_rows, source_column = data[left_table], left_column
            target_rows, target_column = data[right_table], right_column
        source_values = [row[source_column] for row in source_rows]
        for index, row in enumerate(target_rows):
            row[target_column] = source_values[index % len(source_values)]
