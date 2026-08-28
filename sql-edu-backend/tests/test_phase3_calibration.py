"""Bounded Phase 3 BKT calibration artifact tests."""

from __future__ import annotations

import hashlib
import json

import pytest

from core.phase3_bkt import BKT_PARAMETERS_V1
from core.phase3_calibration import (
    CALIBRATED_OFFLINE_STATUS,
    CALIBRATED_OFFLINE_SYNTHETIC_STATUS,
    CALIBRATED_PARAMETER_PREFIX,
    CALIBRATION_ARTIFACT_SCHEMA_VERSION,
    BKTCalibrationError,
    calibration_artifact_from_dict,
    fit_bkt_calibration,
    load_active_bkt_policy,
    load_calibration_artifact,
    sha256_file,
)


def _artifact_payload(*, source_digest: str, status: str = CALIBRATED_OFFLINE_STATUS):
    source_kind = (
        "REAL_STUDENT_EVENTS"
        if status == CALIBRATED_OFFLINE_STATUS
        else "SYNTHETIC_AUDIT_EVENTS"
    )
    version = f"{CALIBRATED_PARAMETER_PREFIX}test"
    return {
        "schema_version": CALIBRATION_ARTIFACT_SCHEMA_VERSION,
        "status": status,
        "source_kind": source_kind,
        "source_digest_sha256": source_digest,
        "sample_count": 100,
        "student_count": 4,
        "training_sample_count": 80,
        "held_out_sample_count": 20,
        "parameter_version": version,
        "parameters": {
            "version": version,
            "initial_mastery": 0.2,
            "slip": 0.1,
            "guess": 0.2,
            "transition": 0.1,
        },
        "fitting_method": "test_fixture",
        "deterministic_seed": 0,
        "held_out": {
            "sample_count": 20,
            "log_loss": 0.5,
            "brier_score": 0.2,
        },
    }


def test_no_artifact_keeps_uncalibrated_default(monkeypatch):
    monkeypatch.delenv("PHASE3_BKT_CALIBRATION_ARTIFACT", raising=False)
    monkeypatch.delenv("PHASE3_BKT_CALIBRATION_SOURCE", raising=False)

    policy = load_active_bkt_policy()

    assert policy.calibration_status == "UNCALIBRATED_MVP"
    assert policy.parameters == BKT_PARAMETERS_V1
    assert policy.rejection_code is None


def test_valid_real_artifact_requires_matching_source_digest(tmp_path):
    source = tmp_path / "learner-events.jsonl"
    source.write_text(
        json.dumps(
            {
                "event_id": "event-1",
                "student_id": "student-1",
                "skill_id": "filter.boundary",
                "observed_at": "2026-08-27T00:00:00Z",
                "is_correct": True,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    artifact_path = tmp_path / "bkt-artifact.json"
    artifact_path.write_text(
        json.dumps(_artifact_payload(source_digest=digest)),
        encoding="utf-8",
    )

    artifact = load_calibration_artifact(artifact_path, source_path=source)

    assert artifact.status == CALIBRATED_OFFLINE_STATUS
    assert artifact.parameters.version.startswith(CALIBRATED_PARAMETER_PREFIX)
    other_source = tmp_path / "other.jsonl"
    other_source.write_text("{}\n", encoding="utf-8")
    with pytest.raises(BKTCalibrationError, match="SOURCE_DIGEST_MISMATCH"):
        load_calibration_artifact(artifact_path, source_path=other_source)


def test_invalid_or_synthetic_artifact_fails_closed_when_selected(tmp_path, monkeypatch):
    artifact_path = tmp_path / "synthetic.json"
    artifact_path.write_text(
        json.dumps(
            _artifact_payload(
                source_digest="0" * 64,
                status=CALIBRATED_OFFLINE_SYNTHETIC_STATUS,
            )
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("PHASE3_BKT_CALIBRATION_ARTIFACT", str(artifact_path))
    monkeypatch.delenv("PHASE3_BKT_CALIBRATION_SOURCE", raising=False)

    policy = load_active_bkt_policy()

    assert policy.calibration_status == "UNCALIBRATED_MVP"
    assert policy.parameters == BKT_PARAMETERS_V1
    assert policy.rejection_code == "BKT_CALIBRATION_SYNTHETIC_NOT_ACTIVE"

    invalid_payload = _artifact_payload(source_digest="0" * 64)
    invalid_payload["parameters"]["slip"] = 0.0
    with pytest.raises(BKTCalibrationError, match="ARTIFACT_INVALID"):
        calibration_artifact_from_dict(invalid_payload)


def test_real_artifact_requires_source_export_before_activation(tmp_path, monkeypatch):
    artifact_path = tmp_path / "real.json"
    artifact_path.write_text(
        json.dumps(_artifact_payload(source_digest="0" * 64)), encoding="utf-8"
    )
    monkeypatch.setenv("PHASE3_BKT_CALIBRATION_ARTIFACT", str(artifact_path))
    monkeypatch.delenv("PHASE3_BKT_CALIBRATION_SOURCE", raising=False)

    policy = load_active_bkt_policy()

    assert policy.calibration_status == "UNCALIBRATED_MVP"
    assert policy.parameters == BKT_PARAMETERS_V1
    assert policy.rejection_code == "BKT_CALIBRATION_SOURCE_REQUIRED"


def test_calibration_source_symlink_is_rejected(tmp_path):
    target = tmp_path / "events.jsonl"
    target.write_text("{}\n", encoding="utf-8")
    link = tmp_path / "events-link.jsonl"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are unavailable in this test environment")

    with pytest.raises(BKTCalibrationError, match="SOURCE_INVALID"):
        sha256_file(link)


def test_fit_is_bounded_deterministic_and_real_source_only(tmp_path):
    source = tmp_path / "events.jsonl"
    rows = []
    for student_index in range(10):
        for attempt in range(10):
            rows.append(
                {
                    "event_id": f"event-{student_index}-{attempt}",
                    "student_id": f"student-{student_index}",
                    "skill_id": "filter.boundary",
                    "observed_at": f"2026-08-27T00:{student_index:02d}:{attempt:02d}Z",
                    "is_correct": (student_index + attempt) % 3 != 0,
                }
            )
    source.write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )

    artifact = fit_bkt_calibration(source, deterministic_seed=7)

    assert artifact.status == CALIBRATED_OFFLINE_STATUS
    assert artifact.sample_count == 100
    assert artifact.student_count == 10
    assert artifact.training_sample_count + artifact.held_out_sample_count == 100
    assert artifact.held_out_sample_count >= 10
    assert artifact.parameters.version.startswith(CALIBRATED_PARAMETER_PREFIX)
    assert 0.0 <= artifact.held_out_brier_score <= 1.0
