"""Generate AST diff pairs for every IR structure case.

This benchmark preserves a one-to-one link from each IR recognition case to an
AST diff pair through `source_ir_case_id`.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
IR_CASES = SCRIPT_DIR / "outputs" / "phase1_ir_structure_cases.jsonl"
AST_CASES = SCRIPT_DIR / "outputs" / "phase1_ast_diff_from_ir_cases.jsonl"
AST_EVIDENCE = SCRIPT_DIR / "outputs" / "phase1_ast_diff_from_ir_detailed_evidence.jsonl"
AST_JSON = SCRIPT_DIR / "outputs" / "phase1_ast_diff_from_ir_capability.json"
AST_MD = SCRIPT_DIR / "outputs" / "phase1_ast_diff_from_ir_capability.md"

sys.path.append(str(SCRIPT_DIR))
from run_phase1_ast_diff_capability import evaluate_case, summarize, write_markdown


TEMPLATES = {
    "SELECT": {
        "standard": "SELECT name, age FROM student",
        "student": "SELECT name FROM student",
        "expected_clauses": ["SELECT"],
        "expected_diff_types": ["column_dropped"],
    },
    "DISTINCT": {
        "standard": "SELECT DISTINCT dept FROM student",
        "student": "SELECT dept FROM student",
        "expected_clauses": ["DISTINCT"],
        "expected_diff_types": ["distinct_changed"],
    },
    "WHERE": {
        "standard": "SELECT name FROM student WHERE age > 18",
        "student": "SELECT name FROM student",
        "expected_clauses": ["WHERE"],
        "expected_diff_types": ["where_changed", "predicate_missing"],
    },
    "Comparison": {
        "standard": "SELECT * FROM t WHERE age > 18",
        "student": "SELECT * FROM t WHERE age >= 18",
        "expected_clauses": ["PREDICATE"],
        "expected_diff_types": ["comparison_operator_changed"],
    },
    "NULL": {
        "standard": "SELECT * FROM student WHERE advisor_id IS NULL",
        "student": "SELECT * FROM student WHERE advisor_id = NULL",
        "expected_clauses": ["PREDICATE"],
        "expected_diff_types": ["comparison_operator_changed", "null_equality_changed"],
    },
    "IN/BETWEEN/LIKE": {
        "standard": "SELECT * FROM course WHERE credits BETWEEN 2 AND 4",
        "student": "SELECT * FROM course WHERE credits BETWEEN 3 AND 4",
        "expected_clauses": ["PREDICATE"],
        "expected_diff_types": ["literal_changed"],
    },
    "Logic": {
        "standard": "SELECT * FROM t WHERE a = 1 AND b = 2",
        "student": "SELECT * FROM t WHERE a = 1 OR b = 2",
        "expected_clauses": ["LOGICAL"],
        "expected_diff_types": ["logical_operator_changed"],
    },
    "JOIN": {
        "standard": "SELECT s.name FROM student s LEFT JOIN takes t ON s.id = t.s_id",
        "student": "SELECT s.name FROM student s JOIN takes t ON s.id = t.s_id",
        "expected_clauses": ["JOIN_TYPE"],
        "expected_diff_types": ["join_type_changed"],
    },
    "JOIN ON": {
        "standard": "SELECT s.name FROM student s JOIN advisor a ON s.id = a.s_id",
        "student": "SELECT s.name FROM student s JOIN advisor a ON s.id = a.i_id",
        "expected_clauses": ["JOIN ON"],
        "expected_diff_types": ["join_on_changed"],
    },
    "GROUP BY": {
        "standard": "SELECT dept, COUNT(*) FROM student GROUP BY dept",
        "student": "SELECT year, COUNT(*) FROM student GROUP BY year",
        "expected_clauses": ["GROUP BY"],
        "expected_diff_types": ["group_by_changed"],
    },
    "HAVING": {
        "standard": "SELECT dept FROM student GROUP BY dept HAVING COUNT(*) > 3",
        "student": "SELECT dept FROM student GROUP BY dept HAVING COUNT(*) >= 3",
        "expected_clauses": ["PREDICATE"],
        "expected_diff_types": ["comparison_operator_changed"],
    },
    "Aggregate": {
        "standard": "SELECT SUM(salary) FROM instructor",
        "student": "SELECT AVG(salary) FROM instructor",
        "expected_clauses": ["AGGREGATE"],
        "expected_diff_types": ["aggregate_function_changed"],
    },
    "ORDER BY": {
        "standard": "SELECT name FROM student ORDER BY age DESC",
        "student": "SELECT name FROM student ORDER BY age ASC",
        "expected_clauses": ["ORDER BY"],
        "expected_diff_types": ["order_by_changed"],
    },
    "LIMIT/OFFSET": {
        "standard": "SELECT name FROM student LIMIT 5",
        "student": "SELECT name FROM student LIMIT 3",
        "expected_clauses": ["LIMIT"],
        "expected_diff_types": ["limit_changed"],
    },
    "Subquery": {
        "standard": "SELECT name FROM student WHERE id IN (SELECT s_id FROM advisor)",
        "student": "SELECT name FROM student",
        "expected_clauses": ["SUBQUERY"],
        "expected_diff_types": ["subquery_removed"],
    },
    "Correlated Subquery": {
        "standard": "SELECT s.name FROM student s WHERE EXISTS (SELECT 1 FROM advisor a WHERE a.s_id = s.id)",
        "student": "SELECT s.name FROM student s WHERE EXISTS (SELECT 1 FROM advisor a WHERE a.i_id = s.id)",
        "expected_clauses": ["CORRELATED SUBQUERY"],
        "expected_diff_types": ["correlated_predicate_changed"],
    },
    "CTE": {
        "standard": "WITH c AS (SELECT id FROM student WHERE dept = 'CS') SELECT id FROM c",
        "student": "WITH c AS (SELECT id FROM student WHERE dept = 'Math') SELECT id FROM c",
        "expected_clauses": ["CTE"],
        "expected_diff_types": ["cte_changed"],
    },
    "Recursive CTE": {
        "standard": "WITH RECURSIVE nums(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM nums WHERE n < 5) SELECT n FROM nums",
        "student": "WITH RECURSIVE nums(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM nums WHERE n < 3) SELECT n FROM nums",
        "expected_clauses": ["CTE_RECURSIVE"],
        "expected_diff_types": ["recursive_cte_changed"],
    },
    "Set Operation": {
        "standard": "SELECT id FROM a UNION SELECT id FROM b",
        "student": "SELECT id FROM a UNION ALL SELECT id FROM b",
        "expected_clauses": ["UNION"],
        "expected_diff_types": ["set_operator_changed"],
    },
    "CASE": {
        "standard": "SELECT CASE WHEN score >= 60 THEN 'pass' ELSE 'fail' END FROM exam",
        "student": "SELECT CASE WHEN score >= 70 THEN 'pass' ELSE 'fail' END FROM exam",
        "expected_clauses": ["SELECT"],
        "expected_diff_types": ["projection_changed"],
    },
    "Window": {
        "standard": "SELECT ROW_NUMBER() OVER (PARTITION BY dept ORDER BY salary DESC) FROM instructor",
        "student": "SELECT ROW_NUMBER() OVER (PARTITION BY name ORDER BY salary DESC) FROM instructor",
        "expected_clauses": ["WINDOW"],
        "expected_diff_types": ["window_over_changed"],
    },
}


def load_ir_cases() -> list[dict]:
    return [json.loads(line) for line in IR_CASES.read_text(encoding="utf-8").splitlines() if line.strip()]


def build_ast_cases() -> list[dict]:
    ast_cases = []
    for ir_case in load_ir_cases():
        category = ir_case["category"]
        representation = ir_case.get("representation", "first_class")
        if representation in {"known_gap", "known_boundary"}:
            ast_case = {
                "id": f"from_ir__{ir_case['id']}",
                "source_ir_case_id": ir_case["id"],
                "category": category,
                "standard": ir_case["sql"],
                "student": ir_case["sql"],
                "expected_clauses": [],
                "expected_diff_types": [],
                "expected_kps": [],
                "representation": representation,
                "note": f"Inherited from IR {representation}: {ir_case.get('note') or ''}",
            }
        else:
            template = dict(TEMPLATES[category])
            ast_case = {
                "id": f"from_ir__{ir_case['id']}",
                "source_ir_case_id": ir_case["id"],
                "category": category,
                "standard": template.pop("standard"),
                "student": template.pop("student"),
                "expected_clauses": template.pop("expected_clauses"),
                "expected_diff_types": template.pop("expected_diff_types"),
                "expected_kps": [],
                "representation": template.pop("representation", "supported"),
                "note": template.pop("note", f"Category-level AST diff pair generated from IR case {ir_case['id']}."),
            }
        ast_cases.append(ast_case)
    return ast_cases


def main() -> None:
    cases = build_ast_cases()
    results = [evaluate_case(case) for case in cases]
    summary = summarize(results)
    AST_CASES.write_text("\n".join(json.dumps(case, ensure_ascii=False) for case in cases) + "\n", encoding="utf-8")
    AST_EVIDENCE.write_text("\n".join(json.dumps(result, ensure_ascii=False) for result in results) + "\n", encoding="utf-8")
    AST_JSON.write_text(json.dumps({"summary": summary, "results": results}, ensure_ascii=False, indent=2), encoding="utf-8")
    write_markdown(summary, results, AST_MD)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"Wrote {AST_CASES}")
    print(f"Wrote {AST_EVIDENCE}")
    print(f"Wrote {AST_JSON}")
    print(f"Wrote {AST_MD}")


if __name__ == "__main__":
    main()
