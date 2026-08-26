"""Single source of truth for Phase 1 rich verdict projections.

The sandbox keeps the low-level ``judge_status`` that explains why a task
stopped.  The public ``status``/``equivalence_conclusion`` pair is deliberately
smaller and fail-closed: only a supported student error can produce a
determinate ``NOT_EQUIVALENT`` conclusion.  Platform, safety, input, and
capability failures never become a student error by accident.
"""

from __future__ import annotations

from dataclasses import dataclass


VERDICT_SUPPORTED = "SUPPORTED"
VERDICT_SUPPORTED_WITH_LIMITS = "SUPPORTED_WITH_LIMITS"
VERDICT_SEMANTIC_BOUNDARY = "SEMANTIC_BOUNDARY"
VERDICT_KNOWN_GAP = "KNOWN_GAP"
VERDICT_ENGINE_GAP = "ENGINE_GAP"
VERDICT_INPUT_GAP = "INPUT_GAP"

EQUIVALENCE_NOT_EQUIVALENT = "NOT_EQUIVALENT"
EQUIVALENCE_UNDECIDED = "UNDECIDED"

GOLD_DETERMINATE_VERDICTS = frozenset({"EQUIVALENT", EQUIVALENCE_NOT_EQUIVALENT})
PRODUCTION_STATUSES = frozenset(
    {
        VERDICT_SUPPORTED,
        VERDICT_SUPPORTED_WITH_LIMITS,
        VERDICT_SEMANTIC_BOUNDARY,
        VERDICT_KNOWN_GAP,
        VERDICT_ENGINE_GAP,
        VERDICT_INPUT_GAP,
    }
)
NON_TEACHABLE_JUDGE_STATUSES = frozenset(
    {"UNSUPPORTED", "SECURITY_REJECTED", "ENGINE_ERROR", "TIMEOUT", "ENGINE_GAP"}
)


@dataclass(frozen=True)
class FailureProjection:
    """Rich production fields for a pre-execution failure."""

    status: str
    equivalence_conclusion: str = EQUIVALENCE_UNDECIDED


# Internal statuses are intentionally explicit.  In particular,
# SECURITY_REJECTED is a policy/capability boundary, not a semantic mismatch;
# it must not be projected as SUPPORTED + NOT_EQUIVALENT.
FAILURE_PROJECTIONS = {
    "WRONG": FailureProjection(VERDICT_SUPPORTED, EQUIVALENCE_NOT_EQUIVALENT),
    "INPUT_ERROR": FailureProjection(VERDICT_INPUT_GAP),
    "UNSUPPORTED": FailureProjection(VERDICT_KNOWN_GAP),
    "SECURITY_REJECTED": FailureProjection(VERDICT_KNOWN_GAP),
    "ENGINE_GAP": FailureProjection(VERDICT_ENGINE_GAP),
    # ENGINE_ERROR/TIMEOUT retain their precise low-level judge_status and
    # error_code; the rich public status remains an engine boundary so callers
    # cannot mistake the absence of a reliable verdict for a wrong answer.
    "ENGINE_ERROR": FailureProjection(VERDICT_ENGINE_GAP),
    "TIMEOUT": FailureProjection(VERDICT_ENGINE_GAP),
    "UNDECIDED": FailureProjection(VERDICT_KNOWN_GAP),
}


def project_failure(internal_status: str) -> FailureProjection:
    """Map a low-level failure to the fail-closed rich production fields."""

    return FAILURE_PROJECTIONS.get(
        str(internal_status or "").upper(),
        FailureProjection(VERDICT_ENGINE_GAP),
    )


def is_teachable_wrong(*, status: str, conclusion: str, judge_status: str) -> bool:
    """Return whether a failure is allowed to create a WRONG observation."""

    return (
        str(status or "").upper() == VERDICT_SUPPORTED
        and str(conclusion or "").upper() == EQUIVALENCE_NOT_EQUIVALENT
        and str(judge_status or "").upper() == "WRONG"
    )


__all__ = [
    "EQUIVALENCE_NOT_EQUIVALENT",
    "EQUIVALENCE_UNDECIDED",
    "FAILURE_PROJECTIONS",
    "FailureProjection",
    "GOLD_DETERMINATE_VERDICTS",
    "NON_TEACHABLE_JUDGE_STATUSES",
    "PRODUCTION_STATUSES",
    "VERDICT_ENGINE_GAP",
    "VERDICT_INPUT_GAP",
    "VERDICT_KNOWN_GAP",
    "VERDICT_SEMANTIC_BOUNDARY",
    "VERDICT_SUPPORTED",
    "VERDICT_SUPPORTED_WITH_LIMITS",
    "is_teachable_wrong",
    "project_failure",
]
