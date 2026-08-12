import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.append(str(PROJECT_ROOT / "data_construct_test" / "scripts"))

from run_e2e_robustness_fuzzer import run_case


OUTPUT_DIR = PROJECT_ROOT / "data_construct_test" / "outputs"
DEFAULT_CASES = OUTPUT_DIR / "gemini_adversarial_cases_20260706.json"


def _load_cases(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    cases = payload["cases"] if isinstance(payload, dict) else payload
    normalized: list[dict[str, Any]] = []
    for idx, case in enumerate(cases, 1):
        item = dict(case)
        item.setdefault("id", f"gemini_{idx:03d}")
        item.setdefault("expect_equiv", False)
        item.setdefault("expected_kps", [])
        item.setdefault("note", "")
        item.setdefault("tactic", "gemini_generated_adversarial")
        normalized.append(item)
    return normalized


def _markdown(results: list[dict[str, Any]], source: Path) -> str:
    status_counts = Counter(item["status"] for item in results)
    operator_counts: dict[str, Counter[str]] = defaultdict(Counter)
    for item in results:
        operator_counts[item["operator"]][item["status"]] += 1

    lines = [
        "# Gemini Adversarial Robustness Report",
        "",
        f"- Source: `{source}`",
        f"- Total: {len(results)}",
        f"- PASS: {status_counts.get('PASS', 0)}",
        f"- Pass rate: {status_counts.get('PASS', 0) / max(1, len(results)):.1%}",
        "",
        "## Status Counts",
        "",
    ]
    for status, count in sorted(status_counts.items()):
        lines.append(f"- {status}: {count}")

    lines.extend(["", "## Operator Counts", ""])
    for operator in sorted(operator_counts):
        counts = ", ".join(f"{status}={count}" for status, count in sorted(operator_counts[operator].items()))
        lines.append(f"- {operator}: {counts}")

    failures = [item for item in results if item["status"] != "PASS"]
    if failures:
        lines.extend(["", "## Failures", "", "| id | operator | status | reason | kp_ids |", "|---|---|---|---|---|"])
        for item in failures:
            kp_ids = ", ".join(item.get("kp_ids") or [])
            reason = str(item.get("reason") or "").replace("|", "\\|")
            lines.append(f"| {item['id']} | {item['operator']} | {item['status']} | {reason} | `{kp_ids}` |")
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--allow-failures", action="store_true")
    args = parser.parse_args()

    cases = _load_cases(args.cases)
    results = [run_case(case) for case in cases]
    status_counts = Counter(item["status"] for item in results)

    report = {
        "source": str(args.cases),
        "summary": {
            "total": len(results),
            "status_counts": dict(status_counts),
            "operator_counts": {
                operator: dict(counts)
                for operator, counts in sorted(
                    {
                        operator: Counter(item["status"] for item in results if item["operator"] == operator)
                        for operator in {item["operator"] for item in results}
                    }.items()
                )
            },
        },
        "results": results,
    }

    json_path = OUTPUT_DIR / "gemini_adversarial_report_20260706.json"
    md_path = OUTPUT_DIR / "gemini_adversarial_report_20260706.md"
    json_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    md_path.write_text(_markdown(results, args.cases), encoding="utf-8")

    total = len(results)
    passed = status_counts.get("PASS", 0)
    print(f"total={total} pass={passed} pass_rate={passed / max(1, total):.1%}")
    for status, count in sorted(status_counts.items()):
        print(f"{status}: {count}")
    print(f"JSON report: {json_path}")
    print(f"Markdown report: {md_path}")

    if not args.allow_failures and passed != total:
        raise SystemExit(f"{total - passed} Gemini-generated cases did not pass")


if __name__ == "__main__":
    main()
