"""Compute explicit Phase 1 statistical acceptance metrics.

All primary denominators are unique question-family audit rows.  Unknown
outcomes are never converted into correctness: ``UNDECIDED``, ``ENGINE_GAP``
and ``INPUT_GAP`` remain separate counters and are excluded from correctness
denominators.  The report includes Wilson 95% intervals and deterministic
strata for category, source, dialect, and compact schema size.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
import math
from pathlib import Path
from typing import Any, Iterable


DETERMINATE = {"EQUIVALENT", "NOT_EQUIVALENT"}
EXCLUDED = {"UNDECIDED", "ENGINE_GAP", "INPUT_GAP"}


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                value = json.loads(line)
                if isinstance(value, dict):
                    rows.append(value)
    return rows


def _wilson(successes: int, trials: int, confidence: float = 0.95) -> dict[str, Any]:
    if trials < 0 or successes < 0 or successes > trials:
        raise ValueError("invalid binomial counts")
    if trials == 0:
        return {
            "successes": successes,
            "trials": trials,
            "rate": None,
            "lower": None,
            "upper": None,
            "confidence": confidence,
        }
    z = 1.959963984540054
    p = successes / trials
    denominator = 1 + z * z / trials
    centre = (p + z * z / (2 * trials)) / denominator
    radius = z * math.sqrt((p * (1 - p) + z * z / (4 * trials)) / trials) / denominator
    return {
        "successes": successes,
        "trials": trials,
        "rate": p,
        "lower": max(0.0, centre - radius),
        "upper": min(1.0, centre + radius),
        "confidence": confidence,
    }


def _schema_size(schema: Any) -> str:
    text = str(schema or "")
    table_count = 0
    column_count = 0
    depth = 0
    in_quote: str | None = None
    for char in text:
        if in_quote:
            if char == in_quote:
                in_quote = None
            continue
        if char in "'\"`":
            in_quote = char
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
    return "small_1_table_1_8_cols" if table_count == 1 and column_count <= 8 else (
        "medium_1_table_9_32_cols" if table_count == 1 and column_count <= 32 else
        "multi_table" if table_count > 1 else "large"
    )


def _stratum_rows(
    rows: Iterable[dict[str, Any]],
    key: str,
) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if key == "category":
            values = row.get("categories") or ["unknown"]
            for value in values:
                result[str(value)].append(row)
        elif key == "schema_size":
            result[_schema_size(row.get("schema"))].append(row)
        else:
            result[str(row.get(key) or "unknown")].append(row)
    return dict(result)


def _oracle_metric(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Report labelled detection while excluding non-decision outcomes."""
    not_equivalent = [row for row in rows if str(row.get("expectation") or "").upper() == "NOT_EQUIVALENT"]
    equivalent = [row for row in rows if str(row.get("expectation") or "").upper() == "EQUIVALENT"]
    def verdict(row: dict[str, Any]) -> str:
        return str((row.get("oracle") or {}).get("verdict") or "UNDECIDED").upper()
    neq_determinate = [row for row in not_equivalent if verdict(row) in DETERMINATE]
    eq_determinate = [row for row in equivalent if verdict(row) in DETERMINATE]
    return {
        "not_equivalent_detection": _wilson(
            sum(verdict(row) == "NOT_EQUIVALENT" for row in neq_determinate),
            len(neq_determinate),
        ),
        "equivalent_false_positive_control": _wilson(
            sum(verdict(row) == "EQUIVALENT" for row in eq_determinate),
            len(eq_determinate),
        ),
        "labelled_families": len(not_equivalent) + len(equivalent),
        "determinate_labelled_families": len(neq_determinate) + len(eq_determinate),
        "excluded": dict(sorted(Counter(verdict(row) for row in not_equivalent + equivalent if verdict(row) in EXCLUDED).items())),
    }


def _chain_metric(rows: list[dict[str, Any]]) -> dict[str, Any]:
    eligible = [
        row for row in rows
        if str(row.get("expected_label") or "").upper() == "NOT_EQUIVALENT"
        and (row.get("checks") or {}).get("oracle_verdict") == "NOT_EQUIVALENT"
        and str(row.get("status") or "").upper() == "PASS"
    ]
    def count(name: str) -> dict[str, Any]:
        return _wilson(sum(bool((row.get("checks") or {}).get(name)) for row in eligible), len(eligible))
    return {
        "eligible_not_equivalent_families": len(eligible),
        "validator_activation": count("witness_validator_activated"),
        "execution_difference": count("execution_difference"),
        "targeted_mutation_repair": count("targeted_mutation_repaired"),
        "attribution_diff_kp_binding": count("attribution_bound_to_diff_kp"),
        "full_chain": count("chain_pass"),
        "excluded_statuses": dict(sorted(Counter(
            str(row.get("status") or (row.get("checks") or {}).get("oracle_verdict") or "UNDECIDED").upper()
            for row in rows
            if str(row.get("expected_label") or "").upper() == "NOT_EQUIVALENT"
            and not (
                str(row.get("status") or "").upper() == "PASS"
                and (row.get("checks") or {}).get("oracle_verdict") == "NOT_EQUIVALENT"
            )
        ).items())),
    }


def _stratified_metrics(rows: list[dict[str, Any]], kind: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name, group in sorted(_stratum_rows(rows, kind).items()):
        result[name] = _oracle_metric(group)
    return result


def _warnings(metric: dict[str, Any]) -> list[str]:
    warnings: list[str] = []
    for name, target, required in (
        ("not_equivalent_detection", 0.99, 300),
        ("equivalent_false_positive_control", 0.999, 3000),
    ):
        sample = metric.get(name) or {}
        trials = int(sample.get("trials") or 0)
        lower = sample.get("lower")
        if trials < required:
            warnings.append(
                f"{name}: n={trials} is below the approximate zero-failure sample size "
                f"n>={required} for a 95% lower bound near {target:.3%}; do not claim the target."
            )
        if lower is not None and lower < target:
            warnings.append(
                f"{name}: Wilson 95% lower bound {lower:.6%} is below target {target:.3%}."
            )
    return warnings


def report(
    gold_path: Path,
    chain_path: Path,
    matrix_path: Path,
    manifest_path: Path,
    output_path: Path,
    markdown_path: Path | None = None,
) -> dict[str, Any]:
    gold_rows = _read_jsonl(gold_path)
    chain_rows = _read_jsonl(chain_path)
    matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    gold_summary = _oracle_metric(gold_rows)
    chain_summary = _chain_metric(chain_rows)
    verdicts = Counter(
        str((row.get("oracle") or {}).get("verdict") or "UNDECIDED")
        for row in gold_rows
    )
    result: dict[str, Any] = {
        "schema_version": 1,
        "metric_unit": "unique_question_family",
        "gold_oracle": gold_summary,
        "gold_verdict_counts": dict(sorted(verdicts.items())),
        "production_chain": chain_summary,
        "by_category": _stratified_metrics(gold_rows, "category"),
        "by_source": _stratified_metrics(gold_rows, "source_id"),
        "by_dialect": _stratified_metrics(gold_rows, "dialect"),
        "by_schema_size": _stratified_metrics(gold_rows, "schema_size"),
        "capability_matrix": {
            "unique_development_families": matrix.get("unique_families"),
            "all_categories_target_met": matrix.get("all_categories_target_met"),
            "target_families_per_category": matrix.get("target_families_per_category"),
            "target_families_per_scenario_axis": matrix.get("target_families_per_scenario_axis"),
            "categories": {
                category: {
                    "families": value.get("families"),
                    "target_met": value.get("target_met"),
                    "scenario_axes": {
                        axis: axis_value
                        for axis, axis_value in (value.get("scenario_axes") or {}).items()
                        if not axis_value.get("met")
                    },
                }
                for category, value in sorted((matrix.get("categories") or {}).items())
            },
        },
        "corpus": {
            "snapshot_id": manifest.get("snapshot_id"),
            "unique_question_families": manifest.get("unique_question_families"),
            "partition_counts": manifest.get("partition_counts"),
            "duplicate_input_records": manifest.get("duplicate_input_records"),
            "split": manifest.get("split"),
        },
    }
    result["warnings"] = _warnings(gold_summary)
    result["warnings"].extend([
        "Production-chain audit is a stratified bounded sample and is not a 300-family-per-category acceptance run.",
        "Native Gold execution is reported per dialect: configured MySQL/PostgreSQL/Oracle trials are included, while unavailable or unhealthy native engines remain ENGINE_GAP and SQLite compatibility rows remain separate.",
    ])
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if markdown_path is not None:
        markdown_path.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            "# Phase 1 Statistical Acceptance",
            "",
            f"- Metric unit: `{result['metric_unit']}`",
            f"- Corpus snapshot: `{result['corpus']['snapshot_id']}`",
            f"- Gold verdicts: `{result['gold_verdict_counts']}`",
            "",
            "## Primary Metrics",
            "",
            "| Metric | Successes | Trials | Rate | Wilson 95% lower |",
            "|---|---:|---:|---:|---:|",
        ]
        for name, value in result["gold_oracle"].items():
            if isinstance(value, dict) and "successes" in value:
                lines.append(
                    f"| {name} | {value['successes']} | {value['trials']} | "
                    f"{value['rate'] if value['rate'] is not None else 'n/a'} | "
                    f"{value['lower'] if value['lower'] is not None else 'n/a'} |"
                )
        lines.extend(["", "## Production Chain", "", f"```json\n{json.dumps(result['production_chain'], ensure_ascii=False, indent=2)}\n```", ""])
        lines.extend(["## Warnings", ""])
        lines.extend(f"- {warning}" for warning in result["warnings"])
        markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gold", type=Path, required=True)
    parser.add_argument("--chain", type=Path, required=True)
    parser.add_argument("--matrix", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--markdown", type=Path)
    args = parser.parse_args(argv)
    result = report(args.gold, args.chain, args.matrix, args.manifest, args.output, args.markdown)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
