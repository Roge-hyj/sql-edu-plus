import argparse
import json
import random
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.append(str(PROJECT_ROOT / "sql-edu-backend"))

from core.error_attribution import evidence_weights_from_observation
from core.parseval_data_generator import generate_and_compare


CaseFactory = Callable[[random.Random, int], dict[str, Any]]

OUTPUT_DIR = PROJECT_ROOT / "data_construct_test" / "outputs"

DEPTS = ["Comp. Sci.", "Math", "Physics", "History", "Biology"]
CITIES = ["Beijing", "Shanghai", "Shenzhen", "Nanjing"]
YEARS = list(range(2015, 2025))


def _case(
    operator: str,
    tactic: str,
    schema: str,
    standard: str,
    student: str,
    expected_kps: list[str],
    *,
    expect_equiv: bool = False,
    note: str = "",
) -> dict[str, Any]:
    return {
        "operator": operator,
        "tactic": tactic,
        "schema": schema,
        "standard": standard,
        "student": student,
        "expected_kps": expected_kps,
        "expect_equiv": expect_equiv,
        "note": note,
    }


def where_case(rng: random.Random, idx: int) -> dict[str, Any]:
    boundary = rng.randint(1, 120)
    col = rng.choice(["credits", "budget", "tot_cred"])
    if col == "credits":
        table = "course"
        schema = "course(course_id, title, dept_name, credits)"
        select_col = "title"
    elif col == "budget":
        table = "department"
        schema = "department(dept_name, building, budget)"
        select_col = "dept_name"
    else:
        table = "student"
        schema = "student(ID, name, dept_name, tot_cred)"
        select_col = "name"
    return _case(
        "WHERE",
        "comparison_boundary_tristate",
        schema,
        f"SELECT {select_col} FROM {table} WHERE {col} > {boundary};",
        f"SELECT {select_col} FROM {table} WHERE {col} >= {boundary};",
        ["where"],
        note="比较符边界 c-1/c/c+1",
    )


def null_case(rng: random.Random, idx: int) -> dict[str, Any]:
    col = rng.choice(["grade", "advisor", "dept_name"])
    return _case(
        "NULL",
        "null_probe",
        f"student(ID, name, {col})",
        f"SELECT name FROM student WHERE {col} IS NULL;",
        f"SELECT name FROM student WHERE {col} = NULL;",
        ["comp-null"],
        note="NULL 与非 NULL 数据",
    )


def select_case(rng: random.Random, idx: int) -> dict[str, Any]:
    variants = [
        ("title, credits", "title"),
        ("course_id, title", "title"),
        ("title, credits, dept_name", "title, credits"),
        ("credits * 2", "credits"),
    ]
    std_cols, stu_cols = rng.choice(variants)
    return _case(
        "SELECT",
        "projection_shape_check",
        "course(course_id, title, dept_name, credits)",
        f"SELECT {std_cols} FROM course WHERE credits > {rng.randint(1, 5)};",
        f"SELECT {stu_cols} FROM course WHERE credits > {rng.randint(1, 5)};",
        ["select-basic"],
        note="投影列数、顺序或表达式不一致",
    )


def distinct_case(rng: random.Random, idx: int) -> dict[str, Any]:
    col = rng.choice(["dept_name", "course_id", "semester", "grade"])
    schema = "takes(ID, course_id, sec_id, semester, year, grade)" if col != "dept_name" else "course(course_id, title, dept_name, credits)"
    table = "takes" if col != "dept_name" else "course"
    return _case(
        "DISTINCT",
        "duplicate_projection_probe",
        schema,
        f"SELECT DISTINCT {col} FROM {table};",
        f"SELECT {col} FROM {table};",
        ["distinct"],
        note="重复投影行",
    )


def join_topology_case(rng: random.Random, idx: int) -> dict[str, Any]:
    return _case(
        "JOIN_TOPOLOGY",
        "join_topology_alignment",
        "student(ID, name, dept_name); takes(ID, course_id); course(course_id, title, dept_name)",
        "SELECT student.name, course.title FROM student JOIN takes ON student.ID = takes.ID JOIN course ON takes.course_id = course.course_id;",
        "SELECT student.name, course.title FROM student, takes, course;",
        ["join-on"],
        note="笛卡尔积与拓扑连接",
    )


def join_drift_case(rng: random.Random, idx: int) -> dict[str, Any]:
    return _case(
        "JOIN_DRIFT",
        "join_key_drift_probe",
        "student(ID, name, dept_name); advisor(s_ID, i_ID)",
        "SELECT student.name FROM student JOIN advisor ON student.ID = advisor.s_ID;",
        "SELECT student.name FROM student JOIN advisor ON student.ID = advisor.i_ID;",
        ["join-on"],
        note="s_ID/i_ID 同组键错位",
    )


def left_join_case(rng: random.Random, idx: int) -> dict[str, Any]:
    return _case(
        "LEFT_JOIN",
        "outer_join_dangling_tuple_probe",
        "student(ID, name, dept_name); takes(ID, course_id)",
        "SELECT student.name, takes.course_id FROM student LEFT JOIN takes ON student.ID = takes.ID;",
        "SELECT student.name, takes.course_id FROM student INNER JOIN takes ON student.ID = takes.ID;",
        ["join-left"],
        note="无匹配右表行",
    )


def group_by_case(rng: random.Random, idx: int) -> dict[str, Any]:
    return _case(
        "GROUP_BY",
        "group_cardinality_probe",
        "instructor(ID, name, dept_name, salary, building)",
        "SELECT dept_name, SUM(salary) FROM instructor GROUP BY dept_name;",
        "SELECT dept_name, SUM(salary) FROM instructor GROUP BY building;",
        ["group-by"],
        note="分组键替换",
    )


def having_agg_case(rng: random.Random, idx: int) -> dict[str, Any]:
    agg, boundary = rng.choice([("SUM", 80000), ("AVG", 50000), ("MIN", 30000), ("MAX", 90000)])
    return _case(
        "HAVING_AGG",
        "aggregate_boundary_probe",
        "instructor(ID, name, dept_name, salary)",
        f"SELECT dept_name FROM instructor GROUP BY dept_name HAVING {agg}(salary) > {boundary};",
        f"SELECT dept_name FROM instructor GROUP BY dept_name HAVING {agg}(salary) < {boundary};",
        ["having"],
        note=f"{agg} 聚合三态边界",
    )


def having_count_case(rng: random.Random, idx: int) -> dict[str, Any]:
    boundary = rng.randint(2, 4)
    return _case(
        "HAVING_COUNT",
        "count_group_size_probe",
        "student(ID, name, dept_name, tot_cred)",
        f"SELECT dept_name FROM student GROUP BY dept_name HAVING COUNT(*) >= {boundary};",
        f"SELECT dept_name FROM student GROUP BY dept_name HAVING COUNT(*) > {boundary};",
        ["having"],
        note="COUNT 组大小边界",
    )


def order_by_case(rng: random.Random, idx: int) -> dict[str, Any]:
    direction = rng.choice([("ASC", "DESC"), ("DESC", "ASC")])
    return _case(
        "ORDER_BY",
        "ordered_compare_probe",
        "course(course_id, title, dept_name, credits)",
        f"SELECT title, credits FROM course ORDER BY credits {direction[0]};",
        f"SELECT title, credits FROM course ORDER BY credits {direction[1]};",
        ["order-by"],
        note="有序精确比较",
    )


def limit_case(rng: random.Random, idx: int) -> dict[str, Any]:
    limit = rng.randint(1, 5)
    offset = rng.randint(0, 2)
    return _case(
        "LIMIT_OFFSET",
        "limit_row_count_probe",
        "course(course_id, title, dept_name, credits)",
        f"SELECT title FROM course ORDER BY credits DESC LIMIT {limit} OFFSET {offset};",
        f"SELECT title FROM course ORDER BY credits DESC LIMIT {limit + 2} OFFSET {offset};",
        ["limit"],
        note="LIMIT/OFFSET 行数边界",
    )


def subquery_case(rng: random.Random, idx: int) -> dict[str, Any]:
    year = rng.choice(YEARS)
    return _case(
        "SUBQUERY",
        "subquery_value_overlap_probe",
        "student(ID, name, dept_name); takes(ID, course_id, year)",
        f"SELECT name FROM student WHERE ID IN (SELECT ID FROM takes WHERE year = {year});",
        f"SELECT name FROM student WHERE ID NOT IN (SELECT ID FROM takes WHERE year = {year});",
        ["where", "subquery-in"],
        note="父子表 ID 值域重合",
    )


def correlated_subquery_case(rng: random.Random, idx: int) -> dict[str, Any]:
    year = rng.choice(YEARS)
    return _case(
        "CORRELATED_SUBQUERY",
        "correlated_subquery_probe",
        "student(ID, name, dept_name); takes(ID, course_id, year)",
        f"SELECT name FROM student s WHERE EXISTS (SELECT 1 FROM takes t WHERE t.ID = s.ID AND t.year = {year});",
        f"SELECT name FROM student s WHERE EXISTS (SELECT 1 FROM takes t WHERE t.ID = s.ID AND t.year = {year + 1});",
        ["subquery-correlated"],
        note="相关列交叉与内层过滤边界",
    )


def set_operator_case(rng: random.Random, idx: int) -> dict[str, Any]:
    dept = rng.choice(DEPTS)
    credits = rng.randint(1, 6)
    op = rng.choice(["UNION", "INTERSECT", "EXCEPT"])
    if op == "UNION":
        return _case(
            "SET_OPERATOR",
            "set_operator_overlap_probe",
            "course(course_id, title, dept_name, credits)",
            f"SELECT title FROM course WHERE dept_name = '{dept}' UNION SELECT title FROM course WHERE credits > {credits};",
            f"SELECT title FROM course WHERE dept_name = '{dept}' UNION ALL SELECT title FROM course WHERE credits > {credits};",
            ["union"],
            note="UNION 去重与 UNION ALL",
        )
    if op == "INTERSECT":
        return _case(
            "SET_OPERATOR",
            "set_operator_overlap_probe",
            "course(course_id, title, dept_name, credits)",
            f"SELECT title FROM course WHERE dept_name = '{dept}' INTERSECT SELECT title FROM course WHERE credits > {credits};",
            f"SELECT title FROM course WHERE dept_name = '{dept}' UNION SELECT title FROM course WHERE credits > {credits};",
            ["intersect"],
            note="交集与并集差异",
        )
    return _case(
        "SET_OPERATOR",
        "set_operator_overlap_probe",
        "course(course_id, title, dept_name, credits)",
        f"SELECT title FROM course EXCEPT SELECT title FROM course WHERE dept_name = '{dept}';",
        "SELECT title FROM course;",
        ["except"],
        note="差集排他数据",
    )


def case_when_case(rng: random.Random, idx: int) -> dict[str, Any]:
    boundary = rng.randint(50, 150)
    return _case(
        "CASE_WHEN",
        "case_boundary_probe",
        "sales(sale_id, category, amount)",
        f"SELECT category, SUM(CASE WHEN amount > {boundary} THEN amount ELSE 0 END) FROM sales GROUP BY category;",
        f"SELECT category, SUM(CASE WHEN amount >= {boundary} THEN amount ELSE 0 END) FROM sales GROUP BY category;",
        ["case"],
        note="CASE WHEN 分支边界",
    )


def window_case(rng: random.Random, idx: int) -> dict[str, Any]:
    return _case(
        "WINDOW",
        "window_partition_order_probe",
        "instructor(ID, name, dept_name, salary)",
        "SELECT name, ROW_NUMBER() OVER (PARTITION BY dept_name ORDER BY salary DESC) AS rn FROM instructor;",
        "SELECT name, ROW_NUMBER() OVER (ORDER BY salary DESC) AS rn FROM instructor;",
        ["window-row-number"],
        note="窗口分区遗漏",
    )


def cte_case(rng: random.Random, idx: int) -> dict[str, Any]:
    boundary = rng.randint(9000, 15000)
    city = rng.choice(CITIES)
    return _case(
        "CTE",
        "cte_base_constraint_probe",
        "works(company_name, person_name, salary); company(company_name, city)",
        f"WITH co AS (SELECT company_name FROM company WHERE city = '{city}') SELECT person_name FROM works JOIN co ON works.company_name = co.company_name WHERE salary > {boundary};",
        f"WITH co AS (SELECT company_name FROM company WHERE city = '{city}') SELECT person_name FROM works JOIN co ON works.company_name = co.company_name WHERE salary < {boundary};",
        ["where", "cte"],
        note="CTE 基表约束传递",
    )


def recursive_cte_case(rng: random.Random, idx: int) -> dict[str, Any]:
    limit = rng.randint(3, 8)
    return _case(
        "RECURSIVE_CTE",
        "recursive_cte_boundary_probe",
        "dummy(id)",
        f"WITH RECURSIVE nums AS (SELECT 1 AS n UNION ALL SELECT n + 1 AS n FROM nums WHERE n < {limit}) SELECT n FROM nums;",
        f"WITH RECURSIVE nums AS (SELECT 1 AS n UNION ALL SELECT n + 1 AS n FROM nums WHERE n < {limit + 2}) SELECT n FROM nums;",
        ["cte-recursive"],
        note="递归终止边界",
    )


def positive_equivalence_case(rng: random.Random, idx: int) -> dict[str, Any]:
    family = rng.choice(["BETWEEN", "IN_OR", "INNER_JOIN", "CTE_INLINE", "GROUP_ORDER"])
    if family == "BETWEEN":
        return _case(
            "POSITIVE_EQUIV",
            "false_positive_guard",
            "student(ID, name, dept_name, tot_cred)",
            "SELECT name FROM student WHERE tot_cred BETWEEN 90 AND 120;",
            "SELECT name FROM student WHERE tot_cred >= 90 AND tot_cred <= 120;",
            [],
            expect_equiv=True,
            note="BETWEEN 等价展开",
        )
    if family == "IN_OR":
        return _case(
            "POSITIVE_EQUIV",
            "false_positive_guard",
            "student(ID, name, dept_name, tot_cred)",
            "SELECT name FROM student WHERE dept_name IN ('Comp. Sci.', 'Math');",
            "SELECT name FROM student WHERE dept_name = 'Comp. Sci.' OR dept_name = 'Math';",
            [],
            expect_equiv=True,
            note="IN 与 OR 等价",
        )
    if family == "INNER_JOIN":
        return _case(
            "POSITIVE_EQUIV",
            "false_positive_guard",
            "student(ID, name); advisor(s_ID, i_ID)",
            "SELECT s.name FROM student s JOIN advisor a ON s.ID = a.s_ID;",
            "SELECT s.name FROM student s INNER JOIN advisor a ON s.ID = a.s_ID;",
            [],
            expect_equiv=True,
            note="JOIN 与 INNER JOIN 等价",
        )
    if family == "CTE_INLINE":
        return _case(
            "POSITIVE_EQUIV",
            "false_positive_guard",
            "instructor(ID, name, dept_name, salary)",
            "WITH avg_sal AS (SELECT AVG(salary) AS v FROM instructor) SELECT name FROM instructor, avg_sal WHERE salary > avg_sal.v;",
            "SELECT name FROM instructor WHERE salary > (SELECT AVG(salary) FROM instructor);",
            [],
            expect_equiv=True,
            note="CTE 内联等价",
        )
    return _case(
        "POSITIVE_EQUIV",
        "false_positive_guard",
        "department(dept_name, building, budget)",
        "SELECT dept_name, building, COUNT(*) FROM department GROUP BY dept_name, building;",
        "SELECT dept_name, building, COUNT(*) FROM department GROUP BY building, dept_name;",
        [],
        expect_equiv=True,
        note="GROUP BY 列顺序等价",
    )


FACTORIES: list[CaseFactory] = [
    where_case,
    null_case,
    select_case,
    distinct_case,
    join_topology_case,
    join_drift_case,
    left_join_case,
    group_by_case,
    having_agg_case,
    having_count_case,
    order_by_case,
    limit_case,
    subquery_case,
    correlated_subquery_case,
    set_operator_case,
    case_when_case,
    window_case,
    cte_case,
    recursive_cte_case,
]


def _norm_kp(value: str) -> str:
    return value.lower().replace(" ", "-").replace("_", "-")


def _kp_hit(actual: list[str], expected: list[str]) -> bool:
    if not expected:
        return True
    aliases = {
        "join": {"join-on", "join-inner", "join-left", "join-right", "join-full"},
        "window": {"window-row-number"},
        "recursive": {"cte-recursive"},
        "null": {"comp-null"},
        "subquery": {"subquery-scalar", "subquery-in", "subquery-exists", "subquery-correlated"},
        "aggregate": {"agg-count", "having"},
    }
    actual_norm = {_norm_kp(item) for item in actual}
    for expected_item in expected:
        expected_norm = _norm_kp(expected_item)
        if actual_norm & aliases.get(expected_norm, {expected_norm}):
            return True
    return False


def _diff_types(diffs: list[dict[str, Any]]) -> list[str]:
    return [str(item.get("diff_type")) for item in diffs if item.get("diff_type")]


def _tactics(tactics: list[dict[str, Any]]) -> list[str]:
    return [str(item.get("tactic")) for item in tactics if item.get("tactic")]


def classify_result(case: dict[str, Any], run: Any, kp_ids: list[str]) -> tuple[str, str]:
    if run.error:
        return "ERROR", run.error
    if case["expect_equiv"]:
        if run.is_equivalent is True:
            return "PASS", "positive sample kept equivalent"
        return "FALSE_POSITIVE", "equivalent SQL pair judged non-equivalent"

    if run.is_equivalent is True:
        return "MISS_EQUIV_TRUE", "negative sample judged equivalent"
    if run.is_equivalent is not False:
        return "ERROR", "no equivalence verdict"
    if not _kp_hit(kp_ids, case["expected_kps"]):
        return "ATTRIBUTION_MISS", f"expected={case['expected_kps']} actual={kp_ids}"
    if not run.data_evidence.get("generation_tactics"):
        return "TACTIC_MISS", "no generation tactic was recorded"
    if not run.mutation_evidence.get("summary", {}).get("executed", 0):
        return "MUTATION_GAP", "no mutation test executed after counterexample"
    return "PASS", "counterexample, attribution and mutation evidence all present"


def run_case(case: dict[str, Any]) -> dict[str, Any]:
    run = generate_and_compare(case["schema"], case["standard"], case["student"], max_rows_per_table=10)
    attr = evidence_weights_from_observation(
        student_sql=case["student"],
        answer_sql=case["standard"],
        is_correct=bool(run.is_equivalent),
        error_message=run.error or run.data_evidence.get("student_exec_error"),
        judge_detail=run.data_evidence,
        mutation_detail=run.mutation_evidence,
    )
    kp_ids = [item.knowledge_point_id for item in attr.attributions]
    status, reason = classify_result(case, run, kp_ids)
    return {
        **case,
        "status": status,
        "reason": reason,
        "is_equivalent": run.is_equivalent,
        "kp_ids": kp_ids,
        "kp_hit": _kp_hit(kp_ids, case["expected_kps"]),
        "standard_row_count": run.data_evidence.get("standard_row_count"),
        "student_row_count": run.data_evidence.get("student_row_count"),
        "ast_diff_types": _diff_types(run.data_evidence.get("ast_diffs", [])),
        "generation_tactics": _tactics(run.data_evidence.get("generation_tactics", [])),
        "mutation_summary": run.mutation_evidence.get("summary"),
        "standard_rows_sample": run.standard_rows[:5],
        "student_rows_sample": run.student_rows[:5],
        "generated_rows": run.test_database,
        "error": run.error or run.data_evidence.get("student_exec_error"),
    }


def build_cases(rng: random.Random, cases_per_operator: int, positives: int) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for factory in FACTORIES:
        for idx in range(cases_per_operator):
            cases.append(factory(rng, idx))
    for idx in range(positives):
        cases.append(positive_equivalence_case(rng, idx))
    rng.shuffle(cases)
    return cases


def render_markdown(results: list[dict[str, Any]], summary: dict[str, Any]) -> str:
    lines = [
        "# 端到端健壮性 Fuzzer 报告",
        "",
        f"- 总用例：{summary['total']}",
        f"- PASS：{summary['status_counts'].get('PASS', 0)}",
        f"- 非 PASS：{summary['total'] - summary['status_counts'].get('PASS', 0)}",
        f"- 通过率：{summary['pass_rate']:.1f}%",
        "",
        "| 状态 | 算子 | tactic | 等价 | KP | 原因 |",
        "| :--- | :--- | :--- | :--- | :--- | :--- |",
    ]
    for item in results:
        lines.append(
            f"| `{item['status']}` | {item['operator']} | `{item['tactic']}` | `{item['is_equivalent']}` | "
            f"`{', '.join(item['kp_ids'])}` | {item['reason']} |"
        )

    failures = [item for item in results if item["status"] != "PASS"]
    if failures:
        lines.extend(["", "## 失败详情", ""])
        for item in failures[:80]:
            lines.extend(
                [
                    f"### {item['operator']} / {item['tactic']}",
                    f"- 状态：`{item['status']}`",
                    f"- 原因：{item['reason']}",
                    f"- 标答：`{item['standard']}`",
                    f"- 学生：`{item['student']}`",
                    f"- AST diff types：`{item['ast_diff_types']}`",
                    f"- 造数 tactics：`{item['generation_tactics']}`",
                    f"- mutation：`{item['mutation_summary']}`",
                    "- 生成数据：",
                    "```json",
                    json.dumps(item["generated_rows"], ensure_ascii=False, indent=2),
                    "```",
                ]
            )
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="Fuzz the AST -> data generation -> sandbox -> mutation -> attribution loop.")
    parser.add_argument("--cases-per-operator", type=int, default=8)
    parser.add_argument("--positive-cases", type=int, default=20)
    parser.add_argument("--seed", type=int, default=20260706)
    parser.add_argument("--fail-on-non-pass", action="store_true")
    args = parser.parse_args()

    rng = random.Random(args.seed)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    results = [run_case(case) for case in build_cases(rng, args.cases_per_operator, args.positive_cases)]

    status_counts: dict[str, int] = defaultdict(int)
    operator_counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    tactic_counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for result in results:
        status_counts[result["status"]] += 1
        operator_counts[result["operator"]][result["status"]] += 1
        tactic_counts[result["tactic"]][result["status"]] += 1

    total = len(results)
    pass_count = status_counts.get("PASS", 0)
    summary = {
        "seed": args.seed,
        "cases_per_operator": args.cases_per_operator,
        "positive_cases": args.positive_cases,
        "total": total,
        "status_counts": dict(status_counts),
        "operator_counts": {key: dict(value) for key, value in operator_counts.items()},
        "tactic_counts": {key: dict(value) for key, value in tactic_counts.items()},
        "pass_rate": pass_count / total * 100 if total else 0.0,
    }
    payload = {"summary": summary, "results": results}
    json_path = OUTPUT_DIR / "e2e_robustness_fuzzer_report.json"
    md_path = OUTPUT_DIR / "e2e_robustness_fuzzer_report.md"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(render_markdown(results, summary), encoding="utf-8")

    print(f"total={total} pass={pass_count} pass_rate={summary['pass_rate']:.1f}%")
    for status, count in sorted(status_counts.items()):
        print(f"{status}: {count}")
    print(f"JSON report: {json_path}")
    print(f"Markdown report: {md_path}")

    if args.fail_on_non_pass and pass_count != total:
        raise SystemExit(f"{total - pass_count} fuzzer cases did not pass")


if __name__ == "__main__":
    main()
