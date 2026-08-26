from __future__ import annotations

import pytest

from core.phase1_verdict import (
    EQUIVALENCE_NOT_EQUIVALENT,
    EQUIVALENCE_UNDECIDED,
    is_teachable_wrong,
    project_failure,
)


@pytest.mark.parametrize(
    ("internal", "status", "conclusion"),
    [
        ("WRONG", "SUPPORTED", EQUIVALENCE_NOT_EQUIVALENT),
        ("INPUT_ERROR", "INPUT_GAP", EQUIVALENCE_UNDECIDED),
        ("UNSUPPORTED", "KNOWN_GAP", EQUIVALENCE_UNDECIDED),
        ("SECURITY_REJECTED", "KNOWN_GAP", EQUIVALENCE_UNDECIDED),
        ("ENGINE_GAP", "ENGINE_GAP", EQUIVALENCE_UNDECIDED),
        ("ENGINE_ERROR", "ENGINE_GAP", EQUIVALENCE_UNDECIDED),
        ("TIMEOUT", "ENGINE_GAP", EQUIVALENCE_UNDECIDED),
        ("UNDECIDED", "KNOWN_GAP", EQUIVALENCE_UNDECIDED),
    ],
)
def test_failure_projection_is_explicit_and_fail_closed(internal, status, conclusion):
    projection = project_failure(internal)
    assert projection.status == status
    assert projection.equivalence_conclusion == conclusion


def test_only_supported_wrong_is_teachable():
    assert is_teachable_wrong(
        status="SUPPORTED",
        conclusion="NOT_EQUIVALENT",
        judge_status="WRONG",
    )
    for status in ("KNOWN_GAP", "ENGINE_GAP", "INPUT_GAP", "SEMANTIC_BOUNDARY"):
        assert not is_teachable_wrong(
            status=status,
            conclusion="NOT_EQUIVALENT",
            judge_status="WRONG",
        )
    assert not is_teachable_wrong(
        status="SUPPORTED",
        conclusion="UNDECIDED",
        judge_status="UNDECIDED",
    )
