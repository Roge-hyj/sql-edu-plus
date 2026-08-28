from __future__ import annotations

import math

import pytest

from core.support_policy import (
    ADAPTATION_CALIBRATION_STATUS,
    CHALLENGE_POLICY_VERSION,
    CHALLENGE_WEIGHTS,
    SUPPORT_POLICY_VERSION,
    SUPPORT_WEIGHTS,
    AdaptationPolicyError,
    ChallengeSignals,
    SupportSignals,
    evaluate_challenge_readiness,
    evaluate_support_need,
    support_level_for_need,
)


def test_policy_metadata_is_versioned_and_not_claimed_as_calibrated():
    assert SUPPORT_POLICY_VERSION == "phase3.support_policy.v2"
    assert CHALLENGE_POLICY_VERSION == "phase3.challenge_policy.v1"
    assert ADAPTATION_CALIBRATION_STATUS == "UNCALIBRATED_MVP"
    assert SUPPORT_WEIGHTS == {
        "mastery_deficit": 0.35,
        "failure_streak_norm": 0.30,
        "recent_hint_ratio": 0.10,
        "behavioral_support_need": 0.10,
        "recent_unassisted_success": -0.15,
    }
    assert CHALLENGE_WEIGHTS == {
        "mastery": 0.50,
        "recent_unassisted_success": 0.30,
        "inverse_behavioral_support_need": 0.20,
    }


def test_support_formula_matches_the_explainable_five_signal_definition():
    signals = SupportSignals(
        mastery=0.4,
        failure_streak_norm=0.5,
        recent_hint_ratio=0.6,
        behavioral_support_need=0.7,
        recent_unassisted_success=0.2,
    )
    expected = (
        0.35 * (1.0 - 0.4)
        + 0.30 * 0.5
        + 0.10 * 0.6
        + 0.10 * 0.7
        - 0.15 * 0.2
    )

    decision = evaluate_support_need(signals)

    assert decision.support_need == pytest.approx(expected)
    assert decision.support_level == 2
    assert decision.to_dict()["calibration_status"] == "UNCALIBRATED_MVP"


@pytest.mark.parametrize(
    ("support_need", "expected_level"),
    [
        (0.0, 1),
        (0.249999, 1),
        (0.25, 2),
        (0.499999, 2),
        (0.50, 3),
        (0.749999, 3),
        (0.75, 4),
        (1.0, 4),
    ],
)
def test_support_level_boundaries_cover_the_entire_unit_interval(
    support_need, expected_level
):
    assert support_level_for_need(support_need) == expected_level


def test_all_four_support_levels_are_reachable_by_real_signal_combinations():
    decisions = [
        evaluate_support_need(
            SupportSignals(1.0, 0.0, 0.0, 0.0, 0.0)
        ),
        evaluate_support_need(
            SupportSignals(0.4, 0.4, 0.0, 0.0, 0.0)
        ),
        evaluate_support_need(
            SupportSignals(0.0, 0.5, 0.0, 0.0, 0.0)
        ),
        evaluate_support_need(
            SupportSignals(0.0, 1.0, 1.0, 1.0, 0.0)
        ),
    ]

    assert [item.support_level for item in decisions] == [1, 2, 3, 4]
    assert [item.support_need for item in decisions] == pytest.approx(
        [0.0, 0.33, 0.50, 0.85]
    )


def test_support_clamps_negative_result_without_hiding_bad_inputs():
    decision = evaluate_support_need(
        SupportSignals(
            mastery=1.0,
            failure_streak_norm=0.0,
            recent_hint_ratio=0.0,
            behavioral_support_need=0.0,
            recent_unassisted_success=1.0,
        )
    )

    assert decision.support_need == 0.0
    assert decision.support_level == 1


def test_challenge_formula_is_separate_and_for_next_exercise_only():
    signals = ChallengeSignals(
        mastery=0.6,
        recent_unassisted_success=0.5,
        behavioral_support_need=0.25,
    )

    decision = evaluate_challenge_readiness(signals)

    assert decision.challenge_readiness == pytest.approx(
        0.50 * 0.6 + 0.30 * 0.5 + 0.20 * (1.0 - 0.25)
    )
    assert decision.to_dict()["usage"] == "NEXT_EXERCISE_DIFFICULTY_ONLY"
    assert "support_level" not in decision.to_dict()


def test_challenge_readiness_reaches_both_extremes():
    lowest = evaluate_challenge_readiness(ChallengeSignals(0.0, 0.0, 1.0))
    highest = evaluate_challenge_readiness(ChallengeSignals(1.0, 1.0, 0.0))

    assert lowest.challenge_readiness == 0.0
    assert highest.challenge_readiness == 1.0


def test_missing_behavioral_proxy_omits_term_instead_of_rewarding_unknown():
    support = evaluate_support_need(
        SupportSignals(0.2, 1 / 3, 0.0, None, 0.0)
    )
    challenge = evaluate_challenge_readiness(
        ChallengeSignals(0.6, 0.0, None)
    )

    assert support.support_need == pytest.approx(0.38)
    assert challenge.challenge_readiness == pytest.approx(0.30)


@pytest.mark.parametrize("bad_value", [math.nan, math.inf, -0.01, 1.01, True, "0.5"])
def test_signal_boundaries_fail_closed_instead_of_silently_clamping_bad_data(
    bad_value,
):
    with pytest.raises(AdaptationPolicyError, match="mastery"):
        SupportSignals(bad_value, 0.0, 0.0, 0.0, 0.0)
    with pytest.raises(AdaptationPolicyError, match="mastery"):
        ChallengeSignals(bad_value, 0.0, 0.0)


def test_evaluators_reject_unstructured_inputs():
    with pytest.raises(AdaptationPolicyError, match="SupportSignals"):
        evaluate_support_need({"mastery": 0.5})
    with pytest.raises(AdaptationPolicyError, match="ChallengeSignals"):
        evaluate_challenge_readiness({"mastery": 0.5})
