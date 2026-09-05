"""Composable witness construction strategies."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Any
from collections import defaultdict
import re
from sqlglot import exp
from core.ast_schema import ASTDiffNode
from core.witness_generation.schema_scope import (
    ColumnSchema,
    SchemaCatalog,
)
from core.witness_generation.obligations import stable_diff_id
from core.witness_generation.planner import write_owner

from core.phase1_foundation import (
    _MAX_SCOPE_AST_NODES_SCANNED,
    _MAX_SCOPE_DIFFS,
    _MAX_SCOPE_DIFF_BINDINGS,
    _MAX_SCOPE_EDGES,
    _MISSING,
    _Phase1ScopeDescriptor,
    _SCOPE_METADATA_VERSION,
    _aggregate_distinct_probe_value,
    _coerce_typed_seed,
    _collect_phase1_scopes,
    _comparison_node_from_diff,
    _comparison_operands_mirrored_equivalent,
    _conceptual_scope_id,
    _constant_true_filter_equivalent,
    _direct_from_table,
    _double_negation_equivalent,
    _in_list_or_equivalent,
    _is_true_filter_equivalent,
    _literal_value,
    _merge_scope_edges,
    _nearest_retained_scope,
    _nearest_select,
    _null_safe_equality_filter_equivalent,
    _nullif_coalesce_case_equivalent,
    _parse_sql,
    _predicate_source_column,
    _scope_edge,
    _scope_for_diff_node,
    _scope_parent_chain,
    _simple_searched_case_equivalent,
    _singleton_equality_in_filter_equivalent,
    _sql_of,
    _standalone_literal_projection_equivalent,
    _static_predicate_scalar,
    _statically_empty_scalar_subquery_null_equivalent,
    _strict_path_variant,
    _subquery_wrapper_is_derived,
    _temporal_comparison_parts,
    _top_select,
)

from core.phase1_sql_semantics import (
    _between_closed_range_equivalent,
    _catalog_has_unary_unique_key,
    _coerce_datetime,
    _comparison_truth_value,
    _counter_value,
    _global_extreme_comparison_equivalent,
    _is_date_column,
    _is_numeric_column,
    _norm_name,
    _order_reference_equivalent,
    _rich_predicate_truth_value,
    _scalar_predicate_values,
    _schema_complete_star_projection_equivalent,
    _seed_value,
    _simple_join_using_on_equivalent,
    _sqlite_declared_affinity,
    _table_key_aliases,
    _unreferenced_output_aliases_equivalent,
    _where_boolean_absorption_equivalent,
)

from core.phase1_constraints import (
    _column_lookup,
    _commutative_set_branch_permutation_equivalent,
    _named_window_inline_equivalent,
    _simple_cte_dependency_chain_inline_equivalent,
    _simple_cte_inline_equivalent,
    _simple_derived_table_inline_equivalent,
    _simple_in_join_equivalent,
    _simple_not_exists_antijoin_equivalent,
    _single_row_aggregate_cte_scalar_equivalent,
    _strict_monotonic_recursive_union_modifier_equivalent,
    _table_aliases,
)

from core.phase1_query_paths import (
    _actual_data_ref,
    _column_ref_in_select,
    _column_ref_in_select_data,
    _direct_select_tables,
    _materialize_correlated_key_drift_witness,
    _query_block_sources,
    _query_cte_select,
    _query_source_select,
    _scope_column_ref,
    _set_select_local_literal_predicates,
    _strict_in_exists_filter_equivalent,
    _strict_in_exists_filter_metadata,
)



def _repair_declared_nonnull_columns(
    data: dict[str, list[dict[str, Any]]],
    *,
    schema_catalog: SchemaCatalog | None = None,
) -> None:
    """Restore hard NOT NULL columns after legacy probes have run.

    NULL is a valid witness only when the catalog permits it.  Legacy probes
    predate the structured catalog and may write NULL based on SQL shape alone;
    this final integrity pass prevents those writes from creating impossible
    databases or false NOT IN counterexamples.
    """
    if schema_catalog is None:
        return
    for table_name, rows in data.items():
        table_schema = schema_catalog.table(table_name)
        if table_schema is None:
            continue
        for column_schema in table_schema.columns.values():
            if column_schema.nullable:
                continue
            actual_column = _column_lookup(list(rows[0])).get(
                _norm_name(column_schema.name)
            ) if rows else None
            if actual_column is None:
                continue
            for index, row in enumerate(rows):
                if row.get(actual_column) is not None:
                    continue
                kind = _authoritative_column_kind(
                    table_name,
                    column_schema.name,
                    schema_catalog,
                )
                seed = _seed_value(column_schema.name, index)
                row[actual_column] = (
                    _coerce_typed_seed(seed, kind, column_schema.name, index)
                    if kind
                    else seed
                )


def _materialize_nested_distinct_projection_witness(
    data: dict[str, list[dict[str, Any]]],
    standard_sql: str,
    student_sql: str,
    ast_diffs: list[ASTDiffNode],
    *,
    schema_catalog: SchemaCatalog | None = None,
) -> bool:
    """Create a duplicate in a nested/CTE DISTINCT query block.

    A root-level DISTINCT can be checked from the final result, but an inner
    DISTINCT is consumed by its parent and may never be visible there.  This
    adapter handles the bounded, relationally honest shape where the owning
    block projects physical columns from direct tables.  It varies one
    non-unique driver path, aligns the JOIN endpoints on both paths, and
    equalizes only the projected payload.  Derived sources and expressions
    are left for the executable-query boundary rather than guessed here.
    """
    nested_diffs = [
        diff
        for diff in ast_diffs
        if diff.diff_type == "distinct_changed"
        and str(diff.extra.get("query_scope") or "root") != "root"
    ]
    if not nested_diffs:
        return False
    root_ast = _parse_sql(standard_sql)
    if root_ast is None:
        return False
    changed = False
    for diff in nested_diffs:
        standard_select = _nearest_select(diff.standard_node)
        if not isinstance(standard_select, exp.Select):
            continue
        distinct = standard_select.args.get("distinct")
        if not distinct or standard_select.args.get("group"):
            continue
        direct_aliases = _direct_select_tables(standard_select)
        direct_physical = set(direct_aliases.values())
        if not direct_physical and not standard_select.args.get("joins"):
            continue
        # Repeated physical aliases require a self-join path and are not safe
        # to collapse into one row-index mapping.
        physical_alias_count: dict[str, int] = defaultdict(int)
        for table_node in standard_select.find_all(exp.Table):
            if table_node.find_ancestor(exp.Select) is standard_select:
                physical_alias_count[_norm_name(table_node.name)] += 1
        if any(count > 1 for count in physical_alias_count.values()):
            continue

        projected_refs: list[tuple[str, str]] = []
        simple_projection = True
        for item in standard_select.expressions or ():
            expression = item.this if isinstance(item, exp.Alias) else item
            if not isinstance(expression, exp.Column):
                simple_projection = False
                break
            ref = _query_column_ref_in_data(
                data,
                expression,
                standard_select,
                root_ast,
            )
            if ref is None:
                ref = _column_ref_in_select_data(data, expression, standard_select)
            if ref is None:
                simple_projection = False
                break
            projected_refs.append(ref)
        if not simple_projection or not projected_refs:
            continue

        edges: list[tuple[tuple[str, str], tuple[str, str]]] = []
        for join in standard_select.args.get("joins") or ():
            on = join.args.get("on")
            if not isinstance(on, exp.Expression):
                continue
            equalities = [on] if isinstance(on, exp.EQ) else list(on.find_all(exp.EQ))
            for equality in equalities:
                if not isinstance(equality.left, exp.Column) or not isinstance(equality.right, exp.Column):
                    continue
                left = _query_column_ref_in_data(
                    data,
                    equality.left,
                    standard_select,
                    root_ast,
                )
                right = _query_column_ref_in_data(
                    data,
                    equality.right,
                    standard_select,
                    root_ast,
                )
                if left is None:
                    left = _column_ref_in_select_data(data, equality.left, standard_select)
                if right is None:
                    right = _column_ref_in_select_data(data, equality.right, standard_select)
                if (
                    left is None
                    or right is None
                    or left[0] == right[0]
                ):
                    continue
                edges.append((left, right))

        lineage_physical = {
            ref[0]
            for ref in [*projected_refs, *[item for edge in edges for item in edge]]
            if any(_norm_name(name) == ref[0] and rows for name, rows in data.items())
        }
        if not lineage_physical:
            lineage_physical = {
                table
                for table in direct_physical
                if any(_norm_name(name) == table and rows for name, rows in data.items())
            }
        if not lineage_physical:
            continue

        table_rows = {
            table: next(
                rows for name, rows in data.items() if _norm_name(name) == table
            )
            for table in lineage_physical
        }
        candidates = [
            _norm_name(_direct_from_table(standard_select).name)
            if isinstance(_direct_from_table(standard_select), exp.Table)
            else "",
            *sorted(lineage_physical),
        ]
        driver = next(
            (
                table
                for table in dict.fromkeys(candidates)
                if table in table_rows
                and len(table_rows[table]) >= 2
                and not any(
                    ref[0] == table
                    and _catalog_has_unary_unique_key(schema_catalog, ref)
                    for ref in projected_refs
                )
            ),
            None,
        )
        if driver is None:
            continue

        # Vary the connected, non-authoritatively-unique tables.  A declared
        # unary key remains shared, while a foreign-key endpoint follows the
        # driver's row.  External corpus catalogs intentionally declare no
        # keys, so their ordinary denormalized teaching joins get two real
        # paths without manufacturing duplicate declared PKs.
        varied = {driver}
        progressed = True
        while progressed:
            progressed = False
            for left, right in edges:
                if left[0] in varied and right[0] not in varied:
                    if not _catalog_has_unary_unique_key(schema_catalog, right):
                        varied.add(right[0])
                        progressed = True
                elif right[0] in varied and left[0] not in varied:
                    if not _catalog_has_unary_unique_key(schema_catalog, left):
                        varied.add(left[0])
                        progressed = True
        path_rows = {
            table: (0, 1) if table in varied and len(table_rows[table]) >= 2 else (0, 0)
            for table in lineage_physical
        }

        # Use query-block lineage for local filters.  A CTE/derived alias is
        # not a physical table in ``data``, but its simple projected column
        # still identifies the source cell that must be reachable.
        _set_query_block_rich_predicates(data, standard_select, root_ast)

        with write_owner("materializer:nested_distinct_projection"):
            # Treat the join graph as connected components.  Independent
            # edge writes are order-dependent for a CTE/derived chain and can
            # leave an earlier join endpoint dangling after a later edge.
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
            for path in (0, 1):
                for refs in components.values():
                    current = None
                    ordered_refs = sorted(
                        refs,
                        key=lambda ref: 1
                        if path_rows[ref[0]] == (0, 1)
                        else 0,
                    )
                    for ref in ordered_refs:
                        actual = _actual_data_ref(data, ref)
                        if actual is None:
                            continue
                        rows, column = actual
                        current = rows[path_rows[ref[0]][path]].get(column)
                        if current is not None:
                            break
                    if current is None:
                        current = "__nested_distinct_join_key__"
                    for ref in refs:
                        actual = _actual_data_ref(data, ref)
                        if actual is None:
                            continue
                        rows, column = actual
                        rows[path_rows[ref[0]][path]][column] = current

            join_refs = {ref for edge in edges for ref in edge}

            for table, column in projected_refs:
                actual = _actual_data_ref(data, (table, column))
                if actual is None:
                    projected_refs = []
                    break
                rows, actual_column = actual
                first = rows[path_rows[table][0]].get(actual_column)
                if (table, column) in join_refs:
                    for path in (0, 1):
                        rows[path_rows[table][path]][actual_column] = first
                    continue
                for path in (0, 1):
                    rows[path_rows[table][path]][actual_column] = first
        if projected_refs:
            changed = True
    return changed


def _materialize_shared_literal_predicate_paths(
    data: dict[str, list[dict[str, Any]]],
    standard_sql: str,
    student_sql: str,
    ast_diffs: list[ASTDiffNode],
    *,
    schema_catalog: SchemaCatalog | None = None,
) -> bool:
    """Make simple string filters reachable for aggregate/DISTINCT worlds.

    The generic seed generator deliberately avoids guessing the type of an
    untyped column.  That is normally the right fail-closed behavior, but it
    leaves a bounded witness empty when a public query filters on a literal
    such as ``country = 'Austria'``.  This final pass is intentionally narrow:
    it only handles a single-table top-level aggregate or DISTINCT query and
    writes the declared literal to two rows.  It never rewrites primary keys,
    projections, joins, or non-text typed columns.
    """
    relevant = {
        "aggregate_function_changed",
        "aggregate_argument_changed",
        "aggregate_distinct_changed",
        "distinct_changed",
    }
    if not any(diff.diff_type in relevant for diff in ast_diffs):
        return False
    standard_ast = _parse_sql(standard_sql)
    student_ast = _parse_sql(student_sql)
    standard_select = _top_select(standard_ast) if standard_ast is not None else None
    student_select = _top_select(student_ast) if student_ast is not None else None
    if not isinstance(standard_select, exp.Select) or not isinstance(student_select, exp.Select):
        return False
    source = _direct_from_table(standard_select)
    if not isinstance(source, exp.Table) or standard_select.args.get("joins"):
        return False
    actual_table = next(
        (name for name in data if _norm_name(name) == _norm_name(source.name)),
        None,
    )
    rows = data.get(actual_table or "")
    if not rows or len(rows) < 2:
        return False

    bindings: dict[str, tuple[str, Any]] = {}
    where = standard_select.args.get("where")
    if not isinstance(where, exp.Where):
        return False
    for node in where.find_all(exp.EQ, exp.In):
        if node.find_ancestor(exp.Select) is not standard_select:
            continue
        column: exp.Column | None = None
        values: list[Any] = []
        if isinstance(node, exp.EQ):
            if isinstance(node.left, exp.Column) and isinstance(node.right, exp.Literal):
                column, values = node.left, [_literal_value(node.right)]
            elif isinstance(node.right, exp.Column) and isinstance(node.left, exp.Literal):
                column, values = node.right, [_literal_value(node.left)]
        elif isinstance(node.this, exp.Column):
            column = node.this
            values = [
                _literal_value(item)
                for item in (node.expressions or ())
                if isinstance(item, exp.Literal)
            ]
        if not isinstance(column, exp.Column) or not values or not all(
            isinstance(value, str) for value in values
        ):
            continue
        ref = _column_ref_in_select_data(data, column, standard_select)
        if ref is None or _norm_name(ref[0]) != _norm_name(actual_table):
            continue
        actual = _actual_data_ref(data, ref)
        if actual is None:
            continue
        _predicate_rows, actual_column = actual
        kind = _authoritative_column_kind(
            actual_table,
            actual_column,
            schema_catalog,
        )
        if kind in {"numeric", "date", "time"}:
            continue
        desired = values[0]
        previous = bindings.get(actual_column)
        if previous is not None and previous[1] != desired:
            return False
        bindings[actual_column] = (actual_column, desired)
    if not bindings:
        return False

    with write_owner("materializer:shared_literal_predicate_paths"):
        for actual_column, (_column_name, value) in bindings.items():
            rows[0][actual_column] = value
            rows[1][actual_column] = value

        # COUNT(column) versus COUNT(*) needs one NULL measure among the two
        # rows that satisfy the filter.  Keep the write conditional on the
        # mutation shape so ordinary aggregate filters are not altered.
        standard_aggregates = [
            aggregate
            for aggregate in standard_select.find_all(exp.AggFunc)
            if _nearest_select(aggregate) is standard_select
        ]
        student_aggregates = [
            aggregate
            for aggregate in student_select.find_all(exp.AggFunc)
            if _nearest_select(aggregate) is student_select
        ]
        if standard_aggregates and student_aggregates:
            standard_count = next(
                (item for item in standard_aggregates if type(item).__name__.upper() == "COUNT"),
                None,
            )
            student_count = next(
                (item for item in student_aggregates if type(item).__name__.upper() == "COUNT"),
                None,
            )
            if standard_count is not None and student_count is not None:
                standard_arg = standard_count.this
                student_arg = student_count.this
                if isinstance(standard_arg, exp.Column) and isinstance(student_arg, exp.Star):
                    ref = _column_ref_in_select_data(data, standard_arg, standard_select)
                    actual = _actual_data_ref(data, ref) if ref else None
                    if actual is not None:
                        measure_rows, measure_column = actual
                        nullable = _catalog_column_schema(
                            actual_table,
                            measure_column,
                            schema_catalog,
                        )
                        if nullable is None or nullable.nullable:
                            if measure_rows[0].get(measure_column) is None:
                                measure_rows[0][measure_column] = 1
                            measure_rows[1][measure_column] = None

        # A top-level DISTINCT needs two reachable rows with the same
        # projected tuple.  Do not copy a primary key or a predicate column.
        if standard_select.args.get("distinct"):
            primary = _primary_key_candidate(list(rows[0]), actual_table)
            projected_columns: list[str] = []
            for item in standard_select.expressions or ():
                expression = item.this if isinstance(item, exp.Alias) else item
                if not isinstance(expression, exp.Column):
                    projected_columns = []
                    break
                ref = _column_ref_in_select_data(data, expression, standard_select)
                actual = _actual_data_ref(data, ref) if ref else None
                if actual is None:
                    projected_columns = []
                    break
                projected_columns.append(actual[1])
            for column in projected_columns:
                if column in bindings or (primary and _norm_name(column) == _norm_name(primary)):
                    continue
                rows[1][column] = rows[0].get(column)
    return True


def _temporal_boundary_value(
    source_column: exp.Column,
    literal: Any,
    *,
    table: str,
    schema_catalog: SchemaCatalog | None,
) -> str | None:
    """Convert a comparison threshold into a valid bounded date value."""
    declared_kind = _authoritative_column_kind(table, source_column.name, schema_catalog)
    if literal is None:
        return None
    parsed = _coerce_datetime(literal)
    if parsed is None:
        # A declared DATE/TIMESTAMP column with a non-ISO literal is still a
        # temporal query, but guessing its format would make the witness
        # engine-dependent.  Let the existing boundary classifier handle it.
        return None
    if declared_kind not in {None, "date", "time"} and not _is_date_column(source_column.name):
        return None
    has_time = any((parsed.hour, parsed.minute, parsed.second, parsed.microsecond))
    return parsed.strftime("%Y-%m-%d %H:%M:%S" if has_time else "%Y-%m-%d")


def _materialize_temporal_filter_paths(
    data: dict[str, list[dict[str, Any]]],
    standard_sql: str,
    *,
    schema_catalog: SchemaCatalog | None = None,
) -> bool:
    """Keep unchanged temporal filters executable after generic probes.

    A comparison mutation may live in HAVING/CASE while an unchanged direct
    date-column filter still controls whether its aggregate path exists.
    Preserve that SQLite path with a bounded ISO calendar value.
    """
    ast = _parse_sql(standard_sql)
    if ast is None:
        return False
    changed = False
    for select in ast.find_all(exp.Select):
        where = select.args.get("where")
        if not isinstance(where, exp.Where):
            continue
        for comparison in where.find_all(
            exp.EQ,
            exp.NEQ,
            exp.GT,
            exp.GTE,
            exp.LT,
            exp.LTE,
        ):
            if comparison.find_ancestor(exp.Select) is not select:
                continue
            parts = _temporal_comparison_parts(comparison)
            if parts is None:
                continue
            value_expression, source_column, literal = parts
            if not _is_date_column(source_column.name):
                continue
            ref = _column_ref_in_select_data(data, source_column, select)
            if ref is None:
                ref = _query_column_ref_in_data(data, source_column, select, ast)
            actual = _actual_data_ref(data, ref) if ref else None
            if actual is None or not actual[0]:
                continue
            boundary = _temporal_boundary_value(
                source_column,
                literal,
                table=ref[0],
                schema_catalog=schema_catalog,
            )
            if boundary is None:
                continue
            effective = type(comparison)
            if value_expression is comparison.right:
                effective = {
                    exp.GT: exp.LT,
                    exp.GTE: exp.LTE,
                    exp.LT: exp.GT,
                    exp.LTE: exp.GTE,
                }.get(effective, effective)

            parsed = _coerce_datetime(boundary)
            if parsed is None:
                continue
            value = parsed
            if effective is exp.GT:
                value = parsed + timedelta(days=1)
            elif effective is exp.LT:
                value = parsed - timedelta(days=1)
            value = value.strftime(
                "%Y-%m-%d %H:%M:%S"
                if any((value.hour, value.minute, value.second, value.microsecond))
                else "%Y-%m-%d"
            )

            def satisfies(current: Any) -> bool:
                current_datetime = _coerce_datetime(current)
                if current_datetime is None:
                    return False
                target_value = _coerce_datetime(literal)
                current_value = current_datetime
                if target_value is None:
                    return False
                if effective is exp.EQ:
                    return current_value == target_value
                if effective is exp.NEQ:
                    return current_value != target_value
                if effective is exp.GT:
                    return current_value > target_value
                if effective is exp.GTE:
                    return current_value >= target_value
                if effective is exp.LT:
                    return current_value < target_value
                if effective is exp.LTE:
                    return current_value <= target_value
                return False
            with write_owner("materializer:temporal_filter_path"):
                for row in actual[0]:
                    if not satisfies(row.get(actual[1])):
                        row[actual[1]] = value
            changed = True
    return changed


def _materialize_subquery_membership_obligation_witness(
    data: dict[str, list[dict[str, Any]]],
    ast_diffs: list[ASTDiffNode],
    standard_sql: str = "",
    student_sql: str = "",
    *,
    schema_catalog: SchemaCatalog | None = None,
) -> None:
    """Keep both matching and non-matching correlated outer paths."""
    diff = next(
        (
            item for item in ast_diffs
            if item.diff_type == "correlated_predicate_changed"
            and (
                not item.extra.get("subquery_depth")
                or item.extra.get("standard_membership_table")
            )
        ),
        None,
    )
    if diff is None:
        diff = next(
            (
                item for item in ast_diffs
                if item.diff_type == "null_sensitive_antijoin_equivalence"
            ),
            None,
        )
    if diff is None:
        # ``IN`` -> ``NOT IN`` is an ordinary membership mutation, not by
        # itself a NULL-semantics obligation.  The legacy NOT IN probe may
        # still leave a NULL in the inner relation, which is legal SQL but can
        # mask the simpler overlap/outer-only path (both queries then return
        # no rows).  Handle this diff here so the final membership owner can
        # remove that accidental NULL when the inner subquery has a filter.
        diff = next(
            (
                item for item in ast_diffs
                if item.diff_type == "in_predicate_negation_changed"
                and item.extra.get("standard_membership_table")
            ),
            None,
        )
    if diff is None:
        return
    if diff.diff_type == "null_sensitive_antijoin_equivalence":
        _materialize_null_sensitive_antijoin_membership_path(
            data,
            diff,
            standard_sql,
            student_sql,
            schema_catalog=schema_catalog,
        )
        return
    requires_inner_null = False
    outer_table = _norm_name(str(diff.extra.get("standard_source_table") or ""))
    inner_table = _norm_name(str(diff.extra.get("standard_membership_table") or ""))
    outer_column = _norm_name(str(diff.extra.get("standard_outer_column") or ""))
    inner_column = _norm_name(str(diff.extra.get("standard_membership_column") or ""))
    if not outer_table or not inner_table or not outer_column or not inner_column:
        return
    outer_name = next((name for name in data if _norm_name(name) == outer_table), None)
    inner_name = next((name for name in data if _norm_name(name) == inner_table), None)
    outer_rows = data.get(outer_name or "") or []
    inner_rows = data.get(inner_name or "") or []
    if len(outer_rows) < 2 or not inner_rows:
        return
    outer_column_actual = _column_lookup(list(outer_rows[0])).get(outer_column)
    inner_column_actual = _column_lookup(list(inner_rows[0])).get(inner_column)
    if not outer_column_actual or not inner_column_actual:
        return
    ordinary_filtered_membership = False
    ordinary_membership_select: exp.Select | None = None
    if diff.diff_type == "in_predicate_negation_changed":
        standard_ast = _parse_sql(standard_sql)
        root_select = _top_select(standard_ast) if standard_ast is not None else None
        if isinstance(root_select, exp.Select):
            for in_node in root_select.find_all(exp.In):
                if in_node.find_ancestor(exp.Select) is not root_select:
                    continue
                query = in_node.args.get("query")
                inner_select = (
                    query.this
                    if isinstance(query, exp.Subquery)
                    and isinstance(query.this, exp.Select)
                    else None
                )
                projected = (
                    inner_select.expressions[0]
                    if isinstance(inner_select, exp.Select)
                    and inner_select.expressions
                    else None
                )
                projected = projected.this if isinstance(projected, exp.Alias) else projected
                if not isinstance(inner_select, exp.Select) or not isinstance(projected, exp.Column):
                    continue
                resolved_inner = _column_ref_in_select_data(
                    data,
                    projected,
                    inner_select,
                )
                if resolved_inner == (inner_table, inner_column):
                    ordinary_membership_select = inner_select
                    ordinary_filtered_membership = isinstance(
                        inner_select.args.get("where"),
                        exp.Where,
                    )
                    break
    inner_values = {
        row.get(inner_column_actual)
        for row in inner_rows
        if row.get(inner_column_actual) is not None
    }
    if not inner_values:
        return
    with write_owner("materializer:subquery_membership_paths"):
        match_value = min(
            inner_values,
            key=lambda value: (type(value).__name__, repr(value)),
        )
        outer_rows[0][outer_column_actual] = match_value
        # On an unfiltered ordinary IN/NOT IN mutation, preserve the NULL
        # deliberately injected by the NULL probe so the existing three-valued
        # logic path remains available.  Use an existing non-NULL row as the
        # overlap row instead.  Filtered subqueries need row zero rewritten so
        # its local predicates can be made reachable below.
        match_row_index = 0
        if not ordinary_filtered_membership:
            match_row_index = next(
                (
                    index
                    for index, row in enumerate(inner_rows)
                    if row.get(inner_column_actual) is not None
                ),
                0,
            )
        inner_rows[match_row_index][inner_column_actual] = match_value
        non_match = 920000 + len(outer_rows)
        while non_match in inner_values:
            non_match += 1
        outer_rows[-1][outer_column_actual] = non_match
        standard_sql = str(diff.extra.get("standard_sql") or "")
        standard_ast = _parse_sql(standard_sql)
        if standard_ast is not None:
            aliases = _table_aliases(standard_ast)
            inner_aliases = {
                alias for alias, table in aliases.items()
                if _norm_name(table) == inner_table
            } | {inner_table}
            for comparison in standard_ast.find_all(
                exp.GT, exp.GTE, exp.LT, exp.LTE, exp.EQ, exp.NEQ
            ):
                if not isinstance(comparison.left, exp.Column) or not isinstance(comparison.right, exp.Literal):
                    continue
                if _norm_name(comparison.left.table or "") not in inner_aliases:
                    continue
                actual_column = _column_lookup(list(inner_rows[0])).get(
                    _norm_name(comparison.left.name)
                )
                boundary = _literal_value(comparison.right)
                if not actual_column or not isinstance(boundary, (int, float, Decimal)):
                    continue
                positive = _comparison_truth_value(comparison, True)
                if positive is not None:
                    inner_rows[0][actual_column] = positive
                break
        if ordinary_filtered_membership and ordinary_membership_select is not None:
            # A filtered ordinary IN/NOT IN mutation needs a non-NULL inner
            # value that actually reaches the subquery result.  Keep explicit
            # ``projected_column IS NULL`` queries on the dedicated NULL path;
            # all other NULLs are compatibility-probe residue and may be
            # replaced by a fresh non-overlapping value.
            projected_null_required = any(
                isinstance(check.this, exp.Column)
                and _column_ref_in_select_data(
                    data,
                    check.this,
                    ordinary_membership_select,
                ) == (inner_table, inner_column)
                and not isinstance(check.parent, exp.Not)
                and isinstance(check.expression, exp.Null)
                for check in ordinary_membership_select.find_all(exp.Is)
            )
            if not projected_null_required:
                used_values = {
                    row.get(inner_column_actual)
                    for row in inner_rows
                    if row.get(inner_column_actual) is not None
                }
                replacement = _counter_value(
                    inner_column_actual,
                    min(
                        used_values,
                        key=lambda value: (type(value).__name__, repr(value)),
                        default=None,
                    ),
                )
                for row in inner_rows:
                    if row.get(inner_column_actual) is not None:
                        continue
                    while replacement is None or replacement in used_values:
                        replacement = _counter_value(
                            inner_column_actual,
                            replacement,
                        )
                    row[inner_column_actual] = replacement
                    used_values.add(replacement)
        if requires_inner_null:
            inner_rows[-1][inner_column_actual] = None
    # A changed correlation key needs stronger evidence than ordinary
    # membership overlap: one outer key must match the standard inner column
    # while matching no value in the student's wrong inner column. Apply this
    # last so generic membership materialization cannot align both keys again.
    _materialize_correlated_key_drift_witness(
        data,
        standard_sql,
        student_sql,
    )


def _materialize_null_sensitive_antijoin_membership_path(
    data: dict[str, list[dict[str, Any]]],
    diff: ASTDiffNode,
    standard_sql: str,
    student_sql: str,
    *,
    schema_catalog: SchemaCatalog | None = None,
) -> bool:
    """Finalize one bounded NULL-sensitive NOT IN/NOT EXISTS witness."""
    metadata = diff.extra
    outer_ref = (
        _norm_name(str(metadata.get("standard_source_table") or "")),
        _norm_name(str(metadata.get("standard_outer_column") or "")),
    )
    inner_ref = (
        _norm_name(str(metadata.get("standard_membership_table") or "")),
        _norm_name(str(metadata.get("standard_membership_column") or "")),
    )
    outer_actual = _actual_data_ref(data, outer_ref)
    inner_actual = _actual_data_ref(data, inner_ref)
    if not all((*outer_ref, *inner_ref)) or outer_actual is None or inner_actual is None:
        return False
    outer_rows, outer_column = outer_actual
    inner_rows, inner_column = inner_actual
    if len(outer_rows) < 2 or len(inner_rows) < 2:
        return False

    not_in_sql = (
        standard_sql
        if str(metadata.get("not_in_side") or "standard") == "standard"
        else student_sql
    )
    not_in_ast = _parse_sql(not_in_sql)
    outer_select = _top_select(not_in_ast) if not_in_ast is not None else None
    membership: tuple[exp.In, exp.Select] | None = None
    if isinstance(outer_select, exp.Select):
        for in_node in outer_select.find_all(exp.In):
            if (
                in_node.find_ancestor(exp.Select) is not outer_select
                or not isinstance(in_node.parent, exp.Not)
                or not isinstance(in_node.this, exp.Column)
            ):
                continue
            query = in_node.args.get("query")
            inner_select = query.this if isinstance(query, exp.Subquery) else None
            projected = (
                inner_select.expressions[0]
                if isinstance(inner_select, exp.Select) and inner_select.expressions
                else None
            )
            projected = projected.this if isinstance(projected, exp.Alias) else projected
            if not isinstance(inner_select, exp.Select) or not isinstance(projected, exp.Column):
                continue
            if (
                _scope_column_ref(in_node.this, outer_select) == outer_ref
                and _scope_column_ref(projected, inner_select) == inner_ref
            ):
                membership = in_node, inner_select
                break
    if membership is None:
        return False
    _, inner_select = membership

    def satisfy_inner_filters(row_index: int) -> None:
        _set_select_local_literal_predicates(data, inner_select, row_index)
        where = inner_select.args.get("where")
        if not isinstance(where, exp.Where):
            return
        for check in where.find_all(exp.Is):
            if (
                check.find_ancestor(exp.Select) is not inner_select
                or not isinstance(check.this, exp.Column)
                or not isinstance(check.expression, exp.Null)
            ):
                continue
            ref = _scope_column_ref(check.this, inner_select)
            actual = _actual_data_ref(data, ref) if ref is not None else None
            if actual is None:
                continue
            rows, column = actual
            if row_index >= len(rows):
                continue
            if isinstance(check.parent, exp.Not):
                value = rows[row_index].get(column)
                rows[row_index][column] = (
                    value
                    if value is not None
                    else typed_seed(
                        ref[0],
                        ref[1],
                        row_index + 1,
                    )
                )
            else:
                rows[row_index][column] = None

    def projected_null_reaches_filter() -> bool:
        where = inner_select.args.get("where")
        if not isinstance(where, exp.Where):
            return True
        for column in where.find_all(exp.Column):
            if (
                column.find_ancestor(exp.Select) is not inner_select
                or _scope_column_ref(column, inner_select) != inner_ref
            ):
                continue
            check = column.parent
            if (
                isinstance(check, exp.Is)
                and isinstance(check.expression, exp.Null)
                and not isinstance(check.parent, exp.Not)
            ):
                continue
            return False
        return True

    def satisfy_outer_null_filters(row_index: int) -> None:
        """Keep a selected outer row reachable through local NULL tests."""
        if not isinstance(outer_select, exp.Select):
            return
        where = outer_select.args.get("where")
        if not isinstance(where, exp.Where):
            return
        actual = _actual_data_ref(data, outer_ref)
        if actual is None or row_index >= len(actual[0]):
            return
        rows, column = actual
        for check in where.find_all(exp.Is):
            if (
                check.find_ancestor(exp.Select) is not outer_select
                or not isinstance(check.this, exp.Column)
                or _scope_column_ref(check.this, outer_select) != outer_ref
                or not isinstance(check.expression, exp.Null)
            ):
                continue
            if isinstance(check.parent, exp.Not):
                rows[row_index][column] = typed_seed(
                    outer_ref[0],
                    outer_ref[1],
                    row_index + 1,
                )
            else:
                rows[row_index][column] = None

    inner_schema = _catalog_column_schema(
        inner_ref[0],
        inner_ref[1],
        schema_catalog,
    )
    outer_schema = _catalog_column_schema(
        outer_ref[0],
        outer_ref[1],
        schema_catalog,
    )
    inner_nullable = inner_schema is None or inner_schema.nullable
    outer_nullable = outer_schema is None or outer_schema.nullable
    metadata = diff.extra
    require_inner_null = bool(metadata.get("require_inner_null", True))
    require_outer_null = bool(metadata.get("require_outer_null", False))
    null_index = 0
    match_index = 1

    def typed_seed(table: str, column: str, index: int, value: Any = None) -> Any:
        """Use declared column affinity for short/ambiguous witness columns."""
        kind = _authoritative_column_kind(table, column, schema_catalog)
        seed = _seed_value(column, index) if value is None else value
        return _coerce_typed_seed(seed, kind, column, index) if kind else seed

    def typed_counter(table: str, column: str, value: Any, index: int) -> Any:
        kind = _authoritative_column_kind(table, column, schema_catalog)
        normalized = _coerce_typed_seed(value, kind, column, index) if kind else value
        counter = _counter_value(column, normalized)
        return _coerce_typed_seed(counter, kind, column, index) if kind else counter

    with write_owner("materializer:null_sensitive_antijoin"):
        # Earlier generic probes may have filled a short column such as ``v``
        # with heuristic text.  Normalize only the two physical membership
        # columns here, before copying values between their witness rows.
        for index, row in enumerate(outer_rows):
            if row.get(outer_column) is not None:
                row[outer_column] = typed_seed(
                    outer_ref[0], outer_column, index, row[outer_column]
                )
        for index, row in enumerate(inner_rows):
            if row.get(inner_column) is not None:
                row[inner_column] = typed_seed(
                    inner_ref[0], inner_column, index, row[inner_column]
                )
        # Keep the selected outer rows reachable through predicates unrelated
        # to the anti-join.  This is deliberately limited to literal local
        # predicates in the root query block; joins and correlations retain
        # their own topology owners.
        if isinstance(outer_select, exp.Select):
            _set_select_local_literal_predicates(data, outer_select, 0)
            _set_select_local_literal_predicates(
                data,
                outer_select,
                len(outer_rows) - (2 if require_outer_null else 1),
            )
        satisfy_inner_filters(null_index)
        satisfy_inner_filters(match_index)
        match_value = inner_rows[match_index].get(inner_column)
        if match_value is None:
            match_value = typed_seed(
                inner_ref[0], inner_column, match_index + 1
            )
            if match_value is None:
                match_value = 930001
            inner_rows[match_index][inner_column] = match_value

        same_cell_domain = (
            outer_rows is inner_rows and outer_column == inner_column
        )
        if same_cell_domain:
            if not outer_nullable:
                return False
            if require_outer_null:
                outer_rows[match_index][outer_column] = match_value
                outer_rows[-2][outer_column] = typed_counter(
                    outer_ref[0],
                    outer_column,
                    match_value,
                    len(outer_rows) - 2,
                )
                outer_rows[-1][outer_column] = None
            else:
                outer_rows[null_index][outer_column] = None
                outer_rows[match_index][outer_column] = match_value
            return True

        outer_rows[0][outer_column] = match_value
        inner_values = {
            row.get(inner_column)
            for row in inner_rows
            if row.get(inner_column) is not None
        }
        non_match = typed_counter(
            outer_ref[0], outer_column, match_value, len(outer_rows) + 1
        )
        while non_match is None or non_match in inner_values:
            non_match = typed_counter(
                outer_ref[0], outer_column, non_match, len(outer_rows) + 1
            )
        outer_rows[-2 if require_outer_null else -1][outer_column] = non_match

        # Apply root-local NULL predicates after the anti-join values are set;
        # otherwise ``v IS NULL`` is overwritten by the generic match-path
        # assignment above and the student EXISTS branch remains unreachable.
        satisfy_outer_null_filters(0)
        if require_outer_null:
            satisfy_outer_null_filters(len(outer_rows) - 1)

        if require_inner_null and inner_nullable and projected_null_reaches_filter():
            inner_rows[null_index][inner_column] = None
            satisfy_inner_filters(null_index)
            inner_rows[null_index][inner_column] = None
            return True
        if require_outer_null and outer_nullable:
            outer_rows[-1][outer_column] = None
            return True
    return False


def _queries_are_supported_equivalent_rewrites(
    standard_ast: exp.Expression,
    student_ast: exp.Expression,
    *,
    schema_catalog: SchemaCatalog | None = None,
) -> bool:
    """Recognize narrow, semantics-preserving rewrites before emitting noisy diffs."""
    return any((
        _unreferenced_output_aliases_equivalent(standard_ast, student_ast),
        _double_negation_equivalent(standard_ast, student_ast),
        _nullif_coalesce_case_equivalent(standard_ast, student_ast),
        _simple_searched_case_equivalent(standard_ast, student_ast),
        _is_true_filter_equivalent(standard_ast, student_ast),
        _constant_true_filter_equivalent(standard_ast, student_ast),
        _in_list_or_equivalent(standard_ast, student_ast),
        _singleton_equality_in_filter_equivalent(standard_ast, student_ast),
        _comparison_operands_mirrored_equivalent(standard_ast, student_ast),
        _order_reference_equivalent(
            standard_ast,
            student_ast,
            schema_catalog=schema_catalog,
        ),
        _simple_join_using_on_equivalent(
            standard_ast,
            student_ast,
            schema_catalog=schema_catalog,
        ),
        _simple_join_using_on_equivalent(
            student_ast,
            standard_ast,
            schema_catalog=schema_catalog,
        ),
        _schema_complete_star_projection_equivalent(
            standard_ast,
            student_ast,
            schema_catalog=schema_catalog,
        ),
        _standalone_literal_projection_equivalent(
            standard_ast,
            student_ast,
        ),
        _null_safe_equality_filter_equivalent(standard_ast, student_ast),
        _where_boolean_absorption_equivalent(standard_ast, student_ast),
        _between_closed_range_equivalent(standard_ast, student_ast),
        _global_extreme_comparison_equivalent(standard_ast, student_ast),
        _simple_derived_table_inline_equivalent(standard_ast, student_ast),
        _simple_derived_table_inline_equivalent(student_ast, standard_ast),
        _named_window_inline_equivalent(standard_ast, student_ast),
        _named_window_inline_equivalent(student_ast, standard_ast),
        _statically_empty_scalar_subquery_null_equivalent(
            standard_ast,
            student_ast,
        ),
        _statically_empty_scalar_subquery_null_equivalent(
            student_ast,
            standard_ast,
        ),
        _simple_cte_dependency_chain_inline_equivalent(standard_ast, student_ast),
        _simple_cte_dependency_chain_inline_equivalent(student_ast, standard_ast),
        _single_row_aggregate_cte_scalar_equivalent(standard_ast, student_ast),
        _single_row_aggregate_cte_scalar_equivalent(student_ast, standard_ast),
        _simple_cte_inline_equivalent(standard_ast, student_ast),
        _simple_cte_inline_equivalent(student_ast, standard_ast),
        _simple_in_join_equivalent(standard_ast, student_ast),
        _simple_in_join_equivalent(student_ast, standard_ast),
        _simple_not_exists_antijoin_equivalent(standard_ast, student_ast),
        _simple_not_exists_antijoin_equivalent(student_ast, standard_ast),
        _strict_in_exists_filter_equivalent(standard_ast, student_ast),
        _strict_in_exists_filter_equivalent(student_ast, standard_ast),
        _schema_nonnull_in_exists_equivalent(
            standard_ast,
            student_ast,
            schema_catalog=schema_catalog,
        ),
        _schema_nonnull_in_exists_equivalent(
            student_ast,
            standard_ast,
            schema_catalog=schema_catalog,
        ),
        _query_null_filters_prove_in_exists_equivalent(
            standard_ast,
            student_ast,
            schema_catalog=schema_catalog,
        ),
        _query_null_filters_prove_in_exists_equivalent(
            student_ast,
            standard_ast,
            schema_catalog=schema_catalog,
        ),
        _strict_monotonic_recursive_union_modifier_equivalent(
            standard_ast,
            student_ast,
        ),
        _commutative_set_branch_permutation_equivalent(
            standard_ast,
            student_ast,
        ),
    ))


def _schema_numeric_projection_identities_equivalent(
    standard_ast: exp.Expression,
    student_ast: exp.Expression,
    schema_catalog: SchemaCatalog,
) -> bool:
    """Recognize simple arithmetic identities only for numeric columns.

    SQLite affinity and value coercion make a blind ``column + 0`` rewrite
    unsound for text columns.  This rule resolves the projected column through
    the supplied physical schema and declines explicitly non-numeric columns.
    """
    if not isinstance(standard_ast, exp.Select) or not isinstance(
        student_ast, exp.Select
    ):
        return False

    def numeric_column(column: exp.Column, select: exp.Select) -> bool:
        aliases = _table_aliases(select)
        table_ref = _norm_name(column.table or "")
        table_name = aliases.get(table_ref, table_ref)
        if not table_name:
            direct_tables = {
                _norm_name(table.name)
                for table in select.find_all(exp.Table)
                if table.find_ancestor(exp.Select) is select
            }
            if len(direct_tables) != 1:
                return False
            table_name = next(iter(direct_tables))
        column_schema = _catalog_column_schema(
            table_name,
            str(column.name),
            schema_catalog,
        )
        if column_schema is None:
            return False
        if column_schema.has_explicit_type:
            return _authoritative_column_kind(
                table_name,
                str(column.name),
                schema_catalog,
            ) == "numeric"
        return _is_numeric_column(str(column.name))

    def is_number(node: exp.Expression, expected: int) -> bool:
        value = _literal_value(node)
        return (
            isinstance(value, (int, float, Decimal))
            and not isinstance(value, bool)
            and value == expected
        )

    def simplify(node: exp.Expression, select: exp.Select) -> exp.Expression:
        if isinstance(node, exp.Add):
            if isinstance(node.left, exp.Column) and is_number(node.right, 0):
                if numeric_column(node.left, select):
                    return node.left.copy()
            if isinstance(node.right, exp.Column) and is_number(node.left, 0):
                if numeric_column(node.right, select):
                    return node.right.copy()
        if isinstance(node, exp.Sub):
            if isinstance(node.left, exp.Column) and is_number(node.right, 0):
                if numeric_column(node.left, select):
                    return node.left.copy()
        if isinstance(node, exp.Mul):
            if isinstance(node.left, exp.Column) and is_number(node.right, 1):
                if numeric_column(node.left, select):
                    return node.left.copy()
            if isinstance(node.right, exp.Column) and is_number(node.left, 1):
                if numeric_column(node.right, select):
                    return node.right.copy()
        return node

    def normalized(ast: exp.Select) -> tuple[str, bool]:
        copied = ast.copy()
        changed = False
        projections: list[exp.Expression] = []
        for item in copied.expressions:
            expression = item.this if isinstance(item, exp.Alias) else item
            simplified = simplify(expression, copied)
            if simplified is not expression:
                changed = True
            if isinstance(item, exp.Alias):
                replacement = item.copy()
                replacement.set("this", simplified)
                projections.append(replacement)
            else:
                projections.append(simplified)
        copied.set("expressions", projections)
        return _sql_of(copied), changed

    standard = normalized(standard_ast)
    student = normalized(student_ast)
    return bool(
        (standard[1] or student[1])
        and standard[0] == student[0]
    )


def _schema_nonnull_in_exists_equivalent(
    in_ast: exp.Expression,
    exists_ast: exp.Expression,
    *,
    schema_catalog: SchemaCatalog | None = None,
) -> bool:
    """Prove the negated membership rewrite under non-NULL key constraints.

    ``NOT IN`` and a correlated ``NOT EXISTS`` differ only on NULL paths when
    their local filters and correlation key are otherwise identical.  If the
    outer operand and the projected inner column are both declared NOT NULL,
    that path is impossible and the rewrite is a schema-backed equivalence.
    """
    if schema_catalog is None:
        return False
    metadata = _strict_in_exists_filter_metadata(
        in_ast,
        exists_ast,
        allow_negated=True,
    )
    if metadata is None:
        return False
    outer = _catalog_column_schema(
        str(metadata.get("standard_source_table") or ""),
        str(metadata.get("standard_outer_column") or ""),
        schema_catalog,
    )
    inner = _catalog_column_schema(
        str(metadata.get("standard_membership_table") or ""),
        str(metadata.get("standard_membership_column") or ""),
        schema_catalog,
    )
    return bool(outer is not None and inner is not None and not outer.nullable and not inner.nullable)


def _query_null_filters_prove_in_exists_equivalent(
    in_ast: exp.Expression,
    exists_ast: exp.Expression,
    *,
    schema_catalog: SchemaCatalog | None = None,
) -> bool:
    """Prove NULL-path absence from local ``IS`` predicates.

    The physical schema is not the only source of nullability.  A root
    ``outer_key IS NOT NULL`` predicate removes the outer UNKNOWN path, while
    an inner ``projected_key IS NOT NULL`` predicate removes the inner-NULL
    path.  Conversely, ``projected_key IS NULL`` on a declared NOT NULL column
    makes the membership subquery empty and is also safe for this rewrite.
    """
    metadata = _strict_in_exists_filter_metadata(
        in_ast,
        exists_ast,
        allow_negated=True,
    )
    if metadata is None:
        return False
    if schema_catalog is None:
        return False

    in_outer = in_ast.find(exp.Select)
    in_node = next(iter(in_ast.find_all(exp.In)), None)
    in_inner = (
        in_node.args.get("query").this
        if isinstance(in_node, exp.In)
        and isinstance(in_node.args.get("query"), exp.Subquery)
        else None
    )
    if not isinstance(in_outer, exp.Select) or not isinstance(in_inner, exp.Select):
        return False
    outer_ref = (
        _norm_name(str(metadata.get("standard_source_table") or "")),
        _norm_name(str(metadata.get("standard_outer_column") or "")),
    )
    inner_ref = (
        _norm_name(str(metadata.get("standard_membership_table") or "")),
        _norm_name(str(metadata.get("standard_membership_column") or "")),
    )

    def local_null_test(select: exp.Select, ref: tuple[str, str], *, want_null: bool) -> bool:
        where = select.args.get("where")
        if not isinstance(where, exp.Where):
            return False
        for node in where.find_all(exp.Is):
            if (
                node.find_ancestor(exp.Select) is not select
                or not isinstance(node.this, exp.Column)
                or _scope_column_ref(node.this, select) != ref
                or not isinstance(node.expression, exp.Null)
            ):
                continue
            is_null = not isinstance(node.parent, exp.Not)
            if is_null == want_null:
                return True
        return False

    outer_nonnull = local_null_test(in_outer, outer_ref, want_null=False)
    inner_nonnull = local_null_test(in_inner, inner_ref, want_null=False)
    inner_forced_null = local_null_test(in_inner, inner_ref, want_null=True)
    inner_schema = _catalog_column_schema(inner_ref[0], inner_ref[1], schema_catalog)
    if inner_forced_null and inner_schema is not None and not inner_schema.nullable:
        return True
    # The rewrite is NULL-safe when both possible sources of UNKNOWN are
    # removed, regardless of whether each removal comes from schema or SQL.
    outer_schema = _catalog_column_schema(outer_ref[0], outer_ref[1], schema_catalog)
    return bool(
        (outer_nonnull or (outer_schema is not None and not outer_schema.nullable))
        and (inner_nonnull or (inner_schema is not None and not inner_schema.nullable))
    )






def _materialize_declared_join_witness(
    data: dict[str, list[dict[str, Any]]],
    ast_diffs: list[ASTDiffNode],
    *,
    standard_sql: str | None = None,
    schema_catalog: SchemaCatalog | None = None,
) -> None:
    """Materialize the declared JOIN topology after compatibility probes.

    JOIN-specific legacy probes can be followed by aggregate/window/PK repair
    logic.  Re-establishing only the declared endpoint here guarantees that a
    JOIN obligation's validator observes one matched value and one genuinely
    dangling left value, without scanning unrelated same-name columns.
    """
    def _pair_parts(pair: Any) -> tuple[str, str, str, str] | None:
        if len(pair) == 2 and all(isinstance(item, (tuple, list)) for item in pair):
            (left_table, left_column), (right_table, right_column) = pair
            return str(left_table), str(left_column), str(right_table), str(right_column)
        if len(pair) == 4:
            return tuple(str(item) for item in pair)  # type: ignore[return-value]
        return None

    def _pair_signature(pair: tuple[str, str, str, str]) -> tuple[tuple[str, str], tuple[str, str]]:
        left_table, left_column, right_table, right_column = pair
        return tuple(sorted((
            (_norm_name(left_table), _norm_name(left_column)),
            (_norm_name(right_table), _norm_name(right_column)),
        )))  # type: ignore[return-value]

    def _edge_key(pair: tuple[str, str, str, str]) -> tuple[str, str]:
        return tuple(sorted((_norm_name(pair[0]), _norm_name(pair[2]))))

    def _group_pairs(raw_pairs: Any) -> dict[tuple[str, str], list[tuple[str, str, str, str]]]:
        grouped: dict[tuple[str, str], list[tuple[str, str, str, str]]] = defaultdict(list)
        for raw_pair in raw_pairs or ():
            parts = _pair_parts(raw_pair)
            if parts is not None:
                grouped[_edge_key(parts)].append(parts)
        return grouped

    def _orient_pair(
        pair: tuple[str, str, str, str],
        left_table: str,
        right_table: str,
    ) -> tuple[str, str] | None:
        pair_left, left_column, pair_right, right_column = pair
        if (
            _norm_name(pair_left) == _norm_name(left_table)
            and _norm_name(pair_right) == _norm_name(right_table)
        ):
            return left_column, right_column
        if (
            _norm_name(pair_left) == _norm_name(right_table)
            and _norm_name(pair_right) == _norm_name(left_table)
        ):
            return right_column, left_column
        return None

    def _actual_column(rows: list[dict[str, Any]], column: str) -> str | None:
        if not rows:
            return None
        return next(
            (name for name in rows[0] if _norm_name(name) == _norm_name(column)),
            None,
        )

    def _materialize_self_edge(
        rows: list[dict[str, Any]],
        standard_pairs: list[tuple[str, str, str, str]],
        student_pairs: list[tuple[str, str, str, str]],
        owner: str,
    ) -> bool:
        if len(rows) < 2:
            return False

        def _reflexive(pairs: list[tuple[str, str, str, str]]) -> bool:
            return bool(pairs) and all(
                _norm_name(pair[1]) == _norm_name(pair[3]) for pair in pairs
            )

        standard_reflexive = _reflexive(standard_pairs)
        student_reflexive = _reflexive(student_pairs)
        if standard_reflexive == student_reflexive:
            return False
        non_reflexive_pairs = student_pairs if standard_reflexive else standard_pairs
        differing_pair = next(
            (
                pair for pair in non_reflexive_pairs
                if _norm_name(pair[1]) != _norm_name(pair[3])
            ),
            None,
        )
        if differing_pair is None:
            return False
        left_column = _actual_column(rows, differing_pair[1])
        right_column = _actual_column(rows, differing_pair[3])
        if left_column is None or right_column is None:
            return False

        anchor = 930000 + len(rows) * 100
        with write_owner(owner):
            # The reflexive predicate matches every row to itself.  Give its
            # key a unique value per row so the non-reflexive path can be made
            # to match every row except the final dangling one.
            reflexive_pairs = standard_pairs if standard_reflexive else student_pairs
            for pair_index, pair in enumerate(reflexive_pairs):
                column = _actual_column(rows, pair[1])
                if column is None:
                    continue
                base = anchor + pair_index * 1000
                for row_index, row in enumerate(rows):
                    row[column] = base + row_index

            for row_index, row in enumerate(rows[:-1]):
                next_row = rows[row_index + 1]
                for pair in non_reflexive_pairs:
                    pair_left = _actual_column(rows, pair[1])
                    pair_right = _actual_column(rows, pair[3])
                    if pair_left is not None and pair_right is not None:
                        row[pair_left] = next_row[pair_right]

            right_values = {row.get(right_column) for row in rows}
            dangling = anchor + 90000
            while dangling in right_values:
                dangling += 1
            rows[-1][left_column] = dangling
        return True

    def _materialize_two_table_edge(
        left_rows: list[dict[str, Any]],
        right_rows: list[dict[str, Any]],
        left_table: str,
        right_table: str,
        standard_pairs: list[tuple[str, str, str, str]],
        student_pairs: list[tuple[str, str, str, str]],
        owner: str,
    ) -> bool:
        def _attempt(
            satisfied_pairs: list[tuple[str, str, str, str]],
            violated_pairs: list[tuple[str, str, str, str]],
        ) -> bool:
            parent: dict[tuple[str, str], tuple[str, str]] = {}

            def _find(cell: tuple[str, str]) -> tuple[str, str]:
                parent.setdefault(cell, cell)
                if parent[cell] != cell:
                    parent[cell] = _find(parent[cell])
                return parent[cell]

            def _union(left: tuple[str, str], right: tuple[str, str]) -> None:
                left_root = _find(left)
                right_root = _find(right)
                if left_root != right_root:
                    parent[right_root] = left_root

            oriented_satisfied: list[tuple[str, str]] = []
            for pair in satisfied_pairs:
                oriented = _orient_pair(pair, left_table, right_table)
                if oriented is None:
                    return False
                left_column = _actual_column(left_rows, oriented[0])
                right_column = _actual_column(right_rows, oriented[1])
                if left_column is None or right_column is None:
                    return False
                oriented_satisfied.append((left_column, right_column))
                _union(("left", left_column), ("right", right_column))

            violating: tuple[str, str] | None = None
            for pair in violated_pairs:
                oriented = _orient_pair(pair, left_table, right_table)
                if oriented is None:
                    continue
                left_column = _actual_column(left_rows, oriented[0])
                right_column = _actual_column(right_rows, oriented[1])
                if left_column is None or right_column is None:
                    continue
                left_cell = ("left", left_column)
                right_cell = ("right", right_column)
                if _find(left_cell) != _find(right_cell):
                    violating = left_column, right_column
                    break
            if violating is None:
                return False

            roots = {_find(cell) for cell in parent}
            root_values = {
                root: 920000 + index * 100
                for index, root in enumerate(sorted(roots))
            }
            with write_owner(owner):
                for cell in list(parent):
                    side, column = cell
                    rows = left_rows if side == "left" else right_rows
                    rows[0][column] = root_values[_find(cell)]

                violating_left, violating_right = violating
                left_value = left_rows[0].get(violating_left)
                right_value = right_rows[0].get(violating_right)
                if left_value == right_value:
                    right_rows[0][violating_right] = 929999
                # Prevent another right row from accidentally satisfying the
                # deliberately false conjunct for the candidate left row.
                for row_index, row in enumerate(right_rows[1:], start=1):
                    if row.get(violating_right) == left_value:
                        row[violating_right] = 929999 + row_index
            return True

        standard_signatures = {_pair_signature(pair) for pair in standard_pairs}
        student_signatures = {_pair_signature(pair) for pair in student_pairs}
        student_only = [
            pair for pair in student_pairs
            if _pair_signature(pair) not in standard_signatures
        ]
        standard_only = [
            pair for pair in standard_pairs
            if _pair_signature(pair) not in student_signatures
        ]
        return (
            bool(student_only) and _attempt(standard_pairs, student_only)
        ) or (
            bool(standard_only) and _attempt(student_pairs, standard_only)
        )

    def _restore_left_row_filter(
        left_rows: list[dict[str, Any]],
        left_table: str,
    ) -> None:
        """Make the deliberately dangling left row reach a query WHERE.

        A dangling tuple only distinguishes LEFT from INNER JOIN when it also
        survives the standard query's row filter.  Legacy generation used a
        generic non-matching marker (``not_Chicago``), which made the
        validator report a known gap for otherwise valid outer-join examples.
        Apply only scalar predicates owned by the preserved relation and do it
        before the final key is re-dangled.  We never alter a primary/foreign
        key here, so the resulting witness remains a valid fixture.
        """
        if not standard_sql or not left_rows:
            return
        ast = _parse_sql(standard_sql)
        select = _top_select(ast) if ast is not None else None
        if not isinstance(select, exp.Select):
            return
        target_index = len(left_rows) - 1
        for rows, column, true_value, _false_value in _select_local_scalar_predicates(
            data,
            select,
        ):
            if rows is left_rows and target_index < len(rows):
                rows[target_index][column] = true_value
        # Rich predicates (IN/BETWEEN/NULL) are intentionally best-effort;
        # scalar equality/range bindings above cover the common teaching shape
        # while leaving unsupported filters to the normal bounded-gap guard.
        if target_index < len(left_rows):
            _set_query_block_rich_predicates(
                data,
                select,
                ast,
                row_index=target_index,
            )

    for diff in ast_diffs:
        if diff.diff_type == "join_on_changed":
            standard_pairs = (diff.extra or {}).get("standard_join_pairs") or ()
            student_pairs = (diff.extra or {}).get("student_join_pairs") or ()
            standard_by_edge = _group_pairs(standard_pairs)
            student_by_edge = _group_pairs(student_pairs)
            for edge in sorted(set(standard_by_edge) | set(student_by_edge)):
                standard_edge = standard_by_edge.get(edge, [])
                student_edge = student_by_edge.get(edge, [])
                if (
                    {_pair_signature(pair) for pair in standard_edge}
                    == {_pair_signature(pair) for pair in student_edge}
                ):
                    continue
                declared = standard_edge or student_edge
                left_table, _, right_table, _ = declared[0]
                left_entry = next(
                    ((name, rows) for name, rows in data.items()
                     if _norm_name(name) == _norm_name(left_table) and rows),
                    None,
                )
                right_entry = next(
                    ((name, rows) for name, rows in data.items()
                     if _norm_name(name) == _norm_name(right_table) and rows),
                    None,
                )
                if not left_entry or not right_entry:
                    continue
                actual_left_table, left_rows = left_entry
                actual_right_table, right_rows = right_entry
                owner = f"materializer:{diff.diff_type}:join_predicate_divergence"
                if _norm_name(actual_left_table) == _norm_name(actual_right_table):
                    materialized = _materialize_self_edge(
                        left_rows,
                        standard_edge,
                        student_edge,
                        owner,
                    )
                else:
                    materialized = _materialize_two_table_edge(
                        left_rows,
                        right_rows,
                        actual_left_table,
                        actual_right_table,
                        standard_edge,
                        student_edge,
                        owner,
                    )
                if materialized:
                    break
            continue
        if diff.diff_type not in {
            "join_missing",
            "join_type_changed",
            "join_predicate_placement_changed",
        }:
            continue
        pairs = (diff.extra or {}).get("standard_join_pairs") or ()
        for pair in pairs:
            if len(pair) == 2 and all(isinstance(item, (tuple, list)) for item in pair):
                (left_table, left_column), (right_table, right_column) = pair
            elif len(pair) == 4:
                left_table, left_column, right_table, right_column = pair
            else:
                continue
            left_entry = next(((name, rows) for name, rows in data.items()
                               if _norm_name(name) == _norm_name(left_table) and rows), None)
            right_entry = next(((name, rows) for name, rows in data.items()
                                if _norm_name(name) == _norm_name(right_table) and rows), None)
            if not left_entry or not right_entry:
                continue
            _, left_rows = left_entry
            _, right_rows = right_entry
            left_actual = next((name for name in left_rows[0]
                                if _norm_name(name) == _norm_name(left_column)), None)
            right_actual = next((name for name in right_rows[0]
                                 if _norm_name(name) == _norm_name(right_column)), None)
            if not left_actual or not right_actual:
                continue
            # The final dangling-row probe may have put NULL on the right
            # endpoint.  NULL never matches an equality JOIN, so do not let
            # set iteration select it as the preserved matched key.
            right_values = [
                row.get(right_actual)
                for row in right_rows
                if row.get(right_actual) is not None
            ]
            # Preserve the first existing match and make the final left row
            # unambiguously absent from the right endpoint.
            if right_values:
                if left_rows:
                    left_rows[0][left_actual] = right_values[0]
                    unique_value = 900000 + len(left_rows)
                    while unique_value in set(right_values):
                        unique_value += 1
                    left_rows[-1][left_actual] = unique_value
                    # The preserved row must satisfy the standard WHERE.  Do
                    # this after assigning the unique key so the filter pass
                    # cannot accidentally restore the JOIN endpoint.
                    _restore_left_row_filter(left_rows, str(left_entry[0]))
                    left_rows[-1][left_actual] = unique_value


def _query_source_column_lineage(
    data: dict[str, list[dict[str, Any]]],
    source: exp.Expression,
    column_name: str,
    root_ast: exp.Expression,
    seen: set[tuple[int, str]],
) -> list[tuple[str, str]]:
    normalized_column = _norm_name(column_name)
    if not normalized_column:
        return []
    if isinstance(source, exp.Table):
        source_name = _norm_name(source.name)
        # A CTE shadows a physical table with the same SQL-visible name.
        source_select = _query_cte_select(root_ast, source_name)
        if source_select is None:
            actual = _actual_data_ref(data, (source_name, normalized_column))
            return [(source_name, normalized_column)] if actual is not None else []
    else:
        source_select = _query_source_select(source, root_ast)
    if source_select is None:
        return []
    return _query_select_column_lineage(
        data,
        source_select,
        normalized_column,
        root_ast,
        seen,
    )


def _query_expression_lineage(
    data: dict[str, list[dict[str, Any]]],
    expression: exp.Expression,
    select: exp.Select,
    root_ast: exp.Expression,
    seen: set[tuple[int, str]],
) -> list[tuple[str, str]]:
    if isinstance(expression, exp.Column):
        return _query_column_lineage(data, expression, select, root_ast, seen)
    if isinstance(expression, exp.AggFunc):
        # MAX/MIN of one physical column is a stable scalar for a duplicated
        # group path (the common CTE status-projection shape).  COUNT/SUM and
        # window values depend on cardinality and must remain opaque.
        if type(expression).__name__.upper() not in {"MAX", "MIN"}:
            return []
    elif isinstance(expression, (exp.Window, exp.Subquery)):
        return []
    columns = list(expression.find_all(exp.Column))
    if len(columns) != 1:
        return []
    return _query_column_lineage(data, columns[0], select, root_ast, seen)


def _query_select_column_lineage(
    data: dict[str, list[dict[str, Any]]],
    select: exp.Select,
    column_name: str,
    root_ast: exp.Expression,
    seen: set[tuple[int, str]],
) -> list[tuple[str, str]]:
    key = (id(select), _norm_name(column_name))
    if key in seen:
        return []
    seen.add(key)
    normalized_column = _norm_name(column_name)
    for item in select.expressions or ():
        expression = item.this if isinstance(item, exp.Alias) else item
        output_name = (
            _norm_name(item.alias)
            if isinstance(item, exp.Alias)
            else _norm_name(expression.name)
            if isinstance(expression, exp.Column)
            else ""
        )
        if output_name != normalized_column:
            continue
        return _query_expression_lineage(data, expression, select, root_ast, seen)

    # A SELECT * projection preserves the source column name.  Resolve it
    # only when exactly one source can provide that name; ambiguity stays
    # unresolved instead of manufacturing a join path.
    if any(isinstance(item, exp.Star) for item in select.expressions or ()):
        candidates: list[tuple[str, str]] = []
        for _alias, source in _query_block_sources(select):
            candidates.extend(
                _query_source_column_lineage(
                    data,
                    source,
                    normalized_column,
                    root_ast,
                    seen.copy(),
                )
            )
        unique = list(dict.fromkeys(candidates))
        if len(unique) == 1:
            return unique
    return []


def _query_column_lineage(
    data: dict[str, list[dict[str, Any]]],
    column: exp.Column,
    select: exp.Select,
    root_ast: exp.Expression,
    seen: set[tuple[int, str]],
) -> list[tuple[str, str]]:
    qualifier = _norm_name(column.table or "")
    sources = _query_block_sources(select)
    if qualifier:
        matching = [
            source
            for alias, source in sources
            if alias == qualifier
            or isinstance(source, exp.Table)
            and _norm_name(source.name) == qualifier
        ]
        if len(matching) != 1:
            return []
        return _query_source_column_lineage(
            data,
            matching[0],
            column.name,
            root_ast,
            seen.copy(),
        )

    candidates: list[tuple[str, str]] = []
    for _alias, source in sources:
        candidates.extend(
            _query_source_column_lineage(
                data,
                source,
                column.name,
                root_ast,
                seen.copy(),
            )
        )
    unique = list(dict.fromkeys(candidates))
    return unique if len(unique) == 1 else []


def _query_column_ref_in_data(
    data: dict[str, list[dict[str, Any]]],
    column: exp.Column,
    select: exp.Select,
    root_ast: exp.Expression,
) -> tuple[str, str] | None:
    lineage = _query_column_lineage(data, column, select, root_ast, set())
    return lineage[0] if len(lineage) == 1 and _actual_data_ref(data, lineage[0]) else None


def _query_block_equality_edges(
    data: dict[str, list[dict[str, Any]]],
    select: exp.Select,
    root_ast: exp.Expression,
) -> list[tuple[tuple[str, str], tuple[str, str]]]:
    edges: list[tuple[tuple[str, str], tuple[str, str]]] = []
    for join in select.args.get("joins") or ():
        on = join.args.get("on")
        if not isinstance(on, exp.Expression):
            continue
        equalities = [on] if isinstance(on, exp.EQ) else list(on.find_all(exp.EQ))
        for equality in equalities:
            left_column = _predicate_source_column(equality.left)
            right_column = _predicate_source_column(equality.right)
            if left_column is None or right_column is None:
                continue
            left = _query_column_ref_in_data(data, left_column, select, root_ast)
            right = _query_column_ref_in_data(data, right_column, select, root_ast)
            if left is None or right is None or left == right:
                continue
            pair = (left, right)
            if pair not in edges and (right, left) not in edges:
                edges.append(pair)
    return edges


def _query_structural_lineage_refs(
    data: dict[str, list[dict[str, Any]]],
    root_ast: exp.Expression,
) -> set[tuple[str, str]]:
    """Return physical cells that also control query-block topology."""
    refs: set[tuple[str, str]] = set()
    clause_keys = ("where", "group", "having", "order")
    selects = list(root_ast.find_all(exp.Select))
    if isinstance(root_ast, exp.Select) and root_ast not in selects:
        selects.append(root_ast)
    for select in selects:
        expressions: list[exp.Expression] = []
        for key in clause_keys:
            node = select.args.get(key)
            if isinstance(node, exp.Expression):
                expressions.append(node)
        for join in select.args.get("joins") or ():
            on = join.args.get("on")
            if isinstance(on, exp.Expression):
                expressions.append(on)
        for expression in expressions:
            for column in expression.find_all(exp.Column):
                resolved = _query_column_ref_in_data(data, column, select, root_ast)
                if resolved is not None:
                    refs.add(resolved)
    return refs


def _set_query_block_rich_predicates(
    data: dict[str, list[dict[str, Any]]],
    select: exp.Select,
    root_ast: exp.Expression,
    row_index: int = 0,
) -> None:
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
        if not isinstance(predicate, exp.Not) and predicate.find_ancestor(exp.Not) is not None:
            continue
        resolved = _rich_predicate_truth_value(predicate, True)
        if resolved is None:
            continue
        column, value = resolved
        ref = _query_column_ref_in_data(data, column, select, root_ast)
        actual = _actual_data_ref(data, ref) if ref else None
        if actual is None or row_index >= len(actual[0]):
            continue
        actual[0][row_index][actual[1]] = value


def _query_block_predicate_values(
    data: dict[str, list[dict[str, Any]]],
    select: exp.Select,
    root_ast: exp.Expression,
    row_index: int,
) -> dict[tuple[str, str], Any]:
    """Collect the exact physical values requested by a query-block filter.

    Reachability may use more than one source row.  Keeping these values per
    row is important: an outer CTE can need a negative boundary row while an
    inner join still needs a positive row to remain executable.  A single
    mutable ``preferred_values`` map would leak the last row's constraints to
    every equality component.
    """
    where = select.args.get("where")
    if not isinstance(where, exp.Where):
        return {}
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
    values: dict[tuple[str, str], Any] = {}
    for predicate in where.find_all(*predicate_types):
        if predicate.find_ancestor(exp.Select) is not select:
            continue
        if not isinstance(predicate, exp.Not) and predicate.find_ancestor(exp.Not) is not None:
            continue
        resolved = _rich_predicate_truth_value(predicate, True)
        if resolved is None:
            continue
        column, value = resolved
        ref = _query_column_ref_in_data(data, column, select, root_ast)
        actual = _actual_data_ref(data, ref) if ref else None
        if actual is None or row_index >= len(actual[0]):
            continue
        values[ref] = value
    return values


def _separate_strict_query_block_row_path(
    data: dict[str, list[dict[str, Any]]],
    root_ast: exp.Expression,
    source_ref: tuple[str, str],
    *,
    row_index: int,
) -> bool:
    """Separate a duplicate equality path used by a strict boundary probe."""
    selects = list(root_ast.find_all(exp.Select))
    if isinstance(root_ast, exp.Select) and root_ast not in selects:
        selects.append(root_ast)
    changed = False
    for select in reversed(selects):
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
        if not (has_lineage_source or is_cte_body or is_derived_body):
            continue
        edges = _query_block_equality_edges(data, select, root_ast)
        if not edges:
            continue
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
        for refs in components.values():
            if not any(ref[0] == source_ref[0] for ref in refs):
                continue
            actuals = [(_actual_data_ref(data, ref), ref) for ref in refs]
            if any(actual is None or row_index >= len(actual[0][0]) for actual, _ref in actuals):
                continue
            row_zero_values = [actual[0][0].get(actual[1]) for actual, _ref in actuals]
            row_values = [actual[0][row_index].get(actual[1]) for actual, _ref in actuals]
            if not row_zero_values or not row_values:
                continue
            # Separate only a component that currently collapses the two
            # logical paths. Existing distinct values already represent a
            # legitimate positive row and should not be rewritten.
            if any(value is None for value in row_zero_values + row_values):
                continue
            if len(set(map(repr, row_zero_values))) != 1 or len(set(map(repr, row_values))) != 1:
                continue
            if row_zero_values[0] != row_values[0]:
                continue
            variant = _strict_path_variant(row_values[0], row_index)
            with write_owner("materializer:strict_query_block_path_split"):
                for actual, _ref in actuals:
                    actual[0][row_index][actual[1]] = variant
            changed = True
    return changed


def _materialize_strict_scalar_count_paths(
    data: dict[str, list[dict[str, Any]]],
    standard_sql: str,
    ast_diffs: list[ASTDiffNode],
) -> bool:
    """Separate correlated COUNT(DISTINCT) output for a strict boundary path."""
    strict_diffs = [
        diff
        for diff in ast_diffs
        if diff.diff_type in {"comparison_operator_changed", "literal_changed"}
        and isinstance(
            _comparison_node_from_diff(
                diff.standard_node,
                diff.extra.get("standard_sql"),
            ),
            (exp.GT, exp.LT),
        )
    ]
    if not strict_diffs:
        return False
    root_ast = _parse_sql(standard_sql)
    if root_ast is None:
        return False
    changed = False
    for subquery in root_ast.find_all(exp.Subquery):
        inner = subquery.this
        outer = subquery.find_ancestor(exp.Select)
        if not isinstance(inner, exp.Select) or not isinstance(outer, exp.Select):
            continue
        aggregate = next((node for node in inner.find_all(exp.Count)), None)
        if aggregate is None:
            continue
        argument = aggregate.this
        if isinstance(argument, exp.Distinct):
            argument = argument.expressions[0] if argument.expressions else None
        if not isinstance(argument, exp.Column):
            continue
        count_ref = _query_column_ref_in_data(data, argument, inner, root_ast)
        count_actual = _actual_data_ref(data, count_ref) if count_ref else None
        if count_actual is None:
            continue

        correlation: tuple[tuple[str, str], exp.Column] | None = None
        where = inner.args.get("where")
        if not isinstance(where, exp.Where):
            continue
        for equality in where.find_all(exp.EQ):
            columns = list(equality.find_all(exp.Column))
            if len(columns) != 2:
                continue
            local_ref = next(
                (
                    _query_column_ref_in_data(data, column, inner, root_ast)
                    for column in columns
                    if _query_column_ref_in_data(data, column, inner, root_ast) is not None
                ),
                None,
            )
            outer_column = next(
                (
                    column
                    for column in columns
                    if _query_column_ref_in_data(data, column, inner, root_ast) is None
                ),
                None,
            )
            if local_ref is not None and isinstance(outer_column, exp.Column):
                correlation = (local_ref, outer_column)
                break
        if correlation is None:
            continue
        inner_key_ref, outer_column = correlation
        outer_key_ref = _query_column_ref_in_data(
            data,
            outer_column,
            outer,
            root_ast,
        )
        if outer_key_ref is None:
            continue
        outer_key_actual = _actual_data_ref(data, outer_key_ref)
        if outer_key_actual is None or len(outer_key_actual[0]) < 2:
            continue
        count_rows, count_column = count_actual
        key_actual = _actual_data_ref(data, inner_key_ref)
        if key_actual is None:
            continue
        key_rows, key_column = key_actual
        target_key = outer_key_actual[0][1].get(outer_key_actual[1])
        if target_key is None:
            continue
        boundary_index: int | None = None
        inner_sql_normalized = re.sub(r"\s+", "", _sql_of(inner)).lower()
        for strict_diff in strict_diffs:
            fragment = re.sub(
                r"\s+",
                "",
                str(strict_diff.extra.get("standard_sql") or ""),
            ).lower()
            if not fragment or fragment not in inner_sql_normalized:
                continue
            comparison = _comparison_node_from_diff(
                strict_diff.standard_node,
                strict_diff.extra.get("standard_sql"),
            )
            if not isinstance(comparison, (exp.GT, exp.LT)):
                continue
            source_column = _predicate_source_column(comparison.left)
            boundary = _static_predicate_scalar(comparison.right)
            if source_column is None or boundary is _MISSING:
                source_column = _predicate_source_column(comparison.right)
                boundary = _static_predicate_scalar(comparison.left)
            if source_column is None or boundary is _MISSING:
                continue
            boundary_ref = _query_column_ref_in_data(
                data,
                source_column,
                inner,
                root_ast,
            )
            boundary_actual = _actual_data_ref(data, boundary_ref) if boundary_ref else None
            if boundary_actual is None:
                continue
            boundary_index = next(
                (
                    index
                    for index, row in enumerate(boundary_actual[0])
                    if index < len(key_rows) and row.get(boundary_actual[1]) == boundary
                ),
                None,
            )
            if boundary_index is not None:
                break
        if boundary_index is not None:
            # Put the exact boundary row in the same correlated group as the
            # reachable outer row.  The strict standard query rejects it,
            # while the inclusive student mutation accepts it; a later row
            # above the boundary keeps both queries executable.
            with write_owner("materializer:strict_scalar_boundary_correlation"):
                key_rows[boundary_index][key_column] = target_key
            changed = True
            continue
        target_indexes = [
            index
            for index, row in enumerate(key_rows)
            if row.get(key_column) == target_key
        ]
        if not target_indexes:
            continue
        # There is already more than one distinct member for this projected
        # key; no write is needed. Otherwise reuse a non-target row while
        # keeping its own identity columns untouched.
        target_values = {
            count_rows[index].get(count_column)
            for index in target_indexes
            if index < len(count_rows)
        }
        if len(target_values) > 1:
            continue
        source_candidates = [
            index
            for index, row in enumerate(key_rows)
            if row.get(key_column) != target_key
            and index < len(count_rows)
        ]
        # Keep the two rows reserved by the strict boundary materializer
        # intact.  Reusing row 0 would remove the standard-side boundary
        # group; prefer a later spare row for the additional COUNT(DISTINCT)
        # member whenever the bounded table has one.
        source_index = next(
            (index for index in source_candidates if index >= 2),
            source_candidates[0] if source_candidates else None,
        )
        if source_index is None:
            continue
        current = count_rows[target_indexes[0]].get(count_column)
        replacement = _aggregate_distinct_probe_value(current, len(target_indexes))
        occupied = {
            row.get(count_column)
            for row in count_rows
            if row.get(key_column) == target_key
        }
        while replacement in occupied:
            replacement = _counter_value(count_column, replacement)
        with write_owner("materializer:strict_scalar_count_path"):
            key_rows[source_index][key_column] = target_key
            count_rows[source_index][count_column] = replacement
        changed = True
    return changed


def _materialize_query_block_comparison_boundaries(
    data: dict[str, list[dict[str, Any]]],
    standard_sql: str,
    student_sql: str,
    ast_diffs: list[ASTDiffNode],
) -> bool:
    """Write an exact scalar boundary after query-block paths are reachable.

    The generic comparison adapter can find ``2001`` in
    ``CAST(at.financial_aid_year AS UNSIGNED) > 2001`` but cannot know that
    the row must first pass three CTE joins and two LIKE predicates.  The
    reachability pass runs before this function; this final, narrow write then
    changes only the physical source cell and preserves the boundary witness.
    Opaque COUNT/window aliases intentionally remain unresolved.
    """
    standard_ast = _parse_sql(standard_sql)
    student_ast = _parse_sql(student_sql)
    if standard_ast is None or student_ast is None:
        return False
    comparison_types = (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE)
    changed = False
    boundary_cells: list[tuple[list[dict[str, Any]], str, Any]] = []
    strict_boundary_specs: list[dict[str, Any]] = []
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
        ):
            continue
        standard_select = _nearest_select(standard_comparison)
        student_select = _nearest_select(student_comparison)
        if not isinstance(standard_select, exp.Select) or not isinstance(
            student_select, exp.Select
        ):
            continue
        # This adapter is for scalar predicates whose source is hidden behind
        # a CTE/derived query block.  Aggregate/window/CASE outputs are not
        # writable physical cells, and direct top-level predicates already
        # have dedicated materializers.  Treating either category as a plain
        # column silently overwrites a valid aggregate boundary witness.
        if any(
            isinstance(node, (exp.AggFunc, exp.Window, exp.Case))
            for comparison in (standard_comparison, student_comparison)
            for node in (comparison.left, comparison.right)
            if isinstance(node, exp.Expression)
            for _nested in node.walk()
        ):
            continue
        standard_sources = _query_block_sources(standard_select)
        has_lineage_source = any(
            not isinstance(source, exp.Table)
            or _query_cte_select(standard_ast, source.name) is not None
            for _alias, source in standard_sources
        )
        if standard_select.find_ancestor(exp.Select) is None and not has_lineage_source:
            continue
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
        if standard_ref is None or student_ref != standard_ref:
            continue
        actual = _actual_data_ref(data, standard_ref)
        if actual is None or not actual[0]:
            continue
        # Only scalar boundary values are accepted.  In particular, do not
        # turn a date/function expression or a NULL comparison into a guessed
        # physical value here; those have dedicated materializers.
        boundary = standard_scalar
        if boundary is None or isinstance(boundary, bool):
            continue
        rows, column = actual
        with write_owner("materializer:query_block_comparison_boundary"):
            _set_query_block_rich_predicates(
                data,
                standard_select,
                standard_ast,
                0,
            )
            _set_query_block_rich_predicates(
                data,
                student_select,
                student_ast,
                0,
            )
            rows[0][column] = boundary
            boundary_cells.append((rows, column, boundary))
            if isinstance(standard_comparison, (exp.GT, exp.LT)):
                strict_boundary_specs.append({
                    "rows": rows,
                    "column": column,
                    "boundary": boundary,
                    "operator": type(standard_comparison),
                    "source_ref": standard_ref,
                    "comparison": standard_comparison,
                })
        changed = True
    if boundary_cells:
        # Predicate reachability writes can change a JOIN key after the exact
        # boundary is installed (for example TERM_CODE LIKE '%SU' followed by
        # ccso.TERM_CODE = at.TERM_CODE).  Re-align only equality paths, then
        # restore the saved boundary cells.  No predicate is re-applied here,
        # so this pass cannot move ``> c`` back to ``c + 1``.
        predicate_values: dict[tuple[str, str], Any] = {}
        all_selects = list(standard_ast.find_all(exp.Select))
        if isinstance(standard_ast, exp.Select) and standard_ast not in all_selects:
            all_selects.append(standard_ast)
        for select in all_selects:
            sources = _query_block_sources(select)
            has_lineage_source = any(
                not isinstance(source, exp.Table)
                or _query_cte_select(standard_ast, source.name) is not None
                for _alias, source in sources
            )
            is_cte_body = any(
                cte.this is select
                for cte in standard_ast.find_all(exp.CTE)
            )
            if not has_lineage_source and not is_cte_body and select is standard_ast:
                continue
            _set_query_block_rich_predicates(data, select, standard_ast, 0)
            where = select.args.get("where")
            if not isinstance(where, exp.Where):
                continue
            for predicate in where.find_all(
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
            ):
                if predicate.find_ancestor(exp.Select) is not select:
                    continue
                if not isinstance(predicate, exp.Not) and predicate.find_ancestor(exp.Not) is not None:
                    continue
                resolved = _rich_predicate_truth_value(predicate, True)
                if resolved is None:
                    continue
                column, value = resolved
                ref = _query_column_ref_in_data(data, column, select, standard_ast)
                if ref is not None and value is not None:
                    predicate_values[ref] = value

        for select in reversed(all_selects):
            sources = _query_block_sources(select)
            has_lineage_source = any(
                not isinstance(source, exp.Table)
                or _query_cte_select(standard_ast, source.name) is not None
                for _alias, source in sources
            )
            if not has_lineage_source:
                continue
            edges = _query_block_equality_edges(data, select, standard_ast)
            if not edges:
                continue
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
            with write_owner("materializer:query_block_boundary_join_alignment"):
                for refs in components.values():
                    values: list[Any] = []
                    for ref in refs:
                        actual = _actual_data_ref(data, ref)
                        if actual is not None and actual[0]:
                            values.append(actual[0][0].get(actual[1]))
                    anchor = next(
                        (
                            predicate_values[ref]
                            for ref in refs
                            if ref in predicate_values
                        ),
                        None,
                    )
                    if anchor is None:
                        anchor = next((value for value in values if value is not None), None)
                    if anchor is None:
                        continue
                    for ref in refs:
                        actual = _actual_data_ref(data, ref)
                        if actual is not None and actual[0]:
                            actual[0][0][actual[1]] = anchor
        # A strict comparison needs both sides of the boundary.  The generic
        # predicate pass can make row 0 and row 1 share a textual JOIN key
        # (for example two ``...SU`` term rows), which lets one source row
        # join to both term records and erases the intended >c/<c split.  For
        # a block that contains the boundary's source table, separate only an
        # otherwise identical row-1 equality component.  The variant keeps
        # the common LIKE suffix/prefix shape and is applied to both JOIN
        # endpoints, so this remains a real path rather than an arbitrary
        # source-cell edit.
        for spec in strict_boundary_specs:
            rows = spec["rows"]
            column = spec["column"]
            boundary = spec["boundary"]
            source_ref = spec["source_ref"]
            operator = spec["operator"]
            comparison = spec.get("comparison")
            # In an EXISTS body, one exact boundary row is sufficient.  An
            # extra ``boundary + 1`` row can satisfy the EXISTS for both
            # versions and leak an otherwise unrelated outer tuple into the
            # standard result, especially when the query also has NOT IN or
            # scalar-count guards.  Keep the second strict-side row for
            # ordinary scalar query blocks, where it is useful to show the
            # full three-valued boundary path.
            comparison_in_exists = bool(
                isinstance(comparison, exp.Expression)
                and any(
                    _sql_of(candidate) == _sql_of(comparison)
                    and candidate.find_ancestor(exp.Exists) is not None
                    for candidate in standard_ast.find_all(
                        exp.EQ,
                        exp.NEQ,
                        exp.GT,
                        exp.GTE,
                        exp.LT,
                        exp.LTE,
                    )
                )
            )
            if (
                not comparison_in_exists
                and len(rows) > 1
                and isinstance(boundary, (int, float, Decimal))
                and not isinstance(boundary, bool)
            ):
                rows[1][column] = boundary + 1 if operator is exp.GT else boundary - 1
                _separate_strict_query_block_row_path(
                    data,
                    standard_ast,
                    source_ref,
                    row_index=1,
                )
        for rows, column, boundary in boundary_cells:
            if rows:
                rows[0][column] = boundary
    return changed


def _quoted_unresolved_identifier_value(
    data: dict[str, list[dict[str, Any]]],
    node: exp.Expression,
    select: exp.Select,
) -> Any:
    """Return SQLite's double-quoted-string fallback value when unambiguous."""
    if not isinstance(node, exp.Column) or node.table:
        return _MISSING
    identifier = node.this
    if not isinstance(identifier, exp.Identifier) or not identifier.args.get(
        "quoted"
    ):
        return _MISSING
    if _column_ref_in_select_data(data, node, select) is not None:
        return _MISSING
    return str(node.name)


def _predicate_scalar_value(
    data: dict[str, list[dict[str, Any]]],
    node: exp.Expression,
    select: exp.Select,
) -> Any:
    if isinstance(node, exp.Literal):
        return _literal_value(node)
    return _quoted_unresolved_identifier_value(data, node, select)


def _select_local_scalar_predicates(
    data: dict[str, list[dict[str, Any]]],
    select: exp.Select,
) -> list[tuple[list[dict[str, Any]], str, Any, Any]]:
    where = select.args.get("where")
    if not isinstance(where, exp.Where):
        return []
    bindings: list[tuple[list[dict[str, Any]], str, Any, Any]] = []
    for comparison in where.find_all(
        exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE
    ):
        if comparison.find_ancestor(exp.Select) is not select:
            continue
        left_ref = (
            _column_ref_in_select_data(data, comparison.left, select)
            if isinstance(comparison.left, exp.Column)
            else None
        )
        right_ref = (
            _column_ref_in_select_data(data, comparison.right, select)
            if isinstance(comparison.right, exp.Column)
            else None
        )
        left_scalar = _predicate_scalar_value(data, comparison.left, select)
        right_scalar = _predicate_scalar_value(data, comparison.right, select)
        if left_ref is not None and right_scalar is not _MISSING:
            ref = left_ref
            scalar = right_scalar
            column_on_left = True
        elif right_ref is not None and left_scalar is not _MISSING:
            ref = right_ref
            scalar = left_scalar
            column_on_left = False
        else:
            continue
        actual = _actual_data_ref(data, ref)
        if actual is None:
            continue
        rows, column = actual
        values = _scalar_predicate_values(
            comparison,
            scalar,
            column,
            column_on_left=column_on_left,
        )
        if values is not None:
            bindings.append((rows, column, values[0], values[1]))
    return bindings


def _apply_same_table_correlated_aggregate_probe(
    data: dict[str, list[dict[str, Any]]],
    standard_sql: str,
    student_sql: str,
) -> None:
    """Create repeated correlation groups with rows below, at and above AVG."""
    for sql in (standard_sql, student_sql):
        ast = _parse_sql(sql)
        if not ast:
            continue
        for subquery in ast.find_all(exp.Subquery):
            inner_select = subquery.this if isinstance(subquery.this, exp.Select) else None
            outer_select = subquery.find_ancestor(exp.Select)
            aggregate = next(
                (subquery.find(kind) for kind in (exp.Avg, exp.Max, exp.Min, exp.Sum) if subquery.find(kind)),
                None,
            )
            if not isinstance(inner_select, exp.Select) or not isinstance(outer_select, exp.Select) or not aggregate:
                continue
            correlation = next(
                (
                    comparison for comparison in inner_select.find_all(exp.EQ)
                    if isinstance(comparison.left, exp.Column)
                    and isinstance(comparison.right, exp.Column)
                ),
                None,
            )
            if not correlation:
                continue
            left_ref = _column_ref_in_select(correlation.left, inner_select)
            right_inner_ref = _column_ref_in_select(correlation.right, inner_select)
            if left_ref and not right_inner_ref:
                inner_key_ref = left_ref
                outer_key_ref = _column_ref_in_select(correlation.right, outer_select)
            elif right_inner_ref and not left_ref:
                inner_key_ref = right_inner_ref
                outer_key_ref = _column_ref_in_select(correlation.left, outer_select)
            else:
                continue
            measure_column_node = aggregate.find(exp.Column)
            measure_ref = _column_ref_in_select(measure_column_node, inner_select) if isinstance(measure_column_node, exp.Column) else None
            if not inner_key_ref or not outer_key_ref or not measure_ref:
                continue
            if inner_key_ref[0] != outer_key_ref[0] or inner_key_ref[0] != measure_ref[0]:
                continue
            key_actual = _actual_data_ref(data, inner_key_ref)
            measure_actual = _actual_data_ref(data, measure_ref)
            if not key_actual or not measure_actual:
                continue
            rows, key_column = key_actual
            measure_rows, measure_column = measure_actual
            if rows is not measure_rows or len(rows) < 3:
                continue
            first_key = rows[0][key_column]
            multiplier = next(
                (
                    float(_literal_value(literal))
                    for mul in subquery.find_all(exp.Mul)
                    for literal in (mul.left, mul.right)
                    if isinstance(literal, exp.Literal)
                    and isinstance(_literal_value(literal), (int, float, Decimal))
                ),
                None,
            )
            offset = next(
                (
                    float(_literal_value(literal))
                    for add in subquery.find_all(exp.Add)
                    for literal in (add.left, add.right)
                    if isinstance(literal, exp.Literal)
                    and isinstance(_literal_value(literal), (int, float, Decimal))
                ),
                None,
            )
            if isinstance(aggregate, exp.Sum) and multiplier == 0.5:
                # Two equal rows make each outer value exactly 0.5 * SUM.
                values = (10, 10)
            elif isinstance(aggregate, (exp.Max, exp.Min)) and offset:
                # Two rows equal to the offset make SUM(rows) == MAX(row)+offset.
                values = (offset, offset)
            else:
                # AVG=15 exactly; MIN/MAX also retain an extreme and a non-extreme row.
                values = (10, 20, 15)
            for index, value in enumerate(values):
                rows[index][key_column] = first_key
                rows[index][measure_column] = value
            for index, row in enumerate(rows[len(values):], start=len(values)):
                if row[key_column] != first_key:
                    continue
                if isinstance(first_key, (int, float, Decimal)):
                    row[key_column] = first_key + 1000 + index
                elif isinstance(first_key, str) and re.match(r"^\d{4}-\d{2}-\d{2}", first_key):
                    row[key_column] = f"2030-01-{(index % 28) + 1:02d}"
                else:
                    row[key_column] = f"__corr_other_{index}__"
            if len(rows) >= 5:
                second_key = rows[3][key_column]
                for index in (3, 4):
                    rows[index][key_column] = second_key
                    rows[index][measure_column] = 40
            return


def _align_having_membership_keys(
    data: dict[str, list[dict[str, Any]]],
    standard_sql: str,
    student_sql: str,
) -> None:
    """Keep HAVING subquery groups reachable through an outer IN predicate."""
    for sql in (standard_sql, student_sql):
        ast = _parse_sql(sql)
        if not ast:
            continue
        aliases = _table_aliases(ast)
        for in_node in ast.find_all(exp.In):
            query = in_node.args.get("query")
            inner = query.this if isinstance(query, exp.Subquery) else None
            if not isinstance(in_node.this, exp.Column) or not isinstance(inner, exp.Select):
                continue
            if not inner.args.get("having") or not isinstance(inner.args.get("group"), exp.Group):
                continue
            group = inner.args["group"]
            group_column = next((item for item in group.expressions if isinstance(item, exp.Column)), None)
            inner_source = _direct_from_table(inner)
            outer_select = _nearest_select(in_node)
            outer_source = _direct_from_table(outer_select)
            if not group_column or not inner_source or not outer_source:
                continue
            if _norm_name(inner_source.name) == _norm_name(outer_source.name):
                continue
            inner_name = aliases.get(_norm_name(inner_source.alias_or_name), _norm_name(inner_source.name))
            outer_name = aliases.get(_norm_name(outer_source.alias_or_name), _norm_name(outer_source.name))
            inner_table = next((name for name in data if _norm_name(name) == inner_name), None)
            outer_table = next((name for name in data if _norm_name(name) == outer_name), None)
            if not inner_table or not outer_table or not data[inner_table] or not data[outer_table]:
                continue
            inner_col = _column_lookup(list(data[inner_table][0])).get(_norm_name(group_column.name))
            outer_col = _column_lookup(list(data[outer_table][0])).get(_norm_name(in_node.this.name))
            if not inner_col or not outer_col:
                continue
            member_values = list(dict.fromkeys(row.get(inner_col) for row in data[inner_table] if row.get(inner_col) is not None))
            for index, value in enumerate(member_values[: len(data[outer_table])]):
                data[outer_table][index][outer_col] = value


def _catalog_column_schema(
    table: str,
    column: str,
    schema_catalog: SchemaCatalog | None,
) -> ColumnSchema | None:
    """Resolve a physical column without falling back to SQL-name guesses."""
    if schema_catalog is None:
        return None
    table_schema = schema_catalog.table(table)
    if table_schema is None:
        return None
    normalized = _norm_name(column)
    return next(
        (
            item
            for key, item in table_schema.columns.items()
            if _norm_name(key) == normalized or _norm_name(item.name) == normalized
        ),
        None,
    )


def _authoritative_column_kind(
    table: str,
    column: str,
    schema_catalog: SchemaCatalog | None,
) -> str | None:
    """Return the declared value family, or None for legacy heuristics."""
    column_schema = _catalog_column_schema(table, column, schema_catalog)
    if column_schema is None or not column_schema.has_explicit_type:
        return None
    declared = str(column_schema.data_type or "").upper()
    if any(token in declared for token in ("DATE", "TIMESTAMP")):
        return "date"
    if "TIME" in declared:
        return "time"
    affinity = _sqlite_declared_affinity(column, declared)
    if affinity in {"INTEGER", "REAL", "NUMERIC"}:
        return "numeric"
    if affinity == "TEXT":
        return "text"
    return None


def _repair_numeric_column_types(
    data: dict[str, list[dict[str, Any]]],
    *,
    schema_catalog: SchemaCatalog | None = None,
) -> None:
    """Remove obvious AST artefacts from columns used as numeric measures."""
    for table, rows in data.items():
        for index, row in enumerate(rows):
            for column, value in list(row.items()):
                # Date-like names such as ``order_date`` and ``view_date``
                # also contain broad numeric hints. SQLite typing and seed
                # generation already give dates precedence; final repair must
                # preserve that same type decision.
                declared_kind = _authoritative_column_kind(
                    table,
                    column,
                    schema_catalog,
                )
                if declared_kind == "text":
                    if value is not None and not isinstance(value, str):
                        row[column] = str(value)
                    continue
                if declared_kind in {"date", "time"}:
                    continue
                if declared_kind != "numeric" and (
                    _is_date_column(column) or not _is_numeric_column(column)
                ):
                    continue
                if value is None or isinstance(value, (int, float, Decimal)):
                    continue
                seed = _seed_value(column, index)
                row[column] = (
                    _coerce_typed_seed(seed, "numeric", column, index)
                    if declared_kind == "numeric"
                    else seed
                )


def _stabilize_filtered_aggregate_witness(
    data: dict[str, list[dict[str, Any]]],
    standard_sql: str,
    student_sql: str,
) -> None:
    """Re-apply the filtered/global AVG witness after broad probes.

    Aggregate probes are intentionally late-bound: generic predicate and
    membership compatibility probes may touch the same measure column.  The
    final pass restores the declared semantic witness without expanding the
    database.
    """
    if not ("AVG(" in standard_sql.upper() and "AVG(" in student_sql.upper() and "WHERE" in student_sql.upper() and "WHERE" not in standard_sql.upper().split("AVG(", 1)[-1]):
        return
    for table, rows in data.items():
        if not rows:
            continue
        lookup = _column_lookup(list(rows[0]))
        measure = next((lookup[key] for key in ("credits", "salary", "amount", "score") if key in lookup), None)
        category = next((lookup[key] for key in ("dept", "department", "dept_name") if key in lookup), None)
        if not measure or not category:
            continue
        filter_value = "CS"
        for index, row in enumerate(rows):
            if index < 2:
                row[category] = filter_value
                row[measure] = 10 + index * 10
            else:
                row[category] = "not_CS"
                row[measure] = 90 + index
        return


def _stabilize_having_sum_boundary(
    data: dict[str, list[dict[str, Any]]],
    standard_sql: str,
    student_sql: str,
) -> None:
    if "HAVING" not in standard_sql.upper() or "SUM(" not in standard_sql.upper():
        return
    for rows in data.values():
        if not rows:
            continue
        lookup = _column_lookup(list(rows[0]))
        group = lookup.get("customerid") or lookup.get("customer_id")
        date = lookup.get("orderdate") or lookup.get("order_date")
        amount = lookup.get("totalamount") or lookup.get("total_amount")
        if not group or not date or not amount:
            continue
        for index, row in enumerate(rows):
            row[group] = 1
            row[date] = "2023-01-01"
            row[amount] = 500 if index == 0 else 0
        return


def _stabilize_same_table_correlated_avg_witness(
    data: dict[str, list[dict[str, Any]]],
    standard_sql: str,
    student_sql: str,
) -> None:
    if "AVG(" not in standard_sql.upper() or "CUSTOMER_ID" not in standard_sql.upper():
        return
    for rows in data.values():
        if len(rows) < 3:
            continue
        lookup = _column_lookup(list(rows[0]))
        key, amount, ident = lookup.get("customer_id"), lookup.get("purch_amt"), lookup.get("id")
        if not key or not amount or not ident:
            continue
        assignments = [(1, 10), (1, 20), (1, 30)]
        for index, (group, value) in enumerate(assignments):
            rows[index][key] = group
            rows[index][amount] = value
            rows[index][ident] = index + 1
        return


def _stabilize_nested_membership_witness(
    data: dict[str, list[dict[str, Any]]],
    standard_sql: str,
    student_sql: str,
) -> None:
    """Materialize one reachable path for each terminal IN literal.

    Generic membership probes distribute values locally, but a deep chain is
    a join-like path: a value must survive every projection boundary.  Build
    bounded paths for both SQL variants in the same world so a US/CA change
    leaves a positive result on each side.
    """
    for path_index, sql in enumerate((standard_sql, student_sql)):
        ast = _parse_sql(sql)
        if not ast:
            continue
        terminal = next(
            (node for node in ast.find_all(exp.EQ)
             if isinstance(node.left, exp.Column) and isinstance(node.right, exp.Literal)),
            None,
        )
        links: list[tuple[tuple[str, str], tuple[str, str]]] = []
        for node in ast.find_all(exp.In):
            query = node.args.get("query")
            inner = query.this if isinstance(query, exp.Subquery) else None
            outer = node.find_ancestor(exp.Select)
            if not isinstance(node.this, exp.Column) or not isinstance(inner, exp.Select):
                continue
            projected = inner.expressions[0] if inner.expressions else None
            projected = projected.this if isinstance(projected, exp.Alias) else projected
            if not isinstance(projected, exp.Column) or not isinstance(outer, exp.Select):
                continue
            outer_ref = _column_ref_in_select(node.this, outer)
            inner_ref = _column_ref_in_select(projected, inner)
            if outer_ref and inner_ref:
                links.append((outer_ref, inner_ref))
        if not terminal or not links:
            continue
        terminal_ref = _column_ref_in_select(terminal.left, terminal.find_ancestor(exp.Select))
        if not terminal_ref:
            continue
        token = 9100 + path_index
        refs = [ref for pair in links for ref in pair] + [terminal_ref]
        for table_ref, column_ref in refs:
            rows = next((items for name, items in data.items() if _norm_name(name) == table_ref), None)
            if not rows or path_index >= len(rows):
                continue
            actual = next((name for name in rows[0] if _norm_name(name) == column_ref), None)
            if actual:
                rows[path_index][actual] = (
                    _literal_value(terminal.right) if (table_ref, column_ref) == terminal_ref else token
                )


def _stabilize_exists_duplicate_projection_witness(
    data: dict[str, list[dict[str, Any]]],
    standard_sql: str,
    student_sql: str,
) -> None:
    """Expose EXISTS-vs-DISTINCT-JOIN differences with duplicate names."""
    if "EXISTS" not in standard_sql.upper() or "DISTINCT" not in student_sql.upper():
        return
    students = next((rows for name, rows in data.items() if _norm_name(name) == "student"), None)
    takes = next((rows for name, rows in data.items() if _norm_name(name) == "takes"), None)
    if not students or not takes or len(students) < 2 or len(takes) < 2:
        return
    student_id = _column_lookup(list(students[0])).get("id")
    student_name = _column_lookup(list(students[0])).get("name")
    takes_id = _column_lookup(list(takes[0])).get("id")
    grade = _column_lookup(list(takes[0])).get("grade")
    if not all((student_id, student_name, takes_id, grade)):
        return
    students[0][student_id], students[1][student_id] = 1, 2
    students[0][student_name] = students[1][student_name] = "Same Name"
    takes[0][takes_id], takes[1][takes_id] = 1, 2
    takes[0][grade] = takes[1][grade] = "A"


def _linear_arithmetic_form(
    node: exp.Expression | None,
    expected_column: str = "",
) -> tuple[Any, Any] | None:
    """Return ``(coefficient, offset)`` for a small linear SQL expression.

    The data generator only needs the deliberately small expression language
    used by the boundary corpus: one column combined with numeric literals by
    ``+``, ``-``, ``*`` or ``/`` (including unary minus).  Expressions with
    multiple columns, non-constant divisors, or function calls are left to the
    existing generic probes.
    """
    if node is None:
        return None
    columns = {
        _norm_name(column.name)
        for column in node.find_all(exp.Column)
        if isinstance(column, exp.Column)
    }
    if len(columns) > 1 or (expected_column and columns and expected_column not in columns):
        return None

    def walk(item: exp.Expression) -> tuple[Any, Any] | None:
        if isinstance(item, exp.Column):
            return 1, 0
        if isinstance(item, exp.Literal) and item.is_number:
            value = _literal_value(item)
            return (0, value) if isinstance(value, (int, float, Decimal)) else None
        if isinstance(item, exp.Paren):
            return walk(item.this)
        if isinstance(item, exp.Neg):
            result = walk(item.this)
            return (-result[0], -result[1]) if result is not None else None
        if isinstance(item, (exp.Add, exp.Sub)):
            left = walk(item.left)
            right = walk(item.right)
            if left is None or right is None:
                return None
            sign = -1 if isinstance(item, exp.Sub) else 1
            return left[0] + sign * right[0], left[1] + sign * right[1]
        if isinstance(item, exp.Mul):
            left = walk(item.left)
            right = walk(item.right)
            if left is None or right is None:
                return None
            if left[0] and right[0]:
                return None
            if right[0] == 0:
                return left[0] * right[1], left[1] * right[1]
            return right[0] * left[1], right[1] * left[1]
        if isinstance(item, exp.Div):
            left = walk(item.left)
            right = walk(item.right)
            if left is None or right is None or right[0] != 0 or right[1] == 0:
                return None
            return left[0] / right[1], left[1] / right[1]
        return None

    result = walk(node)
    if result is None:
        return None
    if not columns and result[0] == 0:
        return None
    return result


def _evaluate_arithmetic_comparison(
    expression: exp.Expression,
    value: Any,
    literal: Any,
    operator: str,
) -> bool:
    linear = _linear_arithmetic_form(expression)
    if linear is None:
        return False
    evaluated = linear[0] * value + linear[1]
    if operator == "GT":
        return evaluated > literal
    if operator == "GTE":
        return evaluated >= literal
    if operator == "LT":
        return evaluated < literal
    if operator == "LTE":
        return evaluated <= literal
    if operator == "EQ":
        return evaluated == literal
    if operator == "NEQ":
        return evaluated != literal
    return False


def _apply_expression_probes(
    rows: list[dict[str, Any]],
    columns: list[str],
    table_name: str,
    standard_sql: str,
    student_sql: str,
) -> None:
    if not rows:
        return
    lookup = _column_lookup(columns)
    asts = [_parse_sql(standard_sql), _parse_sql(student_sql)]

    for ast in asts:
        if not ast:
            continue
        for comparison in ast.find_all(exp.NullSafeEQ, exp.NullSafeNEQ):
            column = comparison.left if isinstance(comparison.left, exp.Column) else comparison.right
            if isinstance(column, exp.Column) and _norm_name(column.name) in lookup:
                rows[-1][lookup[_norm_name(column.name)]] = None

        for coalesce in ast.find_all(exp.Coalesce):
            args = [coalesce.this, *(coalesce.expressions or [])]
            first = args[0] if args else None
            if isinstance(first, exp.Column) and _norm_name(first.name) in lookup:
                rows[0][lookup[_norm_name(first.name)]] = None
                if len(args) > 1 and isinstance(args[1], exp.Column) and _norm_name(args[1].name) in lookup:
                    rows[0][lookup[_norm_name(args[1].name)]] = "coalesce_fallback"

        for node_type, value in ((exp.Abs, -3), (exp.Round, 1.25), (exp.Trim, " Alice ")):
            for function in ast.find_all(node_type):
                column = function.find(exp.Column)
                if column and _norm_name(column.name) in lookup:
                    rows[0][lookup[_norm_name(column.name)]] = value

        for cast in ast.find_all(exp.Cast):
            column = cast.find(exp.Column)
            if column and _norm_name(column.name) in lookup:
                rows[0][lookup[_norm_name(column.name)]] = 3.5

    # Arithmetic predicates need a value derived from the expression rather
    # than the raw literal boundary.  For example, ``credits * 2 > 600`` and
    # ``credits + 2 > 600`` only differ around credits=301; the generic
    # literal-constraint probe cannot see that boundary because neither side
    # is a bare column.  Solve the small linear expression forms supported by
    # the teaching corpus and choose a value for which the two predicates have
    # different truth values.
    arithmetic_candidates: dict[str, set[Any]] = defaultdict(set)
    arithmetic_comparisons: list[tuple[str, exp.Expression, Any, str, int]] = []
    for ast_index, ast in enumerate(asts):
        if not ast:
            continue
        aliases = _table_aliases(ast)
        for comparison in ast.find_all(exp.GT, exp.GTE, exp.LT, exp.LTE, exp.EQ, exp.NEQ):
            left, right = comparison.left, comparison.right
            expression_on_left = _linear_arithmetic_form(left, "") is not None
            expression = left if expression_on_left else right
            literal_node = right if expression is left else left
            if not isinstance(literal_node, exp.Literal):
                continue
            literal = _literal_value(literal_node)
            if not isinstance(literal, (int, float, Decimal)):
                continue
            column = expression.find(exp.Column) if isinstance(expression, exp.Expression) else None
            if not isinstance(column, exp.Column):
                continue
            column_name = _norm_name(column.name)
            if column_name not in lookup:
                continue
            table_ref = _norm_name(column.table or "")
            resolved_table = aliases.get(table_ref, table_ref)
            if resolved_table and resolved_table != _norm_name(table_name):
                continue
            operator = type(comparison).__name__.upper()
            if not expression_on_left:
                operator = {
                    "GT": "LT",
                    "GTE": "LTE",
                    "LT": "GT",
                    "LTE": "GTE",
                }.get(operator, operator)
            arithmetic_comparisons.append((column_name, expression, literal, operator, ast_index))
            linear = _linear_arithmetic_form(expression, column_name)
            if linear is not None:
                coefficient, offset = linear
                if coefficient:
                    boundary = (literal - offset) / coefficient
                    for candidate in (boundary - 2, boundary - 1, boundary, boundary + 1, boundary + 2):
                        if isinstance(candidate, float) and candidate.is_integer():
                            candidate = int(candidate)
                        arithmetic_candidates[column_name].add(candidate)

    if arithmetic_comparisons:
        for column_name, expression, literal, _operator, _ast_index in arithmetic_comparisons:
            arithmetic_candidates[column_name].update({-1, 0, 1, 2, 3, 10, 100, 1000})

        grouped: dict[str, list[tuple[exp.Expression, Any, str, int]]] = defaultdict(list)
        for column_name, expression, literal, operator, ast_index in arithmetic_comparisons:
            grouped[column_name].append((expression, literal, operator, ast_index))

        for column_name, candidates in arithmetic_candidates.items():
            chosen = next(
                (
                    candidate
                    for candidate in sorted(candidates, key=lambda value: float(value))
                    if any(
                        _evaluate_arithmetic_comparison(expression, candidate, literal, operator)
                        != _evaluate_arithmetic_comparison(other_expression, candidate, other_literal, other_operator)
                        for expression, literal, operator, ast_index in grouped[column_name]
                        for other_expression, other_literal, other_operator, other_ast_index in grouped[column_name]
                        if ast_index != other_ast_index
                    )
                ),
                None,
            )
            if chosen is not None:
                rows[0][lookup[column_name]] = chosen

    patterns: list[tuple[str, str]] = []
    for ast in asts:
        if not ast:
            continue
        for like in ast.find_all(exp.Like):
            if isinstance(like.this, exp.Column) and isinstance(like.expression, exp.Literal):
                patterns.append((like.this.name, str(_literal_value(like.expression))))
    if any("_" in pattern for _, pattern in patterns) and any("%" in pattern for _, pattern in patterns):
        column_name, pattern = next((item for item in patterns if "%" in item[1]), patterns[0])
        actual = lookup.get(_norm_name(column_name))
        if actual:
            rows[0][actual] = f"{pattern.split('%', 1)[0]}Long"

def _apply_join_semantic_probes(
    data: dict[str, list[dict[str, Any]]],
    standard_sql: str,
    student_sql: str,
) -> None:
    combined = f"{standard_sql}\n{student_sql}"

    if re.search(r"(?is)\bemployee\s+\w+\s+JOIN\s+employee\b", combined):
        rows = data.get("employee") or []
        if rows and {"id", "manager_id"}.issubset(rows[0]):
            ids = [row["id"] for row in rows]
            for idx, row in enumerate(rows):
                row["manager_id"] = ids[(idx + 1) % len(ids)] if idx % 2 == 0 else max(ids) + 1000 + idx

    if re.search(r"(?is)\bON\b[^;]+\bAND\b", standard_sql) and not re.search(r"(?is)\bON\b[^;]+\bAND\b", student_sql):
        standard_ast = _parse_sql(standard_sql)
        for join in list(standard_ast.find_all(exp.Join)) if standard_ast else []:
            on = join.args.get("on")
            if not isinstance(on, exp.And):
                continue
            comparisons = list(on.find_all(exp.EQ))
            if len(comparisons) < 2:
                continue
            second = comparisons[1]
            if not isinstance(second.right, exp.Column):
                continue
            aliases = _table_aliases(standard_ast)
            table_name = aliases.get(_norm_name(second.right.table), _norm_name(second.right.table))
            rows = next((value for key, value in data.items() if _norm_name(key) == table_name), [])
            if rows:
                actual = _column_lookup(rows[0].keys()).get(_norm_name(second.right.name))
                if actual:
                    value = rows[0][actual]
                    rows[0][actual] = value + 1000 if isinstance(value, (int, float, Decimal)) else f"mismatch_{value}"


def _apply_not_in_null_probe(
    data: dict[str, list[dict[str, Any]]],
    standard_sql: str,
    student_sql: str,
    ast_diffs: list[ASTDiffNode] | None = None,
) -> None:
    # A NULL in the subquery makes every NOT IN predicate UNKNOWN.  That is a
    # useful probe for NOT IN's three-valued logic, but it would also erase the
    # rows needed to observe an independent SELECT DISTINCT difference.  Let
    # the dedicated duplicate projection probe own that narrow case.
    if ast_diffs and all(
        diff.diff_type in {"distinct_changed", "aggregate_distinct_changed"}
        for diff in ast_diffs
    ):
        return
    for sql in (standard_sql, student_sql):
        if not re.search(r"(?is)\bNOT\s+IN\s*\(\s*SELECT\b", sql):
            continue
        ast = _parse_sql(sql)
        if not ast:
            continue
        for in_node in ast.find_all(exp.In):
            if not isinstance(in_node.parent, exp.Not):
                continue
            query = in_node.args.get("query")
            selected = query.find(exp.Column) if isinstance(query, exp.Expression) else None
            table = query.find(exp.Table) if isinstance(query, exp.Expression) else None
            if not selected or not table:
                continue
            rows = next((value for key, value in data.items() if _norm_name(key) == _norm_name(table.name)), [])
            if rows:
                actual = _column_lookup(rows[0].keys()).get(_norm_name(selected.name))
                if actual:
                    rows[0][actual] = None
                    # The NULL must survive the subquery's own filter.  In
                    # ``SELECT id FROM majors WHERE inactive_at IS NULL``,
                    # setting only ``id`` to NULL is ineffective if the same
                    # row still has a non-NULL ``inactive_at`` value.
                    aliases = _table_aliases(query)
                    for null_check in query.find_all(exp.Is):
                        if not isinstance(null_check.expression, exp.Null):
                            continue
                        if isinstance(null_check.parent, exp.Not):
                            continue
                        filter_column = null_check.this
                        if not isinstance(filter_column, exp.Column):
                            continue
                        table_ref = _norm_name(filter_column.table or "")
                        resolved_table = aliases.get(table_ref, table_ref)
                        if resolved_table and resolved_table != _norm_name(table.name):
                            continue
                        filter_actual = _column_lookup(rows[0].keys()).get(
                            _norm_name(filter_column.name)
                        )
                        if filter_actual:
                            rows[0][filter_actual] = None
                    outer_column = in_node.this if isinstance(in_node.this, exp.Column) else None
                    outer_select = in_node.find_ancestor(exp.Select)
                    outer_table = outer_select.find(exp.Table) if outer_select else None
                    outer_rows = next(
                        (
                            value for key, value in data.items()
                            if outer_table and _norm_name(key) == _norm_name(outer_table.name)
                        ),
                        [],
                    )
                    if len(rows) > 1 and outer_rows and outer_column:
                        outer_actual = _column_lookup(outer_rows[0].keys()).get(_norm_name(outer_column.name))
                        if outer_actual:
                            # Keep the NULL member to exercise SQL's three-valued
                            # NOT IN semantics, but also retain observable rows on
                            # the anti-join side.  Without this, a generated inner
                            # relation containing every outer key makes NOT IN
                            # UNKNOWN for every row and masks unrelated DISTINCT
                            # differences as two empty result sets.
                            inner_values = {
                                row.get(actual)
                                for row in rows
                                if row.get(actual) is not None
                            }
                            seed = outer_rows[0].get(outer_actual)
                            unmatched = _counter_value(outer_actual, seed)
                            while unmatched in inner_values or unmatched is None:
                                unmatched = _counter_value(outer_actual, unmatched)

                            # Use the first two rows when available so the later
                            # duplicate-projection probe can expose missing
                            # SELECT DISTINCT. Keep a later row matched whenever
                            # possible so NOT IN/anti-join tests still exercise a
                            # positive membership boundary.
                            anti_count = min(2, len(outer_rows))
                            for outer_row in outer_rows[:anti_count]:
                                outer_row[outer_actual] = unmatched
                            if len(outer_rows) > anti_count:
                                rows[1][actual] = outer_rows[anti_count][outer_actual]


def _direct_scope_tables_and_qualifiers(
    ast: exp.Expression,
    descriptors_by_node: dict[int, _Phase1ScopeDescriptor],
) -> tuple[
    dict[str, list[exp.Table]],
    dict[str, dict[str, int]],
]:
    tables: dict[str, list[exp.Table]] = defaultdict(list)
    qualifiers: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    bounded_nodes = []
    for index, node in enumerate(ast.walk()):
        if index >= _MAX_SCOPE_AST_NODES_SCANNED:
            break
        bounded_nodes.append(node)
    for table in (node for node in bounded_nodes if isinstance(node, exp.Table)):
        descriptor = _nearest_retained_scope(table, descriptors_by_node)
        if descriptor is None:
            continue
        tables[descriptor.scope_id].append(table)
        qualifier = _norm_name(table.alias or table.name)
        if qualifier:
            qualifiers[descriptor.scope_id][qualifier] += 1
    # A derived table's alias belongs to the consuming scope, not to its body.
    for wrapper in (node for node in bounded_nodes if isinstance(node, exp.Subquery)):
        alias = _norm_name(wrapper.alias or "")
        if not alias or not _subquery_wrapper_is_derived(wrapper):
            continue
        consumer = _nearest_retained_scope(
            wrapper,
            descriptors_by_node,
            include_self=False,
        )
        if consumer is not None:
            qualifiers[consumer.scope_id][alias] += 1
    return tables, qualifiers


def _visible_cte_candidates(
    consumer: _Phase1ScopeDescriptor,
    table_name: str,
    descriptors: list[_Phase1ScopeDescriptor],
    descriptors_by_id: dict[str, _Phase1ScopeDescriptor],
) -> list[tuple[int, _Phase1ScopeDescriptor]]:
    ancestors = _scope_parent_chain(consumer, descriptors_by_id)
    owner_distance = {
        item.scope_id: index for index, item in enumerate(ancestors)
    }
    candidates: list[tuple[int, _Phase1ScopeDescriptor]] = []
    for producer in descriptors:
        if producer.scope_kind != "CTE" or _norm_name(producer.cte_name) != table_name:
            continue
        owner = producer.parent_scope_id or ""
        if owner not in owner_distance:
            continue
        # Inside one WITH list only earlier CTEs are visible.  A recursive WITH
        # additionally makes the current producer visible to itself.  This is
        # AST order/flag evidence, not a name-based dependency guess.
        if consumer.scope_kind == "CTE" and consumer.parent_scope_id == owner:
            producer_index = producer.cte_index
            consumer_index = consumer.cte_index
            if producer_index is None or consumer_index is None:
                continue
            if producer_index > consumer_index:
                continue
            if producer_index == consumer_index and not producer.cte_recursive:
                continue
        candidates.append((owner_distance[owner], producer))
    if not candidates:
        return []
    nearest = min(distance for distance, _ in candidates)
    return [item for item in candidates if item[0] == nearest]


def _scope_cte_edges(
    ast: exp.Expression,
    descriptors: list[_Phase1ScopeDescriptor],
    descriptors_by_node: dict[int, _Phase1ScopeDescriptor],
    limitations: set[str],
) -> list[dict[str, Any]]:
    descriptors_by_id = {item.scope_id: item for item in descriptors}
    tables, _ = _direct_scope_tables_and_qualifiers(ast, descriptors_by_node)
    edges: list[dict[str, Any]] = []
    for consumer in descriptors:
        for table in tables.get(consumer.scope_id, ()):
            name = _norm_name(table.name)
            candidates = _visible_cte_candidates(
                consumer,
                name,
                descriptors,
                descriptors_by_id,
            )
            if len(candidates) == 1:
                producer = candidates[0][1]
                edges.append(_scope_edge(
                    "CTE_FEEDS",
                    producer.scope_id,
                    consumer.scope_id,
                    "AST_VISIBLE_CTE_REFERENCE",
                ))
            elif len(candidates) > 1:
                consumer.metadata_complete = False
                limitations.add(
                    f"ambiguous visible CTE reference in {consumer.scope_id}"
                )
    return edges


def _scope_correlation_edges(
    ast: exp.Expression,
    descriptors: list[_Phase1ScopeDescriptor],
    descriptors_by_node: dict[int, _Phase1ScopeDescriptor],
    limitations: set[str],
) -> list[dict[str, Any]]:
    descriptors_by_id = {item.scope_id: item for item in descriptors}
    _, qualifiers = _direct_scope_tables_and_qualifiers(ast, descriptors_by_node)
    columns: dict[str, list[exp.Column]] = defaultdict(list)
    for index, node in enumerate(ast.walk()):
        if index >= _MAX_SCOPE_AST_NODES_SCANNED:
            break
        if not isinstance(node, exp.Column):
            continue
        column = node
        descriptor = _nearest_retained_scope(column, descriptors_by_node)
        if descriptor is not None:
            columns[descriptor.scope_id].append(column)

    edges: list[dict[str, Any]] = []
    for descriptor in descriptors:
        local = qualifiers.get(descriptor.scope_id, {})
        outer_targets: set[str] = set()
        saw_unresolved_outer_qualifier = False
        for column in columns.get(descriptor.scope_id, ()):
            qualifier = _norm_name(column.table or "")
            if not qualifier or qualifier in local:
                continue
            if not descriptor.correlation_allowed:
                saw_unresolved_outer_qualifier = True
                continue
            for outer in _scope_parent_chain(
                descriptor,
                descriptors_by_id,
                include_self=False,
            ):
                count = qualifiers.get(outer.scope_id, {}).get(qualifier, 0)
                if count == 1:
                    outer_targets.add(outer.scope_id)
                    break
                if count > 1:
                    saw_unresolved_outer_qualifier = True
                    limitations.add(
                        f"ambiguous correlated qualifier in {descriptor.scope_id}"
                    )
                    break
                # A CTE producer is a lexical correlation boundary.  Its body
                # may correlate internally, but it cannot capture tables from
                # the statement that consumes the CTE.
                if outer.scope_kind == "CTE":
                    break
            else:
                saw_unresolved_outer_qualifier = True

        for target in sorted(outer_targets):
            descriptor.is_correlated = True
            edges.append(_scope_edge(
                "CORRELATED_TO",
                descriptor.scope_id,
                target,
                "AST_QUALIFIED_OUTER_REFERENCE",
            ))
        if saw_unresolved_outer_qualifier:
            descriptor.metadata_complete = False
            if descriptor.correlation_allowed:
                limitations.add(
                    f"outer qualifier could not be proven in {descriptor.scope_id}"
                )
            elif descriptor.scope_kind == "DERIVED":
                limitations.add(
                    f"non-lateral derived outer reference not linked in {descriptor.scope_id}"
                )
    return edges


def _legacy_scope_fallback(
    label: str,
    descriptors: list[_Phase1ScopeDescriptor],
) -> _Phase1ScopeDescriptor | None:
    raw_label = str(label or "").strip().lower()
    normalized = _norm_name(raw_label)
    if normalized == "root":
        candidates = [item for item in descriptors if item.scope_kind == "ROOT"]
    elif raw_label.startswith("cte:"):
        name = _norm_name(raw_label.split(":", 1)[1])
        candidates = [
            item for item in descriptors
            if item.scope_kind == "CTE" and _norm_name(item.cte_name) == name
        ]
    else:
        # ``nested:N``, ``nested_correlation`` and ``subquery`` do not encode
        # a structural path.  Mapping them in a multi-scope query would be a
        # name/ordinal guess, so intentionally leave them unresolved.
        candidates = []
    return candidates[0] if len(candidates) == 1 else None


def _scope_diff_bindings(
    ast_diffs: list[ASTDiffNode],
    by_side: dict[str, list[_Phase1ScopeDescriptor]],
    by_side_node: dict[str, dict[int, _Phase1ScopeDescriptor]],
    limitations: set[str],
) -> tuple[list[dict[str, Any]], bool]:
    bindings: list[dict[str, Any]] = []
    truncated = False
    structural_by_side = {
        side: {
            item.structural_key: item
            for item in descriptors
        }
        for side, descriptors in by_side.items()
    }

    def paired_descriptor(
        side: str,
        descriptor: _Phase1ScopeDescriptor,
    ) -> _Phase1ScopeDescriptor | None:
        other_side = "student" if side == "standard" else "standard"
        candidate = structural_by_side[other_side].get(descriptor.structural_key)
        if candidate is None or candidate.scope_kind != descriptor.scope_kind:
            return None
        return candidate

    for index, diff in enumerate(ast_diffs):
        if index >= _MAX_SCOPE_DIFFS:
            truncated = True
            limitations.add("AST diff scope scan limit reached")
            break
        diff_id = stable_diff_id(diff, index)
        exact: dict[str, _Phase1ScopeDescriptor] = {}
        for side, node in (
            ("standard", diff.standard_node),
            ("student", diff.student_node),
        ):
            descriptor = _scope_for_diff_node(
                node,
                by_side_node[side],
                by_side[side],
            )
            if descriptor is not None:
                exact[side] = descriptor

        # When only one side has the changed/removed AST node, an identical
        # structural path on the other parsed tree is a proof-quality pairing.
        if len(exact) == 1:
            known_side, known = next(iter(exact.items()))
            other_side = "student" if known_side == "standard" else "standard"
            paired = paired_descriptor(known_side, known)
            if paired is not None:
                exact[other_side] = paired

        statuses: dict[str, str] = {}
        for side, descriptor in exact.items():
            evidence_node = (
                diff.standard_node if side == "standard" else diff.student_node
            )
            identity_match = _scope_for_diff_node(
                evidence_node,
                by_side_node[side],
            )
            if identity_match is descriptor:
                statuses[side] = "EXACT_AST_ANCESTOR"
            elif isinstance(evidence_node, exp.Expression):
                statuses[side] = "EXACT_AST_PATH"
            else:
                statuses[side] = "EXACT_PAIRED_AST_PATH"

        if not exact:
            # With exactly one query block on a side, there is no competing
            # scope and root attribution is proven by elimination.
            for side, descriptors in by_side.items():
                if len(descriptors) == 1:
                    exact[side] = descriptors[0]
                    # ``EXACT`` is the frozen consumer-level status.  The
                    # proof here is elimination: this side has exactly one
                    # retained query block, so no competing scope exists.
                    statuses[side] = "EXACT"

        conceptual_ids = {
            _conceptual_scope_id(descriptor)
            for side, descriptor in exact.items()
            if paired_descriptor(side, descriptor) is not None
        }
        conceptual_scope_id = (
            next(iter(conceptual_ids)) if len(conceptual_ids) == 1 else None
        )
        if exact and (
            conceptual_scope_id is None
            or any(
                paired_descriptor(side, descriptor) is None
                for side, descriptor in exact.items()
            )
        ):
            conceptual_scope_id = None
            limitations.add(f"diff conceptual scope unresolved: {diff_id}")

        if not exact:
            legacy_label = str((diff.extra or {}).get("query_scope") or "")
            for side, descriptors in by_side.items():
                fallback = _legacy_scope_fallback(legacy_label, descriptors)
                if fallback is not None:
                    bindings.append({
                        "diff_id": diff_id,
                        "side": side,
                        "scope_id": fallback.scope_id,
                        "binding_status": "FALLBACK_LABEL",
                    })
            limitations.add(f"diff scope unresolved: {diff_id}")
            continue

        for side, descriptor in sorted(exact.items()):
            if len(bindings) >= _MAX_SCOPE_DIFF_BINDINGS:
                truncated = True
                limitations.add("diff scope binding limit reached")
                return bindings, truncated
            bindings.append({
                "diff_id": diff_id,
                "side": side,
                "scope_id": descriptor.scope_id,
                "binding_status": statuses.get(side, "EXACT_AST_ANCESTOR"),
                **(
                    {"conceptual_scope_id": conceptual_scope_id}
                    if conceptual_scope_id is not None
                    else {}
                ),
            })
    return bindings, truncated


def _build_phase1_scope_metadata(
    standard_ast: exp.Expression,
    student_ast: exp.Expression,
    ast_diffs: list[ASTDiffNode],
) -> dict[str, Any]:
    """Build bounded, deterministic, AST-proven Phase 1 scope evidence.

    No edge is inferred from rendered SQL or from a human-facing scope label.
    CTE name lookup is used only after lexical visibility and WITH-list order
    have been proven from the AST.  Missing proof degrades the contract to
    ``PARTIAL`` and records a limitation instead of fabricating an edge.
    """

    limitations: set[str] = set()
    standard, standard_by_node, standard_truncated = _collect_phase1_scopes(
        standard_ast,
        "standard",
        limitations,
    )
    student, student_by_node, student_truncated = _collect_phase1_scopes(
        student_ast,
        "student",
        limitations,
    )
    by_side = {"standard": standard, "student": student}
    by_side_node = {
        "standard": standard_by_node,
        "student": student_by_node,
    }

    parent_edges: list[dict[str, Any]] = []
    composition_edges: list[dict[str, Any]] = []
    for descriptors in by_side.values():
        by_id = {item.scope_id: item for item in descriptors}
        for descriptor in descriptors:
            parent = by_id.get(descriptor.parent_scope_id or "")
            if parent is None:
                continue
            parent_edges.append(_scope_edge(
                "PARENT",
                descriptor.scope_id,
                parent.scope_id,
                "AST_ANCESTRY",
            ))
            if descriptor.scope_kind == "DERIVED":
                composition_edges.append(_scope_edge(
                    "DERIVED_FEEDS",
                    descriptor.scope_id,
                    parent.scope_id,
                    "AST_FROM_SUBQUERY",
                ))
            if descriptor.scope_kind == "SUBQUERY":
                composition_edges.append(_scope_edge(
                    "SUBQUERY_OF",
                    descriptor.scope_id,
                    parent.scope_id,
                    "AST_SUBQUERY_CONTEXT",
                ))
            if descriptor.scope_kind == "SET_BRANCH" and isinstance(
                parent.node,
                exp.SetOperation,
            ):
                composition_edges.append(_scope_edge(
                    "SET_MEMBER_OF",
                    descriptor.scope_id,
                    parent.scope_id,
                    "AST_SET_OPERAND",
                ))

    composition_edges.extend(_scope_cte_edges(
        standard_ast,
        standard,
        standard_by_node,
        limitations,
    ))
    composition_edges.extend(_scope_cte_edges(
        student_ast,
        student,
        student_by_node,
        limitations,
    ))
    composition_edges.extend(_scope_correlation_edges(
        standard_ast,
        standard,
        standard_by_node,
        limitations,
    ))
    composition_edges.extend(_scope_correlation_edges(
        student_ast,
        student,
        student_by_node,
        limitations,
    ))

    parent_edges = _merge_scope_edges(parent_edges)
    composition_edges = _merge_scope_edges(composition_edges)
    edge_truncated = False
    if len(parent_edges) > _MAX_SCOPE_EDGES:
        parent_edges = parent_edges[:_MAX_SCOPE_EDGES]
        edge_truncated = True
        limitations.add("parent scope edge limit reached")
    if len(composition_edges) > _MAX_SCOPE_EDGES:
        composition_edges = composition_edges[:_MAX_SCOPE_EDGES]
        edge_truncated = True
        limitations.add("composition scope edge limit reached")

    diff_bindings, binding_truncated = _scope_diff_bindings(
        ast_diffs,
        by_side,
        by_side_node,
        limitations,
    )

    # Pairing is descriptive only.  The side-aware IDs remain distinct, so
    # equal CTE aliases or equal column names can never merge two scopes.
    standard_paths = {item.structural_key: item for item in standard}
    student_paths = {item.structural_key: item for item in student}
    paired_ids: dict[str, str] = {}
    conceptual_ids: dict[str, str] = {}
    conceptual_scopes: list[dict[str, Any]] = []
    for structural_key in sorted(set(standard_paths) & set(student_paths)):
        left = standard_paths[structural_key]
        right = student_paths[structural_key]
        if left.scope_kind != right.scope_kind:
            continue
        conceptual_id = _conceptual_scope_id(left)
        paired_ids[left.scope_id] = right.scope_id
        paired_ids[right.scope_id] = left.scope_id
        conceptual_ids[left.scope_id] = conceptual_id
        conceptual_ids[right.scope_id] = conceptual_id
        conceptual_scopes.append({
            "conceptual_scope_id": conceptual_id,
            "scope_kind": left.scope_kind,
            "standard_scope_id": left.scope_id,
            "student_scope_id": right.scope_id,
            "pairing_status": "EXACT_AST_PATH",
        })

    scope_rows: list[dict[str, Any]] = []
    for descriptor in sorted(
        standard + student,
        key=lambda item: (item.side, item.lexical_depth, item.scope_id),
    ):
        row: dict[str, Any] = {
            "scope_id": descriptor.scope_id,
            "side": descriptor.side,
            "scope_kind": descriptor.scope_kind,
            "scope_label": descriptor.scope_label,
            "parent_scope_id": descriptor.parent_scope_id,
            "lexical_depth": descriptor.lexical_depth,
            "metadata_complete": descriptor.metadata_complete,
            "is_set_container": descriptor.is_set_container,
            "is_correlated": descriptor.is_correlated,
            "structural_path": [
                {"node": item[0], "arg": item[1], "index": item[2]}
                for item in descriptor.structural_path
            ],
        }
        if descriptor.cte_name:
            row["cte_name"] = descriptor.cte_name
            row["cte_index"] = descriptor.cte_index
            row["cte_recursive"] = descriptor.cte_recursive
        if descriptor.derived_alias:
            row["derived_alias"] = descriptor.derived_alias
        if descriptor.scope_id in paired_ids:
            row["paired_scope_id"] = paired_ids[descriptor.scope_id]
            row["conceptual_scope_id"] = conceptual_ids[descriptor.scope_id]
        scope_rows.append(row)

    truncated = any((
        standard_truncated,
        student_truncated,
        edge_truncated,
        binding_truncated,
    ))
    status = "COMPLETE" if not limitations and not truncated else "PARTIAL"
    return {
        "schema_version": _SCOPE_METADATA_VERSION,
        "status": status,
        "scopes": scope_rows,
        "conceptual_scopes": conceptual_scopes,
        "parent_edges": parent_edges,
        "composition_edges": composition_edges,
        "diff_bindings": sorted(
            diff_bindings,
            key=lambda item: (
                item["diff_id"],
                item["side"],
                item["scope_id"],
            ),
        ),
        "limitations": sorted(limitations),
        "counts": {
            "scopes": len(scope_rows),
            "conceptual_scopes": len(conceptual_scopes),
            "parent_edges": len(parent_edges),
            "composition_edges": len(composition_edges),
            "diff_bindings": len(diff_bindings),
        },
        "truncated": truncated,
    }


def _primary_key_candidate(columns: list[str], table_name: str) -> str | None:
    if not columns:
        return None
    first_col = columns[0]
    first_norm = _norm_name(first_col)
    table_norm = _norm_name(table_name)
    aliases = _table_key_aliases(table_norm)
    if first_norm == "id" or first_norm in aliases or first_norm in {"ssn", "dno", "dnum", "pno"}:
        return first_col
    if first_norm.endswith("_id") or first_norm.endswith("id"):
        return first_col
    for col in columns:
        norm = _norm_name(col)
        if norm == "id" or norm in aliases:
            return col
    return None


def _is_primary_key_candidate(table_name: str, col: str, columns: list[str]) -> bool:
    pk = _primary_key_candidate(columns, table_name)
    return pk is not None and _norm_name(pk) == _norm_name(col)
