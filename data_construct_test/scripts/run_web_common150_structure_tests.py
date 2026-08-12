"""Web-inspired common SQL teaching holdout for structure IR and ASTDiff.

This suite intentionally does not reuse the legacy online100/frontier cases.
Cases are rewritten from common tutorial topics found in public SQL teaching
materials, with fixed-seed sampling for reproducibility.  The strict bar is
teaching-oriented: a query may parse and produce a coarse clause diff, while
still failing if the structure board lacks the expected specialized IR/ASTDiff
needed to explain a common student mistake.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "sql-edu-backend"))

from core.ast_schema import SQLStructureIR
from core.parseval_data_generator import _parse_sql, extract_ast_diffs

OUTPUT_DIR = PROJECT_ROOT / "data_construct_test" / "outputs"
SEED = 20260723

SOURCES = {
    "postgres_select": {
        "label": "PostgreSQL tutorial: querying a table",
        "url": "https://www.postgresql.org/docs/current/tutorial-select.html",
    },
    "postgres_join": {
        "label": "PostgreSQL tutorial: joins between tables",
        "url": "https://www.postgresql.org/docs/current/tutorial-join.html",
    },
    "postgres_agg": {
        "label": "PostgreSQL tutorial: aggregate functions",
        "url": "https://www.postgresql.org/docs/current/tutorial-agg.html",
    },
    "postgres_window": {
        "label": "PostgreSQL tutorial: window functions",
        "url": "https://www.postgresql.org/docs/current/tutorial-window.html",
    },
    "postgres_with": {
        "label": "PostgreSQL docs: WITH queries",
        "url": "https://www.postgresql.org/docs/current/queries-with.html",
    },
    "postgres_set": {
        "label": "PostgreSQL docs: UNION, CASE, and SELECT reference topics",
        "url": "https://www.postgresql.org/docs/current/queries-union.html",
    },
    "postgres_select_ref": {
        "label": "PostgreSQL docs: SELECT reference",
        "url": "https://www.postgresql.org/docs/current/sql-select.html",
    },
    "sqlzoo_select": {
        "label": "SQLZoo SELECT basics style",
        "url": "https://sqlzoo.net/wiki/SELECT_basics",
    },
    "sqlzoo_join": {
        "label": "SQLZoo JOIN style",
        "url": "https://sqlzoo.net/wiki/The_JOIN_operation",
    },
    "sqlzoo_nested": {
        "label": "SQLZoo nested SELECT style",
        "url": "https://sqlzoo.net/wiki/SELECT_within_SELECT_Tutorial",
    },
    "sqltutorial_case": {
        "label": "SQLTutorial CASE expression style",
        "url": "https://www.sqltutorial.org/sql-case/",
    },
    "sqltutorial_window": {
        "label": "SQLTutorial window functions style",
        "url": "https://www.sqltutorial.org/sql-window-functions/",
    },
    "w3_top": {
        "label": "W3Schools SELECT TOP / LIMIT style",
        "url": "https://www.w3schools.com/sql/sql_top.asp",
    },
    "w3_null": {
        "label": "W3Schools NULL / IN / BETWEEN / LIKE style",
        "url": "https://www.w3schools.com/sql/sql_null_values.asp",
    },
}


Case = dict[str, Any]


def case(
    id_: str,
    structure: str,
    source_key: str,
    standard: str,
    student: str,
    *,
    intent: str,
    strict_target: str,
    ir_features: list[str] | None = None,
    predicate_kinds: list[str] | None = None,
    logic_ops: list[str] | None = None,
    diff_types: list[str] | None = None,
    expect_no_diff: bool = False,
    note: str = "",
) -> Case:
    source = SOURCES[source_key]
    return {
        "id": id_,
        "structure": structure,
        "source": source["label"],
        "source_url": source["url"],
        "standard": standard,
        "student": student,
        "intent": intent,
        "strict_target": strict_target,
        "note": note,
        "expected": {
            "ir_features": ir_features or [],
            "predicate_kinds": predicate_kinds or [],
            "logic_ops": logic_ops or [],
            "diff_types": diff_types or [],
            "expect_no_diff": expect_no_diff,
        },
    }


def _supported(id_: str, structure: str, source: str, std: str, stu: str, **kw: Any) -> Case:
    return case(id_, structure, source, std, stu, intent="supported", **kw)


def _gap(id_: str, structure: str, source: str, std: str, stu: str, **kw: Any) -> Case:
    return case(id_, structure, source, std, stu, intent="strict_gap", **kw)


def _candidate_cases() -> dict[str, list[Case]]:
    pools: dict[str, list[Case]] = defaultdict(list)

    def add(c: Case) -> None:
        pools[c["structure"]].append(c)

    tables = [
        ("students", "name", "gpa", "major_id"),
        ("employees", "employee_name", "salary", "department_id"),
        ("products", "product_name", "price", "category_id"),
        ("orders", "customer_name", "total_amount", "customer_id"),
        ("books", "title", "price", "author_id"),
        ("courses", "course_name", "credits", "dept_id"),
        ("payments", "payer_name", "amount", "account_id"),
        ("tickets", "subject", "priority", "agent_id"),
    ]

    for i, (table, label, metric, fk) in enumerate(tables, 1):
        add(_supported(
            f"web_select_projection_{i}", "SELECT", "sqlzoo_select",
            f"SELECT {label}, {metric} FROM {table};",
            f"SELECT {label} FROM {table};",
            strict_target="projection column add/drop",
            ir_features=["select-basic"],
            diff_types=["projection_changed"],
        ))
        add(_gap(
            f"web_select_alias_gap_{i}", "SELECT", "postgres_select",
            f"SELECT {label} AS display_name FROM {table};",
            f"SELECT {label} FROM {table};",
            strict_target="alias_changed",
            ir_features=["select-basic"],
            diff_types=["alias_changed"],
            note="Common result-column naming exercise; current diffs are projection-level.",
        ))
        add(_gap(
            f"web_select_expression_gap_{i}", "SELECT", "postgres_select",
            f"SELECT {metric} * 2 AS doubled_value FROM {table};",
            f"SELECT {metric} + 2 AS doubled_value FROM {table};",
            strict_target="expression_operator_changed",
            ir_features=["select-basic"],
            diff_types=["expression_operator_changed"],
        ))
        add(_gap(
            f"web_select_function_arg_gap_{i}", "SELECT", "postgres_select",
            f"SELECT ROUND({metric}, 2) FROM {table};",
            f"SELECT ROUND({metric}, 0) FROM {table};",
            strict_target="function_argument_changed",
            ir_features=["select-basic"],
            diff_types=["function_argument_changed"],
        ))

    for i, (table, label, _metric, _fk) in enumerate(tables, 1):
        add(_supported(
            f"web_distinct_basic_{i}", "DISTINCT", "postgres_select",
            f"SELECT DISTINCT {label} FROM {table};",
            f"SELECT {label} FROM {table};",
            strict_target="select DISTINCT toggle",
            ir_features=["select-basic", "distinct"],
            diff_types=["distinct_changed"],
        ))
        add(_gap(
            f"web_distinct_aggregate_gap_{i}", "DISTINCT", "postgres_agg",
            f"SELECT COUNT(DISTINCT {label}) FROM {table};",
            f"SELECT COUNT({label}) FROM {table};",
            strict_target="aggregate_distinct_changed",
            ir_features=["select-basic", "aggregate"],
            diff_types=["aggregate_distinct_changed"],
        ))

    for i, (table, label, metric, _fk) in enumerate(tables, 1):
        add(_supported(
            f"web_where_missing_{i}", "WHERE", "sqlzoo_select",
            f"SELECT {label} FROM {table} WHERE {metric} > {10 * i};",
            f"SELECT {label} FROM {table};",
            strict_target="WHERE predicate missing",
            ir_features=["select-basic", "where"],
            predicate_kinds=["comparison"],
            diff_types=["where_changed", "predicate_missing"],
        ))
        add(_gap(
            f"web_where_expression_gap_{i}", "WHERE", "postgres_select",
            f"SELECT {label} FROM {table} WHERE {metric} * 2 > {100 * i};",
            f"SELECT {label} FROM {table} WHERE {metric} + 2 > {100 * i};",
            strict_target="predicate_expression_operator_changed",
            ir_features=["select-basic", "where"],
            diff_types=["predicate_expression_operator_changed"],
        ))

    operators = [(">", ">="), ("<", "<="), ("=", "<>"), (">=", ">"), ("<=", "<"), ("<>", "=")]
    for i, ((std_op, stu_op), (table, label, metric, _fk)) in enumerate(zip(operators * 2, tables * 2), 1):
        add(_supported(
            f"web_comparison_operator_{i}", "Comparison", "sqlzoo_select",
            f"SELECT {label} FROM {table} WHERE {metric} {std_op} {i * 7};",
            f"SELECT {label} FROM {table} WHERE {metric} {stu_op} {i * 7};",
            strict_target="comparison operator change",
            ir_features=["select-basic", "where"],
            predicate_kinds=["comparison"],
            diff_types=["comparison_operator_changed"],
        ))
        add(_gap(
            f"web_comparison_column_gap_{i}", "Comparison", "postgres_select",
            f"SELECT {label} FROM {table} WHERE {metric} > 50;",
            f"SELECT {label} FROM {table} WHERE {label} > 50;",
            strict_target="comparison_left_column_changed",
            ir_features=["select-basic", "where"],
            diff_types=["comparison_left_column_changed"],
        ))

    nullable = [("students", "email"), ("employees", "manager_id"), ("orders", "shipped_at"), ("books", "isbn"), ("tickets", "closed_at"), ("payments", "cleared_at")]
    for i, (table, col) in enumerate(nullable, 1):
        add(_supported(
            f"web_null_equality_{i}", "NULL", "w3_null",
            f"SELECT * FROM {table} WHERE {col} IS NULL;",
            f"SELECT * FROM {table} WHERE {col} = NULL;",
            strict_target="NULL comparison misuse",
            ir_features=["select-basic", "where"],
            predicate_kinds=["null_check"],
            diff_types=["null_equality_changed"],
        ))
        add(_gap(
            f"web_null_antijoin_gap_{i}", "NULL", "postgres_select",
            f"SELECT name FROM students WHERE major_id NOT IN (SELECT id FROM majors WHERE inactive_at IS NULL);",
            f"SELECT s.name FROM students s WHERE NOT EXISTS (SELECT 1 FROM majors m WHERE m.inactive_at IS NULL AND m.id = s.major_id);",
            strict_target="null_sensitive_antijoin_equivalence",
            ir_features=["select-basic", "where", "subquery-scalar"],
            diff_types=["null_sensitive_antijoin_equivalence"],
            note="NOT IN/NOT EXISTS is a common teaching comparison when nullable subquery values exist.",
        ))

    in_values = [("'A','B','C'", "'A','B'"), ("1,2,3", "1,2"), ("'open','new'", "'open'"), ("10,20,30", "10,30"), ("'CS','MATH'", "'CS'"), ("'paid','refund'", "'paid'")]
    for i, ((std_vals, stu_vals), (table, label, _metric, fk)) in enumerate(zip(in_values, tables), 1):
        add(_supported(
            f"web_in_list_{i}", "IN", "w3_null",
            f"SELECT {label} FROM {table} WHERE {fk} IN ({std_vals});",
            f"SELECT {label} FROM {table} WHERE {fk} IN ({stu_vals});",
            strict_target="IN list member change",
            ir_features=["select-basic", "where"],
            predicate_kinds=["in_list"],
            diff_types=["in_list_member_removed"],
        ))
        add(_gap(
            f"web_in_exists_gap_{i}", "IN", "sqlzoo_nested",
            f"SELECT {label} FROM {table} WHERE {fk} IN (SELECT id FROM departments WHERE active = 1);",
            f"SELECT t.{label} FROM {table} t WHERE EXISTS (SELECT 1 FROM departments d WHERE d.active = 1 AND d.id = t.{fk});",
            strict_target="in_exists_equivalence",
            ir_features=["select-basic", "where", "subquery-scalar"],
            diff_types=["in_exists_equivalence"],
        ))

    for i, (table, label, metric, _fk) in enumerate(tables[:6], 1):
        add(_supported(
            f"web_between_bounds_{i}", "BETWEEN", "w3_null",
            f"SELECT {label} FROM {table} WHERE {metric} BETWEEN {i * 10} AND {i * 20};",
            f"SELECT {label} FROM {table} WHERE {metric} BETWEEN {i * 10 + 1} AND {i * 20};",
            strict_target="BETWEEN bound change",
            ir_features=["select-basic", "where"],
            predicate_kinds=["between"],
            diff_types=["literal_changed"],
        ))
        add(_gap(
            f"web_between_not_gap_{i}", "BETWEEN", "postgres_select",
            f"SELECT {label} FROM {table} WHERE {metric} NOT BETWEEN {i * 10} AND {i * 20};",
            f"SELECT {label} FROM {table} WHERE {metric} < {i * 10} OR {metric} > {i * 20};",
            strict_target="between_expansion_equivalence",
            ir_features=["select-basic", "where"],
            diff_types=["between_expansion_equivalence"],
        ))

    for i, (table, label, _metric, _fk) in enumerate(tables[:6], 1):
        add(_supported(
            f"web_like_pattern_{i}", "LIKE", "w3_null",
            f"SELECT {label} FROM {table} WHERE {label} LIKE 'A%';",
            f"SELECT {label} FROM {table} WHERE {label} LIKE 'B%';",
            strict_target="LIKE pattern change",
            ir_features=["select-basic", "where"],
            predicate_kinds=["like"],
            diff_types=["literal_changed"],
        ))
        add(_gap(
            f"web_like_negation_gap_{i}", "LIKE", "postgres_select",
            f"SELECT {label} FROM {table} WHERE {label} NOT LIKE 'test%';",
            f"SELECT {label} FROM {table} WHERE NOT ({label} LIKE 'test%');",
            strict_target="like_negation_equivalence",
            ir_features=["select-basic", "where"],
            diff_types=["like_negation_equivalence"],
        ))

    for i, (table, label, metric, _fk) in enumerate(tables[:6], 1):
        add(_supported(
            f"web_logic_operator_{i}", "Logic", "sqlzoo_select",
            f"SELECT {label} FROM {table} WHERE {metric} > 10 AND {label} LIKE 'A%';",
            f"SELECT {label} FROM {table} WHERE {metric} > 10 OR {label} LIKE 'A%';",
            strict_target="AND/OR operator change",
            ir_features=["select-basic", "where"],
            predicate_kinds=["logic"],
            logic_ops=["AND"],
            diff_types=["logical_operator_changed"],
        ))
        add(_gap(
            f"web_logic_parentheses_gap_{i}", "Logic", "postgres_select",
            f"SELECT {label} FROM {table} WHERE {metric} > 10 AND ({label} LIKE 'A%' OR {label} LIKE 'B%');",
            f"SELECT {label} FROM {table} WHERE ({metric} > 10 AND {label} LIKE 'A%') OR {label} LIKE 'B%';",
            strict_target="logical_precedence_tree_changed",
            ir_features=["select-basic", "where"],
            diff_types=["logical_precedence_tree_changed"],
        ))

    joins = [
        ("students s", "majors m", "s.major_id = m.id", "s.name, m.major_name"),
        ("employees e", "departments d", "e.department_id = d.id", "e.employee_name, d.department_name"),
        ("orders o", "customers c", "o.customer_id = c.id", "o.customer_name, c.region"),
        ("books b", "authors a", "b.author_id = a.id", "b.title, a.author_name"),
        ("courses c", "departments d", "c.dept_id = d.id", "c.course_name, d.department_name"),
        ("payments p", "accounts a", "p.account_id = a.id", "p.payer_name, a.status"),
        ("tickets t", "agents a", "t.agent_id = a.id", "t.subject, a.agent_name"),
    ]
    for i, (left, right, on, proj) in enumerate(joins, 1):
        left_table = left.split()[0]
        add(_supported(
            f"web_join_missing_{i}", "JOIN", "sqlzoo_join",
            f"SELECT {proj} FROM {left} JOIN {right} ON {on};",
            f"SELECT {proj.split(',')[0]} FROM {left_table};",
            strict_target="JOIN missing",
            ir_features=["select-basic", "join-inner", "join-on"],
            diff_types=["join_missing", "join_on_changed"],
        ))
        add(_gap(
            f"web_join_implicit_equiv_gap_{i}", "JOIN", "postgres_join",
            f"SELECT {proj} FROM {left} JOIN {right} ON {on};",
            f"SELECT {proj} FROM {left}, {right} WHERE {on};",
            strict_target="implicit_explicit_join_equivalence",
            ir_features=["select-basic", "join-inner", "join-on"],
            diff_types=[],
            expect_no_diff=True,
        ))

    for i, (left, right, on, proj) in enumerate(joins[:6], 1):
        bad_on = on.replace("_id", "_code", 1) if "_id" in on else on.replace(".id", ".code", 1)
        add(_supported(
            f"web_join_on_changed_{i}", "JOIN ON", "postgres_join",
            f"SELECT {proj} FROM {left} JOIN {right} ON {on};",
            f"SELECT {proj} FROM {left} JOIN {right} ON {bad_on};",
            strict_target="JOIN ON condition change",
            ir_features=["select-basic", "join-inner", "join-on"],
            diff_types=["join_on_changed"],
        ))
        add(_gap(
            f"web_join_key_gap_{i}", "JOIN ON", "sqlzoo_join",
            f"SELECT {proj} FROM {left} JOIN {right} ON {on};",
            f"SELECT {proj} FROM {left} JOIN {right} ON {bad_on};",
            strict_target="join_key_column_changed",
            ir_features=["select-basic", "join-inner", "join-on"],
            diff_types=["join_key_column_changed"],
        ))

    for i, (table, group_col, metric) in enumerate([
        ("students", "major_id", "gpa"),
        ("employees", "department_id", "salary"),
        ("orders", "customer_id", "total_amount"),
        ("books", "author_id", "price"),
        ("courses", "dept_id", "credits"),
        ("payments", "account_id", "amount"),
        ("tickets", "agent_id", "priority"),
    ], 1):
        add(_supported(
            f"web_group_by_changed_{i}", "GROUP BY", "postgres_agg",
            f"SELECT {group_col}, COUNT(*) FROM {table} GROUP BY {group_col};",
            f"SELECT {group_col}, COUNT(*) FROM {table} GROUP BY {metric};",
            strict_target="GROUP BY expression change",
            ir_features=["select-basic", "group-by", "aggregate", "agg-count"],
            diff_types=["group_by_changed"],
        ))
        add(_gap(
            f"web_group_grain_gap_{i}", "GROUP BY", "postgres_agg",
            f"SELECT {group_col}, COUNT(*) FROM {table} GROUP BY {group_col};",
            f"SELECT {group_col}, status, COUNT(*) FROM {table} GROUP BY {group_col}, status;",
            strict_target="grouping_grain_too_fine",
            ir_features=["select-basic", "group-by", "aggregate"],
            diff_types=["grouping_grain_too_fine"],
        ))

    for i, (table, group_col, metric) in enumerate([
        ("students", "major_id", "gpa"),
        ("employees", "department_id", "salary"),
        ("orders", "customer_id", "total_amount"),
        ("books", "author_id", "price"),
        ("courses", "dept_id", "credits"),
        ("payments", "account_id", "amount"),
    ], 1):
        add(_supported(
            f"web_having_operator_{i}", "HAVING", "postgres_agg",
            f"SELECT {group_col}, AVG({metric}) FROM {table} GROUP BY {group_col} HAVING AVG({metric}) > {i * 10};",
            f"SELECT {group_col}, AVG({metric}) FROM {table} GROUP BY {group_col} HAVING AVG({metric}) >= {i * 10};",
            strict_target="HAVING comparison operator change",
            ir_features=["select-basic", "group-by", "having", "aggregate"],
            predicate_kinds=["comparison"],
            diff_types=["comparison_operator_changed"],
        ))
        add(_gap(
            f"web_having_where_gap_{i}", "HAVING", "postgres_agg",
            f"SELECT {group_col}, AVG({metric}) FROM {table} GROUP BY {group_col} HAVING AVG({metric}) > {i * 10};",
            f"SELECT {group_col}, AVG({metric}) FROM {table} WHERE AVG({metric}) > {i * 10} GROUP BY {group_col};",
            strict_target="aggregate_condition_in_where",
            ir_features=["select-basic", "group-by", "having", "aggregate"],
            diff_types=["aggregate_condition_in_where"],
        ))

    for i, (table, group_col, metric) in enumerate([
        ("students", "major_id", "gpa"),
        ("employees", "department_id", "salary"),
        ("orders", "customer_id", "total_amount"),
        ("books", "author_id", "price"),
        ("courses", "dept_id", "credits"),
        ("payments", "account_id", "amount"),
        ("tickets", "agent_id", "priority"),
    ], 1):
        add(_supported(
            f"web_aggregate_function_{i}", "Aggregate", "postgres_agg",
            f"SELECT {group_col}, MAX({metric}) FROM {table} GROUP BY {group_col};",
            f"SELECT {group_col}, MIN({metric}) FROM {table} GROUP BY {group_col};",
            strict_target="aggregate function change",
            ir_features=["select-basic", "group-by", "aggregate"],
            diff_types=["aggregate_function_changed"],
        ))
        add(_gap(
            f"web_aggregate_arg_gap_{i}", "Aggregate", "postgres_agg",
            f"SELECT AVG({metric}) FROM {table};",
            f"SELECT AVG({group_col}) FROM {table};",
            strict_target="aggregate_argument_changed",
            ir_features=["select-basic", "aggregate"],
            diff_types=["aggregate_argument_changed"],
        ))

    for i, (table, label, metric, _fk) in enumerate(tables[:6], 1):
        add(_supported(
            f"web_order_direction_{i}", "ORDER BY", "postgres_select",
            f"SELECT {label}, {metric} FROM {table} ORDER BY {metric} DESC;",
            f"SELECT {label}, {metric} FROM {table} ORDER BY {metric} ASC;",
            strict_target="ORDER BY direction change",
            ir_features=["select-basic", "order-by"],
            diff_types=["order_by_changed"],
        ))
        add(_gap(
            f"web_order_tiebreaker_gap_{i}", "ORDER BY", "postgres_select",
            f"SELECT {label}, {metric} FROM {table} ORDER BY {metric} DESC, {label} ASC;",
            f"SELECT {label}, {metric} FROM {table} ORDER BY {metric} DESC;",
            strict_target="order_by_tiebreaker_missing",
            ir_features=["select-basic", "order-by"],
            diff_types=["order_by_tiebreaker_missing"],
        ))

    for i, (table, label, metric, _fk) in enumerate(tables[:6], 1):
        add(_supported(
            f"web_limit_count_{i}", "LIMIT / OFFSET", "w3_top",
            f"SELECT {label} FROM {table} ORDER BY {metric} DESC LIMIT {i + 2} OFFSET {i};",
            f"SELECT {label} FROM {table} ORDER BY {metric} DESC LIMIT {i + 3} OFFSET {i};",
            strict_target="LIMIT count change",
            ir_features=["select-basic", "order-by", "limit"],
            diff_types=["limit_changed"],
        ))
        add(_gap(
            f"web_limit_order_gap_{i}", "LIMIT / OFFSET", "postgres_select_ref",
            f"SELECT {label} FROM {table} ORDER BY {metric} DESC LIMIT {i + 2};",
            f"SELECT {label} FROM {table} LIMIT {i + 2};",
            strict_target="top_n_ordering_missing",
            ir_features=["select-basic", "order-by", "limit"],
            diff_types=["top_n_ordering_missing"],
        ))

    for i, (table, label, metric, fk) in enumerate(tables[:6], 1):
        add(_supported(
            f"web_subquery_literal_{i}", "Subquery", "sqlzoo_nested",
            f"SELECT {label} FROM {table} WHERE {metric} > (SELECT AVG({metric}) FROM {table});",
            f"SELECT {label} FROM {table} WHERE {metric} >= (SELECT AVG({metric}) FROM {table});",
            strict_target="subquery comparison operator change",
            ir_features=["select-basic", "where", "subquery-scalar", "aggregate"],
            diff_types=["comparison_operator_changed"],
        ))
        add(_gap(
            f"web_subquery_join_equiv_gap_{i}", "Subquery", "sqlzoo_nested",
            f"SELECT {label} FROM {table} WHERE {fk} IN (SELECT id FROM departments WHERE active = 1);",
            f"SELECT t.{label} FROM {table} t JOIN departments d ON t.{fk} = d.id WHERE d.active = 1;",
            strict_target="subquery_join_equivalence",
            ir_features=["select-basic", "where", "subquery-scalar"],
            expect_no_diff=True,
        ))

    for i, (table, label, metric, fk) in enumerate(tables[:6], 1):
        add(_supported(
            f"web_correlated_predicate_{i}", "Correlated Subquery", "sqlzoo_nested",
            f"SELECT t.{label} FROM {table} t WHERE {metric} > (SELECT AVG(x.{metric}) FROM {table} x WHERE x.{fk} = t.{fk});",
            f"SELECT t.{label} FROM {table} t WHERE {metric} >= (SELECT AVG(x.{metric}) FROM {table} x WHERE x.{fk} = t.{fk});",
            strict_target="correlated subquery predicate change",
            ir_features=["select-basic", "where", "subquery-correlated", "aggregate"],
            diff_types=["comparison_operator_changed", "correlated_predicate_changed"],
        ))
        add(_gap(
            f"web_correlated_anti_join_gap_{i}", "Correlated Subquery", "postgres_join",
            f"SELECT s.name FROM students s WHERE NOT EXISTS (SELECT 1 FROM enrollments e WHERE e.student_id = s.id);",
            f"SELECT s.name FROM students s LEFT JOIN enrollments e ON e.student_id = s.id WHERE e.student_id IS NULL;",
            strict_target="not_exists_left_join_equivalence",
            ir_features=["select-basic", "where", "subquery-correlated"],
            expect_no_diff=True,
        ))

    for i, (table, label, metric, _fk) in enumerate(tables[:6], 1):
        add(_supported(
            f"web_cte_predicate_{i}", "CTE", "postgres_with",
            f"WITH high_value AS (SELECT {label}, {metric} FROM {table} WHERE {metric} > {i * 10}) SELECT {label} FROM high_value;",
            f"WITH high_value AS (SELECT {label}, {metric} FROM {table} WHERE {metric} > {i * 20}) SELECT {label} FROM high_value;",
            strict_target="CTE body predicate change",
            ir_features=["select-basic", "cte"],
            diff_types=["cte_changed", "literal_changed"],
        ))
        add(_gap(
            f"web_cte_inline_equiv_gap_{i}", "CTE", "postgres_with",
            f"WITH high_value AS (SELECT {label}, {metric} FROM {table} WHERE {metric} > {i * 10}) SELECT {label} FROM high_value;",
            f"SELECT {label} FROM {table} WHERE {metric} > {i * 10};",
            strict_target="cte_inline_equivalence",
            ir_features=["select-basic", "cte"],
            expect_no_diff=True,
        ))

    for i in range(1, 7):
        add(_supported(
            f"web_recursive_stop_{i}", "Recursive CTE", "postgres_with",
            f"WITH RECURSIVE nums(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM nums WHERE n < {i + 5}) SELECT SUM(n) FROM nums;",
            f"WITH RECURSIVE nums(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM nums WHERE n < {i + 6}) SELECT SUM(n) FROM nums;",
            strict_target="recursive CTE changed",
            ir_features=["select-basic", "cte-recursive", "union", "aggregate"],
            diff_types=["recursive_cte_changed", "literal_changed"],
        ))
        add(_gap(
            f"web_recursive_specific_gap_{i}", "Recursive CTE", "postgres_with",
            f"WITH RECURSIVE nums(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM nums WHERE n < {i + 5}) SELECT SUM(n) FROM nums;",
            f"WITH RECURSIVE nums(n) AS (SELECT 1 UNION ALL SELECT n + 2 FROM nums WHERE n < {i + 5}) SELECT SUM(n) FROM nums;",
            strict_target="recursive_step_expression_changed",
            ir_features=["select-basic", "cte-recursive", "union", "aggregate"],
            diff_types=["recursive_step_expression_changed"],
        ))

    for i, (table, label, metric, _fk) in enumerate(tables[:6], 1):
        add(_supported(
            f"web_set_operator_{i}", "Set Operation", "postgres_set",
            f"SELECT {label} FROM {table} WHERE {metric} > {i * 10} UNION SELECT {label} FROM {table} WHERE {metric} < {i};",
            f"SELECT {label} FROM {table} WHERE {metric} > {i * 10} INTERSECT SELECT {label} FROM {table} WHERE {metric} < {i};",
            strict_target="set operator change",
            ir_features=["select-basic", "union"],
            diff_types=["set_operator_changed"],
        ))
        add(_gap(
            f"web_set_all_gap_{i}", "Set Operation", "postgres_set",
            f"SELECT {label} FROM {table} UNION ALL SELECT {label} FROM {table};",
            f"SELECT {label} FROM {table} UNION SELECT {label} FROM {table};",
            strict_target="set_all_modifier_changed",
            ir_features=["select-basic", "union"],
            diff_types=["set_all_modifier_changed"],
        ))

    for i, (table, label, metric, _fk) in enumerate(tables[:7], 1):
        add(_supported(
            f"web_case_changed_{i}", "CASE", "sqltutorial_case",
            f"SELECT {label}, CASE WHEN {metric} >= {i * 10} THEN 'high' ELSE 'low' END AS band FROM {table};",
            f"SELECT {label}, CASE WHEN {metric} > {i * 10} THEN 'high' ELSE 'low' END AS band FROM {table};",
            strict_target="CASE expression change",
            ir_features=["select-basic", "case"],
            diff_types=["case_changed"],
        ))
        add(_gap(
            f"web_case_else_gap_{i}", "CASE", "sqltutorial_case",
            f"SELECT {label}, CASE WHEN {metric} >= {i * 10} THEN 'high' ELSE 'low' END AS band FROM {table};",
            f"SELECT {label}, CASE WHEN {metric} >= {i * 10} THEN 'high' END AS band FROM {table};",
            strict_target="case_else_missing",
            ir_features=["select-basic", "case"],
            diff_types=["case_else_missing"],
        ))

    for i, (table, label, metric, fk) in enumerate(tables[:7], 1):
        add(_supported(
            f"web_window_partition_{i}", "Window", "postgres_window",
            f"SELECT {label}, ROW_NUMBER() OVER (PARTITION BY {fk} ORDER BY {metric} DESC) AS rn FROM {table};",
            f"SELECT {label}, ROW_NUMBER() OVER (ORDER BY {metric} DESC) AS rn FROM {table};",
            strict_target="window OVER change",
            ir_features=["select-basic", "window-row-number"],
            diff_types=["window_over_changed"],
        ))
        add(_gap(
            f"web_window_function_gap_{i}", "Window", "sqltutorial_window",
            f"SELECT {label}, RANK() OVER (PARTITION BY {fk} ORDER BY {metric} DESC) AS rnk FROM {table};",
            f"SELECT {label}, ROW_NUMBER() OVER (PARTITION BY {fk} ORDER BY {metric} DESC) AS rnk FROM {table};",
            strict_target="window_function_changed",
            ir_features=["select-basic", "window-row-number"],
            diff_types=["window_function_changed"],
        ))

    for i, (table, label, metric, _fk) in enumerate(tables[:6], 1):
        add(_supported(
            f"web_dialect_fetch_limit_{i}", "Dialect Boundary", "w3_top",
            f"SELECT {label} FROM {table} ORDER BY {metric} DESC FETCH FIRST {i + 2} ROWS ONLY;",
            f"SELECT {label} FROM {table} ORDER BY {metric} DESC LIMIT {i + 2};",
            strict_target="FETCH FIRST/LIMIT equivalent",
            ir_features=["select-basic", "order-by", "limit"],
            expect_no_diff=True,
        ))
        add(_gap(
            f"web_dialect_limit_comma_gap_{i}", "Dialect Boundary", "postgres_select_ref",
            f"SELECT {label} FROM {table} ORDER BY {metric} DESC LIMIT {i}, {i + 2};",
            f"SELECT {label} FROM {table} ORDER BY {metric} DESC LIMIT {i + 2} OFFSET {i};",
            strict_target="mysql_limit_comma_equivalence",
            ir_features=["select-basic", "order-by", "limit"],
            expect_no_diff=True,
        ))

    return dict(pools)


QUOTAS = {
    "SELECT": 7,
    "DISTINCT": 6,
    "WHERE": 6,
    "Comparison": 6,
    "NULL": 6,
    "IN": 6,
    "BETWEEN": 6,
    "LIKE": 6,
    "Logic": 6,
    "JOIN": 7,
    "JOIN ON": 6,
    "GROUP BY": 7,
    "HAVING": 6,
    "Aggregate": 7,
    "ORDER BY": 6,
    "LIMIT / OFFSET": 6,
    "Subquery": 6,
    "Correlated Subquery": 6,
    "CTE": 6,
    "Recursive CTE": 6,
    "Set Operation": 6,
    "CASE": 7,
    "Window": 7,
    "Dialect Boundary": 6,
}


def build_cases(seed: int = SEED) -> list[Case]:
    rng = random.Random(seed)
    pools = _candidate_cases()
    selected: list[Case] = []
    for structure, quota in QUOTAS.items():
        pool = pools[structure]
        supported = [item for item in pool if item["intent"] == "supported"]
        gaps = [item for item in pool if item["intent"] == "strict_gap"]
        gap_quota = max(1, quota // 3)
        supported_quota = quota - gap_quota
        if len(supported) < supported_quota or len(gaps) < gap_quota:
            raise RuntimeError(f"not enough candidates for {structure}")
        selected.extend(rng.sample(supported, supported_quota))
        selected.extend(rng.sample(gaps, gap_quota))
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


def evaluate_case(item: Case) -> dict[str, Any]:
    expected = item["expected"]
    result: dict[str, Any] = {**item}
    errors: list[str] = []
    missing: dict[str, Any] = {}

    std_ast = _parse_sql(item["standard"])
    stu_ast = _parse_sql(item["student"])
    result["standard_parse_ok"] = std_ast is not None
    result["student_parse_ok"] = stu_ast is not None
    if std_ast is None or stu_ast is None:
        errors.append("parse_failed")
        result["strict_pass"] = False
        result["errors"] = errors
        return result

    std_ir = SQLStructureIR.from_ast(std_ast)
    result["standard_ir"] = std_ir.to_dict()
    features = set(std_ir.feature_kps())
    predicate_kinds = {str(item.get("kind")) for item in std_ir.predicate_ir}
    logic_ops = {str(item.get("operator")) for item in std_ir.predicate_ir if item.get("kind") == "logic"}

    missing_features = sorted(set(expected["ir_features"]) - features)
    missing_predicates = sorted(set(expected["predicate_kinds"]) - predicate_kinds)
    missing_logic = sorted(set(expected["logic_ops"]) - logic_ops)
    if missing_features:
        missing["ir_features"] = missing_features
    if missing_predicates:
        missing["predicate_kinds"] = missing_predicates
    if missing_logic:
        missing["logic_ops"] = missing_logic

    try:
        diffs = extract_ast_diffs(item["standard"], item["student"])
        diff_types = [diff.diff_type for diff in diffs]
        result["ast_diff_graph"] = [_diff_dict(diff) for diff in diffs]
    except Exception as exc:
        errors.append(f"diff_exception:{type(exc).__name__}:{exc}")
        diffs = []
        diff_types = []
        result["ast_diff_graph"] = []

    result["diff_types"] = diff_types
    if expected["expect_no_diff"]:
        if diff_types:
            errors.append("expected_no_diff_but_diff_found")
    else:
        missing_diff_types = sorted(set(expected["diff_types"]) - set(diff_types))
        if missing_diff_types:
            missing["diff_types"] = missing_diff_types

    if missing:
        errors.append("missing_expected_structure")
    result["missing"] = missing
    result["errors"] = errors
    result["strict_pass"] = not errors
    return result


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    by_structure: dict[str, dict[str, int]] = {}
    for structure, items in defaultdict(list, {
        structure: [r for r in results if r["structure"] == structure]
        for structure in sorted({r["structure"] for r in results})
    }).items():
        by_structure[structure] = {
            "total": len(items),
            "strict_pass": sum(1 for r in items if r["strict_pass"]),
            "strict_fail": sum(1 for r in items if not r["strict_pass"]),
            "supported_intent": sum(1 for r in items if r["intent"] == "supported"),
            "gap_intent": sum(1 for r in items if r["intent"] == "strict_gap"),
        }

    missing_counter = Counter()
    for result in results:
        for key, values in result.get("missing", {}).items():
            for value in values:
                missing_counter[f"{key}:{value}"] += 1
        for error in result.get("errors", []):
            if error != "missing_expected_structure":
                missing_counter[f"error:{error}"] += 1

    return {
        "seed": SEED,
        "total": len(results),
        "strict_pass": sum(1 for r in results if r["strict_pass"]),
        "strict_fail": sum(1 for r in results if not r["strict_pass"]),
        "by_structure": by_structure,
        "missing_counter": dict(missing_counter.most_common()),
        "source_urls": sorted({r["source_url"] for r in results}),
    }


def write_outputs(results: list[dict[str, Any]], summary: dict[str, Any]) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    report_json = OUTPUT_DIR / "web_common150_structure_report.json"
    cases_jsonl = OUTPUT_DIR / "web_common150_structure_cases.jsonl"
    report_md = OUTPUT_DIR / "web_common150_structure_report.md"

    report_json.write_text(json.dumps({"summary": summary, "results": results}, ensure_ascii=False, indent=2), encoding="utf-8")
    with cases_jsonl.open("w", encoding="utf-8") as fh:
        for result in results:
            fh.write(json.dumps(result, ensure_ascii=False) + "\n")

    lines = [
        "# Web Common150 Structure Report",
        "",
        "New fixed-seed web-inspired teaching holdout. Legacy online100/frontier49 samples are not reused.",
        "",
        f"- Seed: `{summary['seed']}`",
        f"- Total: `{summary['total']}`",
        f"- Strict pass: `{summary['strict_pass']}` (`{summary['strict_pass'] / summary['total']:.2%}`)",
        f"- Strict fail: `{summary['strict_fail']}` (`{summary['strict_fail'] / summary['total']:.2%}`)",
        "",
        "## By Structure",
        "",
        "| structure | total | strict pass | strict fail | supported-intent | gap-intent |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for structure, stats in summary["by_structure"].items():
        lines.append(
            f"| {structure} | {stats['total']} | {stats['strict_pass']} | {stats['strict_fail']} | "
            f"{stats['supported_intent']} | {stats['gap_intent']} |"
        )

    lines.extend(["", "## Top Missing / Error Signals", ""])
    for key, count in list(summary["missing_counter"].items())[:40]:
        lines.append(f"- `{key}`: {count}")

    pass_examples = [r for r in results if r["strict_pass"]][:24]
    fail_examples = [r for r in results if not r["strict_pass"]][:40]

    lines.extend(["", "## Passing Examples", ""])
    for r in pass_examples:
        lines.extend([
            f"### {r['id']} ({r['structure']})",
            f"- source: {r['source']} <{r['source_url']}>",
            f"- target: `{r['strict_target']}`",
            f"- standard: `{r['standard']}`",
            f"- student: `{r['student']}`",
            f"- diff_types: `{', '.join(r['diff_types'])}`",
            "",
        ])

    lines.extend(["", "## Failing Examples", ""])
    for r in fail_examples:
        lines.extend([
            f"### {r['id']} ({r['structure']})",
            f"- source: {r['source']} <{r['source_url']}>",
            f"- target: `{r['strict_target']}`",
            f"- standard: `{r['standard']}`",
            f"- student: `{r['student']}`",
            f"- actual_diff_types: `{', '.join(r['diff_types'])}`",
            f"- missing/errors: `{json.dumps({'missing': r.get('missing'), 'errors': r.get('errors')}, ensure_ascii=False)}`",
            "",
        ])

    lines.extend(["", "## Sources", ""])
    for url in summary["source_urls"]:
        lines.append(f"- <{url}>")

    report_md.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", type=int, default=SEED)
    parser.add_argument("--print-summary", action="store_true")
    args = parser.parse_args()

    cases = build_cases(args.seed)
    if len(cases) != 150:
        raise RuntimeError(f"expected 150 cases, got {len(cases)}")
    results = [evaluate_case(item) for item in cases]
    summary = summarize(results)
    write_outputs(results, summary)
    if args.print_summary:
        print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
