import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.append(str(PROJECT_ROOT / "sql-edu-backend"))

from core.error_attribution import evidence_weights_from_observation
from core.parseval_data_generator import generate_and_compare


def _case(group: str, schema: str, standard: str, student: str, expected: list[str]) -> dict[str, Any]:
    return {
        "group": group,
        "schema": schema,
        "standard": standard,
        "student": student,
        "expected": expected,
    }


def build_cases() -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []

    for boundary in range(1, 11):
        cases.append(_case(
            "WHERE",
            "course(course_id, title, credits)",
            f"SELECT title FROM course WHERE credits > {boundary};",
            f"SELECT title FROM course WHERE credits >= {boundary};",
            ["where"],
        ))

    for idx in range(10):
        col = "grade" if idx % 2 == 0 else "advisor"
        cases.append(_case(
            "NULL",
            f"student(ID, name, {col})",
            f"SELECT name FROM student WHERE {col} IS NULL;",
            f"SELECT name FROM student WHERE {col} = NULL;",
            ["comp-null"],
        ))

    projections = [
        ("title, credits", "title"),
        ("title, dept_name", "dept_name"),
        ("course_id, title", "title"),
        ("title, credits, dept_name", "title, credits"),
        ("credits", "title"),
    ]
    for idx in range(10):
        std_cols, stu_cols = projections[idx % len(projections)]
        cases.append(_case(
            "SELECT",
            "course(course_id, title, dept_name, credits)",
            f"SELECT {std_cols} FROM course WHERE credits > {idx + 1};",
            f"SELECT {stu_cols} FROM course WHERE credits > {idx + 1};",
            ["select-basic"],
        ))

    distinct_cols = ["course_id", "semester", "grade", "year", "sec_id"]
    for idx in range(10):
        col = distinct_cols[idx % len(distinct_cols)]
        cases.append(_case(
            "DISTINCT",
            "takes(ID, course_id, sec_id, semester, year, grade)",
            f"SELECT DISTINCT {col} FROM takes;",
            f"SELECT {col} FROM takes;",
            ["distinct"],
        ))

    for idx in range(10):
        cases.append(_case(
            "JOIN_ON",
            "student(ID, name, dept_name); advisor(s_ID, i_ID)",
            "SELECT student.name FROM student JOIN advisor ON student.ID = advisor.s_ID;",
            "SELECT student.name FROM student JOIN advisor ON student.ID = advisor.i_ID;",
            ["join-on"],
        ))

    for idx in range(10):
        cases.append(_case(
            "LEFT_JOIN",
            "student(ID, name, dept_name); takes(ID, course_id)",
            "SELECT student.name, takes.course_id FROM student LEFT JOIN takes ON student.ID = takes.ID;",
            "SELECT student.name, takes.course_id FROM student INNER JOIN takes ON student.ID = takes.ID;",
            ["join-left"],
        ))

    group_pairs = [("dept_name", "building"), ("building", "dept_name")]
    for idx in range(10):
        std_group, stu_group = group_pairs[idx % len(group_pairs)]
        cases.append(_case(
            "GROUP_BY",
            "instructor(ID, name, dept_name, salary, building)",
            f"SELECT SUM(salary) FROM instructor GROUP BY {std_group};",
            f"SELECT SUM(salary) FROM instructor GROUP BY {stu_group};",
            ["group-by"],
        ))

    aggs = [("SUM", 80000), ("AVG", 50000), ("MIN", 30000), ("MAX", 90000), ("COUNT", 2)]
    for idx in range(10):
        agg, boundary = aggs[idx % len(aggs)]
        expr = "ID" if agg == "COUNT" else "salary"
        op_std, op_stu = (">=", ">") if agg == "COUNT" else (">", "<")
        cases.append(_case(
            "HAVING",
            "instructor(ID, name, dept_name, salary)",
            f"SELECT dept_name FROM instructor GROUP BY dept_name HAVING {agg}({expr}) {op_std} {boundary};",
            f"SELECT dept_name FROM instructor GROUP BY dept_name HAVING {agg}({expr}) {op_stu} {boundary};",
            ["having"],
        ))

    for idx in range(10):
        direction_std, direction_stu = ("DESC", "ASC") if idx % 2 == 0 else ("ASC", "DESC")
        cases.append(_case(
            "ORDER_BY",
            "course(course_id, title, dept_name, credits)",
            f"SELECT title FROM course ORDER BY credits {direction_std};",
            f"SELECT title FROM course ORDER BY credits {direction_stu};",
            ["order-by"],
        ))

    for idx in range(10):
        limit = idx % 5 + 1
        cases.append(_case(
            "LIMIT",
            "course(course_id, title, dept_name, credits)",
            f"SELECT title FROM course LIMIT {limit};",
            f"SELECT title FROM course LIMIT {limit + 2};",
            ["limit"],
        ))

    for year in range(2015, 2025):
        cases.append(_case(
            "SUBQUERY",
            "student(ID, name, dept_name); takes(ID, course_id, year)",
            f"SELECT name FROM student WHERE ID IN (SELECT ID FROM takes WHERE year = {year});",
            f"SELECT name FROM student WHERE ID NOT IN (SELECT ID FROM takes WHERE year = {year});",
            ["where", "subquery-in"],
        ))

    for year in range(2015, 2025):
        cases.append(_case(
            "CORRELATED_SUBQUERY",
            "student(ID, name, dept_name); takes(ID, course_id, year)",
            f"SELECT name FROM student s WHERE EXISTS (SELECT 1 FROM takes t WHERE t.ID = s.ID AND t.year = {year});",
            f"SELECT name FROM student s WHERE EXISTS (SELECT 1 FROM takes t WHERE t.ID = s.ID AND t.year = {year + 1});",
            ["subquery-correlated"],
        ))

    for boundary in range(10000, 10010):
        cases.append(_case(
            "CTE",
            "works(company_name, person_name, salary); company(company_name, city)",
            f"WITH big_co AS (SELECT company_name FROM company WHERE city = 'Beijing') SELECT person_name, salary FROM works JOIN big_co ON works.company_name = big_co.company_name WHERE salary > {boundary};",
            f"WITH big_co AS (SELECT company_name FROM company WHERE city = 'Beijing') SELECT person_name, salary FROM works JOIN big_co ON works.company_name = big_co.company_name WHERE salary < {boundary};",
            ["where", "cte"],
        ))

    for limit in range(3, 13):
        cases.append(_case(
            "RECURSIVE_CTE",
            "dummy(id)",
            f"WITH RECURSIVE nums AS (SELECT 1 AS n UNION ALL SELECT n + 1 AS n FROM nums WHERE n < {limit}) SELECT n FROM nums;",
            f"WITH RECURSIVE nums AS (SELECT 1 AS n UNION ALL SELECT n + 1 AS n FROM nums WHERE n < {limit + 2}) SELECT n FROM nums;",
            ["cte-recursive"],
        ))

    depts = ["Math", "Physics", "Comp. Sci.", "History", "Biology"]
    for idx in range(10):
        left = depts[idx % len(depts)]
        right = depts[(idx + 1) % len(depts)]
        cases.append(_case(
            "UNION",
            "course(course_id, title, dept_name, credits)",
            f"SELECT title FROM course WHERE dept_name = '{left}' UNION SELECT title FROM course WHERE dept_name = '{right}';",
            f"SELECT title FROM course WHERE dept_name = '{left}' UNION ALL SELECT title FROM course WHERE dept_name = '{right}';",
            ["union"],
        ))

    for idx in range(10):
        dept = depts[idx % len(depts)]
        boundary = idx % 6 + 1
        cases.append(_case(
            "INTERSECT",
            "course(course_id, title, dept_name, credits)",
            f"SELECT title FROM course WHERE dept_name = '{dept}' INTERSECT SELECT title FROM course WHERE credits > {boundary};",
            f"SELECT title FROM course WHERE dept_name = '{dept}' UNION SELECT title FROM course WHERE credits > {boundary};",
            ["intersect"],
        ))

    for idx in range(10):
        dept = depts[idx % len(depts)]
        cases.append(_case(
            "EXCEPT",
            "course(course_id, title, dept_name, credits)",
            f"SELECT title FROM course EXCEPT SELECT title FROM course WHERE dept_name = '{dept}';",
            "SELECT title FROM course;",
            ["except"],
        ))

    for boundary in range(95, 105):
        cases.append(_case(
            "CASE",
            "sales(sale_id, category, amount)",
            f"SELECT category, SUM(CASE WHEN amount > {boundary} THEN amount ELSE 0 END) AS big_sales FROM sales GROUP BY category;",
            f"SELECT category, SUM(CASE WHEN amount >= {boundary} THEN amount ELSE 0 END) AS big_sales FROM sales GROUP BY category;",
            ["case"],
        ))

    for idx in range(10):
        cases.append(_case(
            "WINDOW",
            "instructor(ID, name, dept_name, salary)",
            "SELECT name, ROW_NUMBER() OVER (PARTITION BY dept_name ORDER BY salary DESC) AS rank FROM instructor;",
            "SELECT name, ROW_NUMBER() OVER (ORDER BY salary DESC) AS rank FROM instructor;",
            ["window-row-number"],
        ))

    return cases


def run_case(case: dict[str, Any]) -> dict[str, Any]:
    run = generate_and_compare(case["schema"], case["standard"], case["student"], max_rows_per_table=10)
    is_attack_success = run.is_equivalent is False
    attr = evidence_weights_from_observation(
        student_sql=case["student"],
        answer_sql=case["standard"],
        is_correct=bool(run.is_equivalent),
        error_message=run.error or run.data_evidence.get("student_exec_error"),
        judge_detail=run.data_evidence,
        mutation_detail=run.mutation_evidence,
    )
    kp_ids = [item.knowledge_point_id for item in attr.attributions]
    kp_hit = any(kp in kp_ids for kp in case["expected"])
    return {
        **case,
        "attack_success": is_attack_success,
        "kp_hit": kp_hit,
        "kp_ids": kp_ids,
        "is_equivalent": run.is_equivalent,
        "ast_diffs": run.data_evidence.get("ast_diffs", []),
        "generation_tactics": run.data_evidence.get("generation_tactics", []),
        "standard_row_count": run.data_evidence.get("standard_row_count"),
        "student_row_count": run.data_evidence.get("student_row_count"),
        "error": run.error,
    }


def main() -> None:
    results = [run_case(case) for case in build_cases()]
    by_group: dict[str, list[dict[str, Any]]] = {}
    for result in results:
        by_group.setdefault(result["group"], []).append(result)

    summary = []
    for group, items in sorted(by_group.items()):
        attack_pass = sum(1 for item in items if item["attack_success"])
        kp_pass = sum(1 for item in items if item["kp_hit"])
        summary.append({
            "group": group,
            "total": len(items),
            "attack_success": attack_pass,
            "kp_hit": kp_pass,
            "passed": attack_pass == len(items) and kp_pass == len(items),
        })
        print(f"{group}: attack={attack_pass}/{len(items)} kp={kp_pass}/{len(items)}")

    output = {
        "summary": summary,
        "total": len(results),
        "passed": all(item["passed"] for item in summary),
        "failures": [item for item in results if not item["attack_success"] or not item["kp_hit"]],
        "results": results,
    }
    out_path = PROJECT_ROOT / "data_construct_test" / "outputs" / "ast_diff_attack_stress_report.json"
    out_path.write_text(json.dumps(output, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"Stress report written to {out_path}")

    if not output["passed"]:
        raise SystemExit(f"{len(output['failures'])} attack stress cases failed")


if __name__ == "__main__":
    main()
