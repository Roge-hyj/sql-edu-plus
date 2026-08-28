"""Interpretable Phase 3 support and next-challenge policies.

``support_need`` controls how much help to provide for the current teaching
target.  ``challenge_index`` is a separate, interpretable signal for choosing
a future exercise's difficulty.  Keeping the two axes separate prevents one
opaque lambda from simultaneously deciding error order, hint depth, and
difficulty.

The coefficients are explicitly an uncalibrated MVP configuration.  The
inputs are bounded behavioral/state signals; this module does not infer or
claim a student's real psychological fatigue.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from types import MappingProxyType
from typing import Any


SUPPORT_POLICY_VERSION = "phase3.support_policy.v2"
# Canonical name for the former ``challenge_readiness`` signal.  The old
# constant remains as an API alias because already-persisted response metadata
# can still contain ``challenge_policy_version``.
CHALLENGE_INDEX_POLICY_VERSION = "phase3.challenge_index.v1"
CHALLENGE_POLICY_VERSION = "phase3.challenge_policy.v1"
ADAPTATION_CALIBRATION_STATUS = "UNCALIBRATED_MVP"

SUPPORT_WEIGHTS = MappingProxyType(
    {
        "mastery_deficit": 0.35,
        "failure_streak_norm": 0.30,
        "recent_hint_ratio": 0.10,
        "behavioral_support_need": 0.10,
        "recent_unassisted_success": -0.15,
    }
)

CHALLENGE_WEIGHTS = MappingProxyType(
    {
        "mastery": 0.50,
        "recent_unassisted_success": 0.30,
        "inverse_behavioral_support_need": 0.20,
    }
)


class AdaptationPolicyError(ValueError):
    """Raised when an adaptation signal is malformed or outside [0, 1]."""


def _unit_interval(value: Any, *, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AdaptationPolicyError(
            f"{field_name} must be a finite number in [0, 1]"
        )
    number = float(value)
    if not math.isfinite(number) or not 0.0 <= number <= 1.0:
        raise AdaptationPolicyError(
            f"{field_name} must be a finite number in [0, 1]"
        )
    return number


def _clamp_unit(value: float) -> float:
    return max(0.0, min(1.0, value))


@dataclass(frozen=True)
class SupportSignals:
    """Bounded state and behavioral proxies for current hint depth.

    ``None`` means the behavioral proxy was not observed; its term is omitted
    rather than silently treating unknown as either no need or maximum need.
    """

    mastery: float
    failure_streak_norm: float
    recent_hint_ratio: float
    behavioral_support_need: float | None
    recent_unassisted_success: float

    def __post_init__(self) -> None:
        for field_name in (
            "mastery",
            "failure_streak_norm",
            "recent_hint_ratio",
            "recent_unassisted_success",
        ):
            object.__setattr__(
                self,
                field_name,
                _unit_interval(getattr(self, field_name), field_name=field_name),
            )
        if self.behavioral_support_need is not None:
            object.__setattr__(
                self,
                "behavioral_support_need",
                _unit_interval(
                    self.behavioral_support_need,
                    field_name="behavioral_support_need",
                ),
            )


@dataclass(frozen=True)
class SupportDecision:
    support_need: float
    support_level: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "policy_version": SUPPORT_POLICY_VERSION,
            "calibration_status": ADAPTATION_CALIBRATION_STATUS,
            "support_need": self.support_need,
            "support_level": self.support_level,
        }


@dataclass(frozen=True)
class ChallengeSignals:
    """Signals used only to select a later exercise's challenge level.

    A missing behavioral proxy contributes zero.  In particular, it must not
    receive the positive ``1 - 0`` reward that means an observed zero need.
    """

    mastery: float
    recent_unassisted_success: float
    behavioral_support_need: float | None

    def __post_init__(self) -> None:
        for field_name in (
            "mastery",
            "recent_unassisted_success",
        ):
            object.__setattr__(
                self,
                field_name,
                _unit_interval(getattr(self, field_name), field_name=field_name),
            )
        if self.behavioral_support_need is not None:
            object.__setattr__(
                self,
                "behavioral_support_need",
                _unit_interval(
                    self.behavioral_support_need,
                    field_name="behavioral_support_need",
                ),
            )


@dataclass(frozen=True)
class ChallengeDecision:
    challenge_index: float

    @property
    def challenge_readiness(self) -> float:
        """Compatibility alias for clients migrated from the v1 draft."""

        return self.challenge_index

    def to_dict(self) -> dict[str, Any]:
        return {
            "policy_version": CHALLENGE_INDEX_POLICY_VERSION,
            "calibration_status": ADAPTATION_CALIBRATION_STATUS,
            "challenge_index": self.challenge_index,
            "challenge_readiness": self.challenge_index,
            "usage": "NEXT_EXERCISE_DIFFICULTY_ONLY",
        }


def support_level_for_need(support_need: float) -> int:
    """Map the full unit interval into four left-closed support bands."""

    value = _unit_interval(support_need, field_name="support_need")
    if value < 0.25:
        return 1
    if value < 0.50:
        return 2
    if value < 0.75:
        return 3
    return 4


def evaluate_support_need(signals: SupportSignals) -> SupportDecision:
    """Calculate current-task support without influencing target ordering."""

    if not isinstance(signals, SupportSignals):
        raise AdaptationPolicyError("signals must be SupportSignals")
    raw = (
        SUPPORT_WEIGHTS["mastery_deficit"] * (1.0 - signals.mastery)
        + SUPPORT_WEIGHTS["failure_streak_norm"]
        * signals.failure_streak_norm
        + SUPPORT_WEIGHTS["recent_hint_ratio"] * signals.recent_hint_ratio
        + SUPPORT_WEIGHTS["behavioral_support_need"]
        * (
            signals.behavioral_support_need
            if signals.behavioral_support_need is not None
            else 0.0
        )
        + SUPPORT_WEIGHTS["recent_unassisted_success"]
        * signals.recent_unassisted_success
    )
    support_need = _clamp_unit(raw)
    return SupportDecision(
        support_need=support_need,
        support_level=support_level_for_need(support_need),
    )


def evaluate_challenge_index(
    signals: ChallengeSignals,
) -> ChallengeDecision:
    """Calculate a separate next-exercise difficulty index.

    This is a transparent weighted index, not a sigmoid controller, variational
    solution, or calibrated psychometric estimate.  Offline calibration may
    replace the weights only under a new version.
    """

    if not isinstance(signals, ChallengeSignals):
        raise AdaptationPolicyError("signals must be ChallengeSignals")
    raw = (
        CHALLENGE_WEIGHTS["mastery"] * signals.mastery
        + CHALLENGE_WEIGHTS["recent_unassisted_success"]
        * signals.recent_unassisted_success
        + (
            CHALLENGE_WEIGHTS["inverse_behavioral_support_need"]
            * (1.0 - signals.behavioral_support_need)
            if signals.behavioral_support_need is not None
            else 0.0
        )
    )
    return ChallengeDecision(challenge_index=_clamp_unit(raw))


def evaluate_challenge_readiness(
    signals: ChallengeSignals,
) -> ChallengeDecision:
    """Compatibility alias for :func:`evaluate_challenge_index`."""

    return evaluate_challenge_index(signals)


__all__ = [
    "ADAPTATION_CALIBRATION_STATUS",
    "AdaptationPolicyError",
    "CHALLENGE_INDEX_POLICY_VERSION",
    "CHALLENGE_POLICY_VERSION",
    "CHALLENGE_WEIGHTS",
    "ChallengeDecision",
    "ChallengeSignals",
    "SUPPORT_POLICY_VERSION",
    "SUPPORT_WEIGHTS",
    "SupportDecision",
    "SupportSignals",
    "evaluate_challenge_readiness",
    "evaluate_challenge_index",
    "evaluate_support_need",
    "support_level_for_need",
]
