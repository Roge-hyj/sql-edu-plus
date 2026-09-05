"""Public SQLite Phase 1 orchestration and probe registration."""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from collections import Counter, defaultdict
import re
from sqlglot import exp
from core.ast_schema import ASTDiffNode
from core.witness_generation.schema_scope import (
    ColumnRef,
    SchemaCatalog,
    analyze_schema_qualification,
)
from core.witness_generation.obligations import (
    DistinguishingObligation,
    compile_obligations,
    stable_diff_id,
)
from core.witness_generation.planner import (
    WitnessPlanner,
    WitnessSuite,
    WitnessWorld,
    apply_bounded_feedback,
    apply_cell_constraints,
    ConstraintLedger,
    declare_strategy,
    split_world_on_conflict,
    summarize_write_audit,
    track_database_rows,
    write_owner,
)
from core.witness_generation.adapters import LegacyProbeAdapter, LegacyProbeRegistry, run_adapter

from core.phase1_foundation import (
    EQUIVALENCE_UNDECIDED,
    SandboxRun,
    _AGG_FUNC_TYPES,
    _MAX_WITNESS_ATTEMPTS,
    _MAX_WITNESS_ROWS_PER_TABLE,
    _MAX_WITNESS_WORLDS,
    _MUTATION_ORIGINAL_EQUIVALENT,
    _SCOPE_METADATA_VERSION,
    _attach_witness_evidence,
    _classify_bounded_verdict,
    _direct_from_table,
    _distinct_shape_changed,
    _extract_having_aggregate_specs,
    _failed,
    _group_by_items,
    _is_execution_timeout,
    _is_platform_execution_error,
    _join_type_kp,
    _join_type_signature,
    _like_render_node,
    _literal_value,
    _nearest_select,
    _paired_query_blocks,
    _parse_sql,
    _query_block_scope_key,
    _quote_numeric_schema_identifiers,
    _record_world_attempt,
    _record_world_mutation_validation,
    _result_order_clause,
    _scalar_function_roots,
    _schema_qualification_error,
    _set_operator_kp,
    _set_operator_modifier,
    _set_operator_name,
    _set_operator_node,
    _sql_of,
    _top_select,
    _unqualified_sql,
    _world_has_diff,
    SQLiteQueryParseError,
    parse_sqlite_pair_or_raise,
)

from core.phase1_sql_semantics import (
    _build_data_evidence,
    _extract_literal_constraints,
    _is_key_column,
    _is_numeric_column,
    _mutate_by_node_replacement,
    _mutate_query_arg,
    _mutate_query_expressions,
    _norm_name,
    _window_partition_columns,
    parse_schema_column_types,
    parse_schema_text,
)

from core.phase1_constraints import (
    _apply_constraints,
    _apply_dangling_tuple_probe,
    _apply_final_dangling_tuple_probes,
    _apply_group_filter_positive_probe,
    _apply_having_aggregate_probes,
    _apply_join_on_counterexample,
    _apply_same_table_having_membership_probe,
    _column_lookup,
    _constraints_from_ast_diffs,
    _detect_unsupported_features,
    _extract_table_names,
    _group_by_columns_for_sql,
    _is_from_table_of_missing_join,
    _is_likely_sqlite_capability_error,
    _is_recursive_ast,
    _join_on_column_pairs,
    _outer_join_predicate_placement_ast_diffs,
    _right_tables_for_left_joins,
    _table_aliases,
)

from core.phase1_query_paths import (
    _apply_aggregate_function_probe,
    _apply_correlated_subquery_probe,
    _apply_cross_table_having_count_probe,
    _apply_cte_set_overlap_probe,
    _apply_expression_comparison_boundary_probes,
    _apply_nested_except_membership_probe,
    _apply_same_table_membership_probe,
    _apply_scalar_subquery_boundary_probes,
    _apply_self_join_boundary_probes,
    _apply_subquery_aggregate_probes,
    _column_ref_in_select,
    _correlated_subquery_column_pairs,
    _correlated_subquery_context_ast_diffs,
    _set_select_local_literal_predicates,
    _subquery_membership_key_ast_diffs,
)

from core.phase1_witness_strategies import (
    _align_having_membership_keys,
    _apply_expression_probes,
    _apply_join_semantic_probes,
    _apply_not_in_null_probe,
    _apply_same_table_correlated_aggregate_probe,
    _build_phase1_scope_metadata,
    _materialize_declared_join_witness,
    _primary_key_candidate,
)

from core.phase1_witness_materialization import (
    _align_standard_join_equalities,
    _apply_distinct_probes,
    _apply_null_aggregate_probe,
    _apply_subquery_membership_probe,
)

from core.phase1_evidence import (
    _apply_aggregate_argument_probe,
    _apply_cross_table_having_probe,
    _apply_group_by_probes,
    _apply_join_key_drift,
    _apply_logical_operator_probe,
    _apply_nested_membership_chain_probe,
    _apply_order_by_probes,
    _apply_projection_discriminator,
    _apply_set_branch_asymmetry_probe,
    _apply_set_operator_probes,
    _apply_window_alias_predicate_probes,
    _apply_window_probes,
    _apply_window_rank_gap_probe,
    _build_shared_values,
    _dynamic_row_count,
    _execute_mutation_case,
    _execute_sqlite,
    _finalize_generated_witness_data,
    _find_kp_override,
    _prepare_executable_sql_pair,
    _repair_primary_key_candidate_duplicates,
    _run_aggregate_clause_placement_mutation,
    _run_join_clause_mutations,
    _run_join_structure_mutation,
    _typed_base_value,
    _validate_world_atomic_diffs,
    _window_alias_map,
    _window_source_selects,
    extract_ast_diffs,
)



def generate_and_compare(
    schema_text: str,
    standard_sql: str,
    student_sql: str,
    *,
    max_rows_per_table: int = 8,
    schema_catalog: SchemaCatalog | dict[str, Any] | None = None,
) -> SandboxRun:
    """Generate bounded evidence under one fixed SQLite execution contract."""

    schema = parse_schema_text(schema_text)
    schema_types = parse_schema_column_types(schema_text)
    # Numeric-leading schema headers must be quoted before strict SQLite
    # parsing so every downstream component observes the same identifier.
    standard_sql = _quote_numeric_schema_identifiers(standard_sql, schema)
    student_sql = _quote_numeric_schema_identifiers(student_sql, schema)
    unsupported_features = _detect_unsupported_features(
        standard_sql,
        student_sql,
    )
    if unsupported_features:
        feature_text = ", ".join(unsupported_features)
        return _failed(
            f"unsupported_sqlite_feature: {feature_text}",
            None,
            None,
            {},
            [],
            [],
            status="UNSUPPORTED",
            unsupported_features=unsupported_features,
        )
    try:
        standard_ast, student_ast = parse_sqlite_pair_or_raise(
            standard_sql=standard_sql,
            student_sql=student_sql,
        )
    except SQLiteQueryParseError as exc:
        status = {
            "STUDENT_SQL_PARSE_ERROR": "WRONG",
            "STANDARD_SQL_PARSE_ERROR": "INPUT_ERROR",
        }.get(exc.code, "ENGINE_ERROR")
        compatibility_error = {
            "STUDENT_SQL_PARSE_ERROR": "student_sql_parse_failed",
            "STANDARD_SQL_PARSE_ERROR": "standard_sql_parse_failed",
        }.get(exc.code, f"{exc.code}: {exc}")
        return _failed(
            compatibility_error,
            None,
            None,
            {},
            [],
            [],
            status=status,
            error_code=exc.code,
            boundary_evidence=(
                {
                    "reason": "invalid_standard_sql",
                    "sql_role": "standard",
                    "error_code": exc.code,
                }
                if exc.code == "STANDARD_SQL_PARSE_ERROR"
                else None
            ),
            unsupported_features=[exc.code] if status == "UNSUPPORTED" else [],
        )
    try:
        catalog = (
            schema_catalog
            if isinstance(schema_catalog, SchemaCatalog)
            else SchemaCatalog.from_dict(schema_catalog)
            if isinstance(schema_catalog, dict)
            else SchemaCatalog.from_legacy(schema, schema_types)
        )
    except (TypeError, ValueError) as exc:
        return _failed(
            f"schema_catalog_invalid: {exc}",
            None,
            None,
            {},
            [],
            [],
            status="ENGINE_ERROR",
            error_code="SCHEMA_CATALOG_INVALID",
        )
    if schema_catalog is not None:
        # A supplied catalog is authoritative.  Compact schema text remains a
        # portable display/fallback format, not a second source of truth.
        schema = catalog.as_legacy()
        schema_types = catalog.as_legacy_types()
    if not schema and (_extract_table_names(standard_sql) or _extract_table_names(student_sql)):
        return _failed(
            "schema_parse_failed",
            None,
            None,
            {},
            [],
            [],
            status="INPUT_ERROR",
            error_code="SCHEMA_PARSE_FAILED",
            boundary_evidence={
                "reason": "schema_unreplayable",
                "required": "schema_for_referenced_physical_tables",
            },
        )

    if standard_ast is None:
        return _failed(
            "standard_sql_parse_failed",
            None,
            None,
            {},
            [],
            [],
            status="INPUT_ERROR",
            error_code="STANDARD_SQL_PARSE_ERROR",
            boundary_evidence={
                "reason": "invalid_standard_sql",
                "sql_role": "standard",
                "error_code": "STANDARD_SQL_PARSE_ERROR",
            },
        )
    if student_ast is None:
        return _failed("student_sql_parse_failed", None, None, {}, [], [], status="WRONG")
    non_main_namespaces: set[str] = set()
    for query_ast in (standard_ast, student_ast):
        for table in query_ast.find_all(exp.Table):
            parts = []
            for key in ("catalog", "db"):
                value = table.args.get(key)
                if value is None:
                    continue
                raw = str(getattr(value, "this", value) or "").strip('"`[]')
                if raw:
                    parts.append(raw.casefold())
            namespace = ".".join(parts)
            if namespace and namespace != "main":
                non_main_namespaces.add(namespace)
    if non_main_namespaces:
        return _failed(
            "unsupported_sqlite_feature: ATTACHED_DATABASE_NAMESPACE",
            None,
            None,
            {},
            [],
            [],
            status="UNSUPPORTED",
            unsupported_features=["ATTACHED_DATABASE_NAMESPACE"],
            boundary_evidence={
                "reason": "non_main_sqlite_namespace",
                "namespaces": sorted(non_main_namespaces),
            },
        )
    standard_qualification = analyze_schema_qualification(
        standard_sql,
        catalog,
    )
    if not standard_qualification.executable:
        return _failed(
            _schema_qualification_error("standard", standard_qualification),
            None,
            None,
            {},
            [],
            [],
            status="INPUT_ERROR",
            error_code="STANDARD_SCHEMA_QUALIFICATION_FAILED",
        )
    student_qualification = analyze_schema_qualification(
        student_sql,
        catalog,
    )
    if not student_qualification.executable:
        if student_qualification.missing_tables:
            return _failed(
                _schema_qualification_error("student", student_qualification),
                None,
                None,
                {},
                [],
                [],
                status="WRONG",
                error_code="STUDENT_SCHEMA_REFERENCE_FAILED",
            )
    ast_diffs = extract_ast_diffs(
        standard_sql,
        student_sql,
        schema_catalog=catalog,
    )
    witness_suite = generate_witness_suite(
        catalog,
        standard_sql,
        student_sql,
        max_rows_per_table=max_rows_per_table,
        ast_diffs=ast_diffs,
    )
    rows = witness_suite.worlds[0].database

    standard_executable, student_executable = _prepare_executable_sql_pair(
        standard_sql,
        student_sql,
        standard_ast=standard_ast,
        student_ast=student_ast,
    )
    if not standard_executable or not student_executable:
        return _failed(
            "sql_prepare_failed",
            standard_executable,
            student_executable,
            rows,
            [],
            [],
            status="ENGINE_ERROR",
        )

    run = _complete_comparison(
        schema=schema,
        schema_types=schema_types,
        rows=rows,
        standard_sql=standard_sql,
        student_sql=student_sql,
        standard_executable=standard_executable,
        student_executable=student_executable,
        standard_ast=standard_ast,
        student_ast=student_ast,
        ast_diffs=ast_diffs,
        witness_suite=witness_suite,
        schema_catalog=catalog,
    )
    run.data_evidence["schema_catalog"] = {
        "source": catalog.source,
        "database_id": catalog.database_id,
        "physical_table_count": len(catalog.physical_tables),
        "primary_key_count": sum(
            1 for table in catalog.physical_tables.values() if table.primary_key
        ),
        "foreign_key_count": sum(
            len(table.foreign_keys) for table in catalog.physical_tables.values()
        ),
        "authoritative": schema_catalog is not None,
    }
    # Phase 2 consumes explicit scope identities and AST-proven composition
    # edges.  Keep this outside ``ASTDiffNode.extra``: that mapping is part of
    # ``stable_diff_id`` and enriching it here would silently invalidate the
    # frozen Phase 1 evidence identities.
    try:
        run.data_evidence["scope_metadata"] = _build_phase1_scope_metadata(
            standard_ast,
            student_ast,
            ast_diffs,
        )
    except Exception as exc:  # pragma: no cover - last-resort evidence guard
        # Scope enrichment must never change the Phase 1 verdict.  Expose only
        # the exception class, not SQL text or engine details.
        run.data_evidence["scope_metadata"] = {
            "schema_version": _SCOPE_METADATA_VERSION,
            "status": "PARTIAL",
            "scopes": [],
            "conceptual_scopes": [],
            "parent_edges": [],
            "composition_edges": [],
            "diff_bindings": [],
            "limitations": [
                f"scope metadata construction failed: {type(exc).__name__}"
            ],
            "counts": {
                "scopes": 0,
                "conceptual_scopes": 0,
                "parent_edges": 0,
                "composition_edges": 0,
                "diff_bindings": 0,
            },
            "truncated": False,
        }
    return run


def _complete_comparison(
    *,
    schema: dict[str, list[str]],
    schema_types: dict[str, dict[str, str]],
    rows: dict[str, list[dict[str, Any]]],
    standard_sql: str,
    student_sql: str,
    standard_executable: str,
    student_executable: str,
    standard_ast: exp.Expression,
    student_ast: exp.Expression,
    ast_diffs: list[ASTDiffNode],
    witness_suite: WitnessSuite | None = None,
    schema_catalog: SchemaCatalog | None = None,
) -> SandboxRun:
    """Execute bounded witness worlds and select the first real counterexample."""

    suite = witness_suite or WitnessSuite(
        worlds=[WitnessWorld(id="world_01", database=rows)],
        obligations=[],
    )
    obligation_by_id = {item.id: item for item in suite.obligations}
    trials: list[tuple[WitnessWorld, SandboxRun, int]] = []
    selected: tuple[WitnessWorld, SandboxRun, int] | None = None
    for world in suite.worlds:
        world_obligations = [
            obligation_by_id[item]
            for item in world.obligation_ids
            if item in obligation_by_id
        ]
        attempt_limit = _MAX_WITNESS_ATTEMPTS if world_obligations else 1
        for attempt in range(attempt_limit):
            if attempt and not _regenerate_witness_world(
                world=world,
                obligations=world_obligations,
                ast_diffs=ast_diffs,
                schema=schema,
                standard_sql=standard_sql,
                student_sql=student_sql,
                attempt=attempt,
                schema_catalog=schema_catalog,
            ):
                break
            trial = _run_witness_world(
                schema=schema,
                schema_types=schema_types,
                world=world,
                standard_sql=standard_sql,
                student_sql=student_sql,
                standard_executable=standard_executable,
                student_executable=student_executable,
                standard_ast=standard_ast,
                student_ast=student_ast,
                ast_diffs=ast_diffs,
                run_mutations=False,
                schema_catalog=schema_catalog,
            )
            atomic_validation = _validate_world_atomic_diffs(
                world=world,
                run=trial,
                ast_diffs=ast_diffs,
                student_sql=student_sql,
                schema=schema,
                schema_types=schema_types,
            )
            _record_world_attempt(world, trial, attempt, atomic_validation)
            trials.append((world, trial, attempt))
            pair_distinguished = trial.executed and trial.is_equivalent is False
            if pair_distinguished and selected is None:
                selected = (world, trial, attempt)
            obligation_distinguished = bool(
                atomic_validation.get("all_supported_distinguished")
            )
            if pair_distinguished and obligation_distinguished:
                validated = _run_witness_world(
                    schema=schema,
                    schema_types=schema_types,
                    world=world,
                    standard_sql=standard_sql,
                    student_sql=student_sql,
                    standard_executable=standard_executable,
                    student_executable=student_executable,
                    standard_ast=standard_ast,
                    student_ast=student_ast,
                    ast_diffs=ast_diffs,
                    run_mutations=True,
                )
                if validated.executed:
                    trial = validated
                    trials[-1] = (world, trial, attempt)
                    _record_world_mutation_validation(world, trial, ast_diffs)
                    if (
                        selected is not None
                        and selected[0].id == world.id
                        and selected[2] == attempt
                    ):
                        selected = (world, trial, attempt)
            if obligation_distinguished:
                break
            if not trial.executed:
                break
    # Prefer an isolated world with atomic attribution evidence.  The
    # compatibility composite world is useful as a fallback for interactions,
    # but selecting it eagerly can reintroduce unrelated probe rewrites and
    # make the legacy mutation replay fail (for example, a missing JOIN alias
    # or an invalid aggregate placement).
    preferred_candidates = [
        item
        for item in trials
        if item[1].executed
        and item[1].is_equivalent is False
        and any(
            attempt.get("attempt") == item[2]
            and attempt.get("obligation_distinguished")
            for attempt in item[0].execution.get("attempts", [])
        )
    ]
    set_diff_ids = {
        stable_diff_id(diff, index)
        for index, diff in enumerate(ast_diffs)
        if diff.diff_type in {
            "set_operator_changed",
            "set_modifier_changed",
            "set_all_modifier_changed",
        }
    }
    recursive_diff_ids = {
        stable_diff_id(diff, index)
        for index, diff in enumerate(ast_diffs)
        if diff.diff_type in {
            "recursive_cte_changed",
            "recursive_step_expression_changed",
        }
    }
    preferred = next(
        (
            item for item in preferred_candidates
            if set(item[0].diff_ids) & recursive_diff_ids
        ),
        next(
            (
                item for item in preferred_candidates
            if set(item[0].diff_ids) & set_diff_ids
            ),
            preferred_candidates[0] if preferred_candidates else None,
        ),
    )
    if preferred is None:
        preferred = next(
            (
                item
                for item in trials
                if "compatibility_composite_world" in item[0].diagnostics
                and item[1].executed
                and item[1].is_equivalent is False
            ),
            None,
        )
    if preferred is not None:
        selected = preferred
    if selected is None:
        selected = next(
            (item for item in trials if item[1].executed),
            trials[0] if trials else None,
        )
    if selected is None:
        failed = _failed(
            "witness_planner_produced_no_executable_world",
            standard_executable,
            student_executable,
            rows,
            [],
            [],
            status="ENGINE_ERROR",
            error_code="NO_WITNESS_WORLD",
        )
        _attach_witness_evidence(failed, suite, None, ast_diffs)
        return failed

    selected_world, selected_trial, selected_attempt = selected
    final_run = selected_trial
    if not selected_trial.executed:
        _attach_witness_evidence(final_run, suite, selected_world.id, ast_diffs)
        return final_run
    if not final_run.mutation_evidence.get("enabled"):
        replay_world = WitnessWorld(
            id=selected_world.id,
            obligation_ids=list(selected_world.obligation_ids),
            diff_ids=list(selected_world.diff_ids),
            constraints=list(selected_world.constraints),
            minimum_rows=dict(selected_world.minimum_rows),
            database=selected_trial.test_database,
        )
        replay = _run_witness_world(
            schema=schema,
            schema_types=schema_types,
            world=replay_world,
            standard_sql=standard_sql,
            student_sql=student_sql,
            standard_executable=standard_executable,
            student_executable=student_executable,
            standard_ast=standard_ast,
            student_ast=student_ast,
            ast_diffs=ast_diffs,
            run_mutations=True,
            schema_catalog=schema_catalog,
        )
        if replay.executed:
            final_run = replay
        elif selected_trial.executed:
            final_run.mutation_evidence["reason"] = "selected_world_replay_failed"
    selected_world.execution["selected"] = True
    selected_world.execution["selected_attempt"] = selected_attempt
    selected_world.execution["selection_reason"] = (
        "pair_distinguished" if selected_trial.is_equivalent is False else "first_executable_world"
    )
    _attach_witness_evidence(final_run, suite, selected_world.id, ast_diffs)
    return final_run


def _run_witness_world(
    *,
    schema: dict[str, list[str]],
    schema_types: dict[str, dict[str, str]],
    world: WitnessWorld,
    standard_sql: str,
    student_sql: str,
    standard_executable: str,
    student_executable: str,
    standard_ast: exp.Expression,
    student_ast: exp.Expression,
    ast_diffs: list[ASTDiffNode],
    run_mutations: bool,
    schema_catalog: SchemaCatalog | None = None,
) -> SandboxRun:
    world.execution.setdefault("validation_context", {}).update({
        "standard_sql": standard_executable,
        "student_sql": student_executable,
        # Retain the validated source pair beside deterministic executable SQL
        # so nested-scope validation can restore the reference CTE context.
        "standard_source_sql": standard_sql,
    })
    return _complete_comparison_single(
        schema=schema,
        schema_types=schema_types,
        rows=world.database,
        standard_sql=standard_sql,
        student_sql=student_sql,
        standard_executable=standard_executable,
        student_executable=student_executable,
        standard_ast=standard_ast,
        student_ast=student_ast,
        ast_diffs=ast_diffs,
        run_mutations=run_mutations,
        schema_catalog=schema_catalog,
    )


def _regenerate_witness_world(
    *,
    world: WitnessWorld,
    obligations: list[DistinguishingObligation],
    ast_diffs: list[ASTDiffNode],
    schema: dict[str, list[str]],
    standard_sql: str,
    student_sql: str,
    attempt: int,
    schema_catalog: SchemaCatalog | None = None,
) -> bool:
    diff_by_id = {
        stable_diff_id(diff, index): diff
        for index, diff in enumerate(ast_diffs)
    }
    world_diffs = [diff_by_id[item] for item in world.diff_ids if item in diff_by_id]
    current_rows = max((len(items) for items in world.database.values()), default=4)
    required_rows = max(world.minimum_rows.values(), default=4)
    row_limit = min(
        _MAX_WITNESS_ROWS_PER_TABLE,
        max(current_rows + 2, required_rows, 4 + attempt * 2),
    )
    write_audit: list[Any] = []
    candidate = generate_test_database(
        schema,
        standard_sql,
        student_sql,
        max_rows_per_table=row_limit,
        ast_diffs=world_diffs,
        write_audit=write_audit,
        obligations=obligations,
        schema_catalog=schema_catalog,
    )
    feedback = apply_bounded_feedback(candidate, obligations, attempt=attempt)
    world.execution.setdefault("feedback", []).append(feedback)
    if not feedback["targeted"]:
        return False
    # Feedback is allowed to adjust only the selected obligation's bounded
    # domain.  Normalize first, then apply the ledger-owned declarations as
    # the final materialization step so the report describes the actual
    # candidate handed to the executor.
    _finalize_generated_witness_data(
        candidate,
        standard_sql,
        student_sql,
        world_diffs,
        generation_scope=(
            world.execution.get("legacy_probe_adapters", {})
            .get("generation_scope", {})
        ),
        obligations=obligations,
        schema_catalog=schema_catalog,
    )
    with write_owner("planner:cell_constraints"):
        constraint_report = apply_cell_constraints(candidate, world.constraints)
    world.database = candidate
    world.execution["constraint_application"] = constraint_report
    world.execution.setdefault("legacy_write_audits", []).append(
        summarize_write_audit(write_audit)
    )
    world.execution["planning"]["row_limit"] = row_limit
    return True


def _complete_comparison_single(
    *,
    schema: dict[str, list[str]],
    schema_types: dict[str, dict[str, str]],
    rows: dict[str, list[dict[str, Any]]],
    standard_sql: str,
    student_sql: str,
    standard_executable: str,
    student_executable: str,
    standard_ast: exp.Expression,
    student_ast: exp.Expression,
    ast_diffs: list[ASTDiffNode],
    run_mutations: bool = True,
    schema_catalog: SchemaCatalog | None = None,
) -> SandboxRun:
    try:
        std_cols, std_rows = _execute_sqlite(
            schema,
            rows,
            standard_executable,
            schema_types=schema_types,
        )
    except Exception as exc:
        status = "TIMEOUT" if _is_execution_timeout(exc) else (
            "UNSUPPORTED"
            if _is_likely_sqlite_capability_error(str(exc), standard_executable)
            else "ENGINE_ERROR"
        )
        error = f"standard_sql_failed: {exc}"
        error_code = getattr(exc, "code", None)
        return _failed(
            error,
            standard_executable,
            student_executable,
            rows,
            [],
            [],
            status=status,
            error_code=error_code,
        )

    try:
        stu_cols, stu_rows = _execute_sqlite(
            schema,
            rows,
            student_executable,
            schema_types=schema_types,
        )
        student_exec_error = None
    except Exception as exc:
        if _is_platform_execution_error(exc):
            status = "TIMEOUT" if _is_execution_timeout(exc) else "ENGINE_ERROR"
            return _failed(
                f"student_sql_platform_failed: {exc}",
                standard_executable,
                student_executable,
                rows,
                std_rows,
                [],
                status=status,
                error_code=getattr(exc, "code", None),
            )
        stu_cols, stu_rows = [], []
        student_exec_error = str(exc)

    # Only ORDER BY on the result-producing query block defines observable
    # row order. An ORDER BY inside a derived table/CTE may affect LIMIT in
    # that block, but it does not make the outer result ordered.
    ordered = isinstance(_result_order_clause(standard_ast), exp.Order)
    if student_exec_error:
        is_equivalent = False
    elif ordered:
        is_equivalent = len(std_cols) == len(stu_cols) and std_rows == stu_rows
    else:
        is_equivalent = len(std_cols) == len(stu_cols) and Counter(std_rows) == Counter(stu_rows)

    verdict_status, equivalence_conclusion, boundary_evidence = _classify_bounded_verdict(
        standard_sql=standard_sql,
        student_sql=student_sql,
        rows=rows,
        ast_diffs=ast_diffs,
        is_equivalent=is_equivalent,
    )

    evidence = _build_data_evidence(
        is_equivalent=is_equivalent,
        ordered=ordered,
        standard_columns=std_cols,
        student_columns=stu_cols,
        standard_rows=std_rows,
        student_rows=stu_rows,
        standard_ast=standard_ast,
        student_ast=student_ast,
        student_exec_error=student_exec_error,
        ast_diffs=ast_diffs,
    )
    evidence["execution_backend"] = "sqlite"
    evidence["sql_dialect"] = "sqlite"
    evidence["status"] = verdict_status
    evidence["equivalence_conclusion"] = equivalence_conclusion
    evidence["boundary_evidence"] = boundary_evidence
    judge_status = (
        "WRONG"
        if not is_equivalent
        else "UNDECIDED"
        if equivalence_conclusion == EQUIVALENCE_UNDECIDED
        else "CORRECT"
    )
    evidence["judge_status"] = judge_status
    if run_mutations:
        mutation_evidence = _run_mutation_tests(
            schema=schema,
            rows=rows,
            standard_sql=standard_sql,
            student_sql=student_sql,
            standard_columns=std_cols,
            standard_rows=std_rows,
            original_is_equivalent=is_equivalent,
            ordered=ordered,
            schema_types=schema_types,
        )
    else:
        mutation_evidence = {
            "enabled": False,
            "summary": {"executed": 0, "fixed_by_replacement": 0},
            "tests": [],
            "reason": "deferred_until_witness_world_selected",
        }
    return SandboxRun(
        executed=True,
        is_equivalent=is_equivalent,
        error=None,
        standard_sqlite=standard_executable,
        student_sqlite=student_executable,
        standard_rows=std_rows,
        student_rows=stu_rows,
        standard_columns=std_cols,
        student_columns=stu_cols,
        test_database=rows,
        data_evidence=evidence,
        mutation_evidence=mutation_evidence,
        ast_diffs=ast_diffs,
        judge_status=judge_status,
        status=verdict_status,
        equivalence_conclusion=equivalence_conclusion,
        boundary_evidence=boundary_evidence,
    )


def generate_test_database(
    schema: dict[str, list[str]],
    standard_sql: str,
    student_sql: str,
    *,
    max_rows_per_table: int = 8,
    ast_diffs: list[dict[str, Any]] | None = None,
    write_audit: list[Any] | None = None,
    generation_metadata: dict[str, Any] | None = None,
    defer_witness_finalization: bool = False,
    obligations: list[DistinguishingObligation] | None = None,
    schema_catalog: SchemaCatalog | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """
    根据 Schema 以及标答和学生 SQL 提取的语法约束，动态为各表生成隔离测试数据。
    Generates test data dynamically for target database tables based on Schema and SQL predicate constraints.

    实现流程 (Implementation steps):
    1. 提取标答与学生 SQL 中所有的字面量约束条件 (如 WHERE, IN, LIKE, HAVING 等)；
    2. 计算查询语句涉及的目标物理表集合，过滤无关的表；
    3. 构建主外键拓扑对齐的值池 (Shared Values)，保证 JOIN 连接能匹配上；
    4. 逐行填充基础数值种子数据 (_base_value)，然后将谓词三态和空值探针约束注入源数据；
    5. 针对 DISTINCT 去重进行数据行的重复复制探测 (_add_duplicate_probe)。
    """
    # 1. 抽取标答与作答 SQL 内的所有比较、LIKE、IN、BETWEEN 和 NULL 等谓词字面量约束
    ast_diffs = ast_diffs if ast_diffs is not None else extract_ast_diffs(standard_sql, student_sql)
    constraints = _constraints_from_ast_diffs(ast_diffs)
    constraints.extend(_extract_literal_constraints(standard_sql) + _extract_literal_constraints(student_sql))

    # 2. 筛选查询涉及到的表，仅为其生成测试数据以节省内存和执行开销
    tables_in_queries = _extract_table_names(standard_sql) | _extract_table_names(student_sql)
    if tables_in_queries:
        target_tables = {
            table: cols
            for table, cols in schema.items()
            if _norm_name(table) in tables_in_queries
        }
        if not target_tables:
            target_tables = schema
    else:
        target_tables = schema

    # 3. 基础行数保持小规模，但允许由 AST 差异驱动的算子提高最小有效行数。
    #    例如 HAVING COUNT(*) >= c vs > c 必须至少有一个恰好 c 行的分组。
    row_count = _dynamic_row_count(max_rows_per_table, standard_sql, student_sql, ast_diffs)

    # These flags are the isolation boundary for the remaining compatibility
    # probes.  The planner already selected ``ast_diffs`` for this world; the
    # flags prevent helpers that predate the planner from silently consulting
    # the complete SQL pair and rewriting unrelated evidence.
    has_join_world = _world_has_diff(
        ast_diffs,
        clauses={"JOIN", "JOIN TYPE", "JOIN ON"},
        diff_types={
            "join_missing",
            "join_type_changed",
            "join_on_changed",
            "join_predicate_placement_changed",
        },
    )
    has_aggregate_world = _world_has_diff(
        ast_diffs,
        clauses={"GROUP BY", "HAVING", "AGGREGATE"},
        diff_types={
            "group_by_changed", "group_by_expression_changed",
            "grouping_grain_too_fine", "grouping_grain_too_coarse",
            "having_changed", "aggregate_condition_in_where",
            "aggregate_function_changed", "aggregate_argument_changed",
            "aggregate_distinct_changed",
        },
    ) or any(
        constraint.kind == "aggregate_boundary_group"
        for obligation in (obligations or ())
        for constraint in obligation.hard_constraints
    )
    # The specific HAVING comparison is often the only non-redundant diff;
    # the summary ``having_changed`` node is intentionally omitted by the
    # obligation compiler.  Recover that scope from the diff's own SQL
    # metadata without making every ordinary WHERE comparison an aggregate
    # world.
    has_aggregate_world = has_aggregate_world or any(
        getattr(diff, "diff_type", None) in {
            "comparison_operator_changed", "literal_changed",
        }
        and re.search(
            r"\b(?:COUNT|SUM|AVG|MIN|MAX)\s*\(",
            " ".join(
                str(getattr(diff, "extra", {}).get(key) or "")
                for key in ("standard_sql", "student_sql")
            ),
            flags=re.IGNORECASE,
        )
        for diff in ast_diffs
    )
    has_aggregate_world = has_aggregate_world or any(
        constraint.kind == "filtered_aggregate_boundary_path"
        for obligation in (obligations or ())
        for constraint in obligation.hard_constraints
    )
    has_subquery_world = _world_has_diff(
        ast_diffs,
        clauses={"SUBQUERY", "IN", "EXISTS", "NULL"},
        diff_types={
            "subquery_added", "subquery_removed", "correlated_predicate_changed",
            "in_predicate_negation_changed", "null_sensitive_antijoin_equivalence",
            "in_exists_equivalence", "in_list_member_removed", "in_list_member_added",
        },
    )
    has_predicate_world = _world_has_diff(
        ast_diffs,
        clauses={"WHERE", "HAVING", "LOGICAL", "CASE", "SELECT", "PROJECTION", "SUBQUERY", "IN", "NULL"},
        diff_types={
            "predicate_missing", "predicate_added", "comparison_operator_changed",
            "literal_changed", "logical_operator_changed", "logical_precedence_tree_changed",
            "predicate_expression_operator_changed", "regex_pattern_changed",
            "like_pattern_changed",
            "glob_pattern_changed",
            "null_equality_changed",
            "case_changed", "case_else_missing", "case_else_added",
            "case_when_missing", "case_when_added",
        },
    )
    has_projection_world = _world_has_diff(
        ast_diffs,
        clauses={"SELECT", "PROJECTION"},
        diff_types={
            "projection_changed", "column_added", "column_dropped", "star_mismatch",
            "alias_changed", "function_argument_changed",
        },
    )
    has_set_world = _world_has_diff(
        ast_diffs,
        clauses={"UNION", "INTERSECT", "EXCEPT", "SET OPERATOR"},
        diff_types={"set_operator_changed", "set_modifier_changed", "set_all_modifier_changed"},
    )
    has_cte_world = _world_has_diff(
        ast_diffs,
        clauses={"CTE", "CTE RECURSIVE"},
        diff_types={
            "cte_changed", "recursive_cte_changed", "recursive_step_expression_changed",
        },
    )
    has_distinct_world = _world_has_diff(
        ast_diffs,
        clauses={"DISTINCT"},
        diff_types={
            "distinct_changed",
            "aggregate_distinct_changed",
        },
    )
    has_window_world = _world_has_diff(
        ast_diffs,
        clauses={"WINDOW"},
        diff_types={"window_over_changed", "window_function_changed"},
    )
    has_order_world = _world_has_diff(
        ast_diffs,
        clauses={"ORDER BY", "LIMIT"},
        diff_types={
            "order_by_changed", "order_by_tiebreaker_missing", "order_by_key_added",
            "order_direction_changed", "order_nulls_changed", "limit_changed",
        },
    )

    # A world owns one *difference*, but that difference can sit inside a
    # larger relational pipeline.  A literal change in a UNION branch still
    # needs asymmetric branches, and a comparison over a window alias still
    # needs partition/order topology.  Keep those dependencies explicit here
    # rather than letting legacy probes inspect the complete SQL pair and run
    # in every world.  The context only augments the current world; it never
    # merges unrelated obligations.
    parsed_queries = tuple(
        ast
        for ast in (_parse_sql(standard_sql), _parse_sql(student_sql))
        if ast is not None
    )
    query_has_subquery = any(
        ast.find(exp.Subquery) is not None or ast.find(exp.Exists) is not None
        for ast in parsed_queries
    )
    query_has_set = any(
        _set_operator_node(ast) is not None
        for ast in parsed_queries
    )
    query_has_window = any(ast.find(exp.Window) is not None for ast in parsed_queries)
    query_has_recursive = any(_is_recursive_ast(ast) for ast in parsed_queries)

    has_subquery_world = has_subquery_world or (
        query_has_subquery
        and (has_predicate_world or has_aggregate_world or has_distinct_world)
    )
    has_set_world = has_set_world or (
        query_has_set and has_predicate_world
    )
    has_window_world = has_window_world or (
        query_has_window and (
            has_predicate_world
            or has_projection_world
            or has_distinct_world
            or has_aggregate_world
        )
    )
    world_probe_scope = {
        "join": has_join_world,
        "aggregate": has_aggregate_world,
        "subquery": has_subquery_world,
        "predicate": has_predicate_world,
        "projection": has_projection_world,
        "set": has_set_world,
        "cte": has_cte_world,
        "distinct": has_distinct_world,
        "window": has_window_world,
        "order": has_order_world,
    }

    # 4. 构建关联表的主外键种子池，保证 JOIN 条件不为空，解决拓扑对齐与多外键错位偏移
    shared_values = _build_shared_values(target_tables, row_count)
    data: dict[str, list[dict[str, Any]]] = {}

    for table, columns in target_tables.items():
        rows: list[dict[str, Any]] = []
        for idx in range(row_count):
            row = {}
            for col in columns:
                # 填充各字段的基础值（包括 Outer Join 不对称悬浮元组的 None 填充）
                row[col] = _typed_base_value(
                    table,
                    col,
                    idx,
                    shared_values,
                    schema_catalog,
                )
            rows.append(row)
        if write_audit is not None:
            track_database_rows({table: rows}, write_audit)

        # 5. 注入数值边界三态值、HAVING 聚合以及 NULL 空值探针数据
        _apply_constraints(rows, columns, constraints, target_tables)
        if has_aggregate_world:
            _apply_having_aggregate_probes(rows, columns, standard_sql, student_sql, ast_diffs)
            _apply_aggregate_function_probe(
                rows,
                columns,
                table,
                standard_sql,
                student_sql,
                ast_diffs,
            )
            _apply_null_aggregate_probe(rows, columns, standard_sql, student_sql)
        if has_join_world:
            _apply_join_key_drift(rows, columns, shared_values)
        # Dangling tuple probe for LEFT JOIN right tables AND join_missing left tables.
        # When a JOIN is missing, the left (FROM) table needs rows that have no match
        # in the dropped table, so that INNER JOIN would filter them out but SELECT alone won't.
        _apply_dangling = (
            _norm_name(table) in _right_tables_for_left_joins(standard_sql, student_sql, ast_diffs=ast_diffs)
            or _is_from_table_of_missing_join(table, standard_sql, ast_diffs)
        )
        if _apply_dangling and not has_join_world:
            _apply_dangling_tuple_probe(rows, columns, table, standard_sql, student_sql)
        if has_subquery_world:
            _apply_subquery_aggregate_probes(rows, columns, table, standard_sql, student_sql)
            _apply_subquery_membership_probe(rows, columns, table, standard_sql, student_sql)
        if has_predicate_world or has_projection_world:
            _apply_expression_probes(rows, columns, table, standard_sql, student_sql)

        data[table] = rows[:row_count]

    # COUNT/HAVING probes may deliberately duplicate a grouping key.  If that
    # key is also the parent side of a standard JOIN, restore the corresponding
    # foreign-key values before executing the query so the probe does not
    # accidentally turn both sides into empty joins.  Later JOIN-specific
    # tactics can still introduce the requested student-side drift.
    if has_aggregate_world and any(
        spec.get("agg") == "COUNT"
        for sql in (standard_sql, student_sql)
        for spec in _extract_having_aggregate_specs(sql)
    ):
        _align_standard_join_equalities(data, standard_sql)

    if has_aggregate_world:
        _apply_cross_table_having_probe(data, standard_sql, student_sql, ast_diffs)
        _apply_group_filter_positive_probe(data, standard_sql, student_sql, ast_diffs)
    adapter_ledger = ConstraintLedger()
    world_obligations = list(obligations or ())
    world_obligation_ids = [item.id for item in world_obligations]
    active_adapters = LEGACY_PROBE_REGISTRY.active(
        ast_diffs,
        world_obligation_ids,
        standard_sql=standard_sql,
        student_sql=student_sql,
    )
    adapter_runs = []
    adapter_constraint_conflicts = []

    def _run_adapter_stage(stage: str) -> None:
        for adapter in active_adapters:
            adapter_stage = str(adapter.metadata.get("stage") or "main")
            if adapter_stage != stage:
                continue
            adapter_run = run_adapter(
                adapter,
                data=data,
                schema=schema,
                standard_sql=standard_sql,
                student_sql=student_sql,
                ast_diffs=ast_diffs,
                obligation_ids=world_obligation_ids,
                obligations=world_obligations,
                ledger=adapter_ledger,
            )
            adapter_constraint_conflicts.extend(adapter_run.constraint_conflicts)
            adapter_runs.append({
                "name": adapter_run.adapter,
                "stage": stage,
                "activated": adapter_run.activated,
                "applied": adapter_run.applied,
                "conflicts": adapter_run.conflicts,
                "diagnostics": adapter_run.diagnostics,
                "writes": adapter_run.writes,
                "declared_read_set": adapter_run.declared_read_set,
                "declared_write_set": adapter_run.declared_write_set,
                "write_set_satisfied": adapter_run.write_set_satisfied,
            })
            # Adapter diagnostics are intentionally not represented as a
            # table; write audit and world evidence remain the data path.

    _run_adapter_stage("main")
    if has_projection_world:
        _apply_projection_discriminator(data, standard_sql, student_sql, ast_diffs)
    if has_aggregate_world:
        _apply_aggregate_argument_probe(data, ast_diffs)

    if has_set_world:
        _apply_set_branch_asymmetry_probe(data, standard_sql, student_sql, ast_diffs)
    if has_cte_world:
        _apply_cte_outer_projection_probe(data, standard_sql, ast_diffs)

    # Cross-table adapters need the complete database, but must run before PK
    # repair and final JOIN topology alignment.
    _run_adapter_stage("post_main")
    if has_aggregate_world or has_subquery_world:
        _align_having_membership_keys(data, standard_sql, student_sql)

    _repair_primary_key_candidate_duplicates(
        data,
        target_tables,
        standard_sql,
        student_sql,
    )
    standard_join_pairs = _join_on_column_pairs(standard_sql)
    if (
        standard_join_pairs
        and standard_join_pairs == _join_on_column_pairs(student_sql)
    ):
        _align_standard_join_equalities(data, standard_sql)
    if has_aggregate_world:
        _apply_cross_table_having_count_probe(data, standard_sql, student_sql)
    if has_join_world:
        _apply_join_semantic_probes(data, standard_sql, student_sql)
        _apply_self_join_boundary_probes(data, standard_sql, student_sql, ast_diffs)
    if has_subquery_world:
        _apply_same_table_correlated_aggregate_probe(data, standard_sql, student_sql)
        _apply_same_table_membership_probe(data, standard_sql, student_sql)
        _apply_nested_except_membership_probe(data, standard_sql, student_sql)
        _apply_same_table_having_membership_probe(data, standard_sql, student_sql)
        _apply_nested_membership_chain_probe(data, standard_sql, student_sql)
    if has_set_world or has_cte_world:
        _apply_cte_set_overlap_probe(data, standard_sql, student_sql, ast_diffs)
    if has_cte_world or query_has_recursive:
        _apply_recursive_cte_safety(data, schema, standard_sql, student_sql)
    # Recursive UNION/UNION ALL is a semantic difference even when the AST
    # diff is classified at the set node and therefore does not activate the
    # ordinary predicate/set world flags.  Keep the duplicate-state probe on
    # the recursive query path itself; otherwise a valid recursive mutation
    # can execute against a one-row chain and appear equivalent.
    if query_has_recursive or has_cte_world or has_set_world:
        _apply_recursive_set_duplicate_probe(data, standard_sql, student_sql, ast_diffs)
    if has_cte_world:
        _apply_recursive_cte_orphan_probe(data, standard_sql, student_sql)
    _run_adapter_stage("post_repair")
    # Alias-aware probes rewrite window order columns. Apply ranking ties after
    # them so RANK/ROW_NUMBER/DENSE_RANK counterexamples survive to execution.
    if has_window_world:
        _apply_window_rank_gap_probe(data, standard_sql, student_sql)
    # Final-stage adapters own topology that must survive all compatibility
    # repairs. In particular, matched/dangling JOIN rows are written once,
    # after PK alignment and all other row-shape probes.
    _run_adapter_stage("final")
    if not defer_witness_finalization:
        _finalize_generated_witness_data(
            data,
            standard_sql,
            student_sql,
            ast_diffs,
            generation_scope=world_probe_scope,
            obligations=obligations,
            schema_catalog=schema_catalog,
        )
    if generation_metadata is not None:
        # Keep metadata out of the database payload. The optional side channel
        # lets the planner attach the exact adapter execution trace to the
        # WitnessWorld without changing the long-standing return contract.
        generation_metadata["world_diff_ids"] = [
            stable_diff_id(diff, index) for index, diff in enumerate(ast_diffs)
        ]
        generation_metadata["world_probe_scope"] = dict(world_probe_scope)
        generation_metadata["legacy_probe_adapters"] = list(adapter_runs)
        generation_metadata["adapter_conflicts"] = [
            conflict
            for item in adapter_runs
            for conflict in item.get("conflicts", [])
        ]
        # Internal side channel for the planner. Public execution evidence
        # uses the serialized ``adapter_conflicts`` records above so witness
        # reports remain JSON-compatible.
        generation_metadata["_adapter_constraint_conflicts"] = list(
            adapter_constraint_conflicts
        )
    return data


def generate_witness_suite(
    schema: dict[str, list[str]] | SchemaCatalog,
    standard_sql: str,
    student_sql: str,
    *,
    max_rows_per_table: int = 8,
    max_worlds: int = _MAX_WITNESS_WORLDS,
    ast_diffs: list[ASTDiffNode] | None = None,
) -> WitnessSuite:
    """Plan and materialize independent databases for compatible obligations.

    The existing probe implementation remains the compatibility generator for
    now, but it receives only the AST differences assigned to one world.  This
    prevents unrelated JOIN, aggregate, DISTINCT, and window obligations from
    mutating the same database while their declarative replacements are
    migrated incrementally.
    """

    catalog = (
        schema
        if isinstance(schema, SchemaCatalog)
        else SchemaCatalog.from_legacy(schema)
    )
    legacy_schema = catalog.as_legacy()
    resolved_diffs = (
        ast_diffs
        if ast_diffs is not None
        else extract_ast_diffs(standard_sql, student_sql)
    )
    qualifications = (
        analyze_schema_qualification(standard_sql, catalog),
        analyze_schema_qualification(student_sql, catalog),
    )
    obligations = compile_obligations(
        resolved_diffs,
        schema=legacy_schema,
        qualifications=qualifications,
    )
    isolated_limit = (
        max_worlds
        if len(obligations) <= 1
        else max(1, max_worlds - 1)
    )
    suite = WitnessPlanner(max_worlds=isolated_limit).plan(obligations)
    if len(obligations) > 1 and len(suite.worlds) < max_worlds:
        # Keep one compatibility world for interactions that genuinely need
        # several otherwise independent operators (for example a boundary
        # predicate over a DISTINCT window projection).  It is never used as
        # isolated attribution evidence unless its atomic mutants pass.
        composite = WitnessWorld(
            id=f"world_{len(suite.worlds) + 1:02d}",
            obligation_ids=[item.id for item in obligations],
            diff_ids=[item.diff_id for item in obligations],
            minimum_rows={
                table: max(
                    item.minimum_rows.get(table, 0)
                    for item in obligations
                )
                for table in {
                    table
                    for item in obligations
                    for table in item.minimum_rows
                }
            },
            diagnostics=["compatibility_composite_world"],
        )
        suite.worlds.append(composite)
    diff_by_id = {
        stable_diff_id(diff, index): diff
        for index, diff in enumerate(resolved_diffs)
    }
    obligation_by_id = {item.id: item for item in obligations}

    pending_worlds = list(suite.worlds)
    materialized_worlds: list[WitnessWorld] = []
    split_serial = 0
    while pending_worlds:
        world = pending_worlds.pop(0)
        world_diffs = [
            diff_by_id[diff_id]
            for diff_id in world.diff_ids
            if diff_id in diff_by_id
        ]
        required_rows = max(world.minimum_rows.values(), default=0)
        world_row_limit = min(
            _MAX_WITNESS_ROWS_PER_TABLE,
            max(max_rows_per_table, required_rows),
        )
        write_audit: list[Any] = []
        generation_metadata: dict[str, Any] = {}
        world.database = generate_test_database(
            legacy_schema,
            standard_sql,
            student_sql,
            max_rows_per_table=world_row_limit,
            ast_diffs=world_diffs,
            write_audit=write_audit,
            generation_metadata=generation_metadata,
            defer_witness_finalization=True,
            obligations=[
                obligation_by_id[obligation_id]
                for obligation_id in world.obligation_ids
                if obligation_id in obligation_by_id
            ],
            schema_catalog=catalog,
        )
        structured_conflicts = generation_metadata.pop(
            "_adapter_constraint_conflicts", []
        )
        if structured_conflicts:
            conflict = structured_conflicts[0]
            projected_world_count = (
                len(materialized_worlds) + len(pending_worlds) + 2
            )
            if projected_world_count <= max_worlds:
                # Do not clone a generated compatibility database. Each side
                # must be rebuilt from its own obligation/diff subset so no
                # legacy write or TrackedRow audit leaks across worlds.
                world.database = {}
                split_serial += 1
                left, right = split_world_on_conflict(
                    world,
                    conflict,
                    right_world_id=f"{world.id}_split_{split_serial:02d}",
                )
                for candidate in (left, right):
                    candidate.database = {}
                    if len(candidate.obligation_ids) == 1:
                        candidate.diagnostics = [
                            item
                            for item in candidate.diagnostics
                            if item != "compatibility_composite_world"
                        ]
                        candidate.diagnostics.append(
                            "constraint_conflict_isolated_world"
                        )
                pending_worlds[0:0] = [left, right]
                suite.planner_diagnostics.append(
                    "adapter_constraint_conflict_split:"
                    f"{world.id}:{'.'.join(conflict.target)}"
                )
                continue
            world.diagnostics.append("adapter_conflict_world_limit_reached")
            suite.planner_diagnostics.append(
                "adapter_constraint_conflict_world_limit_reached:"
                f"{world.id}:{'.'.join(conflict.target)}"
            )
        world.execution["legacy_probe_adapters"] = {
            "registered": len(LEGACY_PROBE_REGISTRY),
            "migrated": [
                "logical_truth_table",
                "comparison_boundary",
                "null_tristate",
                "join_key_drift",
                "join_matched_dangling",
                "group_grain_split",
                "order_key_separation",
            ],
            "conflict_policy": "split_world",
            "generation_scope": generation_metadata.get("world_probe_scope", {}),
            "world_diff_ids": generation_metadata.get("world_diff_ids", []),
            "runs": generation_metadata.get("legacy_probe_adapters", []),
        }
        world.execution["adapter_conflicts"] = generation_metadata.get(
            "adapter_conflicts", []
        )
        if world.execution["adapter_conflicts"]:
            world.diagnostics.append("adapter_conflict_requires_world_split")
        with write_owner("planner:cell_constraints"):
            constraint_report = apply_cell_constraints(
                world.database,
                world.constraints,
            )
        # Cell constraints are applied after the legacy compatibility probes.
        # Re-run the integrity guard because a malformed constraint must not be
        # able to reintroduce an AST node/string into a numeric witness column.
        _finalize_generated_witness_data(
            world.database,
            standard_sql,
            student_sql,
            world_diffs,
            generation_scope=generation_metadata.get("world_probe_scope", {}),
            obligations=[
                obligation_by_id[obligation_id]
                for obligation_id in world.obligation_ids
                if obligation_id in obligation_by_id
            ],
            schema_catalog=catalog,
        )
        # The window compatibility finalizer rewrites ORDER BY cells after
        # the planner pass. Re-assert only the window-owned NULL placement
        # cells here. Other semantic materializers (especially nested
        # membership) intentionally refine generic boundary constraints; a
        # global replay would erase those multi-table paths.
        final_constraints = [
            item
            for item in world.constraints
            if item.owner == "window_partition_ties"
        ]
        if final_constraints:
            with write_owner("planner:cell_constraints:final"):
                final_constraint_report = apply_cell_constraints(
                    world.database,
                    final_constraints,
                )
            constraint_report = {
                "applied": constraint_report.get("applied", [])
                + final_constraint_report.get("applied", []),
                "unsatisfied": final_constraint_report.get("unsatisfied", []),
                "constraints_satisfied": bool(
                    constraint_report.get("constraints_satisfied")
                    and final_constraint_report.get("constraints_satisfied")
                ),
                "post_finalization_reapplied": True,
            }
        else:
            constraint_report["post_finalization_reapplied"] = False
        world.execution["legacy_write_audit"] = summarize_write_audit(write_audit)
        world.execution["legacy_write_audit"].update({
            "constraint_application_writes_excluded": False,
            "finalization_writes_included": True,
        })
        declarations = [
            declare_strategy(obligation_by_id[obligation_id])
            for obligation_id in world.obligation_ids
            if obligation_id in obligation_by_id
        ]
        world.execution["planning"] = {
            "strategies": [item.strategy for item in declarations],
            "semantic_constraints": [
                spec.kind
                for declaration in declarations
                for spec in declaration.semantic_constraints
            ],
            "row_limit": world_row_limit,
        }
        world.execution["constraint_application"] = constraint_report
        if not constraint_report["constraints_satisfied"]:
            world.diagnostics.append("declared_constraint_not_materialized")
        materialized_worlds.append(world)

    suite.worlds = materialized_worlds
    return suite


def _run_mutation_tests(
    *,
    schema: dict[str, list[str]],
    rows: dict[str, list[dict[str, Any]]],
    standard_sql: str,
    student_sql: str,
    standard_columns: list[str],
    standard_rows: list[tuple[Any, ...]],
    original_is_equivalent: bool = False,
    ordered: bool,
    schema_types: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any]:
    """
    变分隔离测试核心入口：基于 AST 对各算子进行单变量替换与移除测试，收集 Mutant 执行证据。
    Runs mutation tests by creating mutated student SQL variants (replacing/removing clauses)
    and evaluating them in the sandbox to isolate and locate specific faulty operators.
    """
    standard_ast = _parse_sql(standard_sql)
    student_ast = _parse_sql(student_sql)
    if standard_ast is None or student_ast is None:
        return {
            "enabled": False,
            "summary": {"executed": 0, "fixed_by_replacement": 0},
            "tests": [],
            "error": "parse_failed",
        }

    # 定义要参与变分比对的核心算子列表
    specs = [
        {"clause": "WHERE", "knowledge_point_id": "where", "arg": "where", "node_type": exp.Where},
        {"clause": "GROUP BY", "knowledge_point_id": "group-by", "arg": "group", "node_type": exp.Group},
        {"clause": "HAVING", "knowledge_point_id": "having", "arg": "having", "node_type": exp.Having},
        {"clause": "ORDER BY", "knowledge_point_id": "order-by", "arg": "order", "node_type": exp.Order},
        {"clause": "LIMIT", "knowledge_point_id": "limit", "arg": "limit", "node_type": exp.Limit},
        {"clause": "OFFSET", "knowledge_point_id": "limit", "arg": "offset", "node_type": exp.Offset},
    ]
    mutation_context = {
        "schema_types": schema_types or {},
    }
    equivalent_token = _MUTATION_ORIGINAL_EQUIVALENT.set(original_is_equivalent)

    try:
        result = _collect_mutation_test_results(
            specs=specs,
            schema=schema,
            rows=rows,
            standard_ast=standard_ast,
            student_ast=student_ast,
            standard_columns=standard_columns,
            standard_rows=standard_rows,
            ordered=ordered,
            mutation_context=mutation_context,
        )
    finally:
        _MUTATION_ORIGINAL_EQUIVALENT.reset(equivalent_token)
    return result


def _collect_mutation_test_results(
    *,
    specs: list[dict[str, Any]],
    schema: dict[str, list[str]],
    rows: dict[str, list[dict[str, Any]]],
    standard_ast: exp.Expression,
    student_ast: exp.Expression,
    standard_columns: list[str],
    standard_rows: list[tuple[Any, ...]],
    ordered: bool,
    mutation_context: dict[str, Any],
) -> dict[str, Any]:
    tests: list[dict[str, Any]] = []
    for query_scope, standard_query, student_query in _paired_query_blocks(
        standard_ast,
        student_ast,
    ):
        for spec in specs:
            dependent_changes: list[str] = []
            # Clause arguments are intentionally direct lookups. Falling back
            # to find() promotes a nested clause into the outer query block.
            std_node = standard_query.args.get(spec["arg"])
            stu_node = student_query.args.get(spec["arg"])

            if std_node is None and stu_node is None:
                continue
            if std_node is not None and stu_node is not None and _sql_of(std_node) == _sql_of(stu_node):
                continue

            if stu_node is not None and std_node is not None:
                replacement_sql = None
                if spec["clause"] == "WHERE":
                    standard_from = standard_query.args.get("from_")
                    student_from = student_query.args.get("from_")
                    standard_source = _direct_from_table(
                        standard_query if isinstance(standard_query, exp.Select) else None
                    )
                    student_source = _direct_from_table(
                        student_query if isinstance(student_query, exp.Select) else None
                    )
                    from_alias_changed = (
                        standard_source is not None
                        and student_source is not None
                        and _norm_name(standard_source.name) == _norm_name(student_source.name)
                        and _sql_of(standard_from) != _sql_of(student_from)
                    )
                    correlated_where = (
                        isinstance(std_node, exp.Where)
                        and (
                            std_node.find(exp.Subquery) is not None
                            or std_node.find(exp.Exists) is not None
                            or std_node.find(exp.In) is not None
                        )
                    )
                    if correlated_where and from_alias_changed:
                        mutated = student_ast.copy()
                        target_scope = _query_block_scope_key(student_query)
                        mutated_query = next(
                            (
                                node for node in mutated.walk()
                                if isinstance(node, exp.Query)
                                and _query_block_scope_key(node) == target_scope
                            ),
                            None,
                        )
                        if isinstance(mutated_query, exp.Query):
                            mutated_query.set("where", std_node.copy())
                            mutated_query.set(
                                "from_",
                                standard_from.copy()
                                if isinstance(standard_from, exp.Expression)
                                else None,
                            )
                            dependent_changes.append("FROM ALIAS")
                            if (
                                isinstance(standard_query, exp.Select)
                                and isinstance(student_query, exp.Select)
                                and [_sql_of(item) for item in standard_query.expressions]
                                != [_sql_of(item) for item in student_query.expressions]
                                and [_unqualified_sql(item) for item in standard_query.expressions]
                                == [_unqualified_sql(item) for item in student_query.expressions]
                            ):
                                mutated_query.set(
                                    "expressions",
                                    [item.copy() for item in standard_query.expressions],
                                )
                                dependent_changes.append("SELECT")
                            replacement_sql = _sql_of(mutated)
                if replacement_sql is None:
                    replacement_sql = _mutate_by_node_replacement(student_ast, stu_node, std_node)
                if replacement_sql is None:
                    replacement_sql = _mutate_query_arg(
                        student_ast,
                        student_query,
                        spec["arg"],
                        std_node,
                    )
            else:
                replacement_sql = _mutate_query_arg(
                    student_ast,
                    student_query,
                    spec["arg"],
                    std_node,
                )

            if stu_node is not None:
                removal_sql = _mutate_by_node_replacement(student_ast, stu_node, None)
                if removal_sql is None:
                    removal_sql = _mutate_query_arg(
                        student_ast,
                        student_query,
                        spec["arg"],
                        None,
                    )
            else:
                removal_sql = None

            kp_id = spec["knowledge_point_id"]
            if std_node is not None:
                kp_id = _find_kp_override(std_node, kp_id)
            elif stu_node is not None:
                kp_id = _find_kp_override(stu_node, kp_id)

            tests.append(_execute_mutation_case(
                schema=schema,
                rows=rows,
                clause=spec["clause"],
                knowledge_point_id=kp_id,
                replacement_sql=replacement_sql,
                removal_sql=removal_sql,
                standard_columns=standard_columns,
                standard_rows=standard_rows,
                ordered=ordered,
                mutation_scope=[spec["clause"]],
                query_scope=query_scope,
                dependent_changes=dependent_changes,
                **mutation_context,
            ))

    placement_test = _run_join_predicate_placement_mutation(
        schema=schema,
        rows=rows,
        standard_ast=standard_ast,
        student_ast=student_ast,
        standard_columns=standard_columns,
        standard_rows=standard_rows,
        ordered=ordered,
        **mutation_context,
    )
    if placement_test:
        tests.append(placement_test)

    # 3. 针对 JOIN ON 进行专项的连接条件变分测试
    join_test = _run_join_on_mutation(
        schema=schema,
        rows=rows,
        standard_ast=standard_ast,
        student_ast=student_ast,
        standard_columns=standard_columns,
        standard_rows=standard_rows,
        ordered=ordered,
        **mutation_context,
    )
    if join_test:
        tests.append(join_test)

    tests.extend(_run_join_clause_mutations(
        schema=schema,
        rows=rows,
        standard_ast=standard_ast,
        student_ast=student_ast,
        standard_columns=standard_columns,
        standard_rows=standard_rows,
        ordered=ordered,
        **mutation_context,
    ))
    tests.extend(_run_distinct_mutation(
        schema=schema,
        rows=rows,
        standard_ast=standard_ast,
        student_ast=student_ast,
        standard_columns=standard_columns,
        standard_rows=standard_rows,
        ordered=ordered,
        **mutation_context,
    ))
    tests.extend(_run_projection_mutation(
        schema=schema,
        rows=rows,
        standard_ast=standard_ast,
        student_ast=student_ast,
        standard_columns=standard_columns,
        standard_rows=standard_rows,
        ordered=ordered,
        **mutation_context,
    ))

    for specialized_test in (
        _run_subquery_membership_key_mutation(
            schema=schema,
            rows=rows,
            standard_ast=standard_ast,
            student_ast=student_ast,
            standard_columns=standard_columns,
            standard_rows=standard_rows,
            ordered=ordered,
            **mutation_context,
        ),
        _run_correlated_predicate_mutation(
            schema=schema,
            rows=rows,
            standard_ast=standard_ast,
            student_ast=student_ast,
            standard_columns=standard_columns,
            standard_rows=standard_rows,
            ordered=ordered,
            **mutation_context,
        ),
        _run_join_structure_mutation(
            schema=schema,
            rows=rows,
            standard_ast=standard_ast,
            student_ast=student_ast,
            standard_columns=standard_columns,
            standard_rows=standard_rows,
            ordered=ordered,
            **mutation_context,
        ),
        _run_aggregate_clause_placement_mutation(
            schema=schema,
            rows=rows,
            standard_ast=standard_ast,
            student_ast=student_ast,
            standard_columns=standard_columns,
            standard_rows=standard_rows,
            ordered=ordered,
            **mutation_context,
        ),
        _run_grouping_shape_mutation(
            schema=schema,
            rows=rows,
            standard_ast=standard_ast,
            student_ast=student_ast,
            standard_columns=standard_columns,
            standard_rows=standard_rows,
            ordered=ordered,
            **mutation_context,
        ),
        _run_join_type_mutation(
            schema=schema,
            rows=rows,
            standard_ast=standard_ast,
            student_ast=student_ast,
            standard_columns=standard_columns,
            standard_rows=standard_rows,
            ordered=ordered,
            **mutation_context,
        ),
        _run_expression_node_mutation(
            schema=schema,
            rows=rows,
            standard_ast=standard_ast,
            student_ast=student_ast,
            node_type=exp.Case,
            clause="CASE",
            knowledge_point_id="case",
            standard_columns=standard_columns,
            standard_rows=standard_rows,
            ordered=ordered,
            **mutation_context,
        ),
        _run_expression_node_mutation(
            schema=schema,
            rows=rows,
            standard_ast=standard_ast,
            student_ast=student_ast,
            node_type=exp.Window,
            clause="WINDOW",
            knowledge_point_id="window-row-number",
            standard_columns=standard_columns,
            standard_rows=standard_rows,
            ordered=ordered,
            **mutation_context,
        ),
        _run_scalar_function_mutation(
            schema=schema,
            rows=rows,
            standard_ast=standard_ast,
            student_ast=student_ast,
            standard_columns=standard_columns,
            standard_rows=standard_rows,
            ordered=ordered,
            **mutation_context,
        ),
        _run_set_operator_mutation(
            schema=schema,
            rows=rows,
            standard_ast=standard_ast,
            student_ast=student_ast,
            standard_columns=standard_columns,
            standard_rows=standard_rows,
            ordered=ordered,
            **mutation_context,
        ),
        _run_cte_mutation(
            schema=schema,
            rows=rows,
            standard_ast=standard_ast,
            student_ast=student_ast,
            standard_columns=standard_columns,
            standard_rows=standard_rows,
            ordered=ordered,
            **mutation_context,
        ),
        _run_aggregate_function_mutation(
            schema=schema,
            rows=rows,
            standard_ast=standard_ast,
            student_ast=student_ast,
            standard_columns=standard_columns,
            standard_rows=standard_rows,
            ordered=ordered,
            **mutation_context,
        ),
        _run_recursive_cte_mutation(
            schema=schema,
            rows=rows,
            standard_ast=standard_ast,
            student_ast=student_ast,
            standard_columns=standard_columns,
            standard_rows=standard_rows,
            ordered=ordered,
            **mutation_context,
        ),
        _run_like_pattern_mutation(
            schema=schema,
            rows=rows,
            standard_ast=standard_ast,
            student_ast=student_ast,
            standard_columns=standard_columns,
            standard_rows=standard_rows,
            ordered=ordered,
            **mutation_context,
        ),
        _run_glob_pattern_mutation(
            schema=schema,
            rows=rows,
            standard_ast=standard_ast,
            student_ast=student_ast,
            standard_columns=standard_columns,
            standard_rows=standard_rows,
            ordered=ordered,
            **mutation_context,
        ),
    ):
        if specialized_test:
            tests.append(specialized_test)

    return {
        "enabled": True,
        "summary": {
            "executed": sum(1 for test in tests if test.get("replacement_exec_ok") or test.get("removal_exec_ok")),
            "fixed_by_replacement": sum(1 for test in tests if test.get("fixed_by_replacement")),
            "remove_kept_correct": sum(1 for test in tests if test.get("removed_student_clause_equivalent")),
        },
        "tests": tests,
    }


def _run_grouping_shape_mutation(
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
    """Restore a grouping grain together with its dependent projection."""
    standard_select = _top_select(standard_ast)
    student_select = _top_select(student_ast)
    if not isinstance(standard_select, exp.Select) or not isinstance(student_select, exp.Select):
        return None
    standard_group = standard_select.args.get("group")
    student_group = student_select.args.get("group")
    projections_changed = [
        _sql_of(item) for item in standard_select.expressions
    ] != [
        _sql_of(item) for item in student_select.expressions
    ]
    if _sql_of(standard_group) == _sql_of(student_group) or not projections_changed:
        return None

    mutated = student_ast.copy()
    mutated_select = _top_select(mutated)
    if not isinstance(mutated_select, exp.Select):
        return None
    mutated_select.set(
        "group",
        standard_group.copy() if isinstance(standard_group, exp.Expression) else None,
    )
    mutated_select.set(
        "expressions",
        [item.copy() for item in standard_select.expressions],
    )
    return _execute_mutation_case(
        schema=schema,
        rows=rows,
        clause="GROUP BY",
        knowledge_point_id="group-by",
        replacement_sql=_sql_of(mutated),
        removal_sql=None,
        standard_columns=standard_columns,
        standard_rows=standard_rows,
        ordered=ordered,
        schema_types=schema_types,
        action="restore_grouping_grain_and_dependent_projection",
        mutation_scope=["GROUP BY"],
        dependent_changes=["SELECT"],
    )


def _run_join_predicate_placement_mutation(
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
    placements = _outer_join_predicate_placement_ast_diffs(
        standard_ast,
        student_ast,
    )
    if len(placements) != 1:
        return None
    placement = placements[0]
    join_index = placement.extra.get("join_index")
    if not isinstance(join_index, int):
        return None
    standard_select = _top_select(standard_ast)
    mutated = student_ast.copy()
    mutated_select = _top_select(mutated)
    if not isinstance(standard_select, exp.Select) or not isinstance(
        mutated_select, exp.Select
    ):
        return None
    standard_joins = list(standard_select.args.get("joins") or ())
    mutated_joins = list(mutated_select.args.get("joins") or ())
    if join_index >= len(standard_joins) or join_index >= len(mutated_joins):
        return None

    standard_on = standard_joins[join_index].args.get("on")
    mutated_joins[join_index].set(
        "on",
        standard_on.copy() if isinstance(standard_on, exp.Expression) else None,
    )
    standard_where = standard_select.args.get("where")
    mutated_select.set(
        "where",
        standard_where.copy()
        if isinstance(standard_where, exp.Expression)
        else None,
    )
    return _execute_mutation_case(
        schema=schema,
        rows=rows,
        clause="JOIN ON + WHERE",
        knowledge_point_id="join-on",
        replacement_sql=_sql_of(mutated),
        removal_sql=None,
        standard_columns=standard_columns,
        standard_rows=standard_rows,
        ordered=ordered,
        schema_types=schema_types,
        action="move_outer_join_predicate_to_standard_clause",
        mutation_scope=["JOIN ON", "WHERE"],
        query_scope=str(placement.extra.get("query_scope") or "root"),
        dependent_changes=["WHERE"],
    )


def _run_join_on_mutation(
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
    if _outer_join_predicate_placement_ast_diffs(standard_ast, student_ast):
        # Predicate placement is one dependent JOIN ON + WHERE edit.  A bare
        # ON replacement is not a valid causal repair for that obligation.
        return None
    standard_joins = list(standard_ast.find_all(exp.Join))
    student_joins = list(student_ast.find_all(exp.Join))
    if not standard_joins or not student_joins:
        return None
    std_on = [join.args.get("on") for join in standard_joins]
    stu_on = [join.args.get("on") for join in student_joins]
    if [_sql_of(node) for node in std_on] == [_sql_of(node) for node in stu_on]:
        return None
    if (
        [_join_type_signature(join) for join in standard_joins]
        != [_join_type_signature(join) for join in student_joins]
        and any((std is None) != (stu is None) for std, stu in zip(std_on, stu_on))
    ):
        # The join-type mutation owns the ON-clause dependency for CROSS JOIN
        # and outer-join topology changes.
        return None

    mutated = student_ast.copy()
    mutated_joins = list(mutated.find_all(exp.Join))
    for idx, join in enumerate(mutated_joins):
        replacement = std_on[idx] if idx < len(std_on) else None
        if replacement is not None:
            join.set("on", replacement.copy())
    replacement_sql = _sql_of(mutated)
    return _execute_mutation_case(
        schema=schema,
        rows=rows,
        clause="JOIN ON",
        knowledge_point_id="join-on",
        replacement_sql=replacement_sql,
        removal_sql=None,
        standard_columns=standard_columns,
        standard_rows=standard_rows,
        ordered=ordered,
        schema_types=schema_types,
    )


def _run_distinct_mutation(
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
    tests: list[dict[str, Any]] = []
    for query_scope, standard_query, student_query in _paired_query_blocks(
        standard_ast,
        student_ast,
    ):
        if not isinstance(standard_query, exp.Select) or not isinstance(student_query, exp.Select):
            continue
        std_distinct = standard_query.args.get("distinct")
        stu_distinct = student_query.args.get("distinct")
        if _sql_of(std_distinct) == _sql_of(stu_distinct):
            continue
        clause = "DISTINCT"
        tests.append(_execute_mutation_case(
            schema=schema,
            rows=rows,
            clause=clause,
            knowledge_point_id="distinct",
            replacement_sql=_mutate_query_arg(
                student_ast,
                student_query,
                "distinct",
                std_distinct,
            ),
            removal_sql=(
                _mutate_query_arg(student_ast, student_query, "distinct", None)
                if stu_distinct is not None
                else None
            ),
            standard_columns=standard_columns,
            standard_rows=standard_rows,
            ordered=ordered,
            schema_types=schema_types,
            mutation_scope=[clause],
            query_scope=query_scope,
            # A root DISTINCT may be latent on one bounded fixture while still
            # being valid structural isolation evidence. Nested DISTINCT must
            # affect the final result before it can be credited as a repair.
            allow_equivalent_original_fix=query_scope == "root",
        ))
    return tests


def _run_projection_mutation(
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
    tests: list[dict[str, Any]] = []
    for query_scope, standard_query, student_query in _paired_query_blocks(
        standard_ast,
        student_ast,
    ):
        if not isinstance(standard_query, exp.Select) or not isinstance(student_query, exp.Select):
            continue
        std_exprs = standard_query.expressions
        stu_exprs = student_query.expressions
        if [_sql_of(expr) for expr in std_exprs] == [_sql_of(expr) for expr in stu_exprs]:
            continue
        aggregate_projection = any(
            isinstance(expression, exp.AggFunc)
            or expression.find(exp.AggFunc) is not None
            for expression in [*std_exprs, *stu_exprs]
        )
        clause = "AGGREGATE" if aggregate_projection else "SELECT"
        knowledge_point_id = "aggregate" if aggregate_projection else "select-basic"
        tests.append(_execute_mutation_case(
            schema=schema,
            rows=rows,
            clause=clause,
            knowledge_point_id=knowledge_point_id,
            replacement_sql=_mutate_query_expressions(
                student_ast,
                student_query,
                std_exprs,
            ),
            removal_sql=None,
            standard_columns=standard_columns,
            standard_rows=standard_rows,
            ordered=ordered,
            schema_types=schema_types,
            mutation_scope=[clause],
            query_scope=query_scope,
        ))
    return tests


def _run_join_type_mutation(
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
    standard_joins = list(standard_ast.find_all(exp.Join))
    student_joins = list(student_ast.find_all(exp.Join))
    if not standard_joins or not student_joins:
        return None

    std_types = [_join_type_signature(join) for join in standard_joins]
    stu_types = [_join_type_signature(join) for join in student_joins]
    if std_types == stu_types:
        return None

    mutated = student_ast.copy()
    mutated_joins = list(mutated.find_all(exp.Join))
    for idx, join in enumerate(mutated_joins):
        if idx >= len(standard_joins):
            break
        std_join = standard_joins[idx]
        student_join = student_joins[idx]
        join.set("side", std_join.args.get("side"))
        join.set("kind", std_join.args.get("kind"))
        standard_on = std_join.args.get("on")
        student_on = student_join.args.get("on")
        if (standard_on is None) != (student_on is None):
            # CROSS/implicit comma joins and predicate-bearing joins differ in
            # both type and ON presence. Restore that direct dependency in the
            # same atomic topology intervention; leave independently changed
            # predicates alone when both sides already have an ON clause.
            join.set(
                "on",
                standard_on.copy()
                if isinstance(standard_on, exp.Expression)
                else None,
            )

    on_presence_changed = any(
        (std_join.args.get("on") is None) != (stu_join.args.get("on") is None)
        for std_join, stu_join in zip(standard_joins, student_joins)
    )

    return _execute_mutation_case(
        schema=schema,
        rows=rows,
        clause="JOIN TYPE",
        knowledge_point_id=_join_type_kp(standard_joins[0]),
        replacement_sql=_sql_of(mutated),
        removal_sql=None,
        standard_columns=standard_columns,
        standard_rows=standard_rows,
        ordered=ordered,
        schema_types=schema_types,
        action="restore_join_type_and_direct_dependencies",
        mutation_scope=(
            ["JOIN TYPE", "JOIN ON"] if on_presence_changed else ["JOIN TYPE"]
        ),
        dependent_changes=["JOIN ON"] if on_presence_changed else [],
    )


def _run_expression_node_mutation(
    *,
    schema: dict[str, list[str]],
    rows: dict[str, list[dict[str, Any]]],
    standard_ast: exp.Expression,
    student_ast: exp.Expression,
    node_type: type[exp.Expression],
    clause: str,
    knowledge_point_id: str,
    standard_columns: list[str],
    standard_rows: list[tuple[Any, ...]],
    ordered: bool,
    schema_types: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any] | None:
    standard_nodes = list(standard_ast.find_all(node_type))
    student_nodes = list(student_ast.find_all(node_type))
    if not standard_nodes or not student_nodes:
        return None
    if [_sql_of(node) for node in standard_nodes] == [_sql_of(node) for node in student_nodes]:
        return None

    mutated = student_ast.copy()
    mutated_nodes = list(mutated.find_all(node_type))
    for idx, node in enumerate(mutated_nodes):
        if idx >= len(standard_nodes):
            break
        node.replace(standard_nodes[idx].copy())

    return _execute_mutation_case(
        schema=schema,
        rows=rows,
        clause=clause,
        knowledge_point_id=knowledge_point_id,
        replacement_sql=_sql_of(mutated),
        removal_sql=None,
        standard_columns=standard_columns,
        standard_rows=standard_rows,
        ordered=ordered,
        schema_types=schema_types,
    )


def _run_correlated_predicate_mutation(
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
    """Repair exactly one scope-resolved correlated comparison."""
    diffs = _correlated_subquery_context_ast_diffs(
        standard_ast,
        student_ast,
    )
    focused = [
        item
        for item in diffs
        if item.diff_type == "correlated_predicate_changed"
        and item.extra.get("query_scope") in {None, "nested_correlation"}
        and isinstance(item.standard_node, exp.Expression)
        and isinstance(item.student_node, exp.Expression)
    ]
    if len(focused) != 1:
        return None
    diff = focused[0]
    standard_target = diff.standard_node
    student_target = diff.student_node
    # EXISTS polarity is represented by a parent NOT node. Replacing only the
    # EXISTS child would leave NOT EXISTS in place and falsely report a repair.
    if isinstance(student_target.parent, exp.Not) and not isinstance(
        standard_target.parent, exp.Not
    ):
        student_target = student_target.parent
    elif isinstance(standard_target.parent, exp.Not) and not isinstance(
        student_target.parent, exp.Not
    ):
        standard_target = standard_target.parent
    replacement_sql = _mutate_by_node_replacement(
        student_ast,
        student_target,
        standard_target,
    )
    if replacement_sql is None:
        return None
    return _execute_mutation_case(
        schema=schema,
        rows=rows,
        clause="CORRELATED SUBQUERY",
        knowledge_point_id="subquery-correlated",
        replacement_sql=replacement_sql,
        removal_sql=None,
        standard_columns=standard_columns,
        standard_rows=standard_rows,
        ordered=ordered,
        schema_types=schema_types,
        action=(
            "restore_correlated_predicate"
            if student_target is not diff.student_node
            or standard_target is not diff.standard_node
            else "restore_correlated_comparison"
        ),
        mutation_scope=["CORRELATED SUBQUERY"],
        query_scope="nested_correlation",
    )


def _run_subquery_membership_key_mutation(
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
    diffs = _subquery_membership_key_ast_diffs(standard_ast, student_ast)
    if len(diffs) != 1:
        return None
    diff = diffs[0]
    replacement_sql = _mutate_by_node_replacement(
        student_ast,
        diff.student_node,
        diff.standard_node,
    )
    if replacement_sql is None:
        return None
    return _execute_mutation_case(
        schema=schema,
        rows=rows,
        clause="IN",
        knowledge_point_id="subquery-in",
        replacement_sql=replacement_sql,
        removal_sql=None,
        standard_columns=standard_columns,
        standard_rows=standard_rows,
        ordered=ordered,
        schema_types=schema_types,
        action="restore_subquery_membership_key",
        mutation_scope=["IN"],
        query_scope="nested_membership",
    )


def _run_scalar_function_mutation(
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
    standard_nodes = _scalar_function_roots(standard_ast)
    student_nodes = _scalar_function_roots(student_ast)
    if not standard_nodes or len(standard_nodes) != len(student_nodes):
        return None
    if [_sql_of(node) for node in standard_nodes] == [
        _sql_of(node) for node in student_nodes
    ]:
        return None

    mutated = student_ast.copy()
    mutated_nodes = _scalar_function_roots(mutated)
    if len(mutated_nodes) != len(standard_nodes):
        return None
    changed_indexes = [
        index
        for index, (standard_node, student_node) in enumerate(
            zip(standard_nodes, student_nodes)
        )
        if _sql_of(student_node) != _sql_of(standard_node)
    ]
    regex_only_mutation = (
        len(changed_indexes) == 1
        and isinstance(standard_nodes[changed_indexes[0]], exp.RegexpLike)
        and isinstance(student_nodes[changed_indexes[0]], exp.RegexpLike)
    )
    for index in changed_indexes:
        mutated_nodes[index].replace(standard_nodes[index].copy())

    clause = "PREDICATE" if regex_only_mutation else "FUNCTION"
    knowledge_point_id = "regex" if regex_only_mutation else "function"
    mutation_scope = ["REGEXP"] if regex_only_mutation else ["FUNCTION"]

    return _execute_mutation_case(
        schema=schema,
        rows=rows,
        clause=clause,
        knowledge_point_id=knowledge_point_id,
        replacement_sql=_sql_of(mutated),
        removal_sql=None,
        standard_columns=standard_columns,
        standard_rows=standard_rows,
        ordered=ordered,
        schema_types=schema_types,
        mutation_scope=mutation_scope,
    )


def _run_like_pattern_mutation(
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
    """Restore one changed LIKE pattern together with its ESCAPE node."""
    standard_nodes = list(standard_ast.find_all(exp.Like))
    student_nodes = list(student_ast.find_all(exp.Like))
    if not standard_nodes or len(standard_nodes) != len(student_nodes):
        return None

    standard_render_nodes = [_like_render_node(node) for node in standard_nodes]
    student_render_nodes = [_like_render_node(node) for node in student_nodes]
    changed_indexes = [
        index
        for index, (standard_node, student_node) in enumerate(
            zip(standard_render_nodes, student_render_nodes)
        )
        if _sql_of(standard_node) != _sql_of(student_node)
    ]
    if len(changed_indexes) != 1:
        return None
    index = changed_indexes[0]
    standard_node = standard_nodes[index]
    student_node = student_nodes[index]
    if type(standard_node) is not type(student_node):
        return None
    if not isinstance(standard_node.expression, exp.Literal) or not isinstance(
        student_node.expression, exp.Literal
    ):
        return None

    mutated = student_ast.copy()
    mutated_nodes = list(mutated.find_all(exp.Like))
    if len(mutated_nodes) != len(standard_nodes):
        return None
    mutated_render_nodes = [_like_render_node(node) for node in mutated_nodes]
    mutated_render_nodes[index].replace(standard_render_nodes[index].copy())
    predicate_name = "LIKE"
    return _execute_mutation_case(
        schema=schema,
        rows=rows,
        clause="PREDICATE",
        knowledge_point_id="like",
        replacement_sql=_sql_of(mutated),
        removal_sql=None,
        standard_columns=standard_columns,
        standard_rows=standard_rows,
        ordered=ordered,
        schema_types=schema_types,
        mutation_scope=[predicate_name],
    )


def _run_glob_pattern_mutation(
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
    """Restore exactly one changed constant GLOB predicate."""
    standard_nodes = list(standard_ast.find_all(exp.Glob))
    student_nodes = list(student_ast.find_all(exp.Glob))
    if not standard_nodes or len(standard_nodes) != len(student_nodes):
        return None
    changed_indexes = [
        index
        for index, (standard_node, student_node) in enumerate(
            zip(standard_nodes, student_nodes)
        )
        if _sql_of(standard_node) != _sql_of(student_node)
    ]
    if len(changed_indexes) != 1:
        return None
    index = changed_indexes[0]
    standard_node = standard_nodes[index]
    student_node = student_nodes[index]
    if not isinstance(standard_node.expression, exp.Literal) or not isinstance(
        student_node.expression, exp.Literal
    ):
        return None
    mutated = student_ast.copy()
    mutated_nodes = list(mutated.find_all(exp.Glob))
    if len(mutated_nodes) != len(standard_nodes):
        return None
    mutated_nodes[index].replace(standard_node.copy())
    return _execute_mutation_case(
        schema=schema,
        rows=rows,
        clause="PREDICATE",
        knowledge_point_id="glob",
        replacement_sql=_sql_of(mutated),
        removal_sql=None,
        standard_columns=standard_columns,
        standard_rows=standard_rows,
        ordered=ordered,
        schema_types=schema_types,
        mutation_scope=["GLOB"],
    )


def _run_set_operator_mutation(
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
    set_types = (exp.Union, exp.Intersect, exp.Except)

    def set_nodes(ast: exp.Expression) -> list[exp.Expression]:
        return [node for node in ast.walk() if isinstance(node, set_types)]

    standard_nodes = set_nodes(standard_ast)
    student_nodes = set_nodes(student_ast)
    if not standard_nodes:
        return None
    if _sql_of(standard_ast) == _sql_of(student_ast):
        return None

    # A set-operator mutation can remove the whole operator (for example
    # ``SELECT ... EXCEPT SELECT ...`` -> ``SELECT ...``).  In that shape the
    # student AST legitimately contains no set node to pair with.  The old
    # implementation therefore executed the wrong answer but produced no
    # repair evidence, which was reported as a mutation-evidence gap even
    # for the bounded generated corpus.  Restoring the authoritative
    # standard AST is the atomic repair for this exact deletion shape.
    if not student_nodes:
        standard_node = standard_nodes[0]
        return _execute_mutation_case(
            schema=schema,
            rows=rows,
            clause=_set_operator_name(standard_node) or "UNION",
            knowledge_point_id=_set_operator_kp(
                _set_operator_name(standard_node) or "UNION"
            ),
            replacement_sql=_sql_of(standard_ast),
            removal_sql=None,
            standard_columns=standard_columns,
            standard_rows=standard_rows,
            ordered=ordered,
            schema_types=schema_types,
            action="restore_removed_set_operator",
            mutation_scope=[_set_operator_name(standard_node) or "UNION"],
        )

    # Pair operators by AST walk order.  Real teaching queries frequently put
    # UNION inside a CTE or derived table; the old root-only implementation
    # silently omitted mutation evidence for those valid query shapes.
    changed_index = next(
        (
            index
            for index, (standard_node, student_node) in enumerate(
                zip(standard_nodes, student_nodes)
            )
            if (
                type(standard_node) is not type(student_node)
                or _set_operator_modifier(standard_node)
                != _set_operator_modifier(student_node)
                or _sql_of(standard_node) != _sql_of(student_node)
            )
        ),
        None,
    )
    if changed_index is None:
        return None

    standard_node = standard_nodes[changed_index]
    student_node = student_nodes[changed_index]
    mutated = student_ast.copy()
    mutated_nodes = set_nodes(mutated)
    if changed_index >= len(mutated_nodes):
        return None
    mutated_node = mutated_nodes[changed_index]

    if type(standard_node) is type(student_node):
        if _set_operator_modifier(standard_node) != _set_operator_modifier(student_node):
            for arg in ("distinct", "by_name", "side", "kind"):
                mutated_node.set(arg, standard_node.args.get(arg))
        else:
            # The operator and modifier are unchanged but one or both branch
            # bodies differ, so restore the paired nested branches as one
            # atomic mutation.
            mutated_node.set("this", standard_node.this.copy())
            mutated_node.set("expression", standard_node.expression.copy())
    else:
        # A type change (for example UNION -> INTERSECT) replaces only the
        # paired node, preserving all outer CTE/derived-table context.
        if mutated_node is mutated:
            mutated = standard_node.copy()
        else:
            mutated_node.replace(standard_node.copy())
    replacement_sql = _sql_of(mutated)
    return _execute_mutation_case(
        schema=schema,
        rows=rows,
        clause=_set_operator_name(standard_node) or "UNION",
        knowledge_point_id=_set_operator_kp(
            _set_operator_name(standard_node) or "UNION"
        ),
        replacement_sql=replacement_sql,
        removal_sql=None,
        standard_columns=standard_columns,
        standard_rows=standard_rows,
        ordered=ordered,
        schema_types=schema_types,
    )


def _run_cte_mutation(
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
    if _is_recursive_ast(standard_ast) or _is_recursive_ast(student_ast):
        return None
    standard_with = standard_ast.args.get("with_") or standard_ast.args.get("with")
    student_with = student_ast.args.get("with_") or student_ast.args.get("with")
    if _sql_of(standard_with) == _sql_of(student_with):
        return None

    mutated = student_ast.copy()
    mutated.set(
        "with_",
        standard_with.copy() if isinstance(standard_with, exp.Expression) else None,
    )
    dependent_changes: list[str] = []
    standard_select = _top_select(standard_ast)
    mutated_select = _top_select(mutated)
    if isinstance(standard_select, exp.Select) and isinstance(mutated_select, exp.Select):
        standard_from = standard_select.args.get("from_")
        mutated_from = mutated_select.args.get("from_")
        if _sql_of(standard_from) != _sql_of(mutated_from):
            mutated_select.set(
                "from_",
                standard_from.copy() if isinstance(standard_from, exp.Expression) else None,
            )
            dependent_changes.append("FROM")
    return _execute_mutation_case(
        schema=schema,
        rows=rows,
        clause="CTE",
        knowledge_point_id="cte",
        replacement_sql=_sql_of(mutated),
        removal_sql=None,
        standard_columns=standard_columns,
        standard_rows=standard_rows,
        ordered=ordered,
        schema_types=schema_types,
        action="restore_standard_cte_definitions_and_references",
        mutation_scope=["CTE"],
        dependent_changes=dependent_changes,
    )


def _run_aggregate_function_mutation(
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
    standard_aggs = list(standard_ast.find_all(*_AGG_FUNC_TYPES))
    student_aggs = list(student_ast.find_all(*_AGG_FUNC_TYPES))
    if not standard_aggs or len(standard_aggs) != len(student_aggs):
        return None
    if [_sql_of(node) for node in standard_aggs] == [_sql_of(node) for node in student_aggs]:
        return None

    def has_distinct(node: exp.Expression) -> bool:
        return bool(node.args.get("distinct") or isinstance(node.this, exp.Distinct))

    distinct_changed = (
        [type(node) for node in standard_aggs]
        == [type(node) for node in student_aggs]
        and any(
            has_distinct(standard_agg) != has_distinct(student_aggs[index])
            for index, standard_agg in enumerate(standard_aggs)
        )
    )
    mutated = student_ast.copy()
    mutated_aggs = list(mutated.find_all(*_AGG_FUNC_TYPES))
    for index, standard_agg in enumerate(standard_aggs):
        if _sql_of(standard_agg) != _sql_of(student_aggs[index]):
            mutated_aggs[index].replace(standard_agg.copy())
    clause = "DISTINCT" if distinct_changed else "AGGREGATE"
    knowledge_point_id = "distinct" if distinct_changed else "aggregate"
    return _execute_mutation_case(
        schema=schema,
        rows=rows,
        clause=clause,
        knowledge_point_id=knowledge_point_id,
        replacement_sql=_sql_of(mutated),
        removal_sql=None,
        standard_columns=standard_columns,
        standard_rows=standard_rows,
        ordered=ordered,
        schema_types=schema_types,
        action="replace_changed_aggregate_functions",
        mutation_scope=[clause],
    )


def _run_recursive_cte_mutation(
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
    if not (_is_recursive_ast(standard_ast) or _is_recursive_ast(student_ast)):
        return None
    standard_ctes = {_norm_name(cte.alias or ""): cte for cte in standard_ast.find_all(exp.CTE)}
    student_ctes = {_norm_name(cte.alias or ""): cte for cte in student_ast.find_all(exp.CTE)}
    changed_name = next(
        (
            name for name, standard_cte in standard_ctes.items()
            if name in student_ctes and _sql_of(standard_cte.this) != _sql_of(student_ctes[name].this)
        ),
        None,
    )
    if not changed_name:
        if _is_recursive_ast(standard_ast) != _is_recursive_ast(student_ast):
            return _execute_mutation_case(
                schema=schema,
                rows=rows,
                clause="RECURSIVE CTE",
                knowledge_point_id="cte-recursive",
                replacement_sql=_sql_of(standard_ast),
                removal_sql=None,
                standard_columns=standard_columns,
                standard_rows=standard_rows,
                ordered=ordered,
                schema_types=schema_types,
            )
        return None
    mutated = student_ast.copy()
    mutated_cte = next(
        (cte for cte in mutated.find_all(exp.CTE) if _norm_name(cte.alias or "") == changed_name),
        None,
    )
    if not mutated_cte:
        return None
    mutated_cte.set("this", standard_ctes[changed_name].this.copy())
    return _execute_mutation_case(
        schema=schema,
        rows=rows,
        clause="RECURSIVE CTE",
        knowledge_point_id="cte-recursive",
        replacement_sql=_sql_of(mutated),
        removal_sql=None,
        standard_columns=standard_columns,
        standard_rows=standard_rows,
        ordered=ordered,
        schema_types=schema_types,
        mutation_scope=["RECURSIVE CTE"],
    )


def _apply_cte_probes(
    data: dict[str, list[dict[str, Any]]],
    schema: dict[str, list[str]],
    standard_sql: str,
    student_sql: str,
    ast_diffs: list[ASTDiffNode],
) -> None:
    """
    CTE 基表探针：提取 CTE 内部引用的基表和约束。

    递归层级由后置的 ``_apply_recursive_cte_safety`` 独立负责，避免同一
    adapter 既修改普通 CTE 过滤数据又重复重写递归拓扑。
    """
    asts = [_parse_sql(standard_sql), _parse_sql(student_sql)]

    for ast in asts:
        if not ast:
            continue
        for cte in ast.find_all(exp.CTE):
            # 提取 CTE 内部引用的基表
            cte_tables = {_norm_name(t.name) for t in cte.find_all(exp.Table)}
            cte_aliases = {_norm_name(t.alias) for t in cte.find_all(exp.Table) if t.alias}
            inner_refs = cte_tables | cte_aliases

            # 对每个引用的基表，提取 WHERE 约束并应用
            for table_ref in inner_refs:
                table_actual = next((t for t in schema if _norm_name(t) == table_ref), None)
                if not table_actual or table_actual not in data:
                    continue
                rows = data[table_actual]
                columns = schema[table_actual]

                # 提取 CTE 内部的 WHERE 约束
                for where in cte.find_all(exp.Where):
                    constraints = []
                    for cmp in where.find_all(exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE):
                        col_node = cmp.left if isinstance(cmp.left, exp.Column) else cmp.right if isinstance(cmp.right, exp.Column) else None
                        lit_node = cmp.right if isinstance(cmp.left, exp.Column) else cmp.left if isinstance(cmp.right, exp.Column) else None
                        if isinstance(col_node, exp.Column) and isinstance(lit_node, exp.Literal):
                            col_table = _norm_name(col_node.table or table_actual)
                            if col_table == table_ref:
                                constraints.append({
                                    "column": col_node.name,
                                    "op": type(cmp).__name__,
                                    "value": _literal_value(lit_node),
                                    "table": table_actual,
                                })
                    if constraints:
                        _apply_constraints(rows, columns, constraints, {table_actual: columns})

            # When a CTE projects a relationship key used by another table,
            # repeated cyclic names can make opposite CTE predicates return
            # the same outer rows. Align unique keys across both tables.
            cte_select = cte.this.find(exp.Select) if isinstance(cte.this, exp.Expression) else None
            projection = (
                cte_select.expressions[0]
                if isinstance(cte_select, exp.Select) and cte_select.expressions
                else None
            )
            projection = projection.this if isinstance(projection, exp.Alias) else projection
            if isinstance(projection, exp.Column):
                projected_col = _norm_name(projection.name)
                base_table = next(
                    (name for name in data if _norm_name(name) in inner_refs),
                    None,
                )
                if base_table and data.get(base_table):
                    base_col = next(
                        (col for col in data[base_table][0] if _norm_name(col) == projected_col),
                        None,
                    )
                    if base_col:
                        for other_table, other_rows in data.items():
                            if other_table == base_table or not other_rows:
                                continue
                            other_col = next(
                                (col for col in other_rows[0] if _norm_name(col) == projected_col),
                                None,
                            )
                            if not other_col:
                                continue
                            for idx, row in enumerate(data[base_table]):
                                row[base_col] = f"cte_link_{idx:03d}"
                            link_count = max(2, len(data[base_table]) // 2)
                            for idx, row in enumerate(other_rows):
                                row[other_col] = f"cte_link_{idx % link_count:03d}"

                    cte_where = cte.find(exp.Where)
                    predicate = cte_where.find(exp.EQ, exp.NEQ) if cte_where else None
                    if predicate:
                        pred_col = predicate.left if isinstance(predicate.left, exp.Column) else predicate.right
                        pred_value_node = predicate.right if pred_col is predicate.left else predicate.left
                        if isinstance(pred_col, exp.Column) and isinstance(pred_value_node, exp.Literal):
                            actual_pred_col = next(
                                (
                                    col
                                    for col in data[base_table][0]
                                    if _norm_name(col) == _norm_name(pred_col.name)
                                ),
                                None,
                            )
                            if actual_pred_col:
                                split = max(1, len(data[base_table]) // 2)
                                expected_value = _literal_value(pred_value_node)
                                for idx, row in enumerate(data[base_table]):
                                    row[actual_pred_col] = expected_value if idx < split else "Shanghai"

def _apply_recursive_cte_hierarchy(
    data: dict[str, list[dict[str, Any]]],
    schema: dict[str, list[str]],
    cte: exp.CTE,
    query_ast: exp.Expression | None = None,
) -> None:
    cte_name = _norm_name(cte.alias or "")
    set_node = _set_operator_node(cte.this if isinstance(cte.this, exp.Expression) else None)
    recursive_branch = set_node.expression if isinstance(set_node, (exp.Union, exp.Intersect, exp.Except)) else None
    if not cte_name or not isinstance(recursive_branch, exp.Expression):
        return
    aliases = _table_aliases(recursive_branch)
    output_sources = _recursive_cte_output_sources(cte, set_node)
    anchor_branch = set_node.this if isinstance(set_node, (exp.Union, exp.Intersect, exp.Except)) else None
    anchor_select = (
        anchor_branch
        if isinstance(anchor_branch, exp.Select)
        else anchor_branch.find(exp.Select)
        if isinstance(anchor_branch, exp.Expression)
        else None
    )
    anchor_constraints = (
        _recursive_select_literal_constraints(anchor_select)
        if isinstance(anchor_select, exp.Select)
        else []
    )
    outer_constraints = _recursive_outer_literal_constraints(
        query_ast,
        cte_name,
        output_sources,
    )

    for comparison in recursive_branch.find_all(exp.EQ):
        left = comparison.left
        right = comparison.right
        if not isinstance(left, exp.Column) or not isinstance(right, exp.Column):
            continue
        left_table = aliases.get(_norm_name(left.table or ""), _norm_name(left.table or ""))
        right_table = aliases.get(_norm_name(right.table or ""), _norm_name(right.table or ""))
        if left_table == cte_name and right_table != cte_name:
            ancestor_column, base_column, base_table = left, right, right_table
        elif right_table == cte_name and left_table != cte_name:
            ancestor_column, base_column, base_table = right, left, left_table
        else:
            continue
        table_actual = next((name for name in data if _norm_name(name) == base_table), None)
        if not table_actual or not data.get(table_actual):
            continue
        lookup = _column_lookup(schema.get(table_actual, list(data[table_actual][0])))
        child_actual = lookup.get(_norm_name(base_column.name))
        ancestor_actual = lookup.get(_norm_name(ancestor_column.name))
        if ancestor_actual is None and aliases.get(
            _norm_name(ancestor_column.table or ""),
            _norm_name(ancestor_column.table or ""),
        ) == cte_name:
            # A recursive CTE can rename its anchor projection, for example
            # ``recommenders(recommender, member) AS (SELECT recommendedby,
            # memid ...)``.  The recursive join references ``recommender``
            # while the physical table owns ``recommendedby``.  Without this
            # mapping the safety pass leaves seed self-links in place and an
            # identity query can recurse forever.
            ancestor_actual = lookup.get(
                output_sources.get(_norm_name(ancestor_column.name), "")
            )
        if not child_actual or not ancestor_actual:
            continue
        rows = data[table_actual]
        # Keep literal anchors executable after the generic probes have made
        # their topology changes.  This is intentionally limited to direct
        # equality predicates; compound predicates remain for the normal
        # bounded execution path.
        actual_lookup = _column_lookup(schema.get(table_actual, list(rows[0])))
        anchor_values: dict[str, list[Any]] = defaultdict(list)
        for constrained_table, constrained_column, value in anchor_constraints:
            if _norm_name(constrained_table) != _norm_name(table_actual):
                continue
            actual_column = actual_lookup.get(_norm_name(constrained_column))
            if actual_column and value not in anchor_values[actual_column]:
                anchor_values[actual_column].append(value)
        for constrained_column, values in outer_constraints.items():
            actual_column = actual_lookup.get(_norm_name(constrained_column))
            if not actual_column:
                continue
            for value in values:
                if value not in anchor_values[actual_column]:
                    anchor_values[actual_column].append(value)
        for column, values in anchor_values.items():
            if values:
                rows[0][column] = values[0]
        id_actual = _primary_key_candidate(
            schema.get(table_actual, list(rows[0])),
            table_actual,
        )
        if id_actual and _norm_name(child_actual) == _norm_name(id_actual):
            parent_actual = next(
                (
                    column
                    for column in rows[0]
                    if any(
                        token in _norm_name(column)
                        for token in (
                            "parent", "manager", "boss", "supervisor",
                            "reports_to", "recommendedby", "recommender",
                        )
                    )
                    and _norm_name(column) != _norm_name(id_actual)
                ),
                None,
            )
            if parent_actual:
                preferred_ids = list(anchor_values.get(id_actual, ()))
                preferred_ids.extend(
                    value
                    for value in anchor_values.get(ancestor_actual, ())
                    if value not in preferred_ids
                )
                used_ids: set[Any] = set()
                id_values: list[Any] = []
                for value in preferred_ids:
                    if value is not None and value not in used_ids:
                        id_values.append(value)
                        used_ids.add(value)
                for index in range(len(rows)):
                    candidate = (
                        1000 + index
                        if _is_numeric_column(id_actual)
                        else f"__recursive_id_{index}__"
                    )
                    while candidate in used_ids:
                        candidate = (
                            candidate + 1
                            if isinstance(candidate, int)
                            else f"{candidate}_next"
                        )
                    id_values.append(candidate)
                    used_ids.add(candidate)
                    if len(id_values) >= len(rows):
                        break
                for index, row in enumerate(rows):
                    row[id_actual] = id_values[index]
                if anchor_values.get(id_actual):
                    # Ancestor traversal projects the relationship column and
                    # joins it back to the physical key, for example:
                    #
                    #   SELECT recommendedby FROM members WHERE memid = 27
                    #   UNION ALL
                    #   SELECT m.recommendedby
                    #   FROM cte r JOIN members m ON m.memid = r.recommendedby
                    #
                    # The literal row must point *forward* to its parent.  A
                    # root-first chain would make the anchor project NULL and
                    # leave the recursive query empty after its outer join.
                    for index, row in enumerate(rows):
                        row[parent_actual] = (
                            id_values[index + 1]
                            if index + 1 < len(rows)
                            else None
                        )
                else:
                    rows[0][parent_actual] = None
                    for index in range(1, len(rows)):
                        rows[index][parent_actual] = id_values[index - 1]
                return
        if _norm_name(child_actual) == _norm_name(ancestor_actual):
            # A recursive branch such as
            # ``members.recommendedby = recs.recommender`` while projecting
            # ``members.recommendedby`` repeats the same state forever under
            # UNION ALL as soon as one non-NULL match exists.  Keep the
            # physical anchor row, but make the recursive state terminal.
            # This prevents a generated self-loop from being mistaken for a
            # valid witness and keeps execution within the bounded guard.
            for row in rows:
                row[child_actual] = None
            return
        if id_actual and _norm_name(ancestor_actual) == _norm_name(id_actual):
            # Preserve a literal anchor and any outer CTE filters on the
            # recursive output.  The normal hierarchy shape is a bounded
            # parent chain: root -> child -> grandchild.
            preferred_ids = list(anchor_values.get(id_actual, ()))
            for value in anchor_values.get(ancestor_actual, ()):
                if value not in preferred_ids:
                    preferred_ids.append(value)
            used_ids: set[Any] = set()
            id_values: list[Any] = []
            for value in preferred_ids:
                if value is not None and value not in used_ids:
                    id_values.append(value)
                    used_ids.add(value)
            for index in range(len(rows)):
                candidate = (
                    1000 + index
                    if _is_numeric_column(id_actual)
                    else f"__recursive_id_{index}__"
                )
                while candidate in used_ids:
                    candidate = (
                        candidate + 1
                        if isinstance(candidate, int)
                        else f"{candidate}_next"
                    )
                id_values.append(candidate)
                used_ids.add(candidate)
                if len(id_values) >= len(rows):
                    break
            for index, row in enumerate(rows):
                row[ancestor_actual] = id_values[index]
                anchor_parent = anchor_values.get(child_actual, ())
                row[child_actual] = (
                    anchor_parent[0]
                    if index == 0 and anchor_parent
                    else None
                    if index == 0
                    else id_values[index - 1]
                )
            return
        for index, row in enumerate(rows):
            if _is_numeric_column(ancestor_actual):
                row[ancestor_actual] = 1000 + index
            else:
                row[ancestor_actual] = f"__recursive_node_{index}__"
        for index in range(1, len(rows)):
            rows[index][child_actual] = rows[index - 1][ancestor_actual]
        anchor_branch = set_node.this if isinstance(set_node, (exp.Union, exp.Intersect, exp.Except)) else None
        has_null_root = any(
            isinstance(check.expression, exp.Null)
            and isinstance(check.this, exp.Column)
            and _norm_name(check.this.name) == _norm_name(base_column.name)
            for check in anchor_branch.find_all(exp.Is)
        ) if isinstance(anchor_branch, exp.Expression) else False
        if has_null_root:
            rows[0][child_actual] = None
        return

    for table_actual, rows in data.items():
        if not rows:
            continue
        columns = schema.get(table_actual, list(rows[0]))
        lookup = _column_lookup(columns)
        parent_col = next(
            (lookup[name] for name in lookup if any(token in name for token in ("parent", "manager", "boss", "supervisor", "reports_to"))),
            None,
        )
        id_col = _primary_key_candidate(columns, table_actual)
        if not parent_col or not id_col:
            continue
        rows[0][parent_col] = None
        for index in range(1, len(rows)):
            rows[index][parent_col] = rows[index - 1][id_col]
        return


def _recursive_cte_output_sources(
    cte: exp.CTE,
    set_node: exp.Expression,
) -> dict[str, str]:
    """Map explicit recursive CTE output names to simple anchor columns.

    Only direct column projections are mapped.  Computed anchor values have no
    safe physical-column counterpart, so the hierarchy materializer leaves
    those cases to the regular bounded execution guard.
    """

    alias = cte.args.get("alias")
    output_columns = list(getattr(alias, "args", {}).get("columns") or ())
    anchor = set_node.this if isinstance(set_node, (exp.Union, exp.Intersect, exp.Except)) else None
    anchor_select = anchor if isinstance(anchor, exp.Select) else (
        anchor.find(exp.Select) if isinstance(anchor, exp.Expression) else None
    )
    if not isinstance(anchor_select, exp.Select):
        return {}
    if not output_columns:
        output_columns = [
            expression.this if isinstance(expression, exp.Alias) else expression
            for expression in (anchor_select.expressions or ())
        ]
    sources: dict[str, str] = {}
    for output, projection in zip(output_columns, anchor_select.expressions or ()):
        expression = projection.this if isinstance(projection, exp.Alias) else projection
        output_name = (
            str(output.this)
            if isinstance(output, exp.Identifier)
            else output.name
            if isinstance(output, exp.Column)
            else str(output or "")
        )
        if isinstance(expression, exp.Column) and output_name:
            sources[_norm_name(output_name)] = _norm_name(expression.name)
    return sources


def _recursive_select_literal_constraints(
    select: exp.Select | None,
) -> list[tuple[str, str, Any]]:
    """Collect direct ``column = literal`` predicates from one SELECT block."""
    if not isinstance(select, exp.Select):
        return []
    aliases = _table_aliases(select)
    constraints: list[tuple[str, str, Any]] = []
    for where in select.find_all(exp.Where):
        if where.find_ancestor(exp.Select) is not select:
            continue
        for comparison in where.find_all(exp.EQ):
            column = comparison.left if isinstance(comparison.left, exp.Column) else (
                comparison.right if isinstance(comparison.right, exp.Column) else None
            )
            literal = comparison.right if column is comparison.left else comparison.left
            if not isinstance(column, exp.Column) or not isinstance(literal, exp.Literal):
                continue
            value = _literal_value(literal)
            table_name = aliases.get(
                _norm_name(column.table or ""),
                _norm_name(column.table or ""),
            )
            if not table_name:
                direct_tables = {
                    _norm_name(table.name)
                    for table in select.find_all(exp.Table)
                    if table.name
                }
                if len(direct_tables) == 1:
                    table_name = next(iter(direct_tables))
            if table_name:
                constraints.append((table_name, _norm_name(column.name), value))
    return constraints


def _recursive_outer_literal_constraints(
    query_ast: exp.Expression | None,
    cte_name: str,
    output_sources: dict[str, str],
) -> dict[str, list[Any]]:
    """Map simple outer CTE literal filters back to physical columns."""
    if not isinstance(query_ast, exp.Expression) or not output_sources:
        return {}
    aliases = _table_aliases(query_ast)
    result: dict[str, list[Any]] = defaultdict(list)
    for where in query_ast.find_all(exp.Where):
        if where.find_ancestor(exp.CTE) is not None:
            continue
        for comparison in where.find_all(exp.EQ):
            column = comparison.left if isinstance(comparison.left, exp.Column) else (
                comparison.right if isinstance(comparison.right, exp.Column) else None
            )
            literal = comparison.right if column is comparison.left else comparison.left
            if not isinstance(column, exp.Column) or not isinstance(literal, exp.Literal):
                continue
            resolved_table = aliases.get(
                _norm_name(column.table or ""),
                _norm_name(column.table or ""),
            )
            if resolved_table and resolved_table != cte_name:
                continue
            source_column = output_sources.get(_norm_name(column.name))
            if not source_column:
                continue
            value = _literal_value(literal)
            if value not in result[source_column]:
                result[source_column].append(value)
    return dict(result)


def _apply_recursive_cte_safety(
    data: dict[str, list[dict[str, Any]]],
    schema: dict[str, list[str]],
    *sqls: str,
) -> None:
    for sql in sqls:
        ast = _parse_sql(sql)
        if not ast or not _is_recursive_ast(ast):
            continue
        for cte in ast.find_all(exp.CTE):
            _apply_recursive_cte_hierarchy(data, schema, cte, query_ast=ast)


def _apply_recursive_set_duplicate_probe(
    data: dict[str, list[dict[str, Any]]],
    standard_sql: str,
    student_sql: str,
    ast_diffs: list[ASTDiffNode],
) -> None:
    """Create one duplicate recursive state so UNION and UNION ALL diverge."""
    asts = [_parse_sql(standard_sql), _parse_sql(student_sql)]
    modifier_changed = any(
        diff.diff_type == "set_modifier_changed"
        or (
            diff.diff_type == "set_operator_changed"
            and diff.extra.get("standard_modifier") != diff.extra.get("student_modifier")
        )
        for diff in ast_diffs
    ) or (
        len(asts) == 2
        and all(ast is not None and _is_recursive_ast(ast) for ast in asts)
        and _set_operator_modifier(_set_operator_node(asts[0]))
        != _set_operator_modifier(_set_operator_node(asts[1]))
    )
    if not modifier_changed:
        return
    if not any(_is_recursive_ast(ast) for ast in asts):
        return
    if _apply_recursive_graph_diamond_probe(data, asts):
        return
    for table_name, rows in data.items():
        if len(rows) < 3:
            continue
        columns = list(rows[0])
        id_col = _primary_key_candidate(columns, table_name)
        parent_col = next(
            (
                column for column in columns
                if any(token in _norm_name(column) for token in ("parent", "manager", "boss", "supervisor", "reports_to"))
            ),
            None,
        )
        if not id_col or not parent_col:
            continue
        rows[2].update(rows[1])
        return


def _apply_recursive_graph_diamond_probe(
    data: dict[str, list[dict[str, Any]]],
    asts: list[exp.Expression | None],
) -> bool:
    """Materialize one finite diamond for a single-column recursive graph."""

    for ast in asts:
        if ast is None or not _is_recursive_ast(ast):
            continue
        for cte in ast.find_all(exp.CTE):
            body = cte.this
            union = body if isinstance(body, exp.Union) else body.find(exp.Union)
            if not isinstance(union, exp.Union):
                continue
            cte_name = _norm_name(cte.alias or "")
            branches = (union.this, union.expression)
            branch_selects = [
                branch
                if isinstance(branch, exp.Select)
                else branch.find(exp.Select)
                if isinstance(branch, exp.Expression)
                else None
                for branch in branches
            ]
            if any(not isinstance(branch, exp.Select) for branch in branch_selects):
                continue
            recursive_index = next(
                (
                    index
                    for index, branch in enumerate(branch_selects)
                    if any(
                        _norm_name(table.name) == cte_name
                        for table in branch.find_all(exp.Table)
                    )
                ),
                None,
            )
            if recursive_index is None:
                continue
            recursive_select = branch_selects[recursive_index]
            anchor_select = branch_selects[1 - recursive_index]
            if (
                isinstance(recursive_select, exp.Select)
                and isinstance(anchor_select, exp.Select)
                and _apply_recursive_row_graph_diamond(
                    data,
                    cte,
                    cte_name,
                    recursive_select,
                    anchor_select,
                )
            ):
                return True
            if (
                not isinstance(recursive_select, exp.Select)
                or not isinstance(anchor_select, exp.Select)
                or len(recursive_select.expressions or ()) != 1
                or len(anchor_select.expressions or ()) != 1
            ):
                continue
            anchor_expression = anchor_select.expressions[0]
            anchor_expression = (
                anchor_expression.this
                if isinstance(anchor_expression, exp.Alias)
                else anchor_expression
            )
            root = _literal_value(anchor_expression)
            projection = recursive_select.expressions[0]
            projection = projection.this if isinstance(projection, exp.Alias) else projection
            if root is None or not isinstance(projection, exp.Column):
                continue

            aliases = _table_aliases(recursive_select)
            physical_table = aliases.get(
                _norm_name(projection.table or ""),
                _norm_name(projection.table or ""),
            )
            if not physical_table or physical_table == cte_name:
                continue
            equality = next(
                (
                    node
                    for node in recursive_select.find_all(exp.EQ)
                    if len(list(node.find_all(exp.Column))) == 2
                ),
                None,
            )
            if equality is None:
                continue
            columns = list(equality.find_all(exp.Column))
            physical_join = next(
                (
                    column
                    for column in columns
                    if aliases.get(
                        _norm_name(column.table or ""),
                        _norm_name(column.table or ""),
                    ) == physical_table
                ),
                None,
            )
            recursive_join = next(
                (
                    column
                    for column in columns
                    if aliases.get(
                        _norm_name(column.table or ""),
                        _norm_name(column.table or ""),
                    ) == cte_name
                ),
                None,
            )
            if not isinstance(physical_join, exp.Column) or not isinstance(recursive_join, exp.Column):
                continue
            table_name = next(
                (name for name in data if _norm_name(name) == physical_table),
                None,
            )
            rows = data.get(table_name or "")
            if not rows or len(rows) < 4:
                continue
            lookup = _column_lookup(list(rows[0]))
            source_column = lookup.get(_norm_name(physical_join.name))
            target_column = lookup.get(_norm_name(projection.name))
            if not source_column or not target_column or source_column == target_column:
                continue
            if isinstance(root, (int, float, Decimal)) and not isinstance(root, bool):
                first, second, converged = root + 1, root + 2, root + 3
            else:
                first, second, converged = (
                    f"{root}__left",
                    f"{root}__right",
                    f"{root}__merge",
                )
            for row, source_value, target_value in zip(
                rows[:4],
                (root, root, first, second),
                (first, second, converged, converged),
            ):
                row[source_column] = source_value
                row[target_column] = target_value
            return True
    return False


def _apply_recursive_row_graph_diamond(
    data: dict[str, list[dict[str, Any]]],
    cte: exp.CTE,
    cte_name: str,
    recursive_select: exp.Select,
    anchor_select: exp.Select,
) -> bool:
    """Create two predecessor rows converging on one multi-column CTE row."""
    aliases = _table_aliases(recursive_select)
    for equality in recursive_select.find_all(exp.EQ):
        if not isinstance(equality.left, exp.Column) or not isinstance(equality.right, exp.Column):
            continue
        columns = (equality.left, equality.right)
        physical_join = next(
            (
                column
                for column in columns
                if aliases.get(
                    _norm_name(column.table or ""),
                    _norm_name(column.table or ""),
                ) != cte_name
            ),
            None,
        )
        recursive_join = next(
            (
                column
                for column in columns
                if aliases.get(
                    _norm_name(column.table or ""),
                    _norm_name(column.table or ""),
                ) == cte_name
            ),
            None,
        )
        if not isinstance(physical_join, exp.Column) or not isinstance(recursive_join, exp.Column):
            continue
        # ``edge.id = state.link`` can converge because many state rows may
        # share one link. ``child.parent_id = state.id`` is a single-parent
        # hierarchy and cannot form a diamond without violating an id key.
        if not _is_key_column(physical_join.name) or _is_key_column(recursive_join.name):
            continue
        physical_table = aliases.get(
            _norm_name(physical_join.table or ""),
            _norm_name(physical_join.table or ""),
        )
        table_name = next(
            (name for name in data if _norm_name(name) == physical_table),
            None,
        )
        rows = data.get(table_name or "")
        if not rows or len(rows) < 4:
            continue
        lookup = _column_lookup(list(rows[0]))
        id_column = lookup.get(_norm_name(physical_join.name))
        state_column = lookup.get(_norm_name(recursive_join.name))
        if not id_column or not state_column or id_column == state_column:
            continue

        # Every physical row is an anchor in common graph examples. Rows 1
        # and 2 therefore both transition to row 3 at depth 1.
        for row, node_id, next_id in zip(
            rows[:4],
            (1, 2, 3, 4),
            (2, 4, 4, 9004),
        ):
            row[id_column] = node_id
            row[state_column] = next_id
        for index, row in enumerate(rows[4:], start=5):
            row[id_column] = 1000 + index
            row[state_column] = 9000 + index
        for row_index in range(4):
            _set_select_local_literal_predicates(data, anchor_select, row_index)
            _set_select_local_literal_predicates(data, recursive_select, row_index)
        return True
    return False


def _apply_recursive_cte_orphan_probe(
    data: dict[str, list[dict[str, Any]]],
    standard_sql: str,
    student_sql: str,
) -> None:
    """Keep one base-table row unreachable from a recursive hierarchy root."""
    standard_ast = _parse_sql(standard_sql)
    student_ast = _parse_sql(student_sql)
    if not standard_ast or not _is_recursive_ast(standard_ast) or _is_recursive_ast(student_ast):
        return
    recursive_tables = {
        _norm_name(table.name)
        for cte in standard_ast.find_all(exp.CTE)
        for table in cte.this.find_all(exp.Table)
        if _norm_name(table.name) != _norm_name(cte.alias or "")
    }
    for table_name, rows in data.items():
        if _norm_name(table_name) not in recursive_tables or len(rows) < 2:
            continue
        columns = list(rows[0])
        parent_column = next(
            (column for column in columns if any(token in _norm_name(column) for token in ("parent", "manager", "boss", "reports_to"))),
            None,
        )
        if not parent_column:
            continue
        rows[-1][parent_column] = 999999 if _is_numeric_column(parent_column) else "__unreachable_parent__"
        return


def _apply_cte_outer_projection_probe(
    data: dict[str, list[dict[str, Any]]],
    standard_sql: str,
    ast_diffs: list[ASTDiffNode],
) -> None:
    """Prevent repeated output labels from masking outer CTE predicate changes."""
    if not any(diff.clause_category in {"WHERE", "PREDICATE"} for diff in ast_diffs):
        return
    ast = _parse_sql(standard_sql)
    if not ast or not ast.find(exp.CTE):
        return
    outer_select = ast if isinstance(ast, exp.Select) else ast.find(exp.Select)
    if not isinstance(outer_select, exp.Select):
        return
    semantic_columns = {
        _norm_name(column.name)
        for where in outer_select.find_all(exp.Where)
        for column in where.find_all(exp.Column)
    }
    for semantic_node in ast.find_all(
        exp.Window,
        exp.Group,
        exp.Having,
        exp.Join,
        exp.Order,
    ):
        semantic_columns.update(
            _norm_name(column.name)
            for column in semantic_node.find_all(exp.Column)
        )
    for item in outer_select.expressions or []:
        node = item.this if isinstance(item, exp.Alias) else item
        if not isinstance(node, exp.Column) or _norm_name(node.name) in semantic_columns:
            continue
        table_ref = _norm_name(node.table or "")
        aliases = _table_aliases(ast)
        resolved_table = aliases.get(table_ref, table_ref)
        for table_name, rows in data.items():
            if resolved_table and _norm_name(table_name) != resolved_table:
                continue
            if not rows:
                continue
            column = next(
                (col for col in rows[0] if _norm_name(col) == _norm_name(node.name)),
                None,
            )
            if not column:
                continue
            for idx, row in enumerate(rows):
                value = row[column]
                if isinstance(value, str):
                    row[column] = f"{value}__cte_row_{idx:03d}"
            return


def _apply_logical_probe_adapter(data, schema, standard_sql, student_sql, ast_diffs):
    _apply_logical_operator_probe(data, standard_sql, student_sql)


def _contains_boolean_predicate(standard_sql: str, student_sql: str) -> bool:
    text = f"{standard_sql} {student_sql}".upper()
    return " AND " in text or " OR " in text


def _apply_comparison_probe_adapter(data, schema, standard_sql, student_sql, ast_diffs):
    _apply_expression_comparison_boundary_probes(data, standard_sql, ast_diffs)
    _apply_scalar_subquery_boundary_probes(data, standard_sql, student_sql, ast_diffs)


def _comparison_adapter_constraints(obligations):
    return tuple(
        constraint
        for obligation in obligations
        for constraint in declare_strategy(obligation).cell_constraints
        if constraint.owner == "comparison_boundary_tristate"
    )


def _apply_null_probe_adapter(data, schema, standard_sql, student_sql, ast_diffs):
    # A SQL-shape fallback must not invent a NULL witness when the structural
    # layer already proved the complete query pair equivalent.  In particular,
    # NOT NULL membership keys make NOT IN/NOT EXISTS equivalent, and writing a
    # NULL here would violate the schema and manufacture a false counterexample.
    if not ast_diffs:
        return
    _apply_not_in_null_probe(data, standard_sql, student_sql, ast_diffs)


def _apply_join_key_drift_adapter(data, schema, standard_sql, student_sql, ast_diffs):
    _apply_join_on_counterexample(data, standard_sql, student_sql, ast_diffs)
    _materialize_declared_join_witness(data, ast_diffs, standard_sql=standard_sql)


def _join_key_drift_column_set(schema, standard_sql, student_sql, ast_diffs):
    table_lookup = {_norm_name(name): name for name in schema}
    result: set[ColumnRef] = set()
    for sql in (standard_sql, student_sql):
        for pair in _join_on_column_pairs(sql):
            for table, column in pair:
                table_name = table_lookup.get(_norm_name(table))
                if table_name is None:
                    continue
                column_lookup = _column_lookup(schema.get(table_name, ()))
                column_name = column_lookup.get(_norm_name(column))
                if column_name is not None:
                    result.add(ColumnRef(table_name, column_name, "root"))
    return result


def _apply_join_matched_dangling_adapter(
    data, schema, standard_sql, student_sql, ast_diffs
):
    _apply_final_dangling_tuple_probes(
        data,
        standard_sql,
        student_sql,
        ast_diffs,
    )
    _materialize_declared_join_witness(data, ast_diffs, standard_sql=standard_sql)


def _join_matched_dangling_column_set(
    schema, standard_sql, student_sql, ast_diffs
):
    result = set(
        _join_key_drift_column_set(
            schema, standard_sql, student_sql, ast_diffs
        )
    )
    table_lookup = {_norm_name(name): name for name in schema}
    for sql in (standard_sql, student_sql):
        ast = _parse_sql(sql)
        aliases = _table_aliases(ast) if ast is not None else {}
        for table, column in _group_by_columns_for_sql(sql):
            resolved_table = aliases.get(_norm_name(table), _norm_name(table))
            candidate_tables = []
            if resolved_table in table_lookup:
                candidate_tables.append(table_lookup[resolved_table])
            elif not resolved_table:
                candidate_tables.extend(
                    name
                    for name, columns in schema.items()
                    if _norm_name(column) in _column_lookup(columns)
                )
            if len(candidate_tables) != 1:
                continue
            table_name = candidate_tables[0]
            column_name = _column_lookup(schema[table_name]).get(
                _norm_name(column)
            )
            if column_name is not None:
                result.add(ColumnRef(table_name, column_name, "root"))
    return result


def _apply_group_grain_adapter(data, schema, standard_sql, student_sql, ast_diffs):
    _apply_group_by_probes(data, standard_sql, student_sql, ast_diffs)


def _group_grain_column_set(schema, standard_sql, student_sql, ast_diffs):
    result: set[ColumnRef] = set()
    table_lookup = {_norm_name(name): name for name in schema}
    for sql in (standard_sql, student_sql):
        ast = _parse_sql(sql)
        if ast is None:
            continue
        aliases = _table_aliases(ast)
        for _, item in _group_by_items(ast):
            column = item if isinstance(item, exp.Column) else item.find(exp.Column)
            if not isinstance(column, exp.Column):
                continue
            table_ref = aliases.get(
                _norm_name(column.table or ""),
                _norm_name(column.table or ""),
            )
            candidate_tables = []
            if table_ref in table_lookup:
                candidate_tables.append(table_lookup[table_ref])
            elif not table_ref:
                candidate_tables.extend(
                    name
                    for name, columns in schema.items()
                    if _norm_name(column.name) in _column_lookup(columns)
                )
            for table_name in candidate_tables:
                column_name = _column_lookup(schema[table_name]).get(
                    _norm_name(column.name)
                )
                if column_name is not None:
                    result.add(ColumnRef(table_name, column_name, "root"))
    return result


def _apply_correlated_overlap_adapter(
    data, schema, standard_sql, student_sql, ast_diffs
):
    _apply_correlated_subquery_probe(data, schema, standard_sql, student_sql)


def _correlated_overlap_column_set(
    schema, standard_sql, student_sql, ast_diffs
):
    result: set[ColumnRef] = set()
    table_lookup = {_norm_name(name): name for name in schema}
    for pair in _correlated_subquery_column_pairs(standard_sql, student_sql):
        for table, column in pair:
            table_name = table_lookup.get(_norm_name(table))
            if table_name is None:
                continue
            column_name = _column_lookup(schema[table_name]).get(_norm_name(column))
            if column_name is not None:
                result.add(ColumnRef(table_name, column_name, "root"))
    return result


def _apply_set_overlap_adapter(data, schema, standard_sql, student_sql, ast_diffs):
    _apply_set_operator_probes(data, standard_sql, student_sql, ast_diffs)


def _set_overlap_column_set(schema, standard_sql, student_sql, ast_diffs):
    """Declare the physical cells the legacy set probe may rewrite."""
    result: set[ColumnRef] = set()
    table_lookup = {_norm_name(name): name for name in schema}

    for sql in (standard_sql, student_sql):
        node = _set_operator_node(_parse_sql(sql))
        if not isinstance(node, (exp.Union, exp.Intersect, exp.Except)):
            continue
        for branch in (node.this, node.expression):
            select = branch if isinstance(branch, exp.Select) else branch.find(exp.Select)
            if not isinstance(select, exp.Select):
                continue
            table_node = next(
                (
                    table
                    for table in select.find_all(exp.Table)
                    if _norm_name(table.name) in table_lookup
                ),
                None,
            )
            if not isinstance(table_node, exp.Table):
                continue
            table_name = table_lookup[_norm_name(table_node.name)]
            column_lookup = _column_lookup(schema.get(table_name, ()))

            for item in select.expressions or ():
                expression = item.this if isinstance(item, exp.Alias) else item
                column = (
                    expression
                    if isinstance(expression, exp.Column)
                    else expression.find(exp.Column)
                )
                if not isinstance(column, exp.Column):
                    continue
                column_name = column_lookup.get(_norm_name(column.name))
                if column_name is not None:
                    result.add(ColumnRef(table_name, column_name, "root"))

            for constraint in _extract_literal_constraints(_sql_of(select)):
                column_name = column_lookup.get(
                    _norm_name(str(constraint.get("column") or ""))
                )
                if column_name is not None:
                    result.add(ColumnRef(table_name, column_name, "root"))
    return result


def _apply_distinct_adapter(data, schema, standard_sql, student_sql, ast_diffs):
    _apply_distinct_probes(data, standard_sql, student_sql, ast_diffs)


def _distinct_column_set(schema, standard_sql, student_sql, ast_diffs):
    """Declare columns touched by SQLite DISTINCT witness probes."""
    result: set[ColumnRef] = set()
    table_lookup = {_norm_name(name): name for name in schema}

    def add_column(column: exp.Column, aliases: dict[str, str]) -> None:
        table_ref = aliases.get(
            _norm_name(column.table or ""),
            _norm_name(column.table or ""),
        )
        candidate_tables = []
        if table_ref in table_lookup:
            candidate_tables.append(table_lookup[table_ref])
        elif not table_ref:
            # Unqualified columns can belong to any physical source table;
            # retaining all matching candidates keeps the legacy probe inside
            # its declared write boundary for ambiguous teaching schemas.
            candidate_tables.extend(
                name
                for name, columns in schema.items()
                if _norm_name(column.name) in _column_lookup(columns)
            )
        for table_name in candidate_tables:
            column_name = _column_lookup(schema[table_name]).get(
                _norm_name(column.name)
            )
            if column_name is not None:
                result.add(ColumnRef(table_name, column_name, "root"))

    for sql in (standard_sql, student_sql):
        ast = _parse_sql(sql)
        if ast is None:
            continue
        aliases = _table_aliases(ast)
        for select in ast.find_all(exp.Select):
            for column in select.find_all(exp.Column):
                if _nearest_select(column) is select:
                    add_column(column, aliases)
    return result


def _apply_order_key_adapter(data, schema, standard_sql, student_sql, ast_diffs):
    _apply_order_by_probes(data, standard_sql, student_sql, ast_diffs)


def _order_key_column_set(schema, standard_sql, student_sql, ast_diffs):
    result: set[ColumnRef] = set()
    table_lookup = {_norm_name(name): name for name in schema}

    def add_column(column: exp.Column, aliases: dict[str, str]) -> None:
        table_ref = aliases.get(
            _norm_name(column.table or ""),
            _norm_name(column.table or ""),
        )
        candidate_tables = []
        if table_ref in table_lookup:
            candidate_tables.append(table_lookup[table_ref])
        elif not table_ref:
            candidate_tables.extend(
                name
                for name, columns in schema.items()
                if _norm_name(column.name) in _column_lookup(columns)
            )
        for table_name in candidate_tables:
            column_name = _column_lookup(schema[table_name]).get(
                _norm_name(column.name)
            )
            if column_name is not None:
                result.add(ColumnRef(table_name, column_name, "root"))

    for sql in (standard_sql, student_sql):
        ast = _parse_sql(sql)
        if ast is None:
            continue
        aliases = _table_aliases(ast)
        for order in ast.find_all(exp.Order):
            for column in order.find_all(exp.Column):
                add_column(column, aliases)
        select = _top_select(ast)
        if isinstance(select, exp.Select):
            for item in select.expressions or ():
                expression = item.this if isinstance(item, exp.Alias) else item
                if isinstance(expression, exp.Column):
                    add_column(expression, aliases)
    return result


def _apply_window_partition_adapter(
    data, schema, standard_sql, student_sql, ast_diffs
):
    _apply_window_probes(data, standard_sql, student_sql, ast_diffs)


def _window_partition_column_set(
    schema, standard_sql, student_sql, ast_diffs
):
    result: set[ColumnRef] = set()
    table_lookup = {_norm_name(name): name for name in schema}
    for sql in (standard_sql, student_sql):
        ast = _parse_sql(sql)
        if ast is None:
            continue
        for window in ast.find_all(exp.Window):
            source, _ = _window_source_selects(ast, window)
            if not isinstance(source, exp.Table):
                continue
            table_name = table_lookup.get(_norm_name(source.name))
            if table_name is None:
                continue
            column_lookup = _column_lookup(schema[table_name])
            for column in _window_partition_columns(window):
                column_name = column_lookup.get(_norm_name(column.name))
                if column_name is not None:
                    result.add(ColumnRef(table_name, column_name, "root"))
    return result


def _apply_window_alias_predicate_adapter(
    data, schema, standard_sql, student_sql, ast_diffs
):
    _apply_window_alias_predicate_probes(
        data,
        standard_sql,
        student_sql,
        ast_diffs,
    )


def _window_alias_predicate_column_set(
    schema, standard_sql, student_sql, ast_diffs
):
    """Declare physical source columns touched by window-alias topology."""
    result: set[ColumnRef] = set()
    table_lookup = {_norm_name(name): name for name in schema}
    ast = _parse_sql(standard_sql)
    if ast is None:
        return result

    def add_column(table_name: str, column_name: str) -> None:
        actual_table = table_lookup.get(_norm_name(table_name))
        if actual_table is None:
            return
        actual_column = _column_lookup(schema[actual_table]).get(
            _norm_name(column_name)
        )
        if actual_column is not None:
            result.add(ColumnRef(actual_table, actual_column, "root"))

    for window in _window_alias_map(ast).values():
        source, source_chain = _window_source_selects(ast, window)
        if not isinstance(source, exp.Table):
            continue
        for column in window.find_all(exp.Column):
            add_column(source.name, column.name)
        for select in source_chain:
            where = select.args.get("where")
            if not isinstance(where, exp.Where):
                continue
            for comparison in where.find_all(
                exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE
            ):
                if comparison.find_ancestor(exp.Select) is not select:
                    continue
                if not isinstance(comparison.left, exp.Column) or not isinstance(
                    comparison.right, exp.Literal
                ):
                    continue
                ref = _column_ref_in_select(comparison.left, select)
                if ref is not None:
                    add_column(ref[0], ref[1])
    return result


def _contains_window_alias(standard_sql: str, student_sql: str) -> bool:
    standard_ast = _parse_sql(standard_sql)
    student_ast = _parse_sql(student_sql)
    return bool(
        standard_ast is not None
        and student_ast is not None
        and _window_alias_map(standard_ast)
    )


def _apply_cte_base_adapter(data, schema, standard_sql, student_sql, ast_diffs):
    _apply_cte_probes(data, schema, standard_sql, student_sql, ast_diffs)


def _cte_base_column_set(schema, standard_sql, student_sql, ast_diffs):
    """Declare physical columns the legacy CTE base probe can rewrite."""
    result: set[ColumnRef] = set()
    table_lookup = {_norm_name(name): name for name in schema}

    def add_column(table_name: str, column_name: str) -> None:
        actual_table = table_lookup.get(_norm_name(table_name))
        if actual_table is None:
            return
        actual_column = _column_lookup(schema[actual_table]).get(
            _norm_name(column_name)
        )
        if actual_column is not None:
            result.add(ColumnRef(actual_table, actual_column, "root"))

    for sql in (standard_sql, student_sql):
        ast = _parse_sql(sql)
        if ast is None:
            continue
        for cte in ast.find_all(exp.CTE):
            aliases = _table_aliases(cte)
            source_tables = {
                _norm_name(table.name)
                for table in cte.find_all(exp.Table)
                if _norm_name(table.name) in table_lookup
            }
            if not source_tables:
                continue

            for where in cte.find_all(exp.Where):
                for comparison in where.find_all(
                    exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE
                ):
                    column = (
                        comparison.left
                        if isinstance(comparison.left, exp.Column)
                        else comparison.right
                        if isinstance(comparison.right, exp.Column)
                        else None
                    )
                    literal = (
                        comparison.right
                        if column is comparison.left
                        else comparison.left
                    )
                    if not isinstance(column, exp.Column) or not isinstance(
                        literal, exp.Literal
                    ):
                        continue
                    qualifier = aliases.get(
                        _norm_name(column.table or ""),
                        _norm_name(column.table or ""),
                    )
                    candidates = (
                        {qualifier}
                        if qualifier in source_tables
                        else source_tables
                        if not qualifier
                        else set()
                    )
                    for table_name in candidates:
                        add_column(table_name, column.name)

            cte_select = (
                cte.this.find(exp.Select)
                if isinstance(cte.this, exp.Expression)
                else None
            )
            projection = (
                cte_select.expressions[0]
                if isinstance(cte_select, exp.Select) and cte_select.expressions
                else None
            )
            projection = projection.this if isinstance(projection, exp.Alias) else projection
            if not isinstance(projection, exp.Column):
                continue

            # The compatibility probe aligns a projected relationship key
            # across every physical table exposing that key.
            for table_name in schema:
                add_column(table_name, projection.name)
            cte_where = cte.find(exp.Where)
            predicate = cte_where.find(exp.EQ, exp.NEQ) if cte_where else None
            if predicate is None:
                continue
            predicate_column = (
                predicate.left
                if isinstance(predicate.left, exp.Column)
                else predicate.right
                if isinstance(predicate.right, exp.Column)
                else None
            )
            if not isinstance(predicate_column, exp.Column):
                continue
            for table_name in source_tables:
                add_column(table_name, predicate_column.name)
    return result


def _contains_cte(standard_sql: str, student_sql: str) -> bool:
    return any(
        (ast := _parse_sql(sql)) is not None and ast.find(exp.CTE) is not None
        for sql in (standard_sql, student_sql)
    )


def _apply_bounded_order_ties_adapter(
    data, schema, standard_sql, student_sql, ast_diffs
):
    _apply_order_by_probes(data, standard_sql, student_sql, ast_diffs)


LEGACY_PROBE_REGISTRY = LegacyProbeRegistry()
LEGACY_PROBE_REGISTRY.register(
    LegacyProbeAdapter(
        name="logical_truth_table",
        phase=4,
        apply=_apply_logical_probe_adapter,
        diff_types=frozenset({"logical_operator_changed", "logical_precedence_tree_changed"}),
        clauses=frozenset({"LOGICAL", "WHERE"}),
        knowledge_points=frozenset({"where"}),
        sql_trigger=_contains_boolean_predicate,
    )
)
LEGACY_PROBE_REGISTRY.register(
    LegacyProbeAdapter(
        name="comparison_boundary",
        phase=4,
        apply=_apply_comparison_probe_adapter,
        diff_types=frozenset({"comparison_operator_changed", "literal_changed", "predicate_expression_operator_changed"}),
        clauses=frozenset({"WHERE", "HAVING", "SUBQUERY", "PREDICATE"}),
        knowledge_points=frozenset({"where", "where-comp", "subquery-scalar"}),
        constraint_factory=_comparison_adapter_constraints,
    )
)
LEGACY_PROBE_REGISTRY.register(
    LegacyProbeAdapter(
        name="null_tristate",
        phase=4,
        apply=_apply_null_probe_adapter,
        diff_types=frozenset({
            "null_equality_changed",
            "null_sensitive_antijoin_equivalence",
            "in_predicate_negation_changed",
        }),
        clauses=frozenset({"WHERE", "SUBQUERY", "IN", "NULL"}),
        knowledge_points=frozenset({"comp-null", "null-handling", "in-list"}),
        sql_trigger=lambda standard, student: "NOT IN" in f"{standard} {student}".upper(),
    )
)
LEGACY_PROBE_REGISTRY.register(
    LegacyProbeAdapter(
        name="join_key_drift",
        phase=5,
        apply=_apply_join_key_drift_adapter,
        diff_types=frozenset({"join_on_changed"}),
        clauses=frozenset({"JOIN ON"}),
        knowledge_points=frozenset({"join-on"}),
        read_set_factory=_join_key_drift_column_set,
        write_set_factory=_join_key_drift_column_set,
        metadata={"stage": "final"},
    )
)
LEGACY_PROBE_REGISTRY.register(
    LegacyProbeAdapter(
        name="group_grain_split",
        phase=6,
        apply=_apply_group_grain_adapter,
        diff_types=frozenset({
            "group_by_changed",
            "group_by_expression_changed",
            "grouping_grain_too_fine",
            "grouping_grain_too_coarse",
        }),
        clauses=frozenset({"GROUP BY"}),
        knowledge_points=frozenset({"group-by"}),
        read_set_factory=_group_grain_column_set,
        write_set_factory=_group_grain_column_set,
    )
)
LEGACY_PROBE_REGISTRY.register(
    LegacyProbeAdapter(
        name="correlated_subquery_overlap",
        phase=7,
        apply=_apply_correlated_overlap_adapter,
        diff_types=frozenset({
            "subquery_added",
            "subquery_removed",
            "correlated_predicate_changed",
            "in_predicate_negation_changed",
            "null_sensitive_antijoin_equivalence",
            "in_exists_equivalence",
        }),
        read_set_factory=_correlated_overlap_column_set,
        write_set_factory=_correlated_overlap_column_set,
        activation_guard=lambda standard, student: bool(
            _correlated_subquery_column_pairs(standard, student)
        ),
        metadata={"stage": "post_main"},
    )
)
LEGACY_PROBE_REGISTRY.register(
    LegacyProbeAdapter(
        name="set_overlap",
        phase=8,
        apply=_apply_set_overlap_adapter,
        diff_types=frozenset({"set_operator_changed", "set_modifier_changed"}),
        clauses=frozenset({"UNION", "INTERSECT", "EXCEPT"}),
        knowledge_points=frozenset({"union", "intersect", "except"}),
        read_set_factory=_set_overlap_column_set,
        write_set_factory=_set_overlap_column_set,
    )
)
LEGACY_PROBE_REGISTRY.register(
    LegacyProbeAdapter(
        name="window_partition_layout",
        phase=9,
        apply=_apply_window_partition_adapter,
        diff_types=frozenset({"window_over_changed", "window_function_changed"}),
        clauses=frozenset({"WINDOW"}),
        knowledge_points=frozenset({"window-row-number"}),
        read_set_factory=_window_partition_column_set,
        write_set_factory=_window_partition_column_set,
    )
)
LEGACY_PROBE_REGISTRY.register(
    LegacyProbeAdapter(
        name="window_alias_predicate_layout",
        phase=9,
        apply=_apply_window_alias_predicate_adapter,
        diff_types=frozenset({
            "comparison_operator_changed",
            "distinct_changed",
            "window_over_changed",
            "window_function_changed",
        }),
        read_set_factory=_window_alias_predicate_column_set,
        write_set_factory=_window_alias_predicate_column_set,
        activation_guard=_contains_window_alias,
        metadata={"stage": "post_repair"},
    )
)
LEGACY_PROBE_REGISTRY.register(
    LegacyProbeAdapter(
        name="cte_base_constraints",
        phase=10,
        apply=_apply_cte_base_adapter,
        diff_types=frozenset({
            "cte_changed",
            "recursive_cte_changed",
            "recursive_step_expression_changed",
        }),
        read_set_factory=_cte_base_column_set,
        write_set_factory=_cte_base_column_set,
        activation_guard=_contains_cte,
    )
)
LEGACY_PROBE_REGISTRY.register(
    LegacyProbeAdapter(
        name="distinct_projection",
        phase=11,
        apply=_apply_distinct_adapter,
        diff_types=frozenset({
            "distinct_changed",
            "aggregate_distinct_changed",
        }),
        clauses=frozenset({"DISTINCT", "AGGREGATE"}),
        knowledge_points=frozenset({"distinct", "aggregate"}),
        read_set_factory=_distinct_column_set,
        write_set_factory=_distinct_column_set,
        sql_trigger=lambda standard, student: (
            _distinct_shape_changed(standard, student)
        ),
        metadata={"stage": "final"},
    )
)
LEGACY_PROBE_REGISTRY.register(
    LegacyProbeAdapter(
        name="bounded_order_ties",
        phase=12,
        apply=_apply_bounded_order_ties_adapter,
        diff_types=frozenset({
            "limit_changed",
            "window_over_changed",
            "window_function_changed",
        }),
        read_set_factory=_order_key_column_set,
        write_set_factory=_order_key_column_set,
    )
)
LEGACY_PROBE_REGISTRY.register(
    LegacyProbeAdapter(
        name="order_key_separation",
        phase=12,
        apply=_apply_order_key_adapter,
        diff_types=frozenset({
            "order_by_changed",
            "order_by_tiebreaker_missing",
            "order_by_key_added",
            "order_direction_changed",
            "order_nulls_changed",
        }),
        clauses=frozenset({"ORDER BY"}),
        knowledge_points=frozenset({"order-by"}),
        read_set_factory=_order_key_column_set,
        write_set_factory=_order_key_column_set,
    )
)
LEGACY_PROBE_REGISTRY.register(
    LegacyProbeAdapter(
        name="join_matched_dangling",
        phase=5,
        apply=_apply_join_matched_dangling_adapter,
        diff_types=frozenset({
            "join_missing",
            "join_type_changed",
            "join_predicate_placement_changed",
        }),
        clauses=frozenset({"JOIN", "JOIN_TYPE", "JOIN ON"}),
        knowledge_points=frozenset({
            "join-inner", "join-left", "join-right", "join-full", "join-on",
        }),
        read_set_factory=_join_matched_dangling_column_set,
        write_set_factory=_join_matched_dangling_column_set,
        metadata={"stage": "final"},
    )
)
