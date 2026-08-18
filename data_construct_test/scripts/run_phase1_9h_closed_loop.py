"""Nine-hour, evidence-first Phase 1 corpus/test/defect closed loop.

The runner never edits production source automatically.  It collects and
tests evidence, maintains a persistent defect corpus, and emits the exact
examples an engineer/agent must repair before the next round.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "data_construct_test" / "scripts"
OUTPUTS = ROOT / "data_construct_test" / "outputs"
PYTHON = Path(sys.executable)


def args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hours", type=float, default=9.0)
    parser.add_argument("--round-cases", type=int, default=10_000)
    parser.add_argument("--batch-cases", type=int, default=500)
    parser.add_argument("--seed", type=int, default=2026081500)
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--max-rounds", type=int, default=0)
    return parser.parse_args()


def run(command: list[str], *, cwd: Path = ROOT, log: Path | None = None) -> None:
    target = log.open("w", encoding="utf-8") if log else subprocess.DEVNULL
    try:
        completed = subprocess.run(command, cwd=cwd, stdout=target, stderr=subprocess.STDOUT, check=False)
    finally:
        if log:
            target.close()
    if completed.returncode:
        raise RuntimeError(f"command failed ({completed.returncode}): {' '.join(command)}")


def merge_defects(round_dir: Path, registry: Path) -> int:
    known: dict[str, dict] = {}
    if registry.exists():
        for line in registry.read_text(encoding="utf-8").splitlines():
            item = json.loads(line)
            known[item["defect_key"]] = item
    added = 0
    for failure_file in sorted(round_dir.glob("batch_*/failures.jsonl")):
        for line in failure_file.read_text(encoding="utf-8").splitlines():
            item = json.loads(line)
            key = "\0".join((str(item.get("failure_signature")), str(item.get("standard")), str(item.get("student"))))
            if key in known:
                known[key]["observations"] += 1
                continue
            known[key] = {
                "defect_key": key,
                "first_seen": datetime.now(timezone.utc).isoformat(),
                "observations": 1,
                "failure_signature": item.get("failure_signature"),
                "family": item.get("family"),
                "schema": item.get("schema"),
                "standard": item.get("standard"),
                "student": item.get("student"),
                "structure_ok": item.get("structure_stage_met"),
                "data_ok": item.get("data_stage_met"),
                "mutation_ok": item.get("mutation_stage_met"),
                "attribution_ok": item.get("attribution_stage_met"),
                "error": item.get("error"),
            }
            added += 1
    registry.write_text("".join(json.dumps(v, ensure_ascii=False) + "\n" for v in known.values()), encoding="utf-8")
    return added


def main() -> None:
    options = args()
    if options.round_cases % options.batch_cases:
        raise SystemExit("round-cases must be divisible by batch-cases")
    session = OUTPUTS / "phase1_9h_closed_loop" / datetime.now().strftime("%Y%m%d_%H%M%S")
    session.mkdir(parents=True, exist_ok=True)
    registry = session / "defect_registry.jsonl"
    deadline = time.monotonic() + options.hours * 3600
    round_number = 0
    while time.monotonic() < deadline and (not options.max_rounds or round_number < options.max_rounds):
        round_number += 1
        round_dir = session / f"round_{round_number:04d}"
        round_dir.mkdir()
        corpus = round_dir / "web_corpus.jsonl"
        collect = [
            str(PYTHON), str(SCRIPTS / "collect_web_sql_corpus.py"),
            "--output", str(corpus), "--report", str(round_dir / "collection.json"),
            "--max-per-source", "10000",
        ]
        if options.offline:
            collect.append("--offline-cache-only")
        run(collect, log=round_dir / "collection.log")
        batches = options.round_cases // options.batch_cases
        for batch in range(1, batches + 1):
            batch_dir = round_dir / f"batch_{batch:03d}"
            batch_dir.mkdir()
            seed = options.seed + round_number * 1000 + batch
            command = [
                str(PYTHON), str(SCRIPTS / "run_phase1_cfg_convergence_benchmark.py"),
                "--generated-cases", str(options.batch_cases // 2),
                "--web-corpus", str(corpus),
                "--web-cases", str(options.batch_cases // 2),
                "--web-mutations-per-query", "1", "--batch-size", "100",
                "--skip-fragment-stratum", "--seed", str(seed),
            ]
            run(command, log=batch_dir / "run.log")
            for source, destination in (
                (OUTPUTS / "phase1_cfg_convergence_report.json", batch_dir / "report.json"),
                (OUTPUTS / "phase1_cfg_convergence_failures.jsonl", batch_dir / "failures.jsonl"),
                (OUTPUTS / "phase1_cfg_convergence_detailed_evidence.jsonl", batch_dir / "evidence.jsonl"),
            ):
                shutil.copy2(source, destination)
        new_defects = merge_defects(round_dir, registry)
        (round_dir / "round_status.json").write_text(json.dumps({
            "round": round_number,
            "cases": options.round_cases,
            "new_defects": new_defects,
            "finished_at": datetime.now(timezone.utc).isoformat(),
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    (session / "session_complete.json").write_text(json.dumps({
        "rounds": round_number,
        "ended_at": datetime.now(timezone.utc).isoformat(),
        "requested_hours": options.hours,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(session)


if __name__ == "__main__":
    main()
