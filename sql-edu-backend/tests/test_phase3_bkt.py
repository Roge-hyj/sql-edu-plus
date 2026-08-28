"""Phase 3 standard-BKT equations and display-state separation."""

import math

import pytest

from core.phase3_bkt import (
    BKT_PARAMETERS_V1,
    BKTParameters,
    smooth_display_mastery,
    update_bkt,
)


def test_correct_and_incorrect_updates_follow_standard_bayes_equations():
    correct = update_bkt(0.20, is_correct=True)
    incorrect = update_bkt(0.20, is_correct=False)

    assert correct.posterior_mastery == pytest.approx(0.18 / (0.18 + 0.16))
    assert correct.next_prior == pytest.approx(
        correct.posterior_mastery
        + (1.0 - correct.posterior_mastery) * BKT_PARAMETERS_V1.transition
    )
    assert incorrect.posterior_mastery == pytest.approx(0.02 / (0.02 + 0.64))
    assert incorrect.next_prior == pytest.approx(
        incorrect.posterior_mastery
        + (1.0 - incorrect.posterior_mastery) * BKT_PARAMETERS_V1.transition
    )


def test_next_observation_uses_raw_next_prior_without_second_smoothing():
    first = update_bkt(0.20, is_correct=True)
    display_only = smooth_display_mastery(0.99, first.posterior_mastery, alpha=0.20)
    second = update_bkt(first.next_prior, is_correct=False)

    assert display_only != pytest.approx(first.next_prior)
    assert second.prior_mastery == pytest.approx(first.next_prior)


@pytest.mark.parametrize("value", [-0.01, 1.01, math.nan, math.inf])
def test_bkt_rejects_invalid_probability(value):
    with pytest.raises(ValueError):
        update_bkt(value, is_correct=True)


def test_parameter_policy_is_versioned_and_keeps_likelihoods_interior():
    assert BKT_PARAMETERS_V1.version == "phase3.bkt_parameters.v1"
    with pytest.raises(ValueError):
        BKTParameters(
            version="bad.v1",
            initial_mastery=0.2,
            slip=0.0,
            guess=0.2,
            transition=0.1,
        )


def test_display_smoothing_is_optional_and_bounded():
    assert smooth_display_mastery(None, 0.4) == pytest.approx(0.4)
    assert smooth_display_mastery(0.2, 0.8, alpha=0.25) == pytest.approx(0.35)
    with pytest.raises(ValueError):
        smooth_display_mastery(0.2, 0.8, alpha=1.1)
