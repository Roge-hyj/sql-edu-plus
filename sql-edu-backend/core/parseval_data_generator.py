"""ParSEval-inspired test data generation for SQL evidence collection.

This is a bounded, practical implementation for the SQL tutoring pipeline.
It generates small operator-targeted databases from:
- schema text
- standard SQL
- student SQL

The generated rows are designed to expose common SQL DQL mistakes:
WHERE predicates, LIKE/IN/BETWEEN/NULL, JOIN links, GROUP BY/HAVING,
DISTINCT, ORDER BY, LIMIT/TOP, and simple aggregation differences.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Iterable
from collections import Counter, defaultdict
from contextvars import ContextVar
from itertools import product
import math
import re
import sqlite3
import time

import sqlglot
from sqlglot import ErrorLevel, exp
from sqlglot.dialects.sqlite import SQLite
from core.ast_schema import ASTDiffNode
from core.native_engine_runner import (
    NativeQueryExecutionError,
    NativeQuerySession,
    execute_native_query,
    native_query_session,
)
from core.native_query_safety import (
    NativeQuerySafetyError,
    validate_native_query_safety,
)
from core.sql_dialect_resolver import (
    DialectResolution,
    DialectResolutionError,
    DialectResolutionSource,
    GENERIC_SQLGLOT_DIALECT,
    STANDARD_SQL_DIALECT,
    normalize_sql_dialect,
    resolve_sql_dialect_or_raise,
)
from core.witness_generation.schema_scope import (
    ColumnRef,
    ColumnSchema,
    SchemaCatalog,
    SchemaQualification,
    TableSchema,
    analyze_schema_qualification,
    extract_physical_table_names,
)
from core.witness_generation.obligations import (
    ConstraintSpec,
    DistinguishingObligation,
    compile_obligations,
    is_redundant_summary_diff,
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
from core.witness_generation.validators import validate_obligation
from core.witness_generation.adapters import LegacyProbeAdapter, LegacyProbeRegistry, run_adapter
from core.witness_generation.regex_support import (
    RegexEvaluationError,
    first_regex_non_match,
    glob_separating_values,
    like_separating_values,
    regex_matches,
    regex_separating_values,
    similar_separating_values,
    similar_to_matches,
)
SQLite.Generator.SUPPORTS_TABLE_ALIAS_COLUMNS = True

_MUTATION_RENDER_DIALECT: ContextVar[str] = ContextVar(
    "parseval_mutation_render_dialect",
    default="sqlite",
)
_MUTATION_ORIGINAL_EQUIVALENT: ContextVar[bool] = ContextVar(
    "parseval_mutation_original_equivalent",
    default=False,
)
_STRUCTURE_PARSE_DIALECT: ContextVar[str] = ContextVar(
    "parseval_structure_parse_dialect",
    default="",
)

_MAX_WITNESS_WORLDS = 8
_MAX_WITNESS_ATTEMPTS = 8
_MAX_WITNESS_ROWS_PER_TABLE = 32
_SQLITE_PROGRESS_GRANULARITY = 10_000
_SQLITE_VM_INSTRUCTION_BUDGET = 1_000_000
_SQLITE_EXECUTION_TIME_BUDGET_SECONDS = 0.5


# Public Phase 1 verdicts.  ``is_equivalent`` remains as a compatibility
# field, while callers migrate to the richer status/conclusion contract.
VERDICT_SUPPORTED = "SUPPORTED"
VERDICT_SUPPORTED_WITH_LIMITS = "SUPPORTED_WITH_LIMITS"
VERDICT_SEMANTIC_BOUNDARY = "SEMANTIC_BOUNDARY"
VERDICT_KNOWN_GAP = "KNOWN_GAP"
VERDICT_ENGINE_GAP = "ENGINE_GAP"
VERDICT_INPUT_GAP = "INPUT_GAP"
EQUIVALENCE_UNDECIDED = "UNDECIDED"
EQUIVALENCE_NO_COUNTEREXAMPLE = "NO_COUNTEREXAMPLE_FOUND"


@dataclass
class SandboxRun:
    executed: bool
    is_equivalent: bool | None
    error: str | None
    standard_sqlite: str | None
    student_sqlite: str | None
    standard_rows: list[tuple[Any, ...]]
    student_rows: list[tuple[Any, ...]]
    standard_columns: list[str]
    student_columns: list[str]
    test_database: dict[str, list[dict[str, Any]]]
    data_evidence: dict[str, Any]
    mutation_evidence: dict[str, Any]
    ast_diffs: list[ASTDiffNode] = field(default_factory=list)
    judge_status: str = "ENGINE_ERROR"
    error_code: str | None = None
    # Rich verdict contract.  These fields are intentionally additive so the
    # existing boolean API remains source-compatible during Phase 1 migration.
    status: str = VERDICT_ENGINE_GAP
    equivalence_conclusion: str = EQUIVALENCE_UNDECIDED
    boundary_evidence: dict[str, Any] = field(default_factory=dict)


NUMERIC_HINTS = (
    "id", "no", "number", "num", "year", "cred", "credit", "salary", "budget",
    "capacity", "price", "unit", "stock", "order", "level", "hours", "discount",
    "freight", "count", "amount", "amt", "purch", "revenue", "profit", "score",
    "gpa", "grade", "mark", "point", "total", "view", "game", "played",
    "qty", "quantity", "ssn", "dno", "dnum", "pno",
)
DATE_HINTS = ("date", "bdate", "start", "end", "time")


def parse_schema_text(schema: str) -> dict[str, list[str]]:
    """Parse compact schema text like table(col, col); [Order Details](...)."""
    tables: dict[str, list[str]] = {}
    for raw_part in schema.split(";"):
        part = raw_part.strip()
        if not part or "(" not in part or ")" not in part:
            continue
        name = part[: part.find("(")].strip()
        cols = part[part.find("(") + 1 : part.rfind(")")]
        table_name = _clean_identifier(name)
        # Tokenize respecting bracket/backtick/double-quote quoted identifiers
        col_tokens: list[str] = []
        for tok in _split_schema_columns(cols):
            tok = tok.strip()
            if not tok:
                continue
            m = re.match(r'(\[[^\]]+\]|`[^`]+`|"[^"]+")(\s+.*)?$', tok)
            if m:
                col_tokens.append(m.group(1))
            else:
                col_tokens.append(tok.split()[0])
        columns = [_clean_identifier(c) for c in col_tokens]
        columns = [col for col in columns if col]
        # Public teaching corpora occasionally contain duplicate display
        # headers. SQLite cannot create two physical columns with the same
        # normalized name, so keep the first occurrence deterministically.
        deduped: list[str] = []
        seen_columns: set[str] = set()
        for column in columns:
            normalized = _norm_name(column)
            if normalized in seen_columns:
                continue
            seen_columns.add(normalized)
            deduped.append(column)
        columns = deduped
        if table_name and columns:
            existing_name = next(
                (name for name in tables if _norm_name(name) == _norm_name(table_name)),
                None,
            )
            if existing_name is None:
                tables[table_name] = columns
            else:
                existing = tables[existing_name]
                known = {_norm_name(column) for column in existing}
                existing.extend(column for column in columns if _norm_name(column) not in known)
    return tables


def parse_schema_column_types(schema: str) -> dict[str, dict[str, str]]:
    """Parse optional compact column type hints from schema text.

    Supports both legacy `table(col, col)` and typed forms such as
    `orders(id BIGINT, created_at DATETIME, amount DECIMAL)`.
    """
    table_types: dict[str, dict[str, str]] = {}
    for raw_part in schema.split(";"):
        part = raw_part.strip()
        if not part or "(" not in part or ")" not in part:
            continue
        table_name = _clean_identifier(part[: part.find("(")].strip())
        if not table_name:
            continue
        cols = part[part.find("(") + 1 : part.rfind(")")]
        for tok in _split_schema_columns(cols):
            tok = tok.strip()
            if not tok:
                continue
            match = re.match(r'(\[[^\]]+\]|`[^`]+`|"[^"]+"|[A-Za-z_][\w$]*)(?:\s+(.+))?$', tok)
            if not match:
                continue
            column = _clean_identifier(match.group(1))
            type_hint = (match.group(2) or "").strip()
            if column and type_hint:
                table_types.setdefault(table_name, {})[column] = type_hint
    return table_types


def _split_schema_columns(cols: str) -> list[str]:
    tokens: list[str] = []
    start = 0
    depth = 0
    quote: str | None = None
    bracket = False
    for index, char in enumerate(cols):
        if bracket:
            if char == "]":
                bracket = False
            continue
        if quote:
            if char == quote:
                quote = None
            continue
        if char == "[":
            bracket = True
            continue
        if char in {"'", '"', "`"}:
            quote = char
            continue
        if char == "(":
            depth += 1
            continue
        if char == ")" and depth:
            depth -= 1
            continue
        if char == "," and depth == 0:
            tokens.append(cols[start:index].strip())
            start = index + 1
    tail = cols[start:].strip()
    if tail:
        tokens.append(tail)
    return tokens


def generate_and_compare(
    schema_text: str,
    standard_sql: str,
    student_sql: str,
    *,
    max_rows_per_table: int = 8,
    sql_dialect: str | None = None,
    default_sql_dialect: str = "mysql",
    dialect_resolution: DialectResolution | None = None,
    execution_backend: str | None = None,
    native_executor_url: str | None = None,
    schema_catalog: SchemaCatalog | dict[str, Any] | None = None,
) -> SandboxRun:
    schema = parse_schema_text(schema_text)
    schema_types = parse_schema_column_types(schema_text)
    try:
        resolution = dialect_resolution or resolve_sql_dialect_or_raise(
            declared_dialect=sql_dialect,
            standard_sql=standard_sql,
            student_sql=student_sql,
            default_dialect=default_sql_dialect,
        )
    except DialectResolutionError as exc:
        status = {
            "STUDENT_SQL_PARSE_ERROR": "WRONG",
            "STANDARD_SQL_PARSE_ERROR": "INPUT_ERROR",
        }.get(exc.code, "ENGINE_ERROR")
        if exc.code in {"DIALECT_CONFLICT", "UNSUPPORTED_DIALECT", "UNSUPPORTED_DIALECT_FEATURE"}:
            status = "UNSUPPORTED"
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
            sql_dialect=sql_dialect,
        )
    target_dialect = resolution.resolved_dialect
    backend = _select_execution_backend(
        target_dialect=target_dialect,
        execution_backend=execution_backend,
        native_executor_url=native_executor_url,
    )
    if backend == "invalid_backend":
        return _failed(
            f"UNSUPPORTED_EXECUTION_BACKEND: {execution_backend}",
            None,
            None,
            {},
            [],
            [],
            status="ENGINE_ERROR",
            execution_backend=str(execution_backend),
            sql_dialect=target_dialect,
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
            execution_backend=backend,
            sql_dialect=target_dialect,
        )
    if schema_catalog is not None:
        # A supplied catalog is authoritative.  Compact schema text remains a
        # portable display/fallback format, not a second source of truth.
        schema = catalog.as_legacy()
        schema_types = catalog.as_legacy_types()
    if _mentions_sys_views(standard_sql) or _mentions_sys_views(student_sql):
        schema.setdefault("Sys.Views", ["Name"])
        schema_types.setdefault("Sys.Views", {"Name": "TEXT"})
        if catalog.table("Sys.Views") is None:
            catalog.physical_tables["sys.views"] = TableSchema(
                name="Sys.Views",
                columns={"name": ColumnSchema(name="Name")},
            )
    if not schema and (_extract_table_names(standard_sql) or _extract_table_names(student_sql)):
        return _failed("schema_parse_failed", None, None, {}, [], [], status="ENGINE_ERROR")

    unsupported_features = _detect_unsupported_features(
        backend,
        target_dialect,
        standard_sql,
        student_sql,
    )
    if unsupported_features:
        feature_text = ", ".join(unsupported_features)
        return _failed(
            f"unsupported_{backend}_feature: {feature_text}",
            None,
            None,
            {},
            [],
            [],
            status="UNSUPPORTED",
            unsupported_features=unsupported_features,
            execution_backend=backend,
            sql_dialect=target_dialect,
        )

    parse_dialect = resolution.parse_dialect or GENERIC_SQLGLOT_DIALECT
    resolved_asts = resolution.asts if len(resolution.asts) == 2 else ()
    standard_ast = (
        resolved_asts[0]
        if resolved_asts
        else _parse_sql_strict(standard_sql, dialect=parse_dialect)
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
    student_ast = (
        resolved_asts[1]
        if resolved_asts
        else _parse_sql_strict(student_sql, dialect=parse_dialect)
    )
    if student_ast is None:
        return _failed("student_sql_parse_failed", None, None, {}, [], [], status="WRONG")
    qualification_dialect = (
        target_dialect
        if resolution.source == DialectResolutionSource.DEFAULT
        else parse_dialect
    )
    standard_qualification = analyze_schema_qualification(
        standard_sql,
        catalog,
        dialect=qualification_dialect,
    )
    if not standard_qualification.executable:
        return _failed(
            _schema_qualification_error("standard", standard_qualification),
            None,
            None,
            {},
            [],
            [],
            status="ENGINE_ERROR",
            error_code="STANDARD_SCHEMA_QUALIFICATION_FAILED",
            execution_backend=backend,
            sql_dialect=target_dialect,
        )
    student_qualification = analyze_schema_qualification(
        student_sql,
        catalog,
        dialect=qualification_dialect,
    )
    if not student_qualification.executable:
        # Let the real dialect engine reject a student-only bad column/table.
        # The qualification pass is still authoritative for SQLite fixture
        # construction, but native execution is the source of truth for
        # dialect-specific name resolution and error classification.
        if _is_native_backend(backend) and student_qualification.missing_tables:
            return _failed(
                _schema_qualification_error("student", student_qualification),
                None,
                None,
                {},
                [],
                [],
                status="SECURITY_REJECTED",
                error_code="NATIVE_SQL_UNSAFE_OBJECT",
                execution_backend=backend,
                sql_dialect=target_dialect,
            )
        if _is_native_backend(backend):
            # Native engines must classify student-only missing columns using
            # their own resolver; SQLite/schema qualification must not turn a
            # student execution error into a pre-execution WRONG result.
            pass
        elif student_qualification.missing_tables:
            return _failed(
                _schema_qualification_error("student", student_qualification),
                None,
                None,
                {},
                [],
                [],
                status="WRONG",
                error_code="STUDENT_SCHEMA_REFERENCE_FAILED",
                execution_backend=backend,
                sql_dialect=target_dialect,
            )
    ast_diffs = extract_ast_diffs(
        standard_sql,
        student_sql,
        dialect=parse_dialect,
        schema_catalog=catalog,
    )
    structure_token = _STRUCTURE_PARSE_DIALECT.set(parse_dialect)
    try:
        witness_suite = generate_witness_suite(
            catalog,
            standard_sql,
            student_sql,
            max_rows_per_table=max_rows_per_table,
            ast_diffs=ast_diffs,
        )
    finally:
        _STRUCTURE_PARSE_DIALECT.reset(structure_token)
    rows = witness_suite.worlds[0].database

    standard_executable, student_executable = _prepare_executable_sql_pair(
        backend,
        standard_sql,
        student_sql,
        standard_ast=standard_ast,
        student_ast=student_ast,
        target_dialect=target_dialect,
        source_dialect=(
            target_dialect
            if resolution.source == DialectResolutionSource.DEFAULT
            else resolution.parse_dialect
        ),
        preserve_source_sql=(
            resolution.source == DialectResolutionSource.DECLARED
            and resolution.requested_dialect != STANDARD_SQL_DIALECT
        ),
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
            execution_backend=backend,
            sql_dialect=target_dialect,
        )

    if _is_native_backend(backend):
        allowed_tables = schema.keys()
        for owner, executable_sql, failure_status in (
            ("standard", standard_executable, "ENGINE_ERROR"),
            ("student", student_executable, "SECURITY_REJECTED"),
        ):
            try:
                validate_native_query_safety(
                    executable_sql,
                    target_dialect,
                    allowed_tables=allowed_tables,
                )
            except NativeQuerySafetyError as exc:
                return _failed(
                    f"{owner}_native_security_failed: {exc}",
                    standard_executable,
                    student_executable,
                    rows,
                    [],
                    [],
                    status=failure_status,
                    error_code=exc.code,
                    execution_backend=backend,
                    sql_dialect=target_dialect,
                )

    run = _complete_comparison(
        backend=backend,
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
        resolution=resolution,
        target_dialect=target_dialect,
        structure_dialect=parse_dialect,
        native_executor_url=native_executor_url,
        execution_session=None,
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
    return run


def _complete_comparison(
    *,
    backend: str,
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
    resolution: DialectResolution,
    target_dialect: str,
    structure_dialect: str,
    native_executor_url: str | None,
    execution_session: NativeQuerySession | None,
    witness_suite: WitnessSuite | None = None,
    schema_catalog: SchemaCatalog | None = None,
) -> SandboxRun:
    """Execute bounded witness worlds and select the first real counterexample."""

    suite = witness_suite or WitnessSuite(
        worlds=[WitnessWorld(id="world_01", database=rows)],
        obligations=[],
    )
    if _is_native_backend(backend) and execution_session is None and suite.worlds:
        native_world = next(
            (
                world
                for world in reversed(suite.worlds)
                if "compatibility_composite_world" in world.diagnostics
            ),
            suite.worlds[0],
        )
        native_suite = WitnessSuite(
            worlds=[native_world],
            obligations=suite.obligations,
            uncovered_obligations=list(suite.uncovered_obligations),
            planner_diagnostics=list(suite.planner_diagnostics)
            + ["native_fixture_reuse_single_world"],
        )
        try:
            with native_query_session(
                backend,
                schema,
                schema_types,
                native_world.database,
                native_executor_url or "",
            ) as session:
                result = _complete_comparison(
                    backend=backend,
                    schema=schema,
                    schema_types=schema_types,
                    rows=native_world.database,
                    standard_sql=standard_sql,
                    student_sql=student_sql,
                    standard_executable=standard_executable,
                    student_executable=student_executable,
                    standard_ast=standard_ast,
                    student_ast=student_ast,
                    ast_diffs=ast_diffs,
                    resolution=resolution,
                    target_dialect=target_dialect,
                    structure_dialect=structure_dialect,
                    native_executor_url=native_executor_url,
                    execution_session=session,
                    witness_suite=native_suite,
                    schema_catalog=schema_catalog,
                )
        except Exception as exc:
            return _failed(
                f"native_session_failed: {exc}",
                standard_executable,
                student_executable,
                native_world.database,
                [],
                [],
                status="TIMEOUT" if _is_execution_timeout(exc) else "ENGINE_ERROR",
                error_code=getattr(exc, "code", None),
                execution_backend=backend,
                sql_dialect=target_dialect,
            )
        result.data_evidence["planned_witness_suite"] = suite.to_evidence()
        result.data_evidence["native_execution_world_id"] = native_world.id
        return result
    obligation_by_id = {item.id: item for item in suite.obligations}
    trials: list[tuple[WitnessWorld, SandboxRun, int]] = []
    selected: tuple[WitnessWorld, SandboxRun, int] | None = None
    shared_session = execution_session if len(suite.worlds) == 1 else None

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
                structure_dialect=structure_dialect,
                attempt=attempt,
                schema_catalog=schema_catalog,
            ):
                break
            trial = _run_witness_world(
                backend=backend,
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
                resolution=resolution,
                target_dialect=target_dialect,
                structure_dialect=structure_dialect,
                native_executor_url=native_executor_url,
                execution_session=shared_session,
                run_mutations=False,
            )
            terminal_student_error = bool(
                _is_native_backend(backend)
                and
                trial.executed
                and trial.data_evidence.get("student_exec_ok") is False
            )
            if terminal_student_error:
                atomic_validation = {
                    "supported_count": 0,
                    "all_supported_distinguished": False,
                    "tests": [],
                    "reason": "student_execution_error_is_terminal",
                }
            else:
                atomic_validation = _validate_world_atomic_diffs(
                    world=world,
                    run=trial,
                    ast_diffs=ast_diffs,
                    backend=backend,
                    schema=schema,
                    schema_types=schema_types,
                    target_dialect=target_dialect,
                    native_executor_url=native_executor_url,
                    execution_session=shared_session,
                )
            _record_world_attempt(world, trial, attempt, atomic_validation)
            trials.append((world, trial, attempt))
            pair_distinguished = trial.executed and trial.is_equivalent is False
            if pair_distinguished and selected is None:
                selected = (world, trial, attempt)
            if terminal_student_error:
                break
            obligation_distinguished = bool(
                atomic_validation.get("all_supported_distinguished")
            )
            if pair_distinguished and obligation_distinguished:
                validated = _run_witness_world(
                    backend=backend,
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
                    resolution=resolution,
                    target_dialect=target_dialect,
                    structure_dialect=structure_dialect,
                    native_executor_url=native_executor_url,
                    execution_session=shared_session,
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
        if (
            selected is not None
            and _is_native_backend(backend)
            and selected[1].data_evidence.get("student_exec_ok") is False
        ):
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
            execution_backend=backend,
            sql_dialect=target_dialect,
        )
        _attach_witness_evidence(failed, suite, None, ast_diffs)
        return failed

    selected_world, selected_trial, selected_attempt = selected
    final_run = selected_trial
    if not selected_trial.executed:
        _attach_witness_evidence(final_run, suite, selected_world.id, ast_diffs)
        return final_run
    if (
        not final_run.mutation_evidence.get("enabled")
        and not (
            _is_native_backend(backend)
            and final_run.data_evidence.get("student_exec_ok") is False
        )
    ):
        replay_world = WitnessWorld(
            id=selected_world.id,
            obligation_ids=list(selected_world.obligation_ids),
            diff_ids=list(selected_world.diff_ids),
            constraints=list(selected_world.constraints),
            minimum_rows=dict(selected_world.minimum_rows),
            database=selected_trial.test_database,
        )
        replay = _run_witness_world(
            backend=backend,
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
            resolution=resolution,
            target_dialect=target_dialect,
            structure_dialect=structure_dialect,
            native_executor_url=native_executor_url,
            execution_session=shared_session,
            run_mutations=True,
        )
        if replay.executed:
            final_run = replay
        elif selected_trial.executed and not _is_native_backend(backend):
            final_run.mutation_evidence["reason"] = "selected_world_replay_failed"
        else:
            final_run = replay
    selected_world.execution["selected"] = True
    selected_world.execution["selected_attempt"] = selected_attempt
    selected_world.execution["selection_reason"] = (
        "pair_distinguished" if selected_trial.is_equivalent is False else "first_executable_world"
    )
    _attach_witness_evidence(final_run, suite, selected_world.id, ast_diffs)
    return final_run


def _run_witness_world(
    *,
    backend: str,
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
    resolution: DialectResolution,
    target_dialect: str,
    structure_dialect: str,
    native_executor_url: str | None,
    execution_session: NativeQuerySession | None,
    run_mutations: bool,
) -> SandboxRun:
    world.execution.setdefault("validation_context", {}).update({
        "standard_sql": standard_executable,
        "student_sql": student_executable,
        "execution_backend": backend,
    })

    def execute(session: NativeQuerySession | None) -> SandboxRun:
        return _complete_comparison_single(
            backend=backend,
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
            resolution=resolution,
            target_dialect=target_dialect,
            structure_dialect=structure_dialect,
            native_executor_url=native_executor_url,
            execution_session=session,
            run_mutations=run_mutations,
        )

    if not _is_native_backend(backend) or execution_session is not None:
        return execute(execution_session)
    try:
        with native_query_session(
            backend,
            schema,
            schema_types,
            world.database,
            native_executor_url or "",
        ) as session:
            return execute(session)
    except Exception as exc:
        return _failed(
            f"native_session_failed: {exc}",
            standard_executable,
            student_executable,
            world.database,
            [],
            [],
            status="TIMEOUT" if _is_execution_timeout(exc) else "ENGINE_ERROR",
            error_code=getattr(exc, "code", None),
            execution_backend=backend,
            sql_dialect=target_dialect,
        )


def _regenerate_witness_world(
    *,
    world: WitnessWorld,
    obligations: list[DistinguishingObligation],
    ast_diffs: list[ASTDiffNode],
    schema: dict[str, list[str]],
    standard_sql: str,
    student_sql: str,
    structure_dialect: str,
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
    token = _STRUCTURE_PARSE_DIALECT.set(structure_dialect)
    write_audit: list[Any] = []
    try:
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
    finally:
        _STRUCTURE_PARSE_DIALECT.reset(token)
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


def _validate_world_atomic_diffs(
    *,
    world: WitnessWorld,
    run: SandboxRun,
    ast_diffs: list[ASTDiffNode],
    backend: str,
    schema: dict[str, list[str]],
    schema_types: dict[str, dict[str, str]],
    target_dialect: str,
    native_executor_url: str | None,
    execution_session: NativeQuerySession | None,
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
    render_token = _MUTATION_RENDER_DIALECT.set(target_dialect)
    try:
        for diff_id, diff in indexed:
            variant_sql = _atomic_student_variant(diff)
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
                executable_sql = _prepare_mutation_sql(
                    variant_sql,
                    backend,
                    target_dialect,
                    allowed_tables=schema.keys(),
                )
                if not executable_sql:
                    raise ValueError("mutation_sql_prepare_failed")
                columns, result_rows = _execute_with_backend(
                    backend=backend,
                    schema=schema,
                    schema_types=schema_types,
                    rows=run.test_database,
                    sql=executable_sql,
                    native_executor_url=native_executor_url,
                    execution_session=execution_session,
                )
                ordered = bool(run.data_evidence.get("ordered_compare"))
                equivalent = _rows_equivalent(
                    run.standard_columns,
                    run.standard_rows,
                    columns,
                    result_rows,
                    ordered,
                )
                tests.append(
                    {
                        "diff_id": diff_id,
                        "supported": True,
                        "distinguished": not equivalent,
                        "variant_sql": executable_sql,
                        "standard_result": run.standard_rows[:5],
                        "mutant_result": result_rows[:5],
                    }
                )
            except Exception as exc:
                tests.append(
                    {
                        "diff_id": diff_id,
                        "supported": False,
                        "distinguished": False,
                        "reason": str(exc),
                    }
                )
    finally:
        _MUTATION_RENDER_DIALECT.reset(render_token)
    supported = [item for item in tests if item.get("supported")]
    return {
        "supported_count": len(supported),
        "all_supported_distinguished": bool(supported)
        and all(item.get("distinguished") for item in supported)
        and len(supported) == len(tests),
        "tests": tests,
    }


def _atomic_student_variant(diff: ASTDiffNode) -> str | None:
    if diff.diff_type == "join_predicate_placement_changed":
        return str(diff.extra.get("student_query_sql") or "") or None
    if diff.diff_type in {"in_exists_equivalence", "null_sensitive_antijoin_equivalence"}:
        return str(
            diff.extra.get("student_query_sql")
            or diff.extra.get("student_sql")
            or ""
        ) or None
    standard_node = diff.standard_node
    if diff.diff_type in {
        "group_by_changed",
        "group_by_expression_changed",
        "grouping_grain_too_fine",
        "grouping_grain_too_coarse",
    }:
        standard_query_sql = str(diff.extra.get("standard_query_sql") or "")
        if standard_query_sql:
            dialect = _MUTATION_RENDER_DIALECT.get() or None
            root = _parse_sql(standard_query_sql, dialect=dialect)
            target = _top_select(root) if isinstance(root, exp.Expression) else None
            if isinstance(root, exp.Expression) and isinstance(target, exp.Select):
                student_group = (
                    diff.student_node
                    if isinstance(diff.student_node, exp.Group)
                    else None
                )
                if student_group is None:
                    student_query_sql = str(
                        diff.extra.get("student_query_sql") or ""
                    )
                    student_root = _parse_sql(student_query_sql, dialect=dialect)
                    student_select = (
                        _top_select(student_root)
                        if isinstance(student_root, exp.Expression)
                        else None
                    )
                    student_group = (
                        student_select.args.get("group")
                        if isinstance(student_select, exp.Select)
                        else None
                    )
                target.set(
                    "group",
                    student_group.copy()
                    if isinstance(student_group, exp.Group)
                    else None,
                )
                return _sql_of(root)
    if diff.diff_type == "predicate_added":
        student_node = diff.student_node
        standard_query_sql = str(diff.extra.get("standard_query_sql") or "")
        if not isinstance(student_node, exp.Expression) or not standard_query_sql:
            return None
        dialect = _MUTATION_RENDER_DIALECT.get() or None
        root = _parse_sql(standard_query_sql, dialect=dialect)
        target = _top_select(root) if isinstance(root, exp.Expression) else None
        if not isinstance(root, exp.Expression) or not isinstance(target, exp.Select):
            return None
        student_where = student_node.find_ancestor(exp.Where)
        if not isinstance(student_where, exp.Where):
            return None
        existing_where = target.args.get("where")
        if not isinstance(existing_where, exp.Where):
            if student_where.this is not student_node:
                return None
            target.set("where", exp.Where(this=student_node.copy()))
            return _sql_of(root)
        student_predicate = student_where.this
        if not isinstance(student_predicate, (exp.And, exp.Or)):
            return None
        existing_sql = _sql_of(existing_where.this).strip().upper()
        if existing_sql not in {
            _sql_of(student_predicate.left).strip().upper(),
            _sql_of(student_predicate.right).strip().upper(),
        }:
            return None
        target.set("where", student_where.copy())
        return _sql_of(root)
    if not isinstance(standard_node, exp.Expression):
        return None
    if diff.diff_type in {"set_operator_changed", "set_modifier_changed"}:
        student_node = diff.student_node
        if not isinstance(student_node, exp.Expression):
            return None
        if type(standard_node) is type(student_node):
            mutated = standard_node.copy()
            for argument in ("distinct", "by_name", "side", "kind"):
                mutated.set(argument, student_node.args.get(argument))
            replacement = mutated
        else:
            replacement = student_node
        root = standard_node
        while isinstance(root.parent, exp.Expression):
            root = root.parent
        if root is standard_node:
            return _sql_of(replacement)
        return _mutate_by_node_replacement(root, standard_node, replacement)
    root = standard_node
    while isinstance(root.parent, exp.Expression):
        root = root.parent
    replacement = diff.student_node if isinstance(diff.student_node, exp.Expression) else None
    if replacement is None and diff.diff_type in {
        "correlated_predicate_changed",
        "subquery_removed",
        "predicate_missing",
    }:
        # Removing a correlated EXISTS/IN predicate removes the enclosing
        # WHERE clause, not just the child AST node.  Popping the child would
        # render ``WHERE`` invalid and falsely mark the mutation unsupported.
        current: exp.Expression | None = standard_node
        while isinstance(current, exp.Expression):
            parent = current.parent
            if isinstance(parent, (exp.And, exp.Or)):
                sibling = parent.right if parent.left is current else parent.left
                if isinstance(sibling, exp.Expression):
                    return _mutate_by_node_replacement(root, parent, sibling)
            if isinstance(parent, exp.Where) and parent.this is current:
                query = parent.parent
                while isinstance(query, exp.Expression) and not isinstance(query, exp.Query):
                    query = query.parent
                if isinstance(query, exp.Query):
                    return _mutate_query_arg(root, query, "where", None)
            if isinstance(parent, exp.Where):
                break
            current = parent
    return _mutate_by_node_replacement(root, standard_node, replacement)


def _record_world_attempt(
    world: WitnessWorld,
    run: SandboxRun,
    attempt: int,
    atomic_validation: dict[str, Any],
) -> None:
    record = {
        "attempt": attempt,
        "executed": run.executed,
        "judge_status": run.judge_status,
        "error": run.error,
        "error_code": run.error_code,
        "distinguished": run.executed and run.is_equivalent is False,
        "standard_row_count": len(run.standard_rows),
        "student_row_count": len(run.student_rows),
        "standard_result": run.standard_rows[:5],
        "student_result": run.student_rows[:5],
        "obligation_distinguished": bool(
            atomic_validation.get("all_supported_distinguished")
        ),
        "atomic_validation": atomic_validation,
    }
    world.execution["attempted"] = True
    world.execution.setdefault("attempts", []).append(record)
    world.execution["distinguished"] = bool(
        world.execution.get("distinguished") or record["distinguished"]
    )
    world.execution["obligation_distinguished"] = bool(
        world.execution.get("obligation_distinguished")
        or record["obligation_distinguished"]
    )


def _record_world_mutation_validation(
    world: WitnessWorld,
    run: SandboxRun,
    ast_diffs: list[ASTDiffNode],
) -> None:
    _link_mutation_diff_ids(run.mutation_evidence, ast_diffs)
    relevant_tests = [
        item
        for item in run.mutation_evidence.get("tests", [])
        if set(item.get("diff_ids", [])) & set(world.diff_ids)
    ]
    world.execution["mutation_validation"] = {
        "enabled": bool(run.mutation_evidence.get("enabled")),
        "relevant_test_count": len(relevant_tests),
        "relevant_fixed_by_replacement": any(
            item.get("fixed_by_replacement") for item in relevant_tests
        ),
        "tests": [
            {
                "clause": item.get("clause"),
                "knowledge_point_id": item.get("knowledge_point_id"),
                "diff_ids": item.get("diff_ids", []),
                "replacement_exec_ok": item.get("replacement_exec_ok"),
                "replacement_equivalent": item.get("replacement_equivalent"),
                "fixed_by_replacement": item.get("fixed_by_replacement"),
            }
            for item in relevant_tests
        ],
    }


def _attach_witness_evidence(
    run: SandboxRun,
    suite: WitnessSuite,
    selected_world_id: str | None,
    ast_diffs: list[ASTDiffNode],
) -> None:
    effectiveness: list[dict[str, Any]] = []
    for obligation in suite.obligations:
        worlds = [world for world in suite.worlds if obligation.id in world.obligation_ids]
        chosen = next(
            (world for world in worlds if world.execution.get("obligation_distinguished")),
            next((world for world in worlds if world.execution.get("attempted")), worlds[0] if worlds else None),
        )
        declaration = declare_strategy(obligation)
        application = chosen.execution.get("constraint_application", {}) if chosen else {}
        relevant_unsatisfied = [
            item
            for item in application.get("unsatisfied", [])
            if item.get("constraint", {}).get("obligation_id") == obligation.id
        ]
        concrete_verified: bool | None
        if declaration.cell_constraints:
            concrete_verified = not relevant_unsatisfied
        else:
            concrete_verified = None
        attempts = chosen.execution.get("attempts", []) if chosen else []
        result_attempt = next(
            (item for item in attempts if item.get("distinguished")),
            attempts[-1] if attempts else {},
        )
        mutation_validation = chosen.execution.get("mutation_validation", {}) if chosen else {}
        atomic_tests = [
            item
            for attempt in attempts
            for item in attempt.get("atomic_validation", {}).get("tests", [])
            if item.get("diff_id") == obligation.diff_id
        ]
        atomic_distinguished = any(item.get("distinguished") for item in atomic_tests)
        semantic_validation = (
            validate_obligation(
                chosen,
                obligation,
                execution_distinguished=atomic_distinguished,
            ).to_dict()
            if chosen is not None
            else None
        )
        if semantic_validation is not None:
            chosen.execution.setdefault("obligation_validations", {})[obligation.id] = semantic_validation
        effectiveness.append(
            {
                "obligation_id": obligation.id,
                "diff_id": obligation.diff_id,
                "probe": declaration.strategy,
                "activated": chosen is not None,
                "constraints_satisfied": (
                    semantic_validation["constraints_satisfied"]
                    if semantic_validation is not None
                    else concrete_verified
                ),
                "constraint_verification": (
                    "semantic_validator"
                    if semantic_validation is not None
                    else "declarative_cell_verified"
                    if concrete_verified is not None
                    else "legacy_semantic_adapter_unverified"
                ),
                "semantic_validation": semantic_validation,
                "overwritten": bool(application.get("overwritten", False)),
                "world_id": chosen.id if chosen else None,
                "standard_result": result_attempt.get("standard_result", []),
                "student_result": result_attempt.get("student_result", []),
                "pair_distinguished": bool(chosen and chosen.execution.get("distinguished")),
                "distinguished": atomic_distinguished,
                "causal_attribution_verified": bool(
                    atomic_distinguished
                    and semantic_validation
                    and semantic_validation.get("constraints_satisfied")
                ),
                "atomic_validation": atomic_tests,
                "mutation_validation": mutation_validation,
                "attempt_count": len(attempts),
                "success_predicate": obligation.success_predicate,
            }
        )
    run.data_evidence["selected_witness_world_id"] = selected_world_id
    run.data_evidence["any_world_distinguished"] = any(
        world.execution.get("distinguished") for world in suite.worlds
    )
    run.data_evidence["any_obligation_distinguished"] = any(
        item["distinguished"] and item["constraints_satisfied"]
        for item in effectiveness
    )
    run.data_evidence["witness_suite"] = suite.to_evidence()
    run.data_evidence["obligation_effectiveness"] = effectiveness
    _link_mutation_diff_ids(run.mutation_evidence, ast_diffs)
    _finalize_witness_verdict(run, suite, ast_diffs)


def _finalize_witness_verdict(
    run: SandboxRun,
    suite: WitnessSuite,
    ast_diffs: list[ASTDiffNode],
) -> None:
    """Close the verdict only after the complete witness suite is audited.

    is_equivalent is retained as a legacy compatibility field. It means only
    that the currently executed bounded world produced equal results; it must
    not silently become a proof of equivalence when AST differences exist but
    no obligation has been distinguished.
    """
    if not run.executed:
        return
    if run.is_equivalent is False:
        _set_public_verdict(run, VERDICT_SUPPORTED, "NOT_EQUIVALENT")
        return
    if not ast_diffs:
        _set_public_verdict(
            run,
            VERDICT_SUPPORTED,
            EQUIVALENCE_NO_COUNTEREXAMPLE,
        )
        return
    if run.status == VERDICT_SEMANTIC_BOUNDARY:
        _set_public_verdict(
            run,
            VERDICT_SEMANTIC_BOUNDARY,
            EQUIVALENCE_UNDECIDED,
            boundary_evidence=run.boundary_evidence,
        )
        return
    if run.data_evidence.get("any_obligation_distinguished"):
        _set_public_verdict(
            run,
            VERDICT_SUPPORTED,
            EQUIVALENCE_NO_COUNTEREXAMPLE,
        )
        return
    _set_public_verdict(run, VERDICT_KNOWN_GAP, EQUIVALENCE_UNDECIDED)
    run.data_evidence["verdict_guard"] = {
        "reason": "ast_differences_without_distinguished_obligation",
        "obligation_count": len(suite.obligations),
        "distinguished_obligation": False,
    }


def _set_public_verdict(
    run: SandboxRun,
    status: str,
    conclusion: str,
    *,
    boundary_evidence: dict[str, Any] | None = None,
) -> None:
    """Keep the rich Phase 1 verdict and legacy judge field consistent.

    ``is_equivalent`` intentionally remains the bounded-world compatibility
    observation.  Callers must use ``equivalence_conclusion`` for a semantic
    conclusion; an equal bounded result at a known gap or boundary is never
    exposed as ``judge_status=CORRECT``.
    """

    run.status = status
    run.equivalence_conclusion = conclusion
    if boundary_evidence is not None:
        run.boundary_evidence = dict(boundary_evidence)
    if conclusion == "NOT_EQUIVALENT":
        run.judge_status = "WRONG"
    elif conclusion == EQUIVALENCE_UNDECIDED:
        run.judge_status = "UNDECIDED"
    elif run.executed:
        run.judge_status = "CORRECT"
    run.data_evidence["status"] = run.status
    run.data_evidence["equivalence_conclusion"] = run.equivalence_conclusion
    run.data_evidence["boundary_evidence"] = dict(run.boundary_evidence)
    run.data_evidence["judge_status"] = run.judge_status


def _link_mutation_diff_ids(
    mutation_evidence: dict[str, Any],
    ast_diffs: list[ASTDiffNode],
) -> None:
    indexed = [
        (stable_diff_id(diff, index), diff)
        for index, diff in enumerate(ast_diffs)
        if not is_redundant_summary_diff(diff, ast_diffs)
    ]
    for item in mutation_evidence.get("tests", []):
        clause = str(item.get("clause") or "").upper()
        clause_matches = [
            diff_id
            for diff_id, diff in indexed
            if _mutation_clause_matches_diff(clause, diff)
        ]
        knowledge_point = str(item.get("knowledge_point_id") or "").lower()
        kp_matches = [
            diff_id
            for diff_id, diff in indexed
            if diff_id in clause_matches
            and knowledge_point
            and str(diff.knowledge_point_id or "").lower() == knowledge_point
        ]
        matches = kp_matches if len(kp_matches) == 1 else clause_matches
        item["diff_ids"] = matches
        item["obligation_ids"] = [
            f"obligation_{diff_id.removeprefix('diff_')}" for diff_id in matches
        ]
        item["binding_quality"] = (
            "exact"
            if len(matches) == 1 and (len(kp_matches) == 1 or len(clause_matches) == 1)
            else "ambiguous"
            if len(matches) > 1
            else "unbound"
        )
    mutation_evidence["diff_id_linked"] = bool(indexed)


def _mutation_clause_matches_diff(clause: str, diff: ASTDiffNode) -> bool:
    if not clause:
        return False
    diff_clause = diff.clause_category.upper()
    if (
        diff_clause == clause
        or diff_clause.startswith(clause)
        or clause.startswith(diff_clause)
    ):
        return True
    aliases = {
        "PREDICATE": {"WHERE", "HAVING", "QUALIFY", "JOIN ON"},
        "LOGICAL": {"WHERE", "HAVING", "QUALIFY", "JOIN ON"},
        "JOIN_TYPE": {"JOIN TYPE", "JOIN"},
        "SELECT": {"PROJECTION", "SELECT"},
        "AGGREGATE": {"AGGREGATE", "SELECT", "HAVING"},
        "CTE_RECURSIVE": {"CTE", "RECURSIVE CTE"},
    }
    if clause in aliases.get(diff_clause, set()):
        return True
    if diff_clause in {"UNION", "INTERSECT", "EXCEPT"} and clause in {
        "UNION",
        "INTERSECT",
        "EXCEPT",
        "SET OPERATOR",
    }:
        return True
    return False


def _complete_comparison_single(
    *,
    backend: str,
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
    resolution: DialectResolution,
    target_dialect: str,
    structure_dialect: str,
    native_executor_url: str | None,
    execution_session: NativeQuerySession | None,
    run_mutations: bool = True,
) -> SandboxRun:
    try:
        std_cols, std_rows = _execute_with_backend(
            backend=backend,
            schema=schema,
            schema_types=schema_types,
            rows=rows,
            sql=standard_executable,
            native_executor_url=native_executor_url,
            execution_session=execution_session,
        )
    except Exception as exc:
        status = "TIMEOUT" if _is_execution_timeout(exc) else (
            "UNSUPPORTED"
            if _is_likely_backend_capability_error(backend, str(exc), standard_executable)
            else "ENGINE_ERROR"
        )
        return _failed(
            f"standard_sql_failed: {exc}",
            standard_executable,
            student_executable,
            rows,
            [],
            [],
            status=status,
            error_code=getattr(exc, "code", None),
            execution_backend=backend,
            sql_dialect=target_dialect,
        )

    try:
        stu_cols, stu_rows = _execute_with_backend(
            backend=backend,
            schema=schema,
            schema_types=schema_types,
            rows=rows,
            sql=student_executable,
            native_executor_url=native_executor_url,
            execution_session=execution_session,
        )
        student_exec_error = None
    except Exception as exc:
        if _is_platform_execution_error(backend, exc):
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
                execution_backend=backend,
                sql_dialect=target_dialect,
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
    evidence["execution_backend"] = backend
    evidence["sql_dialect"] = target_dialect
    evidence["dialect_resolution"] = resolution.to_dict()
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
            backend=backend,
            schema_types=schema_types,
            sql_dialect=target_dialect,
            structure_dialect=structure_dialect,
            native_executor_url=native_executor_url,
            execution_session=execution_session,
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
        if _mentions_sys_views(standard_sql) or _mentions_sys_views(student_sql):
            target_tables["Sys.Views"] = schema.get("Sys.Views", ["Name"])
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
            "similar_pattern_changed",
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
        clauses={"DISTINCT", "DISTINCT ON"},
        diff_types={
            "distinct_changed",
            "aggregate_distinct_changed",
            "distinct_on_changed",
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
            "order_direction_changed", "limit_changed",
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
    connect_type = getattr(exp, "Connect", None)
    query_has_hierarchical_connect = bool(connect_type) and any(
        ast.find(connect_type) is not None
        for ast in parsed_queries
    )

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
    has_hierarchical_world = query_has_hierarchical_connect and _world_has_diff(
        ast_diffs,
        clauses={"CONNECT BY"},
        diff_types={"hierarchical_query_changed"},
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
        "hierarchical": has_hierarchical_world,
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
    # Compatibility tactics are dispatched once at this phase.  Previously
    # ORDER BY and WINDOW were also called through their fixed helpers below,
    # so a later probe could silently rewrite their evidence.
    # The registry is the single compatibility dispatch point for migrated
    # strategies.  Each active tactic runs once per witness world; the old
    # fixed calls for the same strategies were intentionally removed below.
    adapter_ledger = ConstraintLedger()
    for tactic in TacticRegistry.get_active_tactics(ast_diffs):
        with write_owner(f"registry:{tactic.name}"):
            tactic.apply_data_probe(data, schema, standard_sql, student_sql, ast_diffs)
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
    if has_cte_world:
        _apply_recursive_cte_safety(data, schema, standard_sql, student_sql)
    if has_cte_world or has_set_world:
        _apply_recursive_set_duplicate_probe(data, standard_sql, student_sql, ast_diffs)
    if has_cte_world:
        _apply_recursive_cte_orphan_probe(data, standard_sql, student_sql)
    _run_adapter_stage("post_repair")
    # Alias-aware probes rewrite window order columns. Apply ranking ties after
    # them so RANK/ROW_NUMBER/DENSE_RANK counterexamples survive to execution.
    if has_window_world:
        _apply_window_rank_gap_probe(data, standard_sql, student_sql)
    # These probes depend on the final row topology.  Run them after the
    # generic PK/dedup repairs so later normalization cannot break the path.
    if has_hierarchical_world:
        _apply_oracle_nocycle_probe(data, standard_sql, student_sql)
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
    _materialize_order_obligation_witness(data, standard_sql, ast_diffs)
    _materialize_subquery_membership_obligation_witness(
        data,
        ast_diffs,
        standard_sql,
        student_sql,
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
    _materialize_in_list_obligation_witness(data, standard_sql, ast_diffs)
    _materialize_case_obligation_witness(data, standard_sql, ast_diffs)
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
    _materialize_similar_pattern_witness(data, obligations or [])
    _materialize_limit_antijoin_path(
        data,
        obligations or [],
        standard_sql,
        student_sql,
    )


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


def _materialize_similar_pattern_witness(
    data: dict[str, list[dict[str, Any]]],
    obligations: list[DistinguishingObligation],
) -> None:
    """Write bounded values that separate two constant SIMILAR TO predicates."""
    table_lookup = {_norm_name(name): name for name in data}
    for obligation in obligations:
        spec = next(
            (
                item
                for item in obligation.hard_constraints
                if item.kind == "similar_pattern_separation"
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
        standard_escape = metadata.get("standard_escape")
        student_escape = metadata.get("student_escape")
        if not isinstance(standard_escape, str):
            standard_escape = "\\"
        if not isinstance(student_escape, str):
            student_escape = "\\"
        try:
            separated = similar_separating_values(
                standard_pattern,
                student_pattern,
                standard_escape=standard_escape,
                student_escape=student_escape,
            )
        except RegexEvaluationError:
            continue
        if not separated:
            continue
        with write_owner(f"materializer:{obligation.id}"):
            for row, item in zip(rows[:3], separated[:3]):
                row[column] = item[0]


def _predicate_assignment_truth(
    node: exp.Expression | None,
    assignment: dict[str, bool],
) -> bool | None:
    if node is None:
        return True
    node = _unwrap_paren(node)
    if isinstance(node, exp.And):
        left = _predicate_assignment_truth(node.left, assignment)
        right = _predicate_assignment_truth(node.right, assignment)
        return bool(left and right) if left is not None and right is not None else None
    if isinstance(node, exp.Or):
        left = _predicate_assignment_truth(node.left, assignment)
        right = _predicate_assignment_truth(node.right, assignment)
        return bool(left or right) if left is not None and right is not None else None
    if isinstance(node, exp.Not):
        value = _predicate_assignment_truth(node.this, assignment)
        return not value if value is not None else None
    return assignment.get(_sql_of(node))


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
    selected_assignment: dict[str, bool] | None = None
    selected_values: dict[str, Any] = {}
    for truth_values in product((False, True), repeat=len(keys)):
        assignment = dict(zip(keys, truth_values))
        standard_truth = _predicate_assignment_truth(standard_predicate, assignment)
        student_truth = _predicate_assignment_truth(student_predicate, assignment)
        if standard_truth is None or student_truth is None or standard_truth == student_truth:
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
            selected_assignment = assignment
            selected_values = values
            break
    if selected_assignment is None:
        return

    candidate_tables = []
    for table_name, rows in data.items():
        if not rows:
            continue
        lookup = _column_lookup(rows[0].keys())
        if all(column in lookup for column in selected_values):
            candidate_tables.append((table_name, rows, lookup))
    if len(candidate_tables) != 1:
        return
    _table_name, rows, lookup = candidate_tables[0]
    with write_owner("materializer:predicate_positive_negative"):
        for column, value in selected_values.items():
            rows[0][lookup[column]] = value


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


def _catalog_has_unary_unique_key(
    catalog: SchemaCatalog | None,
    ref: tuple[str, str],
) -> bool:
    if catalog is None:
        return False
    table = catalog.table(ref[0])
    if table is None:
        return False
    column = _norm_name(ref[1])
    if (
        len(table.primary_key) == 1
        and _norm_name(table.primary_key[0]) == column
    ):
        return True
    return any(
        len(constraint) == 1
        and _norm_name(constraint[0]) == column
        for constraint in table.unique_constraints
    )


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

    where = select.args.get("where")
    if not isinstance(where, exp.Where):
        return
    for comparison in where.find_all(exp.EQ):
        function, literal = comparison.left, comparison.right
        if isinstance(comparison.right, (exp.Extract, exp.Year, exp.Month, exp.Day)):
            function, literal = comparison.right, comparison.left
        if not isinstance(literal, exp.Literal):
            continue
        part = ""
        column = None
        if isinstance(function, exp.Extract):
            part = str(function.this).upper()
            column = (
                function.expression
                if isinstance(function.expression, exp.Column)
                else function.find(exp.Column)
            )
        elif isinstance(function, (exp.Year, exp.Month, exp.Day)):
            part = type(function).__name__.upper()
            column = (
                function.this
                if isinstance(function.this, exp.Column)
                else function.find(exp.Column)
            )
        value = _integer_node_value(literal)
        if not isinstance(column, exp.Column) or value is None:
            continue
        ref = _column_ref_in_select_data(data, column, select)
        actual = _actual_data_ref(data, ref) if ref else None
        if not actual:
            continue
        rows, column_name = actual
        actual_table = next(
            (name for name, candidate_rows in data.items() if candidate_rows is rows),
            "",
        )
        if target_table and _norm_name(actual_table) != _norm_name(target_table):
            continue
        if part == "YEAR":
            date_value = f"{value:04d}-01-01"
        elif part == "MONTH":
            date_value = f"2024-{max(1, min(12, value)):02d}-01"
        elif part == "DAY":
            date_value = f"2024-01-{max(1, min(28, value)):02d}"
        else:
            continue
        for row in rows[:row_count]:
            row[column_name] = date_value


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


def _materialized_order_keys(value: Any) -> list[tuple[str, bool]]:
    result: list[tuple[str, bool]] = []
    for item in value or ():
        if not isinstance(item, (tuple, list)) or len(item) < 2:
            continue
        expression = str(item[0] or "").strip()
        if expression:
            result.append((expression, bool(item[1])))
    return result


def _simple_materialized_order_column(expression: str) -> str | None:
    node = _parse_sql(expression)
    if isinstance(node, exp.Ordered):
        node = node.this
    if not isinstance(node, exp.Column) or not node.name:
        return None
    return _norm_name(node.name)


def _order_materializer_values(column: str) -> tuple[Any, Any]:
    if _is_numeric_column(column):
        return 101, 202
    return "order_a", "order_z"


def _ordered_distinct_pair(
    left: Any,
    right: Any,
    column: str,
) -> tuple[Any, Any]:
    if left is None or right is None or left == right:
        return _order_materializer_values(column)
    try:
        ordered = sorted((left, right))
    except Exception:
        ordered = sorted((left, right), key=str)
    return ordered[0], ordered[1]


def _existing_order_pair_indexes(
    rows: list[dict[str, Any]],
    prefix_columns: list[str],
    discriminator_column: str,
) -> tuple[int, int] | None:
    groups: dict[tuple[Any, ...], list[tuple[int, Any]]] = {}
    for index, row in enumerate(rows):
        prefix = tuple(row.get(column) for column in prefix_columns)
        groups.setdefault(prefix, []).append((index, row.get(discriminator_column)))
    for candidates in groups.values():
        for position, (left_index, left_value) in enumerate(candidates):
            if left_value is None:
                continue
            for right_index, right_value in candidates[position + 1:]:
                if right_value is not None and right_value != left_value:
                    return left_index, right_index
    return None


def _materialize_order_obligation_witness(
    data: dict[str, list[dict[str, Any]]],
    standard_sql: str,
    ast_diffs: list[ASTDiffNode],
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
                "order_direction_changed",
                "order_by_tiebreaker_missing",
                "order_by_key_added",
            }
        ),
        None,
    )
    if order_diff is None:
        return
    standard_keys = _materialized_order_keys(
        order_diff.extra.get("standard_order_keys")
    )
    student_keys = _materialized_order_keys(
        order_diff.extra.get("student_order_keys")
    )
    prefix_keys: list[tuple[str, bool]]
    discriminator_key: tuple[str, bool] | None = None
    if order_diff.diff_type == "order_direction_changed":
        changed = next(
            (
                index
                for index, (standard, student) in enumerate(zip(standard_keys, student_keys))
                if standard[0].lower() == student[0].lower()
                and standard[1] != student[1]
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
        distinct_on = select.args.get("distinct") if isinstance(select, exp.Select) else None
        has_distinct_on = isinstance(distinct_on, exp.Distinct) and distinct_on.args.get("on") is not None
        protected_prefix = set(prefix_columns) if has_distinct_on else set()
        payload = next(
            (
                column for column in projected_columns
                if column and column not in protected_prefix
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


def _materialize_subquery_membership_obligation_witness(
    data: dict[str, list[dict[str, Any]]],
    ast_diffs: list[ASTDiffNode],
    standard_sql: str = "",
    student_sql: str = "",
) -> None:
    """Keep both matching and non-matching correlated outer paths."""
    diff = next(
        (
            item for item in ast_diffs
            if item.diff_type == "correlated_predicate_changed"
            and not item.extra.get("subquery_depth")
        ),
        None,
    )
    if diff is None:
        return
    requires_inner_null = any(
        item.diff_type == "null_sensitive_antijoin_equivalence"
        for item in ast_diffs
    )
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
    inner_values = {
        row.get(inner_column_actual)
        for row in inner_rows
        if row.get(inner_column_actual) is not None
    }
    if not inner_values:
        return
    with write_owner("materializer:subquery_membership_paths"):
        match_value = next(iter(inner_values))
        outer_rows[0][outer_column_actual] = match_value
        inner_rows[0][inner_column_actual] = match_value
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


def _materialize_in_list_obligation_witness(
    data: dict[str, list[dict[str, Any]]],
    standard_sql: str,
    ast_diffs: list[ASTDiffNode],
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

    ast = _parse_sql(standard_sql)
    select = _top_select(ast) if ast is not None else None
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
    structure_dialect = _STRUCTURE_PARSE_DIALECT.get() or None
    qualifications = (
        analyze_schema_qualification(standard_sql, catalog, dialect=structure_dialect),
        analyze_schema_qualification(student_sql, catalog, dialect=structure_dialect),
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


def transpile_to_sqlite(sql: str, source_dialect: str | None = None) -> str | None:
    prepared_sql = _prepare_sqlite_source(sql)
    manual = _manual_sqlite_compat(prepared_sql)
    dialects = (source_dialect,) if source_dialect else _dialect_candidates(prepared_sql)
    for dialect in dialects:
        try:
            candidates = sqlglot.transpile(
                prepared_sql,
                read=dialect,
                write="sqlite",
                identify=True,
                error_level=ErrorLevel.IGNORE,
            )
            if candidates:
                return _sqlite_compat(candidates[0])
        except Exception:
            continue
    if manual and re.match(r"(?is)^\s*(select\s+top|with\s+recursive|\(?\s*select)", prepared_sql):
        return manual
    return _manual_sqlite_compat(prepared_sql)


def _failed(
    error: str,
    standard_sqlite: str | None,
    student_sqlite: str | None,
    rows: dict[str, list[dict[str, Any]]],
    std_rows: list[tuple[Any, ...]],
    stu_rows: list[tuple[Any, ...]],
    *,
    status: str,
    error_code: str | None = None,
    boundary_evidence: dict[str, Any] | None = None,
    unsupported_features: list[str] | None = None,
    execution_backend: str = "sqlite",
    sql_dialect: str | None = None,
) -> SandboxRun:
    if status in {"WRONG", "SECURITY_REJECTED"}:
        public_status, conclusion = VERDICT_SUPPORTED, "NOT_EQUIVALENT"
    elif status == "INPUT_ERROR":
        public_status, conclusion = VERDICT_INPUT_GAP, EQUIVALENCE_UNDECIDED
    else:
        public_status, conclusion = VERDICT_ENGINE_GAP, EQUIVALENCE_UNDECIDED
    public_boundary_evidence = dict(boundary_evidence or {})
    return SandboxRun(
        executed=False,
        is_equivalent=None,
        error=error,
        standard_sqlite=standard_sqlite,
        student_sqlite=student_sqlite,
        standard_rows=std_rows,
        student_rows=stu_rows,
        standard_columns=[],
        student_columns=[],
        test_database=rows,
        data_evidence={
            "sandbox_executed": False,
            "sandbox_error": error,
            "judge_status": status,
            "status": public_status,
            "equivalence_conclusion": conclusion,
            "boundary_evidence": public_boundary_evidence,
            "error_code": error_code,
            "execution_backend": execution_backend,
            "sql_dialect": sql_dialect,
            "unsupported_features": unsupported_features or [],
        },
        mutation_evidence={
            "enabled": False,
            "summary": {"executed": 0, "fixed_by_replacement": 0},
            "tests": [],
            "error": error,
        },
        ast_diffs=[],
        judge_status=status,
        error_code=error_code,
        status=public_status,
        equivalence_conclusion=conclusion,
        boundary_evidence=public_boundary_evidence,
    )


def _schema_qualification_error(owner: str, qualification: SchemaQualification) -> str:
    details: list[str] = []
    if qualification.boundary_reason:
        details.append(qualification.boundary_reason)
    if qualification.missing_tables:
        details.append("missing_tables=" + ",".join(sorted(qualification.missing_tables)))
    if qualification.missing_columns:
        rendered = sorted(
            f"{reference.relation}.{reference.column}@{reference.query_scope}"
            for reference in qualification.missing_columns
        )
        details.append("missing_columns=" + ",".join(rendered))
    return f"{owner}_schema_qualification_failed: " + "; ".join(details or ["unknown"])


def _normalize_sql_dialect(sql_dialect: str | None) -> str:
    """Backward-compatible helper for internal callers that require a value."""
    return normalize_sql_dialect(sql_dialect) or "mysql"


def _select_execution_backend(
    *,
    target_dialect: str,
    execution_backend: str | None,
    native_executor_url: str | None,
) -> str:
    if execution_backend is None:
        # Direct library callers retain the historical SQLite compatibility
        # runner. API callers pass ``auto`` explicitly and must never change
        # the resolved dialect's execution semantics silently.
        return "sqlite"

    backend = (execution_backend or "auto").strip().lower()
    backend = {
        "postgresql": "postgres",
        "pg": "postgres",
        "sqlserver": "tsql",
        "sql_server": "tsql",
        "mssql": "tsql",
    }.get(backend, backend)
    if backend == "sqlite":
        return backend
    if backend in {"mysql", "postgres", "tsql", "oracle"}:
        return backend
    if backend in {"native", "auto"}:
        return target_dialect
    return "invalid_backend"


def _is_native_backend(backend: str) -> bool:
    return backend in {"mysql", "postgres", "tsql", "oracle"}


def _prepare_executable_sql_pair(
    backend: str,
    standard_sql: str,
    student_sql: str,
    *,
    standard_ast: exp.Expression | None = None,
    student_ast: exp.Expression | None = None,
    target_dialect: str | None = None,
    source_dialect: str | None = None,
    preserve_source_sql: bool = False,
) -> tuple[str | None, str | None]:
    if backend in {"mysql", "postgres", "tsql", "oracle"}:
        if preserve_source_sql:
            return _prepare_native_sql(standard_sql), _prepare_native_sql(student_sql)
        if not target_dialect or standard_ast is None or student_ast is None:
            return None, None
        return (
            _render_native_ast(standard_ast, target_dialect),
            _render_native_ast(student_ast, target_dialect),
        )
    return (
        transpile_to_sqlite(standard_sql, source_dialect=source_dialect),
        transpile_to_sqlite(student_sql, source_dialect=source_dialect),
    )


def _prepare_native_sql(sql: str) -> str:
    return sql.strip().rstrip(";")


def _render_native_ast(ast: exp.Expression, target_dialect: str) -> str | None:
    """Generate executable SQL from the already resolved AST.

    Automatic resolution can select an engine from only one side of the SQL
    pair. Rendering both ASTs here normalizes shared syntax such as LIMIT and
    FETCH to that same native engine before any fixture or mutation executes.
    """
    render_ast = ast.copy()
    if target_dialect == "tsql":
        # SQLGlot renders a generic FETCH ... WITH TIES as OFFSET/FETCH, but
        # SQL Server exposes WITH TIES only through TOP. Convert a zero-offset
        # FETCH into the equivalent TOP-shaped Limit before rendering. An
        # offset combined with WITH TIES has no direct T-SQL representation.
        for fetch in list(render_ast.find_all(exp.Fetch)):
            options = fetch.args.get("limit_options")
            if not (
                isinstance(options, exp.LimitOptions)
                and options.args.get("with_ties")
            ):
                continue
            parent_query = fetch.find_ancestor(exp.Query)
            if isinstance(parent_query, exp.Query) and parent_query.args.get("offset"):
                return None
            count = fetch.args.get("count")
            if not isinstance(count, exp.Expression):
                return None
            fetch.replace(
                exp.Limit(
                    expression=count.copy(),
                    limit_options=exp.LimitOptions(
                        percent=bool(options.args.get("percent")),
                        rows=False,
                        with_ties=True,
                    ),
                )
            )
    try:
        rendered = render_ast.sql(
            dialect=target_dialect,
            unsupported_level=ErrorLevel.RAISE,
        )
    except Exception:
        return None
    return _prepare_native_sql(rendered) if rendered.strip() else None


def _detect_unsupported_features(
    backend: str,
    target_dialect: str,
    *sql_items: str,
) -> list[str]:
    combined = "\n".join(item for item in sql_items if item)
    if (
        target_dialect == "tsql"
        and re.search(r"(?is)\bSELECT\s+@[A-Z_][A-Z0-9_$]*\s*=", combined)
    ):
        # SELECT-assignment mutates SQL Server session state and normally has
        # no result set. The current comparison contract observes rows only.
        return ["TSQL_VARIABLE_ASSIGNMENT_UNOBSERVABLE"]
    if backend == "sqlite":
        return _detect_sqlite_unsupported_features(
            *sql_items,
            target_dialect=target_dialect,
        )
    if backend in {"mysql", "postgres", "tsql", "oracle"}:
        if target_dialect != backend:
            return [
                f"DIALECT_BACKEND_MISMATCH_{target_dialect.upper()}_TO_{backend.upper()}"
            ]
    if backend == "mysql":
        return _detect_mysql_unsupported_features(*sql_items)
    if backend in {"postgres", "tsql", "oracle"}:
        return []
    return [f"UNKNOWN_BACKEND_{backend.upper()}"]


def _detect_sqlite_unsupported_features(
    *sql_items: str,
    target_dialect: str | None = None,
) -> list[str]:
    """Return dialect features that should not be judged through SQLite."""
    checks: tuple[tuple[str, str], ...] = (
        (
            "LIMIT_WITH_TIES",
            r"(?is)(?:\bTOP\s*(?:\([^)]*\)|\d+)\s+WITH\s+TIES\b|"
            r"\bFETCH\s+(?:FIRST|NEXT)\s+[^;]*?\s+WITH\s+TIES\b)",
        ),
        (
            "LIMIT_PERCENT",
            r"(?is)(?:\bTOP\s*(?:\([^)]*\)|\d+)\s+PERCENT\b|"
            r"\bFETCH\s+(?:FIRST|NEXT)\s+[^;]*?\s+PERCENT\b)",
        ),
        (
            "MYSQL_GROUP_CONCAT_ORDERING",
            r"(?is)\bGROUP_CONCAT\s*\([^)]*\bORDER\s+BY\b",
        ),
        (
            "MYSQL_GROUP_CONCAT_SEPARATOR",
            r"(?is)\bGROUP_CONCAT\s*\([^)]*\bSEPARATOR\b",
        ),
        ("ORACLE_ROWNUM", r"(?is)\bROWNUM\b"),
        ("ORACLE_CONNECT_BY", r"(?is)\bCONNECT\s+BY\b"),
        ("ORACLE_START_WITH", r"(?is)\bSTART\s+WITH\b"),
        ("ORACLE_LISTAGG", r"(?is)\bLISTAGG\s*\("),
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
        ("POSTGRES_JSON_TABLE_FUNCTION", r"(?is)\bjsonb?_array_elements(?:_text)?\s*\("),
    )
    found: list[str] = []
    seen: set[str] = set()
    combined = "\n".join(item for item in sql_items if item)
    for feature, pattern in checks:
        if feature not in seen and re.search(pattern, combined):
            found.append(feature)
            seen.add(feature)
    if (
        target_dialect == "oracle"
        and re.search(r"(?is)(?:^|[^'])''(?:[^']|$)", combined)
        and "ORACLE_EMPTY_STRING_IS_NULL" not in seen
    ):
        # Oracle treats the empty character literal as NULL; SQLite does not.
        found.append("ORACLE_EMPTY_STRING_IS_NULL")
    return found


def _detect_mysql_unsupported_features(*sql_items: str) -> list[str]:
    checks: tuple[tuple[str, str], ...] = (
        ("PIVOT", r"(?is)\bPIVOT\b"),
        ("UNPIVOT", r"(?is)\bUNPIVOT\b"),
        ("APPLY", r"(?is)\b(?:CROSS|OUTER)\s+APPLY\b"),
        ("INTERSECT_ALL", r"(?is)\bINTERSECT\s+ALL\b"),
        ("EXCEPT_ALL", r"(?is)\bEXCEPT\s+ALL\b"),
        ("POSTGRES_SEARCH", r"(?is)\bSEARCH\s+(?:DEPTH|BREADTH)\s+FIRST\b"),
        ("POSTGRES_CYCLE", r"(?is)\bCYCLE\b.+\bUSING\b"),
        ("POSTGRES_DISTINCT_ON", r"(?is)\bDISTINCT\s+ON\s*\("),
        ("POSTGRES_JSON_TABLE_FUNCTION", r"(?is)\bjsonb?_array_elements(?:_text)?\s*\("),
        ("TSQL_TOP_WITH_TIES", r"(?is)\bTOP\s*\(?\s*\d+\s*\)?\s+WITH\s+TIES\b"),
        (
            "STANDARD_FETCH_WITH_TIES",
            r"(?is)\bFETCH\s+(?:FIRST|NEXT)\s+[^;]*?\s+WITH\s+TIES\b",
        ),
        ("FULL_OUTER_JOIN", r"(?is)\bFULL(?:\s+OUTER)?\s+JOIN\b"),
        ("GROUPING_SETS", r"(?is)\bGROUPING\s+SETS\s*\("),
        ("CUBE", r"(?is)\bCUBE\s*\("),
    )
    found: list[str] = []
    seen: set[str] = set()
    combined = "\n".join(item for item in sql_items if item)
    for feature, pattern in checks:
        if feature not in seen and re.search(pattern, combined):
            found.append(feature)
            seen.add(feature)
    return found


def _is_likely_backend_capability_error(backend: str, error: str, sql: str | None) -> bool:
    if backend == "mysql":
        return _is_likely_mysql_capability_error(error, sql)
    if backend == "sqlite":
        return _is_likely_sqlite_capability_error(error, sql)
    # Native PostgreSQL, SQL Server, and Oracle already receive SQL rendered
    # for their own dialect. Do not reuse SQLite feature heuristics here: a
    # connection or permission failure on a valid native construct such as
    # LATERAL must remain an ENGINE_ERROR, not become UNSUPPORTED.
    return False


def _is_platform_execution_error(backend: str, exc: Exception) -> bool:
    """Return whether a student-side exception means no verdict is possible."""
    if _is_execution_timeout(exc):
        return True
    if backend in {"mysql", "postgres", "tsql", "oracle"}:
        return not isinstance(exc, NativeQueryExecutionError)
    return False


def _is_execution_timeout(exc: BaseException) -> bool:
    current: BaseException | None = exc
    while current is not None:
        text = str(current).lower()
        if any(
            marker in text
            for marker in (
                "statement timeout",
                "query timeout",
                "execution timeout",
                "timeout expired",
                "timed out",
                "ora-01013",
                "canceling statement due to statement timeout",
            )
        ):
            return True
        current = current.__cause__
    return False


def _is_likely_sqlite_capability_error(error: str, sql: str | None) -> bool:
    unsupported = _detect_sqlite_unsupported_features(sql or "")
    if unsupported:
        return True
    return bool(
        re.search(r"(?is)\bnear\s+\"?(?:all|lateral|pivot|unpivot|rollup|cube)\"?:\s+syntax error", error)
        or re.search(r"(?is)\bno such function:\s+(?:rollup|cube|grouping|jsonb?_array_elements)", error)
    )


def _is_likely_mysql_capability_error(error: str, sql: str | None) -> bool:
    unsupported = _detect_mysql_unsupported_features(sql or "")
    if unsupported:
        return True
    return bool(re.search(r"(?is)\bsyntax\b|\bnot supported\b", error))


def _parse_sql(sql: str, dialect: str | None = None) -> exp.Expression | None:
    if dialect is None and _STRUCTURE_PARSE_DIALECT.get():
        dialect = _STRUCTURE_PARSE_DIALECT.get()
    dialects = (dialect,) if dialect else _dialect_candidates(sql)
    for candidate in dialects:
        try:
            read_dialect = (
                None
                if candidate in {GENERIC_SQLGLOT_DIALECT, STANDARD_SQL_DIALECT}
                else candidate
            )
            parsed = sqlglot.parse_one(sql, dialect=read_dialect, error_level=ErrorLevel.IGNORE)
            if parsed is not None:
                # Guard against silent mis-parse.  sqlglot is very lenient
                # and may re-interpret keywords (e.g. "SELECT * FORM orders"
                # becomes "SELECT * AS FORM", silently dropping "orders").
                # Heuristic: extract identifier-like words from the original
                # SQL and verify the round-tripped SQL preserves them.
                raw_tokens = set(re.findall(r'\b[A-Za-z_]\w*\b', sql))
                # Exclude SQL keywords that sqlglot may normalise away
                _KW = {'SELECT', 'FROM', 'WHERE', 'AND', 'OR', 'NOT', 'AS',
                       'ON', 'IN', 'IS', 'NULL', 'LIKE', 'BETWEEN', 'JOIN',
                       'LEFT', 'RIGHT', 'INNER', 'OUTER', 'CROSS', 'GROUP',
                       'BY', 'ORDER', 'HAVING', 'LIMIT', 'OFFSET', 'UNION',
                       'ALL', 'DISTINCT', 'EXISTS', 'CASE', 'WHEN', 'THEN',
                       'ELSE', 'END', 'INSERT', 'INTO', 'VALUES', 'UPDATE',
                       'SET', 'DELETE', 'CREATE', 'TABLE', 'DROP', 'ALTER',
                       'INDEX', 'WITH', 'RECURSIVE', 'ASC', 'DESC', 'TRUE',
                       'FALSE', 'CAST', 'INTERSECT', 'EXCEPT', 'IF', 'THEN',
                       'INTEGER', 'INT', 'BIGINT', 'SMALLINT', 'DECIMAL',
                       'NUMERIC', 'VARCHAR', 'CHAR', 'TEXT', 'DATE', 'TIMESTAMP',
                       'BOOLEAN', 'REAL', 'FLOAT', 'DOUBLE',
                       'NULLS', 'FIRST', 'LAST', 'QUALIFY', 'WINDOW', 'ROWS',
                       'RANGE', 'PRECEDING', 'FOLLOWING', 'CURRENT', 'ROW',
                       'REGEXP', 'RLIKE', 'SIMILAR', 'TO', 'GLOB', 'ILIKE',
                       'DAY', 'WEEK', 'MONTH', 'QUARTER', 'YEAR', 'HOUR',
                       'MINUTE', 'SECOND', 'MILLISECOND', 'MICROSECOND'}
                # SQL function names and unquoted identifiers are
                # case-insensitive in every dialect accepted by this guarded
                # parser. sqlglot canonicalizes built-ins (``count`` becomes
                # ``COUNT``), so comparing the original token spelling made a
                # valid round trip look as if it had dropped an identifier.
                # Keep the disappearance guard, but compare its token set in
                # a case-insensitive form. A real silent loss such as the
                # table name in ``SELECT * FORM orders`` is still detected.
                meaningful = {
                    t.casefold()
                    for t in raw_tokens
                    if t.upper() not in _KW
                }
                if meaningful:
                    roundtrip = parsed.sql(dialect=read_dialect)
                    rt_tokens = {
                        t.casefold()
                        for t in re.findall(r'\b[A-Za-z_]\w*\b', roundtrip)
                    }
                    lost = meaningful - rt_tokens
                    # If any meaningful identifier vanished, the parse
                    # likely mis-interpreted the query.
                    if lost:
                        continue
                return parsed
        except Exception:
            continue
    return None


def _parse_sql_strict(sql: str, dialect: str | None = None) -> exp.Expression | None:
    """Parse exactly one complete statement without sqlglot recovery."""
    if dialect is None and _STRUCTURE_PARSE_DIALECT.get():
        dialect = _STRUCTURE_PARSE_DIALECT.get()
    dialects = (dialect,) if dialect else _dialect_candidates(sql)
    for candidate in dialects:
        try:
            read_dialect = (
                None
                if candidate in {GENERIC_SQLGLOT_DIALECT, STANDARD_SQL_DIALECT}
                else candidate
            )
            statements = sqlglot.parse(sql, dialect=read_dialect, error_level=ErrorLevel.RAISE)
            parsed = [
                statement for statement in statements
                if statement is not None and not isinstance(statement, exp.Semicolon)
            ]
            if len(parsed) == 1 and isinstance(parsed[0], exp.Query):
                return parsed[0]
        except Exception:
            continue
    return None


def _dialect_candidates(sql: str) -> tuple[str, ...]:
    if "`" in sql:
        return ("mysql", "sqlite", "postgres", "tsql", "oracle")
    if re.search(r"(?is)\bSELECT\s+TOP\b|\[[^\]]+\]", sql):
        return ("tsql", "sqlite", "mysql", "postgres", "oracle")
    if re.search(r"(?is)\bDISTINCT\s+ON\s*\(|::\s*[A-Za-z_]", sql):
        return ("postgres", "sqlite", "mysql", "tsql", "oracle")
    return ("sqlite", "mysql", "postgres", "tsql", "oracle")


def _has_node(ast: exp.Expression | None, node_type: type[exp.Expression]) -> bool:
    return bool(ast and ast.find(node_type))


def _collect_subqueries(ast: exp.Expression) -> list[exp.Expression]:
    """Extract all subquery inner SELECT nodes from an AST (not the top-level SELECT).

    Covers: Subquery nodes (scalar, IN, FROM, WHERE) and Exists nodes.
    Returns the inner Select of each subquery, in traversal order.
    """
    result: list[exp.Expression] = []
    for node in ast.find_all(exp.Subquery):
        inner = node.this
        if isinstance(inner, exp.Select):
            result.append(inner)
    for node in ast.find_all(exp.Exists):
        inner = node.this
        if isinstance(inner, exp.Select):
            result.append(inner)
    return result


def _subquery_is_correlated(node: exp.Expression) -> bool:
    inner_tables = {str(t.name).lower().strip('"`[]') for t in node.find_all(exp.Table)}
    for table in node.find_all(exp.Table):
        if table.alias:
            inner_tables.add(str(table.alias).lower().strip('"`[]'))
    for col in node.find_all(exp.Column):
        if col.table:
            table_ref = str(col.table).lower().strip('"`[]')
            if table_ref not in inner_tables:
                return True
    return False


def _is_inside_subquery(node: exp.Expression) -> bool:
    """Return True if *node* is a descendant of a Subquery or Exists node."""
    p = node.parent
    while p is not None:
        if isinstance(p, (exp.Subquery, exp.Exists)):
            return True
        p = p.parent
    return False


def _is_inside_join(node: exp.Expression) -> bool:
    p = node.parent
    while p is not None:
        if isinstance(p, exp.Join):
            return True
        p = p.parent
    return False


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

    diffs: list[ASTDiffNode] = []
    paired = min(len(std_subs), len(stu_subs))

    # Recursively diff each paired subquery
    for i in range(paired):
        inner_diffs = _diff_inner(std_subs[i], stu_subs[i], depth=depth)
        if inner_diffs and (_subquery_is_correlated(std_subs[i]) or _subquery_is_correlated(stu_subs[i])):
            diffs.append(ASTDiffNode(
                clause_category="CORRELATED SUBQUERY",
                diff_type="correlated_predicate_changed",
                standard_node=std_subs[i],
                student_node=stu_subs[i],
                knowledge_point_id="subquery-correlated",
                severity=0.78,
                extra={
                    "subquery_depth": depth,
                    "standard_sql": _sql_of(std_subs[i]),
                    "student_sql": _sql_of(stu_subs[i]),
                },
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

    # Recurse one level deeper for nested subqueries inside this subquery
    inner_diffs.extend(_subquery_ast_diffs(std_inner, stu_inner, depth=depth + 1))

    return inner_diffs


def _predicate_sql_key(node: exp.Expression) -> str:
    return re.sub(r"\s+", " ", _sql_of(node).strip()).lower()


def _predicate_leaf_map(node: exp.Expression | None) -> dict[str, exp.Expression]:
    if node is None:
        return {}
    body = node.this if isinstance(node, exp.Where) else node
    return {
        _predicate_sql_key(item): item
        for item in _flatten_and(body)
    }


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


def extract_ast_diffs(
    standard_sql: str,
    student_sql: str,
    dialect: str | None = None,
    schema_catalog: SchemaCatalog | None = None,
) -> list[ASTDiffNode]:
    """Extract focused AST subtree differences used to drive counterexample data generation."""
    standard_ast = _parse_sql(standard_sql, dialect=dialect)
    student_ast = _parse_sql(student_sql, dialect=dialect)
    if standard_ast is None or student_ast is None:
        return []

    if (
        _queries_are_supported_equivalent_rewrites(
            standard_ast,
            student_ast,
            schema_catalog=schema_catalog,
        )
        or schema_catalog is not None
        and _schema_numeric_projection_identities_equivalent(
            standard_ast,
            student_ast,
            schema_catalog,
        )
    ):
        return []

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
    return unique


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
        _in_list_or_equivalent(standard_ast, student_ast),
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
        _null_safe_equality_filter_equivalent(standard_ast, student_ast),
        _where_boolean_absorption_equivalent(standard_ast, student_ast),
        _between_closed_range_equivalent(standard_ast, student_ast),
        _global_extreme_comparison_equivalent(standard_ast, student_ast),
        _simple_cte_inline_equivalent(standard_ast, student_ast),
        _simple_cte_inline_equivalent(student_ast, standard_ast),
        _simple_in_join_equivalent(standard_ast, student_ast),
        _simple_in_join_equivalent(student_ast, standard_ast),
        _simple_not_exists_antijoin_equivalent(standard_ast, student_ast),
        _simple_not_exists_antijoin_equivalent(student_ast, standard_ast),
        _commutative_set_branch_permutation_equivalent(
            standard_ast,
            student_ast,
        ),
    ))


def _simple_join_using_on_equivalent(
    using_ast: exp.Expression,
    on_ast: exp.Expression,
    *,
    schema_catalog: SchemaCatalog | None = None,
) -> bool:
    """Recognize safe ``USING``/``NATURAL`` to explicit ``ON`` rewrites.

    ``USING`` and ``NATURAL`` also change the shape of ``SELECT *`` by
    coalescing duplicate key columns.  Restrict this fast path to explicit
    projections that do not observe those keys, where both forms have the
    same inner-join row semantics.
    """
    if (
        _set_operator_signature(using_ast) != _set_operator_signature(on_ast)
        or _window_signature(using_ast) != _window_signature(on_ast)
        or _outer_distinct_signature(using_ast) != _outer_distinct_signature(on_ast)
        or list(using_ast.find_all(exp.CTE))
        or list(on_ast.find_all(exp.CTE))
    ):
        return False
    using_select = _top_select(using_ast)
    on_select = _top_select(on_ast)
    if not isinstance(using_select, exp.Select) or not isinstance(on_select, exp.Select):
        return False
    using_joins = list(using_select.args.get("joins") or [])
    on_joins = list(on_select.args.get("joins") or [])
    if len(using_joins) != 1 or len(on_joins) != 1:
        return False
    using_join, on_join = using_joins[0], on_joins[0]
    if not isinstance(using_join, exp.Join) or not isinstance(on_join, exp.Join):
        return False
    if str(using_join.args.get("side") or using_join.args.get("kind") or "INNER").upper() not in {"", "INNER"}:
        return False
    if str(on_join.args.get("side") or on_join.args.get("kind") or "INNER").upper() not in {"", "INNER"}:
        return False
    if not isinstance(using_join.this, exp.Table) or not isinstance(on_join.this, exp.Table):
        return False
    using_from = _direct_from_table(using_select)
    on_from = _direct_from_table(on_select)
    if not using_from or not on_from:
        return False
    if _norm_name(using_from.name) != _norm_name(on_from.name):
        return False
    if _norm_name(using_join.this.name) != _norm_name(on_join.this.name):
        return False

    using_columns = [
        _norm_name(item.name)
        for item in (using_join.args.get("using") or [])
        if isinstance(item, exp.Identifier) and item.name
    ]
    is_natural = str(using_join.args.get("method") or "").upper() == "NATURAL"
    if not using_columns and not is_natural:
        return False
    if using_columns and is_natural:
        return False

    left_refs = {
        _norm_name(using_from.name),
        _norm_name(using_from.alias_or_name),
        _norm_name(on_from.name),
        _norm_name(on_from.alias_or_name),
    }
    right_refs = {
        _norm_name(using_join.this.name),
        _norm_name(using_join.this.alias_or_name),
        _norm_name(on_join.this.name),
        _norm_name(on_join.this.alias_or_name),
    }
    on_condition = on_join.args.get("on")
    if not isinstance(on_condition, exp.Expression):
        return False
    on_pairs: set[tuple[str, str]] = set()
    for predicate in _flatten_and(on_condition):
        if not isinstance(predicate, exp.EQ):
            return False
        columns = [predicate.left, predicate.right]
        if not all(isinstance(column, exp.Column) for column in columns):
            return False
        left_column, right_column = columns
        left_table = _norm_name(left_column.table)
        right_table = _norm_name(right_column.table)
        if left_table in left_refs and right_table in right_refs:
            pair = (left_column.name, right_column.name)
        elif right_table in left_refs and left_table in right_refs:
            pair = (right_column.name, left_column.name)
        else:
            return False
        on_pairs.add(tuple(_norm_name(item) for item in pair))
    if is_natural:
        left_schema = schema_catalog.table(using_from.name) if schema_catalog else None
        right_schema = schema_catalog.table(using_join.this.name) if schema_catalog else None
        if left_schema and right_schema:
            expected_columns = sorted(
                set(left_schema.columns) & set(right_schema.columns)
            )
        else:
            # Without a catalog, use the explicit ON pair as the bounded
            # fallback. A catalog-backed run remains authoritative for the
            # full NATURAL common-column set.
            expected_columns = sorted({left for left, right in on_pairs if left == right})
        expected_pairs = {(column, column) for column in expected_columns}
    else:
        expected_pairs = {(column, column) for column in using_columns}
    if not expected_pairs or on_pairs != expected_pairs:
        return False

    for select in (using_select, on_select):
        for expression in select.expressions or []:
            if expression.find(exp.Star):
                return False
            if any(
                _norm_name(column.name) in {item[0] for item in expected_pairs}
                for column in expression.find_all(exp.Column)
            ):
                return False
    if _select_projection_repr(using_ast) != _select_projection_repr(on_ast):
        return False
    for key in ("where", "group", "having", "order", "limit", "offset", "qualify"):
        if _unqualified_sql(using_select.args.get(key)) != _unqualified_sql(on_select.args.get(key)):
            return False
    return True


def _double_negation_equivalent(
    standard_ast: exp.Expression,
    student_ast: exp.Expression,
) -> bool:
    """Recognize queries that differ only by pairs of Boolean ``NOT`` nodes.

    SQL three-valued logic preserves double negation, including UNKNOWN:
    ``NOT NOT UNKNOWN`` is still UNKNOWN.  Normalize the complete AST rather
    than only the outer WHERE so an unrelated change in another query block
    cannot be hidden by this equivalence rule.
    """

    def normalized(ast: exp.Expression) -> tuple[str, bool]:
        copied = ast.copy()
        changed = False
        while True:
            removed_pair = False
            for node in list(copied.find_all(exp.Not)):
                inner = _unwrap_paren(node.this)
                if not isinstance(inner, exp.Not) or inner.this is None:
                    continue
                node.replace(_unwrap_paren(inner.this).copy())
                changed = True
                removed_pair = True
                break
            if not removed_pair:
                break
        return _sql_of(copied), changed

    standard = normalized(standard_ast)
    student = normalized(student_ast)
    return bool((standard[1] or student[1]) and standard[0] == student[0])


def _nullif_coalesce_case_equivalent(
    standard_ast: exp.Expression,
    student_ast: exp.Expression,
) -> bool:
    """Normalize two exact, common function-to-CASE rewrites.

    Only the canonical one-WHEN forms are accepted.  Requiring the CASE
    default to repeat the input expression prevents this fast path from
    hiding a changed ELSE branch, and limiting COALESCE to two arguments
    avoids claiming equivalence for a different fallback chain.
    """

    def case_replacement(node: exp.Case) -> exp.Expression | None:
        if node.args.get("this") is not None:
            return None
        if len(node.args.get("ifs") or []) != 1:
            return None
        branch = (node.args.get("ifs") or [None])[0]
        default = node.args.get("default")
        if not isinstance(branch, exp.If) or not isinstance(default, exp.Expression):
            return None
        condition = branch.args.get("this")
        true_value = branch.args.get("true")
        if not isinstance(condition, exp.Expression) or not isinstance(true_value, exp.Expression):
            return None
        if isinstance(condition, exp.EQ) and isinstance(true_value, exp.Null):
            left, right = condition.left, condition.right
            if _sql_of(left) == _sql_of(default):
                return exp.Nullif(this=default.copy(), expression=right.copy())
            if _sql_of(right) == _sql_of(default):
                return exp.Nullif(this=default.copy(), expression=left.copy())
        if isinstance(condition, exp.Is) and isinstance(condition.expression, exp.Null):
            if _sql_of(condition.this) != _sql_of(default):
                return None
            return exp.Coalesce(
                this=default.copy(),
                expressions=[true_value.copy()],
            )
        return None

    def normalized(ast: exp.Expression) -> tuple[str, bool]:
        copied = ast.copy()
        changed = False
        for node in list(copied.find_all(exp.Case)):
            replacement = case_replacement(node)
            if replacement is None:
                continue
            node.replace(replacement)
            changed = True
        return _sql_of(copied), changed

    standard = normalized(standard_ast)
    student = normalized(student_ast)
    return bool((standard[1] or student[1]) and standard[0] == student[0])


def _order_reference_equivalent(
    standard_ast: exp.Expression,
    student_ast: exp.Expression,
    *,
    schema_catalog: SchemaCatalog | None = None,
) -> bool:
    """Resolve safe ORDER BY ordinals and output aliases to projections."""

    def deterministic(expression: exp.Expression) -> bool:
        return not any(
            expression.find(node_type)
            for node_type in (exp.Func, exp.Subquery, exp.Window)
        )

    def normalized(ast: exp.Expression) -> tuple[str, bool]:
        copied = ast.copy()
        select = _top_select(copied)
        order = _result_order_clause(copied)
        if not isinstance(select, exp.Select) or not isinstance(order, exp.Order):
            return _sql_of(copied), False
        projections = list(select.expressions or [])
        aliases: dict[str, list[exp.Expression]] = defaultdict(list)
        for projection in projections:
            if isinstance(projection, exp.Alias) and projection.alias:
                aliases[_norm_name(projection.alias)].append(projection.this)
        source = _direct_from_table(select)
        source_columns: set[str] = set()
        if schema_catalog is not None and isinstance(source, exp.Table):
            table_schema = schema_catalog.table(source.name)
            if table_schema is not None:
                source_columns = {_norm_name(name) for name in table_schema.columns}

        changed = False
        for item in order.expressions or []:
            expression = item.this if isinstance(item, exp.Ordered) else item
            replacement: exp.Expression | None = None
            if isinstance(expression, exp.Literal) and not expression.is_string:
                try:
                    position = int(str(expression.this))
                except (TypeError, ValueError):
                    position = 0
                if str(position) == str(expression.this) and 1 <= position <= len(projections):
                    projected = projections[position - 1]
                    projected = projected.this if isinstance(projected, exp.Alias) else projected
                    if deterministic(projected):
                        replacement = projected
            elif isinstance(expression, exp.Column) and not expression.table:
                alias = _norm_name(expression.name)
                candidates = aliases.get(alias, [])
                if (
                    len(candidates) == 1
                    and alias not in source_columns
                    and deterministic(candidates[0])
                ):
                    replacement = candidates[0]
            if replacement is None:
                continue
            if isinstance(item, exp.Ordered):
                item.set("this", replacement.copy())
            else:
                item.replace(replacement.copy())
            changed = True
        return _sql_of(copied), changed

    standard = normalized(standard_ast)
    student = normalized(student_ast)
    return bool((standard[1] or student[1]) and standard[0] == student[0])


def _simple_searched_case_equivalent(
    standard_ast: exp.Expression,
    student_ast: exp.Expression,
) -> bool:
    """Convert deterministic simple CASE branches to searched equality CASE."""

    def normalized(ast: exp.Expression) -> tuple[str, bool]:
        copied = ast.copy()
        changed = False
        for node in list(copied.find_all(exp.Case)):
            operand = node.args.get("this")
            branches = list(node.args.get("ifs") or [])
            if not isinstance(operand, exp.Column) or not branches:
                continue
            if not all(
                isinstance(branch, exp.If)
                and isinstance(branch.args.get("this"), (exp.Literal, exp.Null))
                and isinstance(branch.args.get("true"), exp.Expression)
                for branch in branches
            ):
                continue
            searched_branches = [
                exp.If(
                    this=exp.EQ(
                        this=operand.copy(),
                        expression=branch.args["this"].copy(),
                    ),
                    true=branch.args["true"].copy(),
                )
                for branch in branches
            ]
            default = node.args.get("default")
            node.replace(exp.Case(
                ifs=searched_branches,
                default=(
                    default.copy()
                    if isinstance(default, exp.Expression)
                    else None
                ),
            ))
            changed = True
        return _sql_of(copied), changed

    standard = normalized(standard_ast)
    student = normalized(student_ast)
    return bool((standard[1] or student[1]) and standard[0] == student[0])


def _in_list_or_equivalent(
    standard_ast: exp.Expression,
    student_ast: exp.Expression,
) -> bool:
    """Canonicalize finite literal ``IN`` lists and equivalent OR chains."""

    def flatten_or(node: exp.Expression) -> list[exp.Expression]:
        node = _unwrap_paren(node)
        if isinstance(node, exp.Or):
            return flatten_or(node.left) + flatten_or(node.right)
        return [node]

    def normalized(ast: exp.Expression) -> tuple[str, bool]:
        copied = ast.copy()
        changed = False
        # Convert an OR chain of equalities on one expression to an IN list.
        # Process the outer OR first. Converting a child pair to IN before its
        # parent would leave the larger chain as ``IN (...) OR x = ...``.
        for node in list(copied.find_all(exp.Or)):
            terms = flatten_or(node)
            if len(terms) < 2:
                continue
            if not all(
                isinstance(term, exp.EQ)
                and isinstance(term.right, (exp.Literal, exp.Null))
                for term in terms
            ):
                continue
            left_sql = _sql_of(terms[0].left)
            if any(_sql_of(term.left) != left_sql for term in terms[1:]):
                continue
            values: list[exp.Expression] = []
            seen_values: set[str] = set()
            for term in terms:
                value = term.right.copy()
                value_sql = _sql_of(value)
                if value_sql in seen_values:
                    continue
                seen_values.add(value_sql)
                values.append(value)
            values.sort(key=_sql_of)
            node.replace(exp.In(this=terms[0].left.copy(), expressions=values))
            changed = True

        # Sort and deduplicate literal IN lists so ordering and duplicate
        # spelling do not create a false structural difference.
        for node in list(copied.find_all(exp.In)):
            if node.args.get("query") is not None:
                continue
            items = list(node.expressions or [])
            if not items or not all(isinstance(item, (exp.Literal, exp.Null)) for item in items):
                continue
            unique: dict[str, exp.Expression] = {
                _sql_of(item): item.copy()
                for item in items
            }
            canonical_items = [unique[key] for key in sorted(unique)]
            if [_sql_of(item) for item in items] != [_sql_of(item) for item in canonical_items]:
                changed = True
            node.set("expressions", canonical_items)
        return _sql_of(copied), changed

    standard = normalized(standard_ast)
    student = normalized(student_ast)
    return bool((standard[1] or student[1]) and standard[0] == student[0])


def _is_true_filter_equivalent(
    standard_ast: exp.Expression,
    student_ast: exp.Expression,
) -> bool:
    """Normalize ``predicate IS TRUE`` only in a filtering context.

    A WHERE clause keeps rows whose condition is TRUE, so testing that same
    condition with ``IS TRUE`` is equivalent even when it evaluates UNKNOWN.
    The projection case is intentionally excluded because ``NULL IS TRUE``
    yields FALSE while the bare predicate yields NULL.
    """
    filter_types = tuple(
        item
        for item in (exp.Where, exp.Having, getattr(exp, "Qualify", None))
        if isinstance(item, type)
    )

    def normalized(ast: exp.Expression) -> tuple[str, bool]:
        copied = ast.copy()
        changed = False
        for node in list(copied.find_all(exp.Is)):
            expression = node.args.get("expression")
            if not isinstance(expression, exp.Boolean) or expression.this is not True:
                continue
            if not node.find_ancestor(*filter_types):
                continue
            predicate = _unwrap_paren(node.this)
            if not isinstance(predicate, exp.Expression):
                continue
            node.replace(predicate.copy())
            changed = True
        return _sql_of(copied), changed

    standard = normalized(standard_ast)
    student = normalized(student_ast)
    return bool((standard[1] or student[1]) and standard[0] == student[0])


def _null_safe_equality_filter_equivalent(
    standard_ast: exp.Expression,
    student_ast: exp.Expression,
) -> bool:
    """Normalize the safe subset of NULL-safe equality rewrites.

    With a non-NULL constant, ``x IS NOT DISTINCT FROM c`` and ``x = c``
    select the same rows in WHERE/HAVING/QUALIFY.  Their projected Boolean
    values still differ for ``x IS NULL``, so that rewrite is filter-only.
    Comparisons against NULL itself are exact two-valued rewrites to
    ``IS NULL``/``IS NOT NULL`` and are safe in every expression context.
    """
    filter_types = tuple(
        item
        for item in (exp.Where, exp.Having, getattr(exp, "Qualify", None))
        if isinstance(item, type)
    )

    def normalized(ast: exp.Expression) -> tuple[str, bool]:
        copied = ast.copy()
        changed = False
        for node in list(copied.find_all(exp.NullSafeEQ, exp.NullSafeNEQ)):
            left_value = _literal_value(node.left)
            right_value = _literal_value(node.right)
            left_is_null = isinstance(node.left, exp.Null)
            right_is_null = isinstance(node.right, exp.Null)
            if left_is_null or right_is_null:
                subject = node.right if left_is_null else node.left
                replacement: exp.Expression = exp.Is(
                    this=subject.copy(),
                    expression=exp.Null(),
                )
                if isinstance(node, exp.NullSafeNEQ):
                    replacement = exp.Not(this=replacement)
                node.replace(replacement)
                changed = True
                continue
            if not isinstance(node, exp.NullSafeEQ):
                continue
            if not node.find_ancestor(*filter_types):
                continue
            has_non_null_literal = (
                isinstance(node.left, exp.Literal) and left_value is not None
                or isinstance(node.right, exp.Literal) and right_value is not None
            )
            if not has_non_null_literal:
                continue
            node.replace(exp.EQ(this=node.left.copy(), expression=node.right.copy()))
            changed = True
        return _sql_of(copied), changed

    standard = normalized(standard_ast)
    student = normalized(student_ast)
    return bool(
        (standard[1] or student[1])
        and standard[0] == student[0]
    )


def _aggregate_filter_is_only_projection_difference(
    standard_ast: exp.Expression,
    student_ast: exp.Expression,
) -> bool:
    """Check whether removing top-level FILTER wrappers makes queries equal."""
    standard_select = _top_select(standard_ast)
    student_select = _top_select(student_ast)
    if not isinstance(standard_select, exp.Select) or not isinstance(
        student_select, exp.Select
    ):
        return False

    def without_filters(ast: exp.Expression) -> str:
        copied = ast.copy()
        select = _top_select(copied)
        if not isinstance(select, exp.Select):
            return ""
        for node in list(select.find_all(exp.Filter)):
            owner = node.find_ancestor(exp.Select)
            if owner is select and isinstance(node.this, exp.Expression):
                node.replace(node.this.copy())
        return _sql_of(copied)

    return without_filters(standard_ast) == without_filters(student_ast)


def _unreferenced_output_aliases_equivalent(
    standard_ast: exp.Expression,
    student_ast: exp.Expression,
) -> bool:
    """Ignore only unreferenced aliases on the result-producing SELECT.

    Output labels are not part of Phase 1 row-value equivalence.  Aliases in
    CTEs and derived tables remain significant because they define columns
    visible to outer query blocks.  A top-level alias is also significant
    when another clause in the same block refers to it, so those cases are
    conservatively excluded from this rewrite.
    """
    if not isinstance(standard_ast, exp.Select) or not isinstance(
        student_ast, exp.Select
    ):
        return False

    def normalized(ast: exp.Select) -> tuple[str, bool] | None:
        copied = ast.copy()
        aliases = {
            _norm_name(item.alias)
            for item in copied.expressions
            if isinstance(item, exp.Alias) and item.alias
        }
        if not aliases:
            return _sql_of(copied), False

        # Projection aliases may be used by later clauses in the same SELECT.
        # Do not inspect nested SELECTs: their columns belong to another scope.
        for key, clause in copied.args.items():
            if key in {"expressions", "with", "with_"} or clause is None:
                continue
            nodes = clause if isinstance(clause, list) else [clause]
            for node in nodes:
                if not isinstance(node, exp.Expression):
                    continue
                for column in node.find_all(exp.Column):
                    if column.find_ancestor(exp.Select) is not copied:
                        continue
                    if not column.table and _norm_name(column.name) in aliases:
                        return None

        copied.set(
            "expressions",
            [
                item.this.copy() if isinstance(item, exp.Alias) else item.copy()
                for item in copied.expressions
            ],
        )
        return _sql_of(copied), True

    standard = normalized(standard_ast)
    student = normalized(student_ast)
    return bool(
        standard is not None
        and student is not None
        and (standard[1] or student[1])
        and standard[0] == student[0]
    )


def _schema_numeric_projection_identities_equivalent(
    standard_ast: exp.Expression,
    student_ast: exp.Expression,
    schema_catalog: SchemaCatalog,
) -> bool:
    """Recognize simple arithmetic identities only for numeric columns.

    Dialects such as MySQL coerce text in arithmetic expressions, so an AST
    rule that blindly rewrites ``column + 0`` to ``column`` is unsound.  This
    rule resolves the projected column through the supplied physical schema
    and declines explicitly non-numeric columns.
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


def _between_closed_range_equivalent(
    standard_ast: exp.Expression,
    student_ast: exp.Expression,
) -> bool:
    """Recognize positive BETWEEN as its two inclusive comparisons."""

    if not _rewrite_shape_compatible(standard_ast, student_ast):
        return False

    def normalized_signature(
        ast: exp.Expression,
    ) -> tuple[
        tuple[str, tuple[tuple[tuple[str, ...], ...] | None, ...]] | None,
        bool,
    ]:
        copied = ast.copy()
        changed = False
        for between in list(copied.find_all(exp.Between)):
            if (
                isinstance(between.parent, exp.Not)
                or between.args.get("symmetric")
                or between.this is None
                or between.args.get("low") is None
                or between.args.get("high") is None
            ):
                continue
            subject = between.this.copy()
            replacement = exp.and_(
                exp.GTE(
                    this=subject.copy(),
                    expression=between.args["low"].copy(),
                ),
                exp.LTE(
                    this=subject,
                    expression=between.args["high"].copy(),
                ),
            )
            between.replace(replacement)
            changed = True
        return _boolean_absorption_rewrite_signature(copied), changed

    standard_signature, standard_changed = normalized_signature(standard_ast)
    student_signature, student_changed = normalized_signature(student_ast)
    return bool(
        standard_changed != student_changed
        and standard_signature is not None
        and standard_signature == student_signature
    )


def _global_extreme_comparison_equivalent(
    standard_ast: exp.Expression,
    student_ast: exp.Expression,
) -> bool:
    """Recognize equality to an unfiltered global MAX/MIN boundary.

    For rows read from relation R, ``x >= MAX_R(x)`` can only be true at the
    maximum and is therefore equivalent to equality (and symmetrically for
    ``MIN``/``<=``). The proof does not hold for filtered, grouped, joined or
    correlated subqueries, so those shapes are rejected here.
    """

    if not _rewrite_shape_compatible(standard_ast, student_ast):
        return False

    def signature(ast: exp.Expression) -> tuple[str, str] | None:
        if not isinstance(ast, exp.Select):
            return None
        copied = ast.copy()
        where = copied.args.get("where")
        comparison = _unwrap_paren(where.this) if isinstance(where, exp.Where) else None
        if not isinstance(comparison, (exp.EQ, exp.GTE, exp.LTE)):
            return None
        left = comparison.left
        right = comparison.right
        if not isinstance(left, exp.Column) or not isinstance(right, exp.Subquery):
            return None
        inner = right.this
        if not isinstance(inner, exp.Select) or len(inner.expressions or ()) != 1:
            return None
        projected = inner.expressions[0]
        projected = projected.this if isinstance(projected, exp.Alias) else projected
        if not isinstance(projected, (exp.Max, exp.Min)):
            return None
        argument = projected.this
        if not isinstance(argument, exp.Column):
            return None
        if projected.args.get("distinct") or isinstance(argument, exp.Distinct):
            return None
        if any(
            copied.args.get(key)
            for key in ("joins", "group", "having", "qualify", "limit", "offset", "with", "with_")
        ):
            return None
        if any(
            inner.args.get(key)
            for key in (
                "joins", "where", "group", "having", "qualify", "limit",
                "offset", "order", "distinct", "with", "with_",
            )
        ):
            return None

        outer_table = _direct_from_table(copied)
        inner_table = _direct_from_table(inner)
        if (
            not isinstance(outer_table, exp.Table)
            or not isinstance(inner_table, exp.Table)
            or _norm_name(outer_table.name) != _norm_name(inner_table.name)
            or _norm_name(left.name) != _norm_name(argument.name)
        ):
            return None

        def belongs_to(column: exp.Column, table: exp.Table) -> bool:
            qualifier = _norm_name(column.table or "")
            return not qualifier or qualifier in {
                _norm_name(table.name),
                _norm_name(table.alias or ""),
            }

        if not belongs_to(left, outer_table) or not belongs_to(argument, inner_table):
            return None
        if isinstance(projected, exp.Max):
            if not isinstance(comparison, (exp.EQ, exp.GTE)):
                return None
            extreme = "MAX"
        else:
            if not isinstance(comparison, (exp.EQ, exp.LTE)):
                return None
            extreme = "MIN"
        where.set(
            "this",
            exp.EQ(this=left.copy(), expression=right.copy()),
        )
        return _sql_of(copied), extreme

    standard_signature = signature(standard_ast)
    student_signature = signature(student_ast)
    return (
        standard_signature is not None
        and standard_signature == student_signature
        and _sql_of(standard_ast) != _sql_of(student_ast)
    )


def _set_operator_signature(ast: exp.Expression | None) -> tuple[tuple[str, str], ...]:
    """Return the ordered set-operation shape of a query.

    The old rewrite fast path compared only the top ``SELECT`` clauses.  That
    allowed a UNION/INTERSECT change to be mistaken for a boolean rewrite and
    caused the whole AST diff graph to be discarded.  Keeping the complete
    ordered shape also handles nested set expressions deterministically.
    """
    if ast is None:
        return ()
    set_types = (exp.Union, exp.Intersect, exp.Except)
    return tuple(
        (type(node).__name__.upper(), _set_operator_modifier(node) or "")
        for node in ast.walk()
        if isinstance(node, set_types)
    )


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


def _window_signature(ast: exp.Expression | None) -> tuple[str, ...]:
    """Return normalized window-expression nodes, including nested SELECTs."""
    if ast is None:
        return ()
    return tuple(_sql_of(node) for node in ast.find_all(exp.Window))


def _outer_distinct_signature(ast: exp.Expression | None) -> bool:
    """Return whether the top-level SELECT has SELECT DISTINCT."""
    select = _top_select(ast) if ast is not None else None
    return bool(select and select.args.get("distinct"))


def _from_source_signature(ast: exp.Expression | None) -> tuple[tuple[str, tuple[tuple[str, str], ...]], ...]:
    """Return direct FROM sources and JOIN table/type topology for each SELECT.

    This deliberately excludes ON predicates, which are normalized separately
    by ``_extract_join_graph`` so explicit and implicit inner joins remain a
    supported equivalence rewrite.
    """
    if ast is None:
        return ()
    signatures: list[tuple[str, tuple[tuple[str, str], ...]]] = []
    for select in ast.find_all(exp.Select):
        from_clause = select.args.get("from_") or select.args.get("from")
        source = from_clause.this if isinstance(from_clause, exp.From) else None
        if isinstance(source, exp.Table):
            # Alias changes do not alter the relation being read.
            source_sql = f"TABLE:{_norm_name(source.name)}"
        elif isinstance(source, exp.Subquery):
            source_sql = f"SUBQUERY:{_norm_name(source.alias or '')}:{_sql_of(source.this)}"
        else:
            source_sql = _sql_of(source)
        joins: list[tuple[str, str]] = []
        # A comma source is represented by sqlglot as ``CROSS`` JOIN.  When
        # the WHERE clause supplies a cross-table equality, the existing join
        # normalizer treats it as an INNER join; mirror that here so the
        # supported implicit-vs-explicit INNER JOIN rewrite remains valid.
        join_graph = _extract_join_graph(select)
        normalized_join_sides = {
            _norm_name(table): side
            for table, side, _ in join_graph.get("joins", [])
        }
        for join in select.args.get("joins") or []:
            target = join.this
            if isinstance(target, exp.Table):
                table_sql = _norm_name(target.name)
            elif isinstance(target, exp.Subquery) and target.alias:
                table_sql = f"SUBQUERY:{_norm_name(target.alias)}:{_sql_of(target.this)}"
            else:
                table_sql = _sql_of(target)
            side = str(join.args.get("side") or join.args.get("kind") or "INNER").upper()
            if side == "CROSS":
                target_name = _norm_name(target.name) if isinstance(target, exp.Table) else ""
                side = normalized_join_sides.get(target_name, side)
            joins.append((table_sql, side))
        signatures.append((source_sql, tuple(joins)))
    return tuple(signatures)


def _from_source_ast_diffs(
    standard_ast: exp.Expression,
    student_ast: exp.Expression,
) -> list[ASTDiffNode]:
    """Emit a focused diff when a query reads a different source relation."""
    std_sig = _from_source_signature(standard_ast)
    stu_sig = _from_source_signature(student_ast)
    if std_sig == stu_sig:
        return []
    return [ASTDiffNode(
        clause_category="FROM",
        diff_type="from_source_changed",
        standard_node=standard_ast.find(exp.From),
        student_node=student_ast.find(exp.From),
        knowledge_point_id="select-basic",
        severity=0.76,
        extra={
            "standard_sources": std_sig,
            "student_sources": stu_sig,
            "standard_sql": _sql_of(standard_ast.find(exp.From)),
            "student_sql": _sql_of(student_ast.find(exp.From)),
        },
    )]


def _rewrite_shape_compatible(
    standard_ast: exp.Expression,
    student_ast: exp.Expression,
    *,
    allow_cte_inline: bool = False,
) -> bool:
    """Guard semantic-rewrite shortcuts from crossing structural boundaries."""
    if _set_operator_signature(standard_ast) != _set_operator_signature(student_ast):
        return False
    if _window_signature(standard_ast) != _window_signature(student_ast):
        return False
    if _outer_distinct_signature(standard_ast) != _outer_distinct_signature(student_ast):
        return False
    if _from_source_signature(standard_ast) != _from_source_signature(student_ast):
        return False
    if not allow_cte_inline:
        std_ctes = tuple(_sql_of(node) for node in standard_ast.find_all(exp.CTE))
        stu_ctes = tuple(_sql_of(node) for node in student_ast.find_all(exp.CTE))
        if std_ctes != stu_ctes:
            return False
    return True


def _where_boolean_absorption_equivalent(
    standard_ast: exp.Expression,
    student_ast: exp.Expression,
) -> bool:
    if not _rewrite_shape_compatible(standard_ast, student_ast):
        return False
    standard_signature = _boolean_absorption_rewrite_signature(standard_ast)
    student_signature = _boolean_absorption_rewrite_signature(student_ast)
    return (
        standard_signature is not None
        and standard_signature == student_signature
    )


def _boolean_absorption_rewrite_signature(
    ast: exp.Expression,
) -> tuple[str, tuple[tuple[tuple[str, ...], ...] | None, ...]] | None:
    """Canonicalize every query block's WHERE without hiding other changes.

    The previous fast path inspected only ``_top_select``. For a set query
    that is the left branch, so a real predicate change in the right UNION
    branch could be discarded as an equivalent boolean rewrite. Replace each
    SELECT-local WHERE with the same placeholder and retain its canonical DNF
    alongside the complete remaining AST shape. This recognizes absorption
    in any corresponding query block while requiring every non-WHERE node to
    remain unchanged.
    """

    copied = ast.copy()
    where_signatures: list[tuple[tuple[str, ...], ...] | None] = []
    has_where = False
    for select in list(copied.find_all(exp.Select)):
        where = select.args.get("where")
        if not isinstance(where, exp.Where):
            where_signatures.append(None)
            continue
        has_where = True
        where_signatures.append(_boolean_dnf_signature(where.this))
        where.set("this", exp.Boolean(this=True))
    if not has_where:
        return None
    return _sql_of(copied), tuple(where_signatures)


def _boolean_dnf_signature(node: exp.Expression | None) -> tuple[tuple[str, ...], ...]:
    if node is None:
        return tuple()
    terms = _boolean_dnf_terms(_unwrap_paren(node))
    unique_terms = {frozenset(term) for term in terms}
    absorbed = {
        term
        for term in unique_terms
        if not any(other < term for other in unique_terms)
    }
    return tuple(sorted(tuple(sorted(term)) for term in absorbed))


def _boolean_dnf_terms(node: exp.Expression) -> list[frozenset[str]]:
    node = _unwrap_paren(node)
    if isinstance(node, exp.Or):
        return _boolean_dnf_terms(node.left) + _boolean_dnf_terms(node.right)
    if isinstance(node, exp.And):
        return [
            left | right
            for left in _boolean_dnf_terms(node.left)
            for right in _boolean_dnf_terms(node.right)
        ]
    return [frozenset({_sql_of(node)})]


def _direct_from_table(select: exp.Select | None) -> exp.Table | None:
    if not isinstance(select, exp.Select):
        return None
    from_clause = select.args.get("from_") or select.args.get("from")
    return from_clause.this if isinstance(from_clause, exp.From) and isinstance(from_clause.this, exp.Table) else None


def _unqualified_sql(node: exp.Expression | None) -> str:
    if node is None:
        return ""
    copied = node.copy()
    for column in copied.find_all(exp.Column):
        column.set("table", None)
    return _sql_of(copied)


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
        for key in ("joins", "group", "having", "order", "limit", "offset", "qualify", "distinct", "with", "with_")
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
    unsupported = ("joins", "where", "group", "having", "order", "limit", "offset", "qualify")
    if any(outer.args.get(key) for key in unsupported):
        return False
    if any(inline.args.get(key) for key in ("joins", "group", "having", "qualify")):
        return False
    # The outer query's result shaping must be preserved by the inline form.
    # The CTE body WHERE is compared below as the filter that moves outward;
    # ORDER/LIMIT/OFFSET (and a future QUALIFY) belong to the outer query and
    # therefore must match exactly on both sides.
    for key in ("order", "limit", "offset", "qualify"):
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


def _semantic_diff(
    diff_type: str,
    clause: str,
    standard_node: exp.Expression | None,
    student_node: exp.Expression | None,
    knowledge_point_id: str,
    **extra: Any,
) -> ASTDiffNode:
    return ASTDiffNode(
        clause_category=clause,
        diff_type=diff_type,
        standard_node=standard_node,
        student_node=student_node,
        knowledge_point_id=knowledge_point_id,
        severity=0.74,
        extra={
            "standard_sql": _sql_of(standard_node),
            "student_sql": _sql_of(student_node),
            **extra,
        },
    )


def _projection_is_true_inner(node: exp.Expression) -> exp.Expression | None:
    """Return the predicate from the strict ``predicate IS TRUE`` form."""
    node = _unwrap_paren(node)
    if not isinstance(node, exp.Is):
        return None
    truth_value = node.args.get("expression")
    if not isinstance(truth_value, exp.Boolean) or truth_value.this is not True:
        return None
    predicate = _unwrap_paren(node.this)
    # These predicates are already two-valued, so wrapping them in IS TRUE
    # cannot expose the FALSE-versus-NULL projection distinction.
    if isinstance(
        predicate,
        (exp.Boolean, exp.Exists, exp.Is, exp.NullSafeEQ, exp.NullSafeNEQ),
    ):
        return None
    return predicate


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
    if _not_in_not_exists_rewrite(standard_ast, student_ast) or _not_in_not_exists_rewrite(student_ast, standard_ast):
        diffs.append(_semantic_diff(
            "null_sensitive_antijoin_equivalence", "NULL", standard_ast.find(exp.In), student_ast.find(exp.Exists), "null-handling",
            standard_query_sql=standard_sql,
            student_query_sql=student_sql,
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


def _is_not_between_expansion(not_between: exp.Expression | None, expanded: exp.Expression | None) -> bool:
    if not isinstance(not_between, exp.Not) or not isinstance(_unwrap_paren(not_between.this), exp.Between):
        return False
    between = _unwrap_paren(not_between.this)
    expanded = _unwrap_paren(expanded) if isinstance(expanded, exp.Expression) else expanded
    if not isinstance(expanded, exp.Or):
        return False
    left, right = _unwrap_paren(expanded.left), _unwrap_paren(expanded.right)
    if not isinstance(left, exp.LT) or not isinstance(right, exp.GT):
        return False
    return (
        _unqualified_sql(left.left) == _unqualified_sql(between.this) == _unqualified_sql(right.left)
        and _sql_of(left.right) == _sql_of(between.args.get("low"))
        and _sql_of(right.right) == _sql_of(between.args.get("high"))
    )


def _is_like_negation_equivalence(left: exp.Expression | None, right: exp.Expression | None) -> bool:
    def signature(node: exp.Expression | None) -> tuple[str, str] | None:
        node = _unwrap_paren(node) if isinstance(node, exp.Expression) else node
        if not isinstance(node, exp.Not):
            return None
        inner = _unwrap_paren(node.this)
        if not isinstance(inner, exp.Like):
            return None
        return _unqualified_sql(inner.this), _sql_of(inner.expression)
    return signature(left) is not None and signature(left) == signature(right)


def _logical_tree_signature(node: exp.Expression | None) -> Any:
    if node is None:
        return None
    node = _unwrap_paren(node)
    if isinstance(node, (exp.And, exp.Or)):
        operator = "AND" if isinstance(node, exp.And) else "OR"
        children = [_logical_tree_signature(node.left), _logical_tree_signature(node.right)]
        return (operator, *sorted(children, key=repr))
    if isinstance(node, exp.Not):
        return ("NOT", _logical_tree_signature(node.this))
    return _unqualified_sql(node)


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


def _not_in_not_exists_rewrite(not_in_ast: exp.Expression, not_exists_ast: exp.Expression) -> bool:
    in_node = not_in_ast.find(exp.In)
    exists = not_exists_ast.find(exp.Exists)
    if not isinstance(in_node, exp.In) or not isinstance(exists, exp.Exists):
        return False
    return isinstance(in_node.parent, exp.Not) and isinstance(exists.parent, exp.Not) and _in_exists_rewrite(
        not_in_ast,
        not_exists_ast,
        allow_negated=True,
    )


def _select_projection_repr(ast: exp.Expression) -> str:
    """Return a normalised string of just the SELECT projection list.

    ``exp.Select.sql()`` includes FROM / WHERE / JOIN / … so comparing two
    Select nodes with ``_sql_of`` triggers a false ``projection_changed``
    whenever *any* other clause differs.  This helper narrows the comparison
    to the projection expressions only (``SELECT a, b`` → ``"a, b"``).

    Table-alias prefixes (``a.name`` vs ``b.name``) are stripped so that
    semantically identical projections with different aliases compare equal.
    """
    select = ast.find(exp.Select)
    if not isinstance(select, exp.Select):
        return ""
    parts = []
    for item in select.expressions or []:
        item = item.this if isinstance(item, exp.Alias) else item
        # Only strip table prefix from TOP-LEVEL bare column refs (not inside functions).
        # This prevents COUNT(a.id) and COUNT(b.id) from being conflated to COUNT(id).
        if isinstance(item, exp.Column) and item.table:
            stripped = exp.column(item.name)
        elif isinstance(item, exp.Alias) and isinstance(item.this, exp.Column) and item.this.table:
            stripped = exp.alias_(exp.column(item.this.name), item.alias)
        else:
            stripped = item
        parts.append(_sql_of(stripped))
    return ", ".join(parts)


def _projection_change_is_aggregate_function_only(
    standard_ast: exp.Expression,
    student_ast: exp.Expression,
) -> bool:
    """Return True when projection shape differs only by aggregate names.

    ``MAX(hours)`` versus ``MIN(hours)`` already has the focused
    ``aggregate_function_changed`` diff. Reporting the same edit as a dropped
    projection and an added projection creates unrelated obligations,
    especially when the aggregate lives in an EXCEPT/UNION branch.
    """

    standard_select = _top_select(standard_ast)
    student_select = _top_select(student_ast)
    if not isinstance(standard_select, exp.Select) or not isinstance(student_select, exp.Select):
        return False
    standard_items = list(standard_select.expressions or ())
    student_items = list(student_select.expressions or ())
    if not standard_items or len(standard_items) != len(student_items):
        return False

    changed_aggregate = False
    for standard_item, student_item in zip(standard_items, student_items):
        # Wrap each item so a projection that is itself an aggregate has a
        # parent; ``Expression.replace`` cannot replace a detached root node.
        standard_wrapper = exp.Paren(this=standard_item.copy())
        student_wrapper = exp.Paren(this=student_item.copy())
        standard_copy = standard_wrapper.this
        student_copy = student_wrapper.this
        standard_aggregates = list(standard_copy.find_all(*_AGG_FUNC_TYPES))
        student_aggregates = list(student_copy.find_all(*_AGG_FUNC_TYPES))
        if len(standard_aggregates) != len(student_aggregates):
            return False
        for index, (standard_aggregate, student_aggregate) in enumerate(
            zip(standard_aggregates, student_aggregates)
        ):
            if _function_args(standard_aggregate) != _function_args(student_aggregate):
                return False
            if type(standard_aggregate) is not type(student_aggregate):
                changed_aggregate = True
            placeholder = exp.column(f"__aggregate_{index}__")
            standard_aggregate.replace(placeholder.copy())
            student_aggregate.replace(placeholder.copy())
        if _sql_of(standard_wrapper.this) != _sql_of(student_wrapper.this):
            return False
    return changed_aggregate


def _strip_alias(node: exp.Expression) -> exp.Expression:
    """Strip table-alias prefix from top-level bare column refs only.

    Columns inside function calls (e.g. ``COUNT(a.id)``) are left intact
    so that ``COUNT(a.id)`` and ``COUNT(b.id)`` are not conflated.
    """
    if isinstance(node, exp.Column) and node.table:
        return exp.column(node.name)
    if isinstance(node, exp.Alias):
        return _strip_alias(node.this)
    return node


def _projection_label(item: exp.Expression) -> str:
    """Canonical label for one projection item (alias-stripped SQL text)."""
    return _sql_of(_strip_alias(item))


def _projection_column_ast_diffs(
    standard_ast: exp.Expression,
    student_ast: exp.Expression,
) -> list[ASTDiffNode]:
    """Column-level SELECT projection diff.

    When ``projection_changed`` fires at the clause level, this function
    drills down to identify *which* columns were dropped, added or changed,
    populating ``target_column`` so downstream data-generation can act on
    specific columns.
    """
    std_select = _top_select(standard_ast)
    stu_select = _top_select(student_ast)
    if not isinstance(std_select, exp.Select) or not isinstance(stu_select, exp.Select):
        return []
    if _projection_change_is_aggregate_function_only(standard_ast, student_ast):
        return []

    std_items = list(std_select.expressions or [])
    stu_items = list(stu_select.expressions or [])
    if not std_items and not stu_items:
        return []

    # Build normalised label lists
    std_labels = [_projection_label(item) for item in std_items]
    stu_labels = [_projection_label(item) for item in stu_items]

    # Quick equality check (order-sensitive)
    if std_labels == stu_labels:
        return []

    std_set = set(std_labels)
    stu_set = set(stu_labels)
    diffs: list[ASTDiffNode] = []

    # Columns dropped (in standard but not in student)
    for idx, (label, node) in enumerate(zip(std_labels, std_items)):
        if label not in stu_set:
            col_name = _extract_column_name(node)
            diffs.append(ASTDiffNode(
                clause_category="SELECT",
                diff_type="column_dropped",
                target_column=col_name,
                standard_node=node,
                student_node=None,
                knowledge_point_id="select-basic",
                severity=0.7,
                extra={"standard_sql": label, "student_sql": "", "position": idx},
            ))

    # Columns added (in student but not in standard)
    for idx, (label, node) in enumerate(zip(stu_labels, stu_items)):
        if label not in std_set:
            col_name = _extract_column_name(node)
            diffs.append(ASTDiffNode(
                clause_category="SELECT",
                diff_type="column_added",
                target_column=col_name,
                standard_node=None,
                student_node=node,
                knowledge_point_id="select-basic",
                severity=0.5,
                extra={"standard_sql": "", "student_sql": label, "position": idx},
            ))

    # Star expansion mismatch: one side has *, the other doesn't
    std_has_star = any(_is_star(item) for item in std_items)
    stu_has_star = any(_is_star(item) for item in stu_items)
    if std_has_star != stu_has_star:
        diffs.append(ASTDiffNode(
            clause_category="SELECT",
            diff_type="star_mismatch",
            standard_node=std_select,
            student_node=stu_select,
            knowledge_point_id="select-basic",
            severity=0.6,
            extra={
                "standard_has_star": std_has_star,
                "student_has_star": stu_has_star,
                "standard_sql": ", ".join(std_labels),
                "student_sql": ", ".join(stu_labels),
            },
        ))

    return diffs


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


def _function_name(node: exp.Expression) -> str:
    try:
        return str(node.sql_name()).upper()
    except Exception:
        if isinstance(node, exp.Anonymous):
            return str(node.this or "").upper()
        return type(node).__name__.upper()


def _function_args(node: exp.Expression) -> list[str]:
    values: list[exp.Expression] = []
    for key in getattr(node, "arg_types", {}):
        if isinstance(node, exp.Anonymous) and key == "this":
            continue
        value = node.args.get(key)
        if isinstance(value, exp.Expression):
            values.append(value)
        elif isinstance(value, list):
            values.extend(item for item in value if isinstance(item, exp.Expression))
    return [_sql_of(value) for value in values]


def _function_sql(node: exp.Expression) -> str:
    """Render function detail without losing dialect-owned modifiers."""
    if isinstance(node, exp.GroupConcat):
        return node.sql(dialect="mysql", normalize=True)
    return _sql_of(node)


def _logical_connective_shape(node: exp.Expression | None) -> Any:
    """Describe only AND/OR nesting so positional leaves remain comparable."""
    if node is None:
        return None
    node = _unwrap_paren(node)
    if isinstance(node, exp.And):
        return ("AND", _logical_connective_shape(node.left), _logical_connective_shape(node.right))
    if isinstance(node, exp.Or):
        return ("OR", _logical_connective_shape(node.left), _logical_connective_shape(node.right))
    return "LEAF"


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
            },
        ))
    return diffs


def _top_select(ast: exp.Expression) -> exp.Select | None:
    if isinstance(ast, exp.Select):
        return ast
    if isinstance(ast, (exp.Union, exp.Intersect, exp.Except)):
        return ast.this if isinstance(ast.this, exp.Select) else ast.this.find(exp.Select)
    return ast.find(exp.Select)


def _result_order_clause(ast: exp.Expression | None) -> exp.Order | None:
    """Return ORDER BY attached directly to the result-producing query."""
    if not isinstance(ast, exp.Query):
        return None
    order = ast.args.get("order")
    return order if isinstance(order, exp.Order) else None


def _group_by_items(ast: exp.Expression) -> list[tuple[str, exp.Expression]]:
    select = _top_select(ast)
    if not isinstance(select, exp.Select):
        return []
    group = select.args.get("group")
    if not isinstance(group, exp.Group):
        return []
    items: list[tuple[str, exp.Expression]] = []
    for item in group.expressions or []:
        resolved = item
        if isinstance(item, exp.Literal) and not item.is_string:
            try:
                position = int(str(item.this))
            except (TypeError, ValueError):
                position = 0
            if 1 <= position <= len(select.expressions):
                resolved = select.expressions[position - 1]
                if isinstance(resolved, exp.Alias):
                    resolved = resolved.this
        items.append((_sql_of(_strip_alias(resolved)), resolved))
    return items


def _group_by_repr(ast: exp.Expression) -> str:
    # Top-level GROUP BY key order (and duplicate spelling of the same key)
    # does not change the row partition. Composite constructs such as
    # ROLLUP(a, b) remain one SQL item, so their internal order is preserved.
    return " | ".join(sorted({sql for sql, _ in _group_by_items(ast)}))


def _group_by_ast_diffs(
    standard_ast: exp.Expression,
    student_ast: exp.Expression,
) -> list[ASTDiffNode]:
    std_items = _group_by_items(standard_ast)
    stu_items = _group_by_items(student_ast)
    std_set = {sql for sql, _ in std_items}
    stu_set = {sql for sql, _ in stu_items}
    if std_set == stu_set:
        return []
    diff_type = "group_by_expression_changed"
    if std_set and std_set < stu_set:
        diff_type = "grouping_grain_too_fine"
    elif stu_set and stu_set < std_set:
        diff_type = "grouping_grain_too_coarse"
    return [ASTDiffNode(
        clause_category="GROUP BY",
        diff_type=diff_type,
        target_column=next(iter((stu_set - std_set) or (std_set - stu_set)), None),
        standard_node=_top_select(standard_ast).args.get("group") if _top_select(standard_ast) else None,
        student_node=_top_select(student_ast).args.get("group") if _top_select(student_ast) else None,
        knowledge_point_id="group-by",
        severity=0.74,
        extra={
            "standard_keys": sorted(std_set),
            "student_keys": sorted(stu_set),
            "added_keys": sorted(stu_set - std_set),
            "removed_keys": sorted(std_set - stu_set),
            "standard_sql": _group_by_repr(standard_ast),
            "student_sql": _group_by_repr(student_ast),
            "standard_query_sql": _sql_of(standard_ast),
            "student_query_sql": _sql_of(student_ast),
            "standard_group_columns": [
                _extract_column_name(item)
                for _, item in std_items
                if _extract_column_name(item)
            ],
            "student_group_columns": [
                _extract_column_name(item)
                for _, item in stu_items
                if _extract_column_name(item)
            ],
        },
    )]


def _having_placement_ast_diffs(
    standard_ast: exp.Expression,
    student_ast: exp.Expression,
) -> list[ASTDiffNode]:
    std_select = _top_select(standard_ast)
    stu_select = _top_select(student_ast)
    if not isinstance(std_select, exp.Select) or not isinstance(stu_select, exp.Select):
        return []
    std_having = std_select.args.get("having")
    stu_having = stu_select.args.get("having")
    stu_where = stu_select.args.get("where")
    if not std_having or stu_having or not stu_where:
        return []
    if not std_having.find(exp.AggFunc) or not stu_where.find(exp.AggFunc):
        return []
    return [ASTDiffNode(
        clause_category="HAVING",
        diff_type="aggregate_condition_in_where",
        standard_node=std_having,
        student_node=stu_where,
        knowledge_point_id="having",
        severity=0.85,
        extra={"standard_sql": _sql_of(std_having), "student_sql": _sql_of(stu_where)},
    )]


def _order_by_items(ast: exp.Expression) -> list[tuple[str, bool, exp.Expression]]:
    order = _result_order_clause(ast)
    if not isinstance(order, exp.Order):
        return []
    items = []
    for item in order.expressions or []:
        expression = item.this if isinstance(item, exp.Ordered) else item
        items.append((_sql_of(_strip_alias(expression)), bool(item.args.get("desc")), item))
    return items


def _order_by_ast_diffs(
    standard_ast: exp.Expression,
    student_ast: exp.Expression,
) -> list[ASTDiffNode]:
    std_items = _order_by_items(standard_ast)
    stu_items = _order_by_items(student_ast)
    std_sig = [(sql, desc) for sql, desc, _ in std_items]
    stu_sig = [(sql, desc) for sql, desc, _ in stu_items]
    if std_sig == stu_sig:
        return []
    diff_type = None
    if len(std_sig) > len(stu_sig) and std_sig[:len(stu_sig)] == stu_sig:
        diff_type = "order_by_tiebreaker_missing"
    elif len(stu_sig) > len(std_sig) and stu_sig[:len(std_sig)] == std_sig:
        diff_type = "order_by_key_added"
    elif (
        len(std_sig) == len(stu_sig)
        and all(a[0] == b[0] for a, b in zip(std_sig, stu_sig))
        and any(a[1] != b[1] for a, b in zip(std_sig, stu_sig))
    ):
        diff_type = "order_direction_changed"
    if not diff_type:
        return []
    std_order = _result_order_clause(standard_ast)
    stu_order = _result_order_clause(student_ast)
    source = _direct_from_table(_top_select(standard_ast)) if _top_select(standard_ast) else None
    return [ASTDiffNode(
        clause_category="ORDER BY",
        diff_type=diff_type,
        standard_node=std_order,
        student_node=stu_order,
        knowledge_point_id="order-by",
        severity=0.7,
        extra={
            "standard_keys": std_sig,
            "student_keys": stu_sig,
            "standard_order_keys": tuple(std_sig),
            "student_order_keys": tuple(stu_sig),
            "standard_source_table": source.name if isinstance(source, exp.Table) else "",
        },
    )]


def _extract_column_name(node: exp.Expression | None) -> str | None:
    """Best-effort extraction of the primary column name from a projection item."""
    if node is None:
        return None
    if isinstance(node, exp.Column):
        return node.name
    col = node.find(exp.Column)
    if col:
        return col.name
    if isinstance(node, exp.Alias):
        return _extract_column_name(node.this)
    return None


def _is_star(node: exp.Expression) -> bool:
    return isinstance(node, exp.Star) or (isinstance(node, exp.Column) and node.name == "*")


def _flatten_and(node: exp.Expression) -> list[exp.Expression]:
    """Flatten nested AND nodes into a list of leaf predicates."""
    if isinstance(node, exp.And):
        return _flatten_and(node.left) + _flatten_and(node.right)
    return [node]


def _normalize_where_repr(ast: exp.Expression) -> str:
    """Return a canonical string for WHERE clause comparison.

    AND-connected predicates are sorted so that ``a=1 AND b=2`` and
    ``b=2 AND a=1`` compare equal (commutativity).  OR and mixed
    boolean trees are left untouched.

    Cross-table equality predicates (implicit join conditions like
    ``a.id = b.aid``) are excluded so that implicit and explicit JOIN
    styles produce the same WHERE representation.
    """
    select = _top_select(ast)
    where = select.args.get("where") if isinstance(select, exp.Select) else None
    if where is None:
        return ""
    body = where.this
    # Only sort when the top level is pure AND (no OR mixed in)
    predicates = _flatten_and(body)
    has_or = any(isinstance(p, exp.Or) for p in predicates)
    if has_or:
        # Strip top-level cross-table conditions; OR sub-expressions are kept as-is
        # (cross-table conditions nested inside OR are rare and hard to strip structurally).
        preds = [_unwrap_paren(p) for p in predicates if not _is_cross_table_condition(p)]
        if not preds:
            return ""
        # Rebuild from filtered preds (single pred → raw SQL; multiple → AND-join, sorted).
        if len(preds) == 1:
            return _sql_of(preds[0])
        parts = sorted((_sql_of(p) for p in preds), key=str.lower)
        return " AND ".join(parts)
    # Filter out implicit-join conditions (cross-table equalities)
    preds = [_unwrap_paren(p) for p in predicates if not _is_cross_table_condition(p)]
    if not preds:
        return ""
    sorted_preds = sorted((_sql_of(p) for p in preds), key=str.lower)
    return " AND ".join(sorted_preds)


def _unwrap_paren(node: exp.Expression) -> exp.Expression:
    """Strip redundant Paren wrappers so that ``(expr)`` and ``expr`` serialise identically."""
    while isinstance(node, exp.Paren):
        inner = node.this
        if inner is None:
            break
        node = inner
    return node


def _is_cross_table_condition(pred: exp.Expression) -> bool:
    """Return True if *pred* is an equality between columns of different tables.

    E.g. ``a.id = b.aid`` → True (implicit join condition).
    ``a.x > 1`` → False (single-table filter).
    """
    cols = list(pred.find_all(exp.Column))
    tables = {c.table for c in cols if c.table}
    if len(tables) < 2:
        return False
    # Must be an equality comparison (=, not >, <, etc.)
    return isinstance(pred, (exp.EQ,))


def _clause_ast_diffs(standard_ast: exp.Expression, student_ast: exp.Expression) -> list[ASTDiffNode]:
    specs = [
        ("SELECT", "projection_changed", lambda ast: _select_projection_repr(ast), "select-basic"),
        ("WHERE", "where_changed", lambda ast: _normalize_where_repr(ast), "where"),
        ("GROUP BY", "group_by_changed", _group_by_repr, "group-by"),
        ("HAVING", "having_changed", lambda ast: (_top_select(ast).args.get("having") if _top_select(ast) else None), "having"),
        ("ORDER BY", "order_by_changed", _result_order_clause, "order-by"),
        ("QUALIFY", "qualify_changed", lambda ast: (_top_select(ast).args.get("qualify") if _top_select(ast) else None), "window-row-number"),
        ("LIMIT", "limit_changed", _limit_repr, "limit"),
        ("LIMIT", "limit_changed", _offset_repr, "limit"),
    ]
    diffs: list[ASTDiffNode] = []
    for clause, diff_type, getter, kp in specs:
        std_node = getter(standard_ast)
        stu_node = getter(student_ast)
        if _sql_of(std_node) != _sql_of(stu_node):
            if (
                clause == "SELECT"
                and _projection_change_is_aggregate_function_only(
                    standard_ast,
                    student_ast,
                )
            ):
                continue
            if clause == "SELECT":
                std_node = _top_select(standard_ast)
                stu_node = _top_select(student_ast)
            elif diff_type == "limit_changed":
                if getter is _limit_repr:
                    std_node = standard_ast.find(exp.Limit) or standard_ast.find(exp.Fetch)
                    stu_node = student_ast.find(exp.Limit) or student_ast.find(exp.Fetch)
                else:
                    std_node = standard_ast.find(exp.Offset)
                    stu_node = student_ast.find(exp.Offset)
            extra = {
                "standard_sql": _sql_of(std_node),
                "student_sql": _sql_of(stu_node),
            }
            if clause == "GROUP BY":
                extra.update(_group_aggregate_metadata(standard_ast, student_ast))
            elif clause == "HAVING":
                extra.update(_group_aggregate_metadata(standard_ast, student_ast))
            diffs.append(ASTDiffNode(
                clause_category=clause,
                diff_type=diff_type,
                standard_node=std_node,
                student_node=stu_node,
                knowledge_point_id=kp,
                extra=extra,
            ))
    std_top = _top_select(standard_ast)
    stu_top = _top_select(student_ast)
    std_distinct = std_top.args.get("distinct") if isinstance(std_top, exp.Select) else None
    stu_distinct = stu_top.args.get("distinct") if isinstance(stu_top, exp.Select) else None
    if bool(std_distinct) != bool(stu_distinct):
        diffs.append(ASTDiffNode(
            clause_category="DISTINCT",
            diff_type="distinct_changed",
            standard_node=std_distinct,
            student_node=stu_distinct,
            knowledge_point_id="distinct",
            extra={
                "standard_sql": str(bool(std_distinct)),
                "student_sql": str(bool(stu_distinct)),
            }
        ))
    return diffs


def _group_aggregate_metadata(
    standard_ast: exp.Expression,
    student_ast: exp.Expression,
) -> dict[str, Any]:
    """Attach physical grouping context to clause-level GROUP/HAVING diffs."""
    metadata: dict[str, Any] = {}
    for label, ast in (("standard", standard_ast), ("student", student_ast)):
        select = _top_select(ast)
        if not isinstance(select, exp.Select):
            continue
        group = select.args.get("group")
        if isinstance(group, exp.Group):
            metadata[f"{label}_group_columns"] = tuple(
                _sql_of(_strip_alias(item)) for item in group.expressions or ()
            )
        table = next((item for item in select.find_all(exp.Table)), None)
        if table is not None:
            metadata[f"{label}_source_table"] = str(table.name).lower()
        having = select.args.get("having")
        aggregate = having.find(*_AGG_FUNC_TYPES) if isinstance(having, exp.Expression) else None
        if aggregate is not None:
            metadata[f"{label}_aggregate_function"] = type(aggregate).__name__.upper()
            metadata[f"{label}_aggregate_argument"] = (
                _sql_of(aggregate.this) if aggregate.this is not None else "*"
            )
    return metadata


def _advanced_clause_ast_diffs(
    standard_ast: exp.Expression,
    student_ast: exp.Expression,
) -> list[ASTDiffNode]:
    """Compare advanced clauses that live outside ordinary expression lists."""
    std_select = _top_select(standard_ast)
    stu_select = _top_select(student_ast)
    if not isinstance(std_select, exp.Select) or not isinstance(stu_select, exp.Select):
        return []

    def distinct_on(select: exp.Select) -> exp.Expression | None:
        distinct = select.args.get("distinct")
        return distinct.args.get("on") if isinstance(distinct, exp.Distinct) else None

    def group_modifier(select: exp.Select, key: str) -> list[exp.Expression]:
        group = select.args.get("group")
        return list(group.args.get(key) or []) if isinstance(group, exp.Group) else []

    def filters(select: exp.Select) -> list[exp.Expression]:
        return [node for node in select.find_all(exp.Filter) if not _is_inside_subquery(node)]

    def lateral_sources(select: exp.Select) -> list[exp.Expression]:
        return [node for node in select.find_all(exp.Lateral) if not _is_inside_subquery(node)]

    def recursive_decoration(select: exp.Select) -> exp.Expression | None:
        with_node = select.args.get("with_") or select.args.get("with")
        return with_node.args.get("search") if isinstance(with_node, exp.With) else None

    def pivots(select: exp.Select) -> list[exp.Expression]:
        return list(select.find_all(exp.Pivot))

    def table_samples(select: exp.Select) -> list[exp.Expression]:
        return list(select.find_all(exp.TableSample))

    def hierarchical_query(select: exp.Select) -> exp.Expression | None:
        return select.args.get("connect")

    def only_tables(select: exp.Select) -> list[exp.Expression]:
        return [table for table in select.find_all(exp.Table) if table.args.get("only")]

    def structural_sql(node: exp.Expression | None) -> str:
        """Render vendor-only nodes without SQLite dropping their syntax."""
        if node is None:
            return ""
        try:
            return node.sql(normalize=True)
        except Exception:
            return str(node)

    specs: list[tuple[str, str, str, Any]] = [
        ("DISTINCT ON", "distinct_on_changed", "distinct", distinct_on),
        ("GROUPING SETS", "grouping_sets_changed", "group-by", lambda select: group_modifier(select, "grouping_sets")),
        ("ROLLUP", "rollup_changed", "group-by", lambda select: group_modifier(select, "rollup")),
        ("CUBE", "cube_changed", "group-by", lambda select: group_modifier(select, "cube")),
        ("AGGREGATE FILTER", "aggregate_filter_changed", "aggregate", filters),
        ("LATERAL", "lateral_changed", "join-inner", lateral_sources),
        ("PIVOT", "pivot_changed", "pivot", pivots),
        ("TABLE SAMPLE", "table_sample_changed", "table-sample", table_samples),
        ("CONNECT BY", "hierarchical_query_changed", "hierarchical-query", hierarchical_query),
        ("FROM ONLY", "table_only_changed", "table-only", only_tables),
    ]
    diffs: list[ASTDiffNode] = []
    for clause, diff_type, kp, getter in specs:
        std_value = getter(std_select)
        stu_value = getter(stu_select)
        std_sql = (
            " | ".join(structural_sql(item) for item in std_value)
            if isinstance(std_value, list)
            else structural_sql(std_value)
        )
        stu_sql = (
            " | ".join(structural_sql(item) for item in stu_value)
            if isinstance(stu_value, list)
            else structural_sql(stu_value)
        )
        if std_sql == stu_sql:
            continue
        standard_node = (
            std_value[0]
            if isinstance(std_value, list) and std_value
            else std_value
        )
        student_node = (
            stu_value[0]
            if isinstance(stu_value, list) and stu_value
            else stu_value
        )
        extra = {"standard_sql": std_sql, "student_sql": stu_sql}
        target_table = None
        target_column = None
        if diff_type == "aggregate_filter_changed":
            standard_filter = standard_node if isinstance(standard_node, exp.Filter) else None
            student_filter = student_node if isinstance(student_node, exp.Filter) else None

            def filter_predicate(node: exp.Filter | None) -> exp.Expression | None:
                if node is None:
                    return None
                expression = node.args.get("expression")
                if isinstance(expression, exp.Where):
                    return expression.this
                return expression if isinstance(expression, exp.Expression) else None

            def projection_for_filter(
                select: exp.Select,
                filter_node: exp.Filter | None,
            ) -> tuple[int, exp.Expression] | None:
                if filter_node is None:
                    return None
                for index, item in enumerate(select.expressions or ()):
                    expression = item.this if isinstance(item, exp.Alias) else item
                    if filter_node is expression or filter_node in expression.find_all(exp.Filter):
                        return index, expression
                return None

            standard_position = projection_for_filter(std_select, standard_filter)
            student_position = projection_for_filter(stu_select, student_filter)
            position = (
                standard_position[0]
                if standard_position is not None
                else student_position[0]
                if student_position is not None
                else None
            )
            if position is not None:
                if (
                    standard_filter is None
                    and standard_position is None
                    and position < len(std_select.expressions)
                ):
                    standard_node = (
                        std_select.expressions[position].this
                        if isinstance(std_select.expressions[position], exp.Alias)
                        else std_select.expressions[position]
                    )
                if (
                    student_filter is None
                    and student_position is None
                    and position < len(stu_select.expressions)
                ):
                    student_node = (
                        stu_select.expressions[position].this
                        if isinstance(stu_select.expressions[position], exp.Alias)
                        else stu_select.expressions[position]
                    )
            standard_predicate = filter_predicate(standard_filter)
            student_predicate = filter_predicate(student_filter)
            predicate = standard_predicate or student_predicate
            target_column = _extract_column_name(predicate)
            source = _direct_from_table(std_select)
            target_table = source.name if isinstance(source, exp.Table) else None
            group = std_select.args.get("group")
            extra.update({
                "standard_filter_predicate": _sql_of(standard_predicate),
                "student_filter_predicate": _sql_of(student_predicate),
                "standard_source_table": target_table or "",
                "standard_group_columns": tuple(
                    _sql_of(item) for item in group.expressions or ()
                ) if isinstance(group, exp.Group) else (),
                "standard_query_sql": _sql_of(std_select),
                "student_query_sql": _sql_of(stu_select),
            })
        diffs.append(ASTDiffNode(
            clause_category=clause,
            diff_type=diff_type,
            standard_node=standard_node,
            student_node=student_node,
            knowledge_point_id=kp,
            target_table=target_table,
            target_column=target_column,
            severity=0.78,
            extra=extra,
        ))

    std_recursive = recursive_decoration(std_select)
    stu_recursive = recursive_decoration(stu_select)
    if _sql_of(std_recursive) != _sql_of(stu_recursive):
        decoration = std_recursive if std_recursive is not None else stu_recursive
        kind = str(
            decoration.args.get("kind")
            if isinstance(decoration, exp.Expression)
            else ""
        ).upper()
        clause = "CYCLE" if kind == "CYCLE" else "SEARCH"
        diffs.append(ASTDiffNode(
            clause_category=clause,
            diff_type="recursive_cycle_changed" if clause == "CYCLE" else "recursive_search_changed",
            standard_node=std_recursive,
            student_node=stu_recursive,
            knowledge_point_id="cte-recursive",
            severity=0.78,
            extra={
                "standard_sql": _sql_of(std_recursive),
                "student_sql": _sql_of(stu_recursive),
            },
        ))
    return diffs


def _limit_repr(ast: exp.Expression) -> str:
    """Canonical LIMIT/FETCH representation for dialect-equivalent syntax."""
    node = ast.args.get("limit") if isinstance(ast, exp.Query) else None
    if node is None:
        return ""
    expr = getattr(node, "expression", None) or node.args.get("count") or node.args.get("this")
    if expr is None:
        return _sql_of(node)
    options = node.args.get("limit_options")
    modifiers: list[str] = []
    if isinstance(options, exp.LimitOptions):
        if options.args.get("percent"):
            modifiers.append("PERCENT")
        if options.args.get("with_ties"):
            modifiers.append("WITH TIES")
    suffix = f" {' '.join(modifiers)}" if modifiers else ""
    return f"LIMIT {_sql_of(expr)}{suffix}"


def _offset_repr(ast: exp.Expression) -> str:
    node = ast.args.get("offset") if isinstance(ast, exp.Query) else None
    if node is None:
        return ""
    expr = getattr(node, "expression", None) or node.args.get("count") or node.args.get("this")
    if expr is None:
        return _sql_of(node)
    return f"OFFSET {_sql_of(expr)}"


def _select_projection_sql(ast: exp.Expression) -> str:
    select = ast.find(exp.Select)
    if not isinstance(select, exp.Select):
        return ""
    return ", ".join(_sql_of(item) for item in select.expressions or [])


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
        and node.find_ancestor(exp.Connect) is None
        and not _is_cross_table_condition(node)
    ]
    stu_comparisons = [
        _comparison_descriptor(node)
        for node in student_ast.find_all(*_comparison_node_types())
        if not _skip(node)
        and not _is_inside_join(node)
        and node.find_ancestor(exp.Connect) is None
        and not _is_cross_table_condition(node)
    ]
    std_comparisons = [item for item in std_comparisons if item]
    stu_comparisons = [item for item in stu_comparisons if item]

    # Index student comparisons by normalised column name; track which have been matched.
    stu_by_col: dict[str, list[tuple[int, dict[str, Any]]]] = {}
    for idx, item in enumerate(stu_comparisons):
        stu_by_col.setdefault(_norm_name(item["column"]), []).append((idx, item))
    stu_matched: set[int] = set()  # indices of student comparisons already paired

    diffs: list[ASTDiffNode] = []
    for std in std_comparisons:
        candidates = stu_by_col.get(_norm_name(std["column"]), [])
        # Pick the first *unmatched* candidate to avoid double-pairing (BUG-2 fix).
        stu: dict[str, Any] | None = None
        stu_idx: int | None = None
        for idx, cand in candidates:
            if idx not in stu_matched:
                stu, stu_idx = cand, idx
                break
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
            isinstance(standard_node, (exp.Like, exp.ILike))
            and isinstance(student_node, (exp.Like, exp.ILike))
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
                    "case_insensitive": isinstance(standard_node, exp.ILike),
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
        same_similar_predicate = (
            isinstance(standard_node, exp.SimilarTo)
            and isinstance(student_node, exp.SimilarTo)
        )
        if same_similar_predicate and (
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
            diffs.append(ASTDiffNode(
                clause_category="PREDICATE",
                diff_type="similar_pattern_changed",
                target_table=target_table,
                target_column=standard_node.this.name,
                standard_node=_like_render_node(standard_node),
                student_node=_like_render_node(student_node),
                knowledge_point_id="similar-to",
                severity=0.72,
                extra={
                    "standard_pattern": std.get("value"),
                    "student_pattern": stu.get("value"),
                    "standard_escape": std.get("escape"),
                    "student_escape": stu.get("escape"),
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
                clause_category="PREDICATE",
                diff_type=diff_type,
                target_column=std["column"],
                standard_node=std.get("node"),
                student_node=stu.get("node"),
                knowledge_point_id="where",
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


def _extract_logical_skeleton(node: exp.Expression) -> dict[str, Any]:
    """Recursively extract the boolean skeleton of a WHERE expression.

    Returns a dict with:
      - ``operators``: sorted list of ``"AND"`` / ``"OR"`` tokens
      - ``leaves``:    sorted list of leaf comparison SQL strings
    """
    operators: list[str] = []
    leaves: list[str] = []

    def _walk(n: exp.Expression) -> None:
        if isinstance(n, exp.And):
            operators.append("AND")
            _walk(n.left)
            _walk(n.right)
        elif isinstance(n, exp.Or):
            operators.append("OR")
            _walk(n.left)
            _walk(n.right)
        elif isinstance(n, exp.Not):
            # Record NOT as a prefix on the leaf so that NOT(a=1) ≠ a=1.
            inner = n.this
            if isinstance(inner, (exp.And, exp.Or)):
                # NOT wrapping a boolean operator: record NOT and recurse
                operators.append("NOT")
                _walk(inner)
            else:
                # NOT wrapping a leaf comparison: serialise the whole NOT expression
                leaves.append(_sql_of(n))
        elif isinstance(n, exp.Paren):
            _walk(n.this)
        else:
            leaves.append(_sql_of(n))

    _walk(node)
    return {
        "operators": sorted(operators),
        "leaves": sorted(leaves),
    }


def _logical_operator_ast_diffs(
    standard_ast: exp.Expression,
    student_ast: exp.Expression,
) -> list[ASTDiffNode]:
    """Detect AND ↔ OR swaps inside WHERE clauses.

    If both queries have the same set of leaf comparisons but connect them
    with different boolean operators, emit ``logical_operator_changed``.
    """
    std_where = standard_ast.args.get("where") or standard_ast.find(exp.Where)
    stu_where = student_ast.args.get("where") or student_ast.find(exp.Where)
    if std_where is None or stu_where is None:
        return []

    std_skel = _extract_logical_skeleton(std_where.this)
    stu_skel = _extract_logical_skeleton(stu_where.this)

    # Different boolean operator structure → logical operator changed.
    # (Previously required identical leaves, but NOT on leaves changes the leaf text.)
    if std_skel["operators"] != stu_skel["operators"] or std_skel["leaves"] != stu_skel["leaves"]:
        # Only report if the structural difference is in the boolean skeleton,
        # not just a simple predicate value change (those are caught by comparison_ast_diffs).
        if std_skel["operators"] != stu_skel["operators"]:
            source = _direct_from_table(_top_select(standard_ast))
            return [ASTDiffNode(
                clause_category="LOGICAL",
                diff_type="logical_operator_changed",
                standard_node=std_where,
                student_node=stu_where,
                knowledge_point_id="where",
                severity=0.8,
                extra={
                    "standard_operators": std_skel["operators"],
                    "student_operators": stu_skel["operators"],
                    "leaves": std_skel["leaves"],
                    "standard_sql": _sql_of(std_where),
                    "student_sql": _sql_of(stu_where),
                    "standard_predicate_sql": _sql_of(std_where.this),
                    "student_predicate_sql": _sql_of(stu_where.this),
                    "standard_source_table": (
                        source.name if isinstance(source, exp.Table) else ""
                    ),
                },
            )]

    return []


def _comparison_node_types() -> tuple[type[exp.Expression], ...]:
    return (
        exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE,
        exp.NullSafeEQ, exp.NullSafeNEQ,
        exp.Like, exp.ILike, exp.Glob, exp.SimilarTo,
        exp.In, exp.Between, exp.Is,
    )


def _is_directly_negated(node: exp.Expression) -> bool:
    parent = node.parent
    return isinstance(parent, exp.Not) and parent.this is node


def _predicate_negation_ast_diffs(
    standard_ast: exp.Expression,
    student_ast: exp.Expression,
) -> list[ASTDiffNode]:
    """Keep predicate negation visible inside JOIN and CASE expressions.

    ``NOT`` is the parent of ``IN``/``IS`` in sqlglot, so comparing only the
    predicate node loses ``IN`` versus ``NOT IN`` and ``IS NULL`` versus
    ``IS NOT NULL``.  These predicates can also live outside WHERE, where the
    generic comparison pass intentionally does not inspect them.
    """
    diffs: list[ASTDiffNode] = []
    specs = (
        (exp.In, "IN", "in_predicate_negation_changed", "in-list"),
        (exp.Is, "NULL", "null_predicate_negation_changed", "null-handling"),
    )
    for node_type, clause, diff_type, kp_id in specs:
        standard_nodes = list(standard_ast.find_all(node_type))
        student_nodes = list(student_ast.find_all(node_type))
        for standard_node, student_node in zip(standard_nodes, student_nodes):
            standard_negated = _is_directly_negated(standard_node)
            student_negated = _is_directly_negated(student_node)
            if standard_negated == student_negated:
                continue
            standard_render_node = standard_node.parent if standard_negated else standard_node
            student_render_node = student_node.parent if student_negated else student_node
            standard_in = standard_node
            standard_inner = standard_in.args.get("query") if isinstance(standard_in, exp.In) else None
            standard_inner_select = (
                standard_inner.this
                if isinstance(standard_inner, exp.Subquery)
                and isinstance(standard_inner.this, exp.Select)
                else None
            )
            standard_outer_select = standard_node.find_ancestor(exp.Select)
            standard_outer_source = _direct_from_table(standard_outer_select)
            standard_inner_source = _direct_from_table(standard_inner_select)
            standard_projected = (
                standard_inner_select.expressions[0]
                if isinstance(standard_inner_select, exp.Select)
                and standard_inner_select.expressions
                else None
            )
            standard_projected = (
                standard_projected.this
                if isinstance(standard_projected, exp.Alias)
                else standard_projected
            )
            diffs.append(ASTDiffNode(
                clause_category=clause,
                diff_type=diff_type,
                target_column=_extract_column_name(standard_node),
                standard_node=standard_render_node,
                student_node=student_render_node,
                knowledge_point_id=kp_id,
                severity=0.82,
                extra={
                    "standard_negated": standard_negated,
                    "student_negated": student_negated,
                    "standard_sql": _sql_of(standard_render_node),
                    "student_sql": _sql_of(student_render_node),
                    "standard_source_table": (
                        standard_outer_source.name
                        if isinstance(standard_outer_source, exp.Table)
                        else ""
                    ),
                    "standard_membership_table": (
                        standard_inner_source.name
                        if isinstance(standard_inner_source, exp.Table)
                        else ""
                    ),
                    "standard_outer_column": _extract_column_name(standard_in.this),
                    "standard_membership_column": _extract_column_name(standard_projected),
                    "standard_in_values": tuple(
                        _literal_value(item)
                        for item in standard_in.expressions
                        if isinstance(item, exp.Literal)
                    ) if isinstance(standard_in, exp.In) else (),
                },
            ))
    return diffs


def _like_render_node(node: exp.Expression) -> exp.Expression:
    parent = node.parent
    return (
        parent
        if isinstance(parent, exp.Escape) and parent.this is node
        else node
    )


def _like_escape_value(node: exp.Expression) -> str:
    parent = node.parent
    if isinstance(parent, exp.Escape) and parent.this is node:
        value = _literal_value(parent.expression)
        return value if isinstance(value, str) else "\\"
    return "\\"


def _comparison_descriptor(node: exp.Expression) -> dict[str, Any] | None:
    if isinstance(node, exp.In) and isinstance(node.this, exp.Column):
        values = [_literal_value(item) for item in node.expressions if isinstance(item, exp.Literal)]
        return {"column": node.this.name, "op": "IN", "value": values[0] if values else None, "values": values, "value_kind": "literal", "sql": _sql_of(node), "node": node}
    if isinstance(node, exp.Between) and isinstance(node.this, exp.Column):
        return {
            "column": node.this.name,
            "op": "BETWEEN",
            "value": _literal_value(node.args.get("low")),
            "high": _literal_value(node.args.get("high")),
            "value_kind": "literal",
            "sql": _sql_of(node),
            "node": node,
        }
    if isinstance(node, exp.Is) and isinstance(node.this, exp.Column):
        return {"column": node.this.name, "op": "IS", "value": None, "value_is_null": True, "value_kind": "literal", "sql": _sql_of(node), "node": node}
    if isinstance(node, (exp.Like, exp.ILike)) and isinstance(node.this, exp.Column):
        return {
            "column": node.this.name,
            "op": "ILIKE" if isinstance(node, exp.ILike) else "LIKE",
            "value": _literal_value(node.expression),
            "escape": _like_escape_value(node),
            "value_kind": "literal",
            "sql": _sql_of(node),
            "node": node,
        }
    if isinstance(node, exp.Glob) and isinstance(node.this, exp.Column):
        return {
            "column": node.this.name,
            "op": "GLOB",
            "value": _literal_value(node.expression),
            "value_kind": "literal",
            "sql": _sql_of(node),
            "node": node,
        }
    if isinstance(node, exp.SimilarTo) and isinstance(node.this, exp.Column):
        return {
            "column": node.this.name,
            "op": "SIMILAR TO",
            "value": _literal_value(node.expression),
            "escape": _like_escape_value(node),
            "value_kind": "literal",
            "sql": _sql_of(node),
            "node": node,
        }
    left, right = getattr(node, "left", None), getattr(node, "right", None)
    if isinstance(left, exp.Column) and isinstance(right, exp.Column):
        return {
            "column": left.name,
            "left_table": left.table,
            "op": type(node).__name__.upper(),
            "value": _sql_of(right),
            "value_kind": "column",
            "right_column": right.name,
            "right_table": right.table,
            "sql": _sql_of(node),
            "node": node,
        }
    if isinstance(left, exp.Column) and isinstance(right, (exp.Literal, exp.Null)):
        return {
            "column": left.name,
            "op": type(node).__name__.upper(),
            "value": _literal_value(right),
            "value_kind": "literal",
            "value_is_null": isinstance(right, exp.Null),
            "sql": _sql_of(node),
            "node": node,
        }
    if isinstance(right, exp.Column) and isinstance(left, (exp.Literal, exp.Null)):
        return {
            "column": right.name,
            "op": type(node).__name__.upper(),
            "value": _literal_value(left),
            "value_kind": "literal",
            "value_is_null": isinstance(left, exp.Null),
            "sql": _sql_of(node),
            "node": node,
        }
    if isinstance(left, exp.Column) and right is not None:
        return {
            "column": left.name,
            "op": type(node).__name__.upper(),
            "value": _sql_of(right),
            "value_kind": "expression",
            "sql": _sql_of(node),
            "node": node,
        }
    if isinstance(right, exp.Column) and left is not None:
        return {
            "column": right.name,
            "op": type(node).__name__.upper(),
            "value": _sql_of(left),
            "value_kind": "expression",
            "sql": _sql_of(node),
            "node": node,
        }
    # Fallback: any expression on the left (function call, arithmetic, etc.)
    # compared to a literal on the right.  E.g. YEAR(hire_date) = 2020, x + 1 > 5.
    if left is not None and isinstance(right, (exp.Literal, exp.Null)):
        col_name = _extract_column_name(left)
        return {
            "column": col_name or _sql_of(left),
            "op": type(node).__name__.upper(),
            "value": _literal_value(right),
            "value_kind": "literal",
            "value_is_null": isinstance(right, exp.Null),
            "sql": _sql_of(node),
            "node": node,
        }
    # Mirror: literal on the left, expression on the right.
    if right is not None and isinstance(left, (exp.Literal, exp.Null)):
        col_name = _extract_column_name(right)
        return {
            "column": col_name or _sql_of(right),
            "op": type(node).__name__.upper(),
            "value": _literal_value(left),
            "value_kind": "literal",
            "value_is_null": isinstance(left, exp.Null),
            "sql": _sql_of(node),
            "node": node,
        }
    return None


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
                    "standard_sql": next(iter(missing), ""),
                    "student_sql": next(iter(added), ""),
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


def _extract_join_graph(ast: exp.Expression) -> dict[str, Any]:
    """Extract a normalised join graph from a query.

    Both explicit (``JOIN ... ON``) and implicit (``FROM a, b WHERE ...``)
    styles produce the same structure so they compare equal when
    semantically equivalent.

    Returns::

        {
            "joins": [(right_table, join_type, node), ...],
            "conditions": [sorted ON/condition SQL strings],
            "from_tables": [tables in FROM clause],
        }
    """
    joins: list[tuple[str, str, Any]] = []
    conditions: list[str] = []

    has_explicit_on = False  # True if any Join has an ON clause

    # ── JOIN nodes (explicit JOIN ... ON and implicit FROM a, b) ──
    for join_node in ast.find_all(exp.Join):
        jn = join_node.this
        if isinstance(jn, exp.Table):
            table = jn.name
        elif isinstance(jn, exp.Subquery) and jn.alias:
            table = jn.alias
        else:
            table = ""
        side = str(join_node.args.get("side") or join_node.args.get("kind") or "INNER").upper()
        joins.append((table, side, join_node))
        on = join_node.args.get("on")
        if on:
            has_explicit_on = True
            for pred in _flatten_and(on):
                conditions.append(_sql_of(pred))
        using = join_node.args.get("using") or []
        if using:
            # sqlglot stores USING columns as Identifier nodes rather than an
            # expression. Include each key in the same condition signature used
            # for ON predicates so changes such as USING(id) -> USING(code) are
            # visible to the AST diff graph.
            has_explicit_on = True
            conditions.append(
                f"USING ({', '.join(_sql_of(column) for column in using)})"
            )

    # ── FROM clause tables ──
    # Only extract the direct child of FROM (don't recurse into subqueries).
    from_clause = ast.args.get("from_") or ast.args.get("from")
    from_tables: list[str] = []
    if isinstance(from_clause, exp.From):
        child = from_clause.this
        if isinstance(child, exp.Table):
            from_tables.append(child.name)
        elif isinstance(child, exp.Subquery) and child.alias:
            from_tables.append(child.alias)

    # All known table names (FROM + Join nodes)
    all_tables = set(from_tables) | {t for t, _, _ in joins}

    # ── Implicit join: extract cross-table conditions from WHERE ──
    # sqlglot represents FROM a, b as From(a) + Join(b, no ON).
    # If no Join had an ON clause, cross-table WHERE predicates are join conditions.
    if not has_explicit_on and len(all_tables) > 1:
        where = ast.args.get("where") or ast.find(exp.Where)
        if where:
            for pred in _flatten_and(where.this):
                # Only EQ cross-table predicates are join conditions;
                # OR nodes and non-equality comparisons are filters, not joins.
                if _is_cross_table_condition(pred):
                    conditions.append(_sql_of(pred))

    if conditions:
        joins = [
            (table, "INNER" if side == "CROSS" else side, node)
            for table, side, node in joins
        ]

    return {
        "joins": joins,
        "conditions": sorted(conditions),
        "from_tables": sorted(from_tables),
    }


def _set_operator_ast_diffs(standard_ast: exp.Expression, student_ast: exp.Expression) -> list[ASTDiffNode]:
    std_op = _set_operator_name(standard_ast)
    stu_op = _set_operator_name(student_ast)
    std_node = _set_operator_node(standard_ast)
    stu_node = _set_operator_node(student_ast)
    std_modifier = _set_operator_modifier(std_node)
    stu_modifier = _set_operator_modifier(stu_node)
    def branch_metadata(node: exp.Expression | None) -> dict[str, Any]:
        select = node if isinstance(node, exp.Select) else node.find(exp.Select) if isinstance(node, exp.Expression) else None
        source = _direct_from_table(select)
        projection = []
        if isinstance(select, exp.Select):
            for item in select.expressions or ():
                expression = item.this if isinstance(item, exp.Alias) else item
                if isinstance(expression, exp.Column):
                    projection.append(expression.name)
        return {
            "source_table": source.name if isinstance(source, exp.Table) else "",
            "projection_columns": tuple(projection),
        }
    standard_left = branch_metadata(std_node.this if isinstance(std_node, exp.SetOperation) else None)
    standard_right = branch_metadata(std_node.expression if isinstance(std_node, exp.SetOperation) else None)
    student_left = branch_metadata(stu_node.this if isinstance(stu_node, exp.SetOperation) else None)
    student_right = branch_metadata(stu_node.expression if isinstance(stu_node, exp.SetOperation) else None)
    # No set operator in either → no diff
    if not std_op and not stu_op:
        return []
    # Detect both operator changes and duplicate semantics (UNION vs UNION ALL).
    if std_op != stu_op or std_modifier != stu_modifier:
        kp = _set_operator_kp(std_op or stu_op)
        diffs = [ASTDiffNode(
            clause_category=std_op or stu_op,
            diff_type="set_operator_changed",
            standard_node=std_node or standard_ast,
            student_node=stu_node or student_ast,
            knowledge_point_id=kp,
            extra={
                "standard_op": std_op,
                "student_op": stu_op,
                "standard_modifier": std_modifier,
                "student_modifier": stu_modifier,
                "standard_sql": _sql_of(std_node),
                "student_sql": _sql_of(stu_node),
                "standard_left_source_table": standard_left["source_table"],
                "standard_right_source_table": standard_right["source_table"],
                "standard_projection_columns": standard_left["projection_columns"],
                "student_left_source_table": student_left["source_table"],
                "student_right_source_table": student_right["source_table"],
                "student_projection_columns": student_left["projection_columns"],
            }
        )]
        if std_op == stu_op and std_modifier != stu_modifier:
            diffs.append(ASTDiffNode(
                clause_category=std_op or "SET OPERATION",
                diff_type="set_modifier_changed",
                standard_node=std_node,
                student_node=stu_node,
                knowledge_point_id=kp,
                severity=0.78,
                extra={
                    "operator": std_op,
                    "standard_modifier": std_modifier,
                    "student_modifier": stu_modifier,
                    "standard_sql": _sql_of(std_node),
                    "student_sql": _sql_of(stu_node),
                },
            ))
        return diffs
    return []


def _set_operator_name(ast: exp.Expression | None) -> str | None:
    if ast is None:
        return None
    if isinstance(ast, exp.Intersect) or ast.find(exp.Intersect):
        return "INTERSECT"
    if isinstance(ast, exp.Except) or ast.find(exp.Except):
        return "EXCEPT"
    if isinstance(ast, exp.Union) or ast.find(exp.Union):
        return "UNION"
    return None


def _set_operator_node(ast: exp.Expression | None) -> exp.Expression | None:
    if ast is None:
        return None
    if isinstance(ast, (exp.Union, exp.Intersect, exp.Except)):
        return ast
    return ast.find(exp.Union, exp.Intersect, exp.Except)


def _set_operator_modifier(node: exp.Expression | None) -> str | None:
    if not isinstance(node, (exp.Union, exp.Intersect, exp.Except)):
        return None
    return "ALL" if node.args.get("distinct") is False else "DISTINCT"


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
        diffs.append(ASTDiffNode(
            clause_category="WINDOW",
            diff_type="window_over_changed",
            standard_node=std_windows[0] if std_windows else None,
            student_node=stu_windows[0] if stu_windows else None,
            knowledge_point_id="window-row-number",
            extra={
                "standard_count": len(std_windows),
                "student_count": len(stu_windows),
                "standard_sql": " | ".join(_sql_of(node) for node in std_windows),
                "student_sql": " | ".join(_sql_of(node) for node in stu_windows),
            },
        ))
    return diffs


def _window_spec(node: exp.Window) -> dict[str, Any]:
    order_items: list[tuple[str, bool, bool]] = []
    order_columns: list[str] = []
    order = node.args.get("order")
    if isinstance(order, exp.Order):
        for item in order.expressions or ():
            ordered = item if isinstance(item, exp.Ordered) else None
            expression = ordered.this if ordered is not None else item
            if not isinstance(expression, exp.Expression):
                continue
            descending = bool(ordered.args.get("desc")) if ordered is not None else False
            # sqlglot normalizes SQLite's implicit NULL placement into the
            # Ordered node.  Keeping the semantic value here makes
            # ``ASC NULLS FIRST`` equal to plain ASC while preserving an
            # actual NULLS FIRST/LAST change.
            nulls_first = (
                bool(ordered.args.get("nulls_first"))
                if ordered is not None and ordered.args.get("nulls_first") is not None
                else not descending
            )
            expression_sql = _sql_of(expression)
            order_items.append((expression_sql, descending, nulls_first))
            if isinstance(expression, exp.Column):
                order_columns.append(_sql_of(expression))
    return {
        "partition_by": [_sql_of(item) for item in (node.args.get("partition_by") or [])],
        "order": _sql_of(node.args.get("order")),
        "frame": _sql_of(node.args.get("spec")),
        "order_items": tuple(order_items),
        "order_columns": tuple(order_columns),
    }


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
                    diffs.append(diff)
            return diffs
    return []


def _case_ast_diffs(
    standard_ast: exp.Expression,
    student_ast: exp.Expression,
    filter_subqueries: bool = True,
) -> list[ASTDiffNode]:
    """Detect direct CASE expression changes.

    Clause-level SELECT diffs can already reveal CASE changes, but that loses
    the teaching structure. This emits an explicit CASE diff so downstream
    feedback can point students to WHEN/THEN/ELSE logic.
    """
    _skip = _is_inside_subquery if filter_subqueries else (lambda _: False)
    std_cases = [_sql_of(node) for node in standard_ast.find_all(exp.Case) if not _skip(node)]
    stu_cases = [_sql_of(node) for node in student_ast.find_all(exp.Case) if not _skip(node)]
    if std_cases != stu_cases:
        standard_case = next(
            (node for node in standard_ast.find_all(exp.Case) if not _skip(node)),
            None,
        )
        student_case = next(
            (node for node in student_ast.find_all(exp.Case) if not _skip(node)),
            None,
        )
        standard_source = _direct_from_table(_top_select(standard_ast))
        case_metadata = {
            "standard_case_when_predicates": tuple(
                _sql_of(item.this)
                for item in (standard_case.args.get("ifs") or ())
                if isinstance(item, exp.If)
            ) if isinstance(standard_case, exp.Case) else (),
            "student_case_when_predicates": tuple(
                _sql_of(item.this)
                for item in (student_case.args.get("ifs") or ())
                if isinstance(item, exp.If)
            ) if isinstance(student_case, exp.Case) else (),
            "standard_source_table": (
                standard_source.name if isinstance(standard_source, exp.Table) else ""
            ),
        }
        diffs = [ASTDiffNode(
            clause_category="CASE",
            diff_type="case_changed",
            standard_node=standard_ast.find(exp.Case),
            student_node=student_ast.find(exp.Case),
            knowledge_point_id="case",
            severity=0.68,
            extra={
                "standard_sql": " | ".join(std_cases) if std_cases else "",
                "student_sql": " | ".join(stu_cases) if stu_cases else "",
                **case_metadata,
            },
        )]
        std_nodes = [node for node in standard_ast.find_all(exp.Case) if not _skip(node)]
        stu_nodes = [node for node in student_ast.find_all(exp.Case) if not _skip(node)]
        for std_node, stu_node in zip(std_nodes, stu_nodes):
            std_default = std_node.args.get("default")
            stu_default = stu_node.args.get("default")
            if bool(std_default) != bool(stu_default):
                diffs.append(ASTDiffNode(
                    clause_category="CASE",
                    diff_type="case_else_missing" if std_default and not stu_default else "case_else_added",
                    standard_node=std_default,
                    student_node=stu_default,
                    knowledge_point_id="case",
                    severity=0.78,
                    extra={
                        "standard_sql": _sql_of(std_default),
                        "student_sql": _sql_of(stu_default),
                        **case_metadata,
                    },
                ))
            std_ifs = std_node.args.get("ifs") or []
            stu_ifs = stu_node.args.get("ifs") or []
            if len(std_ifs) != len(stu_ifs):
                diffs.append(ASTDiffNode(
                    clause_category="CASE",
                    diff_type="case_when_missing" if len(std_ifs) > len(stu_ifs) else "case_when_added",
                    standard_node=std_node,
                    student_node=stu_node,
                    knowledge_point_id="case",
                    severity=0.78,
                    extra={
                        "standard_when_count": len(std_ifs),
                        "student_when_count": len(stu_ifs),
                        **case_metadata,
                    },
                ))
        return diffs
    return []


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
    if std_contexts == stu_contexts:
        return []
    if _subquery_membership_key_ast_diffs(standard_ast, student_ast):
        # The correlation itself is unchanged; only a surrounding IN lhs is
        # wrong.  Let the nested-membership obligation own that distinction.
        return []
    standard_links = _correlated_subquery_links(standard_ast)
    student_links = _correlated_subquery_links(student_ast)
    standard_pairs = {item[:2] for item in standard_links}
    student_pairs = {item[:2] for item in student_links}
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


def _subquery_predicate_context_sql(node: exp.Expression) -> str:
    current: exp.Expression = node
    parent = current.parent
    while parent is not None:
        if isinstance(parent, (exp.Where, exp.Having)):
            return _sql_of(current)
        if isinstance(parent, exp.Join):
            return _sql_of(current)
        current = parent
        parent = parent.parent
    return _sql_of(node)


_AGG_FUNC_TYPES: tuple[type[exp.Expression], ...] = (
    exp.Count, exp.Sum, exp.Avg, exp.Min, exp.Max,
    exp.Stddev, exp.Variance, exp.GroupConcat,
)


def _aggregate_function_ast_diffs(
    standard_ast: exp.Expression,
    student_ast: exp.Expression,
    filter_subqueries: bool = True,
) -> list[ASTDiffNode]:
    """Detect when the same column uses a different aggregate function.

    E.g. ``AVG(score)`` → ``SUM(score)`` produces
    ``aggregate_function_changed`` with ``target_column="score"``.
    """
    _skip = _is_inside_subquery if filter_subqueries else (lambda _: False)
    def _collect_aggs(ast: exp.Expression) -> dict[str, tuple[str, exp.Expression]]:
        """Map column_name → (func_name, node) for each aggregate."""
        result: dict[str, tuple[str, exp.Expression]] = {}
        for node in ast.find_all(*_AGG_FUNC_TYPES):
            if _skip(node):
                continue
            col = node.find(exp.Column)
            col_name = col.name if col else "*"
            func_name = type(node).__name__.upper()
            result[col_name] = (func_name, node)
        return result

    std_aggs = _collect_aggs(standard_ast)
    stu_aggs = _collect_aggs(student_ast)

    diffs: list[ASTDiffNode] = []
    for col_name, (std_func, std_node) in std_aggs.items():
        if col_name in stu_aggs:
            stu_func, stu_node = stu_aggs[col_name]
            if std_func != stu_func:
                diffs.append(ASTDiffNode(
                    clause_category="AGGREGATE",
                    diff_type="aggregate_function_changed",
                    target_column=col_name,
                    standard_node=std_node,
                    student_node=stu_node,
                    knowledge_point_id="aggregate",
                    severity=0.7,
                    extra={
                        "standard_func": std_func,
                        "student_func": stu_func,
                        "column": col_name,
                        "standard_sql": _sql_of(std_node),
                        "student_sql": _sql_of(stu_node),
                        "standard_aggregate_function": std_func,
                        "student_aggregate_function": stu_func,
                        "standard_aggregate_argument": _sql_of(std_node.this) if std_node.this is not None else "*",
                        "student_aggregate_argument": _sql_of(stu_node.this) if stu_node.this is not None else "*",
                        "standard_group_columns": [sql for sql, _ in _group_by_items(standard_ast)],
                        "student_group_columns": [sql for sql, _ in _group_by_items(student_ast)],
                    },
                ))
    return diffs


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


def _generation_tactics_from_ast_diffs(ast_diffs: list[ASTDiffNode]) -> list[dict[str, Any]]:
    mapping = {
        "projection_changed": "projection_shape_check",
        "column_dropped": "projection_shape_check",
        "column_added": "projection_shape_check",
        "star_mismatch": "projection_shape_check",
        "alias_changed": "output_alias_check",
        "function_argument_changed": "function_argument_boundary_probe",
        "where_changed": "predicate_counterexample",
        "comparison_operator_changed": "comparison_boundary_tristate",
        "predicate_expression_operator_changed": "arithmetic_expression_boundary_probe",
        "logical_operator_changed": "predicate_positive_negative_probe",
        "logical_precedence_tree_changed": "logical_truth_table_probe",
        "literal_changed": "literal_boundary_tristate",
        "predicate_missing": "predicate_positive_negative_probe",
        "predicate_added": "predicate_positive_negative_probe",
        "null_equality_changed": "null_probe",
        "in_list_member_removed": "in_list_membership_probe",
        "in_list_member_added": "in_list_membership_probe",
        "distinct_changed": "duplicate_projection_probe",
        "join_on_changed": "join_key_drift_probe",
        "join_predicate_placement_changed": "join_predicate_placement_probe",
        "join_type_changed": "outer_join_dangling_tuple_probe",
        "join_missing": "outer_join_dangling_tuple_probe",
        "group_by_changed": "group_cardinality_probe",
        "group_by_expression_changed": "group_cross_product_probe",
        "grouping_grain_too_fine": "group_cross_product_probe",
        "grouping_grain_too_coarse": "group_cross_product_probe",
        "having_changed": "aggregate_boundary_probe",
        "aggregate_condition_in_where": "aggregate_clause_placement_probe",
        "aggregate_function_changed": "aggregate_boundary_probe",
        "aggregate_argument_changed": "aggregate_argument_probe",
        "order_by_changed": "ordered_compare_probe",
        "order_by_tiebreaker_missing": "ordered_tie_probe",
        "order_direction_changed": "ordered_compare_probe",
        "order_by_key_added": "ordered_tie_probe",
        "limit_changed": "limit_row_count_probe",
        "set_operator_changed": "set_operator_overlap_probe",
        "set_modifier_changed": "set_operator_overlap_probe",
        "window_over_changed": "window_partition_order_probe",
        "window_function_changed": "window_rank_tie_probe",
        "cte_changed": "cte_base_constraint_probe",
        "recursive_cte_changed": "recursive_cte_boundary_probe",
        "recursive_step_expression_changed": "recursive_cte_boundary_probe",
        "case_changed": "case_branch_probe",
        "case_else_missing": "case_unmatched_row_probe",
        "case_else_added": "case_unmatched_row_probe",
        "case_when_missing": "case_branch_probe",
        "case_when_added": "case_branch_probe",
        "subquery_added": "subquery_equivalence_probe",
        "subquery_removed": "subquery_equivalence_probe",
        "correlated_predicate_changed": "correlated_subquery_path_probe",
        "like_pattern_changed": "like_pattern_separation",
        "glob_pattern_changed": "glob_pattern_separation",
        "similar_pattern_changed": "similar_pattern_separation",
    }
    tactics = []
    for index, diff in enumerate(ast_diffs):
        name = mapping.get(diff.diff_type)
        if name:
            diff_id = stable_diff_id(diff, index)
            tactics.append(
                {
                    "tactic": name,
                    "clause": diff.clause_category,
                    "diff_type": diff.diff_type,
                    "diff_id": diff_id,
                    "obligation_id": f"obligation_{diff_id.removeprefix('diff_')}",
                }
            )
    return tactics


def _has_diff(ast_diffs: list[ASTDiffNode], clause: str) -> bool:
    return any(diff.clause_category == clause for diff in ast_diffs)


def _world_has_diff(
    ast_diffs: Iterable[ASTDiffNode],
    *,
    clauses: Iterable[str] = (),
    diff_types: Iterable[str] = (),
) -> bool:
    """Return whether a legacy probe belongs to this witness world.

    ``generate_test_database`` is also used as the compatibility materializer
    for a single world.  A number of old probes only received the complete SQL
    pair and therefore used to activate from SQL text alone, leaking their
    writes into every isolated world.  This small gate keeps the migration
    bounded: probes may still use the full SQL to understand their own query,
    but they are called only when the selected world owns a matching AST
    obligation.
    """
    clause_keys = {
        str(item or "").strip().upper().replace("_", " ")
        for item in clauses
        if item
    }
    diff_keys = {str(item) for item in diff_types if item}
    for diff in ast_diffs:
        clause = str(getattr(diff, "clause_category", "") or "").upper().replace("_", " ")
        if clause in clause_keys or getattr(diff, "diff_type", None) in diff_keys:
            return True
    return False


def _extract_table_names(sql: str) -> set[str]:
    return {_norm_name(table) for table in extract_physical_table_names(sql)}


def _extract_literal_constraints(sql: str) -> list[dict[str, Any]]:
    ast = _parse_sql(sql)
    if not ast:
        return []
    constraints: list[dict[str, Any]] = []
    for node in ast.find_all(exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE):
        if _is_inside_subquery(node):
            continue
        left, right = node.left, node.right
        right_value = _expression_static_value(right)
        left_value = _expression_static_value(left)
        if isinstance(left, exp.Column) and right_value is not None:
            constraints.append({"column": left.name, "op": type(node).__name__, "value": right_value,
                                "table": left.table or None})
        elif isinstance(right, exp.Column) and left_value is not None:
            constraints.append({"column": right.name, "op": type(node).__name__, "value": left_value,
                                "table": right.table or None})
    for node in ast.find_all(exp.Like):
        if _is_inside_subquery(node):
            continue
        if isinstance(node.this, exp.Column) and isinstance(node.expression, exp.Literal):
            constraints.append({"column": node.this.name, "op": "LIKE", "value": _literal_value(node.expression),
                                "table": node.this.table or None})
    for node in ast.find_all(exp.In):
        if _is_inside_subquery(node):
            continue
        if isinstance(node.this, exp.Column):
            values = [_literal_value(item) for item in node.expressions if isinstance(item, exp.Literal)]
            if values:
                constraints.append({"column": node.this.name, "op": "IN", "value": values[0], "values": values,
                                    "table": node.this.table or None})
    for node in ast.find_all(exp.Between):
        if _is_inside_subquery(node):
            continue
        if isinstance(node.this, exp.Column):
            low_val = _expression_static_value(node.args.get("low"))
            high_val = _expression_static_value(node.args.get("high"))
            constraints.append({"column": node.this.name, "op": "BETWEEN", "value": low_val, "high": high_val,
                                "table": node.this.table or None})
            constraints.append({"column": node.this.name, "op": "BETWEEN", "value": high_val, "high": low_val,
                                "table": node.this.table or None})
    for node in ast.find_all(exp.Is):
        if _is_inside_subquery(node):
            continue
        if isinstance(node.this, exp.Column):
            is_not_null = isinstance(node.expression, exp.Not) or (
                hasattr(node, "args") and node.args.get("not")
            )
            constraints.append({
                "column": node.this.name,
                "op": "IS_NOT_NULL" if is_not_null else "IS_NULL",
                "value": None,
                "table": node.this.table or None
            })
    for node in ast.find_all(exp.NullSafeEQ, exp.NullSafeNEQ):
        if _is_inside_subquery(node):
            continue
        column = node.left if isinstance(node.left, exp.Column) else node.right
        if isinstance(column, exp.Column):
            constraints.append({
                "column": column.name,
                "op": "NULL_SAFE_COMPARISON",
                "value": None,
                "table": column.table or None,
            })
    # Handle NOT(IS NULL) pattern
    for node in ast.find_all(exp.Not):
        if _is_inside_subquery(node):
            continue
        inner = node.this
        if isinstance(inner, exp.Is) and isinstance(inner.this, exp.Column):
            constraints.append({
                "column": inner.this.name,
                "op": "IS_NOT_NULL",
                "value": None,
                "table": inner.this.table or None
            })
    return constraints


def _expression_static_value(node: exp.Expression | None) -> Any:
    if node is None:
        return None
    literal = _literal_value(node)
    if isinstance(node, exp.Literal):
        return literal
    if isinstance(node, exp.Neg):
        value = _expression_static_value(node.this)
        if isinstance(value, (int, float, Decimal)):
            return -value
        return None
    if isinstance(node, exp.Parameter):
        name = str(node.this.this) if isinstance(node.this, exp.Var) else str(node.this)
        value = _parameter_literal(name)
        if value.startswith("'") and value.endswith("'"):
            return value[1:-1]
        return _integer_node_value(exp.Literal.number(value))
    if isinstance(node, exp.Column) and not node.table:
        identifier = node.this
        if isinstance(identifier, exp.Identifier) and identifier.args.get("quoted"):
            return node.name
    if isinstance(node, exp.Anonymous):
        name = str(node.this or "").upper()
        if name in {"GETDATE", "NOW"}:
            return "2024-02-01"
        if name == "DATEADD" and len(node.expressions or []) >= 3:
            part_node, amount_node, value_node = node.expressions[:3]
            part = _date_part_name(part_node)
            amount = _expression_static_value(amount_node)
            value = _expression_static_value(value_node)
            if part and isinstance(amount, (int, float, Decimal)) and value is not None:
                return _sql_date_add(part, amount, value)
    if isinstance(node, (exp.Year, exp.Month, exp.Day)):
        value = _expression_static_value(node.this)
        if value is not None:
            return _sql_date_part(type(node).__name__.lower(), value)
    return None


def _date_part_name(node: exp.Expression | None) -> str | None:
    if node is None:
        return None
    if isinstance(node, exp.Column):
        return node.name.lower()
    if isinstance(node, exp.Var):
        return str(node.this).lower()
    value = _literal_value(node)
    return str(value).strip("'\"").lower() if value is not None else None


def _literal_value(node: exp.Expression | None) -> Any:
    if node is None:
        return None
    if isinstance(node, exp.Literal):
        value = node.this
        if node.is_number:
            try:
                return int(value)
            except Exception:
                try:
                    return float(value)
                except Exception:
                    return value
        return value
    # A SQL expression is not a database literal.  In particular, sqlglot's
    # Subquery node exposes its inner SELECT through ``this``; returning that
    # object here used to leak strings such as ``(SELECT AVG(...))`` into
    # numeric columns.  Only callers that explicitly understand an expression
    # may evaluate it; the generic literal extractor must fail closed.
    return None


def _column_lookup(columns: list[str]) -> dict[str, str]:
    return {_norm_name(col): col for col in columns}


def _distinct_projection_columns(standard_sql: str, student_sql: str, columns: list[str]) -> list[str]:
    lookup = _column_lookup(columns)
    projected: list[str] = []
    for sql in (standard_sql, student_sql):
        ast = _parse_sql(sql)
        select = ast.find(exp.Select) if ast else None
        if not isinstance(select, exp.Select):
            continue
        distinct_items: list[exp.Expression] = []
        if select.args.get("distinct"):
            distinct_items.extend(select.expressions or [])
        for distinct in select.find_all(exp.Distinct):
            if distinct.this is not None:
                distinct_items.append(distinct.this)
            distinct_items.extend(distinct.expressions or [])
        for item in distinct_items:
            column = item if isinstance(item, exp.Column) else item.find(exp.Column)
            if isinstance(column, exp.Column):
                resolved = lookup.get(_norm_name(column.name))
                if resolved and resolved not in projected:
                    projected.append(resolved)
    return projected


def _has_set_operator(*sqls: str) -> bool:
    set_types = tuple(
        item for item in (getattr(exp, "Union", None), getattr(exp, "Intersect", None), getattr(exp, "Except", None))
        if item is not None
    )
    if not set_types:
        return False
    for sql in sqls:
        ast = _parse_sql(sql)
        if ast and (isinstance(ast, set_types) or ast.find(*set_types)):
            return True
    return False


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


def _extract_having_aggregate_specs(sql: str) -> list[dict[str, Any]]:
    ast = _parse_sql(sql)
    if not ast:
        return []
    specs: list[dict[str, Any]] = []
    agg_names = {
        exp.Sum: "SUM", exp.Avg: "AVG", exp.Min: "MIN", exp.Max: "MAX", exp.Count: "COUNT",
    }
    for having in ast.find_all(exp.Having):
        select = having.parent
        while select is not None and not isinstance(select, exp.Select):
            select = select.parent
        group = select.args.get("group") if isinstance(select, exp.Select) else None
        group_columns = [item.name for item in group.expressions if isinstance(item, exp.Column)] if isinstance(group, exp.Group) else []
        if not group_columns:
            continue
        for comparison in having.find_all(exp.GT, exp.GTE, exp.LT, exp.LTE, exp.EQ, exp.NEQ):
            left_agg = comparison.left if isinstance(comparison.left, exp.AggFunc) else comparison.left.find(exp.AggFunc)
            right_agg = comparison.right if isinstance(comparison.right, exp.AggFunc) else comparison.right.find(exp.AggFunc)
            agg = left_agg or right_agg
            literal = comparison.right if agg is left_agg else comparison.left
            if not isinstance(agg, exp.AggFunc) or not isinstance(literal, exp.Literal):
                continue
            boundary = _literal_value(literal)
            if not isinstance(boundary, (int, float, Decimal)):
                continue
            agg_name = next((name for agg_type, name in agg_names.items() if isinstance(agg, agg_type)), type(agg).__name__.upper())
            value_column = agg.find(exp.Column)
            specs.append({
                "agg": agg_name,
                "column": value_column.name if isinstance(value_column, exp.Column) else group_columns[0],
                "group_column": group_columns[0],
                "group_columns": group_columns,
                "boundary": boundary,
                "operator": type(comparison).__name__.upper(),
                "distinct": bool(agg.args.get("distinct") or isinstance(agg.this, exp.Distinct)),
            })
    return specs


def _extract_having_aggregate_spec(sql: str) -> dict[str, Any] | None:
    specs = _extract_having_aggregate_specs(sql)
    return specs[0] if specs else None


def _changed_having_aggregate_spec_for_diffs(
    standard_sql: str,
    student_sql: str,
    ast_diffs: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Select the aggregate owned by the current isolated witness world."""
    specs = _extract_having_aggregate_specs(standard_sql)
    if not specs:
        return None
    targeted_sql = [
        str(diff.get("standard_sql") or "").replace(" ", "").upper()
        for diff in ast_diffs
        if diff.get("diff_type") in {
            "comparison_operator_changed",
            "literal_changed",
            "aggregate_function_changed",
            "aggregate_argument_changed",
        }
    ]
    for spec in specs:
        signature = f"{spec.get('agg', '')}({spec.get('column', '')})".replace(" ", "").upper()
        if any(signature in sql for sql in targeted_sql):
            return spec
    return _changed_having_aggregate_spec(standard_sql, student_sql)


def _changed_having_aggregate_spec(standard_sql: str, student_sql: str) -> dict[str, Any] | None:
    standard_specs = _extract_having_aggregate_specs(standard_sql)
    student_specs = _extract_having_aggregate_specs(student_sql)
    for standard in standard_specs:
        identity = (standard["agg"], standard["column"], standard["group_column"])
        for student in student_specs:
            if identity != (student["agg"], student["column"], student["group_column"]):
                continue
            if (
                standard["operator"] != student["operator"]
                or standard["boundary"] != student["boundary"]
                or standard["distinct"] != student["distinct"]
            ):
                return standard
    return standard_specs[0] if standard_specs else (student_specs[0] if student_specs else None)


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


def _limit_offset_required_rows(sql: str) -> int:
    ast = _parse_sql(sql)
    if not ast:
        return 0
    limit_node = ast.find(exp.Limit)
    offset_node = ast.find(exp.Offset)
    limit = _integer_node_value(limit_node.expression if isinstance(limit_node, exp.Limit) else None)
    offset = _integer_node_value(offset_node.expression if isinstance(offset_node, exp.Offset) else None)
    return max(0, (limit or 0) + (offset or 0) + 1)


def _bounded_cardinality_requirement(
    standard_sql: str,
    student_sql: str,
) -> dict[str, Any] | None:
    limit_required = max(
        _limit_offset_required_rows(standard_sql),
        _limit_offset_required_rows(student_sql),
    )
    distinct_group_required = _distinct_group_having_required_rows(
        standard_sql,
        student_sql,
    )
    candidates = [
        (
            limit_required,
            "LIMIT_OFFSET",
            "the LIMIT/OFFSET difference requires more rows than the bounded witness world permits",
        ),
        (
            distinct_group_required,
            "DISTINCT_GROUP_HAVING",
            "two qualifying GROUP BY results are required to expose the DISTINCT difference",
        ),
    ]
    required, operator, reason = max(candidates, key=lambda item: item[0])
    if required <= _MAX_WITNESS_ROWS_PER_TABLE:
        return None
    return {
        "kind": "FINITE_CARDINALITY",
        "operator": operator,
        "required_rows": required,
        "witness_row_limit": _MAX_WITNESS_ROWS_PER_TABLE,
        "reason": reason,
    }


def _distinct_group_having_required_rows(
    standard_sql: str,
    student_sql: str,
) -> int:
    asts = [_parse_sql(standard_sql), _parse_sql(student_sql)]
    selects = [_top_select(ast) if ast is not None else None for ast in asts]
    if not all(isinstance(select, exp.Select) for select in selects):
        return 0
    distinct_flags = [bool(select.args.get("distinct")) for select in selects]
    if distinct_flags[0] == distinct_flags[1]:
        return 0
    distinct_select = selects[0] if distinct_flags[0] else selects[1]
    requirement = _distinct_having_count_requirement(distinct_select)
    return requirement[1] * 2 if requirement is not None else 0


def _classify_bounded_verdict(
    *,
    standard_sql: str,
    student_sql: str,
    rows: dict[str, list[dict[str, Any]]],
    ast_diffs: list[ASTDiffNode],
    is_equivalent: bool,
) -> tuple[str, str, dict[str, Any]]:
    if not is_equivalent:
        return VERDICT_SUPPORTED, "NOT_EQUIVALENT", {}
    boundary = _bounded_cardinality_requirement(standard_sql, student_sql)
    boundary_matches_diff = bool(
        boundary
        and (
            boundary.get("operator") == "LIMIT_OFFSET"
            and any(diff.clause_category == "LIMIT" for diff in ast_diffs)
            or boundary.get("operator") == "DISTINCT_GROUP_HAVING"
            and any(diff.diff_type == "distinct_changed" for diff in ast_diffs)
        )
    )
    if boundary is not None and boundary_matches_diff:
        boundary["actual_rows"] = max((len(items) for items in rows.values()), default=0)
        return VERDICT_SEMANTIC_BOUNDARY, EQUIVALENCE_UNDECIDED, boundary
    return VERDICT_SUPPORTED, EQUIVALENCE_NO_COUNTEREXAMPLE, {}


def _integer_node_value(node: exp.Expression | None) -> int | None:
    value = _literal_value(node)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    try:
        return int(str(value))
    except Exception:
        return None


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


def _nearest_select(node: exp.Expression) -> exp.Select | None:
    parent = node.parent
    while parent is not None:
        if isinstance(parent, exp.Select):
            return parent
        parent = parent.parent
    return None


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


def _distinct_on_order_changed(standard_sql: str, student_sql: str) -> bool:
    standard_ast = _parse_sql(standard_sql)
    student_ast = _parse_sql(student_sql)
    standard_select = _top_select(standard_ast) if standard_ast else None
    student_select = _top_select(student_ast) if student_ast else None
    if not isinstance(standard_select, exp.Select) or not isinstance(student_select, exp.Select):
        return False
    standard_distinct = standard_select.args.get("distinct")
    student_distinct = student_select.args.get("distinct")
    standard_on = standard_distinct.args.get("on") if isinstance(standard_distinct, exp.Distinct) else None
    student_on = student_distinct.args.get("on") if isinstance(student_distinct, exp.Distinct) else None
    return (
        standard_on is not None
        and _sql_of(standard_on) == _sql_of(student_on)
        and _sql_of(_result_order_clause(standard_ast))
        != _sql_of(_result_order_clause(student_ast))
    )


def _apply_distinct_on_order_probe(
    data: dict[str, list[dict[str, Any]]],
    standard_sql: str,
    student_sql: str,
) -> None:
    if not _distinct_on_order_changed(standard_sql, student_sql):
        return
    standard_ast = _parse_sql(standard_sql)
    student_ast = _parse_sql(student_sql)
    standard_select = _top_select(standard_ast) if standard_ast else None
    if not isinstance(standard_select, exp.Select) or student_ast is None:
        return
    distinct = standard_select.args.get("distinct")
    on = distinct.args.get("on") if isinstance(distinct, exp.Distinct) else None
    key_columns = [
        expression
        for expression in (on.expressions if isinstance(on, exp.Tuple) else [])
        if isinstance(expression, exp.Column)
    ]
    standard_order = _order_by_items(standard_ast)
    student_order = _order_by_items(student_ast)
    order_column = next(
        (
            standard_item[2].this
            for standard_item, student_item in zip(standard_order, student_order)
            if standard_item[0] == student_item[0]
            and standard_item[1] != student_item[1]
            and isinstance(standard_item[2], exp.Ordered)
            and isinstance(standard_item[2].this, exp.Column)
        ),
        None,
    )
    source = _direct_from_table(standard_select)
    if not key_columns or not isinstance(order_column, exp.Column) or source is None:
        return
    table_name = next(
        (name for name in data if _norm_name(name) == _norm_name(source.name)),
        None,
    )
    rows = data.get(table_name or "")
    if not rows or len(rows) < 2:
        return
    lookup = _column_lookup(list(rows[0]))
    actual_keys = [lookup.get(_norm_name(column.name)) for column in key_columns]
    actual_order = lookup.get(_norm_name(order_column.name))
    if not actual_order or any(not key for key in actual_keys):
        return
    for key in actual_keys:
        rows[1][key] = rows[0][key]
    if _is_numeric_column(actual_order):
        rows[0][actual_order], rows[1][actual_order] = 101, 202
    else:
        rows[0][actual_order] = "__distinct_on_low__"
        rows[1][actual_order] = "__distinct_on_high__"


def _distinct_on_projection_changed(standard_sql: str, student_sql: str) -> bool:
    """Return whether DISTINCT ON was replaced by ordinary DISTINCT.

    The two forms can have the same number of DISTINCT nodes and therefore
    evade the older shape-only detector.  They differ whenever two source
    rows share the DISTINCT ON key but have different projected payload.
    """
    def distinct_on_keys(sql: str) -> str | None:
        ast = _parse_sql(sql)
        select = _top_select(ast) if ast else None
        if not isinstance(select, exp.Select):
            return None
        distinct = select.args.get("distinct")
        on = distinct.args.get("on") if isinstance(distinct, exp.Distinct) else None
        return _sql_of(on) if on is not None else None

    standard_on = distinct_on_keys(standard_sql)
    student_on = distinct_on_keys(student_sql)
    return (standard_on is not None) != (student_on is not None)


def _apply_distinct_on_projection_probe(
    data: dict[str, list[dict[str, Any]]],
    standard_sql: str,
    student_sql: str,
) -> None:
    if not _distinct_on_projection_changed(standard_sql, student_sql):
        return
    ast = _parse_sql(standard_sql)
    select = _top_select(ast) if ast else None
    if not isinstance(select, exp.Select):
        return
    distinct = select.args.get("distinct")
    on = distinct.args.get("on") if isinstance(distinct, exp.Distinct) else None
    keys = [item for item in (on.expressions if isinstance(on, exp.Tuple) else [])
            if isinstance(item, exp.Column)]
    projected = [item for item in select.expressions
                 if isinstance(item, exp.Column) and
                 not any(_norm_name(item.name) == _norm_name(key.name) for key in keys)]
    source = _direct_from_table(select)
    if not keys or not projected or not isinstance(source, exp.Table):
        return
    table_name = next((name for name in data if _norm_name(name) == _norm_name(source.name)), None)
    rows = data.get(table_name or "")
    if not rows or len(rows) < 2:
        return
    lookup = _column_lookup(list(rows[0]))
    key_columns = [lookup.get(_norm_name(key.name)) for key in keys]
    payload = next((lookup.get(_norm_name(item.name)) for item in projected), None)
    if not payload or any(column is None for column in key_columns):
        return
    for column in key_columns:
        rows[1][column] = rows[0][column]
    rows[0][payload] = 101
    rows[1][payload] = 202


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
    distinct_on_order_changed = _distinct_on_order_changed(standard_sql, student_sql)
    distinct_on_projection_changed = _distinct_on_projection_changed(standard_sql, student_sql)
    if not distinct_shape_changed and not distinct_on_order_changed and not distinct_on_projection_changed:
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
    _apply_distinct_on_order_probe(data, standard_sql, student_sql)
    _apply_distinct_on_projection_probe(data, standard_sql, student_sql)

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


def _distinct_shape_changed(standard_sql: str, student_sql: str) -> bool:
    def signature(sql: str) -> tuple[int, int]:
        ast = _parse_sql(sql)
        if not ast:
            return (0, 0)
        select_distinct = sum(
            1
            for select in ast.find_all(exp.Select)
            if select.args.get("distinct")
        )
        aggregate_distinct = sum(
            1
            for agg in ast.find_all(exp.AggFunc)
            if agg.args.get("distinct") or isinstance(agg.this, exp.Distinct)
        )
        return select_distinct, aggregate_distinct

    return signature(standard_sql) != signature(student_sql)


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


def _distinct_having_count_requirement(
    select: exp.Select,
) -> tuple[str, int] | None:
    """Return the smallest positive COUNT(DISTINCT ...) group that passes HAVING."""

    having = select.args.get("having")
    if not isinstance(having, exp.Having):
        return None
    inverse = {
        exp.GT: exp.LT,
        exp.GTE: exp.LTE,
        exp.LT: exp.GT,
        exp.LTE: exp.GTE,
        exp.EQ: exp.EQ,
        exp.NEQ: exp.NEQ,
    }
    for comparison in having.find_all(*inverse):
        left_count = (
            comparison.left
            if isinstance(comparison.left, exp.Count)
            else comparison.left.find(exp.Count)
        )
        right_count = (
            comparison.right
            if isinstance(comparison.right, exp.Count)
            else comparison.right.find(exp.Count)
        )
        count = left_count or right_count
        if (
            not isinstance(count, exp.Count)
            or _nearest_select(count) is not select
            or not (count.args.get("distinct") or isinstance(count.this, exp.Distinct))
        ):
            continue
        literal_node = comparison.right if count is left_count else comparison.left
        boundary = _literal_value(literal_node)
        if not isinstance(boundary, (int, float, Decimal)) or isinstance(boundary, bool):
            continue
        operator = type(comparison) if count is left_count else inverse[type(comparison)]
        if operator is exp.GT:
            cardinality = int(boundary) + 1
        elif operator is exp.GTE:
            cardinality = max(1, int(math.ceil(boundary)))
        elif operator is exp.EQ and int(boundary) == boundary:
            cardinality = int(boundary)
        elif operator is exp.NEQ:
            cardinality = 2 if boundary == 1 else 1
        elif operator is exp.LT:
            cardinality = 1 if boundary > 1 else 0
        elif operator is exp.LTE:
            cardinality = 1 if boundary >= 1 else 0
        else:
            cardinality = 0
        column = count.find(exp.Column)
        if cardinality >= 1 and isinstance(column, exp.Column):
            return column.name, cardinality
    return None


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


def _join_on_standard_assignments(
    standard_pairs: list[tuple[tuple[str, str], tuple[str, str]]],
    row_count: int,
) -> dict[tuple[str, str], list[Any]]:
    assignments: dict[tuple[str, str], list[Any]] = {}
    ref_counts = Counter(ref for pair in standard_pairs for ref in pair)
    repeated_refs = [ref for ref, count in ref_counts.items() if count > 1]
    handled_pairs: set[int] = set()

    for group_idx, repeated_ref in enumerate(repeated_refs):
        group = [
            (idx, pair[1] if pair[0] == repeated_ref else pair[0])
            for idx, pair in enumerate(standard_pairs)
            if repeated_ref in pair
        ]
        if len(group) < 2:
            continue
        role_count = len(group)
        slot_count = max(1, row_count // role_count)
        role_pools = [
            [2000 + group_idx * 1000 + role_idx * 100 + slot for slot in range(slot_count)]
            for role_idx in range(role_count)
        ]
        assignments[repeated_ref] = [
            role_pools[idx % role_count][(idx // role_count) % slot_count]
            for idx in range(row_count)
        ]
        for role_idx, (pair_idx, other_ref) in enumerate(group):
            pool = role_pools[role_idx]
            assignments[other_ref] = [pool[idx % slot_count] for idx in range(row_count)]
            handled_pairs.add(pair_idx)

    for pair_idx, (left, right) in enumerate(standard_pairs):
        if pair_idx in handled_pairs:
            continue
        values = [1000 + pair_idx * 100 + idx for idx in range(row_count)]
        assignments.setdefault(left, values)
        assignments.setdefault(right, values)
    return assignments


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
                        next(iter(all_left_values), None),
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


def _materialize_declared_join_witness(
    data: dict[str, list[dict[str, Any]]],
    ast_diffs: list[ASTDiffNode],
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
            right_values = {row.get(right_actual) for row in right_rows}
            # Preserve the first existing match and make the final left row
            # unambiguously absent from the right endpoint.
            if right_values:
                if left_rows:
                    left_rows[0][left_actual] = next(iter(right_values))
                    unique_value = 900000 + len(left_rows)
                    while unique_value in right_values:
                        unique_value += 1
                    left_rows[-1][left_actual] = unique_value


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


def _aggregate_probe_result(function: str, values: tuple[float, ...]) -> float | int | None:
    function = function.upper()
    if function == "COUNT":
        return len(values)
    if function == "SUM":
        return sum(values)
    if function == "AVG":
        return sum(values) / len(values) if values else None
    if function == "MIN":
        return min(values) if values else None
    if function == "MAX":
        return max(values) if values else None
    return None


def _aggregate_function_discriminator_groups(
    standard_function: str,
    student_function: str,
) -> tuple[tuple[float, ...], tuple[float, ...]] | None:
    """Find two tiny groups whose aggregate ordering is reversed."""

    candidates: tuple[tuple[float, ...], ...] = (
        (1, 9),
        (7,),
        (9.5,),
        (15,),
        (3,),
        (4, 4),
        (1, 1, 9),
    )
    for left in candidates:
        for right in candidates:
            if left == right or len(left) + len(right) > 4:
                continue
            standard_left = _aggregate_probe_result(standard_function, left)
            standard_right = _aggregate_probe_result(standard_function, right)
            student_left = _aggregate_probe_result(student_function, left)
            student_right = _aggregate_probe_result(student_function, right)
            if None in {
                standard_left,
                standard_right,
                student_left,
                student_right,
            }:
                return None
            standard_order = (standard_left > standard_right) - (
                standard_left < standard_right
            )
            student_order = (student_left > student_right) - (
                student_left < student_right
            )
            if standard_order and student_order and standard_order == -student_order:
                return left, right
    return None


def _aggregate_group_probe_value(column: str, group: int, position: int) -> Any:
    """Return a unique, non-cyclic key for one aggregate probe group."""

    serial = group + position * 100
    if _is_date_column(column):
        return (datetime(2035, 1, 1) + timedelta(days=serial)).strftime("%Y-%m-%d")
    if _is_numeric_column(column):
        return 700000 + serial
    return f"__aggregate_group_{position}_{group}__"


def _aggregate_probe_order_descending(diff: ASTDiffNode) -> bool | None:
    node = diff.standard_node
    select = node.find_ancestor(exp.Select) if isinstance(node, exp.Expression) else None
    order = select.args.get("order") if isinstance(select, exp.Select) else None
    if not isinstance(order, exp.Order) or not order.expressions:
        return None
    first = order.expressions[0]
    return bool(first.args.get("desc")) if isinstance(first, exp.Ordered) else False


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
        if isinstance(where, exp.Where):
            for comparison in where.find_all(exp.EQ):
                column = comparison.left if isinstance(comparison.left, exp.Column) else comparison.right
                parameter = comparison.right if column is comparison.left else comparison.left
                if not isinstance(column, exp.Column) or not isinstance(parameter, exp.Parameter):
                    continue
                actual = lookup.get(_norm_name(column.name))
                parameter_name = str(parameter.this.this) if isinstance(parameter.this, exp.Var) else str(parameter.this)
                literal = _parameter_literal(parameter_name)
                if actual:
                    assignments[actual] = literal[1:-1] if literal.startswith("'") and literal.endswith("'") else literal
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


def _positive_group_filter_value(
    column: str,
    constraints: list[dict[str, Any]],
    fallback: Any,
    index: int,
) -> Any:
    if _is_date_column(column):
        dates = sorted(
            str(value)
            for item in constraints
            for value in (item.get("value"), item.get("high"))
            if _coerce_datetime(value) is not None
        )
        if dates:
            base = _coerce_datetime(dates[0])
            if base is not None:
                return (base + timedelta(days=index % 2)).strftime("%Y-%m-%d")
    if isinstance(fallback, (int, float, Decimal)):
        return fallback + (index % 2)
    return fallback if index % 2 == 0 else f"{fallback}__group_alt"


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


def _apply_count_group_probe(
    rows: list[dict[str, Any]],
    group_col: str,
    boundary: int,
    *,
    group_cols: list[str] | None = None,
    value_col: str | None = None,
    distinct: bool = False,
) -> None:
    if not rows:
        return
    resolved_group_cols = list(dict.fromkeys(group_cols or [group_col]))
    exact = max(1, boundary)
    high = max(1, boundary + 1)
    low = max(1, boundary - 1)
    targets = [exact]
    remaining = len(rows) - exact
    if remaining >= high:
        targets.append(high)
        remaining -= high
    if remaining >= low:
        targets.append(low)
    elif remaining > 0:
        targets.append(remaining)
    group_names = ["Comp. Sci.", "Math", "Physics", "History", "Biology"]
    idx = 0
    for group_index, (group_name, count) in enumerate(zip(group_names, targets)):
        group_values = {
            column: (
                group_name
                if column == group_col
                and not _is_numeric_column(column)
                and not _is_date_column(column)
                else _group_probe_value(column, group_index, position)
            )
            for position, column in enumerate(resolved_group_cols)
        }
        for member_index in range(count):
            if idx >= len(rows):
                return
            rows[idx].update(group_values)
            if distinct and value_col and value_col != group_col:
                rows[idx][value_col] = f"__having_distinct_{group_name}_{member_index}__"
            idx += 1
    fallback_values = {
        column: (
            group_names[-1]
            if column == group_col
            and not _is_numeric_column(column)
            and not _is_date_column(column)
            else _group_probe_value(column, len(group_names) - 1, position)
        )
        for position, column in enumerate(resolved_group_cols)
    }
    while idx < len(rows):
        rows[idx].update(fallback_values)
        idx += 1


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


def _comparison_subquery_parts(
    comparison: exp.Expression,
) -> tuple[exp.Subquery, exp.Column] | None:
    if not isinstance(comparison, (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE)):
        return None
    left_subquery = comparison.left if isinstance(comparison.left, exp.Subquery) else comparison.left.find(exp.Subquery)
    right_subquery = comparison.right if isinstance(comparison.right, exp.Subquery) else comparison.right.find(exp.Subquery)
    if left_subquery is not None and isinstance(comparison.right, exp.Column):
        return left_subquery, comparison.right
    if right_subquery is not None and isinstance(comparison.left, exp.Column):
        return right_subquery, comparison.left
    return None


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


def _ancestor_selects(select: exp.Select) -> list[exp.Select]:
    """Return query blocks visible to a nested SELECT, nearest first."""
    result: list[exp.Select] = []
    current = select.parent
    while isinstance(current, exp.Expression):
        if isinstance(current, exp.Select):
            result.append(current)
        current = current.parent
    return result


def _correlation_refs(
    comparison: exp.Expression,
    inner: exp.Select,
) -> tuple[tuple[str, str], tuple[str, str]] | None:
    """Resolve one local/outer column pair across every ancestor scope."""
    columns = [
        side
        for side in (comparison.left, comparison.right)
        if isinstance(side, exp.Column)
    ]
    if len(columns) != 2:
        return None
    ancestors = _ancestor_selects(inner)
    for inner_column, outer_column in (columns, reversed(columns)):
        inner_ref = _column_ref_in_select(inner_column, inner)
        if inner_ref is None:
            continue
        outer_ref = next(
            (
                ref
                for ancestor in ancestors
                if (ref := _column_ref_in_select(outer_column, ancestor)) is not None
            ),
            None,
        )
        if outer_ref is not None and outer_ref != inner_ref:
            return outer_ref, inner_ref
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


_MISSING = object()


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


def _normalized_predicate_operator(
    comparison: exp.Expression,
    *,
    column_on_left: bool,
) -> type[exp.Expression]:
    operator = type(comparison)
    if column_on_left:
        return operator
    return {
        exp.GT: exp.LT,
        exp.GTE: exp.LTE,
        exp.LT: exp.GT,
        exp.LTE: exp.GTE,
    }.get(operator, operator)


def _scalar_predicate_values(
    comparison: exp.Expression,
    scalar: Any,
    column: str,
    *,
    column_on_left: bool,
) -> tuple[Any, Any] | None:
    operator = _normalized_predicate_operator(
        comparison,
        column_on_left=column_on_left,
    )
    counter = _counter_value(column, scalar)
    if operator is exp.EQ:
        return scalar, counter
    if operator is exp.NEQ:
        return counter, scalar
    if not isinstance(scalar, (int, float, Decimal)) or isinstance(
        scalar, bool
    ):
        return None
    if operator is exp.GT:
        return scalar + 1, scalar
    if operator is exp.GTE:
        return scalar, scalar - 1
    if operator is exp.LT:
        return scalar - 1, scalar
    if operator is exp.LTE:
        return scalar, scalar + 1
    return None


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
            left_unique = _catalog_has_unary_unique_key(schema_catalog, left_ref)
            right_unique = _catalog_has_unary_unique_key(schema_catalog, right_ref)
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


def _coerce_typed_seed(value: Any, kind: str, col: str, idx: int) -> Any:
    """Keep shared join seeds aligned while enforcing an explicit SQL type."""
    if value is None:
        return None
    if kind == "numeric":
        if isinstance(value, bool):
            return idx + 1
        if isinstance(value, (int, float, Decimal)):
            return value
        try:
            number = float(str(value).strip())
        except (TypeError, ValueError):
            return idx + 1
        return int(number) if number.is_integer() else number
    if kind == "date":
        if isinstance(value, str) and re.match(r"^\d{4}-\d{2}-\d{2}", value):
            return value
        return f"2024-01-{(idx % 9) + 1:02d}"
    if kind == "time":
        if isinstance(value, str) and re.match(r"^\d{2}:\d{2}:\d{2}", value):
            return value
        return f"{idx % 24:02d}:{(idx * 7) % 60:02d}:00"
    if kind == "text":
        return value if isinstance(value, str) else str(value)
    return value


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


def _base_value(col: str, idx: int, shared_values: dict[str, list[Any]]) -> Any:
    """
    主外键关联填充：如果当前列属于某个共享关联组，则从种子池中取值以保障表间能够成功连接。
    Fetches base value aligned with foreign key value pools if the column is part of a join group.
    """
    key = _join_group_key(col)
    if key in shared_values:
        return shared_values[key][idx % len(shared_values[key])]
    return _seed_value(col, idx)


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


def _seed_value(col: str, idx: int) -> Any:
    """
    根据列名分发基础测试数据，并强制包含单调性以检测 ORDER BY 错误。
    Generates a mock seed value for a column based on token name heuristics,
    ensuring monotonicity to expose ORDER BY/sorting logic bugs.
    """
    name = col.lower()

    # 姓名列循环生成
    if name == "name":
        return ["Alice", "Bob", "Carol", "Dave"][idx % 4]

    # 地理数据类型填充
    if name == "location":
        return f"POINT({idx} {idx})"

    # 日期字段：自增递增（单调性，支持 ORDER BY 校验）
    if _is_date_column(col):
        return f"2024-01-{(idx % 9) + 1:02d}"

    # 数字类型：idx + 1 单调递增自增，用于检测 >、>=、LIMIT 和聚合运算
    if _is_numeric_column(col):
        return idx + 1

    # 教学系统常用分类字段循环填充
    if "semester" in name:
        return ["Fall", "Spring", "Summer", "Winter"][idx % 4]
    if "grade" in name:
        return ["A", "B", "C", None][idx % 4]
    if "country" in name:
        return ["USA", "UK", "Germany", "Canada"][idx % 4]
    if "title" in name:
        return ["Sales Manager", "Marketing Lead", "Engineer", "Analyst"][idx % 4]
    if "dept" in name:
        return ["Comp. Sci.", "Math", "Physics", "History"][idx % 4]
    if "name" in name:
        return ["Alice", "Bob", "Carol", "Dave"][idx % 4]

    # 兜底生成唯一字符串，避免碰撞
    return f"{_clean_identifier(col)}_{idx + 1}"


def _like_counter_value(pattern: str) -> str:
    """Generate a string that does NOT match the given LIKE *pattern*.

    Handles four LIKE pattern shapes:
    - prefix-anchored  ``'Alice%'``  → counter must NOT start with ``Alice``
    - suffix-anchored  ``'%son'``    → counter must NOT end with ``son``
    - fully-wild       ``'%test%'``  → counter must NOT contain ``test``
    - exact            ``'Alice'``   → counter is just a different string
    """
    starts_wild = pattern.startswith('%')
    ends_wild = pattern.endswith('%')

    # Extract literal core (strip LIKE metacharacters)
    core: list[str] = []
    i = 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == '%':
            i += 1
        elif ch == '_':
            core.append('Z')
            i += 1
        elif ch == '[':
            j = pattern.find(']', i + 1)
            if j == -1:
                core.append(ch)
                i += 1
            else:
                inner = pattern[i + 1:j]
                if inner.startswith('^') or inner.startswith('!'):
                    core.append('a')
                elif inner:
                    core.append(inner[0])
                else:
                    core.append('a')
                i = j + 1
        else:
            core.append(ch)
            i += 1
    core_str = "".join(core)
    if not core_str:
        return "zz"

    # Fully-wild: %core% — counter must not CONTAIN the core.
    # Replace every character with 'z' to produce a same-length string
    # that is structurally identical but textually disjoint from the core.
    if starts_wild and ends_wild:
        return "z" * max(len(core_str), 3)

    # Prefix-anchored: core% — counter must not START with the core.
    if not starts_wild and ends_wild:
        return "X_" + core_str

    # Suffix-anchored: %core — counter must not END with the core.
    if starts_wild and not ends_wild:
        return core_str + "_X"

    # Exact match: core — just return a different string.
    return "not_" + core_str


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

    temporal_values: dict[str, list[str]] = defaultdict(list)
    for ast in asts:
        if not ast:
            continue
        aliases = _table_aliases(ast)
        for comparison in ast.find_all(exp.EQ):
            function = comparison.left
            literal = comparison.right
            if isinstance(comparison.right, (exp.Extract, exp.Year, exp.Month, exp.Day)):
                function, literal = comparison.right, comparison.left
            if not isinstance(literal, exp.Literal):
                continue
            part = ""
            column = None
            if isinstance(function, exp.Extract):
                part = str(function.this).upper()
                column = function.expression if isinstance(function.expression, exp.Column) else function.find(exp.Column)
            elif isinstance(function, (exp.Year, exp.Month, exp.Day)):
                part = type(function).__name__.upper()
                column = function.this if isinstance(function.this, exp.Column) else function.find(exp.Column)
            if not isinstance(column, exp.Column) or part not in {"YEAR", "MONTH", "DAY"}:
                continue
            table_ref = _norm_name(column.table or "")
            resolved_table = aliases.get(table_ref, table_ref)
            if resolved_table and resolved_table != _norm_name(table_name):
                continue
            actual = lookup.get(_norm_name(column.name))
            value = _integer_node_value(literal)
            if not actual or value is None:
                continue
            if part == "YEAR":
                date_value = f"{value:04d}-01-01"
            elif part == "MONTH":
                date_value = f"2024-{max(1, min(12, value)):02d}-01"
            else:
                date_value = f"2024-01-{max(1, min(28, value)):02d}"
            if date_value not in temporal_values[actual]:
                temporal_values[actual].append(date_value)
    for column, values in temporal_values.items():
        for index, row in enumerate(rows):
            row[column] = values[index % len(values)]


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


def _counter_value(col: str, value: Any) -> Any:
    if value is None:
        return _seed_value(col, 3)
    if isinstance(value, (int, float, Decimal)):
        return value + 999
    text = str(value)
    if "%" in text or "_" in text:
        return _like_counter_value(text)
    if text:
        return f"not_{text}"
    return "counter_value"


def _positive_probe_value(item: dict[str, Any]) -> Any:
    op = str(item.get("op") or "").upper()
    value = item.get("value")
    values = item.get("values") or []
    if op in {"GT", ">"} and isinstance(value, (int, float, Decimal)):
        return value + 1
    if op in {"GTE", "GE", ">="}:
        return value
    if op in {"LT", "<"} and isinstance(value, (int, float, Decimal)):
        return value - 1
    if op in {"LTE", "LE", "<="}:
        return value
    if op == "IN" and values:
        return values[0]
    if op == "LIKE" and isinstance(value, str):
        return value.replace("%", "a").replace("_", "a")
    if op == "IS":
        return None if value is None else value
    if op == "BETWEEN" and isinstance(value, (int, float, Decimal)):
        return value
    return values[0] if values else value


def _counter_probe_value(item: dict[str, Any]) -> Any:
    op = str(item.get("op") or "").upper()
    value = item.get("value")
    values = item.get("values") or []
    if op in {"GT", ">"}:
        return value
    if op in {"GTE", "GE", ">="} and isinstance(value, (int, float, Decimal)):
        return value - 1
    if op in {"LT", "<"}:
        return value
    if op in {"LTE", "LE", "<="} and isinstance(value, (int, float, Decimal)):
        return value + 1
    if op == "EQ" and isinstance(value, (int, float, Decimal)):
        return value + 1
    if op == "NEQ" and isinstance(value, (int, float, Decimal)):
        return value
    if op == "IN" and values:
        if isinstance(values[0], (int, float, Decimal)):
            return max(values) + 1
        return f"not_{values[0]}"
    if op == "BETWEEN" and isinstance(value, (int, float, Decimal)):
        high = item.get("high")
        if isinstance(high, (int, float, Decimal)):
            return high + 1
        return value - 1
    if op == "LIKE" and isinstance(value, str):
        return _like_counter_value(value)
    if op == "IS":
        return "not_null"
    return _counter_value(str(item.get("column") or ""), value)


def _execute_sqlite(
    schema: dict[str, list[str]],
    rows: dict[str, list[dict[str, Any]]],
    sql: str,
    *,
    schema_types: dict[str, dict[str, str]] | None = None,
) -> tuple[list[str], list[tuple[Any, ...]]]:
    """
    在内存 SQLite 隔离沙盒中建表、插入模拟数据并执行 SQL 查询，带有无限递归熔断机制。
    Executes SQL inside an in-memory SQLite sandbox with mock UDFs and infinite recursion guards.
    """
    conn = sqlite3.connect(":memory:")
    try:
        def sql_regexp_like(
            value: Any,
            pattern: Any,
            flags: Any = "",
        ) -> int | None:
            matched = regex_matches(pattern, value, flags=str(flags or ""))
            return None if matched is None else int(matched)

        def sql_regexp(pattern: Any, value: Any) -> int | None:
            # SQLite's infix REGEXP operator invokes regexp(pattern, value).
            return sql_regexp_like(value, pattern)

        def sql_similar_to(
            value: Any,
            pattern: Any,
            escape: Any = "\\",
        ) -> int | None:
            matched = similar_to_matches(
                pattern,
                value,
                escape="\\" if escape is None else str(escape),
            )
            return None if matched is None else int(matched)

        # 1. 注册自定义标量函数与空间地理占位函数，避免执行报错
        conn.create_function("AVG_SALARY", 1, lambda _company: 50000)
        conn.create_function("avg_salary", 1, lambda _company: 50000)
        conn.create_function("ST_WITHIN", 2, lambda _point, _poly: 1)
        conn.create_function("ST_DWITHIN", 3, lambda _a, _b, _distance: 1)
        conn.create_function("ST_DISTANCE", 2, lambda a, b: 0 if a == b else 1)
        conn.create_function("WIDTH_BUCKET", 4, _width_bucket)
        conn.create_function("YEAR", 1, lambda value: _sql_date_part("year", value))
        conn.create_function("MONTH", 1, lambda value: _sql_date_part("month", value))
        conn.create_function("DAY", 1, lambda value: _sql_date_part("day", value))
        conn.create_function("DATEPART", 2, _sql_date_part)
        conn.create_function("DATEADD", 3, _sql_date_add)
        conn.create_function("DATEDIFF", 2, _sql_date_diff_mysql)
        conn.create_function("DATEDIFF", 3, _sql_date_diff)
        conn.create_function("GETDATE", 0, lambda: "2024-02-01")
        conn.create_function("NOW", 0, lambda: "2024-02-01 00:00:00")
        conn.create_function("LEFT", 2, lambda value, size: str(value or "")[:max(0, int(size or 0))])
        conn.create_function("RIGHT", 2, lambda value, size: str(value or "")[-max(0, int(size or 0)):])
        conn.create_function("LEN", 1, lambda value: len(str(value or "")))
        conn.create_function("CONCAT", -1, lambda *values: "".join("" if value is None else str(value) for value in values))
        conn.create_function("IF", 3, lambda condition, yes, no: yes if condition else no)
        conn.create_function("REGEXP_LIKE", -1, sql_regexp_like)
        conn.create_function("REGEXP", 2, sql_regexp)
        conn.create_function("SIMILAR_TO", -1, sql_similar_to)

        cur = conn.cursor()
        # 2. 动态创建测试表并批量插入当前模拟的元组数据
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


def _execute_with_backend(
    *,
    backend: str,
    schema: dict[str, list[str]],
    schema_types: dict[str, dict[str, str]],
    rows: dict[str, list[dict[str, Any]]],
    sql: str,
    native_executor_url: str | None,
    execution_session: NativeQuerySession | None = None,
) -> tuple[list[str], list[tuple[Any, ...]]]:
    if execution_session is not None:
        return execution_session.execute(sql)
    if _is_native_backend(backend):
        return execute_native_query(
            backend,
            schema,
            schema_types,
            rows,
            sql,
            native_executor_url or "",
        )
    return _execute_sqlite(schema, rows, sql, schema_types=schema_types)


def _build_data_evidence(
    *,
    is_equivalent: bool,
    ordered: bool,
    standard_columns: list[str],
    student_columns: list[str],
    standard_rows: list[tuple[Any, ...]],
    student_rows: list[tuple[Any, ...]],
    standard_ast: exp.Expression | None,
    student_ast: exp.Expression | None,
    student_exec_error: str | None,
    ast_diffs: list[ASTDiffNode],
) -> dict[str, Any]:
    standard_counter = Counter(standard_rows)
    student_counter = Counter(student_rows)
    only_standard = list((standard_counter - student_counter).elements())[:5]
    only_student = list((student_counter - standard_counter).elements())[:5]
    duplicate_student = sum(count - 1 for count in student_counter.values() if count > 1)
    duplicate_standard = sum(count - 1 for count in standard_counter.values() if count > 1)
    suspected_cartesian = (
        _join_count(student_ast) > 0
        and not _has_join_on(student_ast)
        and len(student_rows) > max(len(standard_rows) * 2, len(standard_rows) + 3)
    )
    return {
        "sandbox_executed": True,
        "judge_status": "CORRECT" if is_equivalent else "WRONG",
        "student_exec_ok": student_exec_error is None,
        "student_exec_error": student_exec_error,
        "is_equivalent_on_generated_data": is_equivalent,
        "ordered_compare": ordered,
        "row_count_match": len(standard_rows) == len(student_rows),
        "standard_row_count": len(standard_rows),
        "student_row_count": len(student_rows),
        "columns_match": len(standard_columns) == len(student_columns),
        "column_names_match": standard_columns == student_columns,
        "standard_columns": standard_columns,
        "student_columns": student_columns,
        "standard_duplicate_row_count": duplicate_standard,
        "student_duplicate_row_count": duplicate_student,
        "suspected_cartesian_product": suspected_cartesian,
        "only_in_standard_sample": only_standard,
        "only_in_student_sample": only_student,
        "standard_sample_rows": standard_rows[:5],
        "student_sample_rows": student_rows[:5],
        "ast_diffs": [
            {
                **diff.extra,
                "diff_id": stable_diff_id(diff, index),
                "obligation_id": (
                    "obligation_"
                    + stable_diff_id(diff, index).removeprefix("diff_")
                ),
                "clause": diff.clause_category,
                "diff_type": diff.diff_type,
                "column": diff.target_column,
                "table": diff.target_table,
                "standard_sql": diff.extra.get("standard_sql") or _sql_of(diff.standard_node),
                "student_sql": diff.extra.get("student_sql") or _sql_of(diff.student_node),
            }
            for index, diff in enumerate(ast_diffs)
        ],
        "generation_tactics": _generation_tactics_from_ast_diffs(ast_diffs),
    }


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
    backend: str = "sqlite",
    schema_types: dict[str, dict[str, str]] | None = None,
    sql_dialect: str = "sqlite",
    structure_dialect: str | None = None,
    native_executor_url: str | None = None,
    execution_session: NativeQuerySession | None = None,
) -> dict[str, Any]:
    """
    变分隔离测试核心入口：基于 AST 对各算子进行单变量替换与移除测试，收集 Mutant 执行证据。
    Runs mutation tests by creating mutated student SQL variants (replacing/removing clauses)
    and evaluating them in the sandbox to isolate and locate specific faulty operators.
    """
    standard_ast = _parse_sql(standard_sql, dialect=structure_dialect or sql_dialect)
    student_ast = _parse_sql(student_sql, dialect=structure_dialect or sql_dialect)
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
        {"clause": "QUALIFY", "knowledge_point_id": "window-row-number", "arg": "qualify", "node_type": exp.Qualify},
        {"clause": "ORDER BY", "knowledge_point_id": "order-by", "arg": "order", "node_type": exp.Order},
        {"clause": "LIMIT", "knowledge_point_id": "limit", "arg": "limit", "node_type": exp.Limit},
        {"clause": "OFFSET", "knowledge_point_id": "limit", "arg": "offset", "node_type": exp.Offset},
        {"clause": "CONNECT BY", "knowledge_point_id": "hierarchical-query", "arg": "connect", "node_type": exp.Connect},
    ]
    mutation_context = {
        "backend": backend,
        "schema_types": schema_types or {},
        "sql_dialect": sql_dialect,
        "native_executor_url": native_executor_url,
        "execution_session": execution_session,
    }
    render_token = _MUTATION_RENDER_DIALECT.set(sql_dialect)
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
        _MUTATION_RENDER_DIALECT.reset(render_token)
    return result


def _query_block_scope_key(query: exp.Query) -> tuple[Any, ...]:
    """Build a copy-stable AST path for one query block."""
    path: list[tuple[str, str, int | None]] = []
    current: exp.Expression = query
    while current.parent is not None:
        parent = current.parent
        path.append((
            type(parent).__name__,
            str(current.arg_key or ""),
            current.index if isinstance(current.index, int) else None,
        ))
        current = parent
    return (type(query).__name__, tuple(reversed(path)))


def _paired_query_blocks(
    standard_ast: exp.Expression,
    student_ast: exp.Expression,
) -> list[tuple[str, exp.Query, exp.Query]]:
    """Pair only query blocks at the same structural AST path."""
    student_by_scope = {
        _query_block_scope_key(node): node
        for node in student_ast.walk()
        if isinstance(node, exp.Query)
    }
    pairs: list[tuple[str, exp.Query, exp.Query]] = []
    nested_index = 0
    for standard_query in standard_ast.walk():
        if not isinstance(standard_query, exp.Query):
            continue
        student_query = student_by_scope.get(_query_block_scope_key(standard_query))
        if not isinstance(student_query, exp.Query):
            continue
        if standard_query is standard_ast and student_query is student_ast:
            scope = "root"
        else:
            nested_index += 1
            scope = f"nested:{nested_index}"
        pairs.append((scope, standard_query, student_query))
    return pairs


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
                            replacement_sql = _sql_of(
                                mutated,
                                dialect=mutation_context["sql_dialect"],
                            )
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
    tests.extend(_run_table_modifier_mutations(
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
            node_type=exp.Pivot,
            clause="PIVOT",
            knowledge_point_id="pivot",
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
        _run_similar_pattern_mutation(
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
    backend: str = "sqlite",
    schema_types: dict[str, dict[str, str]] | None = None,
    sql_dialect: str = "sqlite",
    native_executor_url: str | None = None,
    execution_session: NativeQuerySession | None = None,
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
        "execution_backend": backend,
        "sql_dialect": sql_dialect,
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
            executable_sql = _prepare_mutation_sql(
                replacement_sql,
                backend,
                sql_dialect,
                allowed_tables=schema.keys(),
            )
            test["replacement_sql"] = executable_sql
            test["replacement_sqlite"] = executable_sql if backend == "sqlite" else None
            if executable_sql:
                cols, result_rows = _execute_with_backend(
                    backend=backend,
                    schema=schema,
                    schema_types=schema_types or {},
                    rows=rows,
                    sql=executable_sql,
                    native_executor_url=native_executor_url,
                    execution_session=execution_session,
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
        except NativeQuerySafetyError as exc:
            test["error"] = f"replacement_security_rejected: {exc}"
        except Exception as exc:
            if _is_platform_execution_error(backend, exc):
                raise
            test["error"] = f"replacement_failed: {exc}"
    if removal_sql:
        try:
            executable_sql = _prepare_mutation_sql(
                removal_sql,
                backend,
                sql_dialect,
                allowed_tables=schema.keys(),
            )
            test["removal_sql"] = executable_sql
            test["removal_sqlite"] = executable_sql if backend == "sqlite" else None
            if executable_sql:
                cols, result_rows = _execute_with_backend(
                    backend=backend,
                    schema=schema,
                    schema_types=schema_types or {},
                    rows=rows,
                    sql=executable_sql,
                    native_executor_url=native_executor_url,
                    execution_session=execution_session,
                )
                equivalent = _rows_equivalent(standard_columns, standard_rows, cols, result_rows, ordered)
                test["removal_exec_ok"] = True
                test["removed_student_clause_equivalent"] = equivalent
        except NativeQuerySafetyError as exc:
            prev = test.get("error")
            test["error"] = (
                f"{prev}; removal_security_rejected: {exc}"
                if prev
                else f"removal_security_rejected: {exc}"
            )
        except Exception as exc:
            if _is_platform_execution_error(backend, exc):
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
    backend: str = "sqlite",
    schema_types: dict[str, dict[str, str]] | None = None,
    sql_dialect: str = "sqlite",
    native_executor_url: str | None = None,
    execution_session: NativeQuerySession | None = None,
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
            replacement_sql=_sql_of(mutated, dialect=sql_dialect),
            removal_sql=None,
            standard_columns=standard_columns,
            standard_rows=standard_rows,
            ordered=ordered,
            backend=backend,
            schema_types=schema_types,
            sql_dialect=sql_dialect,
            native_executor_url=native_executor_url,
            execution_session=execution_session,
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
    backend: str = "sqlite",
    schema_types: dict[str, dict[str, str]] | None = None,
    sql_dialect: str = "sqlite",
    native_executor_url: str | None = None,
    execution_session: NativeQuerySession | None = None,
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
        replacement_sql=_sql_of(mutated, dialect=sql_dialect),
        removal_sql=None,
        standard_columns=standard_columns,
        standard_rows=standard_rows,
        ordered=ordered,
        backend=backend,
        schema_types=schema_types,
        sql_dialect=sql_dialect,
        native_executor_url=native_executor_url,
        execution_session=execution_session,
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
    backend: str = "sqlite",
    schema_types: dict[str, dict[str, str]] | None = None,
    sql_dialect: str = "sqlite",
    native_executor_url: str | None = None,
    execution_session: NativeQuerySession | None = None,
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
        replacement_sql=_sql_of(mutated, dialect=sql_dialect),
        removal_sql=None,
        standard_columns=standard_columns,
        standard_rows=standard_rows,
        ordered=ordered,
        backend=backend,
        schema_types=schema_types,
        sql_dialect=sql_dialect,
        native_executor_url=native_executor_url,
        execution_session=execution_session,
        action="move_aggregate_predicate_from_where_to_having",
        mutation_scope=["HAVING"],
        dependent_changes=["WHERE"],
    )


def _run_grouping_shape_mutation(
    *,
    schema: dict[str, list[str]],
    rows: dict[str, list[dict[str, Any]]],
    standard_ast: exp.Expression,
    student_ast: exp.Expression,
    standard_columns: list[str],
    standard_rows: list[tuple[Any, ...]],
    ordered: bool,
    backend: str = "sqlite",
    schema_types: dict[str, dict[str, str]] | None = None,
    sql_dialect: str = "sqlite",
    native_executor_url: str | None = None,
    execution_session: NativeQuerySession | None = None,
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
        replacement_sql=_sql_of(mutated, dialect=sql_dialect),
        removal_sql=None,
        standard_columns=standard_columns,
        standard_rows=standard_rows,
        ordered=ordered,
        backend=backend,
        schema_types=schema_types,
        sql_dialect=sql_dialect,
        native_executor_url=native_executor_url,
        execution_session=execution_session,
        action="restore_grouping_grain_and_dependent_projection",
        mutation_scope=["GROUP BY"],
        dependent_changes=["SELECT"],
    )


def _prepare_mutation_sql(
    sql: str,
    backend: str,
    sql_dialect: str,
    *,
    allowed_tables: Iterable[str] | None = None,
) -> str | None:
    if backend == "sqlite":
        return transpile_to_sqlite(sql)
    try:
        statements = sqlglot.transpile(
            sql,
            read=sql_dialect,
            write=sql_dialect,
            error_level=ErrorLevel.RAISE,
        )
        executable_sql = _prepare_native_sql(statements[0]) if statements else None
        if executable_sql:
            validate_native_query_safety(
                executable_sql,
                sql_dialect,
                allowed_tables=allowed_tables,
            )
        return executable_sql
    except sqlglot.errors.ParseError:
        return None
    except NativeQuerySafetyError:
        raise
    except Exception:
        return None


def _run_join_predicate_placement_mutation(
    *,
    schema: dict[str, list[str]],
    rows: dict[str, list[dict[str, Any]]],
    standard_ast: exp.Expression,
    student_ast: exp.Expression,
    standard_columns: list[str],
    standard_rows: list[tuple[Any, ...]],
    ordered: bool,
    backend: str = "sqlite",
    schema_types: dict[str, dict[str, str]] | None = None,
    sql_dialect: str = "sqlite",
    native_executor_url: str | None = None,
    execution_session: NativeQuerySession | None = None,
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
        replacement_sql=_sql_of(mutated, dialect=sql_dialect),
        removal_sql=None,
        standard_columns=standard_columns,
        standard_rows=standard_rows,
        ordered=ordered,
        backend=backend,
        schema_types=schema_types,
        sql_dialect=sql_dialect,
        native_executor_url=native_executor_url,
        execution_session=execution_session,
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
    backend: str = "sqlite",
    schema_types: dict[str, dict[str, str]] | None = None,
    sql_dialect: str = "sqlite",
    native_executor_url: str | None = None,
    execution_session: NativeQuerySession | None = None,
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
    replacement_sql = _sql_of(mutated, dialect=sql_dialect)
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
        backend=backend,
        schema_types=schema_types,
        sql_dialect=sql_dialect,
        native_executor_url=native_executor_url,
        execution_session=execution_session,
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
    backend: str = "sqlite",
    schema_types: dict[str, dict[str, str]] | None = None,
    sql_dialect: str = "sqlite",
    native_executor_url: str | None = None,
    execution_session: NativeQuerySession | None = None,
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
        clause = "DISTINCT ON" if (
            isinstance(std_distinct, exp.Distinct)
            and std_distinct.args.get("on") is not None
        ) else "DISTINCT"
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
            backend=backend,
            schema_types=schema_types,
            sql_dialect=sql_dialect,
            native_executor_url=native_executor_url,
            execution_session=execution_session,
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
    backend: str = "sqlite",
    schema_types: dict[str, dict[str, str]] | None = None,
    sql_dialect: str = "sqlite",
    native_executor_url: str | None = None,
    execution_session: NativeQuerySession | None = None,
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
            backend=backend,
            schema_types=schema_types,
            sql_dialect=sql_dialect,
            native_executor_url=native_executor_url,
            execution_session=execution_session,
            mutation_scope=[clause],
            query_scope=query_scope,
        ))
    return tests


def _run_table_modifier_mutations(
    *,
    schema: dict[str, list[str]],
    rows: dict[str, list[dict[str, Any]]],
    standard_ast: exp.Expression,
    student_ast: exp.Expression,
    standard_columns: list[str],
    standard_rows: list[tuple[Any, ...]],
    ordered: bool,
    backend: str = "sqlite",
    schema_types: dict[str, dict[str, str]] | None = None,
    sql_dialect: str = "sqlite",
    native_executor_url: str | None = None,
    execution_session: NativeQuerySession | None = None,
) -> list[dict[str, Any]]:
    """Restore vendor table modifiers without replacing the whole FROM tree."""
    standard_tables = list(standard_ast.find_all(exp.Table))
    student_tables = list(student_ast.find_all(exp.Table))
    if not standard_tables or len(standard_tables) != len(student_tables):
        return []

    tests: list[dict[str, Any]] = []
    for arg, clause, knowledge_point_id in (
        ("sample", "TABLE SAMPLE", "table-sample"),
        ("only", "FROM ONLY", "table-only"),
    ):
        changed_indexes: list[int] = []
        for index, (standard_table, student_table) in enumerate(
            zip(standard_tables, student_tables)
        ):
            if _norm_name(standard_table.name) != _norm_name(student_table.name):
                continue
            standard_value = standard_table.args.get(arg)
            student_value = student_table.args.get(arg)
            standard_repr = (
                _sql_of(standard_value, dialect=sql_dialect)
                if isinstance(standard_value, exp.Expression)
                else str(bool(standard_value))
            )
            student_repr = (
                _sql_of(student_value, dialect=sql_dialect)
                if isinstance(student_value, exp.Expression)
                else str(bool(student_value))
            )
            if standard_repr != student_repr:
                changed_indexes.append(index)
        if not changed_indexes:
            continue

        mutated = student_ast.copy()
        mutated_tables = list(mutated.find_all(exp.Table))
        for index in changed_indexes:
            standard_value = standard_tables[index].args.get(arg)
            mutated_tables[index].set(
                arg,
                standard_value.copy()
                if isinstance(standard_value, exp.Expression)
                else standard_value,
            )

        tests.append(_execute_mutation_case(
            schema=schema,
            rows=rows,
            clause=clause,
            knowledge_point_id=knowledge_point_id,
            replacement_sql=_sql_of(mutated, dialect=sql_dialect),
            removal_sql=None,
            standard_columns=standard_columns,
            standard_rows=standard_rows,
            ordered=ordered,
            backend=backend,
            schema_types=schema_types,
            sql_dialect=sql_dialect,
            native_executor_url=native_executor_url,
            execution_session=execution_session,
            mutation_scope=[clause],
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
    backend: str = "sqlite",
    schema_types: dict[str, dict[str, str]] | None = None,
    sql_dialect: str = "sqlite",
    native_executor_url: str | None = None,
    execution_session: NativeQuerySession | None = None,
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
        join.set("side", std_join.args.get("side"))
        join.set("kind", std_join.args.get("kind"))
        if std_join.args.get("on") is None:
            # CROSS JOIN has no predicate. Keeping the student's ON clause
            # would create a hybrid query instead of restoring the standard.
            join.set("on", None)

    return _execute_mutation_case(
        schema=schema,
        rows=rows,
        clause="JOIN TYPE",
        knowledge_point_id=_join_type_kp(standard_joins[0]),
        replacement_sql=_sql_of(mutated, dialect=sql_dialect),
        removal_sql=None,
        standard_columns=standard_columns,
        standard_rows=standard_rows,
        ordered=ordered,
        backend=backend,
        schema_types=schema_types,
        sql_dialect=sql_dialect,
        native_executor_url=native_executor_url,
        execution_session=execution_session,
        action="restore_join_type_and_direct_dependencies",
        mutation_scope=["JOIN TYPE", "JOIN ON"]
        if any(std_join.args.get("on") is None for std_join in standard_joins)
        else ["JOIN TYPE"],
        dependent_changes=["JOIN ON"]
        if any(
            (std_join.args.get("on") is None) != (stu_join.args.get("on") is None)
            for std_join, stu_join in zip(standard_joins, student_joins)
        )
        else [],
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
    backend: str = "sqlite",
    schema_types: dict[str, dict[str, str]] | None = None,
    sql_dialect: str = "sqlite",
    native_executor_url: str | None = None,
    execution_session: NativeQuerySession | None = None,
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
        replacement_sql=_sql_of(mutated, dialect=sql_dialect),
        removal_sql=None,
        standard_columns=standard_columns,
        standard_rows=standard_rows,
        ordered=ordered,
        backend=backend,
        schema_types=schema_types,
        sql_dialect=sql_dialect,
        native_executor_url=native_executor_url,
        execution_session=execution_session,
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
    backend: str = "sqlite",
    schema_types: dict[str, dict[str, str]] | None = None,
    sql_dialect: str = "sqlite",
    native_executor_url: str | None = None,
    execution_session: NativeQuerySession | None = None,
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
        and item.extra.get("query_scope") == "nested_correlation"
        and isinstance(item.standard_node, exp.Expression)
        and isinstance(item.student_node, exp.Expression)
    ]
    if len(focused) != 1:
        return None
    diff = focused[0]
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
        clause="CORRELATED SUBQUERY",
        knowledge_point_id="subquery-correlated",
        replacement_sql=replacement_sql,
        removal_sql=None,
        standard_columns=standard_columns,
        standard_rows=standard_rows,
        ordered=ordered,
        backend=backend,
        schema_types=schema_types,
        sql_dialect=sql_dialect,
        native_executor_url=native_executor_url,
        execution_session=execution_session,
        action="restore_correlated_comparison",
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
    backend: str = "sqlite",
    schema_types: dict[str, dict[str, str]] | None = None,
    sql_dialect: str = "sqlite",
    native_executor_url: str | None = None,
    execution_session: NativeQuerySession | None = None,
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
        backend=backend,
        schema_types=schema_types,
        sql_dialect=sql_dialect,
        native_executor_url=native_executor_url,
        execution_session=execution_session,
        action="restore_subquery_membership_key",
        mutation_scope=["IN"],
        query_scope="nested_membership",
    )


def _scalar_function_roots(ast: exp.Expression) -> list[exp.Func]:
    """Return scalar function roots, excluding structural/aggregate constructs."""
    roots: list[exp.Func] = []
    excluded_nodes = (exp.AggFunc, exp.Case, exp.Exists)
    excluded_ancestors = (exp.AggFunc, exp.Case, exp.Exists, exp.Pivot, exp.Window)
    for node in ast.walk():
        if not isinstance(node, exp.Func) or isinstance(node, excluded_nodes):
            continue
        parent = node.parent
        nested_or_structural = False
        while parent is not None:
            if isinstance(parent, excluded_ancestors) or isinstance(parent, exp.Func):
                nested_or_structural = True
                break
            parent = parent.parent
        if not nested_or_structural:
            roots.append(node)
    return roots


def _run_scalar_function_mutation(
    *,
    schema: dict[str, list[str]],
    rows: dict[str, list[dict[str, Any]]],
    standard_ast: exp.Expression,
    student_ast: exp.Expression,
    standard_columns: list[str],
    standard_rows: list[tuple[Any, ...]],
    ordered: bool,
    backend: str = "sqlite",
    schema_types: dict[str, dict[str, str]] | None = None,
    sql_dialect: str = "sqlite",
    native_executor_url: str | None = None,
    execution_session: NativeQuerySession | None = None,
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
        replacement_sql=_sql_of(mutated, dialect=sql_dialect),
        removal_sql=None,
        standard_columns=standard_columns,
        standard_rows=standard_rows,
        ordered=ordered,
        backend=backend,
        schema_types=schema_types,
        sql_dialect=sql_dialect,
        native_executor_url=native_executor_url,
        execution_session=execution_session,
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
    backend: str = "sqlite",
    schema_types: dict[str, dict[str, str]] | None = None,
    sql_dialect: str = "sqlite",
    native_executor_url: str | None = None,
    execution_session: NativeQuerySession | None = None,
) -> dict[str, Any] | None:
    """Restore one changed LIKE/ILIKE pattern together with its ESCAPE node."""
    standard_nodes = list(standard_ast.find_all(exp.Like, exp.ILike))
    student_nodes = list(student_ast.find_all(exp.Like, exp.ILike))
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
    mutated_nodes = list(mutated.find_all(exp.Like, exp.ILike))
    if len(mutated_nodes) != len(standard_nodes):
        return None
    mutated_render_nodes = [_like_render_node(node) for node in mutated_nodes]
    mutated_render_nodes[index].replace(standard_render_nodes[index].copy())
    predicate_name = "ILIKE" if isinstance(standard_node, exp.ILike) else "LIKE"
    return _execute_mutation_case(
        schema=schema,
        rows=rows,
        clause="PREDICATE",
        knowledge_point_id="like",
        replacement_sql=_sql_of(mutated, dialect=sql_dialect),
        removal_sql=None,
        standard_columns=standard_columns,
        standard_rows=standard_rows,
        ordered=ordered,
        backend=backend,
        schema_types=schema_types,
        sql_dialect=sql_dialect,
        native_executor_url=native_executor_url,
        execution_session=execution_session,
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
    backend: str = "sqlite",
    schema_types: dict[str, dict[str, str]] | None = None,
    sql_dialect: str = "sqlite",
    native_executor_url: str | None = None,
    execution_session: NativeQuerySession | None = None,
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
        replacement_sql=_sql_of(mutated, dialect=sql_dialect),
        removal_sql=None,
        standard_columns=standard_columns,
        standard_rows=standard_rows,
        ordered=ordered,
        backend=backend,
        schema_types=schema_types,
        sql_dialect=sql_dialect,
        native_executor_url=native_executor_url,
        execution_session=execution_session,
        mutation_scope=["GLOB"],
    )


def _run_similar_pattern_mutation(
    *,
    schema: dict[str, list[str]],
    rows: dict[str, list[dict[str, Any]]],
    standard_ast: exp.Expression,
    student_ast: exp.Expression,
    standard_columns: list[str],
    standard_rows: list[tuple[Any, ...]],
    ordered: bool,
    backend: str = "sqlite",
    schema_types: dict[str, dict[str, str]] | None = None,
    sql_dialect: str = "sqlite",
    native_executor_url: str | None = None,
    execution_session: NativeQuerySession | None = None,
) -> dict[str, Any] | None:
    """Restore exactly one changed constant SIMILAR TO predicate."""
    standard_nodes = list(standard_ast.find_all(exp.SimilarTo))
    student_nodes = list(student_ast.find_all(exp.SimilarTo))
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
    if not isinstance(standard_node.expression, exp.Literal) or not isinstance(
        student_node.expression, exp.Literal
    ):
        return None
    mutated = student_ast.copy()
    mutated_nodes = list(mutated.find_all(exp.SimilarTo))
    if len(mutated_nodes) != len(standard_nodes):
        return None
    mutated_render_nodes = [_like_render_node(node) for node in mutated_nodes]
    mutated_render_nodes[index].replace(standard_render_nodes[index].copy())
    return _execute_mutation_case(
        schema=schema,
        rows=rows,
        clause="PREDICATE",
        knowledge_point_id="similar-to",
        replacement_sql=_sql_of(mutated, dialect=sql_dialect),
        removal_sql=None,
        standard_columns=standard_columns,
        standard_rows=standard_rows,
        ordered=ordered,
        backend=backend,
        schema_types=schema_types,
        sql_dialect=sql_dialect,
        native_executor_url=native_executor_url,
        execution_session=execution_session,
        mutation_scope=["SIMILAR TO"],
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
    backend: str = "sqlite",
    schema_types: dict[str, dict[str, str]] | None = None,
    sql_dialect: str = "sqlite",
    native_executor_url: str | None = None,
    execution_session: NativeQuerySession | None = None,
) -> dict[str, Any] | None:
    std_op = _set_operator_name(standard_ast)
    stu_op = _set_operator_name(student_ast)
    if not std_op or _sql_of(standard_ast) == _sql_of(student_ast):
        return None
    if not isinstance(standard_ast, (exp.Union, exp.Intersect, exp.Except)):
        return None

    if type(standard_ast) is type(student_ast):
        if _set_operator_modifier(standard_ast) != _set_operator_modifier(student_ast):
            replacement_sql = _set_operator_replacement_sql(
                standard_ast, student_ast, dialect=sql_dialect
            )
        else:
            # The operator and modifier are unchanged but one or both branch
            # bodies differ, so restore the branches as one atomic mutation.
            mutated = student_ast.copy()
            mutated.set("this", standard_ast.this.copy())
            mutated.set("expression", standard_ast.expression.copy())
            replacement_sql = _sql_of(mutated, dialect=sql_dialect)
    else:
        replacement_sql = _set_operator_replacement_sql(
            standard_ast, student_ast, dialect=sql_dialect
        )
    return _execute_mutation_case(
        schema=schema,
        rows=rows,
        clause=std_op,
        knowledge_point_id=_set_operator_kp(std_op),
        replacement_sql=replacement_sql,
        removal_sql=None,
        standard_columns=standard_columns,
        standard_rows=standard_rows,
        ordered=ordered,
        backend=backend,
        schema_types=schema_types,
        sql_dialect=sql_dialect,
        native_executor_url=native_executor_url,
        execution_session=execution_session,
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
    backend: str = "sqlite",
    schema_types: dict[str, dict[str, str]] | None = None,
    sql_dialect: str = "sqlite",
    native_executor_url: str | None = None,
    execution_session: NativeQuerySession | None = None,
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
        replacement_sql=_sql_of(mutated, dialect=sql_dialect),
        removal_sql=None,
        standard_columns=standard_columns,
        standard_rows=standard_rows,
        ordered=ordered,
        backend=backend,
        schema_types=schema_types,
        sql_dialect=sql_dialect,
        native_executor_url=native_executor_url,
        execution_session=execution_session,
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
    backend: str = "sqlite",
    schema_types: dict[str, dict[str, str]] | None = None,
    sql_dialect: str = "sqlite",
    native_executor_url: str | None = None,
    execution_session: NativeQuerySession | None = None,
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
        replacement_sql=_sql_of(mutated, dialect=sql_dialect),
        removal_sql=None,
        standard_columns=standard_columns,
        standard_rows=standard_rows,
        ordered=ordered,
        backend=backend,
        schema_types=schema_types,
        sql_dialect=sql_dialect,
        native_executor_url=native_executor_url,
        execution_session=execution_session,
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
    backend: str = "sqlite",
    schema_types: dict[str, dict[str, str]] | None = None,
    sql_dialect: str = "sqlite",
    native_executor_url: str | None = None,
    execution_session: NativeQuerySession | None = None,
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
                replacement_sql=_sql_of(standard_ast, dialect=sql_dialect),
                removal_sql=None,
                standard_columns=standard_columns,
                standard_rows=standard_rows,
                ordered=ordered,
                backend=backend,
                schema_types=schema_types,
                sql_dialect=sql_dialect,
                native_executor_url=native_executor_url,
                execution_session=execution_session,
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
        replacement_sql=_sql_of(mutated, dialect=sql_dialect),
        removal_sql=None,
        standard_columns=standard_columns,
        standard_rows=standard_rows,
        ordered=ordered,
        backend=backend,
        schema_types=schema_types,
        sql_dialect=sql_dialect,
        native_executor_url=native_executor_url,
        execution_session=execution_session,
        mutation_scope=["RECURSIVE CTE"],
    )


def _set_operator_replacement_sql(
    standard_ast: exp.Expression,
    student_ast: exp.Expression,
    dialect: str = "sqlite",
) -> str | None:
    if isinstance(student_ast, type(standard_ast)):
        mutated = student_ast.copy()
        for arg in ("distinct", "by_name", "side", "kind"):
            mutated.set(arg, standard_ast.args.get(arg))
        return _sql_of(mutated, dialect=dialect)
    return _sql_of(standard_ast, dialect=dialect)


def _join_type_signature(join: exp.Join) -> tuple[str, str]:
    return (
        str(join.args.get("side") or "").upper(),
        str(join.args.get("kind") or "").upper(),
    )


def _join_type_kp(join: exp.Join) -> str:
    side = str(join.args.get("side") or "").upper()
    if side == "LEFT":
        return "join-left"
    if side == "RIGHT":
        return "join-right"
    if side == "FULL":
        return "join-full"
    return "join-inner"


def _set_operator_kp(op: str) -> str:
    normalized = op.upper()
    if normalized == "INTERSECT":
        return "intersect"
    if normalized == "EXCEPT":
        return "except"
    return "union"


def _mutate_by_node_replacement(
    ast: exp.Expression,
    target_node: exp.Expression,
    replacement_node: exp.Expression | None
) -> str | None:
    # ``find_all`` walks descendants and therefore does not include the root
    # itself.  Projection and clause summary diffs can legitimately target
    # that root, so handle the identity case before descendant indexing.
    if ast is target_node:
        return _sql_of(replacement_node) if replacement_node is not None else None
    mutated = ast.copy()
    target_type = type(target_node)
    orig_nodes = list(ast.find_all(target_type))
    idx = -1
    for i, node in enumerate(orig_nodes):
        if id(node) == id(target_node):
            idx = i
            break
    if idx == -1:
        return None

    mutated_nodes = list(mutated.find_all(target_type))
    if idx < len(mutated_nodes):
        node_to_mutate = mutated_nodes[idx]
        if replacement_node is not None:
            node_to_mutate.replace(replacement_node.copy())
        else:
            node_to_mutate.pop()
        return _sql_of(mutated)
    return None


def _mutate_query_arg(
    ast: exp.Expression,
    query: exp.Query,
    arg: str,
    replacement: exp.Expression | None,
) -> str | None:
    mutated = ast.copy()
    target_scope = _query_block_scope_key(query)
    target = next(
        (
            node
            for node in mutated.walk()
            if isinstance(node, exp.Query)
            and _query_block_scope_key(node) == target_scope
        ),
        None,
    )
    if not isinstance(target, exp.Query):
        return None
    target.set(arg, replacement.copy() if replacement is not None else None)
    return _sql_of(mutated)


def _mutate_query_expressions(
    ast: exp.Expression,
    query: exp.Query,
    expressions: list[exp.Expression],
) -> str | None:
    mutated = ast.copy()
    target_scope = _query_block_scope_key(query)
    target = next(
        (
            node
            for node in mutated.walk()
            if isinstance(node, exp.Select)
            and _query_block_scope_key(node) == target_scope
        ),
        None,
    )
    if not isinstance(target, exp.Select):
        return None
    target.set("expressions", [expression.copy() for expression in expressions])
    return _sql_of(mutated)


def _rows_equivalent(
    standard_columns: list[str],
    standard_rows: list[tuple[Any, ...]],
    candidate_columns: list[str],
    candidate_rows: list[tuple[Any, ...]],
    ordered: bool,
) -> bool:
    if len(standard_columns) != len(candidate_columns):
        return False
    if ordered:
        return standard_rows == candidate_rows
    return Counter(standard_rows) == Counter(candidate_rows)


def _sql_of(node: exp.Expression | None, dialect: str | None = None) -> str:
    if node is None:
        return ""
    try:
        if dialect == "tsql" and any(
            isinstance(fetch.args.get("limit_options"), exp.LimitOptions)
            and fetch.args["limit_options"].args.get("with_ties")
            for fetch in node.find_all(exp.Fetch)
        ):
            return _render_native_ast(node, "tsql") or ""
        return node.sql(dialect=dialect or _MUTATION_RENDER_DIALECT.get(), normalize=True)
    except Exception:
        return str(node)


def _sqlite_type(col: str) -> str:
    if _is_date_column(col):
        return "TEXT"
    return "REAL" if _is_numeric_column(col) else "TEXT"


def _sqlite_declared_affinity(col: str, declared_type: str | None) -> str:
    """Map authoritative schema types to SQLite affinity without constraints."""
    if not declared_type:
        return _sqlite_type(col)
    declared = str(declared_type).upper()
    if "INT" in declared:
        return "INTEGER"
    if any(token in declared for token in ("CHAR", "CLOB", "TEXT", "DATE", "TIME", "UUID", "JSON")):
        return "TEXT"
    if "BLOB" in declared or not declared.strip():
        return "BLOB"
    if any(token in declared for token in ("REAL", "FLOA", "DOUB")):
        return "REAL"
    return "NUMERIC"


def _is_date_column(col: str) -> bool:
    name = col.lower()
    tokens = [token for token in re.split(r"[^a-z0-9]+", name) if token]
    if name in {"date", "datetime", "timestamp"}:
        return True
    if name.endswith("date") or name.endswith("datetime") or name.endswith("timestamp"):
        return True
    if name.endswith("_at") or name.endswith("_on"):
        return True
    if any(token in {"date", "bdate", "time"} for token in tokens):
        return True
    if any(token in {"start", "end"} for token in tokens) and any(token in {"date", "time"} for token in tokens):
        return True
    return False


def _is_numeric_column(col: str) -> bool:
    name = col.lower()
    if name in {
        "x", "y", "z", "n", "age", "people", "temperature", "month",
        "quarter", "rank", "row_num", "row_number", "tiv_2015", "tiv_2016",
    }:
        return True
    tokens = [token for token in re.split(r"[^a-z0-9]+", name) if token]
    if any(
        token in {"age", "people", "temperature", "allotment", "tiv"}
        for token in tokens
    ):
        return True
    return any(token in name for token in NUMERIC_HINTS)


def _is_key_column(col: str) -> bool:
    name = col.lower()
    return name == "id" or name.endswith("_id") or name.endswith("id") or name in {"ssn", "dno", "dnum", "pno"}


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


def _table_key_aliases(table_name: str) -> set[str]:
    tokens = [token for token in re.split(r"[_\\W]+", table_name) if token]
    aliases = {f"{table_name}_id", f"{table_name}id"}
    if table_name:
        aliases.add(f"{table_name.rstrip('s')}_id")
    if tokens:
        aliases.add(f"{tokens[-1]}_id")
        aliases.add(f"{tokens[-1]}id")
    common = {
        "employee": {"emp_id", "empid", "employee_id"},
        "department": {"dept_id", "deptid", "department_id", "dno", "dnum", "dnumber"},
        "course": {"course_id", "courseid"},
        "student": {"id", "student_id", "sid"},
        "instructor": {"id", "instructor_id", "iid"},
    }
    aliases.update(common.get(table_name, set()))
    return aliases


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


def _apply_oracle_nocycle_probe(
    data: dict[str, list[dict[str, Any]]],
    standard_sql: str,
    student_sql: str,
) -> None:
    """Create a reachable hierarchy loop when only NOCYCLE differs."""
    standard_ast = _parse_sql(standard_sql)
    student_ast = _parse_sql(student_sql)
    standard_select = _top_select(standard_ast) if standard_ast else None
    student_select = _top_select(student_ast) if student_ast else None
    if not isinstance(standard_select, exp.Select) or not isinstance(student_select, exp.Select):
        return
    standard_connect = standard_select.args.get("connect")
    student_connect = student_select.args.get("connect")
    if not isinstance(standard_connect, exp.Connect) or not isinstance(student_connect, exp.Connect):
        return
    if bool(standard_connect.args.get("nocycle")) == bool(
        student_connect.args.get("nocycle")
    ):
        return
    if (
        _sql_of(standard_connect.args.get("start"))
        != _sql_of(student_connect.args.get("start"))
        or _sql_of(standard_connect.args.get("connect"))
        != _sql_of(student_connect.args.get("connect"))
    ):
        return

    probe_connect = (
        standard_connect
        if standard_connect.args.get("nocycle")
        else student_connect
    )
    start = probe_connect.args.get("start")
    relation = probe_connect.args.get("connect")
    if not (
        isinstance(start, exp.Is)
        and isinstance(start.this, exp.Column)
        and isinstance(start.expression, exp.Null)
        and isinstance(relation, exp.EQ)
    ):
        return

    prior_side: exp.Prior | None = None
    child_side: exp.Column | None = None
    if isinstance(relation.this, exp.Prior) and isinstance(relation.expression, exp.Column):
        prior_side = relation.this
        child_side = relation.expression
    elif isinstance(relation.expression, exp.Prior) and isinstance(relation.this, exp.Column):
        prior_side = relation.expression
        child_side = relation.this
    if not isinstance(prior_side, exp.Prior) or not isinstance(prior_side.this, exp.Column):
        return
    if not isinstance(child_side, exp.Column):
        return
    if _norm_name(start.this.name) != _norm_name(child_side.name):
        return

    from_clause = standard_select.args.get("from_")
    table = from_clause.find(exp.Table) if isinstance(from_clause, exp.Expression) else None
    if not isinstance(table, exp.Table):
        return
    table_name = next(
        (name for name in data if _norm_name(name) == _norm_name(table.name)),
        None,
    )
    if table_name is None or len(data.get(table_name) or []) < 3:
        return
    rows = data[table_name]
    columns = _column_lookup(list(rows[0]))
    id_column = columns.get(_norm_name(prior_side.this.name))
    parent_column = columns.get(_norm_name(child_side.name))
    if not id_column or not parent_column:
        return

    root_id = rows[0].get(id_column)
    if root_id is None:
        root_id = _seed_value(id_column, 0)
    child_id = rows[1].get(id_column)
    if child_id is None or child_id == root_id:
        child_id = _unique_key_value(id_column, 1, {root_id}, root_id)

    rows[0][id_column] = root_id
    rows[0][parent_column] = None
    rows[1][id_column] = child_id
    rows[1][parent_column] = root_id
    rows[2][id_column] = root_id
    rows[2][parent_column] = child_id


def _unique_key_value(col: str, idx: int, seen: set[Any], duplicate_value: Any) -> Any:
    base = _seed_value(col, idx)
    if isinstance(duplicate_value, (int, float)) and abs(duplicate_value) >= 100:
        base = duplicate_value + idx
    if isinstance(base, (int, float)):
        candidate: Any = base
        while candidate in seen:
            candidate += 1000
        return candidate
    candidate = str(base)
    suffix = 1
    while candidate in seen:
        suffix += 1
        candidate = f"{base}_{suffix}"
    return candidate


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


def _join_count(ast: exp.Expression | None) -> int:
    return len(list(ast.find_all(exp.Join))) if ast else 0


def _has_join_on(ast: exp.Expression | None) -> bool:
    return any(bool(join.args.get("on")) for join in ast.find_all(exp.Join)) if ast else False


def _prepare_sqlite_source(sql: str) -> str:
    """Remove dialect-only query decorations before sqlglot sees them."""
    sql = re.sub(
        r"(?is)\s+OPTION\s*\(\s*MAXRECURSION\s+\d+\s*\)\s*;?\s*$",
        "",
        sql.strip(),
    )

    search_columns: list[tuple[str, str]] = []

    def replace_search(match: re.Match) -> str:
        by_expression = match.group(2).strip()
        generated_column = match.group(3)
        first_key = by_expression.split(",", 1)[0].strip()
        search_columns.append((generated_column, first_key))
        return " "

    sql = re.sub(
        r"(?is)\s+SEARCH\s+(DEPTH|BREADTH)\s+FIRST\s+BY\s+(.+?)\s+SET\s+"
        r"([A-Za-z_][A-Za-z0-9_]*)\s+(?=SELECT\b)",
        replace_search,
        sql,
    )
    for generated_column, fallback_key in search_columns:
        sql = re.sub(
            rf"(?is)(\bORDER\s+BY\s+){re.escape(generated_column)}\b",
            lambda match, key=fallback_key: match.group(1) + key,
            sql,
        )

    # PostgreSQL CYCLE adds implicit output columns. The bounded SQLite sandbox
    # executes the explicit recursive columns and drops only this decoration.
    sql = re.sub(
        r"(?is)\s+CYCLE\s+.+?\s+SET\s+[A-Za-z_][A-Za-z0-9_]*"
        r"(?:\s+TO\s+\S+\s+DEFAULT\s+\S+)?\s+USING\s+"
        r"[A-Za-z_][A-Za-z0-9_]*\s+(?=SELECT\b)",
        " ",
        sql,
    )

    # SQLite accepts RECURSIVE on a WITH clause even when its CTEs are not
    # recursive, which also covers SQL Server's implicit recursive CTE syntax.
    sql = re.sub(r"(?is)^\s*WITH\s+(?!RECURSIVE\b)", "WITH RECURSIVE ", sql, count=1)
    return sql


def _rewrite_bare_offset(sql: str) -> str:
    pattern = re.compile(r"(?is)(\bLIMIT\s+[^\s;]+\s+)?\bOFFSET\s+(\d+)\b")

    def replace(match: re.Match) -> str:
        limit = match.group(1)
        if limit:
            return f"{limit}OFFSET {match.group(2)}"
        return f"LIMIT -1 OFFSET {match.group(2)}"

    return pattern.sub(replace, sql)


def _rewrite_similar_to(sql: str) -> str:
    identifier = r'(?:"[^"]+"|[A-Za-z_][A-Za-z0-9_]*)'
    qualified = rf'{identifier}(?:\s*\.\s*{identifier})?'
    literal = r"'(?:''|[^'])*'"
    pattern = re.compile(
        rf"(?is)(?P<value>{qualified})\s+SIMILAR\s+TO\s+"
        rf"(?P<pattern>{literal})(?:\s+ESCAPE\s+(?P<escape>{literal}))?"
    )

    def replace(match: re.Match) -> str:
        arguments = [match.group("value"), match.group("pattern")]
        if match.group("escape") is not None:
            arguments.append(match.group("escape"))
        return f"SIMILAR_TO({', '.join(arguments)})"

    return pattern.sub(replace, sql)


def _sqlite_compat(sql: str) -> str:
    sql = sql.rstrip().rstrip(";")
    sql = re.sub(r"\bISNULL\s*\(", "IFNULL(", sql, flags=re.IGNORECASE)
    # T-SQL bracket identifiers must be rewritten only in SQL code. Character
    # classes such as ``'[A-Z]'`` inside REGEXP string literals are data.
    quoted_parts = re.split(r"('(?:''|[^'])*')", sql)
    for index in range(0, len(quoted_parts), 2):
        quoted_parts[index] = re.sub(
            r"\[([^\]]+)\]",
            r'"\1"',
            quoted_parts[index],
        )
    sql = "".join(quoted_parts)
    # Handle Sys.Views in all quoting forms: bare, fully-quoted, and per-part-quoted
    sql = re.sub(r'(?is)(?:"Sys"\."Views"|Sys\.Views)', '"Sys.Views"', sql)
    sql = re.sub(r"(?is)CURRENT_DATE\s*-\s*INTERVAL\s*'1'\s+DAY", "date('now', '-1 day')", sql)
    sql = re.sub(
        r"(?is)\b(DATEADD|DATEDIFF|DATEPART)\s*\(\s*[\"`\[]?(YEAR|QUARTER|MONTH|DAY|WEEK|HOUR|MINUTE|SECOND)[\"`\]]?\s*,",
        lambda match: f"{match.group(1)}('{match.group(2).lower()}',",
        sql,
    )
    sql = re.sub(
        r"(?is)\bEXTRACT\s*\(\s*(YEAR|MONTH|DAY)\s+FROM\s+([^)]+?)\s*\)",
        lambda match: f"{match.group(1).upper()}({match.group(2).strip()})",
        sql,
    )
    sql = re.sub(r"(?is)\s+OPTION\s*\(\s*MAXRECURSION\s+\d+\s*\)\s*$", "", sql)
    sql = re.sub(r"(?is)^\s*SELECT\s+WITH\s+TIES\s+", "SELECT ", sql)
    sql = re.sub(r"(?is)(\bLIMIT\s+\d+)\s+WITH\s+TIES\b", r"\1", sql)
    sql = _rewrite_similar_to(sql)
    sql = _rewrite_bare_offset(sql)
    sql = _rewrite_parenthesized_union(sql)
    sql = _rewrite_quantified_subqueries(sql)
    sql = _replace_named_parameters(sql)
    return sql + ";"


def _manual_sqlite_compat(sql: str) -> str | None:
    sql = sql.strip().rstrip(";")
    top = re.match(r"(?is)^select\s+top\s+(\d+)\s+(?:with\s+ties\s+)?(.+)$", sql)
    if top:
        limit = top.group(1)
        body = top.group(2)
        order_match = re.search(r"(?is)\s+order\s+by\s+", body)
        if order_match:
            sql = "SELECT " + body + f" LIMIT {limit}"
        else:
            sql = "SELECT " + body + f" LIMIT {limit}"
    return _sqlite_compat(sql)


def _replace_named_parameters(sql: str) -> str:
    # Parameter markers inside quoted text are ordinary user data (for
    # example ``'Category:Articles'`` or an e-mail address), not bind
    # parameters. Process only the unquoted SQL segments.
    parts = re.split(r"('(?:''|[^'])*')", sql)
    for index in range(0, len(parts), 2):
        segment = parts[index]
        segment = re.sub(
            r":([A-Za-z_][A-Za-z0-9_]*)",
            lambda match: _parameter_literal(match.group(1)),
            segment,
        )
        segment = re.sub(
            r"@([A-Za-z_][A-Za-z0-9_]*)",
            lambda match: _parameter_literal(match.group(1)),
            segment,
        )
        segment = re.sub(r"(?i)(=\s*)student_name\b", r"\1'Alice'", segment)
        parts[index] = segment
    return "".join(parts)


def _parameter_literal(name: str) -> str:
    normalized = name.lower()
    if normalized in {"d", "dt"} or "date" in normalized:
        # Match the deterministic date domain produced by _seed_value so
        # parameterized equality predicates retain at least one positive row.
        return "'2024-01-01'"
    if "substring" in normalized:
        return "'A'"
    if "instructor" in normalized and "id" in normalized:
        return "1"
    if "student" in normalized and "name" in normalized:
        return "'Alice'"
    if normalized.endswith("id") or normalized.endswith("_id"):
        return "1"
    return "'Alice'"


def _rewrite_parenthesized_union(sql: str) -> str:
    return re.sub(
        r"(?is)^\s*\((SELECT.+?)\)\s+UNION\s+\((SELECT.+?)\)\s*$",
        r"\1 UNION \2",
        sql,
    )


def _rewrite_quantified_subqueries(sql: str) -> str:
    pattern = re.compile(
        r"(?is)([A-Za-z_][A-Za-z0-9_\\.]*|\"[^\"]+\")\s*(<=|>=|<>|!=|=|<|>)\s*"
        r"(ALL|ANY|SOME)\s*\(\s*SELECT\s+([A-Za-z_][A-Za-z0-9_\\.]*|\"[^\"]+\")\s+FROM\s+(.+?)\)",
    )

    def repl(match: re.Match) -> str:
        left, op, quantifier, selected, tail = match.groups()
        quantifier = quantifier.upper()
        aggregate = _quantifier_aggregate(op, quantifier)
        if aggregate is None:
            if op == "=" and quantifier in {"ANY", "SOME"}:
                return f"{left} IN (SELECT {selected} FROM {tail})"
            return match.group(0)
        return f"{left} {op} (SELECT {aggregate}({selected}) FROM {tail})"

    previous = None
    current = sql
    while previous != current:
        previous = current
        current = pattern.sub(repl, current)
    return current


def _quantifier_aggregate(op: str, quantifier: str) -> str | None:
    if quantifier == "ALL":
        if op in {">", ">="}:
            return "MAX"
        if op in {"<", "<="}:
            return "MIN"
    if quantifier in {"ANY", "SOME"}:
        if op in {">", ">="}:
            return "MIN"
        if op in {"<", "<="}:
            return "MAX"
    return None


def _mentions_sys_views(*sqls: str) -> bool:
    return any(re.search(r"(?is)\bSys\.Views\b", sql or "") for sql in sqls)


def _coerce_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        for pattern in ("%Y/%m/%d", "%m/%d/%Y", "%Y%m%d"):
            try:
                return datetime.strptime(text, pattern)
            except ValueError:
                continue
    return None


def _sql_date_part(part: Any, value: Any) -> int | None:
    parsed = _coerce_datetime(value)
    if parsed is None:
        return None
    normalized = str(part or "").lower()
    if normalized == "year":
        return parsed.year
    if normalized == "quarter":
        return (parsed.month - 1) // 3 + 1
    if normalized == "month":
        return parsed.month
    if normalized in {"day", "dayofmonth"}:
        return parsed.day
    if normalized in {"week", "weekofyear"}:
        return int(parsed.strftime("%W")) + 1
    if normalized == "hour":
        return parsed.hour
    if normalized == "minute":
        return parsed.minute
    if normalized == "second":
        return parsed.second
    return None


def _sql_date_add(part: Any, amount: Any, value: Any) -> str | None:
    parsed = _coerce_datetime(value)
    if parsed is None:
        return None
    try:
        count = int(amount)
    except (TypeError, ValueError):
        return None
    normalized = str(part or "").lower()
    if normalized in {"year", "quarter", "month"}:
        months = count * (12 if normalized == "year" else 3 if normalized == "quarter" else 1)
        month_index = parsed.year * 12 + parsed.month - 1 + months
        year, month_zero = divmod(month_index, 12)
        month = month_zero + 1
        day = min(parsed.day, 28)
        result = parsed.replace(year=year, month=month, day=day)
    else:
        units = {
            "week": timedelta(weeks=count),
            "day": timedelta(days=count),
            "hour": timedelta(hours=count),
            "minute": timedelta(minutes=count),
            "second": timedelta(seconds=count),
        }
        result = parsed + units.get(normalized, timedelta(days=count))
    has_time = any((result.hour, result.minute, result.second, result.microsecond))
    return result.strftime("%Y-%m-%d %H:%M:%S" if has_time else "%Y-%m-%d")


def _sql_date_diff(part: Any, start: Any, end: Any) -> int | None:
    start_date = _coerce_datetime(start)
    end_date = _coerce_datetime(end)
    if start_date is None or end_date is None:
        return None
    normalized = str(part or "").lower()
    if normalized == "year":
        return end_date.year - start_date.year
    if normalized == "month":
        return (end_date.year - start_date.year) * 12 + end_date.month - start_date.month
    seconds = (end_date - start_date).total_seconds()
    divisors = {"week": 604800, "day": 86400, "hour": 3600, "minute": 60, "second": 1}
    return int(seconds / divisors.get(normalized, 86400))


def _sql_date_diff_mysql(end: Any, start: Any) -> int | None:
    return _sql_date_diff("day", start, end)


def _width_bucket(value: Any, minimum: Any, maximum: Any, buckets: Any) -> int:
    try:
        value_f = float(value)
        min_f = float(minimum)
        max_f = float(maximum)
        bucket_count = max(1, int(float(buckets)))
    except Exception:
        return 1
    if max_f <= min_f:
        return 1
    if value_f < min_f:
        return 0
    if value_f >= max_f:
        return bucket_count + 1
    width = (max_f - min_f) / bucket_count
    return int((value_f - min_f) / width) + 1


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


def _with_ties_limit_count(ast: exp.Expression | None) -> tuple[bool, int | None]:
    select = _top_select(ast) if ast is not None else None
    limit = select.args.get("limit") if isinstance(select, exp.Select) else None
    if not isinstance(limit, (exp.Limit, exp.Fetch)):
        return False, None
    options = limit.args.get("limit_options")
    with_ties = bool(
        isinstance(options, exp.LimitOptions)
        and options.args.get("with_ties")
    )
    count_node = (
        getattr(limit, "expression", None)
        or limit.args.get("count")
        or limit.args.get("this")
    )
    return with_ties, _integer_node_value(count_node)


def _with_ties_probe_cutoff(
    standard_ast: exp.Expression | None,
    student_ast: exp.Expression | None,
) -> int | None:
    standard_with_ties, standard_count = _with_ties_limit_count(standard_ast)
    student_with_ties, student_count = _with_ties_limit_count(student_ast)
    if standard_with_ties == student_with_ties:
        return None
    cutoff = standard_count if standard_with_ties else student_count
    return cutoff if isinstance(cutoff, int) and cutoff > 0 else None


def _with_ties_probe_indexes(
    row_count: int,
    cutoff: int | None,
    descending: bool,
) -> tuple[int, int] | None:
    if cutoff is None or cutoff < 1 or row_count <= cutoff:
        return None
    if descending:
        return row_count - cutoff, row_count - cutoff - 1
    return cutoff - 1, cutoff


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
    with_ties_cutoff = _with_ties_probe_cutoff(ast, student_ast)
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
                tie_indexes: tuple[int, int] | None = None
                if _has_diff(ast_diffs, "LIMIT"):
                    new_vals = _extend_order_series(vals, len(rows))
                    tie_indexes = _with_ties_probe_indexes(
                        len(rows),
                        with_ties_cutoff,
                        p_desc,
                    )
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

                if tie_indexes:
                    boundary_index, extra_index = tie_indexes
                    rows[extra_index][p_name] = rows[boundary_index][p_name]
                    if s_name:
                        rows[extra_index][s_name] = rows[boundary_index][s_name]

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


def _positive_numeric_series_for_comparison(
    comparison: exp.Expression,
    boundary: int | float | Decimal,
    count: int,
) -> list[Any]:
    if isinstance(comparison, (exp.GT, exp.GTE, exp.EQ)):
        start = boundary + (1 if isinstance(comparison, exp.GT) else 0)
        return [start + index for index in range(count)]
    if isinstance(comparison, (exp.LT, exp.LTE)):
        start = boundary - (1 if isinstance(comparison, exp.LT) else 0)
        return [start - index for index in range(count)]
    return [boundary for _ in range(count)]


def _extend_order_series(values: list[Any], count: int) -> list[Any]:
    if not values:
        return list(range(count))
    unique: list[Any] = []
    for value in values:
        if value not in unique:
            unique.append(value)
    if len(unique) >= count:
        return unique[:count]
    last = unique[-1]
    if isinstance(last, (int, float, Decimal)):
        while len(unique) < count:
            last = last + 1
            unique.append(last)
        return unique
    parsed = _coerce_datetime(last)
    if parsed is not None:
        while len(unique) < count:
            parsed = parsed + timedelta(days=1)
            unique.append(parsed.strftime("%Y-%m-%d"))
        return unique
    while len(unique) < count:
        unique.append(f"{last}__{len(unique):03d}")
    return unique


def _comparison_matches(node: exp.Expression, value: Any) -> bool:
    literal_node = node.right if isinstance(node.right, exp.Literal) else node.left
    literal = _literal_value(literal_node)
    if isinstance(node, exp.EQ):
        return value == literal
    if isinstance(node, exp.NEQ):
        return value != literal
    if not isinstance(value, (int, float, Decimal)) or not isinstance(
        literal, (int, float, Decimal)
    ):
        return False
    if isinstance(node, exp.GT):
        return value > literal
    if isinstance(node, exp.GTE):
        return value >= literal
    if isinstance(node, exp.LT):
        return value < literal
    if isinstance(node, exp.LTE):
        return value <= literal
    return False


def _comparison_truth_value(node: exp.Expression, desired: bool) -> Any | None:
    if not isinstance(node.left, exp.Column) or not isinstance(node.right, exp.Literal):
        return None
    literal = _literal_value(node.right)
    if isinstance(node, exp.EQ):
        if desired:
            return literal
        if isinstance(literal, (int, float, Decimal)):
            return literal + 999
        return f"not_{literal}"
    if isinstance(node, exp.NEQ):
        if not desired:
            return literal
        if isinstance(literal, (int, float, Decimal)):
            return literal + 999
        return f"not_{literal}"
    if not isinstance(literal, (int, float, Decimal)):
        return None
    if isinstance(node, exp.GT):
        return literal + 1 if desired else literal
    if isinstance(node, exp.GTE):
        return literal if desired else literal - 1
    if isinstance(node, exp.LT):
        return literal - 1 if desired else literal
    if isinstance(node, exp.LTE):
        return literal if desired else literal + 1
    return None


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
    if _apply_logical_tree_counterexample_probe(
        data,
        standard_ast,
        student_ast,
        standard_where,
        student_where,
    ):
        return
    if _apply_compound_logic_truth_table_probe(
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


def _logical_leaf_key(node: exp.Expression) -> str:
    return _sql_of(_unwrap_paren(node))


def _logical_leaf_nodes(node: exp.Expression) -> list[exp.Expression]:
    leaves: list[exp.Expression] = []

    def walk(current: exp.Expression) -> None:
        current = _unwrap_paren(current)
        if isinstance(current, (exp.And, exp.Or)):
            walk(current.left)
            walk(current.right)
        else:
            leaves.append(current)

    walk(node)
    return leaves


def _eval_logical_tree(node: exp.Expression, values: dict[str, bool]) -> bool:
    node = _unwrap_paren(node)
    if isinstance(node, exp.And):
        return _eval_logical_tree(node.left, values) and _eval_logical_tree(node.right, values)
    if isinstance(node, exp.Or):
        return _eval_logical_tree(node.left, values) or _eval_logical_tree(node.right, values)
    return values[_logical_leaf_key(node)]


def _predicate_truth_assignment(node: exp.Expression, desired: bool) -> tuple[exp.Column, Any] | None:
    node = _unwrap_paren(node)
    if isinstance(node, (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE)):
        if isinstance(node.left, exp.Column) and isinstance(node.right, exp.Literal):
            value = _comparison_truth_value(node, desired)
            return (node.left, value) if value is not None else None
    if isinstance(node, exp.Like) and isinstance(node.this, exp.Column) and isinstance(node.expression, exp.Literal):
        pattern = str(_literal_value(node.expression))
        if desired:
            candidate = pattern.replace("%", "X").replace("_", "X")
        else:
            candidate = "__no_like_match__"
        return node.this, candidate
    return None


def _compatible_leaf_updates(
    leaves: list[exp.Expression],
    desired_truth: dict[str, bool],
    aliases: dict[str, str],
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
        key = (aliases.get(table_ref, table_ref), _norm_name(column.name))
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
) -> None:
    """Materialize first-match CASE branches after heuristic type repair."""
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
    source_tables = {
        _norm_name(table.name)
        for table in ast.find_all(exp.Table)
        if table.name
    }
    for case_node in ast.find_all(exp.Case):
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
        for table_name, rows in data.items():
            if source_tables and _norm_name(table_name) not in source_tables:
                continue
            if len(rows) < len(predicates) + 1:
                continue
            lookup = _column_lookup(rows[0].keys())
            materialized = True
            assignments: list[dict[str, bool]] = []
            for branch_index, predicate in enumerate(predicates):
                desired = {
                    _logical_leaf_key(leaf): False
                    for prior in predicates[:branch_index]
                    for leaf in _logical_leaf_nodes(prior)
                }
                branch_leaves = _logical_leaf_nodes(predicate)
                if len(branch_leaves) != 1:
                    materialized = False
                    break
                desired[_logical_leaf_key(branch_leaves[0])] = True
                assignments.append(desired)
            assignments.append({_logical_leaf_key(leaf): False for leaf in leaves})
            if not materialized:
                continue
            with write_owner("materializer:case_branch_coverage"):
                for row, desired in zip(rows, assignments):
                    relevant = [leaf for leaf in leaves if _logical_leaf_key(leaf) in desired]
                    updates = _compatible_leaf_updates(relevant, desired, aliases)
                    if not updates:
                        materialized = False
                        break
                    for table_ref, column, value in updates:
                        if table_ref and table_ref != _norm_name(table_name):
                            materialized = False
                            break
                        actual = lookup.get(column)
                        if not actual:
                            materialized = False
                            break
                        row[actual] = value
                    if not materialized:
                        break
            if materialized:
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


def _window_companion_aliases(
    specs: dict[str, list[tuple[exp.Expression, int | float]]],
    changed_aliases: set[str],
) -> set[str]:
    companions: set[str] = set()
    comparison_alias = {
        id(comparison): alias
        for alias, values in specs.items()
        for comparison, _ in values
    }
    for alias in changed_aliases:
        for comparison, _ in specs.get(alias, []):
            current = comparison.parent
            while isinstance(current, exp.And):
                for candidate in current.find_all(
                    exp.EQ,
                    exp.NEQ,
                    exp.GT,
                    exp.GTE,
                    exp.LT,
                    exp.LTE,
                ):
                    companion = comparison_alias.get(id(candidate))
                    if companion and companion != alias:
                        companions.add(companion)
                current = current.parent
    return companions


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
    for predicate in ast.find_all(exp.Where, exp.Having, exp.Qualify):
        predicate_columns = {
            _norm_name(column.name) for column in predicate.find_all(exp.Column)
        }
        if changed_columns & predicate_columns:
            companions.update(predicate_columns & aliases)
    return companions


def _window_partition_columns(window: exp.Window) -> list[exp.Column]:
    columns: list[exp.Column] = []
    for expression in window.args.get("partition_by") or []:
        candidates = [expression] if isinstance(expression, exp.Column) else list(expression.find_all(exp.Column))
        for column in candidates:
            if column not in columns:
                columns.append(column)
    return columns


def _assign_window_groups(
    rows: list[dict[str, Any]],
    columns: list[str],
    group_size: int,
) -> None:
    if not rows or not columns:
        return
    group_size = max(1, group_size)
    for index, row in enumerate(rows):
        group = index // group_size
        for position, column in enumerate(columns):
            row[column] = _group_probe_value(column, group, position + 20)


def _assign_window_order_values(
    rows: list[dict[str, Any]],
    columns: list[str],
) -> None:
    for index, row in enumerate(rows):
        for position, column in enumerate(columns):
            if _is_date_column(column):
                row[column] = f"2024-02-{(index % 28) + 1:02d}"
            elif _is_numeric_column(column):
                row[column] = index * 10 + position + 1
            else:
                row[column] = f"__window_order_{position}_{index:04d}__"


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


def _group_probe_value(column: str, bucket: int, salt: int) -> Any:
    if _is_date_column(column):
        day = 1 + ((bucket + salt) % 28)
        return f"2024-01-{day:02d}"
    if _is_numeric_column(column):
        return 100 + salt * 10 + bucket
    return f"__group_{salt}_{bucket}__"


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


def _clean_identifier(value: str) -> str:
    value = value.strip()
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    if value.startswith("`") and value.endswith("`"):
        value = value[1:-1]
    if value.startswith('"') and value.endswith('"'):
        value = value[1:-1]
    return value.strip()


def _norm_name(value: str) -> str:
    return re.sub(r"[^a-z0-9_]", "", _clean_identifier(value).lower())


def _normalize_cell(value: Any) -> Any:
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _apply_case_probes(
    data: dict[str, list[dict[str, Any]]],
    schema: dict[str, list[str]],
    standard_sql: str,
    student_sql: str,
    ast_diffs: list[ASTDiffNode],
) -> None:
    """
    CASE WHEN 分支遍历探针：为每个 WHEN 条件生成匹配该分支的数据行。
    确保每个 THEN 分支至少有一行数据命中，暴露分支遗漏或条件错误。
    """
    asts = [_parse_sql(standard_sql), _parse_sql(student_sql)]

    for ast in asts:
        if not ast:
            continue
        for case_node in ast.find_all(exp.Case):
            # 提取所有 WHEN 条件
            when_conditions = []
            ifs = case_node.args.get("ifs") or []
            for if_node in ifs:
                if isinstance(if_node, exp.If):
                    cond = if_node.this
                    if cond:
                        when_conditions.append(cond)

            if not when_conditions:
                continue

            # 对每个条件，提取涉及的列和值，在对应表中注入匹配数据
            for branch_idx, cond in enumerate(when_conditions):
                # 提取条件中的列和字面值
                for cmp in cond.find_all(exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE):
                    col_node = cmp.left if isinstance(cmp.left, exp.Column) else cmp.right if isinstance(cmp.right, exp.Column) else None
                    lit_node = cmp.right if isinstance(cmp.left, exp.Column) else cmp.left if isinstance(cmp.right, exp.Column) else None

                    if not isinstance(col_node, exp.Column) or not isinstance(lit_node, exp.Literal):
                        continue

                    col_name = _norm_name(col_node.name)
                    table_ref = _norm_name(col_node.table or "")
                    value = _literal_value(lit_node)

                    # 找到对应的表
                    for table_name, columns in schema.items():
                        if table_ref and _norm_name(table_name) != table_ref:
                            continue
                        lookup = _column_lookup(columns)
                        actual_col = lookup.get(col_name)
                        if not actual_col or table_name not in data:
                            continue

                        rows = data[table_name]
                        if not rows:
                            continue

                        # 在 branch_idx 对应的行注入匹配该分支的值
                        target_row_idx = branch_idx % len(rows)
                        rows[target_row_idx][actual_col] = value


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
) -> None:
    cte_name = _norm_name(cte.alias or "")
    set_node = _set_operator_node(cte.this if isinstance(cte.this, exp.Expression) else None)
    recursive_branch = set_node.expression if isinstance(set_node, (exp.Union, exp.Intersect, exp.Except)) else None
    if not cte_name or not isinstance(recursive_branch, exp.Expression):
        return
    aliases = _table_aliases(recursive_branch)

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
        if not child_actual or not ancestor_actual:
            continue
        rows = data[table_actual]
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
            _apply_recursive_cte_hierarchy(data, schema, cte)


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


from abc import ABC, abstractmethod

class Tactic(ABC):
    phase: int = 0
    trigger_clauses: tuple[str, ...] = ()
    trigger_diff_types: tuple[str, ...] = ()
    trigger_kps: tuple[str, ...] = ()

    @property
    @abstractmethod
    def name(self) -> str:
        pass

    def can_trigger(self, ast_diffs: list[ASTDiffNode]) -> bool:
        if not ast_diffs:
            return False
        for diff in ast_diffs:
            if self._matches_diff(diff):
                return True
        return False

    def _matches_diff(self, diff: ASTDiffNode) -> bool:
        if self.trigger_clauses and not diff.matches_clause(*self.trigger_clauses):
            return False
        if self.trigger_diff_types and not diff.matches_diff_type(*self.trigger_diff_types):
            return False
        if self.trigger_kps and diff.knowledge_point_id not in self.trigger_kps:
            return False
        return True

    @abstractmethod
    def apply_data_probe(
        self,
        data: dict[str, list[dict[str, Any]]],
        schema: dict[str, list[str]],
        standard_sql: str,
        student_sql: str,
        ast_diffs: list[ASTDiffNode]
    ) -> None:
        pass


class JoinOnCounterexampleTactic(Tactic):
    phase = 5
    trigger_clauses = ("JOIN", "JOIN_TYPE", "JOIN ON")
    trigger_diff_types = ("join_missing", "join_type_changed", "join_on_changed")
    trigger_kps = ("join-inner", "join-left", "join-right", "join-full", "join-on")

    @property
    def name(self) -> str:
        return "join_on_counterexample"

    def apply_data_probe(self, data, schema, standard_sql, student_sql, ast_diffs):
        _apply_join_on_counterexample(data, standard_sql, student_sql, ast_diffs)


class OrderByTiesTactic(Tactic):
    phase = 12
    trigger_clauses = ("ORDER BY", "WINDOW", "LIMIT")
    trigger_diff_types = (
        "limit_changed",
        "window_over_changed",
        "window_function_changed",
    )
    trigger_kps = ("order-by", "window-row-number", "limit")

    @property
    def name(self) -> str:
        return "ordered_compare_probe"

    def apply_data_probe(self, data, schema, standard_sql, student_sql, ast_diffs):
        _apply_order_by_probes(data, standard_sql, student_sql, ast_diffs)


class WindowPartitionTactic(Tactic):
    phase = 9
    trigger_clauses = ("WINDOW",)
    trigger_diff_types = ("window_over_changed", "window_function_changed")
    trigger_kps = ("window-row-number",)

    @property
    def name(self) -> str:
        return "window_partition_order_probe"

    def apply_data_probe(self, data, schema, standard_sql, student_sql, ast_diffs):
        _apply_window_probes(data, standard_sql, student_sql, ast_diffs)


class GroupByProbesTactic(Tactic):
    phase = 6
    trigger_clauses = ("GROUP BY", "HAVING")
    trigger_diff_types = (
        "group_by_changed",
        "group_by_expression_changed",
        "grouping_grain_too_fine",
        "grouping_grain_too_coarse",
        "having_changed",
    )
    trigger_kps = ("group-by", "having")

    @property
    def name(self) -> str:
        return "group_cardinality_probe"

    def apply_data_probe(self, data, schema, standard_sql, student_sql, ast_diffs):
        _apply_group_by_probes(data, standard_sql, student_sql, ast_diffs)


class SetOperatorProbesTactic(Tactic):
    phase = 8
    trigger_clauses = ("UNION", "INTERSECT", "EXCEPT")
    trigger_diff_types = ("set_operator_changed", "set_modifier_changed")
    trigger_kps = ("union", "intersect", "except")

    @property
    def name(self) -> str:
        return "set_operator_overlap_probe"

    def apply_data_probe(self, data, schema, standard_sql, student_sql, ast_diffs):
        _apply_set_operator_probes(data, standard_sql, student_sql, ast_diffs)


class CteProbesTactic(Tactic):
    phase = 10
    trigger_clauses = ("CTE", "CTE_RECURSIVE")
    trigger_diff_types = ("cte_changed", "recursive_cte_changed")
    trigger_kps = ("cte", "cte-recursive")

    @property
    def name(self) -> str:
        return "cte_constraint_probe"

    def apply_data_probe(self, data, schema, standard_sql, student_sql, ast_diffs):
        _apply_cte_probes(data, schema, standard_sql, student_sql, ast_diffs)


class CaseWhenProbesTactic(Tactic):
    phase = 13
    trigger_clauses = ()
    trigger_diff_types = ()
    trigger_kps = ()

    @property
    def name(self) -> str:
        return "case_branch_probe"

    def can_trigger(self, ast_diffs: list[ASTDiffNode]) -> bool:
        return any(
            diff.matches_clause("CASE", "SELECT")
            or diff.diff_type == "case_changed"
            for diff in ast_diffs
        )

    def apply_data_probe(self, data, schema, standard_sql, student_sql, ast_diffs):
        _apply_case_probes(data, schema, standard_sql, student_sql, ast_diffs)


class TacticRegistry:
    _registry: list[Tactic] = []

    @classmethod
    def register(cls, tactic: Tactic) -> None:
        cls._registry.append(tactic)

    @classmethod
    def get_active_tactics(cls, ast_diffs: list[ASTDiffNode]) -> list[Tactic]:
        return [
            tactic
            for tactic in sorted(cls._registry, key=lambda item: (item.phase, item.name))
            if tactic.can_trigger(ast_diffs)
        ]

# Migrated tactic classes remain as compatibility definitions. Dispatch for
# JOIN, GROUP, SET, CTE, ORDER and WINDOW probes is owned by the adapters below;
# CASE branch topology is owned by the final obligation materializer.
# ``GroupByProbesTactic`` and ``SetOperatorProbesTactic`` are retained as
# compatibility definitions; their declared adapters below own dispatch.


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
    _apply_not_in_null_probe(data, standard_sql, student_sql, ast_diffs)


def _apply_join_key_drift_adapter(data, schema, standard_sql, student_sql, ast_diffs):
    _apply_join_on_counterexample(data, standard_sql, student_sql, ast_diffs)
    _materialize_declared_join_witness(data, ast_diffs)


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
    _materialize_declared_join_witness(data, ast_diffs)


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
    """Declare columns touched by ordinary and DISTINCT ON witness probes."""
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
            "distinct_on_changed",
        }),
        clauses=frozenset({"DISTINCT", "DISTINCT ON", "AGGREGATE"}),
        knowledge_points=frozenset({"distinct", "aggregate"}),
        read_set_factory=_distinct_column_set,
        write_set_factory=_distinct_column_set,
        sql_trigger=lambda standard, student: (
            _distinct_shape_changed(standard, student)
            or _distinct_on_order_changed(standard, student)
            or _distinct_on_projection_changed(standard, student)
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


__all__ = [
    "SandboxRun",
    "extract_ast_diffs",
    "generate_and_compare",
    "generate_test_database",
    "generate_witness_suite",
    "parse_schema_column_types",
    "parse_schema_text",
    "transpile_to_sqlite",
]
