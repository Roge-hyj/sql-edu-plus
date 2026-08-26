"""Run a reproducible public train/control freeze-pair regression.

This is the public counterpart of the final hidden acceptance runner.  It
accepts only a file named ``public.jsonl`` whose records are explicitly tagged
``partition=public``.  It reuses the exact frozen mutation/control builder and
Gold Oracle, performs two identical aggregate-only evaluations, and writes no
SQL or per-row verdicts to the report.  The report is therefore safe to keep
as auditable public evidence and provides the configuration/hash needed to
replay the result from the public snapshot.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parents[1]
sys.path.insert(0, str(SCRIPT_DIR))

import run_phase1_freeze_verification as freeze  # noqa: E402


def _read_public_records(path: Path) -> list[dict[str, Any]]:
    if path.name != "public.jsonl":
        raise ValueError("public regression requires the public.jsonl snapshot file")
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            record = json.loads(line)
            if not isinstance(record, dict):
                raise ValueError(f"public record {line_number} is not an object")
            if str(record.get("partition") or "").lower() != "public":
                raise ValueError(
                    f"public regression rejected non-public partition at line {line_number}"
                )
            if not str(record.get("sql") or "").strip():
                raise ValueError(f"public record {line_number} has no SQL")
            records.append(record)
    return sorted(records, key=lambda item: str(item.get("family_id") or ""))


def _repo_relative(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path.resolve())


def run(
    input_path: Path,
    output_path: Path,
    *,
    oracle_seeds: tuple[int, ...],
    row_scales: tuple[int, ...],
    artifacts: tuple[Path, ...],
) -> dict[str, Any]:
    records = _read_public_records(input_path)
    pairs, generation = freeze._freeze_pairs(records, partition_label="public")
    first = freeze._evaluate(pairs, oracle_seeds, row_scales)
    second = freeze._evaluate(pairs, oracle_seeds, row_scales)
    manifest_path = input_path.parent / "manifest.json"
    manifest: dict[str, Any] = {}
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    stable = first == second
    artifact_hashes = {
        _repo_relative(path): freeze._sha256_file(path)
        for path in artifacts
    }
    result = {
        "schema_version": 1,
        "mode": "public_control_freeze_pair_regression",
        "frozen_at": datetime.now(timezone.utc).isoformat(),
        "code": freeze._code_fingerprint(),
        "configuration": {
            "input_partition": "public",
            "input_path": _repo_relative(input_path),
            "oracle_seeds": list(oracle_seeds),
            "row_scales": list(row_scales),
            "max_rows_per_table": 32,
            "pair_builder_salt": "phase1-hidden-freeze-v1",
            "feedback_policy": "public aggregate evidence only; no hidden partition is read",
        },
        "corpus": {
            "snapshot_id": manifest.get("snapshot_id"),
            "manifest_path": _repo_relative(manifest_path),
            "manifest_sha256": freeze._sha256_file(manifest_path),
            "public_file_sha256": freeze._sha256_file(input_path),
        },
        "artifacts_sha256": artifact_hashes,
        "public_evaluation": {
            "hidden_partition_read": False,
            "failures_saved_as_digest_only": True,
            "source_families": len(records),
            "generated_pair_rows": len(pairs),
            "pair_generation": generation,
            "first_run": first,
            "second_run": second,
            "stable": stable,
        },
        "acceptance": {
            "public_partition_only": True,
            "generation_complete": generation["generation_failures"] == 0,
            "no_determinate_label_mismatch": first["determinate_label_mismatches"] == 0,
            "repeat_run_stable": stable,
        },
    }
    result["acceptance"]["pass"] = all(result["acceptance"].values())
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return result


def _ints(value: str) -> tuple[int, ...]:
    parsed = tuple(int(item.strip()) for item in value.split(",") if item.strip())
    if not parsed:
        raise ValueError("at least one integer is required")
    return parsed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--oracle-seeds", default="0,1,2")
    parser.add_argument("--row-scales", default="4,8,16")
    parser.add_argument("--artifact", action="append", type=Path, default=[])
    args = parser.parse_args(argv)
    result = run(
        args.input,
        args.output,
        oracle_seeds=_ints(args.oracle_seeds),
        row_scales=_ints(args.row_scales),
        artifacts=tuple(args.artifact),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["acceptance"]["pass"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
