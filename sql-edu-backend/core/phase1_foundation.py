"""Bounded SQLite Phase 1 contracts and dependency-free primitives."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Iterable
from collections import Counter
from contextvars import ContextVar
import hashlib
import math
import json
import re
import sqlglot
from sqlglot import ErrorLevel, exp
from sqlglot.dialects.sqlite import SQLite
from core.ast_schema import ASTDiffNode
from core.witness_generation.schema_scope import SchemaQualification
from core.witness_generation.obligations import (
    is_redundant_summary_diff,
    stable_diff_id,
)
from core.witness_generation.planner import (
    WitnessSuite,
    WitnessWorld,
    declare_strategy,
)
from core.witness_generation.validators import validate_obligation
from core.phase1_verdict import project_failure

# This research build has exactly one parsing and execution contract: SQLite.
class SQLiteQueryParseError(ValueError):
    def __init__(self, code: str, message: str, *, sql_role: str | None = None):
        super().__init__(message)
        self.code = code
        self.sql_role = sql_role


def parse_sqlite_pair_or_raise(
    *,
    standard_sql: str,
    student_sql: str,
) -> tuple[exp.Query, exp.Query]:
    """Parse exactly one read-only query per side under SQLite grammar."""

    asts: list[exp.Query] = []
    for role, sql in (("standard", standard_sql), ("student", student_sql)):
        try:
            statements = sqlglot.parse(
                sql,
                read="sqlite",
                error_level=ErrorLevel.RAISE,
            )
        except Exception as exc:
            code = f"{role.upper()}_SQL_PARSE_ERROR"
            raise SQLiteQueryParseError(code, str(exc), sql_role=role) from exc
        parsed = [
            statement
            for statement in statements
            if statement is not None and not isinstance(statement, exp.Semicolon)
        ]
        if len(parsed) != 1 or not isinstance(parsed[0], exp.Query):
            code = f"{role.upper()}_SQL_PARSE_ERROR"
            raise SQLiteQueryParseError(
                code,
                f"{role} SQL must contain exactly one query",
                sql_role=role,
            )
        asts.append(parsed[0])
    return asts[0], asts[1]


# SQLGlot 29 groups CTE output-column lists with derived-table alias columns
# behind one generator flag. SQLite supports the former, and recursive CTEs
# rely on them, so preserve those columns during deterministic rendering.
SQLite.Generator.SUPPORTS_TABLE_ALIAS_COLUMNS = True

_MUTATION_ORIGINAL_EQUIVALENT: ContextVar[bool] = ContextVar(
    "parseval_mutation_original_equivalent",
    default=False,
)

_MAX_WITNESS_WORLDS = 8
_MAX_WITNESS_ATTEMPTS = 8
_MAX_WITNESS_ROWS_PER_TABLE = 32
# Execution evidence needs enough rows to compare duplicate/set semantics,
# but recursive or Cartesian teaching queries must never be copied without a
# hard cap.  This is deliberately independent of the physical-table limit.
_MAX_RECORDED_RESULT_ROWS = 256
_SQLITE_PROGRESS_GRANULARITY = 10_000
_SQLITE_VM_INSTRUCTION_BUDGET = 1_000_000
_SQLITE_EXECUTION_TIME_BUDGET_SECONDS = 0.5

# Phase 1 -> Phase 2 query-scope contract.  These limits are deliberately
# independent of the SQL execution limits: scope metadata is diagnostic
# evidence and must never become an unbounded copy of the parsed AST.
_SCOPE_METADATA_VERSION = "phase1.scope-metadata.v1"
_MAX_SCOPE_AST_NODES_SCANNED = 8_192
_MAX_SCOPE_NODES = 128
_MAX_SCOPE_EDGES = 256
_MAX_SCOPE_DIFFS = 256
_MAX_SCOPE_DIFF_BINDINGS = 512
_MAX_SCOPE_PATH_DEPTH = 48


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


def _quote_numeric_schema_identifiers(
    sql: str,
    schema: dict[str, list[str]],
) -> str:
    """Quote schema-owned identifiers whose source spelling starts with a digit.

    WikiSQL headers such as ``2007`` are identifiers in the source corpus, but
    a generic SQL parser (and SQLite) otherwise reads the same token as a
    numeric literal.  This is a lexical, schema-aware repair: only declared
    numeric-leading table/column names are changed, and strings, comments, and
    already quoted identifiers are copied byte-for-byte.
    """
    names = {
        str(identifier).casefold()
        for table, columns in schema.items()
        for identifier in (table, *columns)
        if identifier and str(identifier)[0].isdigit()
    }
    if not names:
        return sql

    output: list[str] = []
    index = 0
    quote: str | None = None
    line_comment = False
    block_comment = False
    while index < len(sql):
        char = sql[index]
        next_char = sql[index + 1] if index + 1 < len(sql) else ""
        if line_comment:
            output.append(char)
            if char in "\r\n":
                line_comment = False
            index += 1
            continue
        if block_comment:
            output.append(char)
            if char == "*" and next_char == "/":
                output.append(next_char)
                index += 2
                block_comment = False
            else:
                index += 1
            continue
        if quote:
            output.append(char)
            if char == quote:
                if index + 1 < len(sql) and sql[index + 1] == quote:
                    output.append(sql[index + 1])
                    index += 2
                    continue
                quote = None
            index += 1
            continue
        if char == "-" and next_char == "-":
            output.extend((char, next_char))
            index += 2
            line_comment = True
            continue
        if char == "/" and next_char == "*":
            output.extend((char, next_char))
            index += 2
            block_comment = True
            continue
        if char in "'\"`[":
            quote = "]" if char == "[" else char
            output.append(char)
            index += 1
            continue
        if char.isdigit() and (
            index == 0
            or not (sql[index - 1].isalnum() or sql[index - 1] in "_$")
        ):
            end = index + 1
            while end < len(sql) and (
                sql[end].isalnum() or sql[end] in "_$"
            ):
                end += 1
            token = sql[index:end]
            if token.casefold() in names:
                output.append('"' + token.replace('"', '""') + '"')
            else:
                output.append(token)
            index = end
            continue
        output.append(char)
        index += 1
    return "".join(output)


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


def _with_parent_cte_context(
    full_sql: str,
    query_sql: str,
) -> str:
    """Make a nested query block executable with its parent CTE bindings."""
    root_ast = _parse_sql(full_sql)
    nested_ast = _parse_sql(query_sql)
    if root_ast is None or nested_ast is None:
        return query_sql
    with_node = root_ast.args.get("with_")
    if not isinstance(with_node, exp.With) or not with_node.expressions:
        return query_sql
    nested_select = nested_ast
    if not isinstance(nested_select, exp.Select):
        return query_sql
    wrapper = exp.Select(expressions=[exp.Star()])
    wrapper.set(
        "from_",
        exp.From(this=exp.Subquery(this=nested_select, alias=exp.TableAlias(this=exp.Identifier(this="__nested_query")))),
    )
    wrapper.set("with_", with_node.copy())
    return _sql_of(wrapper)


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
        # Keep the public sample small in the ordinary run object, while the
        # bounded full prefix lets semantic validators distinguish a duplicate
        # from a merely different tail.  Without this, a 16-row DISTINCT
        # witness was truncated to five rows and the validator incorrectly
        # concluded that the projected value sets differed.
        "standard_result": run.standard_rows[:_MAX_RECORDED_RESULT_ROWS],
        "student_result": run.student_rows[:_MAX_RECORDED_RESULT_ROWS],
        "result_rows_truncated": (
            len(run.standard_rows) > _MAX_RECORDED_RESULT_ROWS
            or len(run.student_rows) > _MAX_RECORDED_RESULT_ROWS
        ),
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
    null_antijoin_ids = [
        diff_id
        for diff_id, diff in indexed
        if diff.diff_type == "null_sensitive_antijoin_equivalence"
    ]
    correlated_predicate_ids = [
        diff_id
        for diff_id, diff in indexed
        if diff.diff_type == "correlated_predicate_changed"
    ]
    recursive_step_ids = [
        diff_id
        for diff_id, diff in indexed
        if diff.diff_type == "recursive_step_expression_changed"
    ]
    set_operator_ids = [
        diff_id
        for diff_id, diff in indexed
        if diff.diff_type == "set_operator_changed"
    ]
    null_predicate_ids = [
        diff_id
        for diff_id, diff in indexed
        if diff.diff_type in {
            "null_equality_changed",
            "null_predicate_negation_changed",
        }
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
        # NOT IN/NOT EXISTS is represented as a focused NULL obligation, but
        # the legacy clause mutator restores the enclosing WHERE.  Clause-only
        # matching therefore loses the causal link even though the mutation
        # is the exact repair.  Use this semantic identity only when the AST
        # pass proved there is one such focused diff; multiple NULL differences
        # remain conservative and are not guessed.
        semantic_match = (
            knowledge_point in {"null-handling", "comp-null", "subquery-correlated"}
            and len(null_antijoin_ids) == 1
            and item.get("action") == "replace_student_clause_with_standard_clause"
        )
        null_predicate_match = (
            knowledge_point in {"null-handling", "comp-null"}
            and len(null_predicate_ids) == 1
            and item.get("action") == "replace_student_clause_with_standard_clause"
        )
        case_null_predicate_match = (
            knowledge_point == "case"
            and clause == "CASE"
            and len(null_predicate_ids) == 1
            and item.get("action") == "replace_student_clause_with_standard_clause"
        )
        correlated_match = (
            knowledge_point == "subquery-correlated"
            and len(correlated_predicate_ids) == 1
            and item.get("action") in {
                "restore_correlated_comparison",
                "restore_correlated_predicate",
            }
        )
        recursive_match = (
            knowledge_point == "cte-recursive"
            and clause in {"RECURSIVE CTE", "CTE", "WITH"}
            and len(recursive_step_ids) == 1
            and item.get("action") == "replace_student_clause_with_standard_clause"
        )
        set_operator_match = (
            knowledge_point == "cte-recursive"
            and clause in {"RECURSIVE CTE", "CTE", "WITH"}
            and len(set_operator_ids) == 1
            and item.get("action") == "replace_student_clause_with_standard_clause"
        )
        # A removed predicate inside one branch can produce a literal-diff
        # summary plus the focused predicate_missing node. Prefer the focused
        # node for a whole-WHERE replacement when it is unique; this keeps the
        # repair binding causal instead of declaring the pair ambiguous.
        predicate_gap_ids = [
            diff_id
            for diff_id, diff in indexed
            if diff.diff_type in {"predicate_missing", "predicate_added"}
            and diff_id in clause_matches
        ]
        predicate_gap_match = (
            clause == "WHERE"
            and len(predicate_gap_ids) == 1
            and item.get("action") == "replace_student_clause_with_standard_clause"
        )
        if correlated_match:
            matches = correlated_predicate_ids
        elif recursive_match:
            matches = recursive_step_ids
        elif set_operator_match:
            matches = set_operator_ids
        elif predicate_gap_match:
            matches = predicate_gap_ids
        elif null_predicate_match:
            matches = null_predicate_ids
        elif case_null_predicate_match:
            matches = null_predicate_ids
        elif semantic_match:
            matches = null_antijoin_ids
        else:
            matches = kp_matches if len(kp_matches) == 1 else clause_matches
        item["diff_ids"] = matches
        item["obligation_ids"] = [
            f"obligation_{diff_id.removeprefix('diff_')}" for diff_id in matches
        ]
        item["binding_quality"] = (
            "exact"
            if len(matches) == 1 and (
                semantic_match
                or null_predicate_match
                or case_null_predicate_match
                or correlated_match
                or recursive_match
                or set_operator_match
                or predicate_gap_match
                or len(kp_matches) == 1
                or len(clause_matches) == 1
            )
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
        "PREDICATE": {"WHERE", "HAVING", "JOIN ON"},
        "LOGICAL": {"WHERE", "HAVING", "JOIN ON"},
        # A predicate-level mutation often replaces the enclosing WHERE even
        # when the atomic AST diff is IN/LIKE/BETWEEN. Keep that enclosing
        # clause linked to the one atomic predicate obligation.
        "IN": {"WHERE", "HAVING", "PREDICATE"},
        "LIKE": {"WHERE", "HAVING", "PREDICATE"},
        "BETWEEN": {"WHERE", "HAVING", "PREDICATE"},
        "SELECT": {"PROJECTION", "SELECT", "AGGREGATE", "WINDOW", "CASE"},
        "JOIN_TYPE": {"JOIN TYPE", "JOIN"},
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


def _semantic_literal_value(node: exp.Expression | None) -> Any:
    """Return scalar SQL literal values used by the temporal path materializer.

    ``_literal_value`` intentionally accepts only ``exp.Literal`` because it
    is also used by conservative AST extraction.  A witness writer needs the
    adjacent scalar nodes that SQLGlot represents separately (BOOLEAN and
    NULL), so keep this broader conversion local rather than weakening the
    parser's fail-closed contract.
    """
    if isinstance(node, exp.Boolean):
        return bool(node.this)
    if isinstance(node, exp.Null):
        return None
    return _literal_value(node)


def _comparison_node_from_diff(
    node: Any,
    sql_text: str | None,
) -> exp.Expression | None:
    """Normalize a diff endpoint to its comparison expression."""
    comparison_types = (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE)
    if isinstance(node, comparison_types):
        return node
    if isinstance(sql_text, str) and sql_text.strip():
        parsed = _parse_sql(sql_text)
        if isinstance(parsed, comparison_types):
            return parsed
        if parsed is not None:
            candidate = parsed.find(*comparison_types)
            if isinstance(candidate, comparison_types):
                return candidate
    return None


def _temporal_comparison_parts(
    comparison: exp.Expression,
) -> tuple[exp.Expression, exp.Column, Any] | None:
    """Return a direct SQLite date-column comparison and its literal."""
    if not isinstance(comparison, (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE)):
        return None
    left, right = comparison.left, comparison.right
    value_expression: exp.Expression | None = None
    literal_node: exp.Expression | None = None
    if isinstance(left, exp.Expression) and isinstance(right, (exp.Literal, exp.Boolean, exp.Null)):
        value_expression, literal_node = left, right
    elif isinstance(right, exp.Expression) and isinstance(left, (exp.Literal, exp.Boolean, exp.Null)):
        value_expression, literal_node = right, left
    if value_expression is None or literal_node is None:
        return None

    if not isinstance(value_expression, exp.Column):
        return None
    return value_expression, value_expression, _semantic_literal_value(literal_node)


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


def _materialized_order_keys(value: Any) -> list[tuple[str, bool]]:
    result: list[tuple[str, bool]] = []
    for item in value or ():
        if not isinstance(item, (tuple, list)) or len(item) < 2:
            continue
        expression = str(item[0] or "").strip()
        if expression:
            result.append((expression, bool(item[1])))
    return result


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
) -> SandboxRun:
    projection = project_failure(status)
    public_status = projection.status
    conclusion = projection.equivalence_conclusion
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
            "execution_backend": "sqlite",
            "sql_dialect": "sqlite",
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


def _is_platform_execution_error(exc: Exception) -> bool:
    """Return whether a student-side exception means no verdict is possible."""
    return _is_execution_timeout(exc)


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
            )
        ):
            return True
        current = current.__cause__
    return False


def _parse_sql(sql: str) -> exp.Expression | None:
    try:
        # All externally supplied and internally rewritten queries share the
        # same SQLite AST contract.
        parsed = sqlglot.parse_one(
            sql,
            read="sqlite",
            error_level=ErrorLevel.IGNORE,
        )
        if parsed is not None:
            # Guard against silent mis-parse.  sqlglot is very lenient and may
            # reinterpret keywords (for example ``FORM`` as an alias).
            raw_tokens = set(re.findall(r'\b[A-Za-z_]\w*\b', sql))
            keywords = {
                'SELECT', 'FROM', 'WHERE', 'AND', 'OR', 'NOT', 'AS', 'ON',
                'IN', 'IS', 'NULL', 'LIKE', 'BETWEEN', 'JOIN', 'LEFT',
                'RIGHT', 'INNER', 'OUTER', 'CROSS', 'GROUP', 'BY', 'ORDER',
                'HAVING', 'LIMIT', 'OFFSET', 'UNION', 'ALL', 'DISTINCT',
                'EXISTS', 'CASE', 'WHEN', 'THEN', 'ELSE', 'END', 'INSERT',
                'INTO', 'VALUES', 'UPDATE', 'SET', 'DELETE', 'CREATE', 'TABLE',
                'DROP', 'ALTER', 'INDEX', 'WITH', 'RECURSIVE', 'ASC', 'DESC',
                'TRUE', 'FALSE', 'CAST', 'INTERSECT', 'EXCEPT', 'IF', 'INTEGER',
                'INT', 'BIGINT', 'SMALLINT', 'DECIMAL', 'NUMERIC', 'VARCHAR',
                'CHAR', 'TEXT', 'DATE', 'TIMESTAMP', 'BOOLEAN', 'REAL', 'FLOAT',
                'DOUBLE', 'NULLS', 'FIRST', 'LAST', 'WINDOW', 'ROWS',
                'RANGE', 'PRECEDING', 'FOLLOWING', 'CURRENT', 'ROW', 'REGEXP',
                'GLOB', 'DAY', 'WEEK',
                'MONTH', 'QUARTER', 'YEAR', 'HOUR', 'MINUTE', 'SECOND',
                'MILLISECOND', 'MICROSECOND',
            }
            function_tokens = {
                token.casefold()
                for token in re.findall(r"\b([A-Za-z_]\w*)\s*\(", sql)
            }
            meaningful = {
                token.casefold()
                for token in raw_tokens
                if token.upper() not in keywords
                and token.casefold() not in function_tokens
            }
            if meaningful:
                roundtrip_tokens = {
                    token.casefold()
                    for token in re.findall(
                        r'\b[A-Za-z_]\w*\b',
                        parsed.sql(dialect="sqlite"),
                    )
                }
                if meaningful - roundtrip_tokens:
                    return None
            return parsed
    except Exception:
        return None
    return None


def _collect_subqueries(ast: exp.Expression) -> list[exp.Expression]:
    """Extract all subquery inner SELECT nodes from an AST (not the top-level SELECT).

    Covers: Subquery nodes (scalar, IN, FROM, WHERE) and Exists nodes.
    Returns the inner Select of each subquery, in traversal order.
    """
    result: list[exp.Expression] = []
    seen: set[int] = set()
    # ``find_all(Subquery)`` followed by ``find_all(Exists)`` does not preserve
    # SQL traversal order.  In a predicate sequence such as ``IN(A) AND
    # IN(B) AND NOT IN(C)`` -> ``IN(A) AND EXISTS(B) AND NOT IN(C)``, that
    # split collection pairs B with C and manufactures unrelated nested
    # literal/subquery diffs.  Walk both node kinds together so corresponding
    # query blocks retain their source order.
    for node in ast.walk():
        if isinstance(node, exp.In) and isinstance(node.args.get("query"), exp.Subquery):
            inner = node.args["query"].this
        elif isinstance(node, exp.Subquery):
            inner = node.this
        elif isinstance(node, exp.Exists):
            inner = node.this
            if isinstance(inner, exp.Subquery):
                inner = inner.this
        else:
            continue
        if isinstance(inner, exp.Select) and id(inner) not in seen:
            seen.add(id(inner))
            result.append(inner)
    return result


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


_MIRRORED_COMPARISON_TYPES: dict[type[exp.Expression], type[exp.Expression]] = {
    exp.GT: exp.LT,
    exp.LT: exp.GT,
    exp.GTE: exp.LTE,
    exp.LTE: exp.GTE,
    exp.EQ: exp.EQ,
    exp.NEQ: exp.NEQ,
}


def _comparison_operands_mirrored_equivalent(
    standard_ast: exp.Expression,
    student_ast: exp.Expression,
) -> bool:
    """Recognize comparison operand/order mirrors without weakening 3VL.

    ``a > 1`` and ``1 < a`` (and their equality counterparts) have the same
    SQL truth value, including UNKNOWN.  Canonicalize only comparison nodes
    and require the complete query trees to match afterward.  This avoids
    treating unrelated boolean expressions, such as a projected predicate
    versus ``predicate IS TRUE``, as equivalent.
    """
    if not isinstance(standard_ast, exp.Expression) or not isinstance(
        student_ast, exp.Expression
    ):
        return False

    def canonicalize(ast: exp.Expression) -> exp.Expression:
        copied = ast.copy()
        comparisons = tuple(
            node
            for node in copied.walk()
            if type(node) in _MIRRORED_COMPARISON_TYPES
            and isinstance(node.this, exp.Expression)
            and isinstance(node.expression, exp.Expression)
        )
        for node in comparisons:
            left = node.this
            right = node.expression
            left_sql = _sql_of(left)
            right_sql = _sql_of(right)
            if left_sql <= right_sql:
                continue
            operator = _MIRRORED_COMPARISON_TYPES[type(node)]
            node.replace(operator(this=right.copy(), expression=left.copy()))
        return copied

    standard_canonical = canonicalize(standard_ast)
    student_canonical = canonicalize(student_ast)
    return _sql_of(standard_canonical) == _sql_of(student_canonical) and (
        _sql_of(standard_ast) != _sql_of(student_ast)
    )


def _standalone_literal_projection_equivalent(
    standard_ast: exp.Expression,
    student_ast: exp.Expression,
) -> bool:
    """Compare one standalone literal under the value-only judge contract."""
    if not isinstance(standard_ast, exp.Select) or not isinstance(
        student_ast, exp.Select
    ):
        return False
    if standard_ast.args.get("from_") or student_ast.args.get("from_"):
        return False
    if len(standard_ast.expressions or ()) != 1 or len(student_ast.expressions or ()) != 1:
        return False
    standard_value = standard_ast.expressions[0]
    student_value = student_ast.expressions[0]
    if isinstance(standard_value, exp.Alias) or isinstance(student_value, exp.Alias):
        return False

    equivalent = False
    if (
        isinstance(standard_value, exp.Literal)
        and isinstance(student_value, exp.Literal)
        and not standard_value.is_string
        and not student_value.is_string
    ):
        try:
            equivalent = Decimal(str(standard_value.this)) == Decimal(
                str(student_value.this)
            )
        except (ArithmeticError, ValueError):
            equivalent = False
    else:
        boolean = (
            standard_value
            if isinstance(standard_value, exp.Boolean)
            else student_value
            if isinstance(student_value, exp.Boolean)
            else None
        )
        numeric = (
            student_value
            if boolean is standard_value
            else standard_value
            if boolean is student_value
            else None
        )
        if isinstance(boolean, exp.Boolean) and isinstance(numeric, exp.Literal) and not numeric.is_string:
            try:
                equivalent = Decimal(str(numeric.this)) == Decimal(
                    1 if bool(boolean.this) else 0
                )
            except (ArithmeticError, ValueError):
                equivalent = False
    if not equivalent:
        return False

    standard_copy = standard_ast.copy()
    student_copy = student_ast.copy()
    placeholder = exp.Literal.number(0)
    standard_copy.set("expressions", [placeholder.copy()])
    student_copy.set("expressions", [placeholder.copy()])
    return _sql_of(standard_copy) == _sql_of(student_copy)


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


def _singleton_equality_in_filter_equivalent(
    standard_ast: exp.Expression,
    student_ast: exp.Expression,
) -> bool:
    """Recognize ``x = c`` and ``x IN (c)`` in a filtering context.

    SQL's three-valued logic makes this rewrite safe where rows are filtered:
    both forms retain exactly the rows for which the predicate is TRUE. It is
    intentionally not applied to SELECT projections or multi-value IN lists.
    """
    filter_types = tuple(
        item
        for item in (exp.Where, exp.Having, getattr(exp, "Qualify", None))
        if isinstance(item, type)
    )

    def normalized(ast: exp.Expression) -> tuple[str, bool]:
        copied = ast.copy()
        changed = False
        for node in list(copied.find_all(exp.In)):
            if node.args.get("query") is not None or len(node.expressions or ()) != 1:
                continue
            value = node.expressions[0]
            if not isinstance(node.this, exp.Expression) or not isinstance(
                value, (exp.Literal, exp.Null)
            ):
                continue
            if not node.find_ancestor(*filter_types):
                continue
            node.replace(exp.EQ(this=node.this.copy(), expression=value.copy()))
            changed = True
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


def _constant_true_filter_equivalent(
    standard_ast: exp.Expression,
    student_ast: exp.Expression,
) -> bool:
    """Recognize adding/removing a side-effect-free constant TRUE filter.

    ``WHERE 1 = 1`` (and the same restricted literal form with equal text or
    numeric values) retains every row, including rows whose projected or
    aggregated values are NULL.  The rule is deliberately limited to a
    comparison of two non-NULL literals or the Boolean literal TRUE; columns,
    functions, subqueries, and general constant folding stay on the normal
    witness path.
    """

    def is_constant_true(node: exp.Expression | None) -> bool:
        node = _unwrap_paren(node)
        if isinstance(node, exp.Boolean):
            return node.this is True
        if not isinstance(node, exp.EQ):
            return False
        left = node.left
        right = node.right
        if not isinstance(left, exp.Literal) or not isinstance(right, exp.Literal):
            return False
        if left.is_string and right.is_string:
            return _literal_value(left) == _literal_value(right)
        if left.is_number and right.is_number:
            return _literal_value(left) == _literal_value(right)
        return False

    def normalized(ast: exp.Expression) -> tuple[str, bool]:
        copied = ast.copy()
        changed = False
        for select in copied.find_all(exp.Select):
            where = select.args.get("where")
            if not isinstance(where, exp.Where) or not is_constant_true(where.this):
                continue
            select.set("where", None)
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
    select the same rows in WHERE/HAVING. Their projected Boolean
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




def _window_signature(ast: exp.Expression | None) -> tuple[str, ...]:
    """Return normalized window-expression nodes, including nested SELECTs."""
    if ast is None:
        return ()
    return tuple(_sql_of(node) for node in ast.find_all(exp.Window))


def _outer_distinct_signature(ast: exp.Expression | None) -> bool:
    """Return whether the top-level SELECT has SELECT DISTINCT."""
    select = _top_select(ast) if ast is not None else None
    return bool(select and select.args.get("distinct"))


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


def _statically_empty_scalar_subquery_null_equivalent(
    scalar_ast: exp.Expression,
    null_ast: exp.Expression,
) -> bool:
    """Replace one scalar subquery guarded by a numeric constant falsehood."""
    if not isinstance(scalar_ast, exp.Select) or not isinstance(null_ast, exp.Select):
        return False
    subqueries = list(scalar_ast.find_all(exp.Subquery))
    if len(subqueries) != 1:
        return False
    subquery = subqueries[0]
    if len(scalar_ast.expressions or ()) != 1 or scalar_ast.expressions[0] is not subquery:
        return False
    inner = subquery.this
    if not isinstance(inner, exp.Select):
        return False
    where = inner.args.get("where")
    predicate = _unwrap_paren(where.this) if isinstance(where, exp.Where) else None
    if not isinstance(predicate, exp.EQ):
        return False
    operands = (predicate.left, predicate.right)
    if not all(
        isinstance(item, exp.Literal) and not item.is_string
        for item in operands
    ):
        return False
    try:
        left_value = Decimal(str(predicate.left.this))
        right_value = Decimal(str(predicate.right.this))
    except (ArithmeticError, ValueError):
        return False
    if left_value == right_value:
        return False
    copied = scalar_ast.copy()
    copied_subquery = next(iter(copied.find_all(exp.Subquery)), None)
    if not isinstance(copied_subquery, exp.Subquery):
        return False
    copied_subquery.replace(exp.Null())
    return _sql_of(copied) == _sql_of(null_ast)


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


def _alias_insensitive_sql(node: exp.Expression | None) -> str:
    """Render a narrow query shape without physical-table alias spelling."""
    if not isinstance(node, exp.Expression):
        return ""
    copied = node.copy()
    for table in copied.find_all(exp.Table):
        table.set("alias", None)
    for column in copied.find_all(exp.Column):
        column.set("table", None)
    return _sql_of(copied)












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
    """Render function detail under the fixed SQLite contract."""
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
    # does not change the row partition.
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
        target_column=min((stu_set - std_set) or (std_set - stu_set), default=None),
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


def _order_key_source_table(ast: exp.Expression, index: int) -> str:
    """Resolve a qualified ORDER BY key to its physical table.

    ``_direct_from_table`` identifies the first FROM source, which is useful
    for unqualified keys but wrong for a joined query such as
    ``FROM people JOIN poker_player ... ORDER BY poker_player.earnings``.
    Witness materialization and semantic validation need the table that owns
    the changed key, so use the column qualifier/alias when one is present and
    fall back to the direct source only for genuinely unqualified keys.
    """
    select = _top_select(ast)
    if not isinstance(select, exp.Select):
        return ""
    items = _order_by_items(ast)
    if index < 0 or index >= len(items):
        return ""
    expression = items[index][2]
    expression = expression.this if isinstance(expression, exp.Ordered) else expression
    column = expression if isinstance(expression, exp.Column) else expression.find(exp.Column)
    tables: dict[str, str] = {}
    for table in select.find_all(exp.Table):
        name = str(table.name or "").strip()
        if not name:
            continue
        canonical = name.lower()
        tables[canonical] = name
        alias = str(table.alias or "").strip()
        if alias:
            tables[alias.lower()] = name
    if isinstance(column, exp.Column):
        qualifier = str(column.table or "").strip()
        if qualifier:
            return tables.get(qualifier.lower(), qualifier).lower()
    direct = _direct_from_table(select)
    return str(direct.name or "").lower() if isinstance(direct, exp.Table) else ""


def _order_by_ast_diffs(
    standard_ast: exp.Expression,
    student_ast: exp.Expression,
) -> list[ASTDiffNode]:
    std_items = _order_by_items(standard_ast)
    stu_items = _order_by_items(student_ast)
    std_sig = [(sql, desc) for sql, desc, _ in std_items]
    stu_sig = [(sql, desc) for sql, desc, _ in stu_items]
    std_nulls = [
        bool(item.args.get("nulls_first"))
        if item.args.get("nulls_first") is not None else not desc
        for _sql, desc, item in std_items
    ]
    stu_nulls = [
        bool(item.args.get("nulls_first"))
        if item.args.get("nulls_first") is not None else not desc
        for _sql, desc, item in stu_items
    ]
    diff_type = None
    if std_sig == stu_sig:
        if std_nulls == stu_nulls:
            return []
        diff_type = "order_nulls_changed"
    if diff_type is None and len(std_sig) > len(stu_sig) and std_sig[:len(stu_sig)] == stu_sig:
        diff_type = "order_by_tiebreaker_missing"
    elif diff_type is None and len(stu_sig) > len(std_sig) and stu_sig[:len(std_sig)] == std_sig:
        diff_type = "order_by_key_added"
    elif diff_type is None and (
        len(std_sig) == len(stu_sig)
        and all(a[0] == b[0] for a, b in zip(std_sig, stu_sig))
        and any(a[1] != b[1] for a, b in zip(std_sig, stu_sig))
    ):
        diff_type = "order_direction_changed"
    if not diff_type:
        return []
    std_order = _result_order_clause(standard_ast)
    stu_order = _result_order_clause(student_ast)
    if diff_type == "order_direction_changed":
        changed_index = next(
            (
                index
                for index, (standard, student) in enumerate(zip(std_sig, stu_sig))
                if standard[0] == student[0] and standard[1] != student[1]
            ),
            0,
        )
    elif diff_type == "order_nulls_changed":
        changed_index = next(
            (
                index
                for index, (standard, student) in enumerate(zip(std_sig, stu_sig))
                if standard[0] == student[0]
                and index < len(std_nulls)
                and index < len(stu_nulls)
                and std_nulls[index] != stu_nulls[index]
            ),
            0,
        )
    elif diff_type == "order_by_tiebreaker_missing":
        changed_index = len(stu_sig)
    elif diff_type == "order_by_key_added":
        changed_index = len(std_sig)
    else:
        changed_index = 0
    source_table = _order_key_source_table(standard_ast, changed_index)
    source = _direct_from_table(_top_select(standard_ast)) if _top_select(standard_ast) else None
    if not source_table and isinstance(source, exp.Table):
        source_table = str(source.name or "").lower()
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
            "standard_nulls_first": tuple(std_nulls),
            "student_nulls_first": tuple(stu_nulls),
            "standard_source_table": source_table,
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
                    std_node = standard_ast.find(exp.Limit)
                    stu_node = student_ast.find(exp.Limit)
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
    """Compare SQLite aggregate FILTER clauses outside argument lists."""
    std_select = _top_select(standard_ast)
    stu_select = _top_select(student_ast)
    if not isinstance(std_select, exp.Select) or not isinstance(stu_select, exp.Select):
        return []

    def filters(select: exp.Select) -> list[exp.Expression]:
        return [node for node in select.find_all(exp.Filter) if not _is_inside_subquery(node)]

    def structural_sql(node: exp.Expression | None) -> str:
        """Render one SQLite expression deterministically."""
        if node is None:
            return ""
        try:
            return node.sql(normalize=True)
        except Exception:
            return str(node)

    specs: list[tuple[str, str, str, Any]] = [
        ("AGGREGATE FILTER", "aggregate_filter_changed", "aggregate", filters),
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

    return diffs


def _limit_repr(ast: exp.Expression) -> str:
    """Return the canonical SQLite LIMIT representation."""
    node = ast.args.get("limit") if isinstance(ast, exp.Query) else None
    if node is None:
        return ""
    expr = getattr(node, "expression", None) or node.args.get("count") or node.args.get("this")
    if expr is None:
        return _sql_of(node)
    return f"LIMIT {_sql_of(expr)}"


def _offset_repr(ast: exp.Expression) -> str:
    node = ast.args.get("offset") if isinstance(ast, exp.Query) else None
    if node is None:
        return ""
    expr = getattr(node, "expression", None) or node.args.get("count") or node.args.get("this")
    if expr is None:
        return _sql_of(node)
    return f"OFFSET {_sql_of(expr)}"


def _comparison_node_types() -> tuple[type[exp.Expression], ...]:
    return (
        exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE,
        exp.NullSafeEQ, exp.NullSafeNEQ,
        exp.Like, exp.Glob,
        exp.In, exp.Between, exp.Is,
    )


def _is_directly_negated(node: exp.Expression) -> bool:
    parent = node.parent
    return isinstance(parent, exp.Not) and parent.this is node


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


def _scrub_nested_query_bodies(node: exp.Expression) -> exp.Expression:
    """Keep a predicate wrapper while removing nested query implementation.

    Correlated-context comparison is meant to notice a changed outer
    predicate (for example ``o.id IN (...)`` -> ``o.key IN (...)``).  The
    previous implementation compared the complete subquery SQL, so changing
    only DISTINCT/ORDER/projection inside that subquery was misreported as a
    correlation error.  A literal marker preserves the wrapper/operator and
    deliberately discards inner query text for this one diagnostic summary.
    """
    if isinstance(node, (exp.Subquery, exp.Exists)):
        return exp.Literal.string("__nested_query__")
    copied = node.copy()
    for key, value in list(copied.args.items()):
        if isinstance(value, exp.Expression):
            copied.set(key, _scrub_nested_query_bodies(value))
        elif isinstance(value, list):
            copied.set(
                key,
                [
                    _scrub_nested_query_bodies(item)
                    if isinstance(item, exp.Expression)
                    else item
                    for item in value
                ],
            )
    return copied


_AGG_FUNC_TYPES: tuple[type[exp.Expression], ...] = (
    exp.Count, exp.Sum, exp.Avg, exp.Min, exp.Max,
    exp.Stddev, exp.Variance, exp.GroupConcat,
)


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
        "order_nulls_changed": "ordered_compare_probe",
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


def _nearest_select(node: exp.Expression) -> exp.Select | None:
    parent = node.parent
    while parent is not None:
        if isinstance(parent, exp.Select):
            return parent
        parent = parent.parent
    return None


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


def _aggregate_probe_order_descending(diff: ASTDiffNode) -> bool | None:
    node = diff.standard_node
    select = node.find_ancestor(exp.Select) if isinstance(node, exp.Expression) else None
    order = select.args.get("order") if isinstance(select, exp.Select) else None
    if not isinstance(order, exp.Order) or not order.expressions:
        return None
    first = order.expressions[0]
    return bool(first.args.get("desc")) if isinstance(first, exp.Ordered) else False


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


def _ancestor_selects(select: exp.Select) -> list[exp.Select]:
    """Return query blocks visible to a nested SELECT, nearest first."""
    result: list[exp.Select] = []
    current = select.parent
    while isinstance(current, exp.Expression):
        if isinstance(current, exp.Select):
            result.append(current)
        current = current.parent
    return result


def _static_predicate_scalar(node: exp.Expression | None) -> Any:
    """Return a scalar hidden behind a SQL scalar wrapper.

    Witness generation may use a literal only to make a source row reach a
    query block.  The AST still represents ``LOWER('History')`` and
    ``CAST(2001 AS UNSIGNED)`` as expressions, so the older direct-literal
    helper cannot see them.  This intentionally stays conservative: a value
    is returned only when the expression is a literal-preserving scalar
    wrapper, never by evaluating an arbitrary function.
    """
    if isinstance(node, (exp.Literal, exp.Boolean, exp.Null)):
        return _semantic_literal_value(node)
    if isinstance(node, (exp.Lower, exp.Upper, exp.Paren, exp.Cast)):
        inner = node.this if isinstance(node, exp.Expression) else None
        return _static_predicate_scalar(inner)
    return _MISSING if "_MISSING" in globals() else None


def _predicate_source_column(node: exp.Expression | None) -> exp.Column | None:
    """Return the single source column of a scalar predicate operand."""
    if isinstance(node, exp.Column):
        return node
    if not isinstance(node, exp.Expression):
        return None
    columns = list(node.find_all(exp.Column))
    return columns[0] if len(columns) == 1 else None


def _like_truth_value(pattern: Any, desired: bool) -> Any | None:
    if not isinstance(pattern, str):
        return None
    if desired:
        if pattern in {"", "%"}:
            return "probe"
        # Keep the fixed characters and satisfy the common prefix/suffix
        # teaching patterns without pretending to implement a full LIKE
        # automaton in the data generator.
        core = pattern.replace("%", "").replace("_", "x")
        if pattern.startswith("%"):
            core = "probe" + core
        if pattern.endswith("%"):
            core = core + "_probe"
        return core
    return "__not_like_probe__"


def _strict_path_variant(value: Any, row_index: int) -> Any:
    """Return a deterministic row-path variant that preserves common LIKEs."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float, Decimal)):
        return value + max(1, row_index) * 1000
    if value is None:
        return f"__phase1_path_{row_index}__"
    text = str(value)
    upper = text.upper()
    if upper.endswith("SU"):
        return f"__phase1_path_{row_index}SU"
    if upper.startswith("C"):
        return f"C__phase1_path_{row_index}"
    return f"{text}__phase1_path_{row_index}"


def _aggregate_distinct_probe_value(current: Any, row_index: int) -> Any:
    """Create a type-compatible value for a COUNT(DISTINCT) witness."""
    if isinstance(current, bool):
        return int(row_index + 1)
    if isinstance(current, (int, float, Decimal)):
        return 910000 + row_index
    if current is None:
        return f"__phase1_distinct_{row_index}__"
    return f"__phase1_distinct_{row_index}__"


_MISSING = object()


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


@dataclass
class _Phase1ScopeDescriptor:
    """Internal, non-serializable description of one real query block.

    ``exp.Subquery`` is a wrapper and also happens to subclass ``exp.Query``
    in sqlglot.  The scope contract deliberately records the wrapped SELECT
    or set operation instead, otherwise one SQL subquery would become two
    diagnostic scopes.
    """

    side: str
    node: exp.Expression = field(repr=False)
    scope_id: str
    scope_kind: str
    scope_label: str
    structural_path: tuple[tuple[str, str, int | None], ...]
    structural_key: str
    parent_scope_id: str | None
    lexical_depth: int
    metadata_complete: bool = True
    cte_name: str = ""
    cte_index: int | None = None
    cte_recursive: bool = False
    derived_alias: str = ""
    is_set_container: bool = False
    correlation_allowed: bool = False
    is_correlated: bool = False


def _is_scope_query_node(node: Any) -> bool:
    return isinstance(node, (exp.Select, exp.SetOperation))


def _scope_text(value: Any, limit: int = 128) -> str:
    return re.sub(r"[\x00-\x1f\x7f]+", "", str(value or "").strip())[:limit]


def _scope_structural_path(
    node: exp.Expression,
) -> tuple[tuple[str, str, int | None], ...]:
    key = _query_block_scope_key(node) if isinstance(node, exp.Query) else ()
    raw_path = key[1] if len(key) > 1 and isinstance(key[1], tuple) else ()
    return tuple(raw_path[-_MAX_SCOPE_PATH_DEPTH:])


def _nearest_retained_scope(
    node: exp.Expression | None,
    descriptors_by_node: dict[int, _Phase1ScopeDescriptor],
    *,
    include_self: bool = True,
) -> _Phase1ScopeDescriptor | None:
    current = node if include_self else (node.parent if node is not None else None)
    while isinstance(current, exp.Expression):
        descriptor = descriptors_by_node.get(id(current))
        if descriptor is not None:
            return descriptor
        current = current.parent
    return None


def _scope_wrapper(node: exp.Expression) -> exp.Expression | None:
    """Return the AST construct that gives *node* its query-block role."""
    parent = node.parent
    if isinstance(parent, (exp.CTE, exp.Subquery, exp.Exists)):
        return parent
    if isinstance(parent, exp.SetOperation):
        return parent
    return parent if isinstance(parent, exp.Expression) else None


def _subquery_wrapper_is_derived(wrapper: exp.Subquery) -> bool:
    current: exp.Expression | None = wrapper.parent
    while isinstance(current, exp.Expression):
        if isinstance(current, (exp.From, exp.Join)):
            return True
        if isinstance(current, (exp.Select, exp.SetOperation, exp.Where, exp.Having)):
            return False
        current = current.parent
    return False


def _scope_role(
    node: exp.Expression,
    root: exp.Expression,
) -> tuple[str, str, int | None, bool, str]:
    """Return kind, CTE name/index/recursion and derived alias."""
    if node is root:
        return "ROOT", "", None, False, ""
    wrapper = _scope_wrapper(node)
    if isinstance(wrapper, exp.CTE) and wrapper.this is node:
        with_node = wrapper.parent if isinstance(wrapper.parent, exp.With) else None
        ctes = list(with_node.expressions or ()) if isinstance(with_node, exp.With) else []
        index = next((i for i, item in enumerate(ctes) if item is wrapper), None)
        return (
            "CTE",
            _scope_text(wrapper.alias),
            index,
            bool(with_node.args.get("recursive")) if isinstance(with_node, exp.With) else False,
            "",
        )
    if isinstance(wrapper, exp.Subquery) and wrapper.this is node:
        if _subquery_wrapper_is_derived(wrapper):
            return (
                "DERIVED",
                "",
                None,
                False,
                _scope_text(wrapper.alias),
            )
        return "SUBQUERY", "", None, False, ""
    if isinstance(wrapper, exp.Exists) and wrapper.this is node:
        return "SUBQUERY", "", None, False, ""
    if isinstance(wrapper, exp.SetOperation):
        return "SET_BRANCH", "", None, False, ""
    return "UNKNOWN", "", None, False, ""


def _collect_phase1_scopes(
    ast: exp.Expression,
    side: str,
    limitations: set[str],
) -> tuple[list[_Phase1ScopeDescriptor], dict[int, _Phase1ScopeDescriptor], bool]:
    query_nodes: list[exp.Expression] = []
    scan_truncated = False
    for index, node in enumerate(ast.walk()):
        if index >= _MAX_SCOPE_AST_NODES_SCANNED:
            scan_truncated = True
            limitations.add(f"{side} AST scope scan limit reached")
            break
        if _is_scope_query_node(node):
            query_nodes.append(node)

    if len(query_nodes) > _MAX_SCOPE_NODES:
        query_nodes = query_nodes[:_MAX_SCOPE_NODES]
        scan_truncated = True
        limitations.add(f"{side} query scope limit reached")

    descriptors: list[_Phase1ScopeDescriptor] = []
    descriptors_by_node: dict[int, _Phase1ScopeDescriptor] = {}
    used_scope_ids: set[str] = set()
    for node in query_nodes:
        kind, cte_name, cte_index, cte_recursive, derived_alias = _scope_role(
            node,
            ast,
        )
        raw_scope_key = _query_block_scope_key(node)
        raw_scope_path = (
            raw_scope_key[1]
            if len(raw_scope_key) > 1 and isinstance(raw_scope_key[1], tuple)
            else ()
        )
        if len(raw_scope_path) > _MAX_SCOPE_PATH_DEPTH:
            scan_truncated = True
            limitations.add(f"{side} query scope path depth limit reached")
        structural_path = _scope_structural_path(node)
        structural_key = json.dumps(
            structural_path,
            ensure_ascii=True,
            separators=(",", ":"),
        )
        if node is ast:
            scope_id = f"{side}:root"
            scope_label = "root"
        else:
            digest = hashlib.sha256(structural_key.encode("utf-8")).hexdigest()[:16]
            scope_id = f"{side}:scope:{digest}"
            label_detail = cte_name or derived_alias or digest[:8]
            scope_label = f"{kind.lower()}:{label_detail}"
        if scope_id in used_scope_ids:
            # A cryptographic collision or a repeated, truncated AST path must
            # not collapse two scopes.  Do not invent a suffix whose meaning
            # could vary with traversal order; retain the first and mark the
            # contract partial.
            scan_truncated = True
            limitations.add(f"{side} duplicate structural scope identity rejected")
            continue
        used_scope_ids.add(scope_id)
        parent = _nearest_retained_scope(
            node,
            descriptors_by_node,
            include_self=False,
        )
        lexical_depth = (parent.lexical_depth + 1) if parent is not None else 0
        descriptor = _Phase1ScopeDescriptor(
            side=side,
            node=node,
            scope_id=scope_id,
            scope_kind=kind,
            scope_label=scope_label,
            structural_path=structural_path,
            structural_key=structural_key,
            parent_scope_id=parent.scope_id if parent is not None else None,
            lexical_depth=lexical_depth,
            metadata_complete=kind != "UNKNOWN",
            cte_name=cte_name,
            cte_index=cte_index,
            cte_recursive=cte_recursive,
            derived_alias=derived_alias,
            is_set_container=isinstance(node, exp.SetOperation),
            correlation_allowed=kind == "SUBQUERY",
        )
        if kind == "SET_BRANCH" and parent is not None:
            descriptor.correlation_allowed = parent.correlation_allowed
        if kind == "UNKNOWN":
            limitations.add(f"{side} query scope kind is not provable: {scope_id}")
        descriptors.append(descriptor)
        descriptors_by_node[id(node)] = descriptor

    if not descriptors or descriptors[0].node is not ast:
        scan_truncated = True
        limitations.add(f"{side} root query scope missing")
    return descriptors, descriptors_by_node, scan_truncated


def _scope_edge(
    edge_type: str,
    source: str,
    target: str,
    evidence_ref: str,
) -> dict[str, Any]:
    return {
        "edge_type": edge_type,
        "source_scope_id": source,
        "target_scope_id": target,
        "evidence_refs": [evidence_ref],
    }


def _merge_scope_edges(edges: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[tuple[str, str, str], set[str]] = {}
    for edge in edges:
        key = (
            str(edge.get("edge_type") or ""),
            str(edge.get("source_scope_id") or ""),
            str(edge.get("target_scope_id") or ""),
        )
        if not all(key):
            continue
        merged.setdefault(key, set()).update(
            str(item)
            for item in (edge.get("evidence_refs") or ())
            if item
        )
    return [
        {
            "edge_type": edge_type,
            "source_scope_id": source,
            "target_scope_id": target,
            "evidence_refs": sorted(refs),
        }
        for (edge_type, source, target), refs in sorted(merged.items())
    ]


def _scope_parent_chain(
    descriptor: _Phase1ScopeDescriptor,
    descriptors_by_id: dict[str, _Phase1ScopeDescriptor],
    *,
    include_self: bool = True,
) -> list[_Phase1ScopeDescriptor]:
    result: list[_Phase1ScopeDescriptor] = []
    current: _Phase1ScopeDescriptor | None = descriptor if include_self else (
        descriptors_by_id.get(descriptor.parent_scope_id or "")
    )
    while current is not None and len(result) <= _MAX_SCOPE_PATH_DEPTH:
        result.append(current)
        current = descriptors_by_id.get(current.parent_scope_id or "")
    return result


def _scope_for_diff_node(
    node: Any,
    descriptors_by_node: dict[int, _Phase1ScopeDescriptor],
    descriptors: list[_Phase1ScopeDescriptor] | None = None,
) -> _Phase1ScopeDescriptor | None:
    if not isinstance(node, exp.Expression):
        return None
    # Wrapper-level diffs semantically belong to the wrapped producer/query.
    if isinstance(node, (exp.CTE, exp.Subquery, exp.Exists)):
        inner = node.this
        if isinstance(inner, exp.Expression):
            descriptor = _nearest_retained_scope(inner, descriptors_by_node)
            if descriptor is not None:
                return descriptor
            node = inner
    descriptor = _nearest_retained_scope(node, descriptors_by_node)
    if descriptor is not None or not descriptors:
        return descriptor

    # ``extract_ast_diffs`` intentionally parses its own immutable AST pair,
    # while execution uses a separate validated SQLite AST pair. Object identity is
    # therefore normally different even though the complete structural path
    # is the same.  Match that full path only when it names exactly one
    # retained query block; never fall back to rendered SQL or a query name.
    current: exp.Expression | None = node
    while isinstance(current, exp.Expression) and not _is_scope_query_node(current):
        current = current.parent
    if not isinstance(current, exp.Expression):
        return None
    structural_key = json.dumps(
        _scope_structural_path(current),
        ensure_ascii=True,
        separators=(",", ":"),
    )
    candidates = [
        item for item in descriptors if item.structural_key == structural_key
    ]
    return candidates[0] if len(candidates) == 1 else None


def _conceptual_scope_id(descriptor: _Phase1ScopeDescriptor) -> str:
    if descriptor.scope_kind == "ROOT" and not descriptor.structural_path:
        return "paired:root"
    digest = hashlib.sha256(
        descriptor.structural_key.encode("utf-8")
    ).hexdigest()[:16]
    return f"paired:scope:{digest}"


def _scalar_function_roots(ast: exp.Expression) -> list[exp.Func]:
    """Return scalar function roots, excluding structural/aggregate constructs."""
    roots: list[exp.Func] = []
    excluded_nodes = (exp.AggFunc, exp.Case, exp.Exists)
    excluded_ancestors = (exp.AggFunc, exp.Case, exp.Exists, exp.Window)
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


def _sql_of(node: exp.Expression | None) -> str:
    if node is None:
        return ""
    try:
        return node.sql(dialect="sqlite", normalize=True)
    except Exception:
        return str(node)
