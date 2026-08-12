"""Combined online core + frontier structure tests."""

from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from run_classic_structure_holdout_tests import build_online_final100_cases, run_case
from run_structure_frontier_tests import FRONTIER_CASES

OUTPUT_DIR = PROJECT_ROOT / "data_construct_test" / "outputs"


def summarize(results):
    by_suite = defaultdict(Counter)
    by_structure = defaultdict(Counter)
    for result in results:
        suite = result["suite"]
        by_suite[suite]["total"] += 1
        by_suite[suite]["strict_pass"] += int(result["strict_pass"])
        by_suite[suite]["strict_fail"] += int(not result["strict_pass"])
        by_structure[result["structure"]]["total"] += 1
        by_structure[result["structure"]]["strict_pass"] += int(result["strict_pass"])
        by_structure[result["structure"]]["strict_fail"] += int(not result["strict_pass"])
    failures = [result for result in results if not result["strict_pass"]]
    return {
        "total": len(results),
        "strict_pass": len(results) - len(failures),
        "strict_fail": len(failures),
        "strict_pass_rate": (len(results) - len(failures)) / len(results) * 100 if results else 0,
        "by_suite": {key: dict(value) for key, value in sorted(by_suite.items())},
        "by_structure": {key: dict(value) for key, value in sorted(by_structure.items())},
        "failure_ids": [result["id"] for result in failures],
    }


def render(results, summary):
    failures = [result for result in results if not result["strict_pass"]]
    lines = [
        "# Combined Online Core + Frontier Structure Tests",
        "",
        "This report mixes the fresh online100 core set with online-sourced frontier cases.",
        "",
        f"- Total: `{summary['total']}`",
        f"- Strict pass: `{summary['strict_pass']}` (`{summary['strict_pass_rate']:.2f}%`)",
        f"- Strict fail: `{summary['strict_fail']}`",
        "",
        "## By Suite",
        "",
        "```json",
        json.dumps(summary["by_suite"], ensure_ascii=False, indent=2),
        "```",
        "",
        "## Cannot Pass Samples",
        "",
    ]
    if not failures:
        lines.append("无。")
    for result in failures:
        issues = []
        for key, value in result["hard_failures"].items():
            if value:
                issues.append(f"{key}={value}")
        lines.extend([
            f"### `{result['id']}` / {result['structure']}",
            f"- Suite: `{result['suite']}`",
            f"- Source: {result['source']}",
            f"- Standard: `{result['standard']}`",
            f"- Student: `{result['student']}`",
            f"- Issues: {'; '.join(issues)}",
            f"- Actual clauses: `{result['diff_clauses']}`",
            f"- Actual diff types: `{result['diff_types']}`",
            "",
        ])
    lines.extend([
        "## All Samples",
        "",
        "| suite | id | structure | strict |",
        "|---|---|---|---:|",
    ])
    for result in results:
        lines.append(f"| {result['suite']} | `{result['id']}` | {result['structure']} | {'yes' if result['strict_pass'] else 'NO'} |")
    return "\n".join(lines) + "\n"


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    core_results = []
    for item in build_online_final100_cases(20260723):
        result = run_case(item)
        result["suite"] = "online100-core"
        core_results.append(result)
    frontier_results = []
    for item in FRONTIER_CASES:
        result = run_case(item)
        result["suite"] = "frontier"
        frontier_results.append(result)
    results = core_results + frontier_results
    summary = summarize(results)
    payload = {"summary": summary, "results": results}
    json_path = OUTPUT_DIR / "structure_combined_online_frontier_report.json"
    md_path = OUTPUT_DIR / "structure_combined_online_frontier_report.md"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(render(results, summary), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"JSON report: {json_path}")
    print(f"Markdown report: {md_path}")


if __name__ == "__main__":
    main()
