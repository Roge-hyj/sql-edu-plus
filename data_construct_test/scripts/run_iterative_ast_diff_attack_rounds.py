import argparse
import json
import random
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.append(str(PROJECT_ROOT / "sql-edu-backend"))
sys.path.append(str(PROJECT_ROOT / "data_construct_test" / "scripts"))

from core.error_attribution import evidence_weights_from_observation
from core.parseval_data_generator import generate_and_compare
from run_ast_diff_attack_stress_tests import build_cases


DEPTS = ["Math", "Physics", "Comp. Sci.", "History"]
YEARS = list(range(2015, 2023))


def _case(group: str, schema: str, standard: str, student: str, expected: list[str]) -> dict[str, Any]:
    return {
        "group": group,
        "schema": schema,
        "standard": standard,
        "student": student,
        "expected": expected,
    }


def random_single_case(rng: random.Random) -> dict[str, Any]:
    return rng.choice(build_cases())


def random_combo_case(rng: random.Random) -> dict[str, Any]:
    family = rng.choice([
        "JOIN_WHERE",
        "JOIN_GROUP",
        "GROUP_HAVING",
        "DISTINCT_ORDER",
        "SUBQUERY_NULL",
        "CTE_JOIN_WHERE",
        "CASE_GROUP",
        "WINDOW_ORDER",
        "SET_WHERE",
        "LIMIT_ORDER",
    ])
    boundary = rng.randint(1, 6)
    dept = rng.choice(DEPTS)
    year = rng.choice(YEARS)

    if family == "JOIN_WHERE":
        return _case(
            family,
            "student(ID, name, dept_name); advisor(s_ID, i_ID)",
            f"SELECT student.name FROM student JOIN advisor ON student.ID = advisor.s_ID WHERE student.dept_name = '{dept}';",
            f"SELECT student.name FROM student JOIN advisor ON student.ID = advisor.i_ID WHERE student.dept_name = '{dept}';",
            ["join-on", "where"],
        )
    if family == "JOIN_GROUP":
        return _case(
            family,
            "student(ID, name, dept_name); takes(ID, course_id)",
            "SELECT student.dept_name, COUNT(*) FROM student LEFT JOIN takes ON student.ID = takes.ID GROUP BY student.dept_name;",
            "SELECT student.dept_name, COUNT(*) FROM student INNER JOIN takes ON student.ID = takes.ID GROUP BY student.dept_name;",
            ["join-left", "group-by"],
        )
    if family == "GROUP_HAVING":
        return _case(
            family,
            "instructor(ID, name, dept_name, salary, building)",
            f"SELECT dept_name FROM instructor GROUP BY dept_name HAVING SUM(salary) > {boundary * 10000};",
            f"SELECT dept_name FROM instructor GROUP BY building HAVING SUM(salary) < {boundary * 10000};",
            ["group-by", "having"],
        )
    if family == "DISTINCT_ORDER":
        return _case(
            family,
            "course(course_id, title, dept_name, credits)",
            "SELECT DISTINCT dept_name FROM course ORDER BY dept_name ASC;",
            "SELECT dept_name FROM course ORDER BY dept_name DESC;",
            ["distinct", "order-by"],
        )
    if family == "SUBQUERY_NULL":
        return _case(
            family,
            "student(ID, name, grade); takes(ID, course_id, year)",
            f"SELECT name FROM student WHERE grade IS NULL AND ID IN (SELECT ID FROM takes WHERE year = {year});",
            f"SELECT name FROM student WHERE grade = NULL AND ID IN (SELECT ID FROM takes WHERE year = {year});",
            ["comp-null", "where", "subquery-in"],
        )
    if family == "CTE_JOIN_WHERE":
        return _case(
            family,
            "works(company_name, person_name, salary); company(company_name, city)",
            "WITH big_co AS (SELECT company_name FROM company WHERE city = 'Beijing') SELECT person_name FROM works JOIN big_co ON works.company_name = big_co.company_name WHERE salary > 10000;",
            "WITH big_co AS (SELECT company_name FROM company WHERE city = 'Beijing') SELECT person_name FROM works JOIN big_co ON works.company_name = big_co.company_name WHERE salary < 10000;",
            ["where", "cte"],
        )
    if family == "CASE_GROUP":
        return _case(
            family,
            "sales(sale_id, category, amount)",
            f"SELECT category, SUM(CASE WHEN amount > {boundary * 10} THEN amount ELSE 0 END) FROM sales GROUP BY category;",
            f"SELECT category, SUM(CASE WHEN amount >= {boundary * 10} THEN amount ELSE 0 END) FROM sales GROUP BY category;",
            ["case", "group-by"],
        )
    if family == "WINDOW_ORDER":
        return _case(
            family,
            "instructor(ID, name, dept_name, salary)",
            "SELECT name, ROW_NUMBER() OVER (PARTITION BY dept_name ORDER BY salary DESC) AS rank FROM instructor ORDER BY name ASC;",
            "SELECT name, ROW_NUMBER() OVER (ORDER BY salary DESC) AS rank FROM instructor ORDER BY name DESC;",
            ["window-row-number", "order-by"],
        )
    if family == "SET_WHERE":
        return _case(
            family,
            "course(course_id, title, dept_name, credits)",
            f"SELECT title FROM course WHERE dept_name = '{dept}' INTERSECT SELECT title FROM course WHERE credits > {boundary};",
            f"SELECT title FROM course WHERE dept_name = '{dept}' UNION SELECT title FROM course WHERE credits > {boundary};",
            ["intersect", "where", "union"],
        )
    return _case(
        family,
        "course(course_id, title, dept_name, credits)",
        f"SELECT title FROM course WHERE credits > {boundary} ORDER BY credits DESC LIMIT 3;",
        f"SELECT title FROM course WHERE credits >= {boundary} ORDER BY credits ASC LIMIT 5;",
        ["where", "order-by", "limit"],
    )


def random_case(rng: random.Random) -> dict[str, Any]:
    return random_combo_case(rng) if rng.random() < 0.4 else random_single_case(rng)


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
    attack_success = run.is_equivalent is False
    kp_hit = any(kp in kp_ids for kp in case["expected"])
    return {
        **case,
        "attack_success": attack_success,
        "kp_hit": kp_hit,
        "is_equivalent": run.is_equivalent,
        "kp_ids": kp_ids,
        "ast_diffs": run.data_evidence.get("ast_diffs", []),
        "generation_tactics": run.data_evidence.get("generation_tactics", []),
        "standard_row_count": run.data_evidence.get("standard_row_count"),
        "student_row_count": run.data_evidence.get("student_row_count"),
        "error": run.error,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rounds", type=int, default=100)
    parser.add_argument("--batch-size", type=int, default=200)
    parser.add_argument("--seed", type=int, default=20260706)
    args = parser.parse_args()

    rng = random.Random(args.seed)
    output_dir = PROJECT_ROOT / "data_construct_test" / "outputs"
    output_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = output_dir / "iterative_ast_diff_attack_cases.jsonl"
    summary_path = output_dir / "iterative_ast_diff_attack_summary.json"

    summaries: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    group_totals: dict[str, dict[str, int]] = {}

    with jsonl_path.open("w", encoding="utf-8") as writer:
        for round_idx in range(1, args.rounds + 1):
            round_results = [run_case(random_case(rng)) for _ in range(args.batch_size)]
            for result in round_results:
                writer.write(json.dumps({"round": round_idx, **result}, ensure_ascii=False) + "\n")
                group = result["group"]
                totals = group_totals.setdefault(group, {"total": 0, "attack": 0, "kp": 0})
                totals["total"] += 1
                totals["attack"] += int(result["attack_success"])
                totals["kp"] += int(result["kp_hit"])
            round_failures = [item for item in round_results if not item["attack_success"] or not item["kp_hit"]]
            failures.extend({"round": round_idx, **item} for item in round_failures)
            attack_count = sum(1 for item in round_results if item["attack_success"])
            kp_count = sum(1 for item in round_results if item["kp_hit"])
            round_summary = {
                "round": round_idx,
                "total": args.batch_size,
                "attack_success": attack_count,
                "kp_hit": kp_count,
                "failures": len(round_failures),
            }
            summaries.append(round_summary)
            print(
                f"round={round_idx:03d} attack={attack_count}/{args.batch_size} "
                f"kp={kp_count}/{args.batch_size} failures={len(round_failures)}"
            )

    total = args.rounds * args.batch_size
    attack_total = sum(item["attack_success"] for item in summaries)
    kp_total = sum(item["kp_hit"] for item in summaries)
    summary = {
        "rounds": args.rounds,
        "batch_size": args.batch_size,
        "total": total,
        "attack_success": attack_total,
        "kp_hit": kp_total,
        "failure_count": len(failures),
        "group_totals": group_totals,
        "rounds_detail": summaries,
        "failures": failures[:200],
        "cases_jsonl": str(jsonl_path),
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Summary written to {summary_path}")
    print(f"Cases written to {jsonl_path}")
    if failures:
        raise SystemExit(f"{len(failures)} iterative attack cases failed")


if __name__ == "__main__":
    main()
