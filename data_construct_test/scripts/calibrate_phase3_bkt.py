"""Fit a bounded Phase 3 BKT artifact from a real learner-event JSONL export.

The input must contain one JSON object per line with event_id, student_id (or
user_id), skill_id, observed_at (or created_at), and is_correct (or
observation_result).  The command writes only the validated artifact; it does
not copy raw learner events into the output.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = PROJECT_ROOT / "sql-edu-backend"
sys.path.insert(0, str(BACKEND_ROOT))

from core.phase3_calibration import (  # noqa: E402
    REAL_STUDENT_EVENTS,
    fit_bkt_calibration,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args(argv)

    artifact = fit_bkt_calibration(
        args.input,
        deterministic_seed=args.seed,
        source_kind=REAL_STUDENT_EVENTS,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(artifact.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "output": str(args.output),
                "status": artifact.status,
                "sample_count": artifact.sample_count,
                "student_count": artifact.student_count,
                "held_out_sample_count": artifact.held_out_sample_count,
                "parameter_version": artifact.parameter_version,
                "source_digest_sha256": artifact.source_digest_sha256,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
