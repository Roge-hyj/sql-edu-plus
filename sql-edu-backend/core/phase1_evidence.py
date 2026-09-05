"""SQLite execution, mutation, and evidence integration."""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Iterable
from collections import Counter, defaultdict
from itertools import product
import sqlite3
import time
import sqlglot
from sqlglot import ErrorLevel, exp
from core.ast_schema import ASTDiffNode
from core.witness_generation.schema_scope import SchemaCatalog
from core.witness_generation.obligations import (
    DistinguishingObligation,
    stable_diff_id,
)
from core.witness_generation.planner import (
    WitnessWorld,
    write_owner,
)
from core.witness_generation.regex_support import regex_matches

from core.phase1_foundation import (
    SandboxRun,
    _MAX_WITNESS_ROWS_PER_TABLE,
    _MUTATION_ORIGINAL_EQUIVALENT,
    _SQLITE_EXECUTION_TIME_BUDGET_SECONDS,
    _SQLITE_PROGRESS_GRANULARITY,
    _SQLITE_VM_INSTRUCTION_BUDGET,
    _advanced_clause_ast_diffs,
    _aggregate_filter_is_only_projection_difference,
    _changed_having_aggregate_spec,
    _clause_ast_diffs,
    _coerce_typed_seed,
    _collect_subqueries,
    _comparison_node_from_diff,
    _comparison_subquery_parts,
    _direct_from_table,
    _extract_having_aggregate_specs,
    _function_name,
    _group_by_ast_diffs,
    _group_by_items,
    _has_diff,
    _having_placement_ast_diffs,
    _is_inside_subquery,
    _is_platform_execution_error,
    _join_type_kp,
    _limit_offset_required_rows,
    _literal_value,
    _nearest_select,
    _order_by_ast_diffs,
    _paired_query_blocks,
    _parse_sql,
    _positive_probe_value,
    _projection_column_ast_diffs,
    _query_block_scope_key,
    _rows_equivalent,
    _set_operator_modifier,
    _set_operator_node,
    _sql_of,
    _temporal_comparison_parts,
    _top_select,
    _with_parent_cte_context,
)

from core.phase1_sql_semantics import (
    _aggregate_function_ast_diffs,
    _assign_window_groups,
    _assign_window_order_values,
    _atomic_student_variant,
    _case_ast_diffs,
    _catalog_has_unary_unique_key,
    _comparison_matches,
    _comparison_truth_value,
    _eval_logical_tree,
    _expression_static_value,
    _extend_order_series,
    _extract_literal_constraints,
    _from_source_ast_diffs,
    _group_probe_value,
    _is_numeric_column,
    _logical_leaf_key,
    _logical_leaf_nodes,
    _logical_operator_ast_diffs,
    _mutate_by_node_replacement,
    _norm_name,
    _positive_numeric_series_for_comparison,
    _predicate_negation_ast_diffs,
    _predicate_truth_assignment,
    _prepare_sqlite_source,
    _rewrite_bare_offset,
    _normalize_sqlite_order_aliases,
    _seed_value,
    _set_operator_ast_diffs,
    _sqlite_declared_affinity,
    _table_key_aliases,
    _unique_key_value,
    _window_companion_aliases,
    _window_partition_columns,
    _window_spec,
)

from core.phase1_constraints import (
    _column_lookup,
    _comparison_ast_diffs,
    _cte_ast_diffs,
    _function_argument_ast_diffs,
    _group_by_columns_for_sql,
    _is_recursive_ast,
    _join_ast_diffs,
    _materialize_aggregate_filter_witness,
    _materialize_aggregate_obligation_witness,
    _materialize_glob_pattern_witness,
    _materialize_like_pattern_witness,
    _materialize_like_presence_witness,
    _materialize_null_sensitive_limit_order_witness,
    _materialize_predicate_presence_obligation_witness,
    _materialize_regex_pattern_witness,
    _outer_join_predicate_placement_ast_diffs,
    _projection_alias_ast_diffs,
    _repair_known_unsafe_division_paths,
    _table_aliases,
)

from core.phase1_query_paths import (
    _actual_data_ref,
    _apply_aggregate_function_probe,
    _column_ref_in_select,
    _column_ref_in_select_data,
    _correlated_subquery_context_ast_diffs,
    _correlated_subquery_links,
    _direct_select_tables,
    _materialize_aggregate_filter_presence_witness,
    _materialize_conjunctive_in_exists_membership_witness,
    _materialize_correlated_key_drift_witness,
    _materialize_correlated_scalar_aggregate_key_drift_witness,
    _materialize_declared_aggregate_boundary,
    _materialize_filtered_aggregate_boundary_path,
    _materialize_limit_antijoin_path,
    _materialize_not_in_reachable_path,
    _materialize_select_literal_path,
    _materialize_simple_in_exists_membership_witness,
    _materialize_subquery_comparison_boundary_witness,
    _materialize_subquery_membership_key_drift_witness,
    _set_select_local_literal_predicates,
    _specialized_semantic_ast_diffs,
    _subquery_is_correlated,
)

from core.phase1_witness_strategies import (
    _authoritative_column_kind,
    _is_primary_key_candidate,
    _materialize_declared_join_witness,
    _materialize_nested_distinct_projection_witness,
    _materialize_query_block_comparison_boundaries,
    _materialize_shared_literal_predicate_paths,
    _materialize_strict_scalar_count_paths,
    _materialize_subquery_membership_obligation_witness,
    _materialize_temporal_filter_paths,
    _primary_key_candidate,
    _queries_are_supported_equivalent_rewrites,
    _query_column_ref_in_data,
    _repair_declared_nonnull_columns,
    _repair_numeric_column_types,
    _schema_numeric_projection_identities_equivalent,
    _stabilize_exists_duplicate_projection_witness,
    _stabilize_filtered_aggregate_witness,
    _stabilize_having_sum_boundary,
    _stabilize_nested_membership_witness,
    _stabilize_same_table_correlated_avg_witness,
    _temporal_boundary_value,
)

from core.phase1_witness_materialization import (
    _align_standard_join_equalities,
    _materialize_correlated_exists_boundary_path,
    _materialize_cte_aggregate_alias_boundary,
    _materialize_derived_comparison_boundaries,
    _materialize_derived_sum_alias_boundary,
    _materialize_distinct_join_projection_witness,
    _materialize_having_ratio_boundary,
    _materialize_in_list_obligation_witness,
    _materialize_joined_having_count_boundary,
    _materialize_literal_in_reachability,
    _materialize_null_query_block_paths,
    _materialize_order_obligation_witness,
    _materialize_query_block_aggregate_paths,
    _materialize_query_block_reachability,
    _materialize_select_row_path,
    _materialize_set_grouped_branch_path,
    _materialize_top_level_distinct_filter_witness,
    _materialize_window_alias_cardinality_boundary,
)



def _validate_world_atomic_diffs(
    *,
    world: WitnessWorld,
    run: SandboxRun,
    ast_diffs: list[ASTDiffNode],
    student_sql: str,
    schema: dict[str, list[str]],
    schema_types: dict[str, dict[str, str]],
) -> dict[str, Any]:
    """Validate the obligation with a single-difference mutant.

    Comparing the original student SQL is sufficient for the final verdict,
    but not for attribution when several AST differences coexist.  Here the
    standard AST is mutated at exactly one diff node and executed against the
    same world.  A world counts as covering its obligation only when that
    atomic mutant also differs from the standard result.
    """

    if not run.executed:
        return {
            "supported_count": 0,
            "all_supported_distinguished": False,
            "tests": [],
        }
    indexed = [
        (stable_diff_id(diff, index), diff)
        for index, diff in enumerate(ast_diffs)
        if stable_diff_id(diff, index) in set(world.diff_ids)
    ]
    tests: list[dict[str, Any]] = []
    for diff_id, diff in indexed:
        variant_sql = _atomic_student_variant(diff)
        if not variant_sql and diff.diff_type in {
            # These clause-level nodes intentionally keep compact source
            # fragments in ASTDiffNode. When the planner isolates that
            # one obligation, the original student query is the exact
            # atomic variant and preserves NOT LIKE/ORDER expression
            # polarity that cannot be rebuilt from the fragment alone.
            "where_changed",
            "order_by_changed",
            "projection_changed",
        }:
            variant_sql = student_sql
        if not variant_sql:
            tests.append(
                {
                    "diff_id": diff_id,
                    "supported": False,
                    "distinguished": False,
                    "reason": "ast_node_not_rewritable",
                }
            )
            continue
        try:
            nested_scope = str(diff.extra.get("query_scope") or "root")
            nested_standard_sql = str(
                diff.extra.get("standard_query_sql") or ""
            ).strip()
            nested_student_sql = str(
                diff.extra.get("student_query_sql") or ""
            ).strip()
            if (
                diff.diff_type == "distinct_changed"
                and nested_scope != "root"
                and nested_standard_sql
                and nested_student_sql
            ):
                # A nested DISTINCT is attributed against its own exact
                # query block.  The outer query may aggregate it away,
                # so using the full-query result here would make atomic
                # attribution impossible even when the inner mutant is
                # directly observable.
                parent_sql = (
                    world.execution.get("validation_context", {}).get(
                        "standard_source_sql"
                    )
                    or ""
                )
                nested_standard_sql = _with_parent_cte_context(
                    parent_sql,
                    nested_standard_sql,
                )
                nested_student_sql = _with_parent_cte_context(
                    parent_sql,
                    nested_student_sql,
                )
                nested_standard_exec = _prepare_mutation_sql(
                    nested_standard_sql,
                    allowed_tables=schema.keys(),
                )
                nested_student_exec = _prepare_mutation_sql(
                    nested_student_sql,
                    allowed_tables=schema.keys(),
                )
                if not nested_standard_exec or not nested_student_exec:
                    raise ValueError("nested_mutation_sql_prepare_failed")
                nested_standard_columns, nested_standard_rows = _execute_sqlite(
                    schema,
                    run.test_database,
                    nested_standard_exec,
                    schema_types=schema_types,
                )
                columns, result_rows = _execute_sqlite(
                    schema,
                    run.test_database,
                    nested_student_exec,
                    schema_types=schema_types,
                )
                equivalent = (
                    len(nested_standard_columns) == len(columns)
                    and Counter(nested_standard_rows) == Counter(result_rows)
                )
                standard_result_for_evidence = nested_standard_rows
            else:
                executable_sql = _prepare_mutation_sql(
                    variant_sql,
                    allowed_tables=schema.keys(),
                )
                if not executable_sql:
                    raise ValueError("mutation_sql_prepare_failed")
                columns, result_rows = _execute_sqlite(
                    schema,
                    run.test_database,
                    executable_sql,
                    schema_types=schema_types,
                )
                ordered = bool(run.data_evidence.get("ordered_compare"))
                equivalent = _rows_equivalent(
                    run.standard_columns,
                    run.standard_rows,
                    columns,
                    result_rows,
                    ordered,
                )
                standard_result_for_evidence = run.standard_rows
            tests.append(
                {
                    "diff_id": diff_id,
                    "supported": True,
                    "distinguished": not equivalent,
                    "variant_sql": (
                        nested_student_sql
                        if nested_scope != "root" and diff.diff_type == "distinct_changed"
                        else executable_sql
                    ),
                    "standard_result": standard_result_for_evidence[:5],
                    "mutant_result": result_rows[:5],
                }
            )
        except Exception as exc:
            error_text = str(exc)
            execution_error_distinguished = (
                run.is_equivalent is False
                and any(
                    marker in error_text.lower()
                    for marker in (
                        "no such column",
                        "no such table",
                        "syntax error",
                        "ambiguous column",
                        "unknown column",
                    )
                )
            )
            tests.append(
                {
                    "diff_id": diff_id,
                    "supported": False,
                    "distinguished": execution_error_distinguished,
                    "execution_error": execution_error_distinguished,
                    "reason": error_text,
                }
            )
    supported = [item for item in tests if item.get("supported")]
    return {
        "supported_count": len(supported),
        "all_supported_distinguished": bool(supported)
        and all(item.get("distinguished") for item in supported)
        and len(supported) == len(tests),
        "tests": tests,
    }


def _finalize_generated_witness_data(
    data: dict[str, list[dict[str, Any]]],
    standard_sql: str,
    student_sql: str,
    ast_diffs: list[ASTDiffNode],
    *,
    generation_scope: dict[str, bool] | None = None,
    obligations: list[DistinguishingObligation] | None = None,
    schema_catalog: SchemaCatalog | None = None,
) -> None:
    """Run the single final topology pass for one materialized world."""
    scope = generation_scope or {}
    aggregate_scope = bool(scope.get("aggregate"))
    subquery_scope = bool(scope.get("subquery"))
    distinct_or_join_scope = bool(scope.get("distinct") or scope.get("join"))
    has_declared_aggregate_boundary = any(
        constraint.kind == "aggregate_boundary_group"
        for obligation in (obligations or ())
        for constraint in obligation.hard_constraints
    )
    # Clean generator artefacts before semantic materializers write their
    # owned witness values.  Running this after membership materialization
    # would turn legitimate string literals in numeric-looking key columns
    # back into seed numbers and destroy the path.
    _repair_numeric_column_types(data, schema_catalog=schema_catalog)
    if aggregate_scope and (
        ("AVG(" in standard_sql.upper() and "AVG(" in student_sql.upper())
        or ("HAVING" in standard_sql.upper() and "SUM(" in standard_sql.upper())
    ):
        _stabilize_filtered_aggregate_witness(data, standard_sql, student_sql)
        if not has_declared_aggregate_boundary:
            _stabilize_having_sum_boundary(data, standard_sql, student_sql)
        _stabilize_same_table_correlated_avg_witness(data, standard_sql, student_sql)
    if subquery_scope and any(
        diff.diff_type in {"subquery_added", "subquery_removed", "where_changed", "literal_changed"}
        for diff in ast_diffs
    ):
        _stabilize_nested_membership_witness(data, standard_sql, student_sql)
    if distinct_or_join_scope and any(
        diff.diff_type in {"distinct_changed", "join_added", "join_removed", "join_type_changed"}
        for diff in ast_diffs
    ):
        _stabilize_exists_duplicate_projection_witness(data, standard_sql, student_sql)
    _materialize_predicate_presence_obligation_witness(
        data,
        standard_sql,
        student_sql,
        ast_diffs,
    )
    _materialize_aggregate_filter_presence_witness(
        data,
        standard_sql,
        student_sql,
        ast_diffs,
    )
    _materialize_aggregate_obligation_witness(data, standard_sql, ast_diffs)
    _materialize_declared_aggregate_boundary(
        data,
        obligations or [],
        standard_sql,
    )
    _materialize_joined_having_count_boundary(
        data,
        obligations or [],
        standard_sql,
        schema_catalog=schema_catalog,
    )
    _materialize_filtered_aggregate_boundary_path(
        data,
        obligations or [],
        standard_sql,
        student_sql,
        schema_catalog=schema_catalog,
    )
    # Aggregate-function discrimination depends on the final group topology.
    # Re-materialize it after generic CTE/JOIN/group repairs so increasing the
    # requested witness scale cannot reintroduce cyclic group keys that make
    # SUM and AVG (or MIN/MAX) select the same group again.
    for table_name, rows in data.items():
        if rows:
            _apply_aggregate_function_probe(
                rows,
                list(rows[0]),
                table_name,
                standard_sql,
                student_sql,
                ast_diffs,
            )
    _materialize_window_obligation_witness(data, standard_sql, ast_diffs)
    _materialize_order_obligation_witness(
        data,
        standard_sql,
        student_sql,
        ast_diffs,
        schema_catalog=schema_catalog,
    )
    _materialize_subquery_membership_obligation_witness(
        data,
        ast_diffs,
        standard_sql,
        student_sql,
        schema_catalog=schema_catalog,
    )
    _materialize_correlated_key_drift_witness(
        data,
        standard_sql,
        student_sql,
    )
    _materialize_subquery_membership_key_drift_witness(
        data,
        ast_diffs,
        standard_sql,
    )
    _materialize_subquery_comparison_boundary_witness(
        data,
        standard_sql,
        student_sql,
    )
    _materialize_in_list_obligation_witness(
        data,
        standard_sql,
        ast_diffs,
        schema_catalog=schema_catalog,
    )
    _materialize_case_obligation_witness(
        data,
        standard_sql,
        ast_diffs,
        schema_catalog=schema_catalog,
    )
    _materialize_set_grouped_branch_path(
        data,
        obligations or [],
        standard_sql,
        student_sql,
        schema_catalog=schema_catalog,
    )
    _materialize_scalar_aggregate_boundary_path(
        data,
        obligations or [],
        standard_sql,
        schema_catalog=schema_catalog,
    )
    _materialize_aggregate_filter_witness(data, obligations or [])
    _materialize_regex_pattern_witness(data, obligations or [])
    _materialize_like_pattern_witness(data, obligations or [])
    _materialize_glob_pattern_witness(data, obligations or [])
    _materialize_limit_antijoin_path(
        data,
        obligations or [],
        standard_sql,
        student_sql,
    )
    _materialize_cte_aggregate_alias_boundary(
        data,
        standard_sql,
        student_sql,
        ast_diffs,
        schema_catalog=schema_catalog,
    )
    _materialize_derived_sum_alias_boundary(
        data,
        standard_sql,
        student_sql,
        ast_diffs,
        schema_catalog=schema_catalog,
    )
    _materialize_having_ratio_boundary(
        data,
        standard_sql,
        student_sql,
        ast_diffs,
        schema_catalog=schema_catalog,
    )
    _materialize_window_alias_cardinality_boundary(
        data,
        standard_sql,
        student_sql,
        ast_diffs,
        schema_catalog=schema_catalog,
    )
    _materialize_null_sensitive_limit_order_witness(
        data,
        standard_sql,
        student_sql,
    )
    # Temporal comparisons are finalized last.  Generic literal probes use
    # string sentinels for unknown columns; that is safe for ordinary text
    # predicates but makes DATE/YEAR boundaries either unparsable or
    # accidentally equal on both sides.  Keep this pass narrow and
    # query-block-aware so it only owns worlds with a temporal comparison
    # obligation.
    _materialize_temporal_comparison_witness(
        data,
        standard_sql,
        student_sql,
        ast_diffs,
        schema_catalog=schema_catalog,
    )
    _materialize_correlated_exists_boundary_path(
        data,
        standard_sql,
        student_sql,
        ast_diffs,
    )
    _materialize_correlated_scalar_aggregate_key_drift_witness(
        data,
        standard_sql,
        student_sql,
        ast_diffs,
        schema_catalog=schema_catalog,
    )
    # Resolve simple physical lineage before DISTINCT adapters run.  This is
    # what makes a filter inside a derived table/CTE and its outer JOIN path
    # reach the same concrete rows without relaxing the evidence gate.
    _materialize_query_block_reachability(
        data,
        standard_sql,
        student_sql,
        ast_diffs,
        schema_catalog=schema_catalog,
    )
    _materialize_query_block_comparison_boundaries(
        data,
        standard_sql,
        student_sql,
        ast_diffs,
    )
    # Query-block reachability aligns physical equality paths after the
    # first CTE aggregate pass.  Re-apply this narrow aggregate-alias owner
    # once the path is reachable so AVG/SUM/MIN/MAX aliases still receive the
    # exact outer boundary (for example AVG(age) = 22), without broadening
    # the generic lineage writer.
    _materialize_cte_aggregate_alias_boundary(
        data,
        standard_sql,
        student_sql,
        ast_diffs,
        schema_catalog=schema_catalog,
    )
    _materialize_strict_scalar_count_paths(
        data,
        standard_sql,
        ast_diffs,
    )
    _materialize_query_block_aggregate_paths(
        data,
        standard_sql,
        schema_catalog=schema_catalog,
    )
    _materialize_null_query_block_paths(
        data,
        standard_sql,
        student_sql,
        ast_diffs,
        schema_catalog=schema_catalog,
    )
    # A top-level DISTINCT over a multi-table projection cannot be witnessed
    # by copying payload cells in each physical table independently.  The
    # projected tuple is produced by a *join path*: two distinct fact rows
    # must resolve to the same dimension rows (or to equal payload values).
    # Materialize that path last, after PK repair and all generic probes, so a
    # later compatibility tactic cannot silently undo the duplicate.
    _materialize_distinct_join_projection_witness(
        data,
        standard_sql,
        student_sql,
        ast_diffs,
    )
    _materialize_top_level_distinct_filter_witness(
        data,
        standard_sql,
        student_sql,
        ast_diffs,
    )
    _materialize_nested_distinct_projection_witness(
        data,
        standard_sql,
        student_sql,
        ast_diffs,
        schema_catalog=schema_catalog,
    )
    _materialize_shared_literal_predicate_paths(
        data,
        standard_sql,
        student_sql,
        ast_diffs,
        schema_catalog=schema_catalog,
    )
    _materialize_not_in_reachable_path(
        data,
        standard_sql,
        student_sql,
        ast_diffs,
    )
    _materialize_simple_in_exists_membership_witness(
        data,
        standard_sql,
        student_sql,
    )
    _materialize_conjunctive_in_exists_membership_witness(
        data,
        standard_sql,
        student_sql,
    )
    # Logical truth-table rows are the final owner of the two predicate
    # columns.  Comparison/literal and compatibility materializers above may
    # touch the same cells after the registry probe; replaying the narrow
    # AND/OR/precedence probe here preserves all four boolean assignments for
    # its semantic validator without changing unrelated predicate worlds.
    if any(
        diff.diff_type in {
            "logical_operator_changed",
            "logical_precedence_tree_changed",
        }
        for diff in ast_diffs
    ):
        _apply_logical_operator_probe(data, standard_sql, student_sql)
    if any(
        diff.diff_type in {
            "set_operator_changed",
            "set_modifier_changed",
            "set_all_modifier_changed",
        }
        for diff in ast_diffs
    ):
        # Set-branch overlap is the final owner of projected cells.  Generic
        # numeric/PK repairs can otherwise overwrite it after the registry
        # adapter has created the path.
        _apply_set_operator_probes(data, standard_sql, student_sql, ast_diffs)
        _materialize_union_total_overlap_path(
            data,
            obligations or [],
            standard_sql,
            student_sql,
            schema_catalog=schema_catalog,
        )
    # LIKE presence paths own their source column after all generic literal
    # and compatibility probes.  Replay this narrow adapter at the end so a
    # later numeric/string repair cannot erase the positive/negative value
    # pair it selected.
    _materialize_like_presence_witness(
        data,
        [
            diff
            for diff in ast_diffs
            if diff.diff_type in {"predicate_missing", "predicate_added"}
            and not diff.extra.get("subquery_depth")
        ],
    )
    if any(
        diff.diff_type in {
            "join_missing",
            "join_type_changed",
            "join_on_changed",
            "join_predicate_placement_changed",
        }
        for diff in ast_diffs
    ):
        # Numeric/string compatibility repair above can reinterpret a
        # materialized key (e.g. ``1`` as ``pro_com_1``).  Re-establish the
        # declared JOIN endpoint after that repair, preserving one real match
        # and one dangling path for the join validator.
        _materialize_declared_join_witness(
            data,
            ast_diffs,
            standard_sql=standard_sql,
            schema_catalog=schema_catalog,
        )
    _repair_declared_nonnull_columns(data, schema_catalog=schema_catalog)
    # This is intentionally the final temporal owner.  Literal and
    # compatibility probes may have written a numeric year into a date-like
    # column after the comparison materializer ran; restore valid calendar
    # values only for filters that are actually present in the standard query.
    _materialize_temporal_filter_paths(
        data,
        standard_sql,
        schema_catalog=schema_catalog,
    )
    _materialize_literal_in_reachability(
        data,
        standard_sql,
        schema_catalog=schema_catalog,
    )
    _materialize_derived_comparison_boundaries(
        data,
        standard_sql,
        student_sql,
        ast_diffs,
        schema_catalog=schema_catalog,
    )
    _repair_known_unsafe_division_paths(
        data,
        standard_sql,
        student_sql,
        schema_catalog=schema_catalog,
    )


def _materialize_temporal_comparison_witness(
    data: dict[str, list[dict[str, Any]]],
    standard_sql: str,
    student_sql: str,
    ast_diffs: list[ASTDiffNode],
    *,
    schema_catalog: SchemaCatalog | None = None,
) -> bool:
    """Materialize an exact direct date-column boundary for one world.

    This is intentionally a small, deterministic witness pass.  It does not
    fabricate rows or rewrite arbitrary expressions: it resolves the source
    column through the current query block, aligns existing equality joins,
    makes one local path reachable, and writes the exact temporal boundary.
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
        if not isinstance(standard_comparison, comparison_types) or not isinstance(student_comparison, comparison_types):
            continue
        standard_parts = _temporal_comparison_parts(standard_comparison)
        student_parts = _temporal_comparison_parts(student_comparison)
        if standard_parts is None or student_parts is None:
            continue
        _, standard_column, standard_literal = standard_parts
        _, student_column, student_literal = student_parts
        if _norm_name(standard_column.name) != _norm_name(student_column.name):
            continue
        standard_select = _nearest_select(standard_comparison)
        student_select = _nearest_select(student_comparison)
        if not isinstance(standard_select, exp.Select) or not isinstance(student_select, exp.Select):
            continue
        standard_ref = _column_ref_in_select_data(data, standard_column, standard_select)
        student_ref = _column_ref_in_select_data(data, student_column, student_select)
        if standard_ref is None or student_ref is None or standard_ref != student_ref:
            continue
        actual = _actual_data_ref(data, standard_ref)
        if actual is None or not actual[0]:
            continue
        table_name = standard_ref[0]
        boundary = _temporal_boundary_value(
            standard_column,
            standard_literal,
            table=table_name,
            schema_catalog=schema_catalog,
        )
        if boundary is None:
            # A literal change may have a temporal threshold only on the
            # student side; try that endpoint before giving up.
            boundary = _temporal_boundary_value(
                student_column,
                student_literal,
                table=table_name,
                schema_catalog=schema_catalog,
            )
        if boundary is None:
            continue
        with write_owner(f"materializer:temporal_boundary:{diff.diff_type}"):
            # Equality joins are a dependency of the witness, not a new
            # semantic difference.  Re-aligning the existing standard path
            # is bounded and keeps a date row from being stranded in a join.
            _align_standard_join_equalities(data, standard_sql)
            _materialize_select_literal_path(
                data,
                standard_select,
                0,
                protected=standard_ref,
            )
            _materialize_select_literal_path(
                data,
                student_select,
                0,
                protected=student_ref,
            )
            actual[0][0][actual[1]] = boundary
        changed = True
    return changed


def _materialize_union_total_overlap_path(
    data: dict[str, list[dict[str, Any]]],
    obligations: list[DistinguishingObligation],
    standard_sql: str,
    student_sql: str,
    *,
    schema_catalog: SchemaCatalog | None = None,
) -> bool:
    """Materialize a safe overlap for a grouped ``UNION ALL`` total row.

    Some real-world reporting queries append a grand-total branch to a
    grouped branch, for example ``... GROUP BY building_use UNION ALL SELECT
    'TOTAL', ...``.  The ordinary set probe can only see the first physical
    table it encounters and therefore cannot make the grouped row equal to
    the total row.  When the total is a literal marker and both branches are
    demonstrably driven by the same unfiltered physical table, collapsing the
    grouping key to that marker is a bounded, relationally meaningful witness.

    This adapter is intentionally narrow.  It requires the two branches to
    be unchanged apart from the set modifier, refuses recursive queries and
    unique grouping keys, and validates the proposed data on the actual
    transpiled branch SQL before committing any writes.  If the branch rows
    do not really overlap, the trial data is discarded and the caller keeps
    the honest ``KNOWN_GAP`` result.
    """
    if not any(
        constraint.kind == "set_left_right_overlap"
        for obligation in obligations
        for constraint in obligation.hard_constraints
    ):
        return False

    standard_ast = _parse_sql(standard_sql)
    student_ast = _parse_sql(student_sql)
    standard_node = _set_operator_node(standard_ast)
    student_node = _set_operator_node(student_ast)
    if not isinstance(standard_node, exp.Union) or not isinstance(
        student_node, exp.Union
    ):
        return False
    if _set_operator_modifier(standard_node) == _set_operator_modifier(student_node):
        return False
    if _is_recursive_ast(standard_ast) or _is_recursive_ast(student_ast):
        return False
    if (
        _sql_of(standard_node.this) != _sql_of(student_node.this)
        or _sql_of(standard_node.expression) != _sql_of(student_node.expression)
    ):
        return False

    def branch_selects(branch: exp.Expression) -> list[exp.Select]:
        if isinstance(branch, exp.Select):
            return [branch, *branch.find_all(exp.Select)]
        return list(branch.find_all(exp.Select))

    def first_select(branch: exp.Expression) -> exp.Select | None:
        selects = branch_selects(branch)
        return selects[0] if selects else None

    def has_total_marker(select: exp.Select | None) -> bool:
        if not isinstance(select, exp.Select) or not select.expressions:
            return False
        expression = select.expressions[0]
        expression = expression.this if isinstance(expression, exp.Alias) else expression
        if not isinstance(expression, exp.Literal) or expression.is_number:
            return False
        value = _literal_value(expression)
        return isinstance(value, str) and value.strip().upper() == "TOTAL"

    def local_aggregate(select: exp.Select) -> bool:
        return any(
            isinstance(node, (exp.Count, exp.Sum, exp.Avg, exp.Min, exp.Max))
            and node.find_ancestor(exp.Select) is select
            for node in select.find_all(
                exp.Count, exp.Sum, exp.Avg, exp.Min, exp.Max
            )
        )

    def grouped_source(
        branch: exp.Expression,
    ) -> tuple[str, str, exp.Select, list[dict[str, Any]]] | None:
        for select in branch_selects(branch):
            group = select.args.get("group")
            if not isinstance(group, exp.Group) or len(group.expressions or ()) != 1:
                continue
            if select.args.get("where") is not None or select.args.get("having") is not None:
                continue
            if select.args.get("joins"):
                continue
            group_expression = group.expressions[0]
            if not isinstance(group_expression, exp.Column):
                continue
            direct_tables = _direct_select_tables(select)
            physical_tables = list(dict.fromkeys(direct_tables.values()))
            if len(physical_tables) != 1 or not local_aggregate(select):
                continue
            ref = _column_ref_in_select_data(data, group_expression, select)
            actual = _actual_data_ref(data, ref) if ref is not None else None
            if actual is None:
                continue
            rows, actual_column = actual
            if not rows:
                continue
            table_name = next(
                (
                    name
                    for name, candidate_rows in data.items()
                    if candidate_rows is rows
                ),
                physical_tables[0],
            )
            return table_name, actual_column, select, rows
        return None

    def has_unfiltered_aggregate_source(
        branch: exp.Expression,
        table_name: str,
    ) -> bool:
        for select in branch_selects(branch):
            direct_tables = _direct_select_tables(select)
            physical_tables = list(dict.fromkeys(direct_tables.values()))
            if physical_tables != [_norm_name(table_name)]:
                continue
            if (
                select.args.get("where") is not None
                or select.args.get("group") is not None
                or select.args.get("having") is not None
                or select.args.get("joins")
            ):
                continue
            if local_aggregate(select):
                return True
        return False

    def execute_branch(
        trial_data: dict[str, list[dict[str, Any]]],
        branch: exp.Expression,
    ) -> list[tuple[Any, ...]] | None:
        try:
            branch_sql = transpile_to_sqlite(_sql_of(branch))
            if not branch_sql:
                return None
            fixture_schema = {
                table: list(rows[0])
                for table, rows in trial_data.items()
                if rows
            }
            _columns, result = _execute_sqlite(fixture_schema, trial_data, branch_sql)
            return result
        except Exception:
            return None

    branches = [standard_node.this, standard_node.expression]
    for total_index, total_branch in enumerate(branches):
        total_select = first_select(total_branch)
        if not has_total_marker(total_select):
            continue
        grouped = grouped_source(branches[1 - total_index])
        if grouped is None:
            continue
        table_name, group_column, _group_select, rows = grouped
        if len(rows) < 1:
            continue
        if (
            _catalog_has_unary_unique_key(
                schema_catalog,
                (_norm_name(table_name), _norm_name(group_column)),
            )
            or _is_primary_key_candidate(table_name, group_column, list(rows[0]))
        ):
            continue
        kind = _authoritative_column_kind(table_name, group_column, schema_catalog)
        if kind in {"numeric", "date", "time"}:
            continue
        if not has_unfiltered_aggregate_source(total_branch, table_name):
            continue

        trial_data = {
            table: [dict(row) for row in candidate_rows]
            for table, candidate_rows in data.items()
        }
        trial_rows = next(
            (
                candidate_rows
                for table, candidate_rows in trial_data.items()
                if _norm_name(table) == _norm_name(table_name)
            ),
            None,
        )
        if not trial_rows:
            continue
        trial_lookup = _column_lookup(list(trial_rows[0]))
        trial_column = trial_lookup.get(_norm_name(group_column))
        if not trial_column:
            continue
        for row in trial_rows:
            row[trial_column] = "TOTAL"

        left_result = execute_branch(trial_data, standard_node.this)
        right_result = execute_branch(trial_data, standard_node.expression)
        if left_result is None or right_result is None:
            continue
        if not (set(left_result) & set(right_result)):
            continue

        actual_rows = data.get(table_name)
        if actual_rows is None:
            actual_rows = next(
                (
                    candidate_rows
                    for table, candidate_rows in data.items()
                    if _norm_name(table) == _norm_name(table_name)
                ),
                None,
            )
        if actual_rows is None:
            continue
        actual_lookup = _column_lookup(list(actual_rows[0]))
        actual_column = actual_lookup.get(_norm_name(group_column))
        if not actual_column:
            continue
        with write_owner("materializer:set_union_total_overlap"):
            for row in actual_rows:
                row[actual_column] = "TOTAL"
        return True
    return False


def _materialize_window_obligation_witness(
    data: dict[str, list[dict[str, Any]]],
    standard_sql: str,
    ast_diffs: list[ASTDiffNode],
) -> None:
    """Re-establish declared partition/order topology for one window world."""
    window_diff = next(
        (
            diff for diff in ast_diffs
            if diff.diff_type in {"window_over_changed", "window_function_changed"}
        ),
        None,
    )
    if window_diff is None:
        return
    ast = _parse_sql(standard_sql)
    if ast is None:
        return
    window = ast.find(exp.Window)
    if window is None:
        return
    source, _ = _window_source_selects(ast, window)
    table_name = _norm_name(source.name) if isinstance(source, exp.Table) else ""
    target = next((name for name in data if not table_name or _norm_name(name) == table_name), None)
    rows = data.get(target or "")
    if not rows or len(rows) < 3:
        return
    lookup = _column_lookup(list(rows[0]))
    partition_columns = [
        lookup.get(_norm_name(column.name))
        for column in _window_partition_columns(window)
    ]
    order = window.args.get("order")
    order_columns = []
    if isinstance(order, exp.Order):
        for item in order.expressions or []:
            expression = item.this if isinstance(item, exp.Ordered) else item
            if isinstance(expression, exp.Column):
                actual = lookup.get(_norm_name(expression.name))
                if actual:
                    order_columns.append(actual)
    partition_columns = [item for item in partition_columns if item]
    standard_over = window_diff.extra.get("standard_over") or {}
    student_over = window_diff.extra.get("student_over") or {}
    if not isinstance(standard_over, dict):
        standard_over = {}
    if not isinstance(student_over, dict):
        student_over = {}
    standard_partition_names = {
        _norm_name(str(item).split(".")[-1])
        for item in (standard_over.get("partition_by") or ())
    }
    student_partition_names = {
        _norm_name(str(item).split(".")[-1])
        for item in (student_over.get("partition_by") or ())
    }
    if standard_partition_names != student_partition_names:
        # Make the first two rows equal under the standard partition and
        # different under the student's added/replaced partition key.  This
        # is the minimal witness for SUM(...) OVER (PARTITION BY a,b) versus
        # SUM(...) OVER (PARTITION BY a), and it also works for a replaced
        # partition column.  It is deliberately independent of ORDER BY.
        standard_columns = [
            lookup.get(name)
            for name in standard_partition_names
            if lookup.get(name)
        ]
        standard_only_columns = [
            lookup.get(name)
            for name in standard_partition_names - student_partition_names
            if lookup.get(name)
        ]
        student_only_columns = [
            lookup.get(name)
            for name in student_partition_names - standard_partition_names
            if lookup.get(name)
        ]
        for position, row in enumerate(rows[:2]):
            for column_index, column in enumerate(standard_columns):
                row[column] = _group_probe_value(column, 0, column_index + 90)
            for column_index, column in enumerate(standard_only_columns):
                row[column] = _group_probe_value(column, position, column_index + 95)
            for column_index, column in enumerate(student_only_columns):
                row[column] = _group_probe_value(column, position, column_index + 100)
    if not order_columns:
        return
    standard_function = str(
        window_diff.extra.get("standard_function") or ""
    ).upper()
    student_function = str(
        window_diff.extra.get("student_function") or ""
    ).upper()
    if not standard_function and isinstance(window.this, exp.Window):
        standard_function = type(window.this.this).__name__.upper()
    if not student_function:
        student_window = (
            window_diff.student_node
            if isinstance(window_diff.student_node, exp.Window)
            else None
        )
        if isinstance(student_window, exp.Window):
            student_function = type(student_window.this).__name__.upper()
        else:
            student_function = standard_function
    standard_order_items = tuple(standard_over.get("order_items") or ())
    student_order_items = tuple(student_over.get("order_items") or ())
    direction_gap = any(
        len(standard) >= 2
        and len(student) >= 2
        and standard[0] == student[0]
        and bool(standard[1]) != bool(student[1])
        for standard, student in zip(standard_order_items, student_order_items)
    )
    null_placement_gap = any(
        len(standard) >= 3
        and len(student) >= 3
        and standard[0] == student[0]
        and bool(standard[2]) != bool(student[2])
        for standard, student in zip(standard_order_items, student_order_items)
    )
    rank_gap_required = {standard_function, student_function} & {"RANK", "DENSE_RANK"}
    # Ranking and value/frame changes need a three-row partition: two peer
    # rows plus a distinct trailing row.  With only two rows the generated
    # world can make FIRST/LAST and cumulative-vs-total SUM accidentally
    # equal, even though the AST obligation is correctly identified.
    value_or_frame_gap = (
        window_diff.diff_type == "window_function_changed"
        and {standard_function, student_function} & {"FIRST_VALUE", "LAST_VALUE"}
    ) or (
        window_diff.diff_type == "window_over_changed"
        and (
            standard_over.get("order") != student_over.get("order")
            or standard_over.get("frame") != student_over.get("frame")
        )
    )
    # A ROW_NUMBER direction change needs four ordered rows to make the
    # second-ranked row differ (three rows would leave the middle row the same
    # in both directions).  An explicit NULLS FIRST/LAST change instead needs
    # one NULL plus two non-NULL values.  Keep these two witness shapes
    # separate: treating the implicit NULL placement that accompanies ASC /
    # DESC as an explicit-null test would hide the direction mutation.
    if direction_gap and standard_function == student_function == "ROW_NUMBER":
        same_partition_count = 4
    elif null_placement_gap and not direction_gap:
        same_partition_count = 3
    else:
        same_partition_count = 3 if (rank_gap_required or value_or_frame_gap) else 2
    same_partition_count = min(len(rows), same_partition_count)
    if partition_columns:
        for row in rows[:same_partition_count]:
            for position, column in enumerate(partition_columns):
                row[column] = _group_probe_value(column, 0, position + 90)
        for row in rows[same_partition_count:same_partition_count + 1]:
            for position, column in enumerate(partition_columns):
                row[column] = _group_probe_value(column, 1, position + 90)
    descending = []
    if isinstance(window.args.get("order"), exp.Order):
        descending = [
            bool(item.args.get("desc"))
            for item in window.args.get("order").expressions or []
        ]
    for position, column in enumerate(order_columns):
        is_desc = descending[position] if position < len(descending) else False
        if direction_gap and standard_function == student_function == "ROW_NUMBER":
            # Monotone, distinct values expose ASC/DESC through any outer
            # ``rn = k`` filter while leaving partition membership unchanged.
            for index, row in enumerate(rows[:same_partition_count]):
                if _is_numeric_column(column):
                    row[column] = 1000 + index * 100 + position
                else:
                    row[column] = f"__window_order_{index:03d}_{position}__"
            continue
        if null_placement_gap and not direction_gap:
            rows[0][column] = None
            for index, row in enumerate(rows[1:same_partition_count], start=1):
                if _is_numeric_column(column):
                    row[column] = 1000 + index * 100 + position
                else:
                    row[column] = f"__window_order_{index:03d}_{position}__"
            continue
        if _is_numeric_column(column):
            tied = 1000 + position * 10
            trailing = tied - 100 if is_desc else tied + 100
        else:
            tied = _group_probe_value(column, 0, position + 95)
            trailing = _group_probe_value(column, 1, position + 95)
        rows[0][column] = tied
        rows[1][column] = tied
        if len(rows) >= 3:
            rows[2][column] = trailing


def transpile_to_sqlite(sql: str) -> str | None:
    """Normalize one validated SQLite query for deterministic execution."""
    prepared_sql = _prepare_sqlite_source(sql)
    manual = _manual_sqlite_compat(prepared_sql)
    try:
        candidates = sqlglot.transpile(
            prepared_sql,
            read="sqlite",
            write="sqlite",
            identify=True,
            error_level=ErrorLevel.IGNORE,
        )
        if candidates:
            return _sqlite_compat(candidates[0])
    except Exception:
        pass
    return _manual_sqlite_compat(prepared_sql)


def _prepare_executable_sql_pair(
    standard_sql: str,
    student_sql: str,
    *,
    standard_ast: exp.Expression | None = None,
    student_ast: exp.Expression | None = None,
) -> tuple[str | None, str | None]:
    standard_source = _normalize_sqlite_order_aliases(
        standard_sql,
        standard_ast,
    )
    student_source = _normalize_sqlite_order_aliases(
        student_sql,
        student_ast,
    )
    return (
        transpile_to_sqlite(standard_source),
        transpile_to_sqlite(student_source),
    )


def _subquery_ast_diffs(
    standard_ast: exp.Expression,
    student_ast: exp.Expression,
    depth: int = 1,
) -> list[ASTDiffNode]:
    """Recursively compare subqueries between standard and student SQL.

    Pairs subqueries left-to-right, runs all diff functions on each paired
    inner SELECT, and reports added/removed subqueries when counts differ.

    ``depth`` tracks nesting level for downstream context.
    """
    std_subs = _collect_subqueries(standard_ast)
    stu_subs = _collect_subqueries(student_ast)
    # Keep the scope-resolved physical endpoints beside the paired subquery
    # nodes.  The broad correlated summary is still one atomic diagnostic, but
    # without these endpoints Phase2 cannot build a valid membership-path
    # obligation for an operator change such as ``=`` -> ``<>``.
    standard_correlation_links = {
        id(inner): (outer_ref, inner_ref)
        for outer_ref, inner_ref, inner in _correlated_subquery_links(standard_ast)
    }
    student_correlation_links = {
        id(inner): (outer_ref, inner_ref)
        for outer_ref, inner_ref, inner in _correlated_subquery_links(student_ast)
    }

    diffs: list[ASTDiffNode] = []
    paired = min(len(std_subs), len(stu_subs))

    # Recursively diff each paired subquery
    for i in range(paired):
        inner_diffs = _diff_inner(std_subs[i], stu_subs[i], depth=depth)
        correlation_relevant_types = {
            "where_changed",
            "predicate_added",
            "predicate_missing",
            "comparison_operator_changed",
            "comparison_left_column_changed",
            "logical_operator_changed",
            "logical_precedence_tree_changed",
            "join_on_changed",
            "join_key_column_changed",
            "in_predicate_negation_changed",
            "null_predicate_negation_changed",
        }
        if (
            inner_diffs
            and any(diff.diff_type in correlation_relevant_types for diff in inner_diffs)
            and (_subquery_is_correlated(std_subs[i]) or _subquery_is_correlated(stu_subs[i]))
        ):
            # A correlated subquery is not itself a changed correlation
            # predicate.  DISTINCT, projection, ORDER or aggregate edits in
            # its body must keep their own atomic rule; otherwise this broad
            # summary creates unrelated obligations and weakens mutation
            # attribution for otherwise valid student errors.
            extra = {
                "subquery_depth": depth,
                "standard_sql": _sql_of(std_subs[i]),
                "student_sql": _sql_of(stu_subs[i]),
            }
            standard_link = standard_correlation_links.get(id(std_subs[i]))
            student_link = student_correlation_links.get(id(stu_subs[i]))
            if standard_link is not None:
                extra.update({
                    "standard_source_table": standard_link[0][0],
                    "standard_membership_table": standard_link[1][0],
                    "standard_outer_column": standard_link[0][1],
                    "standard_membership_column": standard_link[1][1],
                })
            if student_link is not None:
                extra.update({
                    "student_source_table": student_link[0][0],
                    "student_membership_table": student_link[1][0],
                    "student_outer_column": student_link[0][1],
                    "student_membership_column": student_link[1][1],
                })
            diffs.append(ASTDiffNode(
                clause_category="CORRELATED SUBQUERY",
                diff_type="correlated_predicate_changed",
                standard_node=std_subs[i],
                student_node=stu_subs[i],
                knowledge_point_id="subquery-correlated",
                severity=0.78,
                extra=extra,
            ))
        diffs.extend(inner_diffs)

    # Unpaired: student has extra subqueries
    for i in range(paired, len(stu_subs)):
        diffs.append(ASTDiffNode(
            clause_category="SUBQUERY",
            diff_type="subquery_added",
            standard_node=None,
            student_node=stu_subs[i],
            knowledge_point_id="subquery",
            extra={
                "subquery_depth": depth,
                "student_sql": _sql_of(stu_subs[i]),
                "standard_sql": "",
            }
        ))

    # Unpaired: standard has subqueries student removed
    for i in range(paired, len(std_subs)):
        diffs.append(ASTDiffNode(
            clause_category="SUBQUERY",
            diff_type="subquery_removed",
            standard_node=std_subs[i],
            student_node=None,
            knowledge_point_id="subquery",
            extra={
                "subquery_depth": depth,
                "standard_sql": _sql_of(std_subs[i]),
                "student_sql": "",
            }
        ))

    return diffs


def _diff_inner(
    std_inner: exp.Expression,
    stu_inner: exp.Expression,
    depth: int,
) -> list[ASTDiffNode]:
    """Run all diff functions on a paired subquery's inner SELECT, with depth tagging."""
    # If inner SELECTs are textually identical (after normalisation), skip
    if _sql_of(std_inner) == _sql_of(stu_inner):
        return []

    inner_diffs: list[ASTDiffNode] = []
    inner_diffs.extend(_clause_ast_diffs(std_inner, stu_inner))
    inner_diffs.extend(_projection_column_ast_diffs(std_inner, stu_inner))
    inner_diffs.extend(_projection_alias_ast_diffs(std_inner, stu_inner))
    inner_diffs.extend(_function_argument_ast_diffs(std_inner, stu_inner, filter_subqueries=False))
    inner_diffs.extend(_group_by_ast_diffs(std_inner, stu_inner))
    inner_diffs.extend(_having_placement_ast_diffs(std_inner, stu_inner))
    inner_diffs.extend(_order_by_ast_diffs(std_inner, stu_inner))
    inner_diffs.extend(_comparison_ast_diffs(std_inner, stu_inner, filter_subqueries=False))
    inner_diffs.extend(_logical_operator_ast_diffs(std_inner, stu_inner))
    inner_diffs.extend(_join_ast_diffs(std_inner, stu_inner))
    inner_diffs.extend(_aggregate_function_ast_diffs(std_inner, stu_inner, filter_subqueries=False))
    inner_diffs.extend(_set_operator_ast_diffs(std_inner, stu_inner))
    inner_diffs.extend(_window_ast_diffs(std_inner, stu_inner, filter_subqueries=False))
    inner_diffs.extend(_case_ast_diffs(std_inner, stu_inner, filter_subqueries=False))

    # Tag every inner diff with subquery_depth so dedup distinguishes levels
    for diff in inner_diffs:
        if diff.extra is None:
            diff.extra = {}
        diff.extra["subquery_depth"] = depth
        # DISTINCT inside a nested query block must be witnessed at that
        # block.  The previous metadata carried only depth, so downstream
        # validation fell back to the outer result and could hide a real
        # inner duplicate behind an aggregate or another DISTINCT.
        if diff.diff_type == "distinct_changed":
            diff.extra.setdefault("query_scope", "subquery")
            diff.extra.setdefault("standard_query_sql", _sql_of(std_inner))
            diff.extra.setdefault("student_query_sql", _sql_of(stu_inner))
            source = _direct_from_table(std_inner)
            if diff.target_table is None and isinstance(source, exp.Table):
                diff.target_table = source.name
            if not diff.extra.get("standard_projection_columns"):
                projection_columns = tuple(dict.fromkeys(
                    _norm_name(column.name)
                    for expression in (
                        std_inner.expressions
                        if isinstance(std_inner, exp.Select)
                        else ()
                    )
                    for column in expression.find_all(exp.Column)
                    if _nearest_select(column) is std_inner
                ))
                if projection_columns:
                    diff.extra["standard_projection_columns"] = projection_columns

    # Recurse one level deeper for nested subqueries inside this subquery
    inner_diffs.extend(_subquery_ast_diffs(std_inner, stu_inner, depth=depth + 1))

    return inner_diffs


def extract_ast_diffs(
    standard_sql: str,
    student_sql: str,
    schema_catalog: SchemaCatalog | None = None,
) -> list[ASTDiffNode]:
    """Extract focused AST subtree differences used to drive counterexample data generation."""
    standard_ast = _parse_sql(standard_sql)
    student_ast = _parse_sql(student_sql)
    if standard_ast is None or student_ast is None:
        return []

    equivalent_rewrite = _queries_are_supported_equivalent_rewrites(
        standard_ast,
        student_ast,
        schema_catalog=schema_catalog,
    )
    schema_projection_identity = (
        schema_catalog is not None
        and _schema_numeric_projection_identities_equivalent(
            standard_ast,
            student_ast,
            schema_catalog,
        )
    )
    if equivalent_rewrite or schema_projection_identity:
        # Preserve explicitly changed output labels for CFG coverage while
        # keeping alias omission as a proven row-value rewrite.
        alias_diffs = [
            diff
            for diff in _projection_alias_ast_diffs(standard_ast, student_ast)
            if (diff.extra or {}).get("standard_alias")
            and (diff.extra or {}).get("student_alias")
        ]
        return alias_diffs

    diffs: list[ASTDiffNode] = []
    diffs.extend(_clause_ast_diffs(standard_ast, student_ast))
    diffs.extend(_advanced_clause_ast_diffs(standard_ast, student_ast))
    diffs.extend(_projection_column_ast_diffs(standard_ast, student_ast))
    diffs.extend(_projection_alias_ast_diffs(standard_ast, student_ast))
    diffs.extend(_function_argument_ast_diffs(standard_ast, student_ast))
    diffs.extend(_group_by_ast_diffs(standard_ast, student_ast))
    diffs.extend(_having_placement_ast_diffs(standard_ast, student_ast))
    diffs.extend(_order_by_ast_diffs(standard_ast, student_ast))
    diffs.extend(_comparison_ast_diffs(standard_ast, student_ast))
    diffs.extend(_predicate_negation_ast_diffs(standard_ast, student_ast))
    diffs.extend(_logical_operator_ast_diffs(standard_ast, student_ast))
    diffs.extend(_join_ast_diffs(standard_ast, student_ast))
    diffs.extend(_set_operator_ast_diffs(standard_ast, student_ast))
    diffs.extend(_window_ast_diffs(standard_ast, student_ast))
    diffs.extend(_cte_ast_diffs(standard_ast, student_ast))
    diffs.extend(_case_ast_diffs(standard_ast, student_ast))
    diffs.extend(_aggregate_function_ast_diffs(standard_ast, student_ast))
    diffs.extend(_correlated_subquery_context_ast_diffs(standard_ast, student_ast))
    diffs.extend(_subquery_ast_diffs(standard_ast, student_ast))
    diffs.extend(_from_source_ast_diffs(standard_ast, student_ast))
    placement_diffs = _outer_join_predicate_placement_ast_diffs(
        standard_ast,
        student_ast,
    )
    diffs.extend(placement_diffs)
    diffs.extend(
        _specialized_semantic_ast_diffs(
            standard_ast,
            student_ast,
            standard_sql=standard_sql,
            student_sql=student_sql,
        )
    )

    seen: set[tuple[Any, ...]] = set()
    unique: list[ASTDiffNode] = []
    for diff in diffs:
        depth = (diff.extra or {}).get("subquery_depth", 0)
        query_scope = str((diff.extra or {}).get("query_scope") or "")
        graph_payload = ("", "")
        if (
            diff.clause_category == "JOIN ON"
            and diff.standard_node is None
            and diff.student_node is None
        ):
            graph_payload = (
                str((diff.extra or {}).get("standard_sql") or ""),
                str((diff.extra or {}).get("student_sql") or ""),
            )
        key = (
            diff.clause_category,
            diff.diff_type,
            diff.target_column,
            _sql_of(diff.standard_node) if isinstance(diff.standard_node, exp.Expression) else str(diff.standard_node or ""),
            _sql_of(diff.student_node) if isinstance(diff.student_node, exp.Expression) else str(diff.student_node or ""),
            # JOIN graph diffs intentionally have no AST node. Include their
            # rendered predicate payload so missing and added keys are not
            # collapsed into one entry by the generic de-duplication pass.
            *graph_payload,
            depth,
            query_scope,
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(diff)
    nested_atomic_comparisons = [
        diff
        for diff in unique
        if diff.diff_type in {"comparison_operator_changed", "literal_changed"}
        and int((diff.extra or {}).get("subquery_depth") or 0) > 0
        and isinstance(diff.standard_node, exp.Expression)
        and isinstance(diff.student_node, exp.Expression)
    ]
    if len(nested_atomic_comparisons) == 1:
        candidate = nested_atomic_comparisons[0]
        replaced_sql = _mutate_by_node_replacement(
            standard_ast,
            candidate.standard_node,
            candidate.student_node,
        )
        # Both strings come from the ASTs parsed at the beginning of this
        # function.  Comparing same-generation canonical strings avoids
        # introducing a parser-roundtrip normalization difference.
        contains_aggregate = any(
            node.find(exp.AggFunc) is not None
            for node in (candidate.standard_node, candidate.student_node)
        )
        if (
            replaced_sql
            and replaced_sql == _sql_of(student_ast)
            and not contains_aggregate
        ):
            # Clause/projection/correlated-subquery summaries are containers
            # around this one proven atomic edit. Keeping them would create
            # unrelated worlds and duplicate mutation attribution.
            unique = [candidate]
    if placement_diffs:
        dependent_types = {
            "where_changed",
            "predicate_missing",
            "predicate_added",
            "join_on_changed",
            "join_key_column_changed",
        }
        unique = [
            diff for diff in unique if diff.diff_type not in dependent_types
        ]
    if any(diff.diff_type == "aggregate_filter_changed" for diff in unique):
        # A FILTER is part of the aggregate expression, not a top-level WHERE
        # clause.  When it is the only projection change, the generic SELECT
        # and predicate diffs are duplicate descriptions of the same fact.
        if _aggregate_filter_is_only_projection_difference(
            standard_ast,
            student_ast,
        ):
            unique = [
                diff
                for diff in unique
                if diff.diff_type == "aggregate_filter_changed"
            ]
    if any(
        diff.diff_type == "boolean_projection_truth_test_changed"
        for diff in unique
    ):
        # The focused detector emits this diff only when normalizing one
        # top-level ``predicate IS TRUE`` projection makes the complete
        # queries equal.  Generic projection/column diffs are therefore
        # dependent descriptions of the same atomic semantic change.
        unique = [
            diff
            for diff in unique
            if diff.diff_type == "boolean_projection_truth_test_changed"
        ]
    if any(
        diff.diff_type == "subquery_membership_key_changed"
        for diff in unique
    ):
        unique = [
            diff
            for diff in unique
            if diff.diff_type == "subquery_membership_key_changed"
        ]
    if any(
        diff.diff_type == "null_sensitive_antijoin_equivalence"
        for diff in unique
    ):
        # The focused detector only emits after proving that replacing the
        # root NOT IN/NOT EXISTS predicates makes the complete queries equal,
        # including identical inner filters and correlation keys. Generic
        # WHERE, projection and correlation diffs are dependent containers.
        unique = [
            diff
            for diff in unique
            if diff.diff_type == "null_sensitive_antijoin_equivalence"
        ]
    return unique


def _window_ast_diffs(
    standard_ast: exp.Expression,
    student_ast: exp.Expression,
    filter_subqueries: bool = True,
) -> list[ASTDiffNode]:
    _skip = _is_inside_subquery if filter_subqueries else (lambda _: False)
    std_windows = [node for node in standard_ast.find_all(exp.Window) if not _skip(node)]
    stu_windows = [node for node in student_ast.find_all(exp.Window) if not _skip(node)]
    diffs: list[ASTDiffNode] = []
    for std_node, stu_node in zip(std_windows, stu_windows):
        std_func = _function_name(std_node.this) if isinstance(std_node.this, exp.Expression) else ""
        stu_func = _function_name(stu_node.this) if isinstance(stu_node.this, exp.Expression) else ""
        if std_func != stu_func:
            std_source, _ = _window_source_selects(standard_ast, std_node)
            stu_source, _ = _window_source_selects(student_ast, stu_node)
            diffs.append(ASTDiffNode(
                clause_category="WINDOW",
                diff_type="window_function_changed",
                standard_node=std_node.this,
                student_node=stu_node.this,
                knowledge_point_id="window-row-number",
                severity=0.76,
                extra={
                    "standard_function": std_func,
                    "student_function": stu_func,
                    "standard_over": _window_spec(std_node),
                    "student_over": _window_spec(stu_node),
                    "standard_window_source_table": (
                        std_source.name if isinstance(std_source, exp.Table) else ""
                    ),
                    "student_window_source_table": (
                        stu_source.name if isinstance(stu_source, exp.Table) else ""
                    ),
                    "standard_sql": _sql_of(std_node.this),
                    "student_sql": _sql_of(stu_node.this),
                },
            ))
        std_spec = _window_spec(std_node)
        stu_spec = _window_spec(stu_node)
        if std_spec != stu_spec:
            std_source, _ = _window_source_selects(standard_ast, std_node)
            stu_source, _ = _window_source_selects(student_ast, stu_node)
            diffs.append(ASTDiffNode(
                clause_category="WINDOW",
                diff_type="window_over_changed",
                standard_node=std_node,
                student_node=stu_node,
                knowledge_point_id="window-row-number",
                extra={
                    "standard_over": std_spec,
                    "student_over": stu_spec,
                    "standard_window_source_table": (
                        std_source.name if isinstance(std_source, exp.Table) else ""
                    ),
                    "student_window_source_table": (
                        stu_source.name if isinstance(stu_source, exp.Table) else ""
                    ),
                    "standard_sql": _sql_of(std_node),
                    "student_sql": _sql_of(stu_node),
                },
            ))
    if len(std_windows) != len(stu_windows):
        # A window can disappear when a CASE branch or projection is removed.
        # Preserve the surviving OVER topology on the count-level diff so the
        # witness planner can still materialize partition/order/tie paths.  The
        # previous count-only payload left every field empty, which made the
        # validator report ``window_table_missing`` even when the base table
        # and an executable window were present.
        std_reference = std_windows[0] if std_windows else None
        stu_reference = stu_windows[0] if stu_windows else None
        std_source, _ = (
            _window_source_selects(standard_ast, std_reference)
            if std_reference is not None
            else (None, None)
        )
        stu_source, _ = (
            _window_source_selects(student_ast, stu_reference)
            if stu_reference is not None
            else (None, None)
        )
        diffs.append(ASTDiffNode(
            clause_category="WINDOW",
            diff_type="window_over_changed",
            standard_node=std_reference,
            student_node=stu_reference,
            knowledge_point_id="window-row-number",
            extra={
                "standard_count": len(std_windows),
                "student_count": len(stu_windows),
                "standard_over": _window_spec(std_reference) if std_reference is not None else {},
                "student_over": _window_spec(stu_reference) if stu_reference is not None else {},
                "standard_window_source_table": (
                    std_source.name if isinstance(std_source, exp.Table) else ""
                ),
                "student_window_source_table": (
                    stu_source.name if isinstance(stu_source, exp.Table) else ""
                ),
                "standard_sql": " | ".join(_sql_of(node) for node in std_windows),
                "student_sql": " | ".join(_sql_of(node) for node in stu_windows),
            },
        ))
    return diffs


def _dynamic_row_count(
    max_rows_per_table: int,
    standard_sql: str,
    student_sql: str,
    ast_diffs: list[dict[str, Any]],
) -> int:
    base = max(4, max_rows_per_table)
    required = base

    count_specs = [
        spec
        for sql in (standard_sql, student_sql)
        for spec in _extract_having_aggregate_specs(sql)
        if spec.get("agg") == "COUNT"
    ]
    for spec in count_specs:
        boundary = int(spec["boundary"])
        # Need groups at boundary, boundary+1, and boundary-1 to distinguish
        # >= vs > and <= vs < operators.  boundary*2+1 rows allows three groups.
        required = max(required, max(1, boundary) * 2 + 1)

    for sql in (standard_sql, student_sql):
        required = max(required, _limit_offset_required_rows(sql))
        ast = _parse_sql(sql)
        if ast is not None and ast.find(exp.Lag):
            # LAG(…, 2) comparison probes need an equality row plus at least
            # two later positive rows so DISTINCT removal is observable too.
            required = max(required, 6)
        if ast is not None:
            windows = _window_alias_map(ast)
            for alias, comparisons in _window_comparison_specs(ast, set(windows)).items():
                window = windows.get(alias)
                if window is None or not isinstance(window.this, exp.RowNumber):
                    continue
                for _, boundary in comparisons:
                    required = max(required, max(3, int(boundary) * 2))

    if any(diff.get("clause") == "LIMIT" for diff in ast_diffs):
        required = max(required, 6)
    return min(_MAX_WITNESS_ROWS_PER_TABLE, required)


def _apply_join_key_drift(rows: list[dict[str, Any]], columns: list[str], shared_values: dict[str, list[Any]]) -> None:
    by_group: dict[str, list[str]] = {}
    for col in columns:
        by_group.setdefault(_join_group_key(col), []).append(col)
    for group, group_cols in by_group.items():
        if len(group_cols) < 2:
            continue
        pool = shared_values.get(group)
        if not pool:
            continue
        for offset, col in enumerate(group_cols[1:], 1):
            for idx, row in enumerate(rows):
                row[col] = pool[(idx + offset) % len(pool)]
            if rows and not _is_primary_key_candidate(
                # Best-effort table name: first table in shared_values schema
                "", col, columns
            ):
                rows[-1][col] = None


def _apply_cross_table_having_probe(
    data: dict[str, list[dict[str, Any]]],
    standard_sql: str,
    student_sql: str,
    ast_diffs: list[ASTDiffNode],
) -> None:
    if not any(diff.clause_category in {"HAVING", "PREDICATE"} for diff in ast_diffs):
        return
    spec = _changed_having_aggregate_spec(standard_sql, student_sql)
    if not spec or spec["agg"] == "COUNT":
        return
    group_location = next(
        (
            (table, _column_lookup(list(rows[0])).get(_norm_name(spec["group_column"])))
            for table, rows in data.items()
            if rows and _norm_name(spec["group_column"]) in _column_lookup(list(rows[0]))
        ),
        None,
    )
    value_location = next(
        (
            (table, _column_lookup(list(rows[0])).get(_norm_name(spec["column"])))
            for table, rows in data.items()
            if rows and _norm_name(spec["column"]) in _column_lookup(list(rows[0]))
        ),
        None,
    )
    if not group_location or not value_location or group_location[0] == value_location[0]:
        return
    group_table, group_col = group_location
    value_table, value_col = value_location
    if not group_col or not value_col:
        return
    _align_standard_join_equalities(data, standard_sql)
    boundary = spec["boundary"]
    targets = [boundary, boundary + 1, boundary - 1]
    for index, row in enumerate(data[value_table]):
        row[value_col] = targets[index % len(targets)]
    for index, row in enumerate(data[group_table]):
        row[group_col] = f"__having_group_{index}__"


def _materialize_scalar_aggregate_boundary_path(
    data: dict[str, list[dict[str, Any]]],
    obligations: list[DistinguishingObligation],
    standard_sql: str,
    *,
    schema_catalog: SchemaCatalog | None = None,
) -> bool:
    """Materialize a reachable outer row equal to a scalar aggregate result.

    This is deliberately bounded to one selected row per physical table.  All
    remaining aggregate-measure rows receive safe numeric values, while local
    predicates exclude them from filtered paths.  The scalar is then executed
    against the candidate database before the outer boundary cell is written.
    """
    specs = [
        (obligation, constraint)
        for obligation in obligations
        for constraint in obligation.hard_constraints
        if constraint.kind == "scalar_subquery_boundary_path"
    ]
    if not specs:
        return False
    ast = _parse_sql(standard_sql)
    if ast is None:
        return False

    for obligation, spec in specs:
        metadata = dict(spec.metadata)
        expected_function = str(
            metadata.get("standard_scalar_aggregate_function") or ""
        ).upper()
        for outer_select in ast.find_all(exp.Select):
            for comparison in outer_select.find_all(
                exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE
            ):
                if comparison.find_ancestor(exp.Select) is not outer_select:
                    continue
                parts = _comparison_subquery_parts(comparison)
                if parts is None:
                    continue
                subquery, outer_column = parts
                inner_select = (
                    subquery.this
                    if isinstance(subquery.this, exp.Select)
                    else subquery.find(exp.Select)
                )
                if not isinstance(inner_select, exp.Select):
                    continue
                aggregate = next(
                    (
                        inner_select.find(kind)
                        for kind in (exp.Avg, exp.Max, exp.Min, exp.Sum)
                        if inner_select.find(kind) is not None
                    ),
                    None,
                )
                if aggregate is None or (
                    expected_function
                    and type(aggregate).__name__.upper() != expected_function
                ):
                    continue
                measure = aggregate.find(exp.Column)
                if not isinstance(measure, exp.Column):
                    continue
                inner_ref = _column_ref_in_select_data(
                    data, measure, inner_select
                )
                outer_ref = _column_ref_in_select_data(
                    data, outer_column, outer_select
                )
                inner_actual = (
                    _actual_data_ref(data, inner_ref) if inner_ref else None
                )
                outer_actual = (
                    _actual_data_ref(data, outer_ref) if outer_ref else None
                )
                if inner_actual is None or outer_actual is None:
                    continue
                inner_rows, measure_column = inner_actual
                outer_rows, outer_column_name = outer_actual
                if not inner_rows or not outer_rows:
                    continue

                with write_owner(
                    f"materializer:{obligation.id}:scalar_aggregate_boundary"
                ):
                    _materialize_select_row_path(
                        data,
                        inner_select,
                        exclude_other_rows=True,
                        schema_catalog=schema_catalog,
                    )
                    _materialize_select_row_path(
                        data,
                        outer_select,
                        exclude_other_rows=True,
                        schema_catalog=schema_catalog,
                    )
                    for index, row in enumerate(inner_rows):
                        if isinstance(aggregate, exp.Max):
                            row[measure_column] = 50 if index == 0 else 49
                        elif isinstance(aggregate, exp.Min):
                            row[measure_column] = 50 if index == 0 else 51
                        elif isinstance(aggregate, exp.Avg):
                            row[measure_column] = 50
                        else:
                            row[measure_column] = 50 if index == 0 else 0

                    schema = {
                        table_name: list(rows[0])
                        for table_name, rows in data.items()
                        if rows
                    }
                    try:
                        _columns, scalar_rows = _execute_sqlite(
                            schema,
                            data,
                            inner_select.sql(dialect="sqlite"),
                            schema_types=(
                                schema_catalog.as_legacy_types()
                                if schema_catalog is not None
                                else None
                            ),
                        )
                    except Exception:
                        continue
                    if not scalar_rows or not scalar_rows[0]:
                        continue
                    boundary = scalar_rows[0][0]
                    if boundary is None:
                        continue
                    outer_rows[0][outer_column_name] = boundary
                return True
    return False


def _apply_nested_membership_chain_probe(
    data: dict[str, list[dict[str, Any]]],
    standard_sql: str,
    student_sql: str,
) -> None:
    """Build end-to-end value paths through arbitrarily nested IN queries."""
    for path_index, sql in enumerate((standard_sql, student_sql)):
        ast = _parse_sql(sql)
        if not ast:
            continue
        links: list[tuple[tuple[str, str], tuple[str, str], exp.Select]] = []
        for in_node in ast.find_all(exp.In):
            query = in_node.args.get("query")
            inner_select = query.this if isinstance(query, exp.Subquery) else None
            outer_select = in_node.find_ancestor(exp.Select)
            if not isinstance(in_node.this, exp.Column) or not isinstance(inner_select, exp.Select):
                continue
            if not isinstance(outer_select, exp.Select) or not inner_select.selects:
                continue
            projected = inner_select.selects[0]
            projected = projected.this if isinstance(projected, exp.Alias) else projected
            if not isinstance(projected, exp.Column):
                continue
            outer_ref = _column_ref_in_select(in_node.this, outer_select)
            inner_ref = _column_ref_in_select(projected, inner_select)
            if outer_ref and inner_ref:
                links.append((outer_ref, inner_ref, inner_select))
        if not links:
            continue

        if len(links) == 1:
            outer_ref, _inner_ref, inner_select = links[0]
            if inner_select.find(exp.Subquery) is None or inner_select.find(exp.AggFunc) is None:
                continue
            executable = transpile_to_sqlite(_sql_of(inner_select))
            outer_actual = _actual_data_ref(data, outer_ref)
            if not executable or not outer_actual:
                continue
            schema = {
                table_name: list(rows[0])
                for table_name, rows in data.items()
                if rows
            }
            try:
                _, inner_results = _execute_sqlite(schema, data, executable)
            except Exception:
                continue
            if not inner_results or not inner_results[0]:
                continue
            outer_rows, outer_column = outer_actual
            if path_index < len(outer_rows):
                outer_rows[path_index][outer_column] = inner_results[0][0]
            continue

        for outer_ref, inner_ref, inner_select in reversed(links):
            # Materialize every local literal predicate before copying values
            # across the membership chain.  This keeps both sides of a
            # standard/student literal change reachable instead of allowing a
            # later generic membership probe to erase the student's path.
            for comparison in inner_select.find_all(exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE):
                if comparison.find_ancestor(exp.Select) is inner_select:
                    _set_select_local_literal_predicates(data, inner_select, path_index)
            outer_actual = _actual_data_ref(data, outer_ref)
            inner_actual = _actual_data_ref(data, inner_ref)
            if not outer_actual or not inner_actual:
                continue
            outer_rows, outer_column = outer_actual
            inner_rows, inner_column = inner_actual
            if path_index >= len(outer_rows) or path_index >= len(inner_rows):
                continue
            inner_value = inner_rows[path_index][inner_column]
            outer_rows[path_index][outer_column] = inner_value
            _set_select_local_literal_predicates(data, inner_select, path_index)


def _build_shared_values(schema: dict[str, list[str]], row_count: int) -> dict[str, list[Any]]:
    """
    拓扑对齐机制：识别 schema 中的连接键字段，并为具有关联性的列建立共享值池，防止 JOIN 时出现空关联。
    Topology alignment: builds shared values groups for join keys across tables to avoid empty JOIN outputs.
    """
    groups: dict[str, list[Any]] = {}
    for columns in schema.values():
        for col in columns:
            # _join_group_key 会提取列的根部语义（例如 e_id, s_id 均归类为 id）
            key = _join_group_key(col)
            if key not in groups:
                groups[key] = [_seed_value(col, idx) for idx in range(row_count)]
    return groups


def _typed_base_value(
    table: str,
    col: str,
    idx: int,
    shared_values: dict[str, list[Any]],
    schema_catalog: SchemaCatalog | None = None,
) -> Any:
    """Generate a base value using catalog type information when available."""
    key = _join_group_key(col)
    value = (
        shared_values[key][idx % len(shared_values[key])]
        if key in shared_values and shared_values[key]
        else _seed_value(col, idx)
    )
    kind = _authoritative_column_kind(table, col, schema_catalog)
    return _coerce_typed_seed(value, kind, col, idx) if kind else value


def _execute_sqlite(
    schema: dict[str, list[str]],
    rows: dict[str, list[dict[str, Any]]],
    sql: str,
    *,
    schema_types: dict[str, dict[str, str]] | None = None,
) -> tuple[list[str], list[tuple[Any, ...]]]:
    """Execute one query in the bounded in-memory SQLite sandbox.

    SQLite delegates its native infix ``REGEXP`` operator to an application
    callback, so that one callback is part of the execution contract.  No
    functions from other SQL engines are emulated here.
    """
    conn = sqlite3.connect(":memory:")
    try:
        def sql_regexp(pattern: Any, value: Any) -> int | None:
            # SQLite's infix REGEXP operator invokes regexp(pattern, value).
            matched = regex_matches(pattern, value)
            return None if matched is None else int(matched)

        conn.create_function("REGEXP", 2, sql_regexp)

        cur = conn.cursor()
        # Build only the bounded witness tables selected by Phase 1.
        for table, columns in schema.items():
            if table not in rows:
                continue
            table_type_key = next(
                (
                    name for name in (schema_types or {})
                    if _norm_name(name) == _norm_name(table)
                ),
                None,
            )
            declared_types = (schema_types or {}).get(table_type_key or "", {})
            normalized_types = {
                _norm_name(column): declared
                for column, declared in declared_types.items()
            }
            defs = ", ".join(
                f'"{col}" {_sqlite_declared_affinity(col, normalized_types.get(_norm_name(col)))}'
                for col in columns
            )
            cur.execute(f'CREATE TABLE "{table}" ({defs})')
            placeholders = ", ".join("?" for _ in columns)
            quoted_cols = ", ".join(f'"{col}"' for col in columns)
            insert_sql = f'INSERT INTO "{table}" ({quoted_cols}) VALUES ({placeholders})'
            values = [tuple(row.get(col) for col in columns) for row in rows[table]]
            if values:
                cur.executemany(insert_sql, values)

        # 3. Execute under both a VM-instruction and wall-clock budget. The
        # old one-shot 100k guard rejected legitimate 32-row nested
        # correlated teaching queries. One million bounded instructions are
        # still small, while the 0.5 second deadline independently stops
        # infinite recursive CTEs and accidental Cartesian explosions.
        progress_calls = 0
        deadline = time.monotonic() + _SQLITE_EXECUTION_TIME_BUDGET_SECONDS

        def abort_expensive_query() -> int:
            nonlocal progress_calls
            progress_calls += 1
            instruction_limit_reached = (
                progress_calls * _SQLITE_PROGRESS_GRANULARITY
                >= _SQLITE_VM_INSTRUCTION_BUDGET
            )
            return int(
                instruction_limit_reached
                or time.monotonic() >= deadline
            )

        conn.set_progress_handler(
            abort_expensive_query,
            _SQLITE_PROGRESS_GRANULARITY,
        )

        # 4. 执行 SQL 并读取数据列和数据行
        cur.execute(sql)
        result_rows = cur.fetchall()
        result_cols = [item[0] for item in (cur.description or [])]
        return result_cols, [tuple(_normalize_cell(cell) for cell in row) for row in result_rows]
    finally:
        conn.close()


def _execute_mutation_case(
    *,
    schema: dict[str, list[str]],
    rows: dict[str, list[dict[str, Any]]],
    clause: str,
    knowledge_point_id: str,
    replacement_sql: str | None,
    removal_sql: str | None,
    standard_columns: list[str],
    standard_rows: list[tuple[Any, ...]],
    ordered: bool,
    schema_types: dict[str, dict[str, str]] | None = None,
    action: str = "replace_student_clause_with_standard_clause",
    mutation_scope: list[str] | None = None,
    query_scope: str = "root",
    dependent_changes: list[str] | None = None,
    allow_equivalent_original_fix: bool = False,
) -> dict[str, Any]:
    test: dict[str, Any] = {
        "clause": clause,
        "knowledge_point_id": knowledge_point_id,
        "action": action,
        "mutation_scope": mutation_scope or [clause],
        "query_scope": query_scope,
        "dependent_changes": dependent_changes or [],
        "execution_backend": "sqlite",
        "sql_dialect": "sqlite",
        "replacement_source_sql": replacement_sql,
        "replacement_sql": None,
        "replacement_sqlite": None,
        "replacement_exec_ok": False,
        "replacement_equivalent": None,
        "fixed_by_replacement": False,
        "removal_source_sql": removal_sql,
        "removal_sql": None,
        "removal_sqlite": None,
        "removal_exec_ok": False,
        "removed_student_clause_equivalent": None,
        "error": None,
    }
    if replacement_sql:
        try:
            executable_sql = _prepare_mutation_sql(replacement_sql)
            test["replacement_sql"] = executable_sql
            test["replacement_sqlite"] = executable_sql
            if executable_sql:
                cols, result_rows = _execute_sqlite(
                    schema,
                    rows,
                    executable_sql,
                    schema_types=schema_types or {},
                )
                equivalent = _rows_equivalent(standard_columns, standard_rows, cols, result_rows, ordered)
                test["replacement_exec_ok"] = True
                test["replacement_equivalent"] = equivalent
                test["fixed_by_replacement"] = (
                    equivalent
                    and (
                        not _MUTATION_ORIGINAL_EQUIVALENT.get()
                        or allow_equivalent_original_fix
                    )
                )
        except Exception as exc:
            if _is_platform_execution_error(exc):
                raise
            test["error"] = f"replacement_failed: {exc}"
    if removal_sql:
        try:
            executable_sql = _prepare_mutation_sql(removal_sql)
            test["removal_sql"] = executable_sql
            test["removal_sqlite"] = executable_sql
            if executable_sql:
                cols, result_rows = _execute_sqlite(
                    schema,
                    rows,
                    executable_sql,
                    schema_types=schema_types or {},
                )
                equivalent = _rows_equivalent(standard_columns, standard_rows, cols, result_rows, ordered)
                test["removal_exec_ok"] = True
                test["removed_student_clause_equivalent"] = equivalent
        except Exception as exc:
            if _is_platform_execution_error(exc):
                raise
            prev = test.get("error")
            test["error"] = f"{prev}; removal_failed: {exc}" if prev else f"removal_failed: {exc}"
    return test


def _run_join_clause_mutations(
    *,
    schema: dict[str, list[str]],
    rows: dict[str, list[dict[str, Any]]],
    standard_ast: exp.Expression,
    student_ast: exp.Expression,
    standard_columns: list[str],
    standard_rows: list[tuple[Any, ...]],
    ordered: bool,
    schema_types: dict[str, dict[str, str]] | None = None,
) -> list[dict[str, Any]]:
    """Mutate one missing/extra JOIN and only its direct dependencies."""
    tests: list[dict[str, Any]] = []
    for query_scope, standard_query, student_query in _paired_query_blocks(
        standard_ast,
        student_ast,
    ):
        if not isinstance(standard_query, exp.Select) or not isinstance(student_query, exp.Select):
            continue
        standard_joins = list(standard_query.args.get("joins") or [])
        student_joins = list(student_query.args.get("joins") or [])
        if not standard_joins and not student_joins:
            continue
        standard_sources = [_sql_of(join.this) for join in standard_joins]
        student_sources = [_sql_of(join.this) for join in student_joins]
        if len(standard_joins) == len(student_joins) and standard_sources == student_sources:
            continue

        mutated = student_ast.copy()
        target_scope = _query_block_scope_key(student_query)
        mutated_select = next(
            (
                node
                for node in mutated.walk()
                if isinstance(node, exp.Select)
                and _query_block_scope_key(node) == target_scope
            ),
            None,
        )
        if not isinstance(mutated_select, exp.Select):
            continue

        dependent_changes: list[str] = []
        standard_from = standard_query.args.get("from_")
        student_from = student_query.args.get("from_")
        if _sql_of(standard_from) != _sql_of(student_from):
            mutated_select.set(
                "from_",
                standard_from.copy() if isinstance(standard_from, exp.Expression) else None,
            )
            dependent_changes.append("FROM ALIAS")
        mutated_select.set("joins", [join.copy() for join in standard_joins])
        if [_sql_of(item) for item in standard_query.expressions] != [
            _sql_of(item) for item in student_query.expressions
        ]:
            mutated_select.set(
                "expressions",
                [item.copy() for item in standard_query.expressions],
            )
            dependent_changes.append("SELECT")
        standard_where = standard_query.args.get("where")
        student_where = student_query.args.get("where")
        if _sql_of(standard_where) != _sql_of(student_where):
            mutated_select.set(
                "where",
                standard_where.copy() if isinstance(standard_where, exp.Expression) else None,
            )
            dependent_changes.append("WHERE")

        reference_join = standard_joins[0] if standard_joins else student_joins[0]
        tests.append(_execute_mutation_case(
            schema=schema,
            rows=rows,
            clause="JOIN",
            knowledge_point_id=_join_type_kp(reference_join),
            replacement_sql=_sql_of(mutated),
            removal_sql=None,
            standard_columns=standard_columns,
            standard_rows=standard_rows,
            ordered=ordered,
            schema_types=schema_types,
            action="restore_join_operator_and_direct_dependencies",
            mutation_scope=["JOIN"],
            query_scope=query_scope,
            dependent_changes=dependent_changes,
        ))
    return tests


def _run_join_structure_mutation(
    *,
    schema: dict[str, list[str]],
    rows: dict[str, list[dict[str, Any]]],
    standard_ast: exp.Expression,
    student_ast: exp.Expression,
    standard_columns: list[str],
    standard_rows: list[tuple[Any, ...]],
    ordered: bool,
    schema_types: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any] | None:
    """Restore a missing/extra JOIN and projection columns that depend on it."""
    standard_select = _top_select(standard_ast)
    student_select = _top_select(student_ast)
    if not isinstance(standard_select, exp.Select) or not isinstance(student_select, exp.Select):
        return None

    standard_joins = list(standard_select.args.get("joins") or [])
    student_joins = list(student_select.args.get("joins") or [])
    if not standard_joins and not student_joins:
        return None
    standard_from = standard_select.args.get("from_")
    student_from = student_select.args.get("from_")
    topology_changed = (
        len(standard_joins) != len(student_joins)
        or _sql_of(standard_from) != _sql_of(student_from)
    )
    if not topology_changed:
        return None

    mutated = student_ast.copy()
    mutated_select = _top_select(mutated)
    if not isinstance(mutated_select, exp.Select):
        return None
    mutated_select.set("from_", standard_from.copy() if standard_from is not None else None)
    mutated_select.set("joins", [join.copy() for join in standard_joins])
    mutated_select.set("expressions", [item.copy() for item in standard_select.expressions])
    standard_where = standard_select.args.get("where")
    mutated_select.set("where", standard_where.copy() if standard_where is not None else None)

    mutation_scope: list[str] = []
    if _sql_of(standard_from) != _sql_of(student_from):
        mutation_scope.append("FROM")
    if [_sql_of(join) for join in standard_joins] != [
        _sql_of(join) for join in student_joins
    ]:
        mutation_scope.append("JOIN")
    if _sql_of(standard_where) != _sql_of(student_select.args.get("where")):
        mutation_scope.append("WHERE")
    if [_sql_of(item) for item in standard_select.expressions] != [
        _sql_of(item) for item in student_select.expressions
    ]:
        mutation_scope.append("SELECT")

    return _execute_mutation_case(
        schema=schema,
        rows=rows,
        clause="JOIN STRUCTURE",
        knowledge_point_id=(
            _join_type_kp(standard_joins[0]) if standard_joins else "join-inner"
        ),
        replacement_sql=_sql_of(mutated),
        removal_sql=None,
        standard_columns=standard_columns,
        standard_rows=standard_rows,
        ordered=ordered,
        schema_types=schema_types,
        action="restore_standard_join_structure_and_dependent_query_shape",
        mutation_scope=mutation_scope,
    )


def _run_aggregate_clause_placement_mutation(
    *,
    schema: dict[str, list[str]],
    rows: dict[str, list[dict[str, Any]]],
    standard_ast: exp.Expression,
    student_ast: exp.Expression,
    standard_columns: list[str],
    standard_rows: list[tuple[Any, ...]],
    ordered: bool,
    schema_types: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any] | None:
    """Move an aggregate predicate from illegal WHERE placement to HAVING."""
    standard_select = _top_select(standard_ast)
    student_select = _top_select(student_ast)
    if not isinstance(standard_select, exp.Select) or not isinstance(student_select, exp.Select):
        return None

    standard_having = standard_select.args.get("having")
    student_having = student_select.args.get("having")
    student_where = student_select.args.get("where")
    if (
        not isinstance(standard_having, exp.Having)
        or student_having is not None
        or not isinstance(student_where, exp.Where)
        or standard_having.find(exp.AggFunc) is None
        or student_where.find(exp.AggFunc) is None
    ):
        return None

    mutated = student_ast.copy()
    mutated_select = _top_select(mutated)
    if not isinstance(mutated_select, exp.Select):
        return None
    standard_where = standard_select.args.get("where")
    mutated_select.set("where", standard_where.copy() if standard_where is not None else None)
    mutated_select.set("having", standard_having.copy())

    return _execute_mutation_case(
        schema=schema,
        rows=rows,
        clause="HAVING",
        knowledge_point_id="having",
        replacement_sql=_sql_of(mutated),
        removal_sql=None,
        standard_columns=standard_columns,
        standard_rows=standard_rows,
        ordered=ordered,
        schema_types=schema_types,
        action="move_aggregate_predicate_from_where_to_having",
        mutation_scope=["HAVING"],
        dependent_changes=["WHERE"],
    )


def _prepare_mutation_sql(
    sql: str,
    *,
    allowed_tables: Iterable[str] | None = None,
) -> str | None:
    # ``allowed_tables`` remains part of the atomic-validator call contract;
    # table ownership was already checked on the validated source AST.
    _ = allowed_tables
    mutation_ast = _parse_sql(sql)
    fixture_sql = _normalize_sqlite_order_aliases(sql, mutation_ast)
    return transpile_to_sqlite(fixture_sql)


def _repair_primary_key_candidate_duplicates(
    data: dict[str, list[dict[str, Any]]],
    schema: dict[str, list[str]],
    *sqls: str,
) -> None:
    grouped_columns = set().union(*(_group_by_columns_for_sql(sql) for sql in sqls)) if sqls else set()
    window_partition_columns: set[tuple[str, str]] = set()
    for sql in sqls:
        ast = _parse_sql(sql)
        if not ast:
            continue
        aliases = _table_aliases(ast)
        for window in ast.find_all(exp.Window):
            for column in window.args.get("partition_by") or []:
                if not isinstance(column, exp.Column):
                    continue
                table_ref = _norm_name(column.table or "")
                window_partition_columns.add((aliases.get(table_ref, table_ref), _norm_name(column.name)))
    replacements: list[tuple[str, str, int, Any, Any]] = []
    for table_name, columns in schema.items():
        rows = data.get(table_name) or []
        pk_col = _primary_key_candidate(columns, table_name)
        if not pk_col:
            continue
        table_norm = _norm_name(table_name)
        pk_norm = _norm_name(pk_col)
        if (table_norm, pk_norm) in window_partition_columns or ("", pk_norm) in window_partition_columns:
            continue
        heuristic_foreign_key = (
            pk_norm != "id"
            and pk_norm not in _table_key_aliases(table_norm)
            and (pk_norm.endswith("_id") or pk_norm.endswith("id"))
        )
        if heuristic_foreign_key and any(
            column == pk_norm and (not table_ref or table_ref == table_norm)
            for table_ref, column in grouped_columns
        ):
            continue
        seen: set[Any] = set()
        for idx, row in enumerate(rows):
            value = row.get(pk_col)
            if value not in seen:
                seen.add(value)
                continue
            replacement = _unique_key_value(pk_col, idx, seen, value)
            row[pk_col] = replacement
            replacements.append((table_name, pk_col, idx, value, replacement))
            seen.add(replacement)

    for parent_table, pk_col, row_idx, old_value, new_value in replacements:
        for table_name, columns in schema.items():
            if table_name == parent_table:
                continue
            child_rows = data.get(table_name) or []
            if row_idx >= len(child_rows):
                continue
            child_pk = _primary_key_candidate(columns, table_name)
            for col in columns:
                if child_pk and _norm_name(col) == _norm_name(child_pk):
                    continue
                if _norm_name(col) == _norm_name(pk_col) and child_rows[row_idx].get(col) == old_value:
                    child_rows[row_idx][col] = new_value


def _join_group_key(col: str) -> str:
    name = _norm_name(col)
    aliases = {
        "id": "id",
        "sid": "id",
        "s_id": "id",
        "iid": "id",
        "i_id": "id",
        "eid": "id",
        "e_id": "id",
        "agent_id": "id",
        "seller_id": "id",
        "user_id": "id",
        "customer_id": "id",
        "empid": "id",
        "emp_id": "id",
        "studentid": "id",
        "student_id": "id",
        "ssn": "ssn",
        "superssn": "ssn",
        "super_ssn": "ssn",
        "mgrssn": "ssn",
        "mgr_ssn": "ssn",
        "essn": "ssn",
        "dno": "department_number",
        "dnumber": "department_number",
        "dnum": "department_number",
        "deptid": "department_number",
        "dept_id": "department_number",
        "department_id": "department_number",
        "pno": "project_number",
        "pnumber": "project_number",
        "proj_id": "project_number",
        "orderid": "order_number",
        "order_id": "order_number",
        "courseid": "course_number",
        "course_id": "course_number",
    }
    return aliases.get(name, name)


def _sqlite_compat(sql: str) -> str:
    # SQLGlot has already parsed and emitted SQLite.  The sole normalization
    # below preserves SQLite's standard OFFSET-without-LIMIT meaning by using
    # its documented ``LIMIT -1`` spelling; no foreign syntax is translated.
    normalized = _restore_sqlite_regexp_callback(sql.rstrip().rstrip(";"))
    normalized = _rewrite_bare_offset(normalized)
    return normalized + ";"


def _restore_sqlite_regexp_callback(sql: str) -> str:
    """Render SQLGlot's REGEXP node through SQLite's callback name.

    SQLGlot emits SQLite's infix ``value REGEXP pattern`` as
    ``REGEXP_LIKE(value, pattern)``.  SQLite itself delegates REGEXP to a
    two-argument callback ordered as ``regexp(pattern, value)``; converting
    only that parsed node keeps execution inside the SQLite contract.
    """
    if "REGEXP_LIKE" not in sql.upper():
        return sql
    try:
        tree = sqlglot.parse_one(sql, read="sqlite", error_level=ErrorLevel.RAISE)
    except Exception:
        return sql

    def replace(node: exp.Expression) -> exp.Expression:
        if not isinstance(node, exp.RegexpLike):
            return node
        return exp.Anonymous(
            this="REGEXP",
            expressions=[node.expression.copy(), node.this.copy()],
        )

    try:
        return tree.transform(replace).sql(dialect="sqlite", identify=True)
    except Exception:
        return sql


def _manual_sqlite_compat(sql: str) -> str | None:
    return _sqlite_compat(sql.strip())


def _is_subquery_correlated(subquery: exp.Subquery) -> bool:
    inner_tables = set()
    for t in subquery.find_all(exp.Table):
        inner_tables.add(_norm_name(t.name))
        if t.alias:
            inner_tables.add(_norm_name(t.alias))
    for col in subquery.find_all(exp.Column):
        if col.table:
            table_ref = _norm_name(col.table)
            if table_ref not in inner_tables:
                return True
    return False


def _find_kp_override(node: exp.Expression | None, default_kp: str) -> str:
    if node is None:
        return default_kp
    if default_kp == "where":
        if node.find(exp.Null) is not None:
            return "comp-null"
        for in_node in node.find_all(exp.In):
            if in_node.args.get("query") is not None and isinstance(in_node.parent, exp.Not):
                return "null-handling"
        subqueries = list(node.find_all(exp.Subquery))
        exists_nodes = list(node.find_all(exp.Exists))
        if any(_is_subquery_correlated(subquery) for subquery in subqueries):
            return "subquery-correlated"
        if any(
            isinstance(exists_node.this, exp.Expression)
            and _subquery_is_correlated(exists_node.this)
            for exists_node in exists_nodes
        ):
            return "subquery-correlated"
        if exists_nodes:
            return "subquery-exists"
        if any(in_node.args.get("query") is not None for in_node in node.find_all(exp.In)):
            return "subquery-in"
        if subqueries:
            return "subquery-scalar"
    curr = node.parent
    while curr is not None:
        if isinstance(curr, exp.CTE):
            with_node = curr.find_ancestor(exp.With)
            if with_node and with_node.args.get("recursive"):
                return "cte-recursive"
            return "cte"
        if isinstance(curr, exp.Subquery):
            if _is_subquery_correlated(curr):
                return "subquery-correlated"
            parent = curr.parent
            if isinstance(parent, exp.In):
                return "subquery-in"
            if isinstance(parent, exp.Exists):
                return "subquery-exists"
            return "subquery-scalar"
        curr = curr.parent
    return default_kp


def _apply_order_by_probes(
    data: dict[str, list[dict[str, Any]]],
    standard_sql: str,
    student_sql: str,
    ast_diffs: list[dict[str, Any]],
) -> None:
    ast = _parse_sql(standard_sql)
    student_ast = _parse_sql(student_sql)
    if not ast:
        return
    order_cols = []

    def ordered_column(node: exp.Expression | None) -> tuple[str, str, bool, bool] | None:
        if isinstance(node, exp.Ordered) and isinstance(node.this, exp.Column):
            return (
                _norm_name(node.this.table or ""),
                _norm_name(node.this.name),
                bool(node.args.get("desc")),
                bool(node.args.get("nulls_first")),
            )
        return None

    def top_ordered(query_ast: exp.Expression | None) -> exp.Ordered | None:
        select = query_ast if isinstance(query_ast, exp.Select) else query_ast.find(exp.Select) if query_ast else None
        order = select.args.get("order") if isinstance(select, exp.Select) else None
        if isinstance(order, exp.Order) and order.expressions and isinstance(order.expressions[0], exp.Ordered):
            return order.expressions[0]
        return None

    std_top_order = top_ordered(ast)
    stu_top_order = top_ordered(student_ast)
    needs_null_probe = bool(
        std_top_order
        and stu_top_order
        and isinstance(std_top_order.this, exp.Column)
        and isinstance(stu_top_order.this, exp.Column)
        and _norm_name(std_top_order.this.name) == _norm_name(stu_top_order.this.name)
        and bool(std_top_order.args.get("nulls_first")) != bool(stu_top_order.args.get("nulls_first"))
    )

    for order in ast.find_all(exp.Order):
        if order.expressions:
            primary = order.expressions[0]
            secondary = order.expressions[1] if len(order.expressions) > 1 else None
            p_info = ordered_column(primary)
            s_info = ordered_column(secondary)
            if p_info:
                order_cols.append((p_info, s_info))
    for window in ast.find_all(exp.Window):
        order = window.find(exp.Order)
        if order and order.expressions:
            primary = order.expressions[0]
            secondary = order.expressions[1] if len(order.expressions) > 1 else None
            p_info = ordered_column(primary)
            s_info = ordered_column(secondary)
            if p_info:
                order_cols.append((p_info, s_info))
    if not order_cols:
        return
    aliases = _table_aliases(ast)
    for p_ref, s_ref in order_cols:
        p_table, p_col, p_desc, _p_nulls_first = p_ref
        resolved_table = aliases.get(p_table, p_table) if p_table else None
        for table_name, rows in data.items():
            if resolved_table and _norm_name(table_name) != resolved_table:
                continue
            norm_columns = { _norm_name(c): c for c in rows[0].keys() } if rows else {}
            if p_col in norm_columns:
                p_name = norm_columns[p_col]
                vals = [r[p_name] for r in rows]
                if _has_diff(ast_diffs, "LIMIT"):
                    new_vals = _extend_order_series(vals, len(rows))
                else:
                    try:
                        sorted_vals = sorted(vals)
                    except Exception:
                        sorted_vals = vals
                    new_vals = []
                    for idx in range(len(rows)):
                        pair_idx = idx // 2 * 2
                        if pair_idx < len(sorted_vals):
                            new_vals.append(sorted_vals[pair_idx])
                        else:
                            new_vals.append(vals[idx])
                for idx, row in enumerate(rows):
                    row[p_name] = new_vals[idx]
                if needs_null_probe and rows:
                    rows[-1][p_name] = None
                if s_ref and s_ref[1] in norm_columns:
                    s_name = norm_columns[s_ref[1]]
                    s_desc = s_ref[2]
                    for idx in range(0, len(rows) - 1, 2):
                        pair = [rows[idx][s_name], rows[idx + 1][s_name]]
                        try:
                            # Insertion order is deliberately opposite to the
                            # reference secondary sort, exposing a missing key.
                            pair.sort(reverse=not s_desc)
                        except Exception:
                            pair.sort(key=lambda value: str(value), reverse=not s_desc)
                        rows[idx][s_name], rows[idx + 1][s_name] = pair
                else:
                    s_name = None

                # Direction changes can be masked when projected text values
                # repeat in a short cycle. Give one non-filter projection a
                # stable row identity so ASC and DESC cannot become palindromic.
                if _has_diff(ast_diffs, "ORDER BY") and not s_ref:
                    select = ast.find(exp.Select)
                    where = ast.find(exp.Where)
                    filter_cols = {
                        _norm_name(col.name)
                        for col in (where.find_all(exp.Column) if where else [])
                    }
                    projected = []
                    for item in (select.expressions if isinstance(select, exp.Select) else []):
                        node = item.this if isinstance(item, exp.Alias) else item
                        if isinstance(node, exp.Column):
                            projected.append(_norm_name(node.name))
                    discriminator = next(
                        (
                            norm_columns[col]
                            for col in projected
                            if col in norm_columns and col != p_col and col not in filter_cols
                        ),
                        None,
                    )
                    if discriminator:
                        for idx, row in enumerate(rows):
                            value = row[discriminator]
                            if isinstance(value, str):
                                row[discriminator] = f"{value}__row_{idx:03d}"
                            elif isinstance(value, (int, float)):
                                row[discriminator] = value * 1000 + idx
    _apply_order_filter_positive_probe(data, ast, ast_diffs)


def _apply_order_filter_positive_probe(
    data: dict[str, list[dict[str, Any]]],
    ast: exp.Expression,
    ast_diffs: list[dict[str, Any]],
) -> None:
    if not _has_diff(ast_diffs, "ORDER BY"):
        return
    ordered_columns: set[str] = set()
    for order in ast.find_all(exp.Order):
        for item in order.expressions or []:
            expression = item.this if isinstance(item, exp.Ordered) else item
            if isinstance(expression, exp.Column):
                ordered_columns.add(_norm_name(expression.name))
    if not ordered_columns:
        return

    for comparison in ast.find_all(exp.GT, exp.GTE, exp.LT, exp.LTE, exp.EQ):
        column = comparison.left if isinstance(comparison.left, exp.Column) else comparison.right
        boundary_node = comparison.right if column is comparison.left else comparison.left
        if not isinstance(column, exp.Column) or _norm_name(column.name) not in ordered_columns:
            continue
        boundary = _expression_static_value(boundary_node)
        if not isinstance(boundary, (int, float, Decimal)):
            continue
        aliases = _table_aliases(ast)
        table_ref = aliases.get(_norm_name(column.table or ""), _norm_name(column.table or ""))
        for table_name, rows in data.items():
            if table_ref and _norm_name(table_name) != table_ref:
                continue
            if len(rows) < 3:
                continue
            actual = _column_lookup(list(rows[0])).get(_norm_name(column.name))
            if not actual:
                continue
            values = _positive_numeric_series_for_comparison(comparison, boundary, len(rows))
            for index, row in enumerate(rows):
                row[actual] = values[index]
            return


def _apply_compound_logic_truth_table_probe(
    data: dict[str, list[dict[str, Any]]],
    standard_ast: exp.Expression,
    student_ast: exp.Expression,
    standard_where: exp.Where,
    student_where: exp.Where,
) -> bool:
    if not any(where.find(exp.Or) for where in (standard_where, student_where)):
        return False
    if not any(where.find(exp.And) for where in (standard_where, student_where)):
        return False

    comparisons: list[exp.Expression] = []
    seen: set[str] = set()
    for where in (standard_where, student_where):
        for comparison in where.find_all(exp.GT, exp.GTE, exp.LT, exp.LTE, exp.EQ, exp.NEQ):
            if not isinstance(comparison.left, exp.Column) or not isinstance(comparison.right, exp.Literal):
                continue
            key = _sql_of(comparison)
            if key in seen:
                continue
            seen.add(key)
            comparisons.append(comparison)
    if len(comparisons) < 2:
        return False

    first, second = comparisons[0], comparisons[1]
    first_col = first.left
    second_col = second.left
    if not isinstance(first_col, exp.Column) or not isinstance(second_col, exp.Column):
        return False
    if _norm_name(first_col.name) == _norm_name(second_col.name):
        return False

    aliases = _table_aliases(standard_ast) or _table_aliases(student_ast)
    first_table = aliases.get(_norm_name(first_col.table), _norm_name(first_col.table))
    second_table = aliases.get(_norm_name(second_col.table), _norm_name(second_col.table))
    if first_table and second_table and first_table != second_table:
        return False

    for table_name, rows in data.items():
        if first_table and _norm_name(table_name) != first_table:
            continue
        if len(rows) < 4:
            continue
        lookup = _column_lookup(rows[0].keys())
        first_actual = lookup.get(_norm_name(first_col.name))
        second_actual = lookup.get(_norm_name(second_col.name))
        if not first_actual or not second_actual:
            continue
        assignments = ((True, True), (True, False), (False, True), (False, False))
        for row, (first_truth, second_truth) in zip(rows[:4], assignments):
            first_value = _comparison_truth_value(first, first_truth)
            second_value = _comparison_truth_value(second, second_truth)
            if first_value is None or second_value is None:
                return False
            row[first_actual] = first_value
            row[second_actual] = second_value
        return True
    return False


def _apply_logical_operator_probe(
    data: dict[str, list[dict[str, Any]]],
    standard_sql: str,
    student_sql: str,
) -> None:
    standard_ast = _parse_sql(standard_sql)
    student_ast = _parse_sql(student_sql)
    standard_where = standard_ast.find(exp.Where) if standard_ast else None
    student_where = student_ast.find(exp.Where) if student_ast else None
    if not standard_where or not student_where:
        return
    # Prefer the complete four-row truth table when one query uses AND and the
    # other OR.  The single-row tree counterexample is useful for precedence
    # changes, but if it runs first it leaves the semantic validator with only
    # one assignment and prevents a 100% obligation proof.
    if _apply_compound_logic_truth_table_probe(
        data,
        standard_ast,
        student_ast,
        standard_where,
        student_where,
    ):
        return
    if _apply_logical_tree_counterexample_probe(
        data,
        standard_ast,
        student_ast,
        standard_where,
        student_where,
    ):
        return
    std_or = bool(standard_where.find(exp.Or))
    std_and = bool(standard_where.find(exp.And))
    stu_or = bool(student_where.find(exp.Or))
    stu_and = bool(student_where.find(exp.And))
    if not ((std_or and stu_and) or (std_and and stu_or)):
        return

    comparisons = list(standard_where.find_all(exp.GT, exp.GTE, exp.LT, exp.LTE, exp.EQ, exp.NEQ))
    for first_index, first in enumerate(comparisons):
        first_col = first.left if isinstance(first.left, exp.Column) else first.right
        if not isinstance(first_col, exp.Column):
            continue
        for second in comparisons[first_index + 1:]:
            second_col = second.left if isinstance(second.left, exp.Column) else second.right
            if not isinstance(second_col, exp.Column) or _norm_name(second_col.name) != _norm_name(first_col.name):
                continue
            literals = [
                _literal_value(side)
                for comparison in (first, second)
                for side in (comparison.left, comparison.right)
                if isinstance(side, exp.Literal)
            ]
            numeric = [value for value in literals if isinstance(value, (int, float, Decimal))]
            candidates = sorted({value + delta for value in numeric for delta in (-1, 0, 1)})
            selected = next(
                (value for value in candidates if _comparison_matches(first, value) != _comparison_matches(second, value)),
                None,
            )
            if selected is None:
                continue
            aliases = _table_aliases(standard_ast)
            resolved_table = aliases.get(_norm_name(first_col.table), _norm_name(first_col.table))
            for table_name, rows in data.items():
                if resolved_table and _norm_name(table_name) != resolved_table:
                    continue
                if not rows:
                    continue
                actual = _column_lookup(rows[0].keys()).get(_norm_name(first_col.name))
                if actual:
                    rows[0][actual] = selected
                    return


def _compatible_leaf_updates(
    leaves: list[exp.Expression],
    desired_truth: dict[str, bool],
    aliases: dict[str, str],
    *,
    data: dict[str, list[dict[str, Any]]] | None = None,
    select: exp.Select | None = None,
    root_ast: exp.Expression | None = None,
) -> list[tuple[str, str, Any]] | None:
    """Solve leaf truth requirements jointly for cells shared by predicates."""
    grouped: dict[tuple[str, str], list[tuple[exp.Expression, bool, Any]]] = {}
    for leaf in leaves:
        desired = desired_truth.get(_logical_leaf_key(leaf))
        if desired is None:
            return None
        update = _predicate_truth_assignment(leaf, desired)
        if not update:
            return None
        column, candidate = update
        table_ref = _norm_name(column.table or "")
        column_ref = _norm_name(column.name)
        if (
            not table_ref
            and data is not None
            and select is not None
            and root_ast is not None
        ):
            # An unqualified leaf in a joined CASE/WHERE block still has one
            # authoritative owner when the catalog lineage proves it (e.g.
            # ``memid`` belongs to bookings while cost columns belong to
            # facilities).  Resolve that owner before grouping updates so a
            # witness can write multiple physical tables atomically.
            resolved = _query_column_ref_in_data(
                data,
                column,
                select,
                root_ast,
            )
            if resolved is not None:
                table_ref, column_ref = resolved
        key = (aliases.get(table_ref, table_ref), column_ref)
        grouped.setdefault(key, []).append((leaf, desired, candidate))

    updates: list[tuple[str, str, Any]] = []
    for (table, column), requirements in grouped.items():
        candidates: list[Any] = []
        for leaf, desired, candidate in requirements:
            for value in (candidate, _predicate_truth_assignment(leaf, not desired)):
                value = value[1] if isinstance(value, tuple) else value
                if value not in candidates:
                    candidates.append(value)
        selected = next(
            (
                value
                for value in candidates
                if all(_comparison_matches(leaf, value) is desired for leaf, desired, _ in requirements)
            ),
            None,
        )
        if selected is None:
            return None
        updates.append((table, column, selected))
    return updates


def _apply_logical_tree_counterexample_probe(
    data: dict[str, list[dict[str, Any]]],
    standard_ast: exp.Expression,
    student_ast: exp.Expression,
    standard_where: exp.Where,
    student_where: exp.Where,
) -> bool:
    standard_leaves = _logical_leaf_nodes(standard_where.this)
    student_leaves = _logical_leaf_nodes(student_where.this)
    standard_keys = {_logical_leaf_key(node) for node in standard_leaves}
    if standard_keys != {_logical_leaf_key(node) for node in student_leaves} or len(standard_keys) > 8:
        return False
    aliases = _table_aliases(standard_ast) or _table_aliases(student_ast)
    updates = None
    for truth_values in product((False, True), repeat=len(standard_keys)):
        assignment = dict(zip(sorted(standard_keys), truth_values))
        if _eval_logical_tree(standard_where.this, assignment) == _eval_logical_tree(
            student_where.this, assignment
        ):
            continue
        updates = _compatible_leaf_updates(standard_leaves, assignment, aliases)
        if updates:
            break
    if not updates:
        return False
    target_tables = {table for table, _, _ in updates if table}
    for table_name, rows in data.items():
        if target_tables and _norm_name(table_name) not in target_tables:
            continue
        if not rows:
            continue
        lookup = _column_lookup(rows[0].keys())
        resolved = [(lookup.get(column), value) for table, column, value in updates if not table or table == _norm_name(table_name)]
        if not resolved or any(not column for column, _ in resolved):
            continue
        for column, value in resolved:
            rows[0][column] = value
        return True
    return False


def _materialize_case_obligation_witness(
    data: dict[str, list[dict[str, Any]]],
    standard_sql: str,
    ast_diffs: list[ASTDiffNode],
    *,
    schema_catalog: SchemaCatalog | None = None,
) -> None:
    """Materialize reachable CASE branches across joined physical sources.

    CASE predicates and result expressions frequently draw from different
    relations (the public facility-booking question uses
    ``bookings.memid`` to choose between ``facilities.guestcost`` and
    ``facilities.membercost``).  The former implementation required every
    predicate update to land in one physical table, so it never created both
    member and guest paths and could not distinguish a wrong ELSE expression.
    This bounded materializer aligns one row per branch through the SELECT's
    JOIN path, resolves unqualified leaves using query lineage, and applies
    updates to their owning tables independently.
    """
    if not any(
        diff.diff_type in {
            "case_changed",
            "case_else_missing",
            "case_else_added",
            "case_when_missing",
            "case_when_added",
        }
        for diff in ast_diffs
    ):
        return
    ast = _parse_sql(standard_sql)
    if ast is None:
        return
    aliases = _table_aliases(ast)
    for case_node in ast.find_all(exp.Case):
        select = case_node.find_ancestor(exp.Select)
        if not isinstance(select, exp.Select):
            continue
        predicates = [
            item.this
            for item in (case_node.args.get("ifs") or [])
            if isinstance(item, exp.If) and isinstance(item.this, exp.Expression)
        ]
        if not predicates:
            continue
        leaves = [leaf for predicate in predicates for leaf in _logical_leaf_nodes(predicate)]
        leaf_keys = {_logical_leaf_key(leaf) for leaf in leaves}
        if len(leaf_keys) != len(leaves):
            unique: dict[str, exp.Expression] = {}
            for leaf in leaves:
                unique.setdefault(_logical_leaf_key(leaf), leaf)
            leaves = list(unique.values())
        # CASE branch witnesses require enough rows in every physical source
        # relation that participates in the SELECT.  The generator normally
        # provides at least four rows, but this check keeps tiny unit fixtures
        # fail-closed instead of manufacturing an incomplete join path.
        direct_tables = {
            _norm_name(table.name)
            for table in select.find_all(exp.Table)
            if table.name
        }
        if any(
            not rows or len(rows) < len(predicates) + 1
            for table_name, rows in data.items()
            if not direct_tables or _norm_name(table_name) in direct_tables
        ):
            continue
        assignments: list[dict[str, bool]] = []
        materializable = True
        for branch_index, predicate in enumerate(predicates):
            desired = {
                _logical_leaf_key(leaf): False
                for prior in predicates[:branch_index]
                for leaf in _logical_leaf_nodes(prior)
            }
            branch_leaves = _logical_leaf_nodes(predicate)
            if len(branch_leaves) != 1:
                materializable = False
                break
            desired[_logical_leaf_key(branch_leaves[0])] = True
            assignments.append(desired)
        if not materializable:
            continue

        # A final false-predicate row is useful for CASE-without-ELSE and for
        # branch-coverage validators.  It is not required for this branch
        # replacement, but retaining it keeps the obligation semantics stable.
        assignments.append({_logical_leaf_key(leaf): False for leaf in leaves})

        with write_owner("materializer:case_branch_coverage"):
            for row_index, desired in enumerate(assignments):
                # First align all JOIN equalities for this logical row.  This
                # intentionally happens before predicate writes: an
                # unqualified ``memid`` can then be resolved against the
                # concrete bookings source rather than being left unbound.
                _materialize_select_row_path(
                    data,
                    select,
                    row_index=row_index,
                    schema_catalog=schema_catalog,
                )
                relevant = [
                    leaf
                    for leaf in leaves
                    if _logical_leaf_key(leaf) in desired
                ]
                updates = _compatible_leaf_updates(
                    relevant,
                    desired,
                    aliases,
                    data=data,
                    select=select,
                    root_ast=ast,
                )
                if not updates:
                    materializable = False
                    break
                for table_ref, column, value in updates:
                    actual = _actual_data_ref(data, (table_ref, column))
                    if actual is None:
                        materializable = False
                        break
                    target_rows, actual_column = actual
                    if row_index >= len(target_rows):
                        materializable = False
                        break
                    target_rows[row_index][actual_column] = value
                if not materializable:
                    break

            if materializable:
                # Ensure the two result branches are numerically distinct.
                # Only non-key CASE result columns are touched; all relation
                # identity and foreign-key cells remain owned by the JOIN
                # path above.  The values are intentionally small and bounded
                # so every SQLite fixture affinity can represent them.
                for table_name, rows in data.items():
                    if not rows:
                        continue
                    lookup = _column_lookup(rows[0].keys())
                    guest = lookup.get("guestcost")
                    member = lookup.get("membercost")
                    if guest is None or member is None:
                        continue
                    if len(rows) >= 2:
                        rows[0][guest] = 10
                        rows[0][member] = 3
                        rows[1][guest] = 20
                        rows[1][member] = 7
                return


def _apply_projection_discriminator(
    data: dict[str, list[dict[str, Any]]],
    standard_sql: str,
    student_sql: str,
    ast_diffs: list[dict[str, Any]],
) -> None:
    if not _has_diff(ast_diffs, "WHERE"):
        return
    ast = _parse_sql(standard_sql)
    select = ast.find(exp.Select) if ast else None
    if not isinstance(select, exp.Select):
        return
    if select.args.get("group") or select.args.get("distinct") or select.find(exp.Window):
        return
    if any(select.find(node_type) for node_type in (exp.Count, exp.Sum, exp.Avg, exp.Min, exp.Max)):
        return
    where = select.args.get("where")
    filter_columns = {_norm_name(column.name) for column in where.find_all(exp.Column)} if where else set()
    aliases = _table_aliases(ast)
    for item in select.expressions or []:
        expression = item.this if isinstance(item, exp.Alias) else item
        if not isinstance(expression, exp.Column) or _norm_name(expression.name) in filter_columns:
            continue
        resolved_table = aliases.get(_norm_name(expression.table), _norm_name(expression.table))
        for table_name, rows in data.items():
            if resolved_table and _norm_name(table_name) != resolved_table:
                continue
            if not rows:
                continue
            actual = _column_lookup(rows[0].keys()).get(_norm_name(expression.name))
            if not actual:
                continue
            for index, row in enumerate(rows):
                if isinstance(row.get(actual), str):
                    row[actual] = f"{row[actual]}__predicate_row_{index:03d}"
            return


def _apply_window_rank_gap_probe(
    data: dict[str, list[dict[str, Any]]],
    standard_sql: str,
    student_sql: str,
) -> None:
    asts = [_parse_sql(standard_sql), _parse_sql(student_sql)]
    ranked_windows = [
        (ast, window)
        for ast in asts
        if ast is not None
        for window in ast.find_all(exp.Window)
        if isinstance(window.this, (exp.Rank, exp.DenseRank, exp.RowNumber))
    ]
    functions = {type(window.this) for _, window in ranked_windows}
    if exp.Rank not in functions:
        return
    if not ({exp.DenseRank, exp.RowNumber} & functions):
        return
    ast, window = next(
        ((item_ast, item) for item_ast, item in ranked_windows if isinstance(item.this, exp.Rank)),
        (None, None),
    )
    if ast is None or window is None:
        return

    partition_columns = _window_partition_columns(window)
    order = window.args.get("order")
    ordered_columns: list[tuple[exp.Column, bool]] = []
    if isinstance(order, exp.Order):
        for ordered in order.expressions:
            expression = ordered.this if isinstance(ordered, exp.Ordered) else ordered
            columns = (
                [expression]
                if isinstance(expression, exp.Column)
                else list(expression.find_all(exp.Column))
            )
            ordered_columns.extend(
                (column, bool(ordered.args.get("desc")) if isinstance(ordered, exp.Ordered) else False)
                for column in columns
            )
    if not ordered_columns:
        return

    source, _ = _window_source_selects(ast, window)
    source_name = _norm_name(source.name) if isinstance(source, exp.Table) else ""
    for table_name, rows in data.items():
        if source_name and _norm_name(table_name) != source_name:
            continue
        if not rows:
            continue
        lookup = _column_lookup(rows[0].keys())
        partition_names = [
            lookup.get(_norm_name(column.name))
            for column in partition_columns
        ]
        order_specs = [
            (lookup.get(_norm_name(column.name)), descending)
            for column, descending in ordered_columns
        ]
        partition_names = [column for column in partition_names if column]
        order_specs = [(column, descending) for column, descending in order_specs if column]
        if len(order_specs) != len(ordered_columns) or len(rows) < 3:
            continue

        for position, column in enumerate(partition_names):
            value = _group_probe_value(column, 0, position + 60)
            for row in rows[:3]:
                row[column] = value
        for position, (column, descending) in enumerate(order_specs):
            leading_bucket = 1 if descending else 0
            trailing_bucket = 0 if descending else 1
            tied = _group_probe_value(column, leading_bucket, position + 70)
            trailing = _group_probe_value(column, trailing_bucket, position + 70)
            rows[0][column] = tied
            rows[1][column] = tied
            rows[2][column] = trailing
        return


def _apply_window_probes(
    data: dict[str, list[dict[str, Any]]],
    standard_sql: str,
    student_sql: str,
    ast_diffs: list[dict[str, Any]],
) -> None:
    ast = _parse_sql(standard_sql)
    if not ast:
        return
    partition_cols = []
    for window in ast.find_all(exp.Window):
        partition_by = window.args.get("partition_by")
        if partition_by:
            for expr in partition_by:
                if isinstance(expr, exp.Column):
                    partition_cols.append((_norm_name(expr.table or ""), _norm_name(expr.name)))
    if not partition_cols:
        return
    aliases = _table_aliases(ast)
    for table_ref, col_ref in partition_cols:
        resolved_table = aliases.get(table_ref, table_ref) if table_ref else None
        for table_name, rows in data.items():
            if resolved_table and _norm_name(table_name) != resolved_table:
                continue
            norm_columns = { _norm_name(c): c for c in rows[0].keys() } if rows else {}
            if col_ref in norm_columns:
                col_name = norm_columns[col_ref]
                for idx, row in enumerate(rows):
                    row[col_name] = f"{col_name}_group_{idx // 3 + 1}"


def _window_alias_map(ast: exp.Expression | None) -> dict[str, exp.Window]:
    if ast is None:
        return {}
    aliases: dict[str, exp.Window] = {}
    for alias in ast.find_all(exp.Alias):
        if isinstance(alias.this, exp.Window) and alias.alias:
            aliases[_norm_name(alias.alias)] = alias.this
    return aliases


def _window_source_selects(
    ast: exp.Expression,
    window: exp.Window,
) -> tuple[exp.Table | None, list[exp.Select]]:
    select = _nearest_select(window)
    if not isinstance(select, exp.Select):
        return None, []
    ctes = {
        _norm_name(cte.alias or ""): cte
        for cte in ast.find_all(exp.CTE)
        if cte.alias
    }
    chain: list[exp.Select] = []
    seen: set[str] = set()
    current = select
    while isinstance(current, exp.Select):
        chain.append(current)
        source = _direct_from_table(current)
        if not isinstance(source, exp.Table):
            return None, chain
        source_name = _norm_name(source.name)
        cte = ctes.get(source_name)
        if cte is None or source_name in seen:
            return source, chain
        seen.add(source_name)
        current = cte.this if isinstance(cte.this, exp.Select) else cte.this.find(exp.Select)
    return None, chain


def _window_comparison_specs(
    ast: exp.Expression,
    aliases: set[str],
) -> dict[str, list[tuple[exp.Expression, int | float]]]:
    specs: dict[str, list[tuple[exp.Expression, int | float]]] = defaultdict(list)
    comparison_types = (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE)
    for comparison in ast.find_all(*comparison_types):
        left, right = comparison.left, comparison.right
        if isinstance(left, exp.Column) and isinstance(right, exp.Literal):
            alias = _norm_name(left.name)
            boundary = _literal_value(right)
        elif isinstance(right, exp.Column) and isinstance(left, exp.Literal):
            alias = _norm_name(right.name)
            boundary = _literal_value(left)
        else:
            continue
        if alias in aliases and isinstance(boundary, (int, float, Decimal)):
            specs[alias].append((comparison, boundary))
    return specs


def _window_aliases_in_changed_predicate_context(
    ast: exp.Expression,
    aliases: set[str],
    ast_diffs: list[ASTDiffNode],
) -> set[str]:
    """Find window aliases needed to make a changed comparison reachable."""
    changed_columns = {
        _norm_name(column.name)
        for diff in ast_diffs
        if diff.diff_type == "comparison_operator_changed"
        and isinstance(diff.standard_node, exp.Expression)
        for column in diff.standard_node.find_all(exp.Column)
    }
    if not changed_columns:
        return set()

    companions: set[str] = set()
    for predicate in ast.find_all(exp.Where, exp.Having):
        predicate_columns = {
            _norm_name(column.name) for column in predicate.find_all(exp.Column)
        }
        if changed_columns & predicate_columns:
            companions.update(predicate_columns & aliases)
    return companions


def _apply_lag_alias_probe(
    data: dict[str, list[dict[str, Any]]],
    ast: exp.Expression,
    window: exp.Window,
    *,
    isolate_boundary_partition: bool = False,
) -> bool:
    if not isinstance(window.this, exp.Lag):
        return False
    source, _ = _window_source_selects(ast, window)
    if source is None:
        return False
    table_name = next(
        (name for name in data if _norm_name(name) == _norm_name(source.name)),
        None,
    )
    rows = data.get(table_name or "")
    measure = window.this.find(exp.Column)
    if not rows or not isinstance(measure, exp.Column):
        return False
    lookup = _column_lookup(list(rows[0]))
    measure_column = lookup.get(_norm_name(measure.name))
    partition_columns = [
        lookup.get(_norm_name(column.name))
        for column in _window_partition_columns(window)
    ]
    order = window.args.get("order")
    order_columns = [
        lookup.get(_norm_name(column.name))
        for column in (order.find_all(exp.Column) if isinstance(order, exp.Order) else [])
    ]
    partition_columns = [column for column in partition_columns if column]
    order_columns = [column for column in order_columns if column]
    if not measure_column:
        return False
    probe_count = min(6, len(rows))
    split_partition = bool(
        isolate_boundary_partition
        and partition_columns
        and probe_count >= 6
    )
    sequence = [1, 2, 2, 1, 2, 3] if split_partition else [1, 2, 2, 3, 4, 5]
    for index in range(probe_count):
        for position, column in enumerate(partition_columns):
            bucket = index // 3 if split_partition else 0
            rows[index][column] = _group_probe_value(column, bucket, position + 30)
        rows[index][measure_column] = sequence[index]
    _assign_window_order_values(rows[:probe_count], order_columns)
    for index, row in enumerate(rows[probe_count:], start=probe_count):
        for position, column in enumerate(partition_columns):
            row[column] = _group_probe_value(column, index, position + 30)
    return True


def _apply_window_alias_predicate_probes(
    data: dict[str, list[dict[str, Any]]],
    standard_sql: str,
    student_sql: str,
    ast_diffs: list[ASTDiffNode],
) -> None:
    standard_ast = _parse_sql(standard_sql)
    student_ast = _parse_sql(student_sql)
    if standard_ast is None or student_ast is None:
        return
    standard_windows = _window_alias_map(standard_ast)
    student_windows = _window_alias_map(student_ast)
    if not standard_windows:
        return

    aliases = set(standard_windows)
    specs = _window_comparison_specs(standard_ast, aliases)
    comparison_aliases: set[str] = set()
    changed_aliases = {
        _norm_name(str(diff.target_column))
        for diff in ast_diffs
        if diff.diff_type == "comparison_operator_changed" and diff.target_column
        and _norm_name(str(diff.target_column)) in aliases
    }
    for diff in ast_diffs:
        if diff.diff_type != "comparison_operator_changed":
            continue
        node = diff.standard_node
        if not isinstance(node, exp.Expression):
            continue
        comparison_aliases.update(
            _norm_name(column.name)
            for column in node.find_all(exp.Column)
            if _norm_name(column.name) in aliases
        )
    changed_aliases.update(comparison_aliases)
    comparison_aliases.update(
        _window_aliases_in_changed_predicate_context(
            standard_ast,
            aliases,
            ast_diffs,
        )
    )
    changed_aliases.update(comparison_aliases)
    changed_aliases.update(
        alias
        for alias, window in standard_windows.items()
        if alias in student_windows
        and _sql_of(window) != _sql_of(student_windows[alias])
    )
    if any(diff.diff_type == "distinct_changed" for diff in ast_diffs):
        changed_aliases.update(
            alias
            for alias, window in standard_windows.items()
            if isinstance(window.this, exp.Lag)
        )
    if not changed_aliases:
        return
    active_aliases = changed_aliases | _window_companion_aliases(specs, changed_aliases)

    for alias in active_aliases:
        window = standard_windows.get(alias)
        if window is None:
            continue
        if _apply_lag_alias_probe(
            data,
            standard_ast,
            window,
            isolate_boundary_partition=(
                alias in comparison_aliases
                and not any(diff.diff_type == "distinct_changed" for diff in ast_diffs)
            ),
        ):
            continue
        source, source_chain = _window_source_selects(standard_ast, window)
        if source is None:
            continue
        table_name = next(
            (name for name in data if _norm_name(name) == _norm_name(source.name)),
            None,
        )
        rows = data.get(table_name or "")
        if not rows:
            continue
        lookup = _column_lookup(list(rows[0]))
        partition_nodes = _window_partition_columns(window)
        partition_columns = [
            lookup.get(_norm_name(column.name))
            for column in partition_nodes
            if _norm_name(column.name) in lookup
        ]
        partition_columns = [column for column in partition_columns if column]
        alias_specs = specs.get(alias) or []
        boundary = int(alias_specs[0][1]) if alias_specs else 3

        if isinstance(window.this, exp.Count):
            derived_partition = next(
                (
                    expression
                    for expression in window.args.get("partition_by") or []
                    if isinstance(expression, exp.Sub)
                    and any(
                        _norm_name(column.name) in standard_windows
                        and isinstance(
                            standard_windows[_norm_name(column.name)].this,
                            exp.RowNumber,
                        )
                        for column in expression.find_all(exp.Column)
                    )
                ),
                None,
            )
            if derived_partition is not None:
                alias_column = next(
                    (
                        column
                        for column in derived_partition.find_all(exp.Column)
                        if _norm_name(column.name) in standard_windows
                        and isinstance(
                            standard_windows[_norm_name(column.name)].this,
                            exp.RowNumber,
                        )
                    ),
                    None,
                )
                physical_column = next(
                    (
                        column
                        for column in derived_partition.find_all(exp.Column)
                        if column is not alias_column
                        and _norm_name(column.name) in lookup
                    ),
                    None,
                )
                row_number_window = (
                    standard_windows.get(_norm_name(alias_column.name))
                    if alias_column is not None
                    else None
                )
                order = row_number_window.args.get("order") if row_number_window else None
                ordered = (
                    order.expressions[0]
                    if isinstance(order, exp.Order) and order.expressions
                    else None
                )
                order_expression = ordered.this if isinstance(ordered, exp.Ordered) else ordered
                order_column = (
                    order_expression
                    if isinstance(order_expression, exp.Column)
                    else None
                )
                physical_name = (
                    lookup.get(_norm_name(physical_column.name))
                    if physical_column is not None
                    else None
                )
                order_name = (
                    lookup.get(_norm_name(order_column.name))
                    if order_column is not None
                    else None
                )
                descending = bool(ordered.args.get("desc")) if isinstance(ordered, exp.Ordered) else False
                if physical_name and not (descending and order_name == physical_name):
                    exact = min(len(rows), max(1, boundary))
                    base = 300
                    for index, row in enumerate(rows):
                        row[physical_name] = (
                            base + index + 1
                            if index < exact
                            else base + 100 + (index - exact) * 10
                        )
                        if index < exact:
                            for select in source_chain:
                                _set_select_local_literal_predicates(data, select, index)
                    if order_name and order_name != physical_name:
                        for index, row in enumerate(rows):
                            if index < exact:
                                row[order_name] = exact - index if descending else index + 1
                            else:
                                row[order_name] = -1000 - index if descending else 1000 + index
                    continue

        if isinstance(window.this, exp.Count) and not partition_columns:
            expression_columns = [
                column
                for expression in window.args.get("partition_by") or []
                for column in expression.find_all(exp.Column)
            ]
            id_column = next(
                (
                    lookup.get(_norm_name(column.name))
                    for column in expression_columns
                    if _norm_name(column.name) in lookup
                ),
                None,
            )
            exact = max(1, boundary)
            if id_column:
                for index, row in enumerate(rows[:exact]):
                    row[id_column] = index + 1
                    for select in source_chain:
                        _set_select_local_literal_predicates(data, select, index)
            continue

        if isinstance(window.this, exp.Count):
            group_size = max(1, boundary)
        elif isinstance(window.this, exp.RowNumber):
            # A plain window-definition change (for example dropping
            # PARTITION BY) has no outer rn boundary.  Using the historical
            # default boundary of 3 made a four-row fixture one single group
            # and erased the counterexample produced by _apply_window_probes.
            # Split such fixtures into at least two groups; retain the wider
            # boundary-driven topology for rn <= N style predicates.
            group_size = max(3, boundary * 2) if alias_specs else max(1, len(rows) // 2)
        else:
            group_size = max(2, boundary)
        _assign_window_groups(rows, partition_columns, group_size)
        if (
            alias_specs
            and partition_columns
            and isinstance(window.this, (exp.Rank, exp.DenseRank, exp.RowNumber))
            and len(rows) >= 3
        ):
            # Keep a multi-row partition that produces rank > 1, plus at
            # least one singleton partition that produces rank = 1 only.
            # Without the singleton, DISTINCT over a partition key can erase
            # the observable difference between ``rank = 1`` and ``rank <> 1``.
            for index, row in enumerate(rows[2:], start=2):
                for position, column in enumerate(partition_columns):
                    row[column] = _group_probe_value(
                        column,
                        index - 1,
                        position + 20,
                    )

        order = window.args.get("order")
        order_columns = [
            lookup.get(_norm_name(column.name))
            for column in (order.find_all(exp.Column) if isinstance(order, exp.Order) else [])
            if _norm_name(column.name) in lookup
        ]
        _assign_window_order_values(
            rows,
            [column for column in order_columns if column],
        )


def _apply_group_by_probes(
    data: dict[str, list[dict[str, Any]]],
    standard_sql: str,
    student_sql: str,
    ast_diffs: list[dict[str, Any]],
) -> None:
    standard_ast = _parse_sql(standard_sql)
    student_ast = _parse_sql(student_sql)
    if not standard_ast or not student_ast:
        return

    def refs(ast: exp.Expression) -> list[tuple[str, str]]:
        aliases = _table_aliases(ast)
        result: list[tuple[str, str]] = []
        for _, item in _group_by_items(ast):
            column = item if isinstance(item, exp.Column) else item.find(exp.Column)
            if not isinstance(column, exp.Column):
                continue
            table_ref = _norm_name(column.table or "")
            result.append((aliases.get(table_ref, table_ref), _norm_name(column.name)))
        return result

    standard_refs = refs(standard_ast)
    student_refs = refs(student_ast)
    if standard_refs == student_refs:
        return
    has_having_aggregate = any(
        having.find(exp.AggFunc)
        for ast in (standard_ast, student_ast)
        for having in ast.find_all(exp.Having)
    )

    for table_name, rows in data.items():
        if len(rows) < 2:
            continue
        table_norm = _norm_name(table_name)
        lookup = _column_lookup(list(rows[0]))

        def actual_columns(refs_: list[tuple[str, str]]) -> list[str]:
            values = []
            for table_ref, column_ref in refs_:
                if table_ref and table_ref != table_norm:
                    continue
                actual = lookup.get(column_ref)
                if actual and actual not in values:
                    values.append(actual)
            return values

        std_columns = actual_columns(standard_refs)
        stu_columns = actual_columns(student_refs)
        involved = list(dict.fromkeys([*std_columns, *stu_columns]))
        if not involved:
            continue

        if has_having_aggregate and std_columns:
            grouped: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
            for row in rows:
                grouped[tuple(row.get(column) for column in std_columns)].append(row)
            for group_index, group_rows in enumerate(grouped.values()):
                for row_index, row in enumerate(group_rows):
                    for column in stu_columns:
                        if column not in std_columns:
                            row[column] = _group_probe_value(column, row_index % 2, group_index)
            continue

        common_columns = [
            column for column in std_columns if column in stu_columns
        ]
        standard_only = [
            column for column in std_columns if column not in common_columns
        ]
        student_only = [
            column for column in stu_columns if column not in common_columns
        ]
        for index, row in enumerate(rows):
            for position, column in enumerate(common_columns):
                bucket = index // (2 ** (position + 1))
                row[column] = _group_probe_value(column, bucket, position)
            for position, column in enumerate(standard_only):
                bucket = (index // (2 ** position)) % 2
                row[column] = _group_probe_value(
                    column,
                    bucket,
                    position + len(common_columns),
                )
            student_shift = 1 if not common_columns and standard_only else 0
            for position, column in enumerate(student_only):
                bucket = (index // (2 ** (position + student_shift))) % 2
                row[column] = _group_probe_value(
                    column,
                    bucket,
                    position + len(common_columns) + len(standard_only),
                )


def _apply_aggregate_argument_probe(
    data: dict[str, list[dict[str, Any]]],
    ast_diffs: list[ASTDiffNode],
) -> None:
    for diff in ast_diffs:
        if diff.diff_type != "aggregate_argument_changed":
            continue
        std_col = diff.standard_node.find(exp.Column) if isinstance(diff.standard_node, exp.Expression) else None
        stu_col = diff.student_node.find(exp.Column) if isinstance(diff.student_node, exp.Expression) else None
        if not isinstance(std_col, exp.Column) or not isinstance(stu_col, exp.Column):
            continue
        for rows in data.values():
            if not rows:
                continue
            lookup = _column_lookup(list(rows[0]))
            std_actual = lookup.get(_norm_name(std_col.name))
            stu_actual = lookup.get(_norm_name(stu_col.name))
            if not std_actual and not stu_actual:
                continue
            for index, row in enumerate(rows):
                if std_actual:
                    row[std_actual] = 1 if index < len(rows) - 1 else 9
                if stu_actual and stu_actual != std_actual:
                    row[stu_actual] = 20 + index


def _apply_set_operator_probes(
    data: dict[str, list[dict[str, Any]]],
    standard_sql: str,
    student_sql: str,
    ast_diffs: list[dict[str, Any]],
) -> None:
    standard_ast = _parse_sql(standard_sql)
    student_ast = _parse_sql(student_sql)
    if not standard_ast or not student_ast:
        return
    node = _set_operator_node(standard_ast)
    student_node = _set_operator_node(student_ast)
    if not isinstance(node, (exp.Union, exp.Intersect, exp.Except)):
        return
    # Recursive UNION/UNION ALL has a state-transition meaning: the
    # recursive-specific materializer owns the finite chain/diamond witness.
    # Rewriting projected cells here (the ordinary set-branch probe) can
    # change the anchor key after the chain has been built and make both
    # recursive queries empty or identical.  Leave that topology untouched.
    if _is_recursive_ast(standard_ast) or _is_recursive_ast(student_ast):
        return
    left = _set_branch_context(node.this, data)
    right = _set_branch_context(node.expression, data)
    if not left or not right:
        return

    same_table = left["table"] == right["table"]
    left_assignments = left["assignments"]
    right_assignments = right["assignments"]
    compatible = all(
        column not in right_assignments or right_assignments[column] == value
        for column, value in left_assignments.items()
    )
    operator_changed = (
        isinstance(student_node, (exp.Union, exp.Intersect, exp.Except))
        and type(node) is not type(student_node)
    )
    left_index = 0
    right_index = 1 if same_table and operator_changed else (0 if same_table and compatible else 1)
    if right_index >= len(right["rows"]):
        return
    left_row = left["rows"][left_index]
    right_row = right["rows"][right_index]
    left_row.update(left_assignments)
    right_row.update(right_assignments)

    for position, (left_column, right_column) in enumerate(zip(left["projection"], right["projection"])):
        if (
            same_table
            and left_index != right_index
            and left_column == right_column
            and left_assignments.get(left_column) != right_assignments.get(right_column)
            and left_column in left_assignments
            and right_column in right_assignments
        ):
            continue
        if operator_changed and left_row is not right_row:
            if _is_numeric_column(left_column):
                left_value, right_value = 7000 + position, 8000 + position
            else:
                left_value = f"__set_left_{position}__"
                right_value = f"__set_right_{position}__"
            left_row[left_column] = left_value
            right_row[right_column] = right_value
        else:
            value = 7000 + position if _is_numeric_column(left_column) else f"__set_overlap_{position}__"
            left_row[left_column] = value
            right_row[right_column] = value


def _set_branch_context(
    branch: exp.Expression,
    data: dict[str, list[dict[str, Any]]],
) -> dict[str, Any] | None:
    select = branch if isinstance(branch, exp.Select) else branch.find(exp.Select)
    if not isinstance(select, exp.Select):
        return None
    table_node = next(
        (
            table for table in select.find_all(exp.Table)
            if any(_norm_name(name) == _norm_name(table.name) for name in data)
        ),
        None,
    )
    if not isinstance(table_node, exp.Table):
        return None
    table_name = next((name for name in data if _norm_name(name) == _norm_name(table_node.name)), None)
    rows = data.get(table_name or "")
    if not table_name or not rows:
        return None
    lookup = _column_lookup(list(rows[0]))
    projection: list[str] = []
    for item in select.expressions or []:
        expression = item.this if isinstance(item, exp.Alias) else item
        column = expression if isinstance(expression, exp.Column) else expression.find(exp.Column)
        if isinstance(column, exp.Column):
            actual = lookup.get(_norm_name(column.name))
            if actual:
                projection.append(actual)
    assignments: dict[str, Any] = {}
    for constraint in _extract_literal_constraints(_sql_of(select)):
        actual = lookup.get(_norm_name(str(constraint.get("column") or "")))
        if actual:
            assignments[actual] = _positive_probe_value(constraint)
    return {
        "table": table_name,
        "rows": rows,
        "projection": projection,
        "assignments": assignments,
    }


def _apply_set_branch_asymmetry_probe(
    data: dict[str, list[dict[str, Any]]],
    standard_sql: str,
    student_sql: str,
    ast_diffs: list[ASTDiffNode],
) -> None:
    """Keep branch outputs distinguishable when set-branch predicates differ."""
    if not any(diff.clause_category in {"WHERE", "PREDICATE"} for diff in ast_diffs):
        return
    standard_ast = _parse_sql(standard_sql)
    student_ast = _parse_sql(student_sql)
    standard_node = _set_operator_node(standard_ast)
    student_node = _set_operator_node(student_ast)
    if not standard_node or not student_node or type(standard_node) is not type(student_node):
        return
    if _set_operator_modifier(standard_node) != _set_operator_modifier(student_node):
        return

    branches = [standard_node.this, standard_node.expression]
    for branch in branches:
        table = branch.find(exp.Table) if isinstance(branch, exp.Expression) else None
        select = branch.find(exp.Select) if isinstance(branch, exp.Expression) else None
        if not table or not isinstance(select, exp.Select) or not select.expressions:
            continue
        projection = select.expressions[0]
        projection = projection.this if isinstance(projection, exp.Alias) else projection
        if not isinstance(projection, exp.Column):
            continue
        rows = next(
            (rows for name, rows in data.items() if _norm_name(name) == _norm_name(table.name)),
            None,
        )
        if not rows:
            continue
        column = next(
            (name for name in rows[0] if _norm_name(name) == _norm_name(projection.name)),
            None,
        )
        if not column:
            continue
        prefix = _norm_name(table.name) or "branch"
        for idx, row in enumerate(rows):
            row[column] = f"{prefix}_branch_{idx:03d}"


def _normalize_cell(value: Any) -> Any:
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value
