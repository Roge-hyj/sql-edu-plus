"""Tests for the evidence-completeness boundary of the capability scorecard."""

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "data_construct_test" / "scripts"))

from run_system_capability_evaluation import (  # noqa: E402
    _add_assessment_completeness,
    _add_combined_phase1_report,
)


def _args(tmp_path: Path, *, complete: bool) -> argparse.Namespace:
    paths = {
        "corpus": tmp_path / "corpus.jsonl",
        "identity": tmp_path / "identity.json",
        "identity_failures": tmp_path / "identity.failures.jsonl",
        "mutation": tmp_path / "mutation.json",
        "mutation_failures": tmp_path / "mutation.failures.jsonl",
        "phase2": tmp_path / "phase2.json",
        "full_chain": tmp_path / "full-chain.json",
    }
    if complete:
        paths["corpus"].write_text('{"source_id":"source"}\n', encoding="utf-8")
        for key, path in paths.items():
            if key == "corpus":
                continue
            if path.suffix == ".jsonl":
                path.write_text("", encoding="utf-8")
            else:
                path.write_text(json.dumps({}), encoding="utf-8")
    return argparse.Namespace(**paths)


def test_closed_assessment_requires_all_reports_and_ledgers(tmp_path):
    metrics = []

    result = _add_assessment_completeness(
        metrics,
        args=_args(tmp_path, complete=False),
    )

    assert result["closed"] is False
    assert set(result["missing"]) == {
        "corpus",
        "identity",
        "identity_failures",
        "mutation",
        "mutation_failures",
        "phase2",
        "full_chain",
    }
    assert metrics[0]["status"] == "FAIL"


def test_empty_failure_ledgers_are_valid_evidence_when_present(tmp_path):
    metrics = []

    result = _add_assessment_completeness(
        metrics,
        args=_args(tmp_path, complete=True),
    )

    assert result == {
        "closed": True,
        "required_artifacts": {
            "corpus": True,
            "identity": True,
            "identity_failures": True,
            "mutation": True,
            "mutation_failures": True,
            "phase2": True,
            "full_chain": True,
        },
        "missing": [],
        "present": 7,
        "total": 7,
    }
    assert metrics[0]["status"] == "PASS"


def test_combined_phase1_report_keeps_identity_and_mutation_denominators_separate(tmp_path):
    failure_path = tmp_path / "phase1.failures.jsonl"
    failure_path.write_text(
        json.dumps(
            {
                "origin": "web_corpus_mutation",
                "scope_status": "witness_evidence_gap",
                "structure_stage_met": True,
                "data_stage_met": False,
                "mutation_stage_met": True,
                "attribution_stage_met": False,
                "expectation_met": False,
                "mutation_summary": {"executed": 1},
                "failure_signature": "data_generation_or_equivalence|WebCorpus|distinct_removed|none",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    report = {
        "web_corpus": "external.jsonl",
        "summary": {
            "family_counts": {
                "WEB_CORPUS_IDENTITY": {"supported": 2},
                "WEB_CORPUS_DISTINCT_REMOVED": {
                    "supported": 1,
                    "witness_evidence_gap": 1,
                },
            }
        },
    }
    metrics = []

    details = _add_combined_phase1_report(
        metrics,
        report=report,
        failures_path=failure_path,
    )

    assert details["identity"]["eligible"] == 2
    assert details["mutation"]["eligible"] == 2
    by_id = {item["metric_id"]: item for item in metrics}
    assert by_id["external.combined.identity.data"]["successes"] == 2
    assert by_id["external.combined.mutation.data"]["successes"] == 1
    assert by_id["external.combined.mutation.evidence_execution"]["successes"] == 2
    assert by_id["external.combined.mutation.negative_attribution"]["successes"] == 1
