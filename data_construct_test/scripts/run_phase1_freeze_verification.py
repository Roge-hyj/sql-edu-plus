"""Run a reproducible, non-feedback Phase 1 freeze verification.

The runner fingerprints code, corpus, configuration, dependencies, engines,
and selected artifacts before evaluating a bounded hidden paired set with the
independent Gold Oracle.  Hidden failures are recorded as digests and
statistics only; this command never edits implementation files and never
feeds hidden outcomes into an optimization loop.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import random
import sqlite3
import subprocess
import sys
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parents[1]
sys.path.insert(0, str(SCRIPT_DIR))

from phase1_gold_oracle import (  # noqa: E402
    EQUIVALENT,
    ENGINE_GAP,
    INPUT_GAP,
    NOT_EQUIVALENT,
    UNDECIDED,
    run_gold_oracle,
)
import build_phase1_mutation_layer as mutation_builder  # noqa: E402


def _schema_size_label(schema: Any) -> str:
    """Return a non-sensitive schema-size stratum for freeze reporting."""
    text = str(schema or "")
    table_count = 0
    column_count = 0
    depth = 0
    quote: str | None = None
    for char in text:
        if quote:
            if char == quote:
                quote = None
            continue
        if char in "'\"`":
            quote = char
        elif char == "(":
            if depth == 0:
                table_count += 1
            depth += 1
        elif char == ")":
            depth = max(0, depth - 1)
        elif char == "," and depth == 1:
            column_count += 1
    if table_count == 0:
        return "unknown"
    column_count += table_count
    if table_count == 1 and column_count <= 8:
        return "small_1_table_1_8_cols"
    if table_count == 1 and column_count <= 32:
        return "medium_1_table_9_32_cols"
    if table_count > 1:
        return "multi_table"
    return "large"


def _sha256_file(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _run_git(*args: str) -> str:
    try:
        return subprocess.run(
            ["git", *args],
            cwd=PROJECT_ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unavailable"


def _code_fingerprint() -> dict[str, Any]:
    diff = _run_git("diff", "--binary", "--", "*.py", "*.json", "*.jsonl", "*.md", "*.txt")
    status = _run_git("status", "--short")
    return {
        "git_head": _run_git("rev-parse", "HEAD"),
        "git_status_sha256": _sha256_text(status),
        "working_tree_diff_sha256": _sha256_text(diff),
        "working_tree_dirty": bool(status),
    }


def _versions() -> dict[str, Any]:
    package_names = ("sqlglot", "pydantic", "pydantic-settings", "pytest")
    packages: dict[str, str] = {}
    for name in package_names:
        try:
            packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            packages[name] = "unavailable"
    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "packages": packages,
        "sqlite": sqlite3.sqlite_version,
        "configured_native_engine_versions": {
            key: value
            for key, value in sorted(os.environ.items())
            if key.startswith("PARSEVAL_") and key.endswith("_VERSION")
        },
    }


def _read_hidden_records(path: Path) -> list[dict[str, Any]]:
    """Read only at freeze time; generated student SQL stays in memory."""
    if path.name != "hidden.jsonl":
        raise ValueError("freeze verification requires the universe hidden.jsonl file")
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            record = json.loads(line)
            if not isinstance(record, dict):
                continue
            if str(record.get("partition") or "").lower() != "hidden":
                raise ValueError("hidden record does not carry hidden partition tag")
            if isinstance(record.get("sql"), str) and record["sql"].strip():
                rows.append(record)
    return rows


def _select(rows: list[dict[str, Any]], limit: int, seed: int) -> list[dict[str, Any]]:
    if limit <= 0 or limit >= len(rows):
        return sorted(rows, key=lambda item: str(item.get("family_id") or ""))
    rng = random.Random(seed)
    selected: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        if index < limit:
            selected.append(row)
            continue
        replacement = rng.randrange(index + 1)
        if replacement < limit:
            selected[replacement] = row
    return sorted(selected, key=lambda item: str(item.get("family_id") or ""))


def _freeze_pairs(
    records: list[dict[str, Any]],
    *,
    partition_label: str = "hidden",
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Create one mutation and one control per frozen-scope family in memory.

    This deliberately reuses the public mutation operators but does not write
    the generated SQL or any per-row oracle result.  A missing operator is a
    freeze coverage gap, never an implicit pass.
    """
    planned: dict[str, list[str]] = {}
    parsed: dict[str, tuple[str | None, int, list[str]]] = {}
    failures: list[str] = []
    for record in records:
        family_id = str(record.get("family_id") or "")
        dialect = mutation_builder._dialect_of(record)
        schema_text = str(record.get("schema") or "")
        tree = mutation_builder._parse(
            str(record["sql"]),
            dialect,
            schema_text,
        )
        if tree is None:
            failures.append(f"{family_id}:parse")
            continue
        baseline = mutation_builder._render(tree, dialect, schema_text)
        if baseline is None:
            failures.append(f"{family_id}:render")
            continue
        index = mutation_builder._pick_index(family_id, "phase1-hidden-freeze-v1", mutation_builder.INDEX_SPACE)
        names = mutation_builder._applicable_families(
            tree,
            baseline,
            dialect=dialect,
            index=index,
            schema_text=str(record.get("schema") or ""),
        )
        if not names:
            failures.append(f"{family_id}:operator")
            continue
        planned[family_id] = names
        parsed[family_id] = (dialect, index, names)
    assignment = mutation_builder._balance(planned)
    pairs: list[dict[str, Any]] = []
    emitted = Counter()
    by_operator: Counter[str] = Counter()
    mutation_families: set[str] = set()
    equivalence_families: set[str] = set()
    source_by_category: Counter[str] = Counter()
    source_by_dialect: Counter[str] = Counter()
    source_by_schema_size: Counter[str] = Counter()
    for record in records:
        for category in record.get("categories") or ["unknown"]:
            source_by_category[str(category)] += 1
        source_by_dialect[str(record.get("dialect") or "generic").lower()] += 1
        source_by_schema_size[_schema_size_label(record.get("schema"))] += 1
    for record in records:
        family_id = str(record.get("family_id") or "")
        if family_id not in assignment:
            continue
        dialect, index, _names = parsed[family_id]
        operator_family = assignment[family_id]
        mutation = mutation_builder.apply_mutation(
            str(record["sql"]),
            operator_family,
            dialect=dialect,
            index=index,
            schema_text=str(record.get("schema") or ""),
        )
        if mutation is None:
            failures.append(f"{family_id}:mutation")
            continue
        gold_sql, student_sql, operator = mutation
        pairs.append({
            **record,
            # The mutation builder returns the canonical AST-rendered gold
            # side.  Using the raw source here would reintroduce parser-only
            # spelling differences (notably numeric-leading identifiers).
            "sql": gold_sql,
            "sql_source_raw": record.get("sql"),
            "student_sql": student_sql,
            "expectation": NOT_EQUIVALENT,
            "freeze_role": "mutation",
        })
        emitted["mutation"] += 1
        mutation_families.add(family_id)
        by_operator[operator] += 1
        control = mutation_builder.apply_equivalence(
            str(record["sql"]),
            dialect=dialect,
            index=index,
            # Use the current record's schema.  ``schema_text`` was only a
            # scratch value in the parsing pass above; reusing it here makes
            # equivalence controls depend on the last hidden row processed.
            schema_text=str(record.get("schema") or ""),
        )
        if control is None:
            failures.append(f"{family_id}:equivalence")
            continue
        control_gold, control_sql, tactic = control
        pairs.append({
            **record,
            # Keep the control paired with the exact baseline AST from which
            # the equivalence rewrite was rendered.
            "sql": control_gold,
            "sql_source_raw": record.get("sql"),
            "student_sql": control_sql,
            "expectation": EQUIVALENT,
            "freeze_role": "equivalence",
        })
        emitted["equivalence"] += 1
        equivalence_families.add(family_id)
        by_operator[tactic] += 1
    supported_families = mutation_families & equivalence_families
    failure_reasons = Counter(
        str(item).rsplit(":", 1)[-1]
        for item in failures
        if ":" in str(item)
    )
    summary = {
        "source_families": len(records),
        "families_with_mutation": emitted["mutation"],
        "families_with_equivalence_control": emitted["equivalence"],
        "families_in_declared_supported_scope": len(supported_families),
        "declared_supported_scope": (
            f"{partition_label} families for which one mutation and one "
            "equivalence control were generated by the frozen public mutation "
            "operators"
        ),
        "scope_coverage_rate": (
            len(supported_families) / len(records) if records else 0.0
        ),
        "pair_rows": len(pairs),
        "generation_failures": len(failures),
        "failure_reasons": dict(sorted(failure_reasons.items())),
        "generation_failure_digest": _sha256_text("\n".join(sorted(failures))) if failures else None,
        "declared_scope_family_digest": _sha256_text(
            "\n".join(sorted(supported_families))
        ) if supported_families else None,
        "operator_digest": _sha256_text(json.dumps(dict(sorted(by_operator.items())), sort_keys=True)) if by_operator else None,
        "source_strata": {
            "by_category": dict(sorted(source_by_category.items())),
            "by_dialect": dict(sorted(source_by_dialect.items())),
            "by_schema_size": dict(sorted(source_by_schema_size.items())),
        },
    }
    return pairs, summary


def _hidden_pairs(records: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Backward-compatible hidden wrapper for existing tests and callers."""
    return _freeze_pairs(records, partition_label="hidden")


def _evaluate(rows: list[dict[str, Any]], seeds: tuple[int, ...], scales: tuple[int, ...]) -> dict[str, Any]:
    verdicts = Counter()
    by_category: Counter[tuple[str, str]] = Counter()
    by_dialect: Counter[tuple[str, str]] = Counter()
    by_schema_size: Counter[tuple[str, str]] = Counter()
    mismatches: list[str] = []
    for row in rows:
        result = run_gold_oracle(
            row.get("schema"),
            row.get("sql"),
            row.get("student_sql"),
            schema_catalog=row.get("schema_catalog"),
            dialect=row.get("dialect"),
            expected=row.get("expectation"),
            seeds=seeds,
            row_scales=scales,
            max_rows_per_table=32,
        )
        verdict = str(result.get("verdict") or UNDECIDED)
        verdicts[verdict] += 1
        for category in row.get("categories") or ["unknown"]:
            by_category[(str(category), verdict)] += 1
        by_dialect[(str(row.get("dialect") or "generic").lower(), verdict)] += 1
        by_schema_size[(_schema_size_label(row.get("schema")), verdict)] += 1
        expected = str(row.get("expectation") or "").upper()
        if verdict in {EQUIVALENT, NOT_EQUIVALENT} and verdict != expected:
            mismatches.append(f"{row.get('family_id') or ''}:{row.get('freeze_role') or ''}")
    return {
        "rows": len(rows),
        "verdicts": dict(sorted(verdicts.items())),
        "by_category": {
            category: dict(sorted({verdict: count for (name, verdict), count in by_category.items() if name == category}.items()))
            for category in sorted({name for name, _verdict in by_category})
        },
        "by_dialect": {
            dialect: dict(sorted({verdict: count for (name, verdict), count in by_dialect.items() if name == dialect}.items()))
            for dialect in sorted({name for name, _verdict in by_dialect})
        },
        "by_schema_size": {
            size: dict(sorted({verdict: count for (name, verdict), count in by_schema_size.items() if name == size}.items()))
            for size in sorted({name for name, _verdict in by_schema_size})
        },
        "determinate_label_mismatches": len(mismatches),
        "failure_family_digest": _sha256_text("\n".join(sorted(mismatches))) if mismatches else None,
    }


def verify(
    hidden_path: Path,
    output_path: Path,
    *,
    hidden_limit: int,
    sample_seed: int,
    oracle_seeds: tuple[int, ...],
    row_scales: tuple[int, ...],
    artifacts: tuple[Path, ...],
) -> dict[str, Any]:
    hidden_records = _select(_read_hidden_records(hidden_path), hidden_limit, sample_seed)
    hidden_rows, generation = _hidden_pairs(hidden_records)
    first = _evaluate(hidden_rows, oracle_seeds, row_scales)
    second = _evaluate(hidden_rows, oracle_seeds, row_scales)
    artifact_hashes = {
        str(path): _sha256_file(path)
        for path in artifacts
    }
    manifest_path = hidden_path.parent / "manifest.json"
    config = {
        "hidden_path": str(hidden_path),
        "sample_seed": sample_seed,
        "hidden_limit": hidden_limit,
        "oracle_seeds": list(oracle_seeds),
        "row_scales": list(row_scales),
        "max_rows_per_table": 32,
        "feedback_policy": "hidden outcomes are report-only; no optimization input or implementation write is permitted",
    }
    result = {
        "schema_version": 1,
        "freeze_id": _sha256_text(json.dumps(config, sort_keys=True, separators=(",", ":"))),
        "frozen_at": datetime.now(timezone.utc).isoformat(),
        "mode": "final_hidden_verification_no_feedback",
        "code": _code_fingerprint(),
        "configuration": config,
        "versions": _versions(),
        "corpus": {
            "manifest_path": str(manifest_path),
            "manifest_sha256": _sha256_file(manifest_path),
            "hidden_file_sha256": _sha256_file(hidden_path),
            "snapshot_id": json.loads(manifest_path.read_text(encoding="utf-8")).get("snapshot_id") if manifest_path.is_file() else None,
        },
        "artifacts_sha256": artifact_hashes,
        "hidden_evaluation": {
            "hidden_partition_read": True,
            "selected_family_count": len(hidden_records),
            "generated_pair_rows": len(hidden_rows),
            "pair_generation": generation,
            "first_run": first,
            "second_run": second,
            "stable": first == second,
            "failures_saved_as_digest_only": True,
        },
        "acceptance": {
            "freeze_inputs_complete": bool(_sha256_file(hidden_path) and _sha256_file(manifest_path)),
            "repeat_run_stable": first == second,
            "no_determinate_label_mismatch": first["determinate_label_mismatches"] == 0,
            # A final freeze must not silently turn unsupported hidden rows into
            # a pass.  The current raw hidden snapshot contains canonical SQL
            # for which the public mutation operators do not yet provide a
            # paired witness/control.  Keep that boundary explicit and fail the
            # full-coverage gate until every hidden family is in scope.
            "hidden_generation_scope_complete": generation["generation_failures"] == 0,
            "declared_scope_nonempty": generation["families_in_declared_supported_scope"] > 0,
        },
    }
    result["acceptance"]["pass"] = all(result["acceptance"].values())
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return result


def _ints(value: str) -> tuple[int, ...]:
    parsed = tuple(int(item.strip()) for item in value.split(",") if item.strip())
    if not parsed:
        raise ValueError("at least one integer is required")
    return parsed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hidden", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--hidden-limit", type=int, default=0)
    parser.add_argument("--sample-seed", type=int, default=20260821)
    parser.add_argument("--oracle-seeds", default="0,1,2")
    parser.add_argument("--row-scales", default="4,8,16")
    parser.add_argument("--artifact", action="append", type=Path, default=[])
    args = parser.parse_args(argv)
    if args.hidden_limit < 0:
        raise SystemExit("hidden-limit must be non-negative")
    result = verify(
        args.hidden,
        args.output,
        hidden_limit=args.hidden_limit,
        sample_seed=args.sample_seed,
        oracle_seeds=_ints(args.oracle_seeds),
        row_scales=_ints(args.row_scales),
        artifacts=tuple(args.artifact),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["acceptance"]["pass"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
