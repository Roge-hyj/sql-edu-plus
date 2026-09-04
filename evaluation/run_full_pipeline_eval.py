from __future__ import annotations

import argparse
import hashlib
import json
import platform
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable


MAX_DATASET_BYTES = 2 * 1024 * 1024
MAX_CASES = 500
MAX_TEXT_CHARS = 16_384
MAX_RESULT_ROWS = 1_024
MAX_TABLE_ROWS = 32
SQLITE_VM_INSTRUCTIONS = 1_000_000
SQLITE_WALL_SECONDS = 0.75

FORBIDDEN_HINT_KEYS = {
    "standard_sql",
    "reference_sql",
    "answer_sql",
    "correct_sql",
    "replacement_sql",
    "replacement_source_sql",
    "replacement_sqlite",
    "mutation_sql",
    "test_database",
    "witness_world",
    "raw_observations",
}

EVIDENCE_GRADES = {
    "AST_ONLY": 0,
    "OUTPUT_ONLY": 1,
    "PAIR_DISTINGUISHED": 2,
    "REPAIR_VERIFIED": 3,
    "CAUSAL_VERIFIED": 4,
}


def parse_args() -> argparse.Namespace:
    script = Path(__file__).resolve()
    repo_root = script.parent.parent if script.parent.name == "evaluation" else Path.cwd()
    parser = argparse.ArgumentParser(
        description="Run the complete SQLite Phase1+Phase2 curated evaluation.",
    )
    parser.add_argument(
        "--dataset",
        type=Path,
        default=repo_root / "evaluation/cases/sqlite_phase12_verified.json",
    )
    parser.add_argument(
        "--backend",
        type=Path,
        default=repo_root / "sql-edu-backend",
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--repeat", type=int, default=1, choices=range(1, 4))
    return parser.parse_args()


def _load_dataset(path: Path) -> tuple[dict[str, Any], str]:
    size = path.stat().st_size
    if size <= 0 or size > MAX_DATASET_BYTES:
        raise ValueError(f"dataset size must be in 1..{MAX_DATASET_BYTES} bytes")
    raw = path.read_bytes()
    payload = json.loads(raw)
    if not isinstance(payload, dict) or payload.get("format_version") != 1:
        raise ValueError("unsupported dataset format")
    if payload.get("engine") != "sqlite":
        raise ValueError("dataset must declare engine=sqlite")
    cases = payload.get("cases")
    if not isinstance(cases, list) or not 1 <= len(cases) <= MAX_CASES:
        raise ValueError(f"dataset must contain 1..{MAX_CASES} cases")
    contexts = payload.get("contexts")
    if not isinstance(contexts, dict):
        raise ValueError("contexts must be an object")

    identifiers: list[str] = []
    triples: list[tuple[str, str, str]] = []
    for raw_case in cases:
        if not isinstance(raw_case, dict):
            raise ValueError("every case must be an object")
        case_id = raw_case.get("id")
        if not isinstance(case_id, str) or not case_id or len(case_id) > 160:
            raise ValueError("case id must be a non-empty bounded string")
        identifiers.append(case_id)
        context_id = raw_case.get("context_id")
        if context_id is not None:
            if context_id not in contexts:
                raise ValueError(f"{case_id}: unknown context {context_id}")
            schema = contexts[context_id].get("schema_text")
        else:
            schema = raw_case.get("schema_text")
        reference = raw_case.get("reference_sql")
        student = raw_case.get("student_sql")
        for label, value in (
            ("schema", schema),
            ("reference_sql", reference),
            ("student_sql", student),
        ):
            if not isinstance(value, str) or len(value) > MAX_TEXT_CHARS:
                raise ValueError(f"{case_id}: {label} must be a bounded string")
        triples.append(tuple(" ".join(value.split()).casefold() for value in (schema, reference, student)))
        if not isinstance(raw_case.get("expectation"), dict):
            raise ValueError(f"{case_id}: expectation must be an object")

    if len(identifiers) != len(set(identifiers)):
        raise ValueError("duplicate case ids")
    if len(triples) != len(set(triples)):
        raise ValueError("duplicate schema/reference/student triples")
    declared = payload.get("declared_counts")
    actual = dict(sorted(Counter(str(case.get("suite")) for case in cases).items()))
    if declared != actual:
        raise ValueError(f"declared suite counts differ: declared={declared}, actual={actual}")
    return payload, hashlib.sha256(raw).hexdigest()


def _all_keys(value: Any) -> Iterable[str]:
    if isinstance(value, dict):
        for key, nested in value.items():
            yield str(key).casefold()
            yield from _all_keys(nested)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            yield from _all_keys(nested)


def _candidate_rules(package: Any) -> list[str]:
    rules: list[str] = []
    if package.primary is not None:
        rules.append(str(package.primary.rule_id))
    rules.extend(str(item.rule_id) for item in package.secondary)
    rules.extend(str(item.rule_id) for item in package.unresolved)
    rules.extend(
        str(item.get("rule_id") or "")
        for item in package.suppressed
        if isinstance(item, dict)
    )
    return list(dict.fromkeys(rule for rule in rules if rule))


def _hint_audit(result: Any, reference_sql: str) -> tuple[bool, list[str], str]:
    issues: list[str] = []
    payloads: list[str] = []
    for level in (1, 2, 3):
        hint = result.learner_hint(level)
        encoded = json.dumps(hint, ensure_ascii=False, sort_keys=True, default=str)
        payloads.append(encoded)
        leaked = FORBIDDEN_HINT_KEYS.intersection(set(_all_keys(hint)))
        if leaked:
            issues.append(f"level_{level}_forbidden_keys:{','.join(sorted(leaked))}")
        if reference_sql.strip() and reference_sql.strip() in encoded:
            issues.append(f"level_{level}_full_reference_sql")
        if hint.get("engine") != "sqlite":
            issues.append(f"level_{level}_wrong_engine")
        expected_witness = level == 2 and result.phase2.witness is not None
        if ("witness" in hint) is not expected_witness:
            issues.append(f"level_{level}_witness_slot")
    digest = hashlib.sha256("\n".join(payloads).encode("utf-8")).hexdigest()
    return not issues, issues, digest


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _normalize_rows(
    rows: Iterable[Iterable[Any]], normalize_cell: Any
) -> list[tuple[Any, ...]]:
    return [tuple(normalize_cell(cell) for cell in row) for row in rows]


def _independent_replay(
    *,
    schema_text: str,
    run: Any,
    parse_schema_text: Any,
    parse_schema_column_types: Any,
    sqlite_declared_affinity: Any,
    normalize_cell: Any,
) -> tuple[bool, str | None]:
    if not run.executed or not run.standard_sqlite or not run.student_sqlite:
        return True, None
    schema = parse_schema_text(schema_text)
    schema_types = parse_schema_column_types(schema_text)
    if not schema:
        return False, "schema_parse_empty"
    connection = sqlite3.connect(":memory:")
    progress_calls = 0
    deadline = time.monotonic() + SQLITE_WALL_SECONDS

    def bounded_progress() -> int:
        nonlocal progress_calls
        progress_calls += 1
        exhausted = progress_calls * 10_000 >= SQLITE_VM_INSTRUCTIONS
        expired = time.monotonic() >= deadline
        return int(exhausted or expired)

    connection.set_progress_handler(bounded_progress, 10_000)
    try:
        rows_by_table = run.test_database or {}
        for table, columns in schema.items():
            if table not in rows_by_table:
                continue
            type_key = next(
                (
                    name for name in schema_types
                    if str(name).casefold() == str(table).casefold()
                ),
                None,
            )
            declared_types = schema_types.get(type_key or "", {})
            normalized_types = {
                str(column).casefold(): declared
                for column, declared in declared_types.items()
            }
            definitions = ", ".join(
                f"{_quote_identifier(str(column))} "
                f"{sqlite_declared_affinity(str(column), normalized_types.get(str(column).casefold()))}"
                for column in columns
            )
            connection.execute(
                f"CREATE TABLE {_quote_identifier(str(table))} ({definitions})"
            )
            rows = rows_by_table[table]
            if not rows:
                continue
            if len(rows) > MAX_TABLE_ROWS:
                return False, f"table_row_limit:{table}:{len(rows)}"
            names = ", ".join(_quote_identifier(str(column)) for column in columns)
            placeholders = ", ".join("?" for _ in columns)
            statement = (
                f"INSERT INTO {_quote_identifier(str(table))} ({names}) "
                f"VALUES ({placeholders})"
            )
            connection.executemany(
                statement,
                [tuple(row.get(column) for column in columns) for row in rows],
            )
        try:
            reference_rows = connection.execute(run.standard_sqlite).fetchmany(MAX_RESULT_ROWS + 1)
        except sqlite3.Error as exc:
            return False, f"reference_error:{type(exc).__name__}:{exc}"
        if len(reference_rows) > MAX_RESULT_ROWS:
            return False, "reference_result_limit"
        try:
            student_rows = connection.execute(run.student_sqlite).fetchmany(MAX_RESULT_ROWS + 1)
            replay_student_error = None
        except sqlite3.Error as exc:
            student_rows = []
            replay_student_error = str(exc)
        if len(student_rows) > MAX_RESULT_ROWS:
            return False, "student_result_limit"

        pipeline_student_error = str(
            (run.data_evidence or {}).get("student_exec_error") or ""
        )
        if _normalize_rows(reference_rows, normalize_cell) != _normalize_rows(
            run.standard_rows, normalize_cell
        ):
            return False, "reference_rows_disagree"
        if bool(replay_student_error) != bool(pipeline_student_error):
            return False, "student_error_state_disagrees"
        if replay_student_error is None and (
            _normalize_rows(student_rows, normalize_cell)
            != _normalize_rows(run.student_rows, normalize_cell)
        ):
            return False, "student_rows_disagree"
        return True, None
    except sqlite3.Error as exc:
        return False, f"replay_setup:{type(exc).__name__}:{exc}"
    finally:
        connection.close()


def _check_expectation(
    expectation: dict[str, Any],
    *,
    phase1: Any,
    phase2: Any,
    candidate_rules: list[str],
    hint_safe: bool,
    replay_ok: bool,
) -> list[str]:
    failures: list[str] = []
    exact = {
        "phase1_status": phase1.status,
        "conclusion": phase1.equivalence_conclusion,
        "phase2_verdict": phase2.verdict,
    }
    for key, actual in exact.items():
        expected = expectation.get(key)
        if expected is not None and actual != expected:
            failures.append(f"{key}:expected={expected}:actual={actual}")
    allowed = {
        "allowed_phase1_statuses": phase1.status,
        "allowed_conclusions": phase1.equivalence_conclusion,
        "allowed_phase2_verdicts": phase2.verdict,
    }
    for key, actual in allowed.items():
        values = expectation.get(key)
        if values is not None and actual not in values:
            failures.append(f"{key}:actual={actual}")
    forbidden = expectation.get("forbid_conclusion")
    if forbidden is not None and phase1.equivalence_conclusion == forbidden:
        failures.append(f"forbidden_conclusion:{forbidden}")

    primary = phase2.primary
    if expectation.get("require_primary") is True and primary is None:
        failures.append("primary_missing")
    if expectation.get("require_primary") is False and primary is not None:
        failures.append(f"unexpected_primary:{primary.rule_id}")
    expected_primary = expectation.get("primary_rule")
    if expected_primary is not None and (
        primary is None or primary.rule_id != expected_primary
    ):
        failures.append(
            f"primary_rule:expected={expected_primary}:"
            f"actual={primary.rule_id if primary else None}"
        )
    expected_candidate = expectation.get("candidate_rule")
    if expected_candidate is not None and expected_candidate not in candidate_rules:
        failures.append(f"candidate_rule_missing:{expected_candidate}")

    minimum_grade = expectation.get("minimum_primary_grade")
    if minimum_grade is not None:
        actual_grade = primary.evidence_grade if primary is not None else None
        if (
            actual_grade not in EVIDENCE_GRADES
            or EVIDENCE_GRADES[actual_grade] < EVIDENCE_GRADES[minimum_grade]
        ):
            failures.append(
                f"primary_grade:minimum={minimum_grade}:actual={actual_grade}"
            )
    if expectation.get("require_phase1_witness"):
        if not phase1.test_database:
            failures.append("phase1_witness_database_missing")
        if not (phase1.data_evidence or {}).get("any_world_distinguished"):
            failures.append("phase1_witness_not_distinguished")
    if expectation.get("require_phase2_evidence"):
        witness = phase2.witness or {}
        has_cases = bool(witness.get("cases"))
        has_delta = bool(witness.get("result_delta"))
        if not (has_cases or has_delta):
            failures.append("phase2_evidence_missing")
    mutation_summary = (phase1.mutation_evidence or {}).get("summary") or {}
    if expectation.get("require_repair") and int(
        mutation_summary.get("fixed_by_replacement") or 0
    ) <= 0:
        failures.append("repair_not_verified")
    if expectation.get("require_safe_hints") and not hint_safe:
        failures.append("hint_audit_failed")
    if phase1.executed and not replay_ok:
        failures.append("independent_replay_failed")
    if phase1.executed:
        if (phase1.data_evidence or {}).get("execution_backend") != "sqlite":
            failures.append("execution_backend_not_sqlite")
        if (phase1.data_evidence or {}).get("sql_dialect") != "sqlite":
            failures.append("sql_dialect_not_sqlite")
        max_rows = max(
            (len(rows) for rows in (phase1.test_database or {}).values()),
            default=0,
        )
        if max_rows > MAX_TABLE_ROWS:
            failures.append(f"table_row_limit_exceeded:{max_rows}")
    return failures


def _run_case(
    case: dict[str, Any],
    *,
    contexts: dict[str, Any],
    run_pipeline: Any,
    parse_schema_text: Any,
    parse_schema_column_types: Any,
    sqlite_declared_affinity: Any,
    normalize_cell: Any,
) -> dict[str, Any]:
    context = contexts.get(case.get("context_id"), {})
    schema_text = str(context.get("schema_text", case.get("schema_text", "")))
    schema_catalog = context.get("schema_catalog")
    reference_sql = str(case["reference_sql"])
    started = time.perf_counter()
    result = run_pipeline(
        schema_text=schema_text,
        reference_sql=reference_sql,
        student_sql=str(case["student_sql"]),
        question=str(case.get("question") or ""),
        max_rows_per_table=8,
        schema_catalog=schema_catalog,
    )
    elapsed_ms = (time.perf_counter() - started) * 1_000
    phase1 = result.phase1
    phase2 = result.phase2
    candidate_rules = _candidate_rules(phase2)
    hint_safe, hint_issues, hint_digest = _hint_audit(result, reference_sql)
    replay_ok, replay_error = _independent_replay(
        schema_text=schema_text,
        run=phase1,
        parse_schema_text=parse_schema_text,
        parse_schema_column_types=parse_schema_column_types,
        sqlite_declared_affinity=sqlite_declared_affinity,
        normalize_cell=normalize_cell,
    )
    failures = _check_expectation(
        case["expectation"],
        phase1=phase1,
        phase2=phase2,
        candidate_rules=candidate_rules,
        hint_safe=hint_safe,
        replay_ok=replay_ok,
    )
    mutation_summary = (phase1.mutation_evidence or {}).get("summary") or {}
    phase2_witness = phase2.witness or {}
    stable = {
        "phase1_status": phase1.status,
        "conclusion": phase1.equivalence_conclusion,
        "judge_status": phase1.judge_status,
        "phase2_verdict": phase2.verdict,
        "diagnosis_status": phase2.diagnosis_status,
        "primary_rule": phase2.primary.rule_id if phase2.primary else None,
        "primary_grade": phase2.primary.evidence_grade if phase2.primary else None,
        "candidate_rules": candidate_rules,
        "repair_fixed": int(mutation_summary.get("fixed_by_replacement") or 0),
        "hint_digest": hint_digest,
        "witness": phase2.witness,
    }
    return {
        "id": case["id"],
        "suite": case.get("suite"),
        "family": case.get("family"),
        "passed": not failures,
        "failures": failures,
        "executed": bool(phase1.executed),
        **{key: value for key, value in stable.items() if key != "witness"},
        "hint_safe": hint_safe,
        "hint_issues": hint_issues,
        "independent_replay_ok": replay_ok,
        "independent_replay_error": replay_error,
        "generated_rows": sum(
            len(rows) for rows in (phase1.test_database or {}).values()
        ),
        "max_table_rows": max(
            (len(rows) for rows in (phase1.test_database or {}).values()),
            default=0,
        ),
        "elapsed_ms": round(elapsed_ms, 3),
        "digest": hashlib.sha256(
            json.dumps(stable, ensure_ascii=False, sort_keys=True, default=str).encode(
                "utf-8"
            )
        ).hexdigest(),
        "error_code": phase1.error_code,
        "error": str(phase1.error or "")[:300],
        "phase1_witness_distinguished": bool(
            (phase1.data_evidence or {}).get("any_world_distinguished")
        ),
        "phase2_witness_availability": phase2_witness.get("availability"),
        "phase2_witness_case_count": len(phase2_witness.get("cases") or []),
        "phase2_has_result_delta": bool(phase2_witness.get("result_delta")),
    }


def main() -> int:
    args = parse_args()
    dataset, dataset_sha256 = _load_dataset(args.dataset.resolve())
    backend = args.backend.resolve()
    if not (backend / "core/pipeline.py").is_file():
        raise FileNotFoundError(f"backend not found: {backend}")
    sys.path.insert(0, str(backend))
    import sqlglot  # noqa: PLC0415
    from core.parseval_data_generator import (  # noqa: PLC0415
        _normalize_cell,
        _sqlite_declared_affinity,
        parse_schema_column_types,
        parse_schema_text,
    )
    from core.pipeline import run_pipeline  # noqa: PLC0415

    all_runs: list[list[dict[str, Any]]] = []
    total_started = time.perf_counter()
    for repeat_index in range(args.repeat):
        rows: list[dict[str, Any]] = []
        for index, case in enumerate(dataset["cases"], start=1):
            row = _run_case(
                case,
                contexts=dataset["contexts"],
                run_pipeline=run_pipeline,
                parse_schema_text=parse_schema_text,
                parse_schema_column_types=parse_schema_column_types,
                sqlite_declared_affinity=_sqlite_declared_affinity,
                normalize_cell=_normalize_cell,
            )
            rows.append(row)
            print(
                f"repeat={repeat_index + 1}/{args.repeat} "
                f"case={index}/{len(dataset['cases'])} id={case['id']} "
                f"passed={row['passed']}",
                flush=True,
            )
        all_runs.append(rows)

    primary_run = all_runs[0]
    digest_mismatches: list[dict[str, Any]] = []
    expected_digests = {row["id"]: row["digest"] for row in primary_run}
    for repeat_index, rows in enumerate(all_runs[1:], start=2):
        for row in rows:
            expected = expected_digests[row["id"]]
            if row["digest"] != expected:
                digest_mismatches.append({
                    "repeat": repeat_index,
                    "id": row["id"],
                    "expected": expected,
                    "actual": row["digest"],
                })

    suite_stats: dict[str, dict[str, int]] = defaultdict(
        lambda: {"total": 0, "passed": 0}
    )
    for row in primary_run:
        stats = suite_stats[str(row["suite"])]
        stats["total"] += 1
        stats["passed"] += int(row["passed"])

    report = {
        "format_version": 1,
        "dataset": dataset["name"],
        "dataset_sha256": dataset_sha256,
        "engine": "sqlite",
        "environment": {
            "python": platform.python_version(),
            "sqlite": sqlite3.sqlite_version,
            "sqlglot": sqlglot.__version__,
        },
        "summary": {
            "cases": len(primary_run),
            "passed": sum(int(row["passed"]) for row in primary_run),
            "failed": sum(int(not row["passed"]) for row in primary_run),
            "repeats": args.repeat,
            "full_pipeline_calls": len(primary_run) * args.repeat,
            "suites": dict(sorted(suite_stats.items())),
            "phase1_statuses": dict(sorted(Counter(row["phase1_status"] for row in primary_run).items())),
            "conclusions": dict(sorted(Counter(row["conclusion"] for row in primary_run).items())),
            "phase2_verdicts": dict(sorted(Counter(row["phase2_verdict"] for row in primary_run).items())),
            "primary_rules": dict(sorted(Counter(row["primary_rule"] for row in primary_run if row["primary_rule"]).items())),
            "repair_verified_cases": sum(int(row["repair_fixed"] > 0) for row in primary_run),
            "hint_payloads_audited": len(primary_run) * 3 * args.repeat,
            "hint_safe_cases": sum(int(row["hint_safe"]) for row in primary_run),
            "phase1_distinguished_witness_cases": sum(
                int(row["phase1_witness_distinguished"]) for row in primary_run
            ),
            "phase2_physical_witness_cases": sum(
                int(row["phase2_witness_case_count"] > 0) for row in primary_run
            ),
            "phase2_output_delta_cases": sum(
                int(row["phase2_has_result_delta"]) for row in primary_run
            ),
            "phase2_witness_availability": dict(sorted(Counter(
                row["phase2_witness_availability"] or "NONE"
                for row in primary_run
            ).items())),
            "independent_replay_cases": sum(int(row["executed"]) for row in primary_run),
            "independent_replay_passed": sum(int(row["executed"] and row["independent_replay_ok"]) for row in primary_run),
            "determinism_mismatches": len(digest_mismatches),
            "max_generated_rows": max(row["generated_rows"] for row in primary_run),
            "max_table_rows": max(row["max_table_rows"] for row in primary_run),
            "elapsed_seconds": round(time.perf_counter() - total_started, 3),
        },
        "determinism_mismatches": digest_mismatches,
        "results": primary_run,
    }
    encoded = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2, sort_keys=True))
    return int(
        report["summary"]["failed"] > 0
        or report["summary"]["determinism_mismatches"] > 0
    )


if __name__ == "__main__":
    raise SystemExit(main())
