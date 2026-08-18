"""Run a small, explicit Phase 1 matrix for the five teaching SQL dialects.

The matrix keeps syntax/IR evidence separate from execution evidence.  Vendor
queries are never counted as SQLite passes: they remain ``PENDING_NATIVE``
until the matching engine is deliberately enabled with ``--native``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = PROJECT_ROOT / "sql-edu-backend"
OUTPUT_DIR = PROJECT_ROOT / "data_construct_test" / "outputs"
sys.path.insert(0, str(BACKEND_ROOT))

from core.ast_schema import SQLStructureIR
from core.parseval_data_generator import extract_ast_diffs, generate_and_compare
from core.sql_dialect_resolver import resolve_sql_dialect_or_raise


FULL_FLOW_EXPECTATION = "full_flow"
SEMANTIC_BOUNDARY_EXPECTATION = "semantic_boundary"
SEMANTIC_BOUNDARY_STATUS = "SEMANTIC_BOUNDARY"


def _load_native_environment() -> None:
    """Load native executor URLs without replacing explicit shell overrides."""

    load_dotenv(BACKEND_ROOT / ".env", override=False)


def _default_output_paths(*, native: bool) -> tuple[Path, Path]:
    suffix = "_native" if native else ""
    stem = f"phase1_teaching_dialect_matrix{suffix}"
    return OUTPUT_DIR / f"{stem}.json", OUTPUT_DIR / f"{stem}.md"


@dataclass(frozen=True)
class MatrixCase:
    case_id: str
    dialect: str
    backend: str
    schema: str
    standard_sql: str
    student_sql: str
    expected_clause: str
    expected_kp: str
    native_required: bool = True
    execution_expectation: str = "full_flow"
    boundary_reason: str | None = None


def _case(
    case_id: str,
    dialect: str,
    backend: str,
    schema: str,
    standard_sql: str,
    student_sql: str,
    expected_clause: str,
    expected_kp: str,
    *,
    native_required: bool = True,
    execution_expectation: str = "full_flow",
    boundary_reason: str | None = None,
) -> MatrixCase:
    return MatrixCase(
        case_id=case_id,
        dialect=dialect,
        backend=backend,
        schema=schema,
        standard_sql=standard_sql,
        student_sql=student_sql,
        expected_clause=expected_clause,
        expected_kp=expected_kp,
        native_required=native_required,
        execution_expectation=execution_expectation,
        boundary_reason=boundary_reason,
    )


def build_cases() -> list[MatrixCase]:
    return [
        _case(
            "standard_where_boundary",
            "standard",
            "sqlite",
            "scores(id INTEGER, score INTEGER);",
            "SELECT id FROM scores WHERE score >= 60",
            "SELECT id FROM scores WHERE score > 60",
            "WHERE",
            "where",
            native_required=False,
        ),
        _case(
            "standard_fetch_count",
            "standard",
            "sqlite",
            "scores(id INTEGER, score INTEGER);",
            "SELECT id FROM scores ORDER BY score DESC FETCH FIRST 2 ROWS ONLY",
            "SELECT id FROM scores ORDER BY score DESC FETCH FIRST 1 ROWS ONLY",
            "LIMIT",
            "limit",
            native_required=False,
        ),
        _case(
            "mysql_limit_comma",
            "mysql",
            "mysql",
            "orders(id BIGINT, amount DECIMAL(10,2));",
            "SELECT id FROM orders ORDER BY id LIMIT 0, 2",
            "SELECT id FROM orders ORDER BY id LIMIT 0, 1",
            "LIMIT",
            "limit",
        ),
        _case(
            "mysql_group_concat_options",
            "mysql",
            "mysql",
            "users(id BIGINT, name VARCHAR(64));",
            "SELECT GROUP_CONCAT(name ORDER BY name ASC SEPARATOR ',') FROM users",
            "SELECT GROUP_CONCAT(name ORDER BY name DESC SEPARATOR ';') FROM users",
            "AGGREGATE",
            "aggregate",
        ),
        _case(
            "mysql_if_function",
            "mysql",
            "mysql",
            "scores(id BIGINT, score INT);",
            "SELECT IF(score >= 60, 'pass', 'fail') FROM scores",
            "SELECT IF(score > 60, 'pass', 'fail') FROM scores",
            "FUNCTION",
            "function",
        ),
        _case(
            "postgres_distinct_on",
            "postgres",
            "postgres",
            "users(id BIGINT, group_id BIGINT, name TEXT);",
            "SELECT DISTINCT ON (group_id) group_id, name FROM users ORDER BY group_id, id",
            "SELECT DISTINCT group_id, name FROM users ORDER BY group_id, id",
            "DISTINCT ON",
            "distinct",
        ),
        _case(
            "postgres_from_only",
            "postgres",
            "postgres",
            "users(id BIGINT, name TEXT);",
            "SELECT id FROM ONLY users",
            "SELECT id FROM users",
            "FROM ONLY",
            "table-only",
            execution_expectation="semantic_boundary",
            boundary_reason=(
                "The bounded fixture has no PostgreSQL inheritance child table, "
                "so ONLY and the plain parent table have the same rows."
            ),
        ),
        _case(
            "postgres_ilike",
            "postgres",
            "postgres",
            "users(id BIGINT, name TEXT);",
            "SELECT id FROM users WHERE name ILIKE 'a%'",
            "SELECT id FROM users WHERE name LIKE 'a%'",
            "WHERE",
            "where",
        ),
        _case(
            "tsql_top_percent",
            "tsql",
            "tsql",
            "scores(id BIGINT, score INT);",
            "SELECT TOP 50 PERCENT id FROM scores ORDER BY score DESC",
            "SELECT TOP 1 id FROM scores ORDER BY score DESC",
            "LIMIT",
            "limit",
        ),
        _case(
            "tsql_top_with_ties",
            "tsql",
            "tsql",
            "scores(id BIGINT, score INT);",
            "SELECT TOP 1 WITH TIES id FROM scores ORDER BY score DESC",
            "SELECT TOP 1 id FROM scores ORDER BY score DESC",
            "LIMIT",
            "limit",
        ),
        _case(
            "tsql_isnull",
            "tsql",
            "tsql",
            "scores(id BIGINT, score INT);",
            "SELECT ISNULL(score, 0) FROM scores",
            "SELECT ISNULL(score, 1) FROM scores",
            "FUNCTION",
            "function",
        ),
        _case(
            "tsql_pivot_columns",
            "tsql",
            "tsql",
            "sales(id BIGINT, amount DECIMAL(10,2), quarter VARCHAR(8));",
            "SELECT * FROM sales PIVOT (SUM(amount) FOR quarter IN ([Q1], [Q2])) p",
            "SELECT * FROM sales PIVOT (SUM(amount) FOR quarter IN ([Q1])) p",
            "PIVOT",
            "pivot",
        ),
        _case(
            "oracle_rownum",
            "oracle",
            "oracle",
            "users(id NUMBER, name VARCHAR2(64));",
            "SELECT id FROM users WHERE ROWNUM <= 2",
            "SELECT id FROM users WHERE ROWNUM < 2",
            "WHERE",
            "where",
        ),
        _case(
            "oracle_connect_by_nocycle",
            "oracle",
            "oracle",
            "employees(employee_id NUMBER, manager_id NUMBER);",
            "SELECT employee_id FROM employees START WITH manager_id IS NULL "
            "CONNECT BY NOCYCLE PRIOR employee_id = manager_id",
            "SELECT employee_id FROM employees START WITH manager_id IS NULL "
            "CONNECT BY PRIOR employee_id = manager_id",
            "CONNECT BY",
            "hierarchical-query",
        ),
        _case(
            "oracle_nvl",
            "oracle",
            "oracle",
            "scores(id NUMBER, score NUMBER);",
            "SELECT NVL(score, 0) FROM scores",
            "SELECT NVL(score, 1) FROM scores",
            "FUNCTION",
            "function",
        ),
        _case(
            "oracle_sample_rate",
            "oracle",
            "oracle",
            "users(id NUMBER, name VARCHAR2(64));",
            "SELECT id FROM users SAMPLE BLOCK (10) SEED (42)",
            "SELECT id FROM users SAMPLE BLOCK (20) SEED (42)",
            "TABLE SAMPLE",
            "table-sample",
            execution_expectation="semantic_boundary",
            boundary_reason=(
                "Oracle SAMPLE is probabilistic on bounded fixtures; one seeded "
                "execution cannot establish general sampling-rate semantics."
            ),
        ),
    ]


def _native_url(case: MatrixCase) -> str:
    if case.backend == "sqlite":
        return ""
    return str(os.environ.get(f"PARSEVAL_{case.backend.upper()}_URL", "")).strip()


def _native_enabled(case: MatrixCase, native: bool) -> bool:
    return native and bool(_native_url(case))


def _run_case(case: MatrixCase, *, native: bool) -> dict[str, Any]:
    item: dict[str, Any] = {"case": asdict(case)}
    try:
        resolution = resolve_sql_dialect_or_raise(
            declared_dialect=case.dialect,
            standard_sql=case.standard_sql,
            student_sql=case.student_sql,
            default_dialect="mysql",
        )
        ast_diffs = extract_ast_diffs(
            case.standard_sql,
            case.student_sql,
            dialect=resolution.parse_dialect,
        )
        irs = [SQLStructureIR.from_ast(ast) for ast in resolution.asts]
        matching = [
            diff
            for diff in ast_diffs
            if diff.clause_category == case.expected_clause
            and diff.knowledge_point_id == case.expected_kp
        ]
        item.update(
            {
                "resolution": resolution.to_dict(),
                "structure": {
                    "status": "PASS" if all(irs) else "FAIL",
                    "ir_features": [ir.feature_kps() for ir in irs],
                },
                "ast_diff": {
                    "status": "PASS" if matching else "FAIL",
                    "count": len(ast_diffs),
                    "matches": [diff.to_dict() for diff in matching],
                },
            }
        )
    except Exception as exc:
        item.update(
            {
                "resolution": {"status": "FAIL", "error": str(exc)},
                "structure": {"status": "FAIL"},
                "ast_diff": {"status": "FAIL"},
            }
        )
        return item

    execute = (not case.native_required) or _native_enabled(case, native)
    if not execute:
        item["execution"] = {
            "status": "PENDING_NATIVE",
            "reason": "native engine URL not enabled; SQLite is not counted",
        }
        return item

    backend = case.backend if case.native_required else "sqlite"
    url = _native_url(case) if backend != "sqlite" else None
    run = generate_and_compare(
        case.schema,
        case.standard_sql,
        case.student_sql,
        sql_dialect=case.dialect,
        default_sql_dialect="mysql",
        execution_backend=backend,
        native_executor_url=url,
    )
    mutation_tests = (run.mutation_evidence or {}).get("tests") or []
    matching_mutations = [
        test
        for test in mutation_tests
        if test.get("clause") == case.expected_clause
        and test.get("knowledge_point_id") == case.expected_kp
    ]
    repairing = [
        test
        for test in matching_mutations
        if test.get("fixed_by_replacement")
    ]
    if case.execution_expectation == SEMANTIC_BOUNDARY_EXPECTATION:
        mutation_observed = any(
            test.get("replacement_exec_ok")
            and test.get("replacement_equivalent")
            for test in matching_mutations
        )
        boundary_ready = bool(run.executed and mutation_observed)
        item["execution"] = {
            "status": SEMANTIC_BOUNDARY_STATUS if run.executed else "FAIL",
            "judge_status": run.judge_status,
            "executed": run.executed,
            "is_equivalent": run.is_equivalent,
            "error": run.error,
            "unsupported_features": (run.data_evidence or {}).get("unsupported_features", []),
            "boundary_reason": case.boundary_reason,
        }
        item["data"] = {
            "status": SEMANTIC_BOUNDARY_STATUS if run.executed else "FAIL",
            "row_counterexample": bool(
                run.executed and run.standard_rows != run.student_rows
            ),
            "boundary_reason": case.boundary_reason,
        }
        item["mutation"] = {
            "status": SEMANTIC_BOUNDARY_STATUS if mutation_observed else "FAIL",
            "structural_replacement_count": len(matching_mutations),
            "repair_count": len(repairing),
            "summary": run.mutation_evidence.get("summary", {}),
        }
        item["full_flow"] = {
            "status": SEMANTIC_BOUNDARY_STATUS if boundary_ready else "FAIL",
            "boundary_reason": case.boundary_reason,
        }
        return item

    item["execution"] = {
        "status": "PASS" if run.executed and run.is_equivalent is False else "FAIL",
        "judge_status": run.judge_status,
        "executed": run.executed,
        "is_equivalent": run.is_equivalent,
        "error": run.error,
        "unsupported_features": (run.data_evidence or {}).get("unsupported_features", []),
    }
    item["data"] = {
        "status": "PASS" if run.executed and run.is_equivalent is False else "FAIL",
        "row_counterexample": bool(
            run.executed and run.standard_rows != run.student_rows
        ),
    }
    item["mutation"] = {
        "status": "PASS" if repairing else "FAIL",
        "repair_count": len(repairing),
        "summary": run.mutation_evidence.get("summary", {}),
    }
    item["full_flow"] = {
        "status": "PASS" if run.executed and run.is_equivalent is False and repairing else "FAIL"
    }
    return item


def _summary(results: list[dict[str, Any]]) -> dict[str, Any]:
    stages = ("structure", "ast_diff", "execution", "data", "mutation", "full_flow")
    by_dialect: dict[str, dict[str, int]] = {}
    for dialect in sorted({item["case"]["dialect"] for item in results}):
        dialect_results = [item for item in results if item["case"]["dialect"] == dialect]
        by_dialect[dialect] = {
            stage: sum(1 for item in dialect_results if item.get(stage, {}).get("status") == "PASS")
            for stage in stages
        }
    return {
        "total": len(results),
        "dialects": Counter(item["case"]["dialect"] for item in results),
        "execution_expectations": Counter(
            item["case"]["execution_expectation"] for item in results
        ),
        "semantic_boundary_ids": [
            item["case"]["case_id"]
            for item in results
            if item["case"]["execution_expectation"]
            == SEMANTIC_BOUNDARY_EXPECTATION
        ],
        "stage_status": {
            stage: Counter(item.get(stage, {}).get("status", "NOT_RUN") for item in results)
            for stage in stages
        },
        "by_dialect": by_dialect,
    }


def build_report(*, native: bool = False) -> dict[str, Any]:
    results = [_run_case(case, native=native) for case in build_cases()]
    return {
        "mode": "native" if native else "offline",
        "summary": _summary(results),
        "results": results,
    }


def render_markdown(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    execution_note = (
        "Vendor cases were executed on their configured native engines; "
        "portable standard cases used SQLite."
        if payload.get("mode") == "native"
        else "SQLite results are reported only for portable cases. Vendor cases remain "
        "`PENDING_NATIVE` until the matching engine is enabled."
    )
    lines = [
        "# Phase 1 teaching dialect matrix",
        "",
        execution_note,
        "Explicit semantic boundaries require native execution and a matching structural mutation, but are not reported as full-flow behavioral proofs.",
        "",
        f"Total cases: **{summary['total']}**",
        f"Semantic boundaries: **{len(summary['semantic_boundary_ids'])}**",
        "",
        "| Dialect | Cases | Structure | AST diff | Execution | Data | Mutation | Full flow |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for dialect in sorted(summary["by_dialect"]):
        values = summary["by_dialect"][dialect]
        count = sum(1 for item in payload["results"] if item["case"]["dialect"] == dialect)
        lines.append(
            f"| {dialect} | {count} | {values['structure']} | {values['ast_diff']} | "
            f"{values['execution']} | {values['data']} | {values['mutation']} | {values['full_flow']} |"
        )
    lines.extend(["", "| Case | Dialect | Expectation | Structure | AST diff | Execution | Data | Mutation | Full flow |", "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"])
    for item in payload["results"]:
        case = item["case"]
        statuses = [item.get(stage, {}).get("status", "NOT_RUN") for stage in ("structure", "ast_diff", "execution", "data", "mutation", "full_flow")]
        lines.append(
            f"| {case['case_id']} | {case['dialect']} | "
            f"{case['execution_expectation']} | "
            + " | ".join(statuses)
            + " |"
        )
    boundary_results = [
        item
        for item in payload["results"]
        if item["case"]["execution_expectation"]
        == SEMANTIC_BOUNDARY_EXPECTATION
    ]
    if boundary_results:
        lines.extend(["", "## Explicit semantic boundaries", ""])
        for item in boundary_results:
            lines.append(
                f"- `{item['case']['case_id']}`: {item['case']['boundary_reason']}"
            )
    return "\n".join(lines) + "\n"


def _write_markdown(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(render_markdown(payload), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--native", action="store_true", help="run cases against configured native URLs")
    parser.add_argument("--fail-on-gap", action="store_true")
    parser.add_argument("--output-json", type=Path)
    parser.add_argument("--output-md", type=Path)
    args = parser.parse_args()

    if args.native:
        _load_native_environment()
    default_json, default_md = _default_output_paths(native=args.native)
    output_json = args.output_json or default_json
    output_md = args.output_md or default_md

    payload = build_report(native=args.native)
    results = payload["results"]
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(json.dumps(payload, ensure_ascii=True, indent=2, default=list) + "\n", encoding="utf-8")
    _write_markdown(output_md, payload)

    print(json.dumps(payload["summary"], ensure_ascii=True, indent=2, default=dict))
    if not args.fail_on_gap:
        return 0
    for item in results:
        for stage in ("structure", "ast_diff"):
            if item.get(stage, {}).get("status") != "PASS":
                return 1
        if not item["case"]["native_required"]:
            if item.get("full_flow", {}).get("status") != "PASS":
                return 1
        elif args.native:
            expected_status = (
                SEMANTIC_BOUNDARY_STATUS
                if item["case"]["execution_expectation"]
                == SEMANTIC_BOUNDARY_EXPECTATION
                else "PASS"
            )
            if item.get("full_flow", {}).get("status") != expected_status:
                return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
