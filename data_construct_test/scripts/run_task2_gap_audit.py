import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.append(str(PROJECT_ROOT / "sql-edu-backend"))

from core.error_attribution import evidence_weights_from_observation
from core.parseval_data_generator import generate_and_compare


AUDIT_CASES = [
    {
        "name": "SetOp - UNION vs UNION ALL 去重模式错",
        "schema": "course(course_id, title, dept_name, credits)",
        "standard": "SELECT title FROM course WHERE dept_name = 'Math' UNION SELECT title FROM course WHERE dept_name = 'Physics';",
        "student": "SELECT title FROM course WHERE dept_name = 'Math' UNION ALL SELECT title FROM course WHERE dept_name = 'Physics';",
        "expected_kp": "union",
    },
    {
        "name": "SetOp - INTERSECT 错写为 UNION",
        "schema": "course(course_id, title, dept_name, credits)",
        "standard": "SELECT title FROM course WHERE dept_name = 'Math' INTERSECT SELECT title FROM course WHERE credits > 3;",
        "student": "SELECT title FROM course WHERE dept_name = 'Math' UNION SELECT title FROM course WHERE credits > 3;",
        "expected_kp": "intersect",
    },
    {
        "name": "SetOp - EXCEPT 差集缺失",
        "schema": "course(course_id, title, dept_name, credits)",
        "standard": "SELECT title FROM course EXCEPT SELECT title FROM course WHERE dept_name = 'Physics';",
        "student": "SELECT title FROM course;",
        "expected_kp": "except",
    },
    {
        "name": "Window - OVER 缺少 PARTITION BY",
        "schema": "instructor(ID, name, dept_name, salary)",
        "standard": "SELECT name, ROW_NUMBER() OVER (PARTITION BY dept_name ORDER BY salary DESC) AS rank FROM instructor;",
        "student": "SELECT name, ROW_NUMBER() OVER (ORDER BY salary DESC) AS rank FROM instructor;",
        "expected_kp": "window-row-number",
    },
    {
        "name": "Join - NATURAL JOIN 隐式多键误用",
        "schema": "student(ID, name, dept_name); advisor(ID, i_ID, dept_name)",
        "standard": "SELECT student.name FROM student JOIN advisor ON student.ID = advisor.ID;",
        "student": "SELECT student.name FROM student NATURAL JOIN advisor;",
        "expected_kp": "join-on",
    },
    {
        "name": "Distinct - 缺少 DISTINCT",
        "schema": "takes(ID, course_id, sec_id, semester, year, grade)",
        "standard": "SELECT DISTINCT course_id FROM takes;",
        "student": "SELECT course_id FROM takes;",
        "expected_kp": "distinct",
    },
    {
        "name": "Having - SUM 聚合边界方向错",
        "schema": "instructor(ID, name, dept_name, salary)",
        "standard": "SELECT dept_name FROM instructor GROUP BY dept_name HAVING SUM(salary) > 80000;",
        "student": "SELECT dept_name FROM instructor GROUP BY dept_name HAVING SUM(salary) < 80000;",
        "expected_kp": "having",
    },
]


def run_case(case: dict) -> dict:
    run = generate_and_compare(
        schema_text=case["schema"],
        standard_sql=case["standard"],
        student_sql=case["student"],
    )
    is_correct = bool(run.is_equivalent)
    if run.error:
        is_correct = False
    error_msg = run.error or run.data_evidence.get("student_exec_error")
    attr_res = evidence_weights_from_observation(
        student_sql=case["student"],
        answer_sql=case["standard"],
        is_correct=is_correct,
        error_message=error_msg,
        judge_detail=run.data_evidence,
        mutation_detail=run.mutation_evidence,
    )
    kp_ids = [item.knowledge_point_id for item in attr_res.attributions]
    return {
        "case": case,
        "is_correct": is_correct,
        "expected_detected": (not is_correct) and case["expected_kp"] in kp_ids,
        "kp_ids": kp_ids,
        "data_evidence": run.data_evidence,
        "mutation_summary": run.mutation_evidence.get("summary") if run.mutation_evidence else None,
        "standard_rows": run.standard_rows[:5],
        "student_rows": run.student_rows[:5],
    }


def generate_report(results: list[dict]) -> None:
    lines = [
        "# Task2 数据检测与空归因专项回归审计",
        "",
        "本报告专门覆盖此前容易出现“沙盒已检测出不等价，但 Attributions 为空”的场景。",
        "每个用例都由系统动态造数、沙盒执行，并检查期望知识点是否进入归因结果。",
        "",
        "| 用例 | 沙盒等价 | 期望 KP | 实际 KP | 检测通过 | 标准/学生行数 |",
        "| :--- | :--- | :--- | :--- | :--- | :--- |",
    ]
    for result in results:
        case = result["case"]
        evidence = result["data_evidence"]
        lines.append(
            f"| {case['name']} | `{result['is_correct']}` | `{case['expected_kp']}` | "
            f"`{', '.join(result['kp_ids'])}` | `{'PASS' if result['expected_detected'] else 'FAIL'}` | "
            f"`{evidence.get('standard_row_count')} / {evidence.get('student_row_count')}` |"
        )
    lines.extend(["", "## 详细样本"])
    for result in results:
        case = result["case"]
        lines.extend(
            [
                f"\n### {case['name']}",
                f"* Schema: `{case['schema']}`",
                "* 标准 SQL:",
                f"```sql\n{case['standard']}\n```",
                "* 学生 SQL:",
                f"```sql\n{case['student']}\n```",
                f"* 数据证据: `{json.dumps(result['data_evidence'], ensure_ascii=False)}`",
                f"* 标准输出样本: `{result['standard_rows']}`",
                f"* 学生输出样本: `{result['student_rows']}`",
                f"* 归因 KP: `{result['kp_ids']}`",
            ]
        )
    out_file = PROJECT_ROOT / "task" / "task2_gap_audit.md"
    out_file.write_text("\n".join(lines), encoding="utf-8")
    print(f"Audit report written to {out_file}")


def main() -> None:
    results = [run_case(case) for case in AUDIT_CASES]
    failed = [result for result in results if not result["expected_detected"]]
    generate_report(results)
    for result in results:
        case = result["case"]
        print(f"{case['name']}: eq={result['is_correct']} kp={result['kp_ids']} pass={result['expected_detected']}")
    if failed:
        raise SystemExit(f"{len(failed)} audit cases failed")


if __name__ == "__main__":
    main()
