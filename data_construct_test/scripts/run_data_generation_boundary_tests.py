"""Evaluate data-generation boundaries from structure diffs.

The report answers two questions:
1. Given ASTDiff/IR-like structure differences, does the current generator
   activate an appropriate data tactic?
2. Does the generated database make standard and student SQL observably diverge
   for non-equivalent teaching mistakes, while preserving equivalent rewrites?

It reuses the fixed-seed web_common150 structure holdout and adds a smaller
online-inspired extension set with explicit schemas.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "sql-edu-backend"))
sys.path.insert(0, str(PROJECT_ROOT / "data_construct_test" / "scripts"))

from sqlglot import exp

from core.parseval_data_generator import (  # noqa: E402
    _parse_sql,
    extract_ast_diffs,
    generate_and_compare,
)

OUTPUT_DIR = PROJECT_ROOT / "data_construct_test" / "outputs"
STRUCTURE_SCRIPT = PROJECT_ROOT / "data_construct_test" / "scripts" / "run_web_common150_structure_tests.py"
STRUCTURE_CASES_JSONL = OUTPUT_DIR / "web_common150_structure_cases.jsonl"


TRUE_EQUIVALENCE_TARGETS = {
    "implicit_explicit_join_equivalence",
    "between_expansion_equivalence",
    "like_negation_equivalence",
    "in_exists_equivalence",
    "subquery_join_equivalence",
    "not_exists_left_join_equivalence",
    "cte_inline_equivalence",
    "mysql_limit_comma_equivalence",
}


def _load_common150_cases() -> list[dict[str, Any]]:
    if STRUCTURE_SCRIPT.exists():
        spec = importlib.util.spec_from_file_location("web_common150_structure", STRUCTURE_SCRIPT)
        if spec and spec.loader:
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return [
                {
                    **case,
                    "dataset": "web_common150",
                    "expected_equivalent": _expected_equivalent(case),
                    "schema": infer_schema(case["standard"], case["student"]),
                }
                for case in module.build_cases()
            ]
    if not STRUCTURE_CASES_JSONL.exists():
        raise FileNotFoundError("web_common150 structure cases are unavailable")
    cases = []
    for line in STRUCTURE_CASES_JSONL.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        case = json.loads(line)
        cases.append({
            **case,
            "dataset": "web_common150",
            "expected_equivalent": _expected_equivalent(case),
            "schema": infer_schema(case["standard"], case["student"]),
        })
    return cases


def _expected_equivalent(case: dict[str, Any]) -> bool:
    expected = case.get("expected") or {}
    if expected.get("expect_no_diff"):
        return True
    return str(case.get("strict_target") or "") in TRUE_EQUIVALENCE_TARGETS


def infer_schema(*sqls: str) -> str:
    """Infer a compact schema from table/column references in SQL text."""
    table_columns: dict[str, set[str]] = defaultdict(set)
    physical_names: dict[str, str] = {}
    aliases: dict[str, str] = {}
    cte_names: set[str] = set()
    cte_output_columns: set[str] = set()

    asts = [_parse_sql(sql) for sql in sqls]
    for ast in asts:
        if not ast:
            continue
        for cte in ast.find_all(exp.CTE):
            if cte.alias:
                cte_names.add(_norm(cte.alias))
            select = cte.this.find(exp.Select) if isinstance(cte.this, exp.Expression) else None
            if isinstance(select, exp.Select):
                for item in select.expressions or []:
                    if isinstance(item, exp.Alias) and item.alias:
                        cte_output_columns.add(_norm(item.alias))
        for table in ast.find_all(exp.Table):
            table_name = _clean(table.name)
            if not table_name or _norm(table_name) in cte_names:
                continue
            table_norm = _norm(table_name)
            canonical_name = physical_names.setdefault(table_norm, table_name)
            table_columns.setdefault(canonical_name, set())
            aliases[table_norm] = canonical_name
            if table.alias:
                aliases[_norm(table.alias)] = canonical_name

    for ast in asts:
        if not ast:
            continue
        for column in ast.find_all(exp.Column):
            column_name = _clean(column.name)
            if not column_name or column_name == "*":
                continue
            table_ref = _norm(column.table or "")
            if table_ref and table_ref in aliases:
                table_columns[aliases[table_ref]].add(column_name)
                continue

            select = column.find_ancestor(exp.Select)
            scope_tables = []
            scope_has_cte = False
            if isinstance(select, exp.Select):
                for table in select.find_all(exp.Table):
                    if table.find_ancestor(exp.Select) is not select:
                        continue
                    table_norm = _norm(table.name)
                    if table_norm in cte_names:
                        scope_has_cte = True
                        continue
                    actual = physical_names.get(table_norm)
                    if actual and actual not in scope_tables:
                        scope_tables.append(actual)
            if scope_has_cte and _norm(column_name) in cte_output_columns:
                continue
            if len(scope_tables) == 1:
                table_columns[scope_tables[0]].add(column_name)
                continue
            if scope_tables:
                column_norm = _norm(column_name)
                preferred = next(
                    (
                        table_name for table_name in scope_tables
                        if column_norm.startswith(_norm(table_name).rstrip("s") + "_")
                        or column_norm.startswith(_norm(table_name) + "_")
                    ),
                    scope_tables[0],
                )
                table_columns[preferred].add(column_name)

    parts = []
    for table_name in sorted(table_columns):
        cols = table_columns[table_name]
        if not cols:
            cols = {"id", "name"}
        ordered = _order_columns(table_name, cols)
        parts.append(f"{table_name}({', '.join(ordered)})")
    return "; ".join(parts)


def _order_columns(table_name: str, columns: set[str]) -> list[str]:
    def key(col: str) -> tuple[int, str]:
        norm = _norm(col)
        if norm in {"id", f"{_norm(table_name)}_id", f"{_norm(table_name).rstrip('s')}_id"}:
            return (0, col)
        if norm.endswith("_id") or norm.endswith("id"):
            return (1, col)
        return (2, col)

    return sorted(columns, key=key)


def _clean(value: Any) -> str:
    text = str(value or "").strip()
    if (text.startswith('"') and text.endswith('"')) or (text.startswith("`") and text.endswith("`")):
        text = text[1:-1]
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    return text


def _norm(value: Any) -> str:
    return re.sub(r"[^a-z0-9_]", "", _clean(value).lower())


def _online_extra_cases() -> list[dict[str, Any]]:
    """New online-inspired cases with explicit schemas.

    Sources used as topic inspiration:
    - SQLZoo tutorial and subquery/JOIN pages
    - PostgreSQL window and WITH/recursive examples
    - SQLTutorial functions/CASE/window topics
    """
    sources = {
        "sqlzoo": "https://sqlzoo.net/wiki/SQL_Tutorial?lang=en",
        "sqlzoo_subquery_join": "https://sqlzoo.net/wiki/Subquery_and_JOIN2",
        "postgres_window": "https://www.postgresql.org/docs/current/tutorial-window.html",
        "postgres_with": "https://www.postgresql.org/docs/current/queries-with.html",
        "sqltutorial_functions": "https://www.sqltutorial.org/sql-functions/",
    }
    cases: list[dict[str, Any]] = []

    def add(id_: str, structure: str, schema: str, standard: str, student: str, *, source: str, expected_equivalent: bool = False, target: str = "") -> None:
        cases.append({
            "id": f"online_extra_{id_}",
            "dataset": "online_extra",
            "structure": structure,
            "source_url": sources[source],
            "schema": schema,
            "standard": standard,
            "student": student,
            "expected_equivalent": expected_equivalent,
            "strict_target": target or ("equivalent_rewrite" if expected_equivalent else "counterexample_required"),
        })

    tables = [
        ("student(ID, name, dept_id, score)", "student", "name", "score", "dept_id"),
        ("employee(id, employee_name, department_id, salary)", "employee", "employee_name", "salary", "department_id"),
        ("orders(order_id, customer_id, customer_name, totalitems)", "orders", "customer_name", "totalitems", "customer_id"),
        ("product(product_id, product_name, category_id, price)", "product", "product_name", "price", "category_id"),
        ("course(course_id, title, dept_id, credits)", "course", "title", "credits", "dept_id"),
    ]
    for idx, (schema, table, label, metric, group_col) in enumerate(tables, 1):
        add(f"where_boundary_{idx}", "WHERE", schema,
            f"SELECT {label} FROM {table} WHERE {metric} > {idx * 10};",
            f"SELECT {label} FROM {table} WHERE {metric} >= {idx * 10};",
            source="sqlzoo")
        add(f"in_or_equiv_{idx}", "IN", schema,
            f"SELECT {label} FROM {table} WHERE {group_col} IN (1, 2);",
            f"SELECT {label} FROM {table} WHERE {group_col} = 1 OR {group_col} = 2;",
            source="sqlzoo", expected_equivalent=True)
        add(f"between_equiv_{idx}", "BETWEEN", schema,
            f"SELECT {label} FROM {table} WHERE {metric} BETWEEN {idx} AND {idx + 5};",
            f"SELECT {label} FROM {table} WHERE {metric} >= {idx} AND {metric} <= {idx + 5};",
            source="sqltutorial_functions", expected_equivalent=True)
        add(f"group_having_{idx}", "HAVING", schema,
            f"SELECT {group_col}, AVG({metric}) FROM {table} GROUP BY {group_col} HAVING AVG({metric}) > {idx * 10};",
            f"SELECT {group_col}, AVG({metric}) FROM {table} GROUP BY {group_col} HAVING AVG({metric}) >= {idx * 10};",
            source="sqlzoo")
        add(f"order_limit_{idx}", "ORDER BY", schema,
            f"SELECT {label} FROM {table} ORDER BY {metric} DESC, {label} ASC LIMIT 3;",
            f"SELECT {label} FROM {table} ORDER BY {metric} DESC LIMIT 3;",
            source="sqlzoo")

    joins = [
        ("student(ID, name, dept_id); department(id, dept_name)", "SELECT s.name, d.dept_name FROM student s JOIN department d ON s.dept_id = d.id;", "SELECT s.name, d.dept_name FROM student s JOIN department d ON s.ID = d.id;"),
        ("employee(id, employee_name, department_id); department(id, dept_name)", "SELECT e.employee_name, d.dept_name FROM employee e JOIN department d ON e.department_id = d.id;", "SELECT e.employee_name, d.dept_name FROM employee e, department d WHERE e.department_id = d.id;"),
        ("orders(order_id, customer_id, totalitems); customer(id, region)", "SELECT o.order_id, c.region FROM orders o LEFT JOIN customer c ON o.customer_id = c.id;", "SELECT o.order_id, c.region FROM orders o JOIN customer c ON o.customer_id = c.id;"),
        ("course(course_id, title, dept_id); department(id, dept_name)", "SELECT c.title FROM course c WHERE c.dept_id IN (SELECT d.id FROM department d WHERE d.dept_name = 'CS');", "SELECT c.title FROM course c JOIN department d ON c.dept_id = d.id WHERE d.dept_name = 'CS';"),
        ("orders(order_id, customer, whn, totalitems)", "SELECT a.customer, a.whn FROM orders a WHERE a.totalitems = (SELECT MAX(b.totalitems) FROM orders b WHERE b.customer = a.customer);", "SELECT a.customer, a.whn FROM orders a WHERE a.totalitems >= (SELECT MAX(b.totalitems) FROM orders b WHERE b.customer = a.customer);"),
    ]
    for idx, (schema, standard, student) in enumerate(joins, 1):
        # Case 5 is also an identity: no row can be greater than its own
        # group's MAX, so `= MAX(...)` and `>= MAX(...)` select the same rows.
        expected_equiv = idx in {2, 4, 5}
        add(f"join_subquery_{idx}", "JOIN" if idx < 4 else "Subquery", schema, standard, student,
            source="sqlzoo_subquery_join", expected_equivalent=expected_equiv)

    cte_cases = [
        ("employee(id, employee_name, department_id, salary)", "WITH high_salary AS (SELECT employee_name, salary FROM employee WHERE salary > 50000) SELECT employee_name FROM high_salary;", "SELECT employee_name FROM employee WHERE salary > 50000;", True),
        ("employee(id, employee_name, department_id, salary)", "WITH high_salary AS (SELECT employee_name, salary FROM employee WHERE salary > 50000) SELECT employee_name FROM high_salary;", "WITH high_salary AS (SELECT employee_name, salary FROM employee WHERE salary >= 50000) SELECT employee_name FROM high_salary;", False),
        ("dummy(id)", "WITH RECURSIVE nums(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM nums WHERE n < 4) SELECT n FROM nums;", "WITH RECURSIVE nums(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM nums WHERE n < 5) SELECT n FROM nums;", False),
        ("department(id, parent_id, name)", "WITH RECURSIVE tree(id, parent_id, name) AS (SELECT id, parent_id, name FROM department WHERE parent_id IS NULL UNION ALL SELECT d.id, d.parent_id, d.name FROM department d JOIN tree t ON d.parent_id = t.id) SELECT name FROM tree;", "SELECT name FROM department;", False),
        ("employee(id, employee_name, department_id, salary)", "WITH dept_avg AS (SELECT department_id, AVG(salary) AS avg_salary FROM employee GROUP BY department_id) SELECT e.employee_name FROM employee e JOIN dept_avg d ON e.department_id = d.department_id WHERE e.salary > d.avg_salary;", "SELECT e.employee_name FROM employee e WHERE e.salary > (SELECT AVG(x.salary) FROM employee x WHERE x.department_id = e.department_id);", True),
    ]
    for idx, (schema, standard, student, equiv) in enumerate(cte_cases, 1):
        add(f"cte_{idx}", "CTE" if idx != 3 else "Recursive CTE", schema, standard, student,
            source="postgres_with", expected_equivalent=equiv)

    set_case_window = [
        ("course(course_id, title, dept_id, credits)", "SELECT title FROM course WHERE dept_id = 1 UNION SELECT title FROM course WHERE credits > 3;", "SELECT title FROM course WHERE dept_id = 1 UNION ALL SELECT title FROM course WHERE credits > 3;", "Set Operation", False),
        ("course(course_id, title, dept_id, credits)", "SELECT title FROM course WHERE dept_id = 1 INTERSECT SELECT title FROM course WHERE credits > 3;", "SELECT title FROM course WHERE dept_id = 1 UNION SELECT title FROM course WHERE credits > 3;", "Set Operation", False),
        ("course(course_id, title, dept_id, credits)", "SELECT title FROM course EXCEPT SELECT title FROM course WHERE credits < 3;", "SELECT title FROM course;", "Set Operation", False),
        ("student(ID, name, score)", "SELECT name, CASE WHEN score >= 60 THEN 'pass' ELSE 'fail' END FROM student;", "SELECT name, CASE WHEN score > 60 THEN 'pass' ELSE 'fail' END FROM student;", "CASE", False),
        ("student(ID, name, score)", "SELECT name, CASE WHEN score >= 60 THEN 'pass' ELSE 'fail' END FROM student;", "SELECT name, CASE WHEN score >= 60 THEN 'pass' END FROM student;", "CASE", False),
        ("employee(id, employee_name, department_id, salary)", "SELECT employee_name, ROW_NUMBER() OVER (PARTITION BY department_id ORDER BY salary DESC) AS rn FROM employee;", "SELECT employee_name, ROW_NUMBER() OVER (ORDER BY salary DESC) AS rn FROM employee;", "Window", False),
        ("employee(id, employee_name, department_id, salary)", "SELECT employee_name, RANK() OVER (PARTITION BY department_id ORDER BY salary DESC) AS rk FROM employee;", "SELECT employee_name, ROW_NUMBER() OVER (PARTITION BY department_id ORDER BY salary DESC) AS rk FROM employee;", "Window", False),
        ("employee(id, employee_name, salary)", "SELECT employee_name FROM employee ORDER BY salary DESC FETCH FIRST 3 ROWS ONLY;", "SELECT employee_name FROM employee ORDER BY salary DESC LIMIT 3;", "Dialect Boundary", True),
        ("employee(id, employee_name, salary)", "SELECT TOP 3 employee_name FROM employee ORDER BY salary DESC;", "SELECT employee_name FROM employee ORDER BY salary DESC LIMIT 3;", "Dialect Boundary", True),
        ("student(ID, name, score)", "SELECT name FROM student WHERE score IS NULL;", "SELECT name FROM student WHERE score = NULL;", "NULL", False),
    ]
    for idx, (schema, standard, student, structure, equiv) in enumerate(set_case_window, 1):
        add(f"set_case_window_{idx}", structure, schema, standard, student,
            source="postgres_window" if structure == "Window" else "sqlzoo",
            expected_equivalent=equiv)

    return cases


def evaluate_case(case: dict[str, Any], *, max_rows: int) -> dict[str, Any]:
    standard = case["standard"]
    student = case["student"]
    schema = case.get("schema") or infer_schema(standard, student)
    expected_equivalent = bool(case.get("expected_equivalent"))

    ast_diffs = []
    diff_error = None
    try:
        ast_diffs = extract_ast_diffs(standard, student)
    except Exception as exc:
        diff_error = f"{type(exc).__name__}: {exc}"

    run = generate_and_compare(schema, standard, student, max_rows_per_table=max_rows)
    column_names_match = bool((run.data_evidence or {}).get("column_names_match", True))
    row_equivalent = run.is_equivalent is True
    observable_mismatch = (run.is_equivalent is False) or (run.executed and not column_names_match)
    row_value_ok = row_equivalent if expected_equivalent else not row_equivalent
    outcome_ok = (not observable_mismatch) if expected_equivalent else observable_mismatch
    tactics = (run.data_evidence or {}).get("generation_tactics", [])
    tactic_ok = expected_equivalent or bool(tactics) or observable_mismatch
    mutation_summary = (run.mutation_evidence or {}).get("summary") or {}
    # For incorrect submissions, require an executed replacement mutant that
    # restores the standard behavior.  A first-run row mismatch alone proves
    # only E_data, not that the mutation layer localized the causal operator.
    mutation_ok = expected_equivalent or bool(mutation_summary.get("fixed_by_replacement"))

    # A structural/data mismatch is not sufficient evidence that the mutation
    # layer can localize the fault.  Keep mutation recovery in the gate so the
    # boundary report measures the full Observe contract (diff -> probe ->
    # ablation evidence), not only the first sandbox divergence.
    status = "PASS" if run.executed and outcome_ok and tactic_ok and mutation_ok else "FAIL"
    if not run.executed:
        status = "EXEC_ERROR"
    elif expected_equivalent and observable_mismatch:
        status = "FALSE_POSITIVE"
    elif not expected_equivalent and not observable_mismatch:
        status = "MISSED_COUNTEREXAMPLE"
    elif not tactic_ok:
        status = "TACTIC_MISSING"
    elif not mutation_ok:
        status = "MUTATION_MISSING"

    return {
        **case,
        "schema": schema,
        "expected_equivalent": expected_equivalent,
        "executed": run.executed,
        "error": run.error,
        "status": status,
        "row_equivalent": row_equivalent,
        "row_value_ok": row_value_ok,
        "observable_mismatch": observable_mismatch,
        "column_names_match": column_names_match,
        "standard_row_count": len(run.standard_rows),
        "student_row_count": len(run.student_rows),
        "standard_rows_sample": run.standard_rows[:5],
        "student_rows_sample": run.student_rows[:5],
        "ast_diff_types": [diff.diff_type for diff in ast_diffs],
        "diff_error": diff_error,
        "generation_tactics": tactics,
        "mutation_summary": mutation_summary,
        "test_database_sample": {table: rows[:5] for table, rows in run.test_database.items()},
    }


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    by_dataset = Counter(result["dataset"] for result in results)
    by_status = Counter(result["status"] for result in results)
    by_structure: dict[str, dict[str, int]] = {}
    for structure in sorted({result["structure"] for result in results}):
        items = [result for result in results if result["structure"] == structure]
        by_structure[structure] = {
            "total": len(items),
            "pass": sum(1 for item in items if item["status"] == "PASS"),
            "fail": sum(1 for item in items if item["status"] != "PASS"),
            "expected_equivalent": sum(1 for item in items if item["expected_equivalent"]),
            "expected_counterexample": sum(1 for item in items if not item["expected_equivalent"]),
        }
    missing_tactic = Counter()
    for result in results:
        if result["status"] == "PASS":
            continue
        if result["executed"] and not result["generation_tactics"] and not result["expected_equivalent"]:
            for diff_type in result["ast_diff_types"] or ["<no_diff>"]:
                missing_tactic[diff_type] += 1
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total": len(results),
        "pass": sum(1 for result in results if result["status"] == "PASS"),
        "fail": sum(1 for result in results if result["status"] != "PASS"),
        "row_value_pass": sum(1 for result in results if result.get("executed") and result.get("row_value_ok")),
        "row_value_fail": sum(1 for result in results if not (result.get("executed") and result.get("row_value_ok"))),
        "expected_counterexamples": sum(1 for result in results if not result.get("expected_equivalent")),
        "row_value_counterexamples": sum(
            1
            for result in results
            if not result.get("expected_equivalent") and result.get("executed") and not result.get("row_equivalent")
        ),
        "observable_counterexamples": sum(
            1
            for result in results
            if not result.get("expected_equivalent") and result.get("executed") and result.get("observable_mismatch")
        ),
        "column_only_counterexamples": sum(
            1
            for result in results
            if (
                not result.get("expected_equivalent")
                and result.get("executed")
                and result.get("observable_mismatch")
                and result.get("row_equivalent")
                and not result.get("column_names_match")
            )
        ),
        "by_dataset": dict(by_dataset),
        "by_status": dict(by_status),
        "by_structure": by_structure,
        "missing_tactic_diff_types": dict(missing_tactic.most_common()),
        "source_urls": sorted({result.get("source_url", "") for result in results if result.get("source_url")}),
    }


def write_report(results: list[dict[str, Any]], summary: dict[str, Any]) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    report_json = OUTPUT_DIR / "data_generation_boundary_report.json"
    report_md = OUTPUT_DIR / "data_generation_boundary_report.md"
    cases_jsonl = OUTPUT_DIR / "data_generation_boundary_cases.jsonl"

    report_json.write_text(json.dumps({"summary": summary, "results": results}, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    with cases_jsonl.open("w", encoding="utf-8") as fh:
        for result in results:
            fh.write(json.dumps(result, ensure_ascii=False, default=str) + "\n")

    lines = [
        "# Data Generation Boundary Report",
        "",
        "This report evaluates whether structure diffs drive data generation into useful counterexample databases.",
        "",
        f"- Generated at: `{summary['generated_at']}`",
        f"- Total: `{summary['total']}`",
        f"- PASS: `{summary['pass']}` (`{summary['pass'] / summary['total']:.2%}`)",
        f"- FAIL: `{summary['fail']}` (`{summary['fail'] / summary['total']:.2%}`)",
        f"- Row-value pass: `{summary['row_value_pass']}` (`{summary['row_value_pass'] / summary['total']:.2%}`)",
        f"- Expected counterexamples: `{summary['expected_counterexamples']}`",
        f"- Row-value counterexamples found: `{summary['row_value_counterexamples']}`",
        f"- Observable counterexamples found: `{summary['observable_counterexamples']}`",
        f"- Column-only counterexamples: `{summary['column_only_counterexamples']}`",
        f"- By dataset: `{summary['by_dataset']}`",
        f"- By status: `{summary['by_status']}`",
        "",
        "## By Structure",
        "",
        "| structure | total | pass | fail | expected equivalent | expected counterexample |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for structure, stats in summary["by_structure"].items():
        lines.append(
            f"| {structure} | {stats['total']} | {stats['pass']} | {stats['fail']} | "
            f"{stats['expected_equivalent']} | {stats['expected_counterexample']} |"
        )

    lines.extend(["", "## Failure Examples", ""])
    failures = [result for result in results if result["status"] != "PASS"]
    for result in failures[:60]:
        lines.extend([
            f"### {result['id']} ({result['structure']})",
            f"- dataset/status: `{result['dataset']}` / `{result['status']}`",
            f"- expected_equivalent: `{result['expected_equivalent']}`",
            f"- target: `{result.get('strict_target', '')}`",
            f"- schema: `{result['schema']}`",
            f"- standard: `{result['standard']}`",
            f"- student: `{result['student']}`",
            f"- row_equivalent / observable_mismatch: `{result['row_equivalent']}` / `{result['observable_mismatch']}`",
            f"- ast_diff_types: `{result['ast_diff_types']}`",
            f"- generation_tactics: `{result['generation_tactics']}`",
            f"- mutation_summary: `{result['mutation_summary']}`",
            f"- standard_rows_sample: `{result['standard_rows_sample']}`",
            f"- student_rows_sample: `{result['student_rows_sample']}`",
            "",
        ])

    row_value_misses = [
        result
        for result in results
        if not result["expected_equivalent"] and result["executed"] and result["row_equivalent"]
    ]
    lines.extend(["", "## Row-Value Misses", ""])
    for result in row_value_misses[:60]:
        lines.extend([
            f"### {result['id']} ({result['structure']})",
            f"- dataset/status: `{result['dataset']}` / `{result['status']}`",
            f"- target: `{result.get('strict_target', '')}`",
            f"- column_names_match: `{result['column_names_match']}`",
            f"- standard: `{result['standard']}`",
            f"- student: `{result['student']}`",
            f"- ast_diff_types: `{result['ast_diff_types']}`",
            f"- generation_tactics: `{result['generation_tactics']}`",
            f"- standard_rows_sample: `{result['standard_rows_sample']}`",
            f"- student_rows_sample: `{result['student_rows_sample']}`",
            "",
        ])

    lines.extend(["", "## Passing Examples", ""])
    for result in [item for item in results if item["status"] == "PASS"][:30]:
        lines.extend([
            f"### {result['id']} ({result['structure']})",
            f"- dataset: `{result['dataset']}`",
            f"- expected_equivalent: `{result['expected_equivalent']}`",
            f"- standard: `{result['standard']}`",
            f"- student: `{result['student']}`",
            f"- ast_diff_types: `{result['ast_diff_types']}`",
            f"- generation_tactics: `{result['generation_tactics']}`",
            f"- row_equivalent / observable_mismatch: `{result['row_equivalent']}` / `{result['observable_mismatch']}`",
            "",
        ])

    lines.extend(["", "## Sources", ""])
    for url in summary["source_urls"]:
        lines.append(f"- <{url}>")

    report_md.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-rows", type=int, default=10)
    parser.add_argument("--print-summary", action="store_true")
    args = parser.parse_args()

    cases = _load_common150_cases() + _online_extra_cases()
    results = [evaluate_case(case, max_rows=args.max_rows) for case in cases]
    summary = summarize(results)
    write_report(results, summary)
    if args.print_summary:
        print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
