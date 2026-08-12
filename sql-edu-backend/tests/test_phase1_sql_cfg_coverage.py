from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest
import sqlglot
from sqlglot import ErrorLevel

from core.ast_schema import ASTDiffNode, SQLStructureIR
from core.error_attribution import evidence_weights_from_observation
from core.parseval_data_generator import (
    extract_ast_diffs,
    generate_and_compare,
    parse_schema_text,
    transpile_to_sqlite,
)
from core.sql_parser import infer_output_columns_from_sql


REQUIRED_CFG_LABELS = {
    "select-basic",
    "distinct",
    "alias",
    "arithmetic",
    "case",
    "null-handling",
    "where",
    "where-comp",
    "between",
    "in-list",
    "subquery-in",
    "subquery-exists",
    "subquery-scalar",
    "like",
    "join-inner",
    "join-left",
    "join-right-full",
    "join-on",
    "complex-join",
    "union",
    "intersect",
    "except",
    "group-by",
    "having",
    "order-by",
    "limit-offset",
    "agg-count",
    "window-row-number",
    "window-agg",
    "cte",
    "cte-recursive",
}


@dataclass(frozen=True)
class CFGCase:
    case_id: str
    labels: set[str]
    schema: str
    standard: str
    student: str
    expected_equivalent: bool
    expected_kps: set[str]
    max_rows_per_table: int = 8


CFG_CASES = [
    CFGCase(
        "select_projection_missing",
        {"select-basic"},
        "course(course_id, title, credits);",
        "SELECT title, credits FROM course",
        "SELECT title FROM course",
        False,
        {"select-basic"},
    ),
    CFGCase(
        "distinct_missing",
        {"distinct"},
        "takes(ID, course_id, year);",
        "SELECT DISTINCT course_id FROM takes",
        "SELECT course_id FROM takes",
        False,
        {"distinct"},
    ),
    CFGCase(
        "alias_changed",
        {"alias"},
        "course(course_id, title, credits);",
        "SELECT title AS course_title FROM course",
        "SELECT title AS title FROM course",
        True,
        set(),
    ),
    CFGCase(
        "arithmetic_projection_changed",
        {"arithmetic"},
        "sales(id, amount);",
        "SELECT id, amount + 1 AS amount2 FROM sales",
        "SELECT id, amount AS amount2 FROM sales",
        False,
        {"select-basic"},
    ),
    CFGCase(
        "case_boundary_changed",
        {"case"},
        "student(id, grade);",
        "SELECT id, CASE WHEN grade >= 60 THEN 'pass' ELSE 'fail' END FROM student",
        "SELECT id, CASE WHEN grade >= 70 THEN 'pass' ELSE 'fail' END FROM student",
        False,
        {"case"},
    ),
    CFGCase(
        "null_predicate_changed",
        {"null-handling"},
        "employee(id, name, manager_id);",
        "SELECT name FROM employee WHERE manager_id IS NULL",
        "SELECT name FROM employee WHERE manager_id IS NOT NULL",
        False,
        {"where"},
    ),
    CFGCase(
        "where_comparison_boundary",
        {"where", "where-comp"},
        "course(course_id, title, credits);",
        "SELECT title FROM course WHERE credits > 3",
        "SELECT title FROM course WHERE credits >= 3",
        False,
        {"where"},
    ),
    CFGCase(
        "between_predicate_changed",
        {"between"},
        "course(course_id, title, credits);",
        "SELECT title FROM course WHERE credits BETWEEN 3 AND 5",
        "SELECT title FROM course WHERE credits > 3",
        False,
        {"where"},
    ),
    CFGCase(
        "in_list_changed",
        {"in-list"},
        "course(course_id, title, dept_name);",
        "SELECT title FROM course WHERE dept_name IN ('CS', 'Math')",
        "SELECT title FROM course WHERE dept_name IN ('CS')",
        False,
        {"where"},
    ),
    CFGCase(
        "like_pattern_changed",
        {"like"},
        "course(course_id, title);",
        "SELECT title FROM course WHERE title LIKE 'Data%'",
        "SELECT title FROM course WHERE title LIKE '%Data'",
        False,
        {"where"},
    ),
    CFGCase(
        "subquery_in_negated",
        {"subquery-in"},
        "student(ID, name); takes(ID, course_id, year);",
        "SELECT name FROM student WHERE ID IN (SELECT ID FROM takes WHERE year = 2017)",
        "SELECT name FROM student WHERE ID NOT IN (SELECT ID FROM takes WHERE year = 2017)",
        False,
        {"where", "subquery-scalar"},
    ),
    CFGCase(
        "exists_negated",
        {"subquery-exists"},
        "student(id, name); takes(student_id, course_id);",
        "SELECT name FROM student s WHERE EXISTS (SELECT 1 FROM takes t WHERE t.student_id = s.id)",
        "SELECT name FROM student s WHERE NOT EXISTS (SELECT 1 FROM takes t WHERE t.student_id = s.id)",
        False,
        {"where", "subquery-correlated", "subquery-scalar"},
    ),
    CFGCase(
        "scalar_subquery_replaced",
        {"subquery-scalar"},
        "instructor(id, name, salary);",
        "SELECT name FROM instructor WHERE salary > (SELECT AVG(salary) FROM instructor)",
        "SELECT name FROM instructor WHERE salary > 50000",
        False,
        {"subquery-scalar", "where", "agg-count"},
    ),
    CFGCase(
        "inner_join_on_key_changed",
        {"join-inner", "join-on"},
        "student(ID, name); advisor(s_ID, i_ID);",
        "SELECT student.name FROM student JOIN advisor ON student.ID = advisor.s_ID",
        "SELECT student.name FROM student JOIN advisor ON student.ID = advisor.i_ID",
        False,
        {"join-on"},
    ),
    CFGCase(
        "left_join_changed_to_inner",
        {"join-left"},
        "student(ID, name); takes(ID, course_id);",
        "SELECT s.name FROM student s LEFT JOIN takes t ON s.ID = t.ID",
        "SELECT s.name FROM student s JOIN takes t ON s.ID = t.ID",
        False,
        {"join-left"},
    ),
    CFGCase(
        "right_join_changed_to_inner",
        {"join-right-full"},
        "student(ID, name); takes(ID, course_id);",
        "SELECT s.name FROM takes t RIGHT JOIN student s ON s.ID = t.ID",
        "SELECT s.name FROM takes t JOIN student s ON s.ID = t.ID",
        False,
        {"join-right"},
    ),
    CFGCase(
        "full_join_changed_to_left",
        {"join-right-full"},
        "student(ID, name); takes(ID, course_id);",
        "SELECT s.name FROM student s FULL JOIN takes t ON s.ID = t.ID",
        "SELECT s.name FROM student s LEFT JOIN takes t ON s.ID = t.ID",
        False,
        {"join-full"},
    ),
    CFGCase(
        "complex_join_on_key_changed",
        {"complex-join"},
        "student(ID, name, dept_id); dept(dept_id, dept_name); takes(ID, course_id);",
        (
            "SELECT s.name, d.dept_name, t.course_id "
            "FROM student s "
            "JOIN dept d ON s.dept_id = d.dept_id "
            "JOIN takes t ON s.ID = t.ID"
        ),
        (
            "SELECT s.name, d.dept_name, t.course_id "
            "FROM student s "
            "JOIN dept d ON s.ID = d.dept_id "
            "JOIN takes t ON s.ID = t.ID"
        ),
        False,
        {"join-on"},
    ),
    CFGCase(
        "union_branch_removed",
        {"union"},
        "course(course_id, title, dept_name, credits);",
        "SELECT title FROM course WHERE dept_name = 'CS' UNION SELECT title FROM course WHERE credits > 3",
        "SELECT title FROM course WHERE dept_name = 'CS'",
        False,
        {"union", "where"},
    ),
    CFGCase(
        "intersect_changed_to_union",
        {"intersect"},
        "course(course_id, title, dept_name, credits);",
        "SELECT title FROM course WHERE dept_name = 'CS' INTERSECT SELECT title FROM course WHERE credits > 3",
        "SELECT title FROM course WHERE dept_name = 'CS' UNION SELECT title FROM course WHERE credits > 3",
        False,
        {"intersect"},
    ),
    CFGCase(
        "except_removed",
        {"except"},
        "course(course_id, title, dept_name, credits);",
        "SELECT title FROM course EXCEPT SELECT title FROM course WHERE dept_name = 'CS'",
        "SELECT title FROM course",
        False,
        {"except"},
    ),
    CFGCase(
        "group_by_column_changed",
        {"group-by"},
        "instructor(ID, name, dept_name, salary, building);",
        "SELECT SUM(salary) FROM instructor GROUP BY dept_name",
        "SELECT SUM(salary) FROM instructor GROUP BY building",
        False,
        {"group-by"},
    ),
    CFGCase(
        "having_count_boundary",
        {"having"},
        "student(ID, name, dept_name, tot_cred);",
        "SELECT dept_name FROM student GROUP BY dept_name HAVING COUNT(*) >= 4",
        "SELECT dept_name FROM student GROUP BY dept_name HAVING COUNT(*) > 4",
        False,
        {"having"},
    ),
    CFGCase(
        "order_by_direction_changed",
        {"order-by"},
        "course(course_id, title, credits);",
        "SELECT title FROM course ORDER BY credits DESC",
        "SELECT title FROM course ORDER BY credits ASC",
        False,
        {"order-by"},
    ),
    CFGCase(
        "limit_offset_changed",
        {"limit-offset"},
        "course(course_id, title, credits);",
        "SELECT title FROM course ORDER BY credits DESC LIMIT 2 OFFSET 1",
        "SELECT title FROM course ORDER BY credits DESC LIMIT 2 OFFSET 2",
        False,
        {"limit"},
    ),
    CFGCase(
        "aggregate_function_changed",
        {"agg-count"},
        "orders(id, customer_id, amount);",
        "SELECT customer_id, COUNT(*) FROM orders GROUP BY customer_id",
        "SELECT customer_id, SUM(amount) FROM orders GROUP BY customer_id",
        False,
        {"agg-count"},
    ),
    CFGCase(
        "window_row_number_partition_removed",
        {"window-row-number"},
        "instructor(ID, name, dept_name, salary);",
        "SELECT name, ROW_NUMBER() OVER (PARTITION BY dept_name ORDER BY salary DESC) AS rn FROM instructor",
        "SELECT name, ROW_NUMBER() OVER (ORDER BY salary DESC) AS rn FROM instructor",
        False,
        {"window-row-number"},
    ),
    CFGCase(
        "window_aggregate_partition_removed",
        {"window-agg"},
        "sales(id, region, amount);",
        "SELECT region, amount, SUM(amount) OVER (PARTITION BY region) AS region_total FROM sales",
        "SELECT region, amount, SUM(amount) OVER () AS region_total FROM sales",
        False,
        {"window-row-number"},
    ),
    CFGCase(
        "cte_removed",
        {"cte"},
        "employee(emp_id, name, salary);",
        "WITH high_salary AS (SELECT * FROM employee WHERE salary > 50000) SELECT name FROM high_salary",
        "SELECT name FROM employee",
        False,
        {"cte", "where"},
    ),
    CFGCase(
        "recursive_cte_body_changed",
        {"cte-recursive"},
        "employee(emp_id, manager_id);",
        (
            "WITH RECURSIVE h AS ("
            "SELECT emp_id, manager_id FROM employee WHERE manager_id IS NULL "
            "UNION ALL "
            "SELECT e.emp_id, e.manager_id FROM employee e JOIN h ON e.manager_id = h.emp_id"
            ") SELECT emp_id FROM h"
        ),
        (
            "WITH RECURSIVE h AS ("
            "SELECT emp_id, manager_id FROM employee WHERE manager_id IS NULL"
            ") SELECT emp_id FROM h"
        ),
        False,
        {"cte-recursive"},
    ),
]


def _parse(sql: str):
    return sqlglot.parse_one(sql, dialect="mysql", error_level=ErrorLevel.RAISE)


def _kp_ids(result) -> set[str]:
    return {item.knowledge_point_id for item in result.attributions}


def test_cfg_case_table_covers_every_documented_phase1_label():
    covered = set().union(*(case.labels for case in CFG_CASES))
    assert covered == REQUIRED_CFG_LABELS


@pytest.mark.parametrize("case", CFG_CASES, ids=[case.case_id for case in CFG_CASES])
def test_phase1_cfg_construct_end_to_end(case: CFGCase):
    standard_ast = _parse(case.standard)
    student_ast = _parse(case.student)
    standard_ir = SQLStructureIR.from_ast(standard_ast)
    student_ir = SQLStructureIR.from_ast(student_ast)

    assert standard_ir.projection
    assert student_ir.projection
    assert transpile_to_sqlite(case.standard)
    assert transpile_to_sqlite(case.student)

    diffs = extract_ast_diffs(case.standard, case.student)
    assert diffs, f"{case.case_id} should produce at least one AST diff"
    assert all(isinstance(diff, ASTDiffNode) for diff in diffs)

    run = generate_and_compare(
        case.schema,
        case.standard,
        case.student,
        max_rows_per_table=case.max_rows_per_table,
    )
    assert run.executed is True, run.error
    assert run.is_equivalent is case.expected_equivalent
    assert run.data_evidence["sandbox_executed"] is True
    assert run.data_evidence["ast_diffs"]
    assert "generation_tactics" in run.data_evidence
    assert run.mutation_evidence["summary"]["executed"] >= 0

    attribution = evidence_weights_from_observation(
        student_sql=case.student,
        answer_sql=case.standard,
        is_correct=bool(run.is_equivalent),
        error_message=run.error or run.data_evidence.get("student_exec_error"),
        judge_detail=run.data_evidence,
        mutation_detail=run.mutation_evidence,
        ast_diffs=[diff.to_dict() for diff in run.ast_diffs],
    )
    observed_kps = _kp_ids(attribution)
    if case.expected_equivalent:
        assert not any(
            item.severity >= 0.7 and item.error_type != "complication"
            for item in attribution.attributions
        )
    else:
        assert observed_kps & case.expected_kps, (
            f"{case.case_id} expected one of {case.expected_kps}, got {observed_kps}"
        )
    assert attribution.observation["E_AST"]["student_parse_ok"] is True
    assert attribution.observation["E_AST"]["standard_parse_ok"] is True
    assert attribution.observation["E_AST"]["ast_diffs"]


def test_phase1_cfg_positive_equivalent_query_still_has_no_attribution():
    sql = "SELECT title FROM course WHERE credits > 3 ORDER BY title ASC LIMIT 3"
    run = generate_and_compare("course(course_id, title, credits);", sql, sql)
    assert run.executed is True
    assert run.is_equivalent is True

    attribution = evidence_weights_from_observation(
        student_sql=sql,
        answer_sql=sql,
        is_correct=True,
        judge_detail=run.data_evidence,
        mutation_detail=run.mutation_evidence,
        ast_diffs=[diff.to_dict() for diff in run.ast_diffs],
    )
    assert attribution.attributions == []


def test_phase1_support_functions_used_by_cfg_pipeline():
    schema = parse_schema_text('course(course_id INT, "course title" TEXT); [Order Details]([Order ID], `Unit Price`);')
    assert schema == {
        "course": ["course_id", "course title"],
        "Order Details": ["Order ID", "Unit Price"],
    }

    assert infer_output_columns_from_sql(
        "SELECT course_id AS id, title AS `course title` FROM course"
    ) == "id, course title"
    assert infer_output_columns_from_sql("SELECT * FROM course") is None

    top_sql = transpile_to_sqlite("SELECT TOP 2 name FROM student ORDER BY name DESC")
    assert top_sql is not None
    assert "LIMIT 2" in top_sql.upper()

    top_ties_sql = transpile_to_sqlite("SELECT TOP 1 WITH TIES seller_id FROM Sales ORDER BY SUM(price) DESC")
    assert top_ties_sql is not None
    assert "WITH TIES" not in top_ties_sql.upper()
    assert "LIMIT 1" in top_ties_sql.upper()

    extract_sql = transpile_to_sqlite("SELECT id FROM orders WHERE EXTRACT(YEAR FROM order_date) = 2024")
    assert extract_sql is not None
    assert "YEAR(" in extract_sql.upper()
    assert "EXTRACT" not in extract_sql.upper()

    dateadd_sql = transpile_to_sqlite("SELECT id FROM orders WHERE created_at >= DATEADD(day, -1, @d)")
    assert dateadd_sql is not None
    assert "DATEADD('day'" in dateadd_sql
    assert "@d" not in dateadd_sql

    function_sql = transpile_to_sqlite("SELECT CONCAT(first_name, ' ', last_name) AS full_name FROM student")
    assert function_sql is not None
    assert "||" in function_sql
