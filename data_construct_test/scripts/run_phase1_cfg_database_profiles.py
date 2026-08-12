"""Cross-check Phase 1 CFG pairs over diverse random database profiles."""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = PROJECT_ROOT / "sql-edu-backend"
OUTPUT_DIR = PROJECT_ROOT / "data_construct_test" / "outputs"
sys.path.insert(0, str(BACKEND_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.parseval_data_generator import (  # noqa: E402
    _execute_sqlite,
    _has_node,
    _parse_sql,
    _rows_equivalent,
    extract_ast_diffs,
    generate_test_database,
    parse_schema_text,
    transpile_to_sqlite,
)
from run_phase1_cfg_fragment_benchmark import build_cases  # noqa: E402


PROFILES = (
    "targeted",
    "empty",
    "singleton",
    "uniform",
    "null_heavy",
    "duplicate_heavy",
    "group_skew",
    "join_aligned",
)
KNOWN_BOUNDARY_IDS = {
    "from_lateral_correlated",
    "group_rollup",
    "group_cube",
    "set_intersect_all",
    "set_except_all",
}
NUMERIC_TOKENS = (
    "id", "credit", "salary", "amount", "budget", "year", "score", "count",
    "number", "value", "rank", "total",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seeds", type=int, default=8)
    parser.add_argument("--seed", type=int, default=20260722)
    return parser.parse_args()


def _is_numeric(column: str) -> bool:
    lowered = column.lower()
    return any(token in lowered for token in NUMERIC_TOKENS)


def _value(column: str, row: int, rng: random.Random, profile: str) -> Any:
    name = column.lower()
    if profile == "null_heavy" and rng.random() < 0.48:
        return None
    if _is_numeric(column):
        choices: list[Any] = [-10, -1, 0, 1, 2, 3, 4, 5, 6, 10, 90, 100, 1000, 50000, 90000]
        if "amount" in name or "salary" in name:
            choices.extend([1.25, 3.5, -2.75])
        if profile == "group_skew":
            return [0, 1, 1, 1, 3][row % 5]
        return rng.choice(choices)
    if "dept" in name:
        return rng.choice([None, "CS", "Comp. Sci.", "Math", "Physics", "History"])
    if "title" in name:
        return rng.choice([None, "Data", "DataX", "DataLong", "Database", "Sales Manager"])
    if "name" in name:
        return rng.choice([None, "Alice", "Bob", "Carol", " Alice ", "O'Brien"])
    if "grade" in name:
        return rng.choice([None, "A", "B", "C"])
    if "city" in name:
        return rng.choice(["Beijing", "Shanghai", "Nanjing"])
    if profile == "group_skew":
        return f"g{row % 2}"
    return rng.choice([None, "x", "y", "Data%", "Unknown"])


def generate_database(
    schema: dict[str, list[str]],
    profile: str,
    seed: int,
    standard_sql: str = "",
    student_sql: str = "",
) -> dict[str, list[dict[str, Any]]]:
    if profile == "targeted":
        return generate_test_database(
            schema,
            standard_sql,
            student_sql,
            max_rows_per_table=10,
            ast_diffs=extract_ast_diffs(standard_sql, student_sql),
        )
    rng = random.Random(seed)
    row_counts = {
        "empty": 0,
        "singleton": 1,
        "uniform": 7,
        "null_heavy": 9,
        "duplicate_heavy": 8,
        "group_skew": 13,
        "join_aligned": 10,
    }
    count = row_counts[profile]
    data: dict[str, list[dict[str, Any]]] = {}
    for table, columns in schema.items():
        rows = [
            {column: _value(column, row, rng, profile) for column in columns}
            for row in range(count)
        ]
        if profile == "duplicate_heavy" and rows:
            for row in range(1, len(rows)):
                for column in columns:
                    if "id" not in column.lower():
                        rows[row][column] = rows[row % 2][column]
        if profile == "join_aligned":
            for row_index, row in enumerate(rows):
                for column in columns:
                    if "id" in column.lower() or column.lower().endswith("number"):
                        row[column] = row_index % 4
        data[table] = rows
    return data


def execute_pair(case: dict[str, Any], database: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    schema = parse_schema_text(case["schema"])
    standard_sqlite = transpile_to_sqlite(case["standard"])
    student_sqlite = transpile_to_sqlite(case["student"])
    if not standard_sqlite or not student_sqlite:
        return {"executed": False, "error": "transpile_failed"}
    try:
        std_cols, std_rows = _execute_sqlite(schema, database, standard_sqlite)
        stu_cols, stu_rows = _execute_sqlite(schema, database, student_sqlite)
    except Exception as exc:
        return {"executed": False, "error": f"{type(exc).__name__}: {exc}"}
    ast = _parse_sql(case["standard"])
    ordered = bool(ast and _has_node(ast, __import__("sqlglot").exp.Order))
    equivalent = _rows_equivalent(std_cols, std_rows, stu_cols, stu_rows, ordered)
    return {
        "executed": True,
        "error": None,
        "is_equivalent": equivalent,
        "standard_columns": std_cols,
        "student_columns": stu_cols,
        "standard_rows": std_rows,
        "student_rows": stu_rows,
    }


def render_markdown(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    lines = [
        "# Phase 1 CFG Database Profile Attacks",
        "",
        f"Generated at: `{payload['generated_at']}`",
        "",
        "Random database profiles can find counterexamples, but cannot prove equivalence over all finite databases.",
        "",
        f"- Executions: `{summary['executions']}`",
        f"- Cases: `{summary['cases']}`",
        f"- Profiles: `{summary['profiles']}`",
        f"- Seeds per profile: `{summary['seeds_per_profile']}`",
        f"- Equivalent-pair violations: `{summary['equivalent_pair_violations']}`",
        f"- Execution errors: `{summary['execution_errors']}`",
        f"- Negative cases distinguished by at least one profile: `{summary['negative_case_detection_rate']:.2%}`",
        f"- Negative cases distinguished by random profiles only: `{summary['random_profile_negative_detection_rate']:.2%}`",
        "",
        "## Equivalent Counterexamples",
        "",
    ]
    violations = payload["equivalent_violations"]
    if not violations:
        lines.append("No equivalent-pair counterexample was found in this profile corpus.")
    for item in violations:
        lines.extend([
            f"### {item['case_id']}",
            "",
            f"- Profile/seed: `{item['profile']}` / `{item['seed']}`",
            f"- Standard: `{item['standard']}`",
            f"- Student: `{item['student']}`",
            f"- Database: `{item['database']}`",
            f"- Rows: `{item['standard_rows']}` vs `{item['student_rows']}`",
            "",
        ])
    return "\n".join(lines) + "\n"


def main() -> None:
    args = parse_args()
    if args.seeds <= 0:
        raise SystemExit("seeds must be positive")
    cases = [
        case for case in build_cases()
        if case["expectation"] != "syntax_rejected" and case["id"] not in KNOWN_BOUNDARY_IDS
    ]
    results: list[dict[str, Any]] = []
    negative_detection: Counter[str] = Counter()
    equivalent_violations: list[dict[str, Any]] = []
    execution_errors: list[dict[str, Any]] = []

    for case in cases:
        schema = parse_schema_text(case["schema"])
        for profile in PROFILES:
            for seed_offset in range(args.seeds):
                seed = args.seed + seed_offset * 1009 + sum(ord(char) for char in case["id"])
                database = generate_database(schema, profile, seed, case["standard"], case["student"])
                run = execute_pair(case, database)
                item = {
                    "case_id": case["id"],
                    "production": case["production"],
                    "alternative": case["alternative"],
                    "expectation": case["expectation"],
                    "profile": profile,
                    "seed": seed,
                    "schema": case["schema"],
                    "standard": case["standard"],
                    "student": case["student"],
                    "database": database,
                    **run,
                }
                results.append(item)
                if not run["executed"]:
                    execution_errors.append(item)
                elif case["expectation"] == "equivalent" and run["is_equivalent"] is False:
                    equivalent_violations.append(item)
                elif case["expectation"] == "not_equivalent" and run["is_equivalent"] is False:
                    negative_detection[case["id"]] += 1

    negative_cases = [case for case in cases if case["expectation"] == "not_equivalent"]
    detected_cases = sum(negative_detection[case["id"]] > 0 for case in negative_cases)
    random_detected_cases = sum(
        any(
            item["case_id"] == case["id"]
            and item["profile"] != "targeted"
            and item.get("is_equivalent") is False
            for item in results
        )
        for case in negative_cases
    )
    summary = {
        "executions": len(results),
        "cases": len(cases),
        "profiles": list(PROFILES),
        "seeds_per_profile": args.seeds,
        "equivalent_pair_violations": len(equivalent_violations),
        "equivalent_cases_with_violation": len({item["case_id"] for item in equivalent_violations}),
        "execution_errors": len(execution_errors),
        "negative_cases": len(negative_cases),
        "negative_cases_detected": detected_cases,
        "negative_case_detection_rate": detected_cases / len(negative_cases) if negative_cases else 1.0,
        "random_profile_negative_cases_detected": random_detected_cases,
        "random_profile_negative_detection_rate": random_detected_cases / len(negative_cases) if negative_cases else 1.0,
        "profile_counts": dict(Counter(item["profile"] for item in results)),
    }
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "base_seed": args.seed,
        "summary": summary,
        "equivalent_violations": equivalent_violations,
        "execution_errors": execution_errors,
        "negative_detection_counts": dict(negative_detection),
    }
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUTPUT_DIR / "phase1_cfg_database_profiles_report.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    (OUTPUT_DIR / "phase1_cfg_database_profiles_report.md").write_text(
        render_markdown(payload), encoding="utf-8"
    )
    (OUTPUT_DIR / "phase1_cfg_database_profiles_all.jsonl").write_text(
        "\n".join(json.dumps(item, ensure_ascii=False, default=str) for item in results) + "\n",
        encoding="utf-8",
    )
    (OUTPUT_DIR / "phase1_cfg_database_profiles_counterexamples.jsonl").write_text(
        "\n".join(json.dumps(item, ensure_ascii=False, default=str) for item in equivalent_violations)
        + ("\n" if equivalent_violations else ""),
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
