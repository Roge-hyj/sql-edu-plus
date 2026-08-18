"""Replay persisted Phase 1 defect examples after a repair."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from run_phase1_cfg_convergence_benchmark import _quote_unsafe_schema_identifiers
from core.parseval_data_generator import generate_and_compare


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("registry", type=Path)
    args = parser.parse_args()
    results = []
    for line in args.registry.read_text(encoding="utf-8").splitlines():
        item = json.loads(line)
        schema = str(item.get("schema") or "")
        standard = _quote_unsafe_schema_identifiers(str(item["standard"]), schema)
        student = _quote_unsafe_schema_identifiers(str(item["student"]), schema)
        run = generate_and_compare(schema, standard, student, max_rows_per_table=12)
        expected_equivalent = item.get("family") == "WEB_CORPUS_IDENTITY"
        passed = bool(run.executed and run.is_equivalent is expected_equivalent)
        results.append({
            "signature": item.get("failure_signature"),
            "passed": passed,
            "executed": run.executed,
            "equivalent": run.is_equivalent,
            "expected_equivalent": expected_equivalent,
            "error": run.error,
            "standard": item["standard"],
            "student": item["student"],
        })
    failed = [item for item in results if not item["passed"]]
    print(json.dumps({
        "total": len(results),
        "passed": len(results) - len(failed),
        "failed": len(failed),
        "failures": failed,
    }, ensure_ascii=False, indent=2))
    raise SystemExit(bool(failed))


if __name__ == "__main__":
    main()
