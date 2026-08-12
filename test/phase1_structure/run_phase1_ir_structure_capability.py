"""Phase 1 IR structure capability benchmark.

This script evaluates only structure recognition. It does not run dynamic data
generation, sandbox equivalence, or mutation tests.
"""

from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import sqlglot
from sqlglot import ErrorLevel, exp

def _find_project_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "sql-edu-backend").exists() and (parent / "data_construct_test").exists():
            return parent
    raise RuntimeError("Cannot locate project root from test script path")


PROJECT_ROOT = _find_project_root()
BACKEND_ROOT = PROJECT_ROOT / "sql-edu-backend"
OUTPUT_DIR = Path(__file__).resolve().parent / "outputs"
sys.path.append(str(BACKEND_ROOT))

from core.ast_schema import SQLStructureIR


def _case(
    case_id: str,
    category: str,
    sql: str,
    checks: list[dict[str, Any]],
    *,
    representation: str = "first_class",
    dialect_hint: str | None = None,
    note: str = "",
) -> dict[str, Any]:
    return {
        "id": case_id,
        "category": category,
        "sql": sql,
        "checks": checks,
        "representation": representation,
        "dialect_hint": dialect_hint,
        "note": note,
    }


def build_cases() -> list[dict[str, Any]]:
    return [
        _case(
            "select_projection_alias_star_expression",
            "SELECT",
            "SELECT *, name AS student_name, age + 1 AS age_next FROM student",
            [
                {"type": "field_count_at_least", "field": "projection", "value": 3},
                {"type": "field_contains", "field": "projection", "needles": ["*", "name AS student_name", "age + 1 AS age_next"]},
            ],
        ),
        _case(
            "distinct_top_level_and_count_distinct",
            "DISTINCT",
            "SELECT DISTINCT dept, COUNT(DISTINCT id) AS cnt FROM student GROUP BY dept",
            [
                {"type": "field_equals", "field": "distinct", "value": True},
                {"type": "aggregate_functions_include", "values": ["COUNT"]},
                {"type": "aggregate_distinct_include", "function": "COUNT", "arg": "id"},
            ],
        ),
        _case(
            "where_basic_and_compound_predicates",
            "WHERE",
            "SELECT name FROM student WHERE age > 18 AND dept = 'CS'",
            [
                {"type": "field_count_at_least", "field": "where_predicates", "value": 1},
                {"type": "predicate_contexts_include", "values": ["WHERE"]},
                {"type": "predicate_operators_include", "values": [">", "="]},
            ],
        ),
        _case(
            "comparison_all_common_operators",
            "Comparison",
            "SELECT * FROM t WHERE a = 1 AND b <> 2 AND c < 3 AND d <= 4 AND e > 5 AND f >= 6 AND g != 7",
            [
                {"type": "predicate_kinds_include", "values": ["comparison"]},
                {"type": "predicate_operators_include", "values": ["=", "<>", "<", "<=", ">", ">="]},
            ],
        ),
        _case(
            "null_predicates",
            "NULL",
            "SELECT * FROM student WHERE name IS NULL OR email IS NOT NULL OR grade = NULL",
            [
                {"type": "predicate_kinds_include", "values": ["null_check", "null_comparison"]},
                {"type": "predicate_operators_include", "values": ["IS NULL", "IS NOT NULL", "="]},
            ],
        ),
        _case(
            "in_between_like_predicates",
            "IN/BETWEEN/LIKE",
            "SELECT * FROM course WHERE dept IN ('CS', 'Math') AND credits BETWEEN 2 AND 4 AND title LIKE 'Intro%' AND code NOT LIKE 'X_%'",
            [
                {"type": "predicate_kinds_include", "values": ["in_list", "between", "like"]},
                {"type": "predicate_operators_include", "values": ["IN", "BETWEEN", "LIKE", "NOT LIKE"]},
            ],
        ),
        _case(
            "logic_and_or_not_parentheses",
            "Logic",
            "SELECT * FROM t WHERE NOT (a = 1 OR b = 2) AND c = 3",
            [
                {"type": "logic_operators_include", "values": ["AND", "OR", "NOT"]},
            ],
        ),
        _case(
            "join_common_types",
            "JOIN",
            "SELECT * FROM a JOIN b ON a.id = b.a_id LEFT JOIN c ON b.id = c.b_id RIGHT JOIN d ON c.id = d.c_id FULL JOIN e ON d.id = e.d_id CROSS JOIN f",
            [
                {"type": "field_count_at_least", "field": "joins", "value": 5},
                {"type": "join_types_include", "values": ["INNER", "LEFT", "RIGHT", "FULL", "CROSS"]},
            ],
        ),
        _case(
            "join_self_join_aliases",
            "JOIN",
            "SELECT e1.name, e2.name FROM employee e1 JOIN employee e2 ON e1.manager_id = e2.id",
            [
                {"type": "field_count_at_least", "field": "joins", "value": 1},
                {"type": "field_contains", "field": "joins", "needles": ["employee", "manager_id", "e2.id"]},
            ],
        ),
        _case(
            "join_on_single_multi_nonequi_conditions",
            "JOIN ON",
            "SELECT * FROM a JOIN b ON a.id = b.a_id AND b.score > 10",
            [
                {"type": "field_count_at_least", "field": "joins", "value": 1},
                {"type": "join_conditions_contain", "needles": ["a.id = b.a_id", "b.score > 10"]},
            ],
        ),
        _case(
            "group_by_single_multi_expression",
            "GROUP BY",
            "SELECT dept, year, COUNT(*) FROM takes GROUP BY dept, year, credits + 1",
            [
                {"type": "field_count_at_least", "field": "group_by", "value": 3},
                {"type": "field_contains", "field": "group_by", "needles": ["dept", "year", "credits + 1"]},
            ],
        ),
        _case(
            "having_aggregate_predicate",
            "HAVING",
            "SELECT dept, COUNT(*) FROM student GROUP BY dept HAVING COUNT(*) > 3",
            [
                {"type": "field_count_at_least", "field": "having_predicates", "value": 1},
                {"type": "predicate_contexts_include", "values": ["HAVING"]},
                {"type": "predicate_operators_include", "values": [">"]},
                {"type": "aggregate_functions_include", "values": ["COUNT"]},
            ],
        ),
        _case(
            "having_without_group_by",
            "HAVING",
            "SELECT COUNT(*) AS cnt FROM student HAVING COUNT(*) > 0",
            [
                {"type": "field_count_at_least", "field": "having_predicates", "value": 1},
                {"type": "predicate_contexts_include", "values": ["HAVING"]},
                {"type": "predicate_operators_include", "values": [">"]},
                {"type": "aggregate_functions_include", "values": ["COUNT"]},
            ],
        ),
        _case(
            "aggregate_common_functions",
            "Aggregate",
            "SELECT COUNT(*), SUM(salary), AVG(salary), MIN(salary), MAX(salary), COUNT(DISTINCT dept) FROM instructor",
            [
                {"type": "aggregate_functions_include", "values": ["COUNT", "SUM", "AVG", "MIN", "MAX"]},
                {"type": "aggregate_distinct_include", "function": "COUNT", "arg": "dept"},
            ],
        ),
        _case(
            "order_by_direction_multi_expression_alias_ordinal",
            "ORDER BY",
            "SELECT name, salary * 2 AS score FROM instructor ORDER BY name ASC, score DESC, salary + 1, 1",
            [
                {"type": "field_count_at_least", "field": "order_by", "value": 4},
                {"type": "order_directions_include", "values": ["ASC", "DESC"]},
                {"type": "field_contains", "field": "order_by", "needles": ["name", "score", "salary + 1", "1"]},
            ],
        ),
        _case(
            "limit_offset_sqlite",
            "LIMIT/OFFSET",
            "SELECT name FROM student ORDER BY id LIMIT 5 OFFSET 2",
            [
                {"type": "limit_keys_include", "keys": ["limit", "offset"]},
                {"type": "field_contains", "field": "limit_offset", "needles": ["5", "2"]},
            ],
        ),
        _case(
            "limit_mysql_offset_count",
            "LIMIT/OFFSET",
            "SELECT name FROM student LIMIT 2, 5",
            [
                {"type": "limit_keys_include", "keys": ["limit", "offset"]},
                {"type": "field_contains", "field": "limit_offset", "needles": ["5", "2"]},
            ],
            dialect_hint="mysql",
        ),
        _case(
            "limit_tsql_top",
            "LIMIT/OFFSET",
            "SELECT TOP 5 name FROM student",
            [
                {"type": "limit_keys_include", "keys": ["limit"]},
                {"type": "field_contains", "field": "limit_offset", "needles": ["5"]},
            ],
            dialect_hint="tsql",
        ),
        _case(
            "subquery_in_scalar_exists",
            "Subquery",
            "SELECT name FROM student s WHERE s.id IN (SELECT t.id FROM takes t) AND s.age > (SELECT AVG(age) FROM student) AND EXISTS (SELECT 1 FROM advisor a WHERE a.s_id = s.id)",
            [
                {"type": "field_count_at_least", "field": "subqueries", "value": 3},
                {"type": "field_contains", "field": "subqueries", "needles": ["SELECT t.id", "AVG(age)", "EXISTS"]},
            ],
        ),
        _case(
            "correlated_subquery_outer_column_reference",
            "Correlated Subquery",
            "SELECT s.name FROM student s WHERE EXISTS (SELECT 1 FROM advisor a WHERE a.s_id = s.id)",
            [
                {"type": "subquery_correlated_count_at_least", "value": 1},
            ],
        ),
        _case(
            "cte_multiple_and_dependent",
            "CTE",
            "WITH base AS (SELECT id, dept FROM student), cs AS (SELECT id FROM base WHERE dept = 'CS') SELECT id FROM cs",
            [
                {"type": "field_count_at_least", "field": "ctes", "value": 2},
                {"type": "field_contains", "field": "ctes", "needles": ["base", "cs", "dept = 'CS'"]},
            ],
        ),
        _case(
            "cte_recursive",
            "Recursive CTE",
            "WITH RECURSIVE nums(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM nums WHERE n < 5) SELECT n FROM nums",
            [
                {"type": "cte_recursive_count_at_least", "value": 1},
                {"type": "field_contains", "field": "ctes", "needles": ["nums"]},
            ],
        ),
        _case(
            "set_operation_union_intersect_except",
            "Set Operation",
            "SELECT id FROM a UNION SELECT id FROM b INTERSECT SELECT id FROM c EXCEPT SELECT id FROM d",
            [
                {"type": "set_operations_include", "values": ["UNION", "INTERSECT", "EXCEPT"]},
            ],
        ),
        _case(
            "case_simple_and_searched",
            "CASE",
            "SELECT CASE status WHEN 'A' THEN 1 ELSE 0 END AS simple_case, CASE WHEN score >= 60 THEN 'pass' ELSE 'fail' END AS searched_case FROM exam",
            [
                {"type": "field_count_at_least", "field": "case_branches", "value": 2},
                {"type": "field_contains", "field": "case_branches", "needles": ["CASE", "score >= 60"]},
            ],
        ),
        _case(
            "window_rank_aggregate_frame",
            "Window",
            "SELECT ROW_NUMBER() OVER (PARTITION BY dept ORDER BY salary DESC) AS rn, RANK() OVER (ORDER BY salary DESC) AS rnk, DENSE_RANK() OVER (ORDER BY salary DESC) AS dr, SUM(salary) OVER (PARTITION BY dept ORDER BY salary ROWS BETWEEN 1 PRECEDING AND CURRENT ROW) AS rolling_sum FROM instructor",
            [
                {"type": "field_count_at_least", "field": "window_functions", "value": 4},
                {"type": "field_contains", "field": "window_functions", "needles": ["ROW_NUMBER", "RANK", "DENSE_RANK", "SUM", "ROWS BETWEEN"]},
            ],
        ),
        _case(
            "select_table_star",
            "SELECT",
            "SELECT s.* FROM student s",
            [
                {"type": "field_contains", "field": "projection", "needles": ["s.*"]},
                {"type": "field_contains", "field": "table_references", "needles": ["student", "s"]},
            ],
        ),
        _case(
            "select_function_and_cast_projection",
            "SELECT",
            "SELECT LOWER(name) AS lower_name, CAST(age AS INTEGER) AS age_i FROM student",
            [
                {"type": "field_contains", "field": "projection", "needles": ["LOWER", "CAST"]},
                {"type": "field_contains", "field": "expression_ir", "needles": ["function", "cast"]},
            ],
        ),
        _case(
            "select_scalar_subquery_projection",
            "SELECT",
            "SELECT name, (SELECT COUNT(*) FROM takes) AS total_takes FROM student",
            [
                {"type": "field_count_at_least", "field": "subqueries", "value": 1},
                {"type": "aggregate_functions_include", "values": ["COUNT"]},
            ],
        ),
        _case(
            "distinct_multi_projection",
            "DISTINCT",
            "SELECT DISTINCT dept, year FROM takes",
            [
                {"type": "field_equals", "field": "distinct", "value": True},
                {"type": "field_contains", "field": "projection", "needles": ["dept", "year"]},
            ],
        ),
        _case(
            "comparison_null_safe_operators",
            "Comparison",
            "SELECT * FROM t WHERE a IS DISTINCT FROM b OR c IS NOT DISTINCT FROM d",
            [
                {"type": "predicate_operators_include", "values": ["IS DISTINCT FROM", "IS NOT DISTINCT FROM"]},
                {"type": "logic_operators_include", "values": ["OR"]},
            ],
        ),
        _case(
            "null_coalesce_nullif_functions",
            "NULL",
            "SELECT COALESCE(score, 0), NULLIF(status, 'X') FROM exam",
            [
                {"type": "field_contains", "field": "projection", "needles": ["COALESCE", "NULLIF"]},
                {"type": "field_contains", "field": "expression_ir", "needles": ["COALESCE", "NULLIF"]},
            ],
        ),
        _case(
            "predicate_not_in_list",
            "IN/BETWEEN/LIKE",
            "SELECT * FROM student WHERE dept NOT IN ('CS', 'Math')",
            [
                {"type": "predicate_kinds_include", "values": ["in_list"]},
                {"type": "predicate_operators_include", "values": ["NOT IN"]},
            ],
        ),
        _case(
            "predicate_in_subquery",
            "IN/BETWEEN/LIKE",
            "SELECT name FROM student WHERE id IN (SELECT s_id FROM advisor)",
            [
                {"type": "predicate_kinds_include", "values": ["in_subquery"]},
                {"type": "field_count_at_least", "field": "subqueries", "value": 1},
            ],
        ),
        _case(
            "predicate_not_between",
            "IN/BETWEEN/LIKE",
            "SELECT * FROM course WHERE credits NOT BETWEEN 2 AND 4",
            [
                {"type": "predicate_kinds_include", "values": ["between"]},
                {"type": "predicate_operators_include", "values": ["NOT BETWEEN"]},
            ],
        ),
        _case(
            "predicate_like_escape",
            "IN/BETWEEN/LIKE",
            "SELECT * FROM course WHERE title LIKE 'A!_%' ESCAPE '!'",
            [
                {"type": "predicate_kinds_include", "values": ["like"]},
                {"type": "field_contains", "field": "where_predicates", "needles": ["ESCAPE"]},
            ],
        ),
        _case(
            "logic_demorgan_shape",
            "Logic",
            "SELECT * FROM t WHERE NOT (a = 1 OR b = 2)",
            [
                {"type": "logic_operators_include", "values": ["NOT", "OR"]},
                {"type": "predicate_operators_include", "values": ["="]},
            ],
        ),
        _case(
            "from_implicit_comma_join",
            "JOIN",
            "SELECT a.id FROM a, b WHERE a.id = b.a_id",
            [
                {"type": "field_contains", "field": "table_references", "needles": ["a", "b"]},
                {"type": "predicate_operators_include", "values": ["="]},
            ],
        ),
        _case(
            "join_natural",
            "JOIN",
            "SELECT * FROM student NATURAL JOIN takes",
            [
                {"type": "field_count_at_least", "field": "joins", "value": 1},
                {"type": "field_contains", "field": "joins", "needles": ["NATURAL"]},
            ],
        ),
        _case(
            "join_using_single_column",
            "JOIN ON",
            "SELECT * FROM student JOIN takes USING (id)",
            [
                {"type": "field_count_at_least", "field": "joins", "value": 1},
                {"type": "field_contains", "field": "joins", "needles": ["USING", "id"]},
            ],
        ),
        _case(
            "join_using_multiple_columns",
            "JOIN ON",
            "SELECT * FROM enrollment JOIN grades USING (student_id, course_id)",
            [
                {"type": "field_count_at_least", "field": "joins", "value": 1},
                {"type": "field_contains", "field": "joins", "needles": ["USING", "student_id", "course_id"]},
            ],
        ),
        _case(
            "join_three_table_chain",
            "JOIN",
            "SELECT s.name, c.title FROM student s JOIN takes t ON s.id = t.s_id JOIN course c ON t.course_id = c.id",
            [
                {"type": "field_count_at_least", "field": "joins", "value": 2},
                {"type": "field_contains", "field": "joins", "needles": ["takes", "course"]},
            ],
        ),
        _case(
            "group_by_alias_and_ordinal",
            "GROUP BY",
            "SELECT dept AS d, COUNT(*) FROM student GROUP BY d, 1",
            [
                {"type": "field_count_at_least", "field": "group_by", "value": 2},
                {"type": "field_contains", "field": "group_by", "needles": ["d", "1"]},
            ],
        ),
        _case(
            "having_sum_avg_min_max_predicates",
            "HAVING",
            "SELECT dept FROM instructor GROUP BY dept HAVING SUM(salary) > 1000 AND AVG(salary) >= 100 AND MIN(salary) < MAX(salary)",
            [
                {"type": "predicate_contexts_include", "values": ["HAVING"]},
                {"type": "aggregate_functions_include", "values": ["SUM", "AVG", "MIN", "MAX"]},
                {"type": "logic_operators_include", "values": ["AND"]},
            ],
        ),
        _case(
            "aggregate_distinct_sum_avg",
            "Aggregate",
            "SELECT SUM(DISTINCT salary), AVG(DISTINCT salary) FROM instructor",
            [
                {"type": "aggregate_functions_include", "values": ["SUM", "AVG"]},
                {"type": "field_contains", "field": "aggregate_functions", "needles": ["distinct"]},
            ],
        ),
        _case(
            "order_by_nulls_first_last",
            "ORDER BY",
            "SELECT name FROM instructor ORDER BY salary DESC NULLS LAST, name ASC NULLS FIRST",
            [
                {"type": "field_count_at_least", "field": "order_by", "value": 2},
                {"type": "order_nulls_include", "values": ["LAST", "FIRST"]},
            ],
        ),
        _case(
            "limit_fetch_first",
            "LIMIT/OFFSET",
            "SELECT name FROM student FETCH FIRST 5 ROWS ONLY",
            [
                {"type": "limit_keys_include", "keys": ["limit"]},
                {"type": "field_contains", "field": "limit_offset", "needles": ["5"]},
            ],
        ),
        _case(
            "subquery_derived_table",
            "Subquery",
            "SELECT x.dept FROM (SELECT dept FROM student WHERE age > 18) x",
            [
                {"type": "field_count_at_least", "field": "subqueries", "value": 1},
                {"type": "field_contains", "field": "subqueries", "needles": ["SELECT dept"]},
            ],
        ),
        _case(
            "subquery_not_exists",
            "Subquery",
            "SELECT s.name FROM student s WHERE NOT EXISTS (SELECT 1 FROM takes t WHERE t.s_id = s.id)",
            [
                {"type": "field_count_at_least", "field": "subqueries", "value": 1},
                {"type": "logic_operators_include", "values": ["NOT"]},
            ],
        ),
        _case(
            "subquery_any_all",
            "Subquery",
            "SELECT name FROM instructor WHERE salary > ANY (SELECT salary FROM instructor) AND salary >= ALL (SELECT salary FROM instructor)",
            [
                {"type": "field_count_at_least", "field": "subqueries", "value": 2},
                {"type": "predicate_kinds_include", "values": ["quantified_comparison"]},
                {"type": "predicate_quantifiers_include", "values": ["ANY", "ALL"]},
                {"type": "predicate_operators_include", "values": [">", ">="]},
            ],
        ),
        _case(
            "cte_column_list",
            "CTE",
            "WITH c(id, name) AS (SELECT id, name FROM student) SELECT name FROM c",
            [
                {"type": "field_count_at_least", "field": "ctes", "value": 1},
                {"type": "field_contains", "field": "ctes", "needles": ["c", "id", "name"]},
            ],
        ),
        _case(
            "cte_three_dependency_chain",
            "CTE",
            "WITH a AS (SELECT id FROM student), b AS (SELECT id FROM a), c AS (SELECT id FROM b) SELECT id FROM c",
            [
                {"type": "field_count_at_least", "field": "ctes", "value": 3},
                {"type": "field_contains", "field": "ctes", "needles": ["a", "b", "c"]},
            ],
        ),
        _case(
            "case_nested",
            "CASE",
            "SELECT CASE WHEN score >= 90 THEN CASE WHEN honors = 1 THEN 'A+' ELSE 'A' END ELSE 'B' END FROM exam",
            [
                {"type": "field_count_at_least", "field": "case_branches", "value": 2},
                {"type": "predicate_operators_include", "values": [">=", "="]},
            ],
        ),
        _case(
            "window_lag_lead_ntile_values",
            "Window",
            "SELECT LAG(score, 1, 0) OVER (PARTITION BY student_id ORDER BY exam_date), LEAD(score) OVER (ORDER BY exam_date), NTILE(4) OVER (ORDER BY score), FIRST_VALUE(score) OVER (ORDER BY exam_date), LAST_VALUE(score) OVER (ORDER BY exam_date) FROM exam",
            [
                {"type": "field_count_at_least", "field": "window_functions", "value": 5},
                {"type": "field_contains", "field": "window_function_details", "needles": ["LAG", "LEAD", "NTILE", "FIRST_VALUE", "LAST_VALUE"]},
            ],
        ),
        _case(
            "window_named_reference",
            "Window",
            "SELECT ROW_NUMBER() OVER w FROM instructor WINDOW w AS (PARTITION BY dept ORDER BY salary DESC)",
            [
                {"type": "field_count_at_least", "field": "window_functions", "value": 1},
                {"type": "field_contains", "field": "window_function_details", "needles": ["window_name", "w", "is_named_reference"]},
                {"type": "field_contains", "field": "named_windows", "needles": ["w", "dept", "salary"]},
            ],
        ),
        _case(
            "select_quoted_identifier",
            "SELECT",
            'SELECT "student name" AS student_name FROM student',
            [
                {"type": "field_contains", "field": "projection", "needles": ["student name", "student_name"]},
                {"type": "field_contains", "field": "expression_ir", "needles": ["column", "student_name"]},
            ],
        ),
        _case(
            "select_unary_parenthesized_modulo",
            "SELECT",
            "SELECT -(score % 10) AS neg_mod FROM exam",
            [
                {"type": "field_contains", "field": "projection", "needles": ["score", "%", "neg_mod"]},
                {"type": "field_contains", "field": "expression_ir", "needles": ["neg_mod"]},
            ],
        ),
        _case(
            "where_boolean_literal",
            "WHERE",
            "SELECT * FROM flags WHERE enabled = TRUE AND archived = FALSE",
            [
                {"type": "predicate_contexts_include", "values": ["WHERE"]},
                {"type": "predicate_operators_include", "values": ["="]},
                {"type": "logic_operators_include", "values": ["AND"]},
            ],
        ),
        _case(
            "comparison_column_to_column",
            "Comparison",
            "SELECT * FROM t WHERE start_date <= end_date",
            [
                {"type": "predicate_kinds_include", "values": ["comparison"]},
                {"type": "predicate_operators_include", "values": ["<="]},
                {"type": "field_contains", "field": "predicate_ir", "needles": ["start_date", "end_date"]},
            ],
        ),
        _case(
            "join_non_equi_between_tables",
            "JOIN ON",
            "SELECT * FROM a JOIN b ON a.score > b.min_score",
            [
                {"type": "field_count_at_least", "field": "joins", "value": 1},
                {"type": "predicate_contexts_include", "values": ["JOIN ON"]},
                {"type": "predicate_operators_include", "values": [">"]},
            ],
        ),
        _case(
            "group_by_function_expression",
            "GROUP BY",
            "SELECT LOWER(dept), COUNT(*) FROM student GROUP BY LOWER(dept)",
            [
                {"type": "field_count_at_least", "field": "group_by", "value": 1},
                {"type": "field_contains", "field": "group_by", "needles": ["LOWER"]},
                {"type": "aggregate_functions_include", "values": ["COUNT"]},
            ],
        ),
        _case(
            "order_by_collate",
            "ORDER BY",
            "SELECT name FROM student ORDER BY name COLLATE NOCASE ASC",
            [
                {"type": "field_count_at_least", "field": "order_by", "value": 1},
                {"type": "field_contains", "field": "order_by", "needles": ["name"]},
                {"type": "order_collations_include", "values": ["NOCASE"]},
            ],
        ),
        _case(
            "subquery_nested_in",
            "Subquery",
            "SELECT name FROM student WHERE id IN (SELECT s_id FROM advisor WHERE i_id IN (SELECT id FROM instructor))",
            [
                {"type": "field_count_at_least", "field": "subqueries", "value": 2},
                {"type": "predicate_kinds_include", "values": ["in_subquery"]},
            ],
        ),
        _case(
            "correlated_scalar_subquery",
            "Correlated Subquery",
            "SELECT s.name FROM student s WHERE s.age > (SELECT AVG(s2.age) FROM student s2 WHERE s2.dept = s.dept)",
            [
                {"type": "subquery_correlated_count_at_least", "value": 1},
                {"type": "aggregate_functions_include", "values": ["AVG"]},
            ],
        ),
        _case(
            "set_operation_parenthesized_branches",
            "Set Operation",
            "(SELECT id FROM a UNION SELECT id FROM b) EXCEPT SELECT id FROM c",
            [
                {"type": "set_operations_include", "values": ["UNION", "EXCEPT"]},
            ],
        ),
        _case(
            "case_simple_without_else",
            "CASE",
            "SELECT CASE grade WHEN 'A' THEN 4 WHEN 'B' THEN 3 END AS points FROM grades",
            [
                {"type": "field_count_at_least", "field": "case_branches", "value": 1},
                {"type": "field_contains", "field": "case_branches", "needles": ["WHEN", "THEN"]},
            ],
        ),
        _case(
            "window_range_frame",
            "Window",
            "SELECT SUM(amount) OVER (ORDER BY day RANGE BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) FROM sales",
            [
                {"type": "field_count_at_least", "field": "window_functions", "value": 1},
                {"type": "field_contains", "field": "window_function_details", "needles": ["RANGE", "UNBOUNDED PRECEDING"]},
            ],
        ),
        _case(
            "gap_distinct_on",
            "DISTINCT",
            "SELECT DISTINCT ON (dept) dept, name FROM student ORDER BY dept, name",
            [],
            representation="known_gap",
            dialect_hint="postgres",
            note="PostgreSQL DISTINCT ON is outside current typed DISTINCT IR.",
        ),
        _case(
            "gap_grouping_sets",
            "GROUP BY",
            "SELECT region, product, SUM(amount) FROM sales GROUP BY GROUPING SETS ((region), (product))",
            [],
            representation="known_gap",
            note="GROUPING SETS is not first-class typed in current GROUP BY IR.",
        ),
        _case(
            "gap_aggregate_filter",
            "Aggregate",
            "SELECT COUNT(*) FILTER (WHERE score > 0) FROM exam",
            [],
            representation="known_gap",
            note="Aggregate FILTER predicate is not first-class typed in current aggregate IR.",
        ),
        _case(
            "gap_recursive_search_cycle",
            "Recursive CTE",
            "WITH RECURSIVE t(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM t WHERE n < 3) SEARCH DEPTH FIRST BY n SET ord SELECT n FROM t",
            [],
            representation="known_gap",
            note="Recursive SEARCH/CYCLE clauses are outside current typed recursive CTE IR.",
        ),
        _case(
            "boundary_lateral",
            "Dialect Boundary",
            "SELECT s.name, x.value FROM student s CROSS JOIN LATERAL (SELECT s.id + 1 AS value) x",
            [],
            representation="known_boundary",
            note="Known execution/transpilation boundary.",
        ),
        _case(
            "boundary_qualify",
            "Dialect Boundary",
            "SELECT name, ROW_NUMBER() OVER (ORDER BY score DESC) AS rn FROM exam QUALIFY rn = 1",
            [],
            representation="known_boundary",
            note="QUALIFY is treated as a dialect boundary for Phase 1.",
        ),
        _case(
            "boundary_rollup",
            "Dialect Boundary",
            "SELECT region, SUM(amount) FROM sales GROUP BY ROLLUP(region)",
            [],
            representation="known_boundary",
            note="Known execution/transpilation boundary.",
        ),
        _case(
            "boundary_cube",
            "Dialect Boundary",
            "SELECT region, product, SUM(amount) FROM sales GROUP BY CUBE(region, product)",
            [],
            representation="known_boundary",
            note="Known execution/transpilation boundary.",
        ),
        _case(
            "set_operation_intersect_all",
            "Set Operation",
            "SELECT id FROM a INTERSECT ALL SELECT id FROM b",
            [
                {"type": "set_operations_include", "values": ["INTERSECT"]},
                {"type": "set_operation_detail", "operator": "INTERSECT", "all": True},
            ],
            note="IR supports the ALL flag; execution support is evaluated in the sandbox stage.",
        ),
        _case(
            "set_operation_except_all",
            "Set Operation",
            "SELECT id FROM a EXCEPT ALL SELECT id FROM b",
            [
                {"type": "set_operations_include", "values": ["EXCEPT"]},
                {"type": "set_operation_detail", "operator": "EXCEPT", "all": True},
            ],
            note="IR supports the ALL flag; execution support is evaluated in the sandbox stage.",
        ),
    ]


def _dialects_for(sql: str, hint: str | None) -> list[str]:
    if hint:
        return [hint, "sqlite", "mysql", "tsql"]
    if "`" in sql:
        return ["mysql", "sqlite", "tsql"]
    if "TOP" in sql.upper() or "[" in sql:
        return ["tsql", "sqlite", "mysql"]
    return ["sqlite", "mysql", "tsql"]


def parse_query(sql: str, hint: str | None = None) -> tuple[exp.Expression | None, str | None, str | None]:
    errors: list[str] = []
    for dialect in _dialects_for(sql, hint):
        try:
            statements = sqlglot.parse(sql, dialect=dialect, error_level=ErrorLevel.RAISE)
            parsed = [
                statement for statement in statements
                if statement is not None and not isinstance(statement, exp.Semicolon)
            ]
            if len(parsed) == 1 and isinstance(parsed[0], exp.Query):
                return parsed[0], dialect, None
            errors.append(f"{dialect}: not_one_query")
        except Exception as exc:
            errors.append(f"{dialect}: {exc}")
    return None, None, "; ".join(errors)


def _norm(value: Any) -> str:
    if isinstance(value, dict):
        text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    elif isinstance(value, list):
        text = " ".join(_norm(item) for item in value)
    else:
        text = str(value)
    return " ".join(text.replace('"', "").replace("`", "").replace("[", "").replace("]", "").split()).lower()


def _field_text(ir: dict[str, Any], field: str) -> str:
    return _norm(ir.get(field))


def evaluate_check(ir: dict[str, Any], check: dict[str, Any]) -> tuple[bool, str]:
    check_type = check["type"]
    if check_type == "field_count_at_least":
        field = check["field"]
        actual = ir.get(field) or []
        return len(actual) >= int(check["value"]), f"{field} count {len(actual)} >= {check['value']}"

    if check_type == "field_contains":
        field = check["field"]
        text = _field_text(ir, field)
        missing = [needle for needle in check["needles"] if _norm(needle) not in text]
        return not missing, f"{field} missing {missing}" if missing else f"{field} contains expected text"

    if check_type == "field_contains_any":
        field = check["field"]
        text = _field_text(ir, field)
        missing_groups = []
        for group in check["groups"]:
            if not any(_norm(needle) in text for needle in group):
                missing_groups.append(group)
        return not missing_groups, f"{field} missing any-of {missing_groups}" if missing_groups else f"{field} contains expected alternatives"

    if check_type == "field_equals":
        field = check["field"]
        return ir.get(field) == check["value"], f"{field} expected {check['value']}, got {ir.get(field)}"

    if check_type == "join_types_include":
        values = {str(item).upper() for item in check["values"]}
        actual = {str(join.get("type") or "").upper() for join in ir.get("joins") or []}
        missing = sorted(values - actual)
        return not missing, f"join types missing {missing}, actual {sorted(actual)}"

    if check_type == "join_conditions_contain":
        text = _norm([join.get("condition") for join in ir.get("joins") or []])
        missing = [needle for needle in check["needles"] if _norm(needle) not in text]
        return not missing, f"join conditions missing {missing}" if missing else "join conditions contain expected text"

    if check_type == "order_directions_include":
        values = {str(item).upper() for item in check["values"]}
        actual = {str(item.get("direction") or "").upper() for item in ir.get("order_by") or []}
        missing = sorted(values - actual)
        return not missing, f"order directions missing {missing}, actual {sorted(actual)}"

    if check_type == "order_nulls_include":
        values = {str(item).upper() for item in check["values"]}
        actual = {str(item.get("nulls") or "").upper() for item in ir.get("order_by") or []}
        missing = sorted(values - actual)
        return not missing, f"order nulls missing {missing}, actual {sorted(actual)}"

    if check_type == "order_collations_include":
        values = {str(item).upper() for item in check["values"]}
        actual = {str(item.get("collation") or "").upper() for item in ir.get("order_by") or []}
        missing = sorted(values - actual)
        return not missing, f"order collations missing {missing}, actual {sorted(actual)}"

    if check_type == "limit_keys_include":
        actual = set((ir.get("limit_offset") or {}).keys())
        expected = set(check["keys"])
        missing = sorted(expected - actual)
        return not missing, f"limit keys missing {missing}, actual {sorted(actual)}"

    if check_type == "subquery_correlated_count_at_least":
        actual = sum(1 for item in ir.get("subqueries") or [] if item.get("is_correlated"))
        return actual >= int(check["value"]), f"correlated subquery count {actual} >= {check['value']}"

    if check_type == "cte_recursive_count_at_least":
        actual = sum(1 for item in ir.get("ctes") or [] if item.get("recursive"))
        return actual >= int(check["value"]), f"recursive CTE count {actual} >= {check['value']}"

    if check_type == "set_operations_include":
        actual = {str(item).upper() for item in ir.get("set_operations") or []}
        expected = {str(item).upper() for item in check["values"]}
        missing = sorted(expected - actual)
        return not missing, f"set operations missing {missing}, actual {sorted(actual)}"

    if check_type == "set_operation_detail":
        operator = str(check["operator"]).upper()
        expected_all = bool(check.get("all"))
        for item in ir.get("set_operation_details") or []:
            if str(item.get("operator") or "").upper() == operator and bool(item.get("all")) == expected_all:
                return True, f"set operation detail found {operator} all={expected_all}"
        return False, f"set operation detail missing {operator} all={expected_all}"

    if check_type == "predicate_contexts_include":
        actual = {str(item.get("context") or "").upper() for item in ir.get("predicate_ir") or []}
        expected = {str(item).upper() for item in check["values"]}
        missing = sorted(expected - actual)
        return not missing, f"predicate contexts missing {missing}, actual {sorted(actual)}"

    if check_type == "predicate_kinds_include":
        actual = {str(item.get("kind") or "") for item in ir.get("predicate_ir") or []}
        expected = {str(item) for item in check["values"]}
        missing = sorted(expected - actual)
        return not missing, f"predicate kinds missing {missing}, actual {sorted(actual)}"

    if check_type == "predicate_operators_include":
        actual = {str(item.get("operator") or "").upper() for item in ir.get("predicate_ir") or []}
        expected = {str(item).upper() for item in check["values"]}
        missing = sorted(expected - actual)
        return not missing, f"predicate operators missing {missing}, actual {sorted(actual)}"

    if check_type == "predicate_quantifiers_include":
        actual = {str(item.get("quantifier") or "").upper() for item in ir.get("predicate_ir") or []}
        expected = {str(item).upper() for item in check["values"]}
        missing = sorted(expected - actual)
        return not missing, f"predicate quantifiers missing {missing}, actual {sorted(actual)}"

    if check_type == "logic_operators_include":
        actual = {
            str(item.get("operator") or "").upper()
            for item in ir.get("predicate_ir") or []
            if item.get("kind") == "logic"
        }
        expected = {str(item).upper() for item in check["values"]}
        missing = sorted(expected - actual)
        return not missing, f"logic operators missing {missing}, actual {sorted(actual)}"

    if check_type == "aggregate_functions_include":
        actual = {str(item.get("function") or "").upper() for item in ir.get("aggregate_functions") or []}
        expected = {str(item).upper() for item in check["values"]}
        missing = sorted(expected - actual)
        return not missing, f"aggregate functions missing {missing}, actual {sorted(actual)}"

    if check_type == "aggregate_distinct_include":
        function = str(check["function"]).upper()
        arg = _norm(check.get("arg") or "")
        matched = False
        for item in ir.get("aggregate_functions") or []:
            if str(item.get("function") or "").upper() != function:
                continue
            if not item.get("distinct"):
                continue
            args = _norm(item.get("args") or [])
            if not arg or arg in args:
                matched = True
                break
        return matched, f"distinct aggregate {function}({arg}) present={matched}"

    return False, f"unknown check type {check_type}"


def evaluate_case(case: dict[str, Any]) -> dict[str, Any]:
    ast, dialect, parse_error = parse_query(case["sql"], case.get("dialect_hint"))
    parse_ok = ast is not None
    ir_build_ok = False
    ir_dict: dict[str, Any] = {}
    check_results: list[dict[str, Any]] = []

    if ast is not None:
        try:
            ir = SQLStructureIR.from_ast(ast)
            ir_dict = ir.to_dict()
            ir_build_ok = True
        except Exception as exc:
            parse_error = f"ir_build_failed: {exc}"

    if ir_build_ok:
        for check in case["checks"]:
            passed, detail = evaluate_check(ir_dict, check)
            check_results.append({
                "check": check,
                "passed": passed,
                "detail": detail,
            })

    checks_passed = all(item["passed"] for item in check_results)
    if case["representation"] in {"known_boundary", "known_gap"}:
        bucket = "known_boundary"
        if case["representation"] == "known_gap":
            bucket = "known_gap"
    elif parse_ok and ir_build_ok and checks_passed:
        bucket = case["representation"]
    else:
        bucket = "unexpected_failure"

    return {
        **case,
        "parse_ok": parse_ok,
        "dialect": dialect,
        "parse_error": parse_error,
        "ir_build_ok": ir_build_ok,
        "ir": ir_dict,
        "checks_passed": checks_passed,
        "check_results": check_results,
        "capability_bucket": bucket,
    }


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(results)
    buckets = Counter(item["capability_bucket"] for item in results)
    categories = defaultdict(Counter)
    for item in results:
        categories[item["category"]][item["capability_bucket"]] += 1
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total": total,
        "parse_success": sum(1 for item in results if item["parse_ok"]),
        "ir_build_success": sum(1 for item in results if item["ir_build_ok"]),
        "buckets": dict(buckets),
        "category_buckets": {category: dict(counter) for category, counter in sorted(categories.items())},
        "case_support_rate": (buckets["first_class"] + buckets["weak_textual"]) / max(total - buckets["known_boundary"], 1),
    }


def write_markdown(summary: dict[str, Any], results: list[dict[str, Any]], path: Path) -> None:
    lines: list[str] = []
    lines.append("# Phase 1 IR Structure Capability")
    lines.append("")
    lines.append(f"Generated at: `{summary['generated_at']}`")
    lines.append("")
    lines.append("This benchmark evaluates IR structure recognition only. It does not test data generation, sandbox equivalence, or mutation isolation.")
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    lines.append(f"- Total cases: `{summary['total']}`")
    lines.append(f"- Parse success: `{summary['parse_success']}`")
    lines.append(f"- IR build success: `{summary['ir_build_success']}`")
    lines.append(f"- Buckets: `{summary['buckets']}`")
    lines.append(f"- Non-boundary support rate: `{summary['case_support_rate']:.2%}`")
    lines.append("")
    lines.append("Bucket meanings:")
    lines.append("")
    lines.append("- `first_class`: captured by dedicated IR fields.")
    lines.append("- `weak_textual`: visible only as SQL text inside an IR field.")
    lines.append("- `known_boundary`: outside current Phase 1 IR/execution boundary.")
    lines.append("- `known_gap`: in or near teaching scope, but not first-class typed by the current IR.")
    lines.append("- `unexpected_failure`: expected supported structure was not captured.")
    lines.append("")
    lines.append("## Category Matrix")
    lines.append("")
    lines.append("| category | first_class | weak_textual | known_gap | known_boundary | unexpected_failure |")
    lines.append("| --- | ---: | ---: | ---: | ---: | ---: |")
    for category, counter in summary["category_buckets"].items():
        lines.append(
            f"| {category} | {counter.get('first_class', 0)} | {counter.get('weak_textual', 0)} | "
            f"{counter.get('known_gap', 0)} | {counter.get('known_boundary', 0)} | "
            f"{counter.get('unexpected_failure', 0)} |"
        )
    lines.append("")
    lines.append("## Cases")
    lines.append("")
    lines.append("| result | category | id | dialect | checks | note |")
    lines.append("| --- | --- | --- | --- | --- | --- |")
    for item in results:
        passed = sum(1 for check in item["check_results"] if check["passed"])
        total_checks = len(item["check_results"])
        note = item.get("note") or ""
        lines.append(
            f"| `{item['capability_bucket']}` | {item['category']} | `{item['id']}` | "
            f"`{item.get('dialect')}` | `{passed}/{total_checks}` | {note} |"
        )
    failures = [item for item in results if item["capability_bucket"] == "unexpected_failure"]
    if failures:
        lines.append("")
        lines.append("## Unexpected Failures")
        lines.append("")
        for item in failures:
            lines.append(f"### {item['id']}")
            lines.append("")
            lines.append(f"- Category: `{item['category']}`")
            lines.append(f"- Parse OK: `{item['parse_ok']}`")
            lines.append(f"- IR build OK: `{item['ir_build_ok']}`")
            if item.get("parse_error"):
                lines.append(f"- Error: `{item['parse_error']}`")
            lines.append("")
            lines.append("```sql")
            lines.append(item["sql"])
            lines.append("```")
            lines.append("")
            lines.append("Checks:")
            lines.append("")
            for result in item["check_results"]:
                status = "PASS" if result["passed"] else "FAIL"
                lines.append(f"- `{status}` {result['detail']}")
            lines.append("")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    cases = build_cases()
    results = [evaluate_case(case) for case in cases]
    summary = summarize(results)
    json_path = OUTPUT_DIR / "phase1_ir_structure_capability.json"
    md_path = OUTPUT_DIR / "phase1_ir_structure_capability.md"
    cases_path = OUTPUT_DIR / "phase1_ir_structure_cases.jsonl"
    evidence_path = OUTPUT_DIR / "phase1_ir_structure_detailed_evidence.jsonl"
    cases_path.write_text(
        "\n".join(json.dumps(case, ensure_ascii=False) for case in cases) + "\n",
        encoding="utf-8",
    )
    evidence_path.write_text(
        "\n".join(json.dumps(result, ensure_ascii=False) for result in results) + "\n",
        encoding="utf-8",
    )
    json_path.write_text(
        json.dumps({"summary": summary, "results": results}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    write_markdown(summary, results, md_path)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"Wrote {cases_path}")
    print(f"Wrote {evidence_path}")
    print(f"Wrote {json_path}")
    print(f"Wrote {md_path}")


if __name__ == "__main__":
    main()
