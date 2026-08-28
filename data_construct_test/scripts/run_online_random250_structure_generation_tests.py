"""Online-mined random250 SQL structure and data-generation test set.

This suite differs from web_common150/web_common250: standard SQL statements
are mined from online public exercise/solution files at runtime and cached with
source URLs.  Only the student SQL is deterministically mutated so the current
ASTDiff and ParSEval-style data generator can be tested against known mistakes.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import multiprocessing
import os
import pickle
import random
import re
import resource
import signal
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlglot import exp

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "sql-edu-backend"))
sys.path.insert(0, str(PROJECT_ROOT / "data_construct_test" / "scripts"))

from core.ast_schema import SQLStructureIR  # noqa: E402
from core.parseval_data_generator import (  # noqa: E402
    _parse_sql,
    _parse_sql_strict,
    extract_ast_diffs,
    generate_and_compare,
)
from run_data_generation_boundary_tests import infer_schema  # noqa: E402

OUTPUT_DIR = PROJECT_ROOT / "data_construct_test" / "outputs"
CACHE_DIR = OUTPUT_DIR / "online_random250_raw"
SEED = 20260724

LEETCODE_TREE_API = (
    "https://api.github.com/repos/siqichen-usc/LeetCode-SQL-Summary/"
    "git/trees/master?recursive=1"
)

STATIC_SOURCES = [
    {
        "source_id": "advanced_sql_practice_mysql",
        "source_name": "Advanced SQL Practice MySQL",
        "base_url": "https://github.com/santoshkhatri9860/advanced-sql-practice-mysql",
        "raw_prefix": "https://raw.githubusercontent.com/santoshkhatri9860/advanced-sql-practice-mysql/main/",
        "paths": [
            "archive/00_original_everything.sql",
            "solutions/01_subqueries_solutions.sql",
            "solutions/02_exists_correlated_solutions.sql",
            "solutions/03_window_basics_solutions.sql",
            "solutions/04_window_frames_solutions.sql",
            "solutions/05_lag_lead_solutions.sql",
            "solutions/06_row_number_solutions.sql",
            "solutions/07_rank_dense_rank_solutions.sql",
            "solutions/08_cte_non_recursive_solutions.sql",
            "solutions/09_cte_multi_solutions.sql",
            "solutions/10_recursive_cte_numbers_solutions.sql",
            "solutions/11_recursive_cte_hierarchy_solutions.sql",
            "solutions/12_datetime_solutions.sql",
        ],
    },
    {
        "source_id": "amirai31_sql_exercises",
        "source_name": "SQL-Exercises",
        "base_url": "https://github.com/amirai31/SQL-Exercises",
        "raw_prefix": "https://raw.githubusercontent.com/amirai31/SQL-Exercises/main/",
        "paths": [
            "SQL basics.sql",
            "SQL - Group BY, HAVING, ORDER BY.sql",
            "SQL_nested queries.sql",
            "SQL_Window functions and CTEs.sql",
            "SQL-Functions.sql",
            "SQL - Pattern Matching.sql",
        ],
    },
    {
        "source_id": "w3resource_sql_exercises_mirror",
        "source_name": "w3resource SQL Exercises mirror",
        "base_url": "https://github.com/tweichle/w3resource-SQL-Exercises",
        "branch": "master",
        "raw_prefix": "https://raw.githubusercontent.com/tweichle/w3resource-SQL-Exercises/master/",
        "paths": [
            "SQL Exercises - SUBQUERIES on Sales Database.sql",
            "SQL Exercises - SUBQUERIES on HR Database.sql",
            "SQL Exercises - JOINS on Sales Database.sql",
            "SQL Exercises - JOINS on HR Database.sql",
        ],
    },
]

DOC_EXCERPT_SOURCES = [
    {
        "source_id": "postgres_queries_union",
        "source_name": "PostgreSQL set-operation documentation",
        "url": "https://www.postgresql.org/docs/current/queries-union.html",
    },
    {
        "source_id": "postgres_with_docs",
        "source_name": "PostgreSQL WITH documentation",
        "url": "https://www.postgresql.org/docs/current/queries-with.html",
    },
    {
        "source_id": "postgres_select_docs",
        "source_name": "PostgreSQL SELECT reference",
        "url": "https://www.postgresql.org/docs/current/sql-select.html",
    },
    {
        "source_id": "w3schools_top_limit",
        "source_name": "W3Schools SELECT TOP/LIMIT/FETCH examples",
        "url": "https://www.w3schools.com/sql/sql_top.asp",
    },
]

CATEGORIES = [
    "SELECT",
    "DISTINCT",
    "WHERE",
    "JOIN ON",
    "GROUP BY",
    "HAVING",
    "Aggregate",
    "ORDER BY",
    "LIMIT / OFFSET",
    "Subquery",
    "Correlated Subquery",
    "CTE",
    "Recursive CTE",
    "Set Operation",
    "CASE",
    "Window",
    "Dialect Boundary",
]

QUOTAS = {
    "SELECT": 15,
    "DISTINCT": 15,
    "WHERE": 15,
    "JOIN ON": 15,
    "GROUP BY": 15,
    "HAVING": 15,
    "Aggregate": 15,
    "ORDER BY": 15,
    "LIMIT / OFFSET": 15,
    "Subquery": 15,
    "CTE": 15,
    "Window": 15,
    "Correlated Subquery": 14,
    "Recursive CTE": 14,
    "Set Operation": 14,
    "CASE": 14,
    "Dialect Boundary": 14,
}


def _slug(value: str) -> str:
    return re.sub(r"\W+", "_", value.lower()).strip("_") or "source"


def _download(url: str, *, offline_cache_only: bool = False, timeout: int = 60) -> str:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    suffix = Path(urllib.parse.urlparse(url).path).suffix or ".txt"
    cache_path = CACHE_DIR / f"{hashlib.sha256(url.encode()).hexdigest()[:16]}{suffix}"
    if cache_path.exists() and cache_path.stat().st_size:
        return cache_path.read_text(encoding="utf-8", errors="replace")
    if offline_cache_only:
        raise FileNotFoundError(f"missing cached source: {url}")
    request = urllib.request.Request(url, headers={"User-Agent": "sql-edu-online-random250/1.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read().decode("utf-8", errors="replace")
    cache_path.write_text(raw, encoding="utf-8")
    return raw


def _leetcode_sources(offline_cache_only: bool, timeout: int) -> list[dict[str, str]]:
    payload = json.loads(_download(LEETCODE_TREE_API, offline_cache_only=offline_cache_only, timeout=timeout))
    sources = []
    for node in payload.get("tree", []):
        path = str(node.get("path") or "")
        if not path.endswith(".sql"):
            continue
        quoted = urllib.parse.quote(path)
        sources.append({
            "source_id": "leetcode_sql_summary",
            "source_name": "LeetCode SQL Summary",
            "source_url": f"https://github.com/siqichen-usc/LeetCode-SQL-Summary/blob/master/{quoted}",
            "raw_url": f"https://raw.githubusercontent.com/siqichen-usc/LeetCode-SQL-Summary/master/{quoted}",
            "member": path,
        })
    return sources


def _iter_sources(offline_cache_only: bool, timeout: int) -> list[dict[str, str]]:
    sources = []
    for group in STATIC_SOURCES:
        for path in group["paths"]:
            quoted = urllib.parse.quote(path)
            branch = group.get("branch", "main")
            sources.append({
                "source_id": group["source_id"],
                "source_name": group["source_name"],
                "source_url": f"{group['base_url']}/blob/{branch}/{quoted}",
                "raw_url": f"{group['raw_prefix']}{quoted}",
                "member": path,
            })
    sources.extend(_leetcode_sources(offline_cache_only, timeout))
    for source in DOC_EXCERPT_SOURCES:
        sources.append({
            "source_id": source["source_id"],
            "source_name": source["source_name"],
            "source_url": source["url"],
            "raw_url": source["url"],
            "member": urllib.parse.urlparse(source["url"]).path.rsplit("/", 1)[-1],
        })
    return sources


def _strip_sql_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", "\n", text, flags=re.DOTALL)
    text = re.sub(r"--.*?$", "\n", text, flags=re.MULTILINE)
    text = re.sub(r"#.*?$", "\n", text, flags=re.MULTILINE)
    return text


def _extract_from_html(text: str) -> str:
    blocks = re.findall(r"<pre[^>]*>(.*?)</pre>|<code[^>]*>(.*?)</code>", text, flags=re.I | re.S)
    snippets = []
    for block in blocks:
        value = block[0] or block[1]
        value = re.sub(r"<[^>]+>", " ", value)
        snippets.append(html.unescape(value))
    return "\n;\n".join(snippets) if snippets else text


def _split_sql_statements(text: str) -> list[str]:
    text = _extract_from_html(text)
    text = _strip_sql_comments(text)
    statements: list[str] = []
    current: list[str] = []
    quote: str | None = None
    for char in text:
        current.append(char)
        if quote:
            if char == quote:
                quote = None
            continue
        if char in {"'", '"', "`"}:
            quote = char
            continue
        if char == ";":
            statement = _normalize_sql("".join(current))
            if statement:
                statements.append(statement)
            current = []
    tail = _normalize_sql("".join(current))
    if tail:
        statements.append(tail)
    return statements


def _normalize_sql(sql: str) -> str:
    sql = html.unescape(sql)
    sql = re.sub(r"\bGO\b", " ", sql, flags=re.I)
    sql = re.sub(r"\s+", " ", sql.strip())
    sql = sql.rstrip(";").strip()
    return sql + ";" if sql else ""


def _is_query(sql: str) -> bool:
    return bool(re.match(r"(?is)^\s*(with|select)\b", sql)) and not re.search(
        r"(?is)\b(insert|update|delete|create|drop|alter|truncate)\b", sql
    )


def _labels(sql: str, member: str) -> set[str]:
    text = f" {sql.lower()} "
    member_text = member.lower()
    labels: set[str] = {"SELECT"}
    if " distinct " in text:
        labels.add("DISTINCT")
    if " where " in text:
        labels.add("WHERE")
    if " join " in text and " on " in text:
        labels.add("JOIN ON")
    if " group by " in text:
        labels.add("GROUP BY")
    if " having " in text:
        labels.add("HAVING")
    if re.search(r"\b(count|sum|avg|min|max)\s*\(", text):
        labels.add("Aggregate")
    if " order by " in text:
        labels.add("ORDER BY")
    if re.search(r"\b(limit|offset|fetch\s+first|top)\b", text):
        labels.add("LIMIT / OFFSET")
    if re.search(r"\(\s*select\b|\bexists\s*\(", text):
        labels.add("Subquery")
    if ("correlated" in member_text or _looks_correlated(sql)) and re.search(r"\b\w+\.\w+\b", text):
        labels.add("Correlated Subquery")
    if re.match(r"(?is)^\s*with\b", sql):
        labels.add("CTE")
    if "recursive" in text or "recursive" in member_text:
        labels.add("Recursive CTE")
    if re.search(r"\b(union|intersect|except)\b", text):
        labels.add("Set Operation")
    if " case " in text and " when " in text:
        labels.add("CASE")
    if re.search(r"\bover\s*\(", text):
        labels.add("Window")
    if re.search(
        r"\b(top|dateadd|datepart|getdate|date_sub|ifnull|if\s*\(|fetch\s+first|year\s*\(|month\s*\(|limit\s+\d+\s*,)",
        text,
    ):
        labels.add("Dialect Boundary")
    return labels


def _looks_correlated(sql: str) -> bool:
    lowered = sql.lower()
    subquery_match = re.search(r"\(\s*select\b", lowered)
    if not subquery_match and " exists " not in f" {lowered} ":
        return False
    outer_part = lowered[: subquery_match.start()] if subquery_match else lowered
    inner_part = lowered[subquery_match.start() :] if subquery_match else lowered
    aliases = set()
    for match in re.finditer(
        r"\b(?:from|join)\s+[`\"\[]?[a-z_][\w$]*[`\"\]]?(?:\s+(?:as\s+)?)?([a-z_][\w$]*)?",
        outer_part,
    ):
        alias = match.group(1)
        if alias and alias not in {
            "where", "join", "on", "group", "having", "order", "limit",
            "left", "right", "inner", "outer", "cross",
        }:
            aliases.add(alias)
    return any(re.search(rf"\b{re.escape(alias)}\s*\.", inner_part) for alias in aliases)


def collect_online_queries(offline_cache_only: bool = False, timeout: int = 60) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for source in _iter_sources(offline_cache_only, timeout):
        try:
            raw = _download(source["raw_url"], offline_cache_only=offline_cache_only, timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            records.append({**source, "collection_error": str(exc), "sql": ""})
            continue
        for sql in _split_sql_statements(raw):
            if not _is_query(sql) or len(sql) < 20 or len(sql) > 2500:
                continue
            key = hashlib.sha256(re.sub(r"\s+", " ", sql.lower()).encode()).hexdigest()
            if key in seen:
                continue
            seen.add(key)
            labels = _labels(sql, source["member"])
            if len(labels) <= 1:
                continue
            records.append({
                "id": f"online_sql_{key[:20]}",
                **source,
                "sql": sql,
                "labels": sorted(labels),
            })
    return [record for record in records if record.get("sql")]


def _first_int(text: str) -> int | None:
    match = re.search(r"\b(\d+)\b", text)
    return int(match.group(1)) if match else None


def _run_with_parser_timeout(function, *, timeout_seconds: float = 1.0):
    """Bound parser/mutation work before it enters the isolated evaluator.

    Candidate construction runs in the parent process, so a pathological
    public SQL string must not be allowed to wedge the whole corpus build.
    ``SIGALRM`` interrupts SQLGlot's Python parser on the POSIX/WSL target;
    non-POSIX callers retain the previous behavior because ``setitimer`` is
    not universally available there.
    """

    if not hasattr(signal, "SIGALRM") or not hasattr(signal, "setitimer"):
        return function()

    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.setitimer(signal.ITIMER_REAL, 0)

    def _timeout_handler(_signum, _frame):
        raise TimeoutError("public SQL parser exceeded its construction budget")

    signal.signal(signal.SIGALRM, _timeout_handler)
    signal.setitimer(signal.ITIMER_REAL, max(0.01, float(timeout_seconds)))
    try:
        return function()
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        if previous_timer[0] or previous_timer[1]:
            signal.setitimer(signal.ITIMER_REAL, *previous_timer)


def _bump_first_int(sql: str, delta: int = 1) -> str:
    return re.sub(r"\b(\d+)\b", lambda m: str(int(m.group(1)) + delta), sql, count=1)


def mutate_sql(sql: str, category: str) -> tuple[str | None, str]:
    text = sql
    if category == "DISTINCT" and re.search(r"(?is)\bselect\s+distinct\b", text):
        mutated = _mutate_observable_distinct(text)
        if mutated != text:
            return mutated, "distinct_removed"
    if category == "JOIN ON" and re.search(r"(?is)\bon\b", text):
        mutated = _mutate_join_comparison(text)
        if mutated != text:
            return mutated, "join_on_operator_changed"
    if category in {"WHERE", "HAVING", "Subquery", "Correlated Subquery", "CTE"}:
        mutated = _mutate_comparison(text)
        if mutated != text:
            return mutated, f"{category.lower().replace(' ', '_')}_comparison_changed"
    if category == "GROUP BY" and re.search(r"(?is)\bgroup\s+by\b", text):
        mutated = _mutate_group_by(text)
        if mutated != text:
            return mutated, "group_by_expression_changed"
    if category == "Aggregate":
        mutated = _mutate_aggregate(text)
        if mutated != text:
            return mutated, "aggregate_function_changed"
    if category == "ORDER BY" and re.search(r"(?is)\border\s+by\b", text):
        if re.search(r"(?is)\bdesc\b", text):
            return re.sub(r"(?is)\bdesc\b", "ASC", text, count=1), "order_direction_changed"
        if re.search(r"(?is)\basc\b", text):
            return re.sub(r"(?is)\basc\b", "DESC", text, count=1), "order_direction_changed"
        return re.sub(r"(?is)\border\s+by\s+([^;]+?)(\s+limit|\s+offset|\s+fetch|\s*$)", r"ORDER BY 1 DESC\2", text, count=1), "order_expression_changed"
    if category == "LIMIT / OFFSET":
        mutated = re.sub(r"(?is)\blimit\s+(\d+)\s*,\s*(\d+)", lambda m: f"LIMIT {m.group(1)}, {int(m.group(2)) + 1}", text, count=1)
        if mutated != text:
            return mutated, "limit_count_changed"
        mutated = re.sub(r"(?is)\blimit\s+(\d+)", lambda m: f"LIMIT {int(m.group(1)) + 1}", text, count=1)
        if mutated != text:
            return mutated, "limit_count_changed"
        mutated = re.sub(r"(?is)\boffset\s+(\d+)", lambda m: f"OFFSET {int(m.group(1)) + 1}", text, count=1)
        if mutated != text:
            return mutated, "offset_changed"
        mutated = re.sub(r"(?is)\btop\s+(\d+)", lambda m: f"TOP {int(m.group(1)) + 1}", text, count=1)
        if mutated != text:
            return mutated, "top_count_changed"
        mutated = re.sub(r"(?is)\bfetch\s+first\s+(\d+)", lambda m: f"FETCH FIRST {int(m.group(1)) + 1}", text, count=1)
        if mutated != text:
            return mutated, "fetch_count_changed"
    if category == "Recursive CTE":
        mutated = re.sub(r"(?is)(\bn\s*[+]\s*)1\b", r"\g<1>2", text, count=1)
        if mutated != text:
            return mutated, "recursive_step_changed"
        bumped = _bump_first_int(text)
        if bumped != text:
            return bumped, "recursive_boundary_changed"
    if category == "Set Operation":
        mutated, target = _mutate_observable_set_operation(text)
        if mutated != text:
            return mutated, target
        if re.search(r"(?is)\bintersect\b", text):
            return re.sub(r"(?is)\bintersect\b", "UNION", text, count=1), "set_operator_changed"
        if re.search(r"(?is)\bexcept\b", text):
            return re.sub(r"(?is)\bexcept\b", "UNION", text, count=1), "set_operator_changed"
    if category == "CASE":
        mutated = _mutate_comparison(text)
        if mutated != text:
            return mutated, "case_condition_changed"
        if re.search(r"(?is)\belse\b", text):
            return re.sub(r"(?is)\s+else\s+[^;]+?\s+end\b", " END", text, count=1), "case_else_removed"
    if category == "Window":
        if re.search(r"(?is)\bpartition\s+by\b", text):
            return re.sub(r"(?is)\bpartition\s+by\s+.+?(\s+order\s+by|\))", r"\1", text, count=1), "window_partition_removed"
        if re.search(r"(?is)\brow_number\s*\(", text):
            return re.sub(r"(?is)\brow_number\s*\(", "RANK(", text, count=1), "window_function_changed"
    if category == "Dialect Boundary":
        mutated = _mutate_dialect_boundary(text)
        if mutated != text:
            return mutated, "dialect_boundary_expression_changed"
    if category == "SELECT":
        return _mutate_projection(text), "projection_changed"
    return None, "no_mutation"


def _mutate_observable_distinct(sql: str) -> str:
    ast = _parse_sql(sql)
    if not ast:
        return sql
    mutated = ast.copy()
    for select in mutated.find_all(exp.Select):
        if not select.args.get("distinct"):
            continue
        parent = select.parent
        semantically_absorbed = False
        while parent is not None and not isinstance(parent, exp.Select):
            if isinstance(parent, (exp.In, exp.Exists)):
                semantically_absorbed = True
                break
            if isinstance(parent, (exp.Except, exp.Intersect)):
                semantically_absorbed = True
                break
            if isinstance(parent, exp.Union) and parent.args.get("distinct") is not False:
                semantically_absorbed = True
                break
            parent = parent.parent
        if semantically_absorbed:
            continue
        select.set("distinct", None)
        return mutated.sql()
    return sql


def _mutate_observable_set_operation(sql: str) -> tuple[str, str]:
    ast = _parse_sql(sql)
    node = next(
        (item for item in ast.walk() if isinstance(item, (exp.Union, exp.Intersect, exp.Except))),
        None,
    ) if ast else None
    if not isinstance(node, (exp.Union, exp.Intersect, exp.Except)):
        return sql, "no_mutation"
    if isinstance(node, exp.Union) and (
        _is_monotonic_schema_free_recursive_union(node)
        or _is_single_parent_hierarchy_recursive_union(node)
    ):
        return sql, "no_mutation"
    parent = node.parent
    inside_membership = False
    while parent is not None:
        if isinstance(parent, (exp.In, exp.Exists)):
            inside_membership = True
            break
        parent = parent.parent
    left_select = node.this if isinstance(node.this, exp.Select) else node.this.find(exp.Select)
    right_select = node.expression if isinstance(node.expression, exp.Select) else node.expression.find(exp.Select)
    left_item = left_select.expressions[0] if isinstance(left_select, exp.Select) and left_select.expressions else None
    right_item = right_select.expressions[0] if isinstance(right_select, exp.Select) and right_select.expressions else None
    disjoint_constants = (
        isinstance(left_item, exp.Literal)
        and isinstance(right_item, exp.Literal)
        and left_item.this != right_item.this
    )
    if isinstance(node, exp.Union) and not inside_membership and not disjoint_constants:
        if node.args.get("distinct") is False:
            return re.sub(r"(?is)\bunion\s+all\b", "UNION", sql, count=1), "set_all_removed"
        return re.sub(r"(?is)\bunion\b", "UNION ALL", sql, count=1), "set_all_added"
    if isinstance(node, exp.Intersect):
        return re.sub(r"(?is)\bintersect\b", "UNION", sql, count=1), "set_operator_changed"
    if isinstance(node, exp.Except):
        return re.sub(r"(?is)\bexcept\b", "UNION", sql, count=1), "set_operator_changed"
    return re.sub(r"(?is)\bunion(?:\s+all)?\b", "INTERSECT", sql, count=1), "set_operator_changed"


def _is_monotonic_schema_free_recursive_union(node: exp.Union) -> bool:
    """Reject UNION modifier mutations that cannot create duplicate states."""
    cte = node.find_ancestor(exp.CTE)
    if not isinstance(cte, exp.CTE) or not cte.alias_or_name:
        return False
    cte_name = _norm_sql_fragment(str(cte.alias_or_name))
    table_names = {
        _norm_sql_fragment(table.name)
        for table in cte.this.find_all(exp.Table)
        if table.name
    }
    if table_names != {cte_name}:
        return False
    recursive_select = (
        node.expression
        if isinstance(node.expression, exp.Select)
        else node.expression.find(exp.Select)
    )
    if not isinstance(recursive_select, exp.Select) or not recursive_select.expressions:
        return False
    expression = recursive_select.expressions[0]
    expression = expression.this if isinstance(expression, exp.Alias) else expression
    if not isinstance(expression, (exp.Add, exp.Sub, exp.Mul)):
        return False
    operands = (expression.left, expression.right)
    if not any(isinstance(item, exp.Column) for item in operands):
        return False
    deltas = [item for item in operands if isinstance(item, exp.Literal) and item.is_number]
    if not deltas:
        return False
    numeric_deltas = [float(item.this) for item in deltas]
    if isinstance(expression, (exp.Add, exp.Sub)) and all(
        value == 0 for value in numeric_deltas
    ):
        return False
    if isinstance(expression, exp.Mul) and not any(
        abs(value) > 1 for value in numeric_deltas
    ):
        return False
    where = recursive_select.args.get("where")
    return bool(
        isinstance(where, exp.Where)
        and where.find(exp.Column)
        and next(
            (
                literal
                for literal in where.find_all(exp.Literal)
                if literal.is_number
            ),
            None,
        )
    )


def _is_single_parent_hierarchy_recursive_union(node: exp.Union) -> bool:
    """Recognize tree recursion where every entity has one parent path."""
    cte = node.find_ancestor(exp.CTE)
    if not isinstance(cte, exp.CTE) or not cte.alias_or_name:
        return False
    cte_name = _norm_sql_fragment(str(cte.alias_or_name))
    recursive_select = (
        node.expression
        if isinstance(node.expression, exp.Select)
        else node.expression.find(exp.Select)
    )
    if not isinstance(recursive_select, exp.Select):
        return False
    aliases = {
        _norm_sql_fragment(table.alias_or_name): _norm_sql_fragment(table.name)
        for table in recursive_select.find_all(exp.Table)
    }
    physical_tables = {
        table_name for table_name in aliases.values() if table_name != cte_name
    }
    if len(physical_tables) != 1:
        return False
    hierarchy_tokens = ("parent", "manager", "boss", "supervisor", "reports_to")
    for equality in recursive_select.find_all(exp.EQ):
        if not isinstance(equality.left, exp.Column) or not isinstance(equality.right, exp.Column):
            continue
        columns = (equality.left, equality.right)
        physical = next(
            (
                column
                for column in columns
                if aliases.get(_norm_sql_fragment(column.table), "") in physical_tables
            ),
            None,
        )
        recursive = next(
            (
                column
                for column in columns
                if aliases.get(_norm_sql_fragment(column.table), "") == cte_name
            ),
            None,
        )
        if (
            isinstance(physical, exp.Column)
            and isinstance(recursive, exp.Column)
            and any(token in _norm_sql_fragment(physical.name) for token in hierarchy_tokens)
            and (
                _norm_sql_fragment(recursive.name) == "id"
                or _norm_sql_fragment(recursive.name).endswith("_id")
            )
        ):
            return True
    return False


def _mutate_comparison(sql: str) -> str:
    replacements = [(">=", ">"), ("<=", "<"), ("<>", "="), ("!=", "="), (">", ">="), ("<", "<="), ("=", "<>")]
    for old, new in replacements:
        pattern = rf"(?<![<>=!]){re.escape(old)}(?![<>=])"
        mutated = re.sub(pattern, new, sql, count=1)
        if mutated != sql:
            return mutated
    return _bump_first_int(sql) or sql


def _mutate_join_comparison(sql: str) -> str:
    ast = _parse_sql(sql)
    if ast is None:
        return sql
    mutated = ast.copy()
    replacements = {
        exp.GTE: exp.GT,
        exp.LTE: exp.LT,
        exp.GT: exp.GTE,
        exp.LT: exp.LTE,
        exp.EQ: exp.NEQ,
        exp.NEQ: exp.EQ,
    }
    for join in mutated.find_all(exp.Join):
        predicate = join.args.get("on")
        if not isinstance(predicate, exp.Expression):
            continue
        comparison = next(
            (
                node
                for node in predicate.walk()
                if type(node) in replacements
                and node.find_ancestor(exp.Join) is join
            ),
            None,
        )
        if comparison is None:
            continue
        replacement_type = replacements[type(comparison)]
        comparison.replace(
            replacement_type(
                this=comparison.this.copy(),
                expression=comparison.expression.copy(),
            )
        )
        return mutated.sql()
    return sql


def _mutate_aggregate(sql: str) -> str:
    replacements = [("count", "sum"), ("sum", "avg"), ("avg", "sum"), ("max", "min"), ("min", "max")]
    for old, new in replacements:
        mutated = re.sub(rf"(?is)\b{old}\s*\(", f"{new.upper()}(", sql, count=1)
        if mutated != sql:
            return mutated
    return sql


def _mutate_group_by(sql: str) -> str:
    ast = _parse_sql(sql)
    if not ast:
        return sql
    mutated = ast.copy()
    grouped_select = next(
        (select for select in mutated.find_all(exp.Select) if isinstance(select.args.get("group"), exp.Group)),
        None,
    )
    if not isinstance(grouped_select, exp.Select):
        return sql

    group = grouped_select.args.get("group")
    current_items = list(group.expressions or []) if isinstance(group, exp.Group) else []
    current_sql = {_norm_sql_fragment(item.sql()) for item in current_items}

    for projection in grouped_select.expressions or []:
        expression = projection.this if isinstance(projection, exp.Alias) else projection
        if expression.find(exp.AggFunc) or expression.find(exp.Window):
            continue
        candidate = expression if isinstance(expression, exp.Column) else expression.find(exp.Column)
        if not isinstance(candidate, exp.Column):
            continue
        if _norm_sql_fragment(candidate.sql()) not in current_sql:
            grouped_select.set("group", exp.Group(expressions=[candidate.copy()]))
            return mutated.sql()

    if len(current_items) > 1:
        key_index = next(
            (
                index
                for index, item in enumerate(current_items)
                for column in ([item] if isinstance(item, exp.Column) else item.find_all(exp.Column))
                if _norm_sql_fragment(column.name) == "id"
                or _norm_sql_fragment(column.name).endswith("_id")
            ),
            len(current_items) - 1,
        )
        grouped_select.set(
            "group",
            exp.Group(
                expressions=[
                    item.copy()
                    for index, item in enumerate(current_items)
                    if index != key_index
                ]
            ),
        )
        return mutated.sql()

    grouped_select.set("group", exp.Group(expressions=[exp.Literal.string("__group_probe__")]))
    return mutated.sql()


def _norm_sql_fragment(sql: str) -> str:
    return re.sub(r"\s+", "", sql).strip('"`[]').lower()


def _mutate_projection(sql: str) -> str | None:
    match = re.match(r"(?is)\s*select\s+(.+?)\s+from\s+", sql)
    if not match:
        return None
    projection = match.group(1)
    if "," not in projection or re.search(r"(?is)\bdistinct\b|\bover\s*\(", projection):
        return None
    first = projection.split(",", 1)[0].strip()
    return sql[: match.start(1)] + first + sql[match.end(1) :]


def _mutate_dialect_boundary(sql: str) -> str:
    replacements = [
        (r"(?is)\byear\s*\(", "MONTH("),
        (r"(?is)\bmonth\s*\(", "YEAR("),
        (r"(?is)\bdateadd\s*\(\s*month", "DATEADD(DAY"),
        (r"(?is)\bdateadd\s*\(\s*day", "DATEADD(MONTH"),
        (r"(?is)\bfetch\s+first\s+(\d+)", lambda m: f"FETCH FIRST {int(m.group(1)) + 1}"),
        (r"(?is)\btop\s+(\d+)", lambda m: f"TOP {int(m.group(1)) + 1}"),
        (r"(?is)\blimit\s+(\d+)\s*,\s*(\d+)", lambda m: f"LIMIT {int(m.group(1)) + 1}, {m.group(2)}"),
    ]
    for pattern, repl in replacements:
        mutated = re.sub(pattern, repl, sql, count=1)
        if mutated != sql:
            return mutated
    return _bump_first_int(sql) or sql


def _case_from_record(record: dict[str, Any], category: str) -> dict[str, Any] | None:
    try:
        student, target = _run_with_parser_timeout(
            lambda: mutate_sql(record["sql"], category)
        )
        if not student or student == record["sql"]:
            return None
        standard_ast = _run_with_parser_timeout(lambda: _parse_sql(record["sql"]))
        student_ast = _run_with_parser_timeout(lambda: _parse_sql(student))
    except (TimeoutError, RecursionError, MemoryError, ValueError):
        # Public sources are inputs, not trusted parser fixtures.  A malformed
        # or pathological candidate is excluded from the smoke corpus rather
        # than blocking generation or being treated as a product capability.
        return None
    if standard_ast is None or student_ast is None:
        return None
    case_key = hashlib.sha256(f"{record['id']}\0{category}\0{student}".encode()).hexdigest()[:20]
    return {
        "id": f"online_random250_{case_key}",
        "dataset": "online_random250",
        "structure": category,
        "source": record["source_name"],
        "source_id": record["source_id"],
        "source_url": record["source_url"],
        "member": record["member"],
        "standard": record["sql"],
        "student": student,
        "student_generation_method": "deterministic_mutation",
        "strict_target": target,
        "expected_equivalent": False,
        "online_labels": record["labels"],
    }


def _smoke_quotas(total_limit: int) -> dict[str, int]:
    """Distribute a smoke-test limit across categories without building 250 cases."""

    quotas = {category: 0 for category in QUOTAS}
    remaining = total_limit
    while remaining:
        progressed = False
        for category, maximum in QUOTAS.items():
            if quotas[category] >= maximum:
                continue
            quotas[category] += 1
            remaining -= 1
            progressed = True
            if remaining == 0:
                break
        if not progressed:
            break
    return {category: quota for category, quota in quotas.items() if quota}


def _build_smoke_cases(
    records: list[dict[str, Any]],
    seed: int,
    total_limit: int,
) -> list[dict[str, Any]]:
    """Build only the requested cases, parsing candidates lazily.

    The full corpus builder validates every candidate before quota sampling.
    That is appropriate for a report regeneration, but made ``--case-limit 1``
    parse thousands of unused standard/student pairs.  Smoke mode instead
    shuffles cheap record references and parses only until each small quota is
    filled.
    """

    rng = random.Random(seed)
    selected: list[dict[str, Any]] = []
    used_standards: set[str] = set()
    shortages: dict[str, dict[str, int]] = {}

    for category, quota in _smoke_quotas(total_limit).items():
        candidates = [record for record in records if category in record["labels"]]
        rng.shuffle(candidates)
        picks: list[dict[str, Any]] = []

        for allow_reused_standard in (False, True):
            for record in candidates:
                standard_key = hashlib.sha256(record["sql"].lower().encode()).hexdigest()
                if not allow_reused_standard and standard_key in used_standards:
                    continue
                case = _case_from_record(record, category)
                if case is None or case in picks:
                    continue
                picks.append(case)
                used_standards.add(standard_key)
                if len(picks) == quota:
                    break
            if len(picks) == quota:
                break

        if len(picks) < quota:
            shortages[category] = {
                "available": len(candidates),
                "selected": len(picks),
                "quota": quota,
            }
        selected.extend(picks)

    if shortages:
        raise RuntimeError(f"not enough online mined smoke candidates: {json.dumps(shortages, ensure_ascii=False)}")
    rng.shuffle(selected)
    for index, item in enumerate(selected, 1):
        item["sample_index"] = index
    return selected


def build_cases(
    records: list[dict[str, Any]],
    seed: int,
    total_limit: int = 0,
) -> list[dict[str, Any]]:
    if total_limit:
        return _build_smoke_cases(records, seed, total_limit)

    rng = random.Random(seed)
    pools: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        for category in record["labels"]:
            if category not in QUOTAS:
                continue
            case = _case_from_record(record, category)
            if case is None:
                continue
            pools[category].append(case)

    selected: list[dict[str, Any]] = []
    used_standards: set[str] = set()
    shortages = {}
    for category, quota in QUOTAS.items():
        candidates = pools[category][:]
        rng.shuffle(candidates)
        picks = []
        for item in candidates:
            key = hashlib.sha256(item["standard"].lower().encode()).hexdigest()
            if key in used_standards:
                continue
            picks.append(item)
            used_standards.add(key)
            if len(picks) == quota:
                break
        if len(picks) < quota:
            for item in candidates:
                if item in picks:
                    continue
                picks.append(item)
                if len(picks) == quota:
                    break
        if len(picks) < quota:
            shortages[category] = {"available": len(pools[category]), "selected": len(picks), "quota": quota}
        selected.extend(picks)
    if shortages:
        raise RuntimeError(f"not enough online mined candidates: {json.dumps(shortages, ensure_ascii=False)}")
    rng.shuffle(selected)
    for index, item in enumerate(selected, 1):
        item["sample_index"] = index
    return selected


def _diff_dict(diff: Any) -> dict[str, Any]:
    try:
        return diff.to_dict()
    except Exception:
        return {
            "clause": getattr(diff, "clause_category", ""),
            "diff_type": getattr(diff, "diff_type", ""),
            "knowledge_point_id": getattr(diff, "knowledge_point_id", ""),
            "extra": getattr(diff, "extra", {}),
        }


def evaluate_case(case: dict[str, Any], max_rows: int) -> dict[str, Any]:
    result = {**case}
    errors: list[str] = []
    standard_ast = _parse_sql_strict(case["standard"])
    student_ast = _parse_sql_strict(case["student"])
    result["standard_parse_ok"] = standard_ast is not None
    result["student_parse_ok"] = student_ast is not None
    if standard_ast is None or student_ast is None:
        standard_failed = standard_ast is None
        result["executed"] = False
        result["execution_error"] = (
            "standard_sql_parse_failed" if standard_failed else "student_sql_parse_failed"
        )
        result["row_equivalent"] = False
        result["observable_mismatch"] = False
        result["verdict_status"] = "INPUT_GAP" if standard_failed else "SUPPORTED"
        result["equivalence_conclusion"] = (
            "UNDECIDED" if standard_failed else "NOT_EQUIVALENT"
        )
        result["error_code"] = (
            "STANDARD_SQL_PARSE_ERROR" if standard_failed else "STUDENT_SQL_PARSE_ERROR"
        )
        result["boundary_evidence"] = (
            {
                "reason": "invalid_standard_sql",
                "sql_role": "standard",
                "error_code": "STANDARD_SQL_PARSE_ERROR",
            }
            if standard_failed
            else {}
        )
        result["data_generation_status"] = (
            "INPUT_GAP" if standard_failed else "INVALID_STUDENT_SQL"
        )
        errors.append(result["data_generation_status"])
        result["errors"] = errors
        result["strict_pass"] = False
        return result

    standard_ir = SQLStructureIR.from_ast(standard_ast)
    result["standard_ir_features"] = sorted(standard_ir.feature_kps())
    result["standard_ir"] = standard_ir.to_dict()

    try:
        diffs = extract_ast_diffs(case["standard"], case["student"])
        result["ast_diff_graph"] = [_diff_dict(diff) for diff in diffs]
        result["diff_types"] = [diff.diff_type for diff in diffs]
    except Exception as exc:  # noqa: BLE001
        diffs = []
        result["ast_diff_graph"] = []
        result["diff_types"] = []
        errors.append(f"diff_exception:{type(exc).__name__}:{exc}")
    if not result["diff_types"]:
        errors.append("diff_missing")

    schema = infer_schema(case["standard"], case["student"])
    result["schema"] = schema
    run = generate_and_compare(schema, case["standard"], case["student"], max_rows_per_table=max_rows)
    result["executed"] = run.executed
    result["execution_error"] = run.error
    result["verdict_status"] = run.status
    result["equivalence_conclusion"] = run.equivalence_conclusion
    result["boundary_evidence"] = run.boundary_evidence
    result["error_code"] = run.error_code
    result["judge_status"] = run.judge_status
    result["row_equivalent"] = run.is_equivalent is True
    result["observable_mismatch"] = (
        run.is_equivalent is False
        or bool(run.executed and not (run.data_evidence or {}).get("column_names_match", True))
    )
    result["standard_row_count"] = len(run.standard_rows)
    result["student_row_count"] = len(run.student_rows)
    result["standard_rows_sample"] = run.standard_rows[:5]
    result["student_rows_sample"] = run.student_rows[:5]
    result["generation_tactics"] = (run.data_evidence or {}).get("generation_tactics", [])
    result["mutation_summary"] = (run.mutation_evidence or {}).get("summary") or {}
    result["test_database_sample"] = {table: rows[:5] for table, rows in run.test_database.items()}
    result["data_generation_status"] = _data_generation_status(result)
    if result["data_generation_status"] != "PASS":
        errors.append(result["data_generation_status"])
    result["errors"] = errors
    result["strict_pass"] = not errors
    return result


def _configure_worker_memory(memory_mb: int) -> None:
    """Apply a hard address-space cap before evaluating one or more cases."""

    limit = int(memory_mb) * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_AS, (limit, limit))


_CASE_EVALUATION_TIMEOUT_SECONDS = 15.0


def _case_worker_entry(
    case: dict[str, Any],
    max_rows: int,
    result_path: str,
    worker_memory_mb: int,
) -> None:
    """Evaluate one public case in a killable process group."""

    try:
        if os.name == "posix":
            try:
                os.setsid()
            except OSError:
                pass
        _configure_worker_memory(worker_memory_mb)
        envelope = ("ok", evaluate_case(case, max_rows=max_rows))
    except BaseException as exc:  # pragma: no cover - exercised through parent projection
        envelope = ("error", type(exc).__name__, str(exc))
    with open(result_path, "wb") as handle:
        pickle.dump(envelope, handle, protocol=pickle.HIGHEST_PROTOCOL)


def _terminate_case_process(process: multiprocessing.Process) -> None:
    """Kill a case child and its private descendants without touching parent."""

    if not process.is_alive():
        process.join(timeout=1.0)
        return
    if os.name == "posix" and process.pid is not None:
        try:
            process_group = os.getpgid(process.pid)
        except (ProcessLookupError, OSError):
            process_group = None
        if process_group == process.pid:
            try:
                os.killpg(process_group, signal.SIGKILL)
            except ProcessLookupError:
                pass
        else:
            process.kill()
    else:
        process.kill()
    process.join(timeout=1.0)


def _resource_timeout_result(case: dict[str, Any], reason: str) -> dict[str, Any]:
    """Project an evaluator timeout/crash without inventing a SQL verdict."""

    return {
        **case,
        "standard_parse_ok": False,
        "student_parse_ok": False,
        "executed": False,
        "execution_error": reason,
        "row_equivalent": False,
        "observable_mismatch": False,
        "verdict_status": "SUPPORTED_WITH_LIMITS",
        "equivalence_conclusion": "UNDECIDED",
        "error_code": "RESOURCE_LIMIT",
        "boundary_evidence": {"reason": reason},
        "generation_tactics": [],
        "mutation_summary": {},
        "data_generation_status": "RESOURCE_LIMIT",
        "errors": ["RESOURCE_LIMIT"],
        "strict_pass": False,
    }


def _evaluate_case_bounded(
    case: dict[str, Any],
    *,
    max_rows: int,
    worker_memory_mb: int,
    timeout_seconds: float = _CASE_EVALUATION_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Run one evaluator with a hard wall-clock bound and killable cleanup."""

    context = multiprocessing.get_context("spawn")
    result_fd, result_path = tempfile.mkstemp(
        prefix="sql-edu-public-case-", suffix=".pickle"
    )
    os.close(result_fd)
    process = context.Process(
        target=_case_worker_entry,
        args=(case, max_rows, result_path, worker_memory_mb),
        daemon=True,
    )
    started = False
    try:
        process.start()
        started = True
        deadline = time.monotonic() + max(0.01, float(timeout_seconds))
        while process.is_alive():
            if time.monotonic() >= deadline:
                _terminate_case_process(process)
                return _resource_timeout_result(case, "case evaluator exceeded wall-clock budget")
            time.sleep(0.01)
        process.join(timeout=0)
        try:
            with open(result_path, "rb") as handle:
                envelope = pickle.load(handle)
        except Exception as exc:
            return _resource_timeout_result(
                case,
                f"case evaluator exited without a result (exitcode={process.exitcode}): {exc}",
            )
        if envelope[0] == "ok":
            return envelope[1]
        return _resource_timeout_result(case, f"case evaluator failed: {envelope[1]}: {envelope[2]}")
    finally:
        if started:
            _terminate_case_process(process)
        try:
            os.unlink(result_path)
        except FileNotFoundError:
            pass


def evaluate_cases_isolated(
    cases: list[dict[str, Any]],
    *,
    max_rows: int,
    worker_memory_mb: int = 1536,
    worker_recycle_cases: int = 1,
) -> list[dict[str, Any]]:
    """Evaluate cases one-by-one in killable workers with bounded memory/time.

    SQL ASTs contain parent links and the witness suite retains several worlds
    during one comparison.  A fresh spawned child per case prevents object
    graphs from accumulating and, unlike ``ProcessPoolExecutor.map``, lets the
    parent terminate one pathological case without waiting for pool shutdown.
    ``worker_recycle_cases`` remains accepted for CLI compatibility; each case
    is stricter than that setting and receives a fresh child.
    """

    if not cases:
        return []
    return [
        _evaluate_case_bounded(
            case,
            max_rows=max_rows,
            worker_memory_mb=worker_memory_mb,
        )
        for case in cases
    ]


def _data_generation_status(result: dict[str, Any]) -> str:
    verdict_status = result.get("verdict_status")
    if verdict_status in {
        "INPUT_GAP",
        "ENGINE_GAP",
        "SEMANTIC_BOUNDARY",
        "KNOWN_GAP",
        "SUPPORTED_WITH_LIMITS",
    }:
        return str(verdict_status)
    if not result["executed"]:
        if result.get("error_code") == "STUDENT_SQL_PARSE_ERROR":
            return "INVALID_STUDENT_SQL"
        return "EXEC_ERROR"
    if result["observable_mismatch"]:
        return "PASS"
    if result["generation_tactics"]:
        return "TACTIC_BUT_NO_COUNTEREXAMPLE"
    return "MISSED_COUNTEREXAMPLE"


def summarize(records: list[dict[str, Any]], cases: list[dict[str, Any]], results: list[dict[str, Any]], seed: int) -> dict[str, Any]:
    by_structure: dict[str, dict[str, int]] = {}
    for structure in CATEGORIES:
        items = [result for result in results if result["structure"] == structure]
        by_structure[structure] = {
            "total": len(items),
            "strict_pass": sum(1 for item in items if item["strict_pass"]),
            "strict_fail": sum(1 for item in items if not item["strict_pass"]),
            "executed": sum(1 for item in items if item.get("executed")),
            "observable_counterexample": sum(1 for item in items if item.get("observable_mismatch")),
            "tactic_activated": sum(1 for item in items if item.get("generation_tactics")),
        }
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "seed": seed,
        "online_records_mined": len(records),
        "total": len(results),
        "strict_pass": sum(1 for result in results if result["strict_pass"]),
        "strict_fail": sum(1 for result in results if not result["strict_pass"]),
        "executed": sum(1 for result in results if result.get("executed")),
        "observable_counterexamples": sum(1 for result in results if result.get("observable_mismatch")),
        "tactic_activated": sum(1 for result in results if result.get("generation_tactics")),
        "by_structure": by_structure,
        "by_status": dict(Counter(result.get("data_generation_status") for result in results)),
        "by_verdict_status": dict(Counter(result.get("verdict_status") for result in results)),
        "by_equivalence_conclusion": dict(
            Counter(result.get("equivalence_conclusion") for result in results)
        ),
        "by_source": dict(Counter(case["source_id"] for case in cases)),
        "source_urls": sorted({case["source_url"] for case in cases}),
        "errors": dict(Counter(error for result in results for error in result.get("errors", []))),
    }


def write_outputs(
    records: list[dict[str, Any]],
    results: list[dict[str, Any]],
    summary: dict[str, Any],
    *,
    output_dir: Path = OUTPUT_DIR,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    report_json = output_dir / "online_random250_structure_generation_report.json"
    cases_jsonl = output_dir / "online_random250_structure_generation_cases.jsonl"
    report_md = output_dir / "online_random250_structure_generation_report.md"
    corpus_jsonl = output_dir / "online_random250_mined_corpus.jsonl"

    report_json.write_text(json.dumps({"summary": summary, "results": results}, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    with cases_jsonl.open("w", encoding="utf-8") as fh:
        for result in results:
            fh.write(json.dumps(result, ensure_ascii=False, default=str) + "\n")
    with corpus_jsonl.open("w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    lines = [
        "# Online Random250 Structure + Generation Report",
        "",
        "Standard SQL statements are mined from online public SQL exercise/solution sources. Student SQL is a deterministic mutation used to test whether structure differences drive useful counterexample data.",
        "",
        f"- Generated at: `{summary['generated_at']}`",
        f"- Seed: `{summary['seed']}`",
        f"- Online SQL records mined: `{summary['online_records_mined']}`",
        f"- Cases: `{summary['total']}`",
        f"- Strict pass: `{summary['strict_pass']}` (`{summary['strict_pass'] / summary['total']:.2%}`)",
        f"- Strict fail: `{summary['strict_fail']}` (`{summary['strict_fail'] / summary['total']:.2%}`)",
        f"- Executed in sandbox: `{summary['executed']}`",
        f"- Observable counterexamples: `{summary['observable_counterexamples']}`",
        f"- Tactic activated: `{summary['tactic_activated']}`",
        f"- Data-generation status: `{summary['by_status']}`",
        f"- Verdict status: `{summary['by_verdict_status']}`",
        f"- Equivalence conclusion: `{summary['by_equivalence_conclusion']}`",
        "",
        "## By Structure",
        "",
        "| structure | total | strict pass | strict fail | executed | observable counterexample | tactic activated |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for structure, stats in summary["by_structure"].items():
        lines.append(
            f"| {structure} | {stats['total']} | {stats['strict_pass']} | {stats['strict_fail']} | "
            f"{stats['executed']} | {stats['observable_counterexample']} | {stats['tactic_activated']} |"
        )
    lines.extend(["", "## Failure Examples", ""])
    for result in [item for item in results if not item["strict_pass"]][:80]:
        lines.extend([
            f"### {result['id']} ({result['structure']})",
            f"- source: {result['source']} / `{result['member']}` <{result['source_url']}>",
            f"- target: `{result['strict_target']}`",
            f"- status: `{result.get('data_generation_status')}`",
            f"- verdict: `{result.get('verdict_status')}` / `{result.get('equivalence_conclusion')}`",
            f"- error_code: `{result.get('error_code')}`",
            f"- boundary_evidence: `{result.get('boundary_evidence', {})}`",
            f"- errors: `{result.get('errors')}`",
            f"- standard: `{result['standard']}`",
            f"- student: `{result['student']}`",
            f"- schema: `{result.get('schema', '')}`",
            f"- diff_types: `{result.get('diff_types', [])}`",
            f"- tactics: `{result.get('generation_tactics', [])}`",
            f"- standard_rows_sample: `{result.get('standard_rows_sample', [])}`",
            f"- student_rows_sample: `{result.get('student_rows_sample', [])}`",
            "",
        ])
    lines.extend(["", "## Sources", ""])
    for url in summary["source_urls"]:
        lines.append(f"- <{url}>")
    report_md.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", type=int, default=SEED)
    parser.add_argument("--max-rows", type=int, default=10)
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--offline-cache-only", action="store_true")
    parser.add_argument("--print-summary", action="store_true")
    parser.add_argument(
        "--worker-memory-mb",
        type=int,
        default=1536,
        help="hard RLIMIT_AS for the single evaluation worker",
    )
    parser.add_argument(
        "--worker-recycle-cases",
        type=int,
        default=1,
        help="restart the worker after this many cases",
    )
    parser.add_argument(
        "--case-limit",
        type=int,
        default=0,
        help="lazily build and evaluate only N quota-balanced smoke cases",
    )
    parser.add_argument(
        "--in-process",
        action="store_true",
        help="debug only: disable the memory-isolated worker",
    )
    parser.add_argument(
        "--no-write",
        action="store_true",
        help="evaluate and print without replacing report files",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=OUTPUT_DIR,
        help="directory for generated report/corpus artifacts",
    )
    args = parser.parse_args()

    if not 1 <= args.max_rows <= 32:
        parser.error("--max-rows must be between 1 and 32")
    if not 256 <= args.worker_memory_mb <= 4096:
        parser.error("--worker-memory-mb must be between 256 and 4096")
    if args.worker_recycle_cases < 1:
        parser.error("--worker-recycle-cases must be at least 1")
    if args.case_limit < 0:
        parser.error("--case-limit cannot be negative")
    if args.case_limit > sum(QUOTAS.values()):
        parser.error(f"--case-limit cannot exceed {sum(QUOTAS.values())}")

    records = collect_online_queries(args.offline_cache_only, args.timeout)
    cases = build_cases(records, args.seed, total_limit=args.case_limit)
    expected_cases = args.case_limit or sum(QUOTAS.values())
    if len(cases) != expected_cases:
        raise RuntimeError(f"expected {expected_cases} cases, got {len(cases)}")
    if args.in_process:
        results = [evaluate_case(case, max_rows=args.max_rows) for case in cases]
    else:
        results = evaluate_cases_isolated(
            cases,
            max_rows=args.max_rows,
            worker_memory_mb=args.worker_memory_mb,
            worker_recycle_cases=args.worker_recycle_cases,
        )
    summary = summarize(records, cases, results, args.seed)
    if not args.no_write:
        write_outputs(records, results, summary, output_dir=args.output_dir)
    if args.print_summary:
        print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
