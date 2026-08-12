import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import sqlglot
from sqlglot import ErrorLevel, exp

PROJECT_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = PROJECT_ROOT / "sql-edu-backend"
OUTPUT_DIR = PROJECT_ROOT / "data_construct_test" / "outputs"
sys.path.append(str(BACKEND_ROOT))

from core.error_attribution import evidence_weights_from_observation
from core.parseval_data_generator import extract_ast_diffs, generate_and_compare
from core.ast_schema import SQLStructureIR


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


def _case(
    case_id: str,
    area: str,
    expectation: str,
    schema: str,
    standard: str,
    student: str,
    expected_kps: list[str] | None = None,
    *,
    cfg_labels: list[str] | None = None,
    attack_kind: str = "semantic_mutation",
    max_rows_per_table: int = 8,
    note: str = "",
) -> dict[str, Any]:
    return {
        "id": case_id,
        "area": area,
        "expectation": expectation,
        "schema": schema,
        "standard": standard,
        "student": student,
        "expected_kps": expected_kps or [],
        "cfg_labels": cfg_labels or expected_kps or [],
        "attack_kind": attack_kind,
        "max_rows_per_table": max_rows_per_table,
        "note": note,
    }


def build_cases() -> list[dict[str, Any]]:
    """Curated Phase 1 capability samples.

    expectation:
    - equivalent: current pipeline should execute and judge equivalent.
    - not_equivalent: current pipeline should execute, expose a mismatch, and hit
      the expected attribution KP when provided.
    - syntax_rejected: current pipeline should reject before sandbox execution.
    """
    return [
        _case(
            "parse_syntax_unclosed_parenthesis",
            "AST_PARSE",
            "syntax_rejected",
            "student(id, name);",
            "SELECT name FROM student",
            "SELECT name FROM (student",
            ["select-basic"],
            cfg_labels=["select-basic"],
            attack_kind="parser",
            note=(
                "Known direct generate_and_compare gap: sqlglot transpilation can "
                "rewrite the malformed FROM item into a runnable subquery."
            ),
        ),
        _case(
            "correct_self_equivalent",
            "SANDBOX_EQUIVALENCE",
            "equivalent",
            "course(course_id, title, credits);",
            "SELECT title FROM course WHERE credits > 3",
            "SELECT title FROM course WHERE credits > 3",
            cfg_labels=["select-basic", "where", "where-comp"],
            attack_kind="identity_control",
        ),
        _case(
            "where_boundary_detected",
            "WHERE",
            "not_equivalent",
            "course(course_id, title, credits);",
            "SELECT title FROM course WHERE credits > 3",
            "SELECT title FROM course WHERE credits >= 3",
            ["where"],
            cfg_labels=["where", "where-comp"],
        ),
        _case(
            "select_column_missing_detected",
            "SELECT",
            "not_equivalent",
            "course(course_id, title, credits);",
            "SELECT title, credits FROM course",
            "SELECT title FROM course",
            ["select-basic"],
        ),
        _case(
            "distinct_missing_detected",
            "DISTINCT",
            "not_equivalent",
            "takes(ID, course_id, year);",
            "SELECT DISTINCT course_id FROM takes",
            "SELECT course_id FROM takes",
            ["distinct"],
        ),
        _case(
            "join_on_key_mismatch_detected",
            "JOIN_ON",
            "not_equivalent",
            "student(ID, name); advisor(s_ID, i_ID);",
            "SELECT student.name FROM student JOIN advisor ON student.ID = advisor.s_ID",
            "SELECT student.name FROM student JOIN advisor ON student.ID = advisor.i_ID",
            ["join-on"],
            cfg_labels=["join-inner", "join-on"],
        ),
        _case(
            "left_join_changed_to_inner_detected",
            "JOIN_TYPE",
            "not_equivalent",
            "student(ID, name); takes(ID, course_id);",
            "SELECT s.name FROM student s LEFT JOIN takes t ON s.ID = t.ID",
            "SELECT s.name FROM student s JOIN takes t ON s.ID = t.ID",
            ["join-left"],
        ),
        _case(
            "group_by_column_mismatch_detected",
            "GROUP_BY",
            "not_equivalent",
            "instructor(ID, name, dept_name, salary, building);",
            "SELECT SUM(salary) FROM instructor GROUP BY dept_name",
            "SELECT SUM(salary) FROM instructor GROUP BY building",
            ["group-by"],
        ),
        _case(
            "having_count_boundary_detected",
            "HAVING",
            "not_equivalent",
            "student(ID, name, dept_name, tot_cred);",
            "SELECT dept_name FROM student GROUP BY dept_name HAVING COUNT(*) >= 4",
            "SELECT dept_name FROM student GROUP BY dept_name HAVING COUNT(*) > 4",
            ["having"],
        ),
        _case(
            "limit_count_mismatch_detected",
            "LIMIT",
            "not_equivalent",
            "course(course_id, title, credits);",
            "SELECT title FROM course LIMIT 2",
            "SELECT title FROM course LIMIT 4",
            ["limit"],
            cfg_labels=["limit-offset"],
        ),
        _case(
            "order_direction_detected_with_8_rows",
            "ORDER_BY",
            "not_equivalent",
            "course(course_id, title, credits);",
            "SELECT title FROM course ORDER BY credits DESC",
            "SELECT title FROM course ORDER BY credits ASC",
            ["order-by"],
            max_rows_per_table=8,
            note="Supported when the generated projected title sequence differs by direction.",
        ),
        _case(
            "order_direction_missed_with_10_rows",
            "ORDER_BY",
            "not_equivalent",
            "course(course_id, title, credits);",
            "SELECT title FROM course ORDER BY credits DESC",
            "SELECT title FROM course ORDER BY credits ASC",
            ["order-by"],
            max_rows_per_table=10,
            note=(
                "Known data-generation gap: the projected title cycle can make ASC "
                "and DESC outputs identical even though ORDER BY differs."
            ),
        ),
        _case(
            "case_boundary_detected",
            "CASE",
            "not_equivalent",
            "sales(sale_id, category, amount);",
            (
                "SELECT category, SUM(CASE WHEN amount > 100 THEN amount ELSE 0 END) "
                "FROM sales GROUP BY category"
            ),
            (
                "SELECT category, SUM(CASE WHEN amount >= 100 THEN amount ELSE 0 END) "
                "FROM sales GROUP BY category"
            ),
            ["case"],
        ),
        _case(
            "window_partition_missing_detected",
            "WINDOW",
            "not_equivalent",
            "instructor(ID, name, dept_name, salary);",
            (
                "SELECT name, ROW_NUMBER() OVER (PARTITION BY dept_name ORDER BY salary DESC) AS rn "
                "FROM instructor"
            ),
            "SELECT name, ROW_NUMBER() OVER (ORDER BY salary DESC) AS rn FROM instructor",
            ["window-row-number"],
        ),
        _case(
            "union_all_mismatch_detected",
            "UNION",
            "not_equivalent",
            "course(course_id, title, dept_name, credits);",
            (
                "SELECT title FROM course WHERE dept_name = 'CS' "
                "UNION SELECT title FROM course WHERE credits > 3"
            ),
            (
                "SELECT title FROM course WHERE dept_name = 'CS' "
                "UNION ALL SELECT title FROM course WHERE credits > 3"
            ),
            ["union"],
        ),
        _case(
            "except_missing_detected",
            "EXCEPT",
            "not_equivalent",
            "course(course_id, title, dept_name, credits);",
            "SELECT title FROM course EXCEPT SELECT title FROM course WHERE dept_name = 'CS'",
            "SELECT title FROM course",
            ["except"],
        ),
        _case(
            "correlated_subquery_literal_detected",
            "CORRELATED_SUBQUERY",
            "not_equivalent",
            "student(ID, name, dept_name); takes(ID, course_id, year);",
            (
                "SELECT name FROM student s WHERE EXISTS "
                "(SELECT 1 FROM takes t WHERE t.ID = s.ID AND t.year = 2017)"
            ),
            (
            "SELECT name FROM student s WHERE EXISTS "
                "(SELECT 1 FROM takes t WHERE t.ID = s.ID AND t.year = 2018)"
            ),
            ["subquery-correlated"],
            cfg_labels=["subquery-exists"],
        ),
        _case(
            "alias_header_only_equivalence",
            "ALIAS",
            "equivalent",
            "student(id, name);",
            "SELECT name AS student_name FROM student",
            "SELECT name FROM student",
            cfg_labels=["alias"],
            attack_kind="equivalent_rewrite",
            note="Alias-only differences are a false-positive trap when column headers are compared.",
        ),
        _case(
            "arithmetic_identity_equivalence",
            "ARITHMETIC",
            "equivalent",
            "sales(id, amount);",
            "SELECT id, amount + 0 AS amount FROM sales",
            "SELECT id, amount AS amount FROM sales",
            cfg_labels=["arithmetic"],
            attack_kind="equivalent_rewrite",
        ),
        _case(
            "null_equals_null_detected",
            "NULL_HANDLING",
            "not_equivalent",
            "employee(id, name, manager_id);",
            "SELECT name FROM employee WHERE manager_id IS NULL",
            "SELECT name FROM employee WHERE manager_id = NULL",
            ["where", "comp-null"],
            cfg_labels=["null-handling"],
            attack_kind="null_three_valued_logic",
        ),
        _case(
            "between_equivalent_expansion",
            "BETWEEN",
            "equivalent",
            "course(course_id, title, credits);",
            "SELECT title FROM course WHERE credits BETWEEN 3 AND 5",
            "SELECT title FROM course WHERE credits >= 3 AND credits <= 5",
            cfg_labels=["between"],
            attack_kind="equivalent_rewrite",
        ),
        _case(
            "in_list_equivalent_or_expansion",
            "IN_LIST",
            "equivalent",
            "course(course_id, title, dept_name);",
            "SELECT title FROM course WHERE dept_name IN ('Math', 'Physics')",
            "SELECT title FROM course WHERE dept_name = 'Math' OR dept_name = 'Physics'",
            cfg_labels=["in-list"],
            attack_kind="equivalent_rewrite",
        ),
        _case(
            "subquery_in_negation_detected",
            "SUBQUERY_IN",
            "not_equivalent",
            "student(id, name); takes(id, course_id, year);",
            "SELECT name FROM student WHERE id IN (SELECT id FROM takes WHERE year = 2017)",
            "SELECT name FROM student WHERE id NOT IN (SELECT id FROM takes WHERE year = 2017)",
            ["where", "subquery-scalar"],
            cfg_labels=["subquery-in"],
        ),
        _case(
            "scalar_subquery_replaced_by_literal",
            "SUBQUERY_SCALAR",
            "not_equivalent",
            "instructor(id, name, salary);",
            "SELECT name FROM instructor WHERE salary > (SELECT AVG(salary) FROM instructor)",
            "SELECT name FROM instructor WHERE salary > 50000",
            ["subquery-scalar", "where", "agg-count"],
            cfg_labels=["subquery-scalar"],
        ),
        _case(
            "like_suffix_changed_to_prefix",
            "LIKE",
            "not_equivalent",
            "course(course_id, title);",
            "SELECT title FROM course WHERE title LIKE 'Data%'",
            "SELECT title FROM course WHERE title LIKE '%Data'",
            ["where"],
            cfg_labels=["like"],
        ),
        _case(
            "right_join_changed_to_inner",
            "JOIN_TYPE",
            "not_equivalent",
            "student(id, name); takes(id, course_id);",
            "SELECT s.name FROM takes t RIGHT JOIN student s ON s.id = t.id",
            "SELECT s.name FROM takes t JOIN student s ON s.id = t.id",
            ["join-right"],
            cfg_labels=["join-right-full"],
            attack_kind="dangling_tuple",
        ),
        _case(
            "complex_join_middle_key_drift",
            "COMPLEX_JOIN",
            "not_equivalent",
            "student(id, name, dept_id); dept(dept_id, dept_name); takes(id, course_id);",
            (
                "SELECT s.name, d.dept_name, t.course_id FROM student s "
                "JOIN dept d ON s.dept_id = d.dept_id JOIN takes t ON s.id = t.id"
            ),
            (
                "SELECT s.name, d.dept_name, t.course_id FROM student s "
                "JOIN dept d ON s.id = d.dept_id JOIN takes t ON s.id = t.id"
            ),
            ["join-on"],
            cfg_labels=["complex-join"],
            attack_kind="join_topology",
        ),
        _case(
            "intersect_changed_to_union",
            "INTERSECT",
            "not_equivalent",
            "course(course_id, title, dept_name, credits);",
            (
                "SELECT title FROM course WHERE dept_name = 'CS' "
                "INTERSECT SELECT title FROM course WHERE credits > 3"
            ),
            (
                "SELECT title FROM course WHERE dept_name = 'CS' "
                "UNION SELECT title FROM course WHERE credits > 3"
            ),
            ["intersect"],
            cfg_labels=["intersect"],
        ),
        _case(
            "aggregate_count_changed_to_sum",
            "AGGREGATE",
            "not_equivalent",
            "orders(id, customer_id, amount);",
            "SELECT customer_id, COUNT(*) FROM orders GROUP BY customer_id",
            "SELECT customer_id, SUM(amount) FROM orders GROUP BY customer_id",
            ["agg-count"],
            cfg_labels=["agg-count"],
        ),
        _case(
            "window_aggregate_partition_removed",
            "WINDOW_AGG",
            "not_equivalent",
            "sales(id, region, amount);",
            "SELECT id, SUM(amount) OVER (PARTITION BY region) AS total FROM sales",
            "SELECT id, SUM(amount) OVER () AS total FROM sales",
            ["window-row-number"],
            cfg_labels=["window-agg"],
        ),
        _case(
            "cte_filter_removed",
            "CTE",
            "not_equivalent",
            "employee(id, name, salary);",
            "WITH high_salary AS (SELECT * FROM employee WHERE salary > 50000) SELECT name FROM high_salary",
            "SELECT name FROM employee",
            ["cte", "where"],
            cfg_labels=["cte"],
        ),
        _case(
            "recursive_cte_termination_changed",
            "CTE_RECURSIVE",
            "not_equivalent",
            "dummy(id);",
            (
                "WITH RECURSIVE nums(n) AS (SELECT 1 UNION ALL "
                "SELECT n + 1 FROM nums WHERE n < 5) SELECT n FROM nums"
            ),
            (
                "WITH RECURSIVE nums(n) AS (SELECT 1 UNION ALL "
                "SELECT n + 1 FROM nums WHERE n < 3) SELECT n FROM nums"
            ),
            ["cte-recursive"],
            cfg_labels=["cte-recursive"],
            attack_kind="recursive_boundary",
        ),
        _case(
            "order_secondary_key_missing",
            "ORDER_BY",
            "not_equivalent",
            "instructor(id, name, salary);",
            "SELECT name FROM instructor ORDER BY salary ASC, name DESC",
            "SELECT name FROM instructor ORDER BY salary ASC",
            ["order-by"],
            cfg_labels=["order-by"],
            attack_kind="tie_probe",
            note="Requires duplicate salary values whose names break ties in the opposite order.",
        ),
        _case(
            "rank_changed_to_row_number_on_ties",
            "WINDOW",
            "not_equivalent",
            "instructor(id, name, dept_name, salary);",
            (
                "SELECT name, RANK() OVER (PARTITION BY dept_name ORDER BY salary DESC) AS rn "
                "FROM instructor"
            ),
            (
                "SELECT name, ROW_NUMBER() OVER (PARTITION BY dept_name ORDER BY salary DESC) AS rn "
                "FROM instructor"
            ),
            ["window-row-number"],
            cfg_labels=["window-row-number"],
            attack_kind="tie_probe",
            note="Requires duplicate salary values inside one partition.",
        ),
        _case(
            "cte_inline_equivalence",
            "CTE",
            "equivalent",
            "employee(id, name, salary);",
            "WITH e AS (SELECT name FROM employee WHERE salary > 3) SELECT name FROM e",
            "SELECT name FROM employee WHERE salary > 3",
            cfg_labels=["cte"],
            attack_kind="equivalent_rewrite",
        ),
        _case(
            "aggregate_null_denominator",
            "NULL_AGGREGATE",
            "not_equivalent",
            "instructor(id, name, dept_name, salary);",
            "SELECT AVG(salary) FROM instructor",
            "SELECT SUM(salary) / COUNT(*) FROM instructor",
            ["agg-count", "null-handling"],
            cfg_labels=["agg-count", "null-handling"],
            attack_kind="null_aggregate_probe",
            note="Requires at least one NULL salary so COUNT(*) and AVG use different denominators.",
        ),
        _case(
            "having_sum_exact_boundary",
            "HAVING",
            "not_equivalent",
            "instructor(id, name, dept_name, salary);",
            (
                "SELECT dept_name, SUM(salary) FROM instructor GROUP BY dept_name "
                "HAVING SUM(salary) > 50000"
            ),
            (
                "SELECT dept_name, SUM(salary) FROM instructor GROUP BY dept_name "
                "HAVING SUM(salary) >= 50000"
            ),
            ["having"],
            cfg_labels=["having", "agg-count"],
            attack_kind="aggregate_exact_boundary",
        ),
        _case(
            "having_count_column_vs_star_null",
            "HAVING",
            "not_equivalent",
            "instructor(id, name, dept_name, salary);",
            "SELECT dept_name FROM instructor GROUP BY dept_name HAVING COUNT(salary) >= 2",
            "SELECT dept_name FROM instructor GROUP BY dept_name HAVING COUNT(*) >= 2",
            ["having"],
            cfg_labels=["having", "agg-count", "null-handling"],
            attack_kind="null_aggregate_probe",
        ),
        _case(
            "scalar_subquery_global_vs_filtered_average",
            "SUBQUERY_SCALAR",
            "not_equivalent",
            "instructor(id, name, dept_name, salary);",
            "SELECT name FROM instructor WHERE salary > (SELECT AVG(salary) FROM instructor)",
            (
                "SELECT name FROM instructor WHERE salary > "
                "(SELECT AVG(salary) FROM instructor WHERE dept_name = 'Comp. Sci.')"
            ),
            ["subquery-scalar", "where", "agg-count"],
            cfg_labels=["subquery-scalar", "agg-count"],
            attack_kind="subquery_distribution",
            max_rows_per_table=10,
        ),
        _case(
            "union_branch_predicates_swapped",
            "UNION",
            "not_equivalent",
            "instructor(id, name, dept_name, salary); student(id, name, dept_name, tot_cred);",
            (
                "SELECT name FROM instructor WHERE dept_name = 'Comp. Sci.' "
                "UNION SELECT name FROM student WHERE dept_name = 'Math'"
            ),
            (
                "SELECT name FROM instructor WHERE dept_name = 'Math' "
                "UNION SELECT name FROM student WHERE dept_name = 'Comp. Sci.'"
            ),
            ["union", "where"],
            cfg_labels=["union", "where"],
            attack_kind="set_branch_asymmetry",
        ),
        _case(
            "recursive_cte_without_physical_schema",
            "CTE_RECURSIVE",
            "not_equivalent",
            "",
            (
                "WITH RECURSIVE nums(n) AS (SELECT 1 UNION ALL "
                "SELECT n + 1 FROM nums WHERE n < 5) SELECT n FROM nums"
            ),
            (
                "WITH RECURSIVE nums(n) AS (SELECT 1 UNION ALL "
                "SELECT n + 1 FROM nums WHERE n < 3) SELECT n FROM nums"
            ),
            ["cte-recursive"],
            cfg_labels=["cte-recursive"],
            attack_kind="schema_free_recursive_query",
        ),
        _case(
            "left_anti_join_limit_boundary",
            "COMBINATION",
            "not_equivalent",
            "student(id, name, dept_name); takes(id, course_id);",
            (
                "SELECT s.name FROM student s LEFT JOIN takes t ON s.id = t.id "
                "WHERE t.id IS NULL LIMIT 2"
            ),
            (
                "SELECT s.name FROM student s LEFT JOIN takes t ON s.id = t.id "
                "WHERE t.id IS NULL LIMIT 3"
            ),
            ["limit"],
            cfg_labels=["join-left", "null-handling", "limit-offset"],
            attack_kind="operator_combination",
        ),
        _case(
            "order_nulls_last_missing",
            "ORDER_BY",
            "not_equivalent",
            "instructor(id, name, dept_name, salary);",
            "SELECT name, salary FROM instructor ORDER BY salary ASC NULLS LAST",
            "SELECT name, salary FROM instructor ORDER BY salary ASC",
            ["order-by"],
            cfg_labels=["order-by", "null-handling"],
            attack_kind="null_sorting_probe",
        ),
        _case(
            "cte_reverse_city_condition",
            "CTE",
            "not_equivalent",
            "works(company_name, person_name, salary); company(company_name, city);",
            (
                "WITH bj AS (SELECT company_name FROM company WHERE city = 'Beijing') "
                "SELECT person_name FROM works WHERE company_name IN (SELECT company_name FROM bj)"
            ),
            (
                "WITH bj AS (SELECT company_name FROM company WHERE city <> 'Beijing') "
                "SELECT person_name FROM works WHERE company_name IN (SELECT company_name FROM bj)"
            ),
            ["cte", "where"],
            cfg_labels=["cte", "subquery-in", "where"],
            attack_kind="cte_join_topology",
        ),
        _case(
            "anti_join_vs_not_in_with_null",
            "NULL_SUBQUERY",
            "not_equivalent",
            "student(id, name); takes(id, course_id);",
            (
                "SELECT s.name FROM student s LEFT JOIN takes t ON s.id = t.id "
                "WHERE t.id IS NULL"
            ),
            "SELECT name FROM student WHERE id NOT IN (SELECT id FROM takes)",
            ["join-left", "where", "subquery-scalar"],
            cfg_labels=["join-left", "subquery-in", "null-handling"],
            attack_kind="null_three_valued_logic",
            note="These forms are not equivalent when the NOT IN subquery contains NULL.",
        ),
        _case(
            "limit_large_vs_unbounded",
            "LIMIT",
            "not_equivalent",
            "student(id, name, tot_cred);",
            "SELECT name FROM student ORDER BY tot_cred DESC LIMIT 100",
            "SELECT name FROM student ORDER BY tot_cred DESC",
            ["limit"],
            cfg_labels=["limit-offset", "order-by"],
            attack_kind="cardinality_boundary",
            note="These queries diverge for databases with more than 100 rows.",
        ),
        _case(
            "exists_vs_join_distinct_with_duplicate_names",
            "SUBQUERY_EXISTS",
            "not_equivalent",
            "student(id, name); takes(id, grade);",
            (
                "SELECT name FROM student s WHERE EXISTS "
                "(SELECT 1 FROM takes t WHERE t.id = s.id AND t.grade = 'A')"
            ),
            (
                "SELECT DISTINCT s.name FROM student s JOIN takes t ON s.id = t.id "
                "WHERE t.grade = 'A'"
            ),
            ["subquery-correlated", "distinct", "join-inner"],
            cfg_labels=["subquery-exists", "distinct", "join-inner"],
            attack_kind="duplicate_projection_semantics",
            note="JOIN DISTINCT collapses different students that share the same name.",
        ),
    ]


def _strict_parse_ok(sql: str) -> bool:
    try:
        statements = sqlglot.parse(sql, dialect="mysql", error_level=ErrorLevel.RAISE)
        parsed = [
            statement for statement in statements
            if statement is not None and not isinstance(statement, exp.Semicolon)
        ]
        return len(parsed) == 1 and isinstance(parsed[0], exp.Query)
    except Exception:
        return False


def _build_ir(sql: str) -> SQLStructureIR | None:
    try:
        statements = sqlglot.parse(sql, dialect="mysql", error_level=ErrorLevel.RAISE)
        parsed = [
            statement for statement in statements
            if statement is not None and not isinstance(statement, exp.Semicolon)
        ]
        if len(parsed) != 1 or not isinstance(parsed[0], exp.Query):
            return None
        return SQLStructureIR.from_ast(parsed[0])
    except Exception:
        return None


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, exp.Expression):
        try:
            return value.sql(dialect="sqlite")
        except Exception:
            return str(value)
    if hasattr(value, "to_dict"):
        return _json_safe(value.to_dict())
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_safe(val) for key, val in value.items()}
    return str(value)


def _hit_expected_kp(kp_ids: list[str], expected: list[str]) -> bool:
    if not expected:
        return True
    return any(kp in kp_ids for kp in expected)


def run_case(case: dict[str, Any]) -> dict[str, Any]:
    std_parse_ok = _strict_parse_ok(case["standard"])
    stu_parse_ok = _strict_parse_ok(case["student"])
    standard_ir = _build_ir(case["standard"])
    student_ir = _build_ir(case["student"])
    std_ir_ok = standard_ir is not None
    stu_ir_ok = student_ir is not None

    try:
        raw_diffs = extract_ast_diffs(case["standard"], case["student"])
        diff_exception = None
    except Exception as exc:
        raw_diffs = []
        diff_exception = f"{type(exc).__name__}: {exc}"

    try:
        run = generate_and_compare(
            case["schema"],
            case["standard"],
            case["student"],
            max_rows_per_table=case["max_rows_per_table"],
        )
        attr = evidence_weights_from_observation(
            student_sql=case["student"],
            answer_sql=case["standard"],
            is_correct=bool(run.is_equivalent),
            error_message=run.error or run.data_evidence.get("student_exec_error"),
            judge_detail=run.data_evidence,
            mutation_detail=run.mutation_evidence,
            ast_diffs=[diff.to_dict() for diff in run.ast_diffs],
        )
        kp_ids = [item.knowledge_point_id for item in attr.attributions]
        kp_hit = _hit_expected_kp(kp_ids, case["expected_kps"])
        high_risk_attributions = [
            item
            for item in attr.attributions
            if item.severity >= 0.7 and item.error_type != "complication"
        ]
        exception = None
    except Exception as exc:
        run = None
        attr = None
        kp_ids = []
        kp_hit = False
        high_risk_attributions = []
        exception = f"{type(exc).__name__}: {exc}"

    if case["expectation"] == "equivalent":
        parse_stage_met = std_parse_ok and stu_parse_ok and std_ir_ok and stu_ir_ok
        structure_stage_met = True
        data_stage_met = bool(run and run.executed and run.is_equivalent is True)
        attribution_stage_met = bool(attr is not None and not high_risk_attributions)
        expectation_met = data_stage_met and attribution_stage_met
    elif case["expectation"] == "not_equivalent":
        parse_stage_met = std_parse_ok and stu_parse_ok and std_ir_ok and stu_ir_ok
        structure_stage_met = bool(raw_diffs)
        data_stage_met = bool(run and run.executed and run.is_equivalent is False)
        attribution_stage_met = kp_hit
        expectation_met = data_stage_met and attribution_stage_met
    elif case["expectation"] == "syntax_rejected":
        parse_stage_met = std_parse_ok and not stu_parse_ok
        structure_stage_met = parse_stage_met
        data_stage_met = bool(run and run.executed is False)
        attribution_stage_met = True
        expectation_met = parse_stage_met and data_stage_met
    else:
        raise ValueError(f"Unknown expectation: {case['expectation']}")

    mutation_tests = (run.mutation_evidence.get("tests", []) if run else []) or []
    fixed_clauses = [
        item.get("clause")
        for item in mutation_tests
        if item.get("fixed_by_replacement")
    ]

    return {
        **case,
        "capability_bucket": "supported" if expectation_met else "known_gap",
        "expectation_met": expectation_met,
        "strict_standard_parse_ok": std_parse_ok,
        "strict_student_parse_ok": stu_parse_ok,
        "standard_ir_build_ok": std_ir_ok,
        "student_ir_build_ok": stu_ir_ok,
        "standard_ir": _json_safe(standard_ir),
        "student_ir": _json_safe(student_ir),
        "parse_stage_met": parse_stage_met,
        "structure_stage_met": structure_stage_met,
        "data_stage_met": data_stage_met,
        "attribution_stage_met": attribution_stage_met,
        "diff_exception": diff_exception,
        "extract_ast_diff_count": len(raw_diffs),
        "extract_ast_diff_types": [
            {
                "clause": diff.clause_category,
                "diff_type": diff.diff_type,
                "kp": diff.knowledge_point_id,
            }
            for diff in raw_diffs
        ],
        "ast_diff_graph": _json_safe([diff.to_dict() for diff in raw_diffs]),
        "executed": run.executed if run else False,
        "is_equivalent": run.is_equivalent if run else None,
        "error": run.error if run else exception,
        "standard_row_count": len(run.standard_rows) if run else 0,
        "student_row_count": len(run.student_rows) if run else 0,
        "standard_rows_sample": _json_safe((run.standard_rows if run else [])[:5]),
        "student_rows_sample": _json_safe((run.student_rows if run else [])[:5]),
        "standard_columns": _json_safe(run.standard_columns if run else []),
        "student_columns": _json_safe(run.student_columns if run else []),
        "test_database": _json_safe(run.test_database if run else {}),
        "generation_tactics": _json_safe(run.data_evidence.get("generation_tactics", []) if run else []),
        "execution_evidence": _json_safe(run.data_evidence if run else {}),
        "run_ast_diffs": _json_safe([diff.to_dict() for diff in run.ast_diffs] if run else []),
        "mutation_fixed_clauses": _json_safe(fixed_clauses),
        "mutation_summary": _json_safe((run.mutation_evidence or {}).get("summary") if run else None),
        "mutation_evidence": _json_safe(run.mutation_evidence if run else {}),
        "kp_ids": kp_ids,
        "kp_hit": kp_hit,
        "diagnostic_clean": bool(
            attr is not None
            and all(item.error_type == "complication" for item in attr.attributions)
        ),
        "high_risk_attributions": _json_safe(
            [item.to_dict() for item in high_risk_attributions]
        ),
        "top_attributions": _json_safe([item.to_dict() for item in (attr.attributions if attr else [])[:5]]),
        "attributions": _json_safe([item.to_dict() for item in (attr.attributions if attr else [])]),
    }


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(results)
    supported = sum(1 for item in results if item["capability_bucket"] == "supported")
    by_area = defaultdict(lambda: {"total": 0, "supported": 0, "known_gap": 0})
    by_expectation = Counter(item["expectation"] for item in results)
    stage_pass = Counter()
    by_cfg_label = defaultdict(lambda: {"total": 0, "supported": 0, "known_gap": 0})
    for item in results:
        area = by_area[item["area"]]
        area["total"] += 1
        area[item["capability_bucket"]] += 1
        for stage in ("parse", "structure", "data", "attribution"):
            if item[f"{stage}_stage_met"]:
                stage_pass[stage] += 1
        for label in item["cfg_labels"]:
            if label not in REQUIRED_CFG_LABELS:
                continue
            label_summary = by_cfg_label[label]
            label_summary["total"] += 1
            label_summary[item["capability_bucket"]] += 1
    covered_labels = set(by_cfg_label)
    return {
        "total_cases": total,
        "supported_cases": supported,
        "known_gap_cases": total - supported,
        "support_rate": round(supported / total, 4) if total else 0.0,
        "by_expectation": dict(by_expectation),
        "stage_pass": {
            stage: {
                "passed": stage_pass[stage],
                "total": total,
                "rate": round(stage_pass[stage] / total, 4) if total else 0.0,
            }
            for stage in ("parse", "structure", "data", "attribution")
        },
        "by_area": dict(sorted(by_area.items())),
        "cfg_coverage": {
            "required": len(REQUIRED_CFG_LABELS),
            "covered": len(covered_labels),
            "missing": sorted(REQUIRED_CFG_LABELS - covered_labels),
            "by_label": dict(sorted(by_cfg_label.items())),
        },
        "known_gap_ids": [item["id"] for item in results if item["capability_bucket"] == "known_gap"],
        "stage_gap_ids": {
            stage: [item["id"] for item in results if not item[f"{stage}_stage_met"]]
            for stage in ("parse", "structure", "data", "attribution")
        },
        "spurious_attribution_ids": [
            item["id"]
            for item in results
            if item["expectation"] == "equivalent" and not item["diagnostic_clean"]
        ],
    }


def render_markdown(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    lines = [
        "# Phase 1 Capability Samples",
        "",
        f"Generated at: `{payload['generated_at']}`",
        "",
        "This report records executable samples for the current Phase 1 functions:",
        "`extract_ast_diffs`, `generate_and_compare`, and `evidence_weights_from_observation`.",
        "",
        "## Summary",
        "",
        f"- Total cases: `{summary['total_cases']}`",
        f"- Supported: `{summary['supported_cases']}`",
        f"- Known gaps: `{summary['known_gap_cases']}`",
        f"- Support rate: `{summary['support_rate']:.1%}`",
        f"- CFG labels covered: `{summary['cfg_coverage']['covered']}/{summary['cfg_coverage']['required']}`",
        f"- Missing CFG labels: `{', '.join(summary['cfg_coverage']['missing']) or '-'}`",
        f"- Equivalent cases with attribution noise: `{len(summary['spurious_attribution_ids'])}`",
        "",
        "## Pipeline Stages",
        "",
        "| stage | passed | rate |",
        "| --- | ---: | ---: |",
    ]
    for stage, stage_summary in summary["stage_pass"].items():
        lines.append(
            f"| {stage} | {stage_summary['passed']}/{stage_summary['total']} | "
            f"{stage_summary['rate']:.1%} |"
        )

    lines.extend([
        "",
        "## CFG Capability",
        "",
        "| CFG label | cases | supported | gaps |",
        "| --- | ---: | ---: | ---: |",
    ])
    for label, label_summary in summary["cfg_coverage"]["by_label"].items():
        lines.append(
            f"| {label} | {label_summary['total']} | {label_summary['supported']} | "
            f"{label_summary['known_gap']} |"
        )

    lines.extend([
        "",
        "## Case Matrix",
        "",
        "| id | CFG | kind | expected | result | IR | AST | data | attribution |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ])
    for item in payload["results"]:
        lines.append(
            "| {id} | {cfg} | {kind} | {expectation} | {bucket} | {ir_ok} | {ast_ok} | {data_ok} | {attr_ok} |".format(
                id=item["id"],
                cfg=", ".join(item["cfg_labels"]) or "-",
                kind=item["attack_kind"],
                expectation=item["expectation"],
                bucket=item["capability_bucket"],
                ir_ok=item["standard_ir_build_ok"] and item["student_ir_build_ok"],
                ast_ok=item["structure_stage_met"],
                data_ok=item["data_stage_met"],
                attr_ok=item["attribution_stage_met"],
            )
        )

    gaps = [item for item in payload["results"] if item["capability_bucket"] == "known_gap"]
    lines.extend(["", "## Known Gaps", ""])
    if not gaps:
        lines.append("No known gaps in this sample set.")
    for item in gaps:
        lines.extend(
            [
                f"### {item['id']}",
                "",
                f"- Area: `{item['area']}`",
                f"- CFG labels: `{', '.join(item['cfg_labels']) or '-'}`",
                f"- Attack kind: `{item['attack_kind']}`",
                f"- Expectation: `{item['expectation']}`",
                f"- Strict student parse ok: `{item['strict_student_parse_ok']}`",
                f"- Pipeline executed/equivalent: `{item['executed']}` / `{item['is_equivalent']}`",
                f"- Stage parse/AST/data/attribution: `{item['parse_stage_met']}` / `{item['structure_stage_met']}` / `{item['data_stage_met']}` / `{item['attribution_stage_met']}`",
                f"- Note: {item['note'] or '-'}",
                "",
                "Standard SQL:",
                "```sql",
                item["standard"],
                "```",
                "",
                "Student SQL:",
                "```sql",
                item["student"],
                "```",
                "",
                f"- Standard rows sample: `{item['standard_rows_sample']}`",
                f"- Student rows sample: `{item['student_rows_sample']}`",
                f"- Generated database: `{item['test_database']}`",
                "",
            ]
        )
    return "\n".join(lines)


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    results = [run_case(case) for case in build_cases()]
    summary = summarize(results)
    if summary["cfg_coverage"]["missing"]:
        raise RuntimeError(
            "Capability corpus is missing CFG labels: "
            + ", ".join(summary["cfg_coverage"]["missing"])
        )
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": summary,
        "results": results,
    }
    json_path = OUTPUT_DIR / "phase1_capability_samples.json"
    md_path = OUTPUT_DIR / "phase1_capability_samples.md"
    json_path.write_text(json.dumps(_json_safe(payload), ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(render_markdown(payload), encoding="utf-8")
    print(f"JSON report: {json_path}")
    print(f"Markdown report: {md_path}")
    print(json.dumps(payload["summary"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
