"""Web-inspired common SQL teaching holdout for 250 additional cases.

This is a supplement to the existing web_common150 suite.  It reuses the same
online-inspired source templates, but expands the fixed-seed sampling budget to
250 cases so the coverage is denser around the structure blocks the user asked
for: SELECT, DISTINCT, WHERE, JOIN ON, GROUP BY, HAVING, Aggregate, ORDER BY,
LIMIT / OFFSET, Subquery, Correlated Subquery, CTE, Recursive CTE, Set
Operation, CASE, Window, and Dialect Boundary.
"""

from __future__ import annotations

import json
import random
import sys
from collections import Counter
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import run_web_common150_structure_tests as base

OUTPUT_DIR = PROJECT_ROOT / "data_construct_test" / "outputs"
SEED = 20260724

QUOTAS: dict[str, int] = {
    "SELECT": 11,
    "DISTINCT": 11,
    "WHERE": 11,
    "Comparison": 11,
    "NULL": 10,
    "IN": 10,
    "BETWEEN": 10,
    "LIKE": 10,
    "Logic": 10,
    "JOIN": 11,
    "JOIN ON": 11,
    "GROUP BY": 11,
    "HAVING": 10,
    "Aggregate": 11,
    "ORDER BY": 10,
    "LIMIT / OFFSET": 10,
    "Subquery": 10,
    "Correlated Subquery": 10,
    "CTE": 10,
    "Recursive CTE": 10,
    "Set Operation": 10,
    "CASE": 11,
    "Window": 11,
    "Dialect Boundary": 10,
}


def build_cases(seed: int = SEED) -> list[dict[str, Any]]:
    rng = random.Random(seed)
    pools = base._candidate_cases()
    selected: list[dict[str, Any]] = []
    for structure, quota in QUOTAS.items():
        pool = pools[structure]
        supported = [item for item in pool if item["intent"] == "supported"]
        gaps = [item for item in pool if item["intent"] == "strict_gap"]
        min_gap_quota = max(1, quota // 3)
        supported_quota = min(len(supported), quota - min_gap_quota)
        gap_quota = quota - supported_quota
        if len(supported) < supported_quota or len(gaps) < gap_quota:
            raise RuntimeError(
                f"not enough candidates for {structure}: "
                f"supported={len(supported)} needed={supported_quota}, "
                f"gaps={len(gaps)} needed={gap_quota}"
            )
        selected.extend(rng.sample(supported, supported_quota))
        selected.extend(rng.sample(gaps, gap_quota))

    if len(selected) != 250:
        raise RuntimeError(f"expected 250 cases, got {len(selected)}")

    rng.shuffle(selected)
    for index, item in enumerate(selected, 1):
        item["sample_index"] = index
    return selected


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    by_structure: dict[str, Counter[str]] = {}
    for result in results:
        by_structure.setdefault(result["structure"], Counter())
        by_structure[result["structure"]]["total"] += 1
        by_structure[result["structure"]]["strict_pass"] += int(result["strict_pass"])
        by_structure[result["structure"]]["strict_fail"] += int(not result["strict_pass"])

    failures = [result for result in results if not result["strict_pass"]]
    return {
        "seed": SEED,
        "total": len(results),
        "strict_pass": len(results) - len(failures),
        "strict_fail": len(failures),
        "strict_pass_rate": (len(results) - len(failures)) / len(results) * 100 if results else 0,
        "failure_ids": [result["id"] for result in failures],
        "by_structure": {key: dict(value) for key, value in sorted(by_structure.items())},
    }


def write_outputs(results: list[dict[str, Any]], summary: dict[str, Any]) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    report_json = OUTPUT_DIR / "web_common250_structure_report.json"
    cases_jsonl = OUTPUT_DIR / "web_common250_structure_cases.jsonl"
    report_md = OUTPUT_DIR / "web_common250_structure_report.md"

    payload = {"summary": summary, "results": results}
    report_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    with cases_jsonl.open("w", encoding="utf-8") as fh:
        for result in results:
            fh.write(json.dumps(result, ensure_ascii=False) + "\n")

    lines = [
        "# Web Common250 Structure Report",
        "",
        "Supplementary online-inspired holdout covering the same structure blocks at a denser scale.",
        "",
        f"- Seed: `{summary['seed']}`",
        f"- Total: `{summary['total']}`",
        f"- Strict pass: `{summary['strict_pass']}` (`{summary['strict_pass_rate']:.2f}%`) ",
        f"- Strict fail: `{summary['strict_fail']}`",
        "",
        "## By Structure",
        "",
        "| structure | total | strict pass | strict fail |",
        "|---|---:|---:|---:|",
    ]
    for structure, stats in summary["by_structure"].items():
        lines.append(
            f"| {structure} | {stats['total']} | {stats['strict_pass']} | {stats['strict_fail']} |"
        )

    lines.extend(["", "## Failures", ""])
    failures = [result for result in results if not result["strict_pass"]]
    if not failures:
        lines.append("None.")
    else:
        for result in failures[:50]:
            lines.extend([
                f"### {result['id']} ({result['structure']})",
                f"- source: {result['source']}",
                f"- standard: `{result['standard']}`",
                f"- student: `{result['student']}`",
                f"- issues: `{json.dumps(result['missing'], ensure_ascii=False)}`",
                "",
            ])

    report_md.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    cases = build_cases()
    results = [base.evaluate_case(item) for item in cases]
    summary = summarize(results)
    write_outputs(results, summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
