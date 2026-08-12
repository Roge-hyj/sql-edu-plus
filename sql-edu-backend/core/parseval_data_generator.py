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
from typing import Any
from collections import Counter, defaultdict
from itertools import product
import re
import sqlite3
import uuid
from urllib.parse import unquote, urlparse

import sqlglot
from sqlglot import ErrorLevel, exp
from sqlglot.dialects.sqlite import SQLite
from core.ast_schema import ASTDiffNode
SQLite.Generator.SUPPORTS_TABLE_ALIAS_COLUMNS = True


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
    execution_backend: str | None = None,
    native_executor_url: str | None = None,
) -> SandboxRun:
    schema = parse_schema_text(schema_text)
    schema_types = parse_schema_column_types(schema_text)
    target_dialect = _normalize_sql_dialect(sql_dialect)
    backend = _select_execution_backend(
        target_dialect=target_dialect,
        execution_backend=execution_backend,
        native_executor_url=native_executor_url,
    )
    if _mentions_sys_views(standard_sql) or _mentions_sys_views(student_sql):
        schema.setdefault("Sys.Views", ["Name"])
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

    standard_ast = _parse_sql_strict(standard_sql)
    if standard_ast is None:
        return _failed("standard_sql_parse_failed", None, None, {}, [], [], status="ENGINE_ERROR")
    student_ast = _parse_sql_strict(student_sql)
    if student_ast is None:
        return _failed("student_sql_parse_failed", None, None, {}, [], [], status="WRONG")
    ast_diffs = extract_ast_diffs(standard_sql, student_sql)
    rows = generate_test_database(
        schema,
        standard_sql,
        student_sql,
        max_rows_per_table=max_rows_per_table,
        ast_diffs=ast_diffs,
    )

    standard_executable, student_executable = _prepare_executable_sql_pair(
        backend,
        standard_sql,
        student_sql,
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

    try:
        std_cols, std_rows = _execute_with_backend(
            backend=backend,
            schema=schema,
            schema_types=schema_types,
            rows=rows,
            sql=standard_executable,
            native_executor_url=native_executor_url,
        )
    except Exception as exc:
        status = (
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
        )
        student_exec_error = None
    except Exception as exc:
        stu_cols, stu_rows = [], []
        student_exec_error = str(exc)

    ordered = _has_node(standard_ast, exp.Order)
    if student_exec_error:
        is_equivalent = False
    elif ordered:
        is_equivalent = len(std_cols) == len(stu_cols) and std_rows == stu_rows
    else:
        is_equivalent = len(std_cols) == len(stu_cols) and Counter(std_rows) == Counter(stu_rows)

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
    mutation_evidence = _run_mutation_tests(
        schema=schema,
        rows=rows,
        standard_sql=standard_sql,
        student_sql=student_sql,
        standard_columns=std_cols,
        standard_rows=std_rows,
        ordered=ordered,
    )
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
        judge_status="CORRECT" if is_equivalent else "WRONG",
    )


def generate_test_database(
    schema: dict[str, list[str]],
    standard_sql: str,
    student_sql: str,
    *,
    max_rows_per_table: int = 8,
    ast_diffs: list[dict[str, Any]] | None = None,
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

    # 4. 构建关联表的主外键种子池，保证 JOIN 条件不为空，解决拓扑对齐与多外键错位偏移
    shared_values = _build_shared_values(target_tables, row_count)
    data: dict[str, list[dict[str, Any]]] = {}

    for table, columns in target_tables.items():
        rows: list[dict[str, Any]] = []
        for idx in range(row_count):
            row = {}
            for col in columns:
                # 填充各字段的基础值（包括 Outer Join 不对称悬浮元组的 None 填充）
                row[col] = _base_value(col, idx, shared_values)
            rows.append(row)

        # 5. 注入数值边界三态值、HAVING 聚合以及 NULL 空值探针数据
        _apply_constraints(rows, columns, constraints, target_tables)
        _apply_having_aggregate_probes(rows, columns, standard_sql, student_sql, ast_diffs)
        _apply_null_aggregate_probe(rows, columns, standard_sql, student_sql)
        if _has_diff(ast_diffs, "JOIN ON") or _has_diff(ast_diffs, "JOIN_TYPE"):
            _apply_join_key_drift(rows, columns, shared_values)
        # Dangling tuple probe for LEFT JOIN right tables AND join_missing left tables.
        # When a JOIN is missing, the left (FROM) table needs rows that have no match
        # in the dropped table, so that INNER JOIN would filter them out but SELECT alone won't.
        _apply_dangling = (
            _norm_name(table) in _right_tables_for_left_joins(standard_sql, student_sql, ast_diffs=ast_diffs)
            or _is_from_table_of_missing_join(table, standard_sql, ast_diffs)
        )
        if _apply_dangling:
            _apply_dangling_tuple_probe(rows, columns, table, standard_sql, student_sql)
        _apply_subquery_aggregate_probes(rows, columns, table, standard_sql, student_sql)
        _apply_subquery_membership_probe(rows, columns, table, standard_sql, student_sql)
        _apply_expression_probes(rows, columns, table, standard_sql, student_sql)

        data[table] = rows[:row_count]

    for tactic in TacticRegistry.get_active_tactics(ast_diffs):
        tactic.apply_data_probe(data, schema, standard_sql, student_sql, ast_diffs)

    _apply_cross_table_having_probe(data, standard_sql, student_sql, ast_diffs)
    _apply_group_filter_positive_probe(data, standard_sql, student_sql, ast_diffs)
    _apply_order_by_probes(data, standard_sql, student_sql, ast_diffs)
    _apply_window_probes(data, standard_sql, student_sql, ast_diffs)
    _apply_window_rank_gap_probe(data, standard_sql, student_sql)
    _apply_logical_operator_probe(data, standard_sql, student_sql)
    _apply_projection_discriminator(data, standard_sql, student_sql, ast_diffs)
    _apply_aggregate_argument_probe(data, ast_diffs)

    _apply_set_branch_asymmetry_probe(data, standard_sql, student_sql, ast_diffs)
    _apply_cte_outer_projection_probe(data, standard_sql, ast_diffs)

    # 相关子查询探针（需要跨表数据）
    _apply_correlated_subquery_probe(data, schema, standard_sql, student_sql)
    _align_having_membership_keys(data, standard_sql, student_sql)

    _repair_primary_key_candidate_duplicates(
        data,
        target_tables,
        standard_sql,
        student_sql,
    )
    _apply_join_semantic_probes(data, standard_sql, student_sql)
    _apply_not_in_null_probe(data, standard_sql, student_sql, ast_diffs)
    _apply_recursive_cte_safety(data, schema, standard_sql, student_sql)
    _apply_recursive_set_duplicate_probe(data, standard_sql, student_sql, ast_diffs)
    _apply_recursive_cte_orphan_probe(data, standard_sql, student_sql)
    _apply_same_table_having_membership_probe(data, standard_sql, student_sql)
    _apply_distinct_probes(data, standard_sql, student_sql, ast_diffs)
    # These probes depend on the final row topology.  Run them after the
    # generic PK/dedup repairs so later normalization cannot break the path.
    _apply_nested_membership_chain_probe(data, standard_sql, student_sql)
    _apply_same_table_correlated_aggregate_probe(data, standard_sql, student_sql)

    return data


def transpile_to_sqlite(sql: str) -> str | None:
    prepared_sql = _prepare_sqlite_source(sql)
    manual = _manual_sqlite_compat(prepared_sql)
    for dialect in _dialect_candidates(prepared_sql):
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
    unsupported_features: list[str] | None = None,
    execution_backend: str = "sqlite",
    sql_dialect: str = "mysql",
) -> SandboxRun:
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
    )


def _normalize_sql_dialect(sql_dialect: str | None) -> str:
    normalized = (sql_dialect or "mysql").strip().lower()
    aliases = {
        "mariadb": "mysql",
        "postgresql": "postgres",
        "pg": "postgres",
        "sqlserver": "tsql",
        "mssql": "tsql",
        "sql_server": "tsql",
    }
    return aliases.get(normalized, normalized if normalized in {"mysql", "postgres", "tsql", "sqlite"} else "mysql")


def _select_execution_backend(
    *,
    target_dialect: str,
    execution_backend: str | None,
    native_executor_url: str | None,
) -> str:
    backend = (execution_backend or "auto").strip().lower()
    if backend in {"sqlite", "mysql"}:
        return backend
    if backend in {"native", "auto"} and target_dialect == "mysql" and native_executor_url:
        return "mysql"
    return "sqlite"


def _prepare_executable_sql_pair(
    backend: str,
    standard_sql: str,
    student_sql: str,
) -> tuple[str | None, str | None]:
    if backend == "mysql":
        return _prepare_native_sql(standard_sql), _prepare_native_sql(student_sql)
    return transpile_to_sqlite(standard_sql), transpile_to_sqlite(student_sql)


def _prepare_native_sql(sql: str) -> str:
    return sql.strip().rstrip(";")


def _detect_unsupported_features(
    backend: str,
    target_dialect: str,
    *sql_items: str,
) -> list[str]:
    if backend == "sqlite":
        return _detect_sqlite_unsupported_features(*sql_items)
    if backend == "mysql":
        if target_dialect != "mysql":
            return [f"NATIVE_{target_dialect.upper()}_EXECUTOR_MISSING"]
        return _detect_mysql_unsupported_features(*sql_items)
    return [f"UNKNOWN_BACKEND_{backend.upper()}"]


def _detect_sqlite_unsupported_features(*sql_items: str) -> list[str]:
    """Return dialect features that should not be judged through SQLite."""
    checks: tuple[tuple[str, str], ...] = (
        ("PIVOT", r"(?is)\bPIVOT\b"),
        ("UNPIVOT", r"(?is)\bUNPIVOT\b"),
        ("LATERAL", r"(?is)\bLATERAL\b"),
        ("APPLY", r"(?is)\b(?:CROSS|OUTER)\s+APPLY\b"),
        ("ROLLUP", r"(?is)\bROLLUP\s*\("),
        ("WITH_ROLLUP", r"(?is)\bWITH\s+ROLLUP\b"),
        ("CUBE", r"(?is)\bCUBE\s*\("),
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
    return _is_likely_sqlite_capability_error(error, sql)


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


def _parse_sql(sql: str) -> exp.Expression | None:
    for dialect in _dialect_candidates(sql):
        try:
            parsed = sqlglot.parse_one(sql, dialect=dialect, error_level=ErrorLevel.IGNORE)
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
                       'NULLS', 'FIRST', 'LAST', 'QUALIFY', 'WINDOW', 'ROWS',
                       'RANGE', 'PRECEDING', 'FOLLOWING', 'CURRENT', 'ROW'}
                meaningful = {t for t in raw_tokens if t.upper() not in _KW}
                if meaningful:
                    roundtrip = parsed.sql(dialect=dialect)
                    rt_tokens = set(re.findall(r'\b[A-Za-z_]\w*\b', roundtrip))
                    lost = meaningful - rt_tokens
                    # If any meaningful identifier vanished, the parse
                    # likely mis-interpreted the query.
                    if lost:
                        continue
                return parsed
        except Exception:
            continue
    return None


def _parse_sql_strict(sql: str) -> exp.Expression | None:
    """Parse exactly one complete statement without sqlglot recovery."""
    for dialect in _dialect_candidates(sql):
        try:
            statements = sqlglot.parse(sql, dialect=dialect, error_level=ErrorLevel.RAISE)
            parsed = [
                statement for statement in statements
                if statement is not None and not isinstance(statement, exp.Semicolon)
            ]
            if len(parsed) == 1 and isinstance(parsed[0], exp.Query):
                return parsed[0]
        except Exception:
            continue
    return None


def _dialect_candidates(sql: str) -> tuple[str, str, str]:
    if "`" in sql:
        return ("mysql", "sqlite", "tsql")
    if re.search(r"(?is)\bSELECT\s+TOP\b|\[[^\]]+\]", sql):
        return ("tsql", "sqlite", "mysql")
    return ("sqlite", "mysql", "tsql")


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


def extract_ast_diffs(standard_sql: str, student_sql: str) -> list[ASTDiffNode]:
    """Extract focused AST subtree differences used to drive counterexample data generation."""
    standard_ast = _parse_sql(standard_sql)
    student_ast = _parse_sql(student_sql)
    if standard_ast is None or student_ast is None:
        return []

    if _queries_are_supported_equivalent_rewrites(standard_ast, student_ast):
        return []

    diffs: list[ASTDiffNode] = []
    diffs.extend(_clause_ast_diffs(standard_ast, student_ast))
    diffs.extend(_projection_column_ast_diffs(standard_ast, student_ast))
    diffs.extend(_projection_alias_ast_diffs(standard_ast, student_ast))
    diffs.extend(_function_argument_ast_diffs(standard_ast, student_ast))
    diffs.extend(_group_by_ast_diffs(standard_ast, student_ast))
    diffs.extend(_having_placement_ast_diffs(standard_ast, student_ast))
    diffs.extend(_order_by_ast_diffs(standard_ast, student_ast))
    diffs.extend(_comparison_ast_diffs(standard_ast, student_ast))
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
    diffs.extend(_specialized_semantic_ast_diffs(standard_ast, student_ast))

    seen: set[tuple[Any, ...]] = set()
    unique: list[ASTDiffNode] = []
    for diff in diffs:
        depth = (diff.extra or {}).get("subquery_depth", 0)
        key = (
            diff.clause_category,
            diff.diff_type,
            diff.target_column,
            _sql_of(diff.standard_node) if isinstance(diff.standard_node, exp.Expression) else str(diff.standard_node or ""),
            _sql_of(diff.student_node) if isinstance(diff.student_node, exp.Expression) else str(diff.student_node or ""),
            depth,
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(diff)
    return unique


def _queries_are_supported_equivalent_rewrites(
    standard_ast: exp.Expression,
    student_ast: exp.Expression,
) -> bool:
    """Recognize narrow, semantics-preserving rewrites before emitting noisy diffs."""
    return any((
        _where_boolean_absorption_equivalent(standard_ast, student_ast),
        _simple_cte_inline_equivalent(standard_ast, student_ast),
        _simple_cte_inline_equivalent(student_ast, standard_ast),
        _simple_in_join_equivalent(standard_ast, student_ast),
        _simple_in_join_equivalent(student_ast, standard_ast),
        _simple_not_exists_antijoin_equivalent(standard_ast, student_ast),
        _simple_not_exists_antijoin_equivalent(student_ast, standard_ast),
    ))


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
    std_select = _top_select(standard_ast)
    stu_select = _top_select(student_ast)
    if not isinstance(std_select, exp.Select) or not isinstance(stu_select, exp.Select):
        return False
    std_where = std_select.args.get("where")
    stu_where = stu_select.args.get("where")
    if not isinstance(std_where, exp.Where) or not isinstance(stu_where, exp.Where):
        return False
    if _boolean_dnf_signature(std_where.this) != _boolean_dnf_signature(stu_where.this):
        return False
    return (
        _select_projection_repr(standard_ast) == _select_projection_repr(student_ast)
        and _group_by_repr(standard_ast) == _group_by_repr(student_ast)
        and _sql_of(std_select.args.get("having")) == _sql_of(stu_select.args.get("having"))
        and _sql_of(std_select.args.get("order")) == _sql_of(stu_select.args.get("order"))
        and _limit_repr(standard_ast) == _limit_repr(student_ast)
        and _offset_repr(standard_ast) == _offset_repr(student_ast)
        and _extract_join_graph(standard_ast) == _extract_join_graph(student_ast)
    )


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
    cte_projection = {
        _norm_name(item.alias_or_name or _projection_label(item))
        for item in cte_select.expressions or []
    }
    inline_projection = [_norm_name(_projection_label(item)) for item in inline.expressions or []]
    return (
        outer_projection == inline_projection
        and all(item in cte_projection for item in outer_projection)
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


def _specialized_semantic_ast_diffs(
    standard_ast: exp.Expression,
    student_ast: exp.Expression,
) -> list[ASTDiffNode]:
    """Add diagnostics that require comparing expression shape, not clause text."""
    diffs: list[ASTDiffNode] = []
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
    for std_cmp, stu_cmp in zip(std_comps, stu_comps):
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
            ))

    if _in_exists_rewrite(standard_ast, student_ast) or _in_exists_rewrite(student_ast, standard_ast):
        diffs.append(_semantic_diff(
            "in_exists_equivalence", "SUBQUERY", standard_ast.find(exp.In), student_ast.find(exp.Exists), "subquery-exists",
        ))
    if _not_in_not_exists_rewrite(standard_ast, student_ast) or _not_in_not_exists_rewrite(student_ast, standard_ast):
        diffs.append(_semantic_diff(
            "null_sensitive_antijoin_equivalence", "NULL", standard_ast.find(exp.In), student_ast.find(exp.Exists), "null-handling",
        ))

    std_order = std_select.args.get("order") if isinstance(std_select, exp.Select) else None
    stu_order = stu_select.args.get("order") if isinstance(stu_select, exp.Select) else None
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
    std_select = standard_ast.find(exp.Select)
    stu_select = student_ast.find(exp.Select)
    if not isinstance(std_select, exp.Select) or not isinstance(stu_select, exp.Select):
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


def _function_argument_ast_diffs(
    standard_ast: exp.Expression,
    student_ast: exp.Expression,
    filter_subqueries: bool = True,
) -> list[ASTDiffNode]:
    skip = _is_inside_subquery if filter_subqueries else (lambda _: False)
    std_funcs = [node for node in standard_ast.find_all(exp.Func) if not skip(node)]
    stu_funcs = [node for node in student_ast.find_all(exp.Func) if not skip(node)]
    diffs: list[ASTDiffNode] = []
    for std_func, stu_func in zip(std_funcs, stu_funcs):
        if _function_name(std_func) != _function_name(stu_func):
            continue
        std_args = _function_args(std_func)
        stu_args = _function_args(stu_func)
        if std_args == stu_args:
            continue
        is_aggregate = isinstance(std_func, exp.AggFunc) and isinstance(stu_func, exp.AggFunc)
        if is_aggregate:
            std_columns = sorted({_norm_name(column.name) for column in std_func.find_all(exp.Column)})
            stu_columns = sorted({_norm_name(column.name) for column in stu_func.find_all(exp.Column)})
            if std_columns == stu_columns:
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
                "standard_sql": _sql_of(std_func),
                "student_sql": _sql_of(stu_func),
            },
        ))
    return diffs


def _top_select(ast: exp.Expression) -> exp.Select | None:
    if isinstance(ast, exp.Select):
        return ast
    if isinstance(ast, (exp.Union, exp.Intersect, exp.Except)):
        return ast.this if isinstance(ast.this, exp.Select) else ast.this.find(exp.Select)
    return ast.find(exp.Select)


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
    return " | ".join(sql for sql, _ in _group_by_items(ast))


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
    select = _top_select(ast)
    order = select.args.get("order") if isinstance(select, exp.Select) else None
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
    if std_items == stu_items:
        return []
    std_sig = [(sql, desc) for sql, desc, _ in std_items]
    stu_sig = [(sql, desc) for sql, desc, _ in stu_items]
    diff_type = None
    if len(std_sig) > len(stu_sig) and std_sig[:len(stu_sig)] == stu_sig:
        diff_type = "order_by_tiebreaker_missing"
    elif len(stu_sig) > len(std_sig) and stu_sig[:len(std_sig)] == std_sig:
        diff_type = "order_by_key_added"
    elif len(std_sig) == len(stu_sig) and all(a[0] == b[0] for a, b in zip(std_sig, stu_sig)):
        diff_type = "order_direction_changed"
    if not diff_type:
        return []
    std_order = _top_select(standard_ast).args.get("order") if _top_select(standard_ast) else None
    stu_order = _top_select(student_ast).args.get("order") if _top_select(student_ast) else None
    return [ASTDiffNode(
        clause_category="ORDER BY",
        diff_type=diff_type,
        standard_node=std_order,
        student_node=stu_order,
        knowledge_point_id="order-by",
        severity=0.7,
        extra={"standard_keys": std_sig, "student_keys": stu_sig},
    )]


def _extract_column_name(node: exp.Expression) -> str | None:
    """Best-effort extraction of the primary column name from a projection item."""
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
    where = ast.args.get("where") or ast.find(exp.Where)
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
        ("HAVING", "having_changed", lambda ast: ast.args.get("having") or ast.find(exp.Having), "having"),
        ("ORDER BY", "order_by_changed", lambda ast: ast.args.get("order"), "order-by"),
        ("QUALIFY", "qualify_changed", lambda ast: ast.args.get("qualify") or ast.find(exp.Qualify), "window-row-number"),
        ("LIMIT", "limit_changed", _limit_repr, "limit"),
        ("LIMIT", "limit_changed", _offset_repr, "limit"),
    ]
    diffs: list[ASTDiffNode] = []
    for clause, diff_type, getter, kp in specs:
        std_node = getter(standard_ast)
        stu_node = getter(student_ast)
        if _sql_of(std_node) != _sql_of(stu_node):
            diffs.append(ASTDiffNode(
                clause_category=clause,
                diff_type=diff_type,
                standard_node=std_node,
                student_node=stu_node,
                knowledge_point_id=kp,
                extra={
                    "standard_sql": _sql_of(std_node),
                    "student_sql": _sql_of(stu_node),
                }
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


def _limit_repr(ast: exp.Expression) -> str:
    """Canonical LIMIT/FETCH representation for dialect-equivalent syntax."""
    node = ast.args.get("limit") or ast.find(exp.Limit) or ast.find(exp.Fetch)
    if node is None:
        return ""
    expr = getattr(node, "expression", None) or node.args.get("count") or node.args.get("this")
    if expr is None:
        return _sql_of(node)
    return f"LIMIT {_sql_of(expr)}"


def _offset_repr(ast: exp.Expression) -> str:
    node = ast.args.get("offset") or ast.find(exp.Offset)
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
        if not _skip(node) and not _is_inside_join(node) and not _is_cross_table_condition(node)
    ]
    stu_comparisons = [
        _comparison_descriptor(node)
        for node in student_ast.find_all(*_comparison_node_types())
        if not _skip(node) and not _is_inside_join(node) and not _is_cross_table_condition(node)
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
                }
            ))
            continue
        stu_matched.add(stu_idx)
        std_values = std.get("values")
        stu_values = stu.get("values")
        values_changed = std_values is not None and stu_values is not None and std_values != stu_values
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
                },
            )]

    return []


def _comparison_node_types() -> tuple[type[exp.Expression], ...]:
    return (
        exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE,
        exp.NullSafeEQ, exp.NullSafeNEQ,
        exp.Like, exp.In, exp.Between, exp.Is,
    )


def _comparison_descriptor(node: exp.Expression) -> dict[str, Any] | None:
    if isinstance(node, exp.In) and isinstance(node.this, exp.Column):
        values = [_literal_value(item) for item in node.expressions if isinstance(item, exp.Literal)]
        return {"column": node.this.name, "op": "IN", "value": values[0] if values else None, "values": values, "sql": _sql_of(node), "node": node}
    if isinstance(node, exp.Between) and isinstance(node.this, exp.Column):
        return {
            "column": node.this.name,
            "op": "BETWEEN",
            "value": _literal_value(node.args.get("low")),
            "high": _literal_value(node.args.get("high")),
            "sql": _sql_of(node),
            "node": node,
        }
    if isinstance(node, exp.Is) and isinstance(node.this, exp.Column):
        return {"column": node.this.name, "op": "IS", "value": None, "value_is_null": True, "sql": _sql_of(node), "node": node}
    if isinstance(node, exp.Like) and isinstance(node.this, exp.Column):
        return {"column": node.this.name, "op": "LIKE", "value": _literal_value(node.expression), "sql": _sql_of(node), "node": node}
    left, right = getattr(node, "left", None), getattr(node, "right", None)
    if isinstance(left, exp.Column) and isinstance(right, exp.Column):
        return {
            "column": left.name,
            "op": type(node).__name__.upper(),
            "value": _sql_of(right),
            "right_column": right.name,
            "sql": _sql_of(node),
            "node": node,
        }
    if isinstance(left, exp.Column) and isinstance(right, (exp.Literal, exp.Null)):
        return {
            "column": left.name,
            "op": type(node).__name__.upper(),
            "value": _literal_value(right),
            "value_is_null": isinstance(right, exp.Null),
            "sql": _sql_of(node),
            "node": node,
        }
    if isinstance(right, exp.Column) and isinstance(left, (exp.Literal, exp.Null)):
        return {
            "column": right.name,
            "op": type(node).__name__.upper(),
            "value": _literal_value(left),
            "value_is_null": isinstance(left, exp.Null),
            "sql": _sql_of(node),
            "node": node,
        }
    if isinstance(left, exp.Column) and right is not None:
        return {
            "column": left.name,
            "op": type(node).__name__.upper(),
            "value": _sql_of(right),
            "sql": _sql_of(node),
            "node": node,
        }
    if isinstance(right, exp.Column) and left is not None:
        return {
            "column": right.name,
            "op": type(node).__name__.upper(),
            "value": _sql_of(left),
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
        std_set = set(std_conds)
        stu_set = set(stu_conds)
        missing = std_set - stu_set
        added = stu_set - std_set
        for cond in missing:
            diffs.append(ASTDiffNode(
                clause_category="JOIN ON",
                diff_type="join_on_changed",
                standard_node=None,
                student_node=None,
                knowledge_point_id="join-on",
                extra={"standard_sql": cond, "student_sql": ""},
            ))
        for cond in added:
            diffs.append(ASTDiffNode(
                clause_category="JOIN ON",
                diff_type="join_on_changed",
                standard_node=None,
                student_node=None,
                knowledge_point_id="join-on",
                extra={"standard_sql": "", "student_sql": cond},
            ))

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
                    "standard_sql": _sql_of(std_node.this),
                    "student_sql": _sql_of(stu_node.this),
                },
            ))
        std_spec = _window_spec(std_node)
        stu_spec = _window_spec(stu_node)
        if std_spec != stu_spec:
            diffs.append(ASTDiffNode(
                clause_category="WINDOW",
                diff_type="window_over_changed",
                standard_node=std_node,
                student_node=stu_node,
                knowledge_point_id="window-row-number",
                extra={
                    "standard_over": std_spec,
                    "student_over": stu_spec,
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
    return {
        "partition_by": [_sql_of(item) for item in (node.args.get("partition_by") or [])],
        "order": _sql_of(node.args.get("order")),
        "frame": _sql_of(node.args.get("spec")),
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

    # Non-recursive CTE: report if CTE definitions differ (added, removed, or changed).
    if std_ctes or stu_ctes:
        if std_ctes != stu_ctes:
            return [ASTDiffNode(
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
                    extra={"standard_when_count": len(std_ifs), "student_when_count": len(stu_ifs)},
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
        if diff.diff_type == "null_equality_changed":
            constraints.append({"column": column, "op": "IS", "value": None, "source": "ast_diff"})
        elif isinstance(value, (int, float, Decimal)):
            constraints.append({"column": column, "op": diff.get("standard_op") or "DIFF", "value": value, "source": "ast_diff"})
        elif value is not None:
            constraints.append({"column": column, "op": diff.get("standard_op") or "DIFF", "value": value, "source": "ast_diff"})
        if diff.get("student_value") is not None:
            constraints.append({"column": column, "op": diff.get("student_op") or "DIFF", "value": diff.get("student_value"), "source": "ast_diff"})
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
        "logical_operator_changed": "predicate_positive_negative_probe",
        "logical_precedence_tree_changed": "logical_truth_table_probe",
        "literal_changed": "literal_boundary_tristate",
        "predicate_missing": "predicate_positive_negative_probe",
        "predicate_added": "predicate_positive_negative_probe",
        "null_equality_changed": "null_probe",
        "distinct_changed": "duplicate_projection_probe",
        "join_on_changed": "join_key_drift_probe",
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
    }
    tactics = []
    for diff in ast_diffs:
        name = mapping.get(diff.diff_type)
        if name:
            tactics.append({"tactic": name, "clause": diff.clause_category, "diff_type": diff.diff_type})
    return tactics


def _has_diff(ast_diffs: list[ASTDiffNode], clause: str) -> bool:
    return any(diff.clause_category == clause for diff in ast_diffs)


def _extract_table_names(sql: str) -> set[str]:
    ast = _parse_sql(sql)
    if not ast:
        return set()
    cte_names = {_norm_name(cte.alias or "") for cte in ast.find_all(exp.CTE)}
    return {
        _norm_name(table.name)
        for table in ast.find_all(exp.Table)
        if _norm_name(table.name) not in cte_names
    }


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
    return getattr(node, "this", None)


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
                "boundary": boundary,
                "operator": type(comparison).__name__.upper(),
                "distinct": bool(agg.args.get("distinct") or isinstance(agg.this, exp.Distinct)),
            })
    return specs


def _extract_having_aggregate_spec(sql: str) -> dict[str, Any] | None:
    specs = _extract_having_aggregate_specs(sql)
    return specs[0] if specs else None


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

    if any(diff.get("clause") == "LIMIT" for diff in ast_diffs):
        required = max(required, 6)
    return required


def _limit_offset_required_rows(sql: str) -> int:
    ast = _parse_sql(sql)
    if not ast:
        return 0
    limit_node = ast.find(exp.Limit)
    offset_node = ast.find(exp.Offset)
    limit = _integer_node_value(limit_node.expression if isinstance(limit_node, exp.Limit) else None)
    offset = _integer_node_value(offset_node.expression if isinstance(offset_node, exp.Offset) else None)
    return max(0, (limit or 0) + (offset or 0) + 1)


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
    # Set operators without a SELECT DISTINCT still use non-key payload columns.
    if not probe_cols:
        probe_cols = [col for col in columns if not _is_key_column(col)]
    # A DISTINCT over an ID-looking business key (product_id in a history table,
    # for example) explicitly needs duplicate source values. PK repair has already
    # run before this late probe, so keep the query-observable duplicate here.
    for col in probe_cols:
        rows[1][col] = rows[0][col]


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


def _apply_distinct_probes(
    data: dict[str, list[dict[str, Any]]],
    standard_sql: str,
    student_sql: str,
    ast_diffs: list[ASTDiffNode],
) -> None:
    if not any(
        diff.diff_type in {"distinct_changed", "aggregate_distinct_changed"}
        for diff in ast_diffs
    ) and not _distinct_shape_changed(standard_sql, student_sql):
        return
    for table_name, rows in data.items():
        if not rows:
            continue
        _add_duplicate_probe(
            rows,
            list(rows[0]),
            table_name,
            standard_sql,
            student_sql,
            ast_diffs,
        )

    _apply_grouped_distinct_probe(data, standard_sql, student_sql)

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


def _apply_having_aggregate_probes(
    rows: list[dict[str, Any]],
    columns: list[str],
    standard_sql: str,
    student_sql: str,
    ast_diffs: list[dict[str, Any]] | None = None,
) -> None:
    if ast_diffs is not None and not any(diff.get("clause") in {"HAVING", "PREDICATE", "AGGREGATE"} for diff in ast_diffs):
        return
    spec = _changed_having_aggregate_spec(standard_sql, student_sql)
    if not spec:
        return
    group_col = _column_lookup(columns).get(_norm_name(spec["group_column"]))
    if spec["agg"] == "COUNT":
        if group_col:
            value_col = _column_lookup(columns).get(_norm_name(spec["column"]))
            _apply_count_group_probe(
                rows,
                group_col,
                int(spec["boundary"]),
                value_col=value_col,
                distinct=bool(spec.get("distinct")),
            )
            _apply_having_companion_probes(rows, columns, standard_sql, spec)
        return
    value_col = _column_lookup(columns).get(_norm_name(spec["column"]))
    if not value_col or not group_col:
        return
    for idx, row in enumerate(rows):
        row[group_col] = idx // 2 + 1
    grouped: dict[Any, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(row.get(group_col), []).append(row)
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
        if not group_col or not value_col or spec["agg"] == "COUNT":
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
    value_col: str | None = None,
    distinct: bool = False,
) -> None:
    if not rows:
        return
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
    for group_name, count in zip(group_names, targets):
        for member_index in range(count):
            if idx >= len(rows):
                return
            rows[idx][group_col] = group_name
            if distinct and value_col and value_col != group_col:
                rows[idx][value_col] = f"__having_distinct_{group_name}_{member_index}__"
            idx += 1
    while idx < len(rows):
        rows[idx][group_col] = group_names[-1]
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
            while parent is not None and not isinstance(parent, (exp.GT, exp.GTE, exp.LT, exp.LTE, exp.EQ, exp.NEQ)):
                parent = parent.parent
            if parent is None:
                continue
            outer_col = parent.left if isinstance(parent.left, exp.Column) else parent.right if isinstance(parent.right, exp.Column) else None
            if not isinstance(outer_col, exp.Column):
                continue
            if _norm_name(outer_col.table or table_name) != _norm_name(table_name):
                continue
            actual_col = lookup.get(_norm_name(outer_col.name))
            if not actual_col or not rows:
                continue
            # Extract the actual boundary from the parent comparison literal
            boundary_literal = _literal_value(parent.right) if isinstance(parent.right, exp.Literal) else (
                _literal_value(parent.left) if isinstance(parent.left, exp.Literal) else 50
            )
            if not isinstance(boundary_literal, (int, float, Decimal)):
                boundary_literal = 50
            b = boundary_literal
            pattern = [b - 1, b - 1, b, b + 1, b + 1]
            for idx, row in enumerate(rows):
                row[actual_col] = pattern[idx % len(pattern)]
            return


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
    asts = [_parse_sql(standard_sql), _parse_sql(student_sql)]
    # 收集相关子查询的内外层引用: (outer_table, outer_col, inner_table, inner_col)
    correlations: list[tuple[str, str, str, str]] = []

    for ast in asts:
        if not ast:
            continue
        aliases = _table_aliases(ast)
        for subquery in ast.find_all(exp.Subquery):
            if not _is_subquery_correlated(subquery):
                continue
            # 收集内层表
            inner_tables: set[str] = set()
            for t in subquery.find_all(exp.Table):
                inner_tables.add(_norm_name(t.name))
                if t.alias:
                    inner_tables.add(_norm_name(t.alias))
            # 找到引用外层表的列
            for col in subquery.find_all(exp.Column):
                if col.table:
                    table_ref = _norm_name(col.table)
                    if table_ref not in inner_tables:
                        # 这是外层引用
                        outer_table = aliases.get(table_ref, table_ref)
                        outer_col = _norm_name(col.name)
                        # 找内层与之比较的列
                        parent = col.parent
                        if isinstance(parent, (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE)):
                            other = parent.right if parent.left is col else parent.left
                            if isinstance(other, exp.Column) and other.table:
                                inner_table_ref = _norm_name(other.table)
                                inner_col = _norm_name(other.name)
                                if inner_table_ref in inner_tables:
                                    inner_table = aliases.get(inner_table_ref, inner_table_ref)
                                    correlations.append((outer_table, outer_col, inner_table, inner_col))

    if not correlations:
        return

    # 对每个相关引用，确保内外层列有重叠值
    for outer_table, outer_col, inner_table, inner_col in correlations:
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

        # 找到实际列名
        outer_col_actual = next((c for c in schema[outer_table_actual] if _norm_name(c) == outer_col), None)
        inner_col_actual = next((c for c in schema[inner_table_actual] if _norm_name(c) == inner_col), None)
        if not outer_col_actual or not inner_col_actual:
            continue

        # 确保至少有 2 行重叠值
        overlap_values = [_seed_value(outer_col_actual, i) for i in range(min(3, len(outer_rows), len(inner_rows)))]
        for i, val in enumerate(overlap_values):
            if i < len(outer_rows):
                outer_rows[i][outer_col_actual] = val
            if i < len(inner_rows):
                inner_rows[i][inner_col_actual] = val


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
        ref = _column_ref_in_select(column, select)
        actual = _actual_data_ref(data, ref) if ref else None
        if not actual:
            continue
        rows, column_name = actual
        if row_index >= len(rows):
            continue
        value = _comparison_truth_value(comparison, True)
        if value is not None:
            rows[row_index][column_name] = value


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
        if len(links) < 2:
            continue

        for outer_ref, inner_ref, inner_select in reversed(links):
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


def _base_value(col: str, idx: int, shared_values: dict[str, list[Any]]) -> Any:
    """
    主外键关联填充：如果当前列属于某个共享关联组，则从种子池中取值以保障表间能够成功连接。
    Fetches base value aligned with foreign key value pools if the column is part of a join group.
    """
    key = _join_group_key(col)
    if key in shared_values:
        return shared_values[key][idx % len(shared_values[key])]
    return _seed_value(col, idx)


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
) -> tuple[list[str], list[tuple[Any, ...]]]:
    """
    在内存 SQLite 隔离沙盒中建表、插入模拟数据并执行 SQL 查询，带有无限递归熔断机制。
    Executes SQL inside an in-memory SQLite sandbox with mock UDFs and infinite recursion guards.
    """
    conn = sqlite3.connect(":memory:")
    try:
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

        # 2. 注册进度挂接器 (Progress Handler)，指令达到 10w 条时触发熔断中断，防御 Recursive CTE 死循环
        # Sandbox Guard: interrupts connection if query takes more than 100,000 instructions
        conn.set_progress_handler(lambda: 1, 100000)

        cur = conn.cursor()
        # 3. 动态创建测试表并批量插入当前模拟的元组数据
        for table, columns in schema.items():
            if table not in rows:
                continue
            defs = ", ".join(f'"{col}" {_sqlite_type(col)}' for col in columns)
            cur.execute(f'CREATE TABLE "{table}" ({defs})')
            placeholders = ", ".join("?" for _ in columns)
            quoted_cols = ", ".join(f'"{col}"' for col in columns)
            insert_sql = f'INSERT INTO "{table}" ({quoted_cols}) VALUES ({placeholders})'
            values = [tuple(row.get(col) for col in columns) for row in rows[table]]
            if values:
                cur.executemany(insert_sql, values)

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
) -> tuple[list[str], list[tuple[Any, ...]]]:
    if backend == "mysql":
        if not native_executor_url:
            raise RuntimeError("mysql_native_executor_not_configured")
        return _execute_mysql_native(schema, schema_types, rows, sql, native_executor_url)
    return _execute_sqlite(schema, rows, sql)


def _execute_mysql_native(
    schema: dict[str, list[str]],
    schema_types: dict[str, dict[str, str]],
    rows: dict[str, list[dict[str, Any]]],
    sql: str,
    native_executor_url: str,
) -> tuple[list[str], list[tuple[Any, ...]]]:
    try:
        import pymysql
    except Exception as exc:  # pragma: no cover - depends on deployment env
        raise RuntimeError("pymysql_not_installed_for_mysql_native_executor") from exc

    params = _parse_mysql_url(native_executor_url)
    database = f"parseval_{uuid.uuid4().hex[:24]}"
    conn = pymysql.connect(
        host=params["host"],
        port=params["port"],
        user=params["user"],
        password=params["password"],
        database=params.get("database") or None,
        charset="utf8mb4",
        autocommit=True,
        connect_timeout=3,
        read_timeout=5,
        write_timeout=5,
        cursorclass=pymysql.cursors.Cursor,
    )
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"CREATE DATABASE {_mysql_ident(database)} "
                "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )
            cur.execute(f"USE {_mysql_ident(database)}")
            try:
                cur.execute("SET SESSION MAX_EXECUTION_TIME = 2000")
            except Exception:
                pass
            try:
                cur.execute("SET SESSION SQL_SELECT_LIMIT = 1000")
            except Exception:
                pass

            for table, columns in schema.items():
                if table not in rows:
                    continue
                column_defs = ", ".join(
                    f"{_mysql_ident(col)} {_mysql_type(table, col, schema_types, rows.get(table, []))}"
                    for col in columns
                )
                cur.execute(f"CREATE TABLE {_mysql_ident(table)} ({column_defs})")
                values = [tuple(_mysql_param_value(row.get(col)) for col in columns) for row in rows[table]]
                if values:
                    placeholders = ", ".join("%s" for _ in columns)
                    quoted_cols = ", ".join(_mysql_ident(col) for col in columns)
                    cur.executemany(
                        f"INSERT INTO {_mysql_ident(table)} ({quoted_cols}) VALUES ({placeholders})",
                        values,
                    )

            cur.execute(sql)
            result_rows = cur.fetchall()
            result_cols = [item[0] for item in (cur.description or [])]
            return result_cols, [tuple(_normalize_cell(cell) for cell in row) for row in result_rows]
    finally:
        try:
            with conn.cursor() as cur:
                cur.execute(f"DROP DATABASE IF EXISTS {_mysql_ident(database)}")
        finally:
            conn.close()


def _parse_mysql_url(url: str) -> dict[str, Any]:
    parsed = urlparse(url)
    if not parsed.scheme.startswith("mysql"):
        raise ValueError("PARSEVAL_MYSQL_URL must use a mysql scheme")
    return {
        "host": parsed.hostname or "127.0.0.1",
        "port": parsed.port or 3306,
        "user": unquote(parsed.username or ""),
        "password": unquote(parsed.password or ""),
        "database": (parsed.path or "").lstrip("/") or None,
    }


def _mysql_ident(name: str) -> str:
    return "`" + name.replace("`", "``") + "`"


def _mysql_type(
    table: str,
    column: str,
    schema_types: dict[str, dict[str, str]],
    table_rows: list[dict[str, Any]],
) -> str:
    explicit = _mysql_type_from_hint((schema_types.get(table) or {}).get(column))
    if explicit:
        return explicit
    values = [row.get(column) for row in table_rows if row.get(column) is not None]
    if values:
        if all(isinstance(value, bool) for value in values):
            return "TINYINT"
        if all(isinstance(value, int) and not isinstance(value, bool) for value in values):
            return "BIGINT"
        if all(isinstance(value, (int, float, Decimal)) and not isinstance(value, bool) for value in values):
            return "DOUBLE"
        if all(_looks_like_datetime_literal(value) for value in values):
            return "DATETIME"
    if _is_date_column(column):
        return "DATETIME"
    if _is_numeric_column(column):
        return "DOUBLE"
    return "VARCHAR(255)"


def _mysql_type_from_hint(type_hint: str | None) -> str | None:
    if not type_hint:
        return None
    normalized = type_hint.strip().upper()
    normalized = re.sub(r"\s+NOT\s+NULL\b|\s+NULL\b", "", normalized)
    normalized = re.sub(r"\s+PRIMARY\s+KEY\b", "", normalized)
    if any(token in normalized for token in ("INT", "SERIAL")):
        return "BIGINT"
    if any(token in normalized for token in ("DECIMAL", "NUMERIC", "DOUBLE", "FLOAT", "REAL")):
        return "DOUBLE"
    if "BOOL" in normalized:
        return "TINYINT"
    if "TIMESTAMP" in normalized or "DATETIME" in normalized:
        return "DATETIME"
    if normalized == "DATE" or normalized.startswith("DATE "):
        return "DATE"
    if "TIME" in normalized:
        return "TIME"
    if any(token in normalized for token in ("CHAR", "TEXT", "JSON", "UUID", "ENUM")):
        return "VARCHAR(255)"
    return None


def _mysql_param_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    return value


def _looks_like_datetime_literal(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    return bool(re.match(r"^\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}:\d{2})?$", value.strip()))


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
                "clause": diff.clause_category,
                "diff_type": diff.diff_type,
                "column": diff.target_column,
                "table": diff.target_table,
                "standard_sql": diff.extra.get("standard_sql") or _sql_of(diff.standard_node),
                "student_sql": diff.extra.get("student_sql") or _sql_of(diff.student_node),
            }
            for diff in ast_diffs
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
    ordered: bool,
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
    tests: list[dict[str, Any]] = []

    # 遍历算子进行替换与移除测试
    for spec in specs:
        std_node = standard_ast.args.get(spec["arg"]) or standard_ast.find(spec["node_type"])
        stu_node = student_ast.args.get(spec["arg"]) or student_ast.find(spec["node_type"])

        # 两者均没有该子句，跳过
        if std_node is None and stu_node is None:
            continue
        # 两者子句结构完全等价，跳过
        if std_node is not None and stu_node is not None and _sql_of(std_node) == _sql_of(stu_node):
            continue

        # 1. 替换变体 (Replacement Mutant)：用标准答案子句替换学生出错的子句
        if stu_node is not None and std_node is not None:
            replacement_sql = _mutate_by_node_replacement(student_ast, stu_node, std_node)
            if replacement_sql is None:
                replacement_sql = _mutate_select_arg(student_ast, spec["arg"], std_node)
        else:
            replacement_sql = _mutate_select_arg(student_ast, spec["arg"], std_node)

        # 2. 移除变体 (Removal Mutant)：剔除学生多写或写错的冗余子句
        if stu_node is not None:
            removal_sql = _mutate_by_node_replacement(student_ast, stu_node, None)
            if removal_sql is None:
                removal_sql = _mutate_select_arg(student_ast, spec["arg"], None)
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
        ))

    # 3. 针对 JOIN ON 进行专项的连接条件变分测试
    join_test = _run_join_on_mutation(
        schema=schema,
        rows=rows,
        standard_ast=standard_ast,
        student_ast=student_ast,
        standard_columns=standard_columns,
        standard_rows=standard_rows,
        ordered=ordered,
    )
    if join_test:
        tests.append(join_test)

    for specialized_test in (
        _run_distinct_mutation(
            schema=schema,
            rows=rows,
            standard_ast=standard_ast,
            student_ast=student_ast,
            standard_columns=standard_columns,
            standard_rows=standard_rows,
            ordered=ordered,
        ),
        _run_projection_mutation(
            schema=schema,
            rows=rows,
            standard_ast=standard_ast,
            student_ast=student_ast,
            standard_columns=standard_columns,
            standard_rows=standard_rows,
            ordered=ordered,
        ),
        _run_join_type_mutation(
            schema=schema,
            rows=rows,
            standard_ast=standard_ast,
            student_ast=student_ast,
            standard_columns=standard_columns,
            standard_rows=standard_rows,
            ordered=ordered,
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
        ),
        _run_set_operator_mutation(
            schema=schema,
            rows=rows,
            standard_ast=standard_ast,
            student_ast=student_ast,
            standard_columns=standard_columns,
            standard_rows=standard_rows,
            ordered=ordered,
        ),
        _run_recursive_cte_mutation(
            schema=schema,
            rows=rows,
            standard_ast=standard_ast,
            student_ast=student_ast,
            standard_columns=standard_columns,
            standard_rows=standard_rows,
            ordered=ordered,
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
) -> dict[str, Any]:
    test: dict[str, Any] = {
        "clause": clause,
        "knowledge_point_id": knowledge_point_id,
        "action": "replace_student_clause_with_standard_clause",
        "replacement_sqlite": None,
        "replacement_exec_ok": False,
        "replacement_equivalent": None,
        "fixed_by_replacement": False,
        "removal_sqlite": None,
        "removal_exec_ok": False,
        "removed_student_clause_equivalent": None,
        "error": None,
    }
    if replacement_sql:
        sqlite_sql = transpile_to_sqlite(replacement_sql)
        test["replacement_sqlite"] = sqlite_sql
        if sqlite_sql:
            try:
                cols, result_rows = _execute_sqlite(schema, rows, sqlite_sql)
                equivalent = _rows_equivalent(standard_columns, standard_rows, cols, result_rows, ordered)
                test["replacement_exec_ok"] = True
                test["replacement_equivalent"] = equivalent
                test["fixed_by_replacement"] = equivalent
            except Exception as exc:
                test["error"] = f"replacement_failed: {exc}"
    if removal_sql:
        sqlite_sql = transpile_to_sqlite(removal_sql)
        test["removal_sqlite"] = sqlite_sql
        if sqlite_sql:
            try:
                cols, result_rows = _execute_sqlite(schema, rows, sqlite_sql)
                equivalent = _rows_equivalent(standard_columns, standard_rows, cols, result_rows, ordered)
                test["removal_exec_ok"] = True
                test["removed_student_clause_equivalent"] = equivalent
            except Exception as exc:
                prev = test.get("error")
                test["error"] = f"{prev}; removal_failed: {exc}" if prev else f"removal_failed: {exc}"
    return test


def _run_join_on_mutation(
    *,
    schema: dict[str, list[str]],
    rows: dict[str, list[dict[str, Any]]],
    standard_ast: exp.Expression,
    student_ast: exp.Expression,
    standard_columns: list[str],
    standard_rows: list[tuple[Any, ...]],
    ordered: bool,
) -> dict[str, Any] | None:
    standard_joins = list(standard_ast.find_all(exp.Join))
    student_joins = list(student_ast.find_all(exp.Join))
    if not standard_joins or not student_joins:
        return None
    std_on = [join.args.get("on") for join in standard_joins]
    stu_on = [join.args.get("on") for join in student_joins]
    if [_sql_of(node) for node in std_on] == [_sql_of(node) for node in stu_on]:
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
) -> dict[str, Any] | None:
    std_select = standard_ast.find(exp.Select)
    stu_select = student_ast.find(exp.Select)
    if not isinstance(std_select, exp.Select) or not isinstance(stu_select, exp.Select):
        return None
    std_distinct = std_select.args.get("distinct")
    stu_distinct = stu_select.args.get("distinct")

    std_nested_distincts = list(standard_ast.find_all(exp.Distinct))
    stu_nested_distincts = list(student_ast.find_all(exp.Distinct))

    if bool(std_distinct) != bool(stu_distinct):
        mutated = student_ast.copy()
        mutated_select = mutated.find(exp.Select)
        if not isinstance(mutated_select, exp.Select):
            return None
        mutated_select.set("distinct", std_distinct.copy() if std_distinct is not None else None)
        return _execute_mutation_case(
            schema=schema,
            rows=rows,
            clause="DISTINCT",
            knowledge_point_id="distinct",
            replacement_sql=_sql_of(mutated),
            removal_sql=_mutate_select_distinct(student_ast, None) if stu_distinct is not None else None,
            standard_columns=standard_columns,
            standard_rows=standard_rows,
            ordered=ordered,
        )
    elif len(std_nested_distincts) != len(stu_nested_distincts):
        mutated = student_ast.copy()
        mutated_select = mutated.find(exp.Select)
        if isinstance(mutated_select, exp.Select) and isinstance(std_select, exp.Select):
            mutated_select.set("expressions", [expr.copy() for expr in std_select.expressions])
            return _execute_mutation_case(
                schema=schema,
                rows=rows,
                clause="DISTINCT",
                knowledge_point_id="distinct",
                replacement_sql=_sql_of(mutated),
                removal_sql=None,
                standard_columns=standard_columns,
                standard_rows=standard_rows,
                ordered=ordered,
            )
    return None


def _run_projection_mutation(
    *,
    schema: dict[str, list[str]],
    rows: dict[str, list[dict[str, Any]]],
    standard_ast: exp.Expression,
    student_ast: exp.Expression,
    standard_columns: list[str],
    standard_rows: list[tuple[Any, ...]],
    ordered: bool,
) -> dict[str, Any] | None:
    std_select = standard_ast.find(exp.Select)
    stu_select = student_ast.find(exp.Select)
    if not isinstance(std_select, exp.Select) or not isinstance(stu_select, exp.Select):
        return None
    std_exprs = std_select.expressions
    stu_exprs = stu_select.expressions
    if [_sql_of(e) for e in std_exprs] == [_sql_of(e) for e in stu_exprs]:
        return None

    mutated = student_ast.copy()
    mutated_select = mutated.find(exp.Select)
    if isinstance(mutated_select, exp.Select):
        mutated_select.set("expressions", [e.copy() for e in std_exprs])
        return _execute_mutation_case(
            schema=schema,
            rows=rows,
            clause="SELECT",
            knowledge_point_id="select-basic",
            replacement_sql=_sql_of(mutated),
            removal_sql=None,
            standard_columns=standard_columns,
            standard_rows=standard_rows,
            ordered=ordered,
        )
    return None


def _run_join_type_mutation(
    *,
    schema: dict[str, list[str]],
    rows: dict[str, list[dict[str, Any]]],
    standard_ast: exp.Expression,
    student_ast: exp.Expression,
    standard_columns: list[str],
    standard_rows: list[tuple[Any, ...]],
    ordered: bool,
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
) -> dict[str, Any] | None:
    std_op = _set_operator_name(standard_ast)
    stu_op = _set_operator_name(student_ast)
    if not std_op or _sql_of(standard_ast) == _sql_of(student_ast):
        return None
    if not isinstance(standard_ast, (exp.Union, exp.Intersect, exp.Except)):
        return None

    replacement_sql = _set_operator_replacement_sql(standard_ast, student_ast)
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
    )


def _set_operator_replacement_sql(standard_ast: exp.Expression, student_ast: exp.Expression) -> str | None:
    if isinstance(student_ast, type(standard_ast)):
        mutated = student_ast.copy()
        for arg in ("distinct", "by_name", "side", "kind"):
            mutated.set(arg, standard_ast.args.get(arg))
        return _sql_of(mutated)
    return _sql_of(standard_ast)


def _mutate_select_distinct(ast: exp.Expression, replacement: exp.Expression | None) -> str | None:
    mutated = ast.copy()
    select = mutated.find(exp.Select)
    if not isinstance(select, exp.Select):
        return None
    select.set("distinct", replacement.copy() if replacement is not None else None)
    return _sql_of(mutated)


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


def _mutate_select_arg(ast: exp.Expression, arg: str, replacement: exp.Expression | None) -> str | None:
    mutated = ast.copy()
    mutated.set(arg, replacement.copy() if replacement is not None else None)
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


def _sql_of(node: exp.Expression | None) -> str:
    if node is None:
        return ""
    try:
        return node.sql(dialect="sqlite", normalize=True)
    except Exception:
        return str(node)


def _sqlite_type(col: str) -> str:
    if _is_date_column(col):
        return "TEXT"
    return "REAL" if _is_numeric_column(col) else "TEXT"


def _is_date_column(col: str) -> bool:
    name = col.lower()
    tokens = [token for token in re.split(r"[^a-z0-9]+", name) if token]
    if name in {"date", "day", "month", "year"}:
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


def _sqlite_compat(sql: str) -> str:
    sql = sql.rstrip().rstrip(";")
    sql = re.sub(r"\bISNULL\s*\(", "IFNULL(", sql, flags=re.IGNORECASE)
    sql = re.sub(r"\[([^\]]+)\]", r'"\1"', sql)
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
    sql = re.sub(r":([A-Za-z_][A-Za-z0-9_]*)", lambda match: _parameter_literal(match.group(1)), sql)
    sql = re.sub(r"@([A-Za-z_][A-Za-z0-9_]*)", lambda match: _parameter_literal(match.group(1)), sql)
    sql = re.sub(r"(?i)(=\s*)student_name\b", r"\1'Alice'", sql)
    return sql


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
    return result.strftime("%Y-%m-%d %H:%M:%S" if result.time() else "%Y-%m-%d")


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
        p_table, p_col, _p_desc, _p_nulls_first = p_ref
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
    if not isinstance(value, (int, float, Decimal)) or not isinstance(literal, (int, float, Decimal)):
        return False
    if isinstance(node, exp.GT):
        return value > literal
    if isinstance(node, exp.GTE):
        return value >= literal
    if isinstance(node, exp.LT):
        return value < literal
    if isinstance(node, exp.LTE):
        return value <= literal
    if isinstance(node, exp.EQ):
        return value == literal
    if isinstance(node, exp.NEQ):
        return value != literal
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
    assignment = next(
        (
            dict(zip(sorted(standard_keys), truth_values))
            for truth_values in product((False, True), repeat=len(standard_keys))
            if _eval_logical_tree(standard_where.this, dict(zip(sorted(standard_keys), truth_values)))
            != _eval_logical_tree(student_where.this, dict(zip(sorted(standard_keys), truth_values)))
        ),
        None,
    )
    if not assignment:
        return False
    aliases = _table_aliases(standard_ast) or _table_aliases(student_ast)
    updates: list[tuple[str, str, Any]] = []
    for leaf in standard_leaves:
        update = _predicate_truth_assignment(leaf, assignment[_logical_leaf_key(leaf)])
        if not update:
            return False
        column, value = update
        table_ref = _norm_name(column.table or "")
        updates.append((aliases.get(table_ref, table_ref), _norm_name(column.name), value))
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
    if not any(ast and ast.find(exp.DenseRank) for ast in asts):
        return
    if not any(ast and ast.find(exp.Rank) for ast in asts):
        return
    ast = next((item for item in asts if item and item.find(exp.Window)), None)
    window = ast.find(exp.Window) if ast else None
    if not window:
        return
    partition = next(
        (item for item in (window.args.get("partition_by") or []) if isinstance(item, exp.Column)),
        None,
    )
    order = window.args.get("order")
    ordered = order.expressions[0] if isinstance(order, exp.Order) and order.expressions else None
    order_column = ordered.this if isinstance(ordered, exp.Ordered) and isinstance(ordered.this, exp.Column) else None
    if not partition or not order_column:
        return
    for rows in data.values():
        if not rows:
            continue
        lookup = _column_lookup(rows[0].keys())
        partition_name = lookup.get(_norm_name(partition.name))
        order_name = lookup.get(_norm_name(order_column.name))
        if not partition_name or not order_name:
            continue
        groups: dict[Any, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            groups[row[partition_name]].append(row)
        for group_rows in groups.values():
            if len(group_rows) >= 3:
                group_rows[0][order_name] = 20
                group_rows[1][order_name] = 20
                group_rows[2][order_name] = 10
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

        for index, row in enumerate(rows):
            for position, column in enumerate(std_columns):
                bucket = index // (2 ** (position + 1))
                row[column] = _group_probe_value(column, bucket, position)
            for position, column in enumerate(stu_columns):
                if column in std_columns:
                    continue
                bucket = (index // (2 ** position)) % 2
                row[column] = _group_probe_value(column, bucket, position + len(std_columns))


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
    if not standard_ast:
        return
    node = _set_operator_node(standard_ast)
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
    left_index = 0
    right_index = 0 if same_table and compatible else 1
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
    CTE 探针：提取 CTE 内部引用的基表和约束，确保基表数据满足 CTE 逻辑。
    递归 CTE：确保数据有足够的递归层级（如 parent-child 层级 >= 3）。
    """
    asts = [_parse_sql(standard_sql), _parse_sql(student_sql)]
    is_recursive = any(_is_recursive_ast(ast) for ast in asts if ast)

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

            if is_recursive:
                _apply_recursive_cte_hierarchy(data, schema, cte)


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
    if not any(diff.diff_type == "set_modifier_changed" for diff in ast_diffs):
        return
    asts = [_parse_sql(standard_sql), _parse_sql(student_sql)]
    if not any(_is_recursive_ast(ast) for ast in asts):
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
    filter_columns = {
        _norm_name(column.name)
        for where in outer_select.find_all(exp.Where)
        for column in where.find_all(exp.Column)
    }
    for item in outer_select.expressions or []:
        node = item.this if isinstance(item, exp.Alias) else item
        if not isinstance(node, exp.Column) or _norm_name(node.name) in filter_columns:
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
    trigger_clauses = ("JOIN", "JOIN_TYPE", "JOIN ON")
    trigger_diff_types = ("join_missing", "join_type_changed", "join_on_changed")
    trigger_kps = ("join-inner", "join-left", "join-right", "join-full", "join-on")

    @property
    def name(self) -> str:
        return "join_on_counterexample"

    def apply_data_probe(self, data, schema, standard_sql, student_sql, ast_diffs):
        _apply_join_on_counterexample(data, standard_sql, student_sql, ast_diffs)


class OrderByTiesTactic(Tactic):
    trigger_clauses = ("ORDER BY", "WINDOW")
    trigger_diff_types = (
        "order_by_changed",
        "order_by_tiebreaker_missing",
        "order_by_key_added",
        "order_direction_changed",
        "window_over_changed",
        "window_function_changed",
    )
    trigger_kps = ("order-by", "window-row-number")

    @property
    def name(self) -> str:
        return "ordered_compare_probe"

    def apply_data_probe(self, data, schema, standard_sql, student_sql, ast_diffs):
        _apply_order_by_probes(data, standard_sql, student_sql, ast_diffs)


class WindowPartitionTactic(Tactic):
    trigger_clauses = ("WINDOW",)
    trigger_diff_types = ("window_over_changed", "window_function_changed")
    trigger_kps = ("window-row-number",)

    @property
    def name(self) -> str:
        return "window_partition_order_probe"

    def apply_data_probe(self, data, schema, standard_sql, student_sql, ast_diffs):
        _apply_window_probes(data, standard_sql, student_sql, ast_diffs)


class GroupByProbesTactic(Tactic):
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
    trigger_clauses = ("UNION", "INTERSECT", "EXCEPT")
    trigger_diff_types = ("set_operator_changed", "set_modifier_changed")
    trigger_kps = ("union", "intersect", "except")

    @property
    def name(self) -> str:
        return "set_operator_overlap_probe"

    def apply_data_probe(self, data, schema, standard_sql, student_sql, ast_diffs):
        _apply_set_operator_probes(data, standard_sql, student_sql, ast_diffs)


class CteProbesTactic(Tactic):
    trigger_clauses = ("CTE", "CTE_RECURSIVE")
    trigger_diff_types = ("cte_changed", "recursive_cte_changed")
    trigger_kps = ("cte", "cte-recursive")

    @property
    def name(self) -> str:
        return "cte_constraint_probe"

    def apply_data_probe(self, data, schema, standard_sql, student_sql, ast_diffs):
        _apply_cte_probes(data, schema, standard_sql, student_sql, ast_diffs)


class CaseWhenProbesTactic(Tactic):
    trigger_clauses = ()
    trigger_diff_types = ()
    trigger_kps = ()

    @property
    def name(self) -> str:
        return "case_branch_probe"

    def can_trigger(self, ast_diffs: list[ASTDiffNode]) -> bool:
        # 始终触发，由 _apply_case_probes 内部检查是否有 CASE
        return True

    def apply_data_probe(self, data, schema, standard_sql, student_sql, ast_diffs):
        _apply_case_probes(data, schema, standard_sql, student_sql, ast_diffs)


class TacticRegistry:
    _registry: list[Tactic] = []

    @classmethod
    def register(cls, tactic: Tactic) -> None:
        cls._registry.append(tactic)

    @classmethod
    def get_active_tactics(cls, ast_diffs: list[ASTDiffNode]) -> list[Tactic]:
        return [t for t in sorted(cls._registry, key=lambda tactic: tactic.name) if t.can_trigger(ast_diffs)]

TacticRegistry.register(JoinOnCounterexampleTactic())
TacticRegistry.register(OrderByTiesTactic())
TacticRegistry.register(WindowPartitionTactic())
TacticRegistry.register(GroupByProbesTactic())
TacticRegistry.register(SetOperatorProbesTactic())
TacticRegistry.register(CteProbesTactic())
TacticRegistry.register(CaseWhenProbesTactic())


__all__ = [
    "SandboxRun",
    "extract_ast_diffs",
    "generate_and_compare",
    "generate_test_database",
    "parse_schema_column_types",
    "parse_schema_text",
    "transpile_to_sqlite",
]
