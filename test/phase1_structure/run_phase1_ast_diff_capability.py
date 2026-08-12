"""Phase 1 AST diff capability benchmark.

This script evaluates whether extract_ast_diffs can point to structural
differences between reference SQL and student SQL.
"""

from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = next(
    parent for parent in Path(__file__).resolve().parents
    if (parent / "sql-edu-backend").exists()
)
BACKEND_ROOT = PROJECT_ROOT / "sql-edu-backend"
OUTPUT_DIR = Path(__file__).resolve().parent / "outputs"
sys.path.append(str(BACKEND_ROOT))

from core.parseval_data_generator import extract_ast_diffs


def _case(
    case_id: str,
    category: str,
    standard: str,
    student: str,
    *,
    expected_clauses: list[str] | None = None,
    expected_diff_types: list[str] | None = None,
    expected_kps: list[str] | None = None,
    representation: str = "supported",
    note: str = "",
) -> dict[str, Any]:
    return {
        "id": case_id,
        "category": category,
        "standard": standard,
        "student": student,
        "expected_clauses": expected_clauses or [],
        "expected_diff_types": expected_diff_types or [],
        "expected_kps": expected_kps or [],
        "representation": representation,
        "note": note,
    }


def build_cases() -> list[dict[str, Any]]:
    return [
        _case(
            "select_column_dropped",
            "SELECT",
            "SELECT name, age FROM student",
            "SELECT name FROM student",
            expected_clauses=["SELECT"],
            expected_diff_types=["column_dropped"],
        ),
        _case(
            "select_column_added",
            "SELECT",
            "SELECT name FROM student",
            "SELECT name, age FROM student",
            expected_clauses=["SELECT"],
            expected_diff_types=["column_added"],
        ),
        _case(
            "select_expression_changed",
            "SELECT",
            "SELECT salary * 1.1 AS adjusted FROM instructor",
            "SELECT salary AS adjusted FROM instructor",
            expected_clauses=["SELECT"],
            expected_diff_types=["projection_changed"],
        ),
        _case(
            "select_order_changed",
            "SELECT",
            "SELECT name, dept FROM student",
            "SELECT dept, name FROM student",
            expected_clauses=["SELECT"],
            expected_diff_types=["projection_changed"],
        ),
        _case(
            "distinct_missing",
            "DISTINCT",
            "SELECT DISTINCT dept FROM student",
            "SELECT dept FROM student",
            expected_clauses=["DISTINCT"],
            expected_diff_types=["distinct_changed"],
        ),
        _case(
            "count_distinct_missing",
            "DISTINCT",
            "SELECT COUNT(DISTINCT dept) FROM student",
            "SELECT COUNT(dept) FROM student",
            expected_clauses=["DISTINCT"],
            expected_diff_types=["distinct_changed"],
        ),
        _case(
            "where_missing",
            "WHERE",
            "SELECT name FROM student WHERE age > 18",
            "SELECT name FROM student",
            expected_clauses=["WHERE"],
            expected_diff_types=["where_changed", "predicate_missing"],
        ),
        _case(
            "where_extra",
            "WHERE",
            "SELECT name FROM student",
            "SELECT name FROM student WHERE age > 18",
            expected_clauses=["WHERE"],
            expected_diff_types=["where_changed", "predicate_added"],
        ),
        _case(
            "comparison_gt_to_gte",
            "Comparison",
            "SELECT * FROM t WHERE age > 18",
            "SELECT * FROM t WHERE age >= 18",
            expected_clauses=["PREDICATE"],
            expected_diff_types=["comparison_operator_changed"],
        ),
        _case(
            "comparison_literal_changed",
            "Comparison",
            "SELECT * FROM t WHERE age = 18",
            "SELECT * FROM t WHERE age = 20",
            expected_clauses=["PREDICATE"],
            expected_diff_types=["literal_changed"],
        ),
        _case(
            "comparison_column_changed",
            "Comparison",
            "SELECT * FROM t WHERE start_date <= end_date",
            "SELECT * FROM t WHERE start_date < end_date",
            expected_clauses=["PREDICATE"],
            expected_diff_types=["comparison_operator_changed"],
        ),
        _case(
            "null_is_null_to_equals_null",
            "NULL",
            "SELECT * FROM student WHERE advisor_id IS NULL",
            "SELECT * FROM student WHERE advisor_id = NULL",
            expected_clauses=["PREDICATE"],
            expected_diff_types=["comparison_operator_changed", "null_equality_changed"],
        ),
        _case(
            "null_is_null_to_is_not_null",
            "NULL",
            "SELECT * FROM student WHERE advisor_id IS NULL",
            "SELECT * FROM student WHERE advisor_id IS NOT NULL",
            expected_clauses=["WHERE"],
            expected_diff_types=["where_changed"],
        ),
        _case(
            "in_list_member_removed",
            "IN/BETWEEN/LIKE",
            "SELECT * FROM course WHERE dept IN ('CS', 'Math', 'Bio')",
            "SELECT * FROM course WHERE dept IN ('CS', 'Math')",
            expected_clauses=["PREDICATE"],
            expected_diff_types=["in_list_member_removed"],
        ),
        _case(
            "between_boundary_changed",
            "IN/BETWEEN/LIKE",
            "SELECT * FROM course WHERE credits BETWEEN 2 AND 4",
            "SELECT * FROM course WHERE credits BETWEEN 3 AND 4",
            expected_clauses=["PREDICATE"],
            expected_diff_types=["literal_changed"],
        ),
        _case(
            "like_pattern_changed",
            "IN/BETWEEN/LIKE",
            "SELECT * FROM course WHERE title LIKE 'Intro%'",
            "SELECT * FROM course WHERE title LIKE 'Advanced%'",
            expected_clauses=["PREDICATE"],
            expected_diff_types=["literal_changed"],
        ),
        _case(
            "logic_and_to_or",
            "Logic",
            "SELECT * FROM t WHERE a = 1 AND b = 2",
            "SELECT * FROM t WHERE a = 1 OR b = 2",
            expected_clauses=["LOGICAL"],
            expected_diff_types=["logical_operator_changed"],
        ),
        _case(
            "logic_not_removed",
            "Logic",
            "SELECT * FROM t WHERE NOT (a = 1)",
            "SELECT * FROM t WHERE a = 1",
            expected_clauses=["WHERE"],
            expected_diff_types=["where_changed"],
        ),
        _case(
            "join_missing",
            "JOIN",
            "SELECT s.name FROM student s JOIN takes t ON s.id = t.s_id",
            "SELECT s.name FROM student s",
            expected_clauses=["JOIN"],
            expected_diff_types=["join_missing"],
        ),
        _case(
            "join_type_left_to_inner",
            "JOIN",
            "SELECT s.name FROM student s LEFT JOIN takes t ON s.id = t.s_id",
            "SELECT s.name FROM student s JOIN takes t ON s.id = t.s_id",
            expected_clauses=["JOIN_TYPE"],
            expected_diff_types=["join_type_changed"],
        ),
        _case(
            "join_on_key_changed",
            "JOIN ON",
            "SELECT s.name FROM student s JOIN advisor a ON s.id = a.s_id",
            "SELECT s.name FROM student s JOIN advisor a ON s.id = a.i_id",
            expected_clauses=["JOIN ON"],
            expected_diff_types=["join_on_changed"],
        ),
        _case(
            "join_on_predicate_removed",
            "JOIN ON",
            "SELECT * FROM a JOIN b ON a.id = b.a_id AND b.score > 10",
            "SELECT * FROM a JOIN b ON a.id = b.a_id",
            expected_clauses=["JOIN ON"],
            expected_diff_types=["join_on_changed"],
        ),
        _case(
            "group_by_column_changed",
            "GROUP BY",
            "SELECT dept, COUNT(*) FROM student GROUP BY dept",
            "SELECT year, COUNT(*) FROM student GROUP BY year",
            expected_clauses=["SELECT", "GROUP BY"],
            expected_diff_types=["projection_changed", "group_by_changed"],
        ),
        _case(
            "group_by_column_missing",
            "GROUP BY",
            "SELECT dept, year, COUNT(*) FROM takes GROUP BY dept, year",
            "SELECT dept, COUNT(*) FROM takes GROUP BY dept",
            expected_clauses=["SELECT", "GROUP BY"],
            expected_diff_types=["column_dropped", "group_by_changed"],
        ),
        _case(
            "having_operator_changed",
            "HAVING",
            "SELECT dept FROM student GROUP BY dept HAVING COUNT(*) > 3",
            "SELECT dept FROM student GROUP BY dept HAVING COUNT(*) >= 3",
            expected_clauses=["PREDICATE"],
            expected_diff_types=["comparison_operator_changed"],
        ),
        _case(
            "aggregate_sum_to_avg",
            "Aggregate",
            "SELECT SUM(salary) FROM instructor",
            "SELECT AVG(salary) FROM instructor",
            expected_clauses=["AGGREGATE"],
            expected_diff_types=["aggregate_function_changed"],
        ),
        _case(
            "order_direction_changed",
            "ORDER BY",
            "SELECT name FROM student ORDER BY age DESC",
            "SELECT name FROM student ORDER BY age ASC",
            expected_clauses=["ORDER BY"],
            expected_diff_types=["order_by_changed"],
        ),
        _case(
            "order_secondary_key_missing",
            "ORDER BY",
            "SELECT name FROM student ORDER BY age DESC, name ASC",
            "SELECT name FROM student ORDER BY age DESC",
            expected_clauses=["ORDER BY"],
            expected_diff_types=["order_by_changed"],
        ),
        _case(
            "limit_changed",
            "LIMIT/OFFSET",
            "SELECT name FROM student LIMIT 5",
            "SELECT name FROM student LIMIT 3",
            expected_clauses=["LIMIT"],
            expected_diff_types=["limit_changed"],
        ),
        _case(
            "offset_changed",
            "LIMIT/OFFSET",
            "SELECT name FROM student LIMIT 5 OFFSET 2",
            "SELECT name FROM student LIMIT 5 OFFSET 3",
            expected_clauses=["LIMIT"],
            expected_diff_types=["limit_changed"],
        ),
        _case(
            "subquery_removed",
            "Subquery",
            "SELECT name FROM student WHERE id IN (SELECT s_id FROM advisor)",
            "SELECT name FROM student",
            expected_clauses=["SUBQUERY"],
            expected_diff_types=["subquery_removed"],
        ),
        _case(
            "subquery_predicate_changed",
            "Subquery",
            "SELECT name FROM student WHERE id IN (SELECT s_id FROM advisor WHERE i_id = 1)",
            "SELECT name FROM student WHERE id IN (SELECT s_id FROM advisor WHERE i_id = 2)",
            expected_clauses=["PREDICATE"],
            expected_diff_types=["literal_changed"],
        ),
        _case(
            "correlated_subquery_column_changed",
            "Correlated Subquery",
            "SELECT s.name FROM student s WHERE EXISTS (SELECT 1 FROM advisor a WHERE a.s_id = s.id)",
            "SELECT s.name FROM student s WHERE EXISTS (SELECT 1 FROM advisor a WHERE a.i_id = s.id)",
            expected_clauses=["CORRELATED SUBQUERY"],
            expected_diff_types=["correlated_predicate_changed"],
        ),
        _case(
            "cte_body_changed",
            "CTE",
            "WITH c AS (SELECT id FROM student WHERE dept = 'CS') SELECT id FROM c",
            "WITH c AS (SELECT id FROM student WHERE dept = 'Math') SELECT id FROM c",
            expected_clauses=["CTE"],
            expected_diff_types=["cte_changed"],
        ),
        _case(
            "recursive_cte_boundary_changed",
            "Recursive CTE",
            "WITH RECURSIVE nums(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM nums WHERE n < 5) SELECT n FROM nums",
            "WITH RECURSIVE nums(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM nums WHERE n < 3) SELECT n FROM nums",
            expected_clauses=["CTE_RECURSIVE"],
            expected_diff_types=["recursive_cte_changed"],
        ),
        _case(
            "set_union_to_union_all",
            "Set Operation",
            "SELECT id FROM a UNION SELECT id FROM b",
            "SELECT id FROM a UNION ALL SELECT id FROM b",
            expected_clauses=["UNION"],
            expected_diff_types=["set_operator_changed"],
        ),
        _case(
            "set_intersect_to_union",
            "Set Operation",
            "SELECT id FROM a INTERSECT SELECT id FROM b",
            "SELECT id FROM a UNION SELECT id FROM b",
            expected_clauses=["INTERSECT"],
            expected_diff_types=["set_operator_changed"],
        ),
        _case(
            "case_branch_changed",
            "CASE",
            "SELECT CASE WHEN score >= 60 THEN 'pass' ELSE 'fail' END FROM exam",
            "SELECT CASE WHEN score >= 70 THEN 'pass' ELSE 'fail' END FROM exam",
            expected_clauses=["SELECT"],
            expected_diff_types=["projection_changed"],
        ),
        _case(
            "window_partition_changed",
            "Window",
            "SELECT ROW_NUMBER() OVER (PARTITION BY dept ORDER BY salary DESC) FROM instructor",
            "SELECT ROW_NUMBER() OVER (PARTITION BY name ORDER BY salary DESC) FROM instructor",
            expected_clauses=["WINDOW"],
            expected_diff_types=["window_over_changed"],
        ),
        _case(
            "window_function_changed",
            "Window",
            "SELECT RANK() OVER (ORDER BY salary DESC) FROM instructor",
            "SELECT ROW_NUMBER() OVER (ORDER BY salary DESC) FROM instructor",
            expected_clauses=["WINDOW"],
            expected_diff_types=["window_over_changed"],
        ),
        _case(
            "distinct_on_gap",
            "Dialect Boundary",
            "SELECT DISTINCT ON (dept) dept, name FROM student ORDER BY dept, name",
            "SELECT DISTINCT dept, name FROM student ORDER BY dept, name",
            representation="known_gap",
            note="PostgreSQL DISTINCT ON is outside current AST diff typing.",
        ),
        _case(
            "rollup_gap",
            "Dialect Boundary",
            "SELECT region, SUM(amount) FROM sales GROUP BY ROLLUP(region)",
            "SELECT region, SUM(amount) FROM sales GROUP BY region",
            representation="known_gap",
            note="ROLLUP is parsed but current SQLite-oriented semantic path treats it as boundary.",
        ),
    ]


def evaluate_case(case: dict[str, Any]) -> dict[str, Any]:
    try:
        diffs = extract_ast_diffs(case["standard"], case["student"])
        error = None
    except Exception as exc:
        diffs = []
        error = str(exc)

    actual = [diff.to_dict() for diff in diffs]
    actual_clauses = {str(item.get("clause") or "") for item in actual}
    actual_diff_types = {str(item.get("diff_type") or "") for item in actual}
    actual_kps = {str(item.get("knowledge_point_id") or "") for item in actual}

    missing_clauses = sorted(set(case["expected_clauses"]) - actual_clauses)
    missing_diff_types = sorted(set(case["expected_diff_types"]) - actual_diff_types)
    missing_kps = sorted(set(case["expected_kps"]) - actual_kps)
    passed = not missing_clauses and not missing_diff_types and not missing_kps and error is None

    if case["representation"] in {"known_gap", "known_boundary"}:
        bucket = case["representation"]
    elif passed:
        bucket = "supported"
    else:
        bucket = "unexpected_failure"

    return {
        **case,
        "error": error,
        "diff_count": len(actual),
        "actual_clauses": sorted(actual_clauses),
        "actual_diff_types": sorted(actual_diff_types),
        "actual_kps": sorted(actual_kps),
        "missing_clauses": missing_clauses,
        "missing_diff_types": missing_diff_types,
        "missing_kps": missing_kps,
        "passed": passed,
        "capability_bucket": bucket,
        "ast_diffs": actual,
    }


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    buckets = Counter(item["capability_bucket"] for item in results)
    categories = defaultdict(Counter)
    for item in results:
        categories[item["category"]][item["capability_bucket"]] += 1
    ir_coverage = build_ir_to_ast_coverage(categories)
    total = len(results)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total": total,
        "buckets": dict(buckets),
        "category_buckets": {category: dict(counter) for category, counter in sorted(categories.items())},
        "ir_to_ast_category_coverage": ir_coverage,
        "support_rate": buckets["supported"] / max(total - buckets["known_boundary"], 1),
        "unexpected_failure_count": buckets["unexpected_failure"],
    }


def build_ir_to_ast_coverage(ast_categories: dict[str, Counter]) -> list[dict[str, Any]]:
    ir_report = OUTPUT_DIR / "phase1_ir_structure_capability.json"
    if not ir_report.exists():
        return []
    data = json.loads(ir_report.read_text(encoding="utf-8"))
    ir_categories = data.get("summary", {}).get("category_buckets", {})
    rows: list[dict[str, Any]] = []
    for category, counter in sorted(ir_categories.items()):
        ir_supported = int(counter.get("first_class", 0)) + int(counter.get("weak_textual", 0))
        ir_gap = int(counter.get("known_gap", 0))
        ir_boundary = int(counter.get("known_boundary", 0))
        ast_counter = ast_categories.get(category, Counter())
        ast_supported = int(ast_counter.get("supported", 0))
        ast_gap = int(ast_counter.get("known_gap", 0))
        ast_boundary = int(ast_counter.get("known_boundary", 0))
        if ast_supported:
            status = "diff_supported"
        elif ast_gap:
            status = "diff_known_gap"
        elif ast_boundary:
            status = "diff_boundary"
        else:
            status = "missing_diff_cases"
        rows.append({
            "category": category,
            "ir_supported_cases": ir_supported,
            "ir_known_gap_cases": ir_gap,
            "ir_known_boundary_cases": ir_boundary,
            "ast_supported_cases": ast_supported,
            "ast_known_gap_cases": ast_gap,
            "ast_known_boundary_cases": ast_boundary,
            "status": status,
        })
    return rows


def write_markdown(summary: dict[str, Any], results: list[dict[str, Any]], path: Path) -> None:
    lines = [
        "# Phase 1 AST Diff Capability",
        "",
        f"Generated at: `{summary['generated_at']}`",
        "",
        "This benchmark follows the IR benchmark: each category first tested for single-query IR recognition is then tested with standard/student SQL pairs to see whether structural differences are identified.",
        "",
        "## Summary",
        "",
        f"- Total cases: `{summary['total']}`",
        f"- Buckets: `{summary['buckets']}`",
        f"- Non-boundary support rate: `{summary['support_rate']:.2%}`",
        f"- Unexpected failures: `{summary['unexpected_failure_count']}`",
        "",
        "## IR To AST Diff Continuity",
        "",
        "| IR category | IR supported | IR gaps | AST supported | AST gaps | status |",
        "| --- | ---: | ---: | ---: | ---: | --- |",
    ]
    for row in summary.get("ir_to_ast_category_coverage") or []:
        lines.append(
            f"| {row['category']} | {row['ir_supported_cases']} | {row['ir_known_gap_cases']} | "
            f"{row['ast_supported_cases']} | {row['ast_known_gap_cases']} | `{row['status']}` |"
        )
    lines.extend([
        "## Category Matrix",
        "",
        "| category | supported | known_gap | known_boundary | unexpected_failure |",
        "| --- | ---: | ---: | ---: | ---: |",
    ])
    for category, counter in summary["category_buckets"].items():
        lines.append(
            f"| {category} | {counter.get('supported', 0)} | {counter.get('known_gap', 0)} | "
            f"{counter.get('known_boundary', 0)} | {counter.get('unexpected_failure', 0)} |"
        )
    lines.extend([
        "",
        "## Cases",
        "",
        "| result | category | id | expected diff types | actual diff types | note |",
        "| --- | --- | --- | --- | --- | --- |",
    ])
    for item in results:
        lines.append(
            f"| `{item['capability_bucket']}` | {item['category']} | `{item['id']}` | "
            f"`{item['expected_diff_types']}` | `{item['actual_diff_types']}` | {item.get('note') or ''} |"
        )
    failures = [item for item in results if item["capability_bucket"] == "unexpected_failure"]
    if failures:
        lines.extend(["", "## Unexpected Failures", ""])
        for item in failures:
            lines.extend([
                f"### {item['id']}",
                "",
                f"- Category: `{item['category']}`",
                f"- Missing clauses: `{item['missing_clauses']}`",
                f"- Missing diff types: `{item['missing_diff_types']}`",
                "",
                "```sql",
                "-- standard",
                item["standard"],
                "",
                "-- student",
                item["student"],
                "```",
                "",
            ])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    cases = build_cases()
    results = [evaluate_case(case) for case in cases]
    summary = summarize(results)

    cases_path = OUTPUT_DIR / "phase1_ast_diff_cases.jsonl"
    evidence_path = OUTPUT_DIR / "phase1_ast_diff_detailed_evidence.jsonl"
    json_path = OUTPUT_DIR / "phase1_ast_diff_capability.json"
    md_path = OUTPUT_DIR / "phase1_ast_diff_capability.md"

    cases_path.write_text("\n".join(json.dumps(case, ensure_ascii=False) for case in cases) + "\n", encoding="utf-8")
    evidence_path.write_text("\n".join(json.dumps(result, ensure_ascii=False) for result in results) + "\n", encoding="utf-8")
    json_path.write_text(json.dumps({"summary": summary, "results": results}, ensure_ascii=False, indent=2), encoding="utf-8")
    write_markdown(summary, results, md_path)

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"Wrote {cases_path}")
    print(f"Wrote {evidence_path}")
    print(f"Wrote {json_path}")
    print(f"Wrote {md_path}")


if __name__ == "__main__":
    main()
