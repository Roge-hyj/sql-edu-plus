"""Small, auditable Bayesian Knowledge Tracing primitives for Phase 3.

This module deliberately implements only the standard four-parameter BKT
transition.  It does not add a second inertia term to the learning state.
The bundled parameters are an *uncalibrated MVP policy* and are versioned so
that later offline calibration can coexist with historical observation events.
"""

from dataclasses import dataclass
import math


BKT_PARAMETER_VERSION_V1 = "phase3.bkt_parameters.v1"
DISPLAY_MASTERY_POLICY_VERSION_V1 = "phase3.display_mastery.v1"


def _validate_probability(value: float, *, name: str) -> float:
    value = float(value)
    if not math.isfinite(value) or not 0.0 <= value <= 1.0:
        raise ValueError(f"{name} must be a finite probability in [0, 1]")
    return value


@dataclass(frozen=True, slots=True)
class BKTParameters:
    """Versioned four-parameter BKT policy.

    ``initial_mastery`` is P(L0), ``slip`` is P(incorrect | learned),
    ``guess`` is P(correct | not learned), and ``transition`` is the learning
    probability applied after observing the answer.
    """

    version: str
    initial_mastery: float
    slip: float
    guess: float
    transition: float

    def __post_init__(self) -> None:
        if not self.version or len(self.version) > 64:
            raise ValueError("BKT parameter version must be 1..64 characters")
        _validate_probability(self.initial_mastery, name="initial_mastery")
        slip = _validate_probability(self.slip, name="slip")
        guess = _validate_probability(self.guess, name="guess")
        _validate_probability(self.transition, name="transition")
        # Strict interior values keep both Bayes denominators defined even at
        # the endpoints P(L)=0 and P(L)=1.
        if slip in (0.0, 1.0):
            raise ValueError("slip must be strictly between 0 and 1")
        if guess in (0.0, 1.0):
            raise ValueError("guess must be strictly between 0 and 1")


# Conservative defaults for wiring and effect tests only.  They have not been
# fitted to production student traces and must not be described as calibrated.
BKT_PARAMETERS_V1 = BKTParameters(
    version=BKT_PARAMETER_VERSION_V1,
    initial_mastery=0.20,
    slip=0.10,
    guess=0.20,
    transition=0.10,
)


@dataclass(frozen=True, slots=True)
class BKTUpdate:
    prior_mastery: float
    posterior_mastery: float
    next_prior: float
    observation_is_correct: bool
    parameter_version: str


def update_bkt(
    prior_mastery: float,
    *,
    is_correct: bool,
    parameters: BKTParameters = BKT_PARAMETERS_V1,
) -> BKTUpdate:
    """Apply one standard BKT observation and its learning transition.

    The returned ``next_prior`` is the only value that should feed the next
    BKT observation.  ``posterior_mastery`` is the state immediately after the
    Bayes evidence update and before the transition.
    """

    prior = _validate_probability(prior_mastery, name="prior_mastery")
    if not isinstance(is_correct, bool):
        raise TypeError("is_correct must be bool")

    if is_correct:
        learned_likelihood = prior * (1.0 - parameters.slip)
        unlearned_likelihood = (1.0 - prior) * parameters.guess
    else:
        learned_likelihood = prior * parameters.slip
        unlearned_likelihood = (1.0 - prior) * (1.0 - parameters.guess)

    denominator = learned_likelihood + unlearned_likelihood
    if denominator <= 0.0 or not math.isfinite(denominator):
        raise ValueError("BKT observation has an undefined Bayes denominator")

    posterior = learned_likelihood / denominator
    next_prior = posterior + (1.0 - posterior) * parameters.transition
    return BKTUpdate(
        prior_mastery=prior,
        posterior_mastery=posterior,
        next_prior=next_prior,
        observation_is_correct=is_correct,
        parameter_version=parameters.version,
    )


def smooth_display_mastery(
    previous_display: float | None,
    raw_mastery: float,
    *,
    alpha: float = 0.20,
) -> float:
    """Return an optional UI-only exponential moving average.

    This helper is intentionally separate from :func:`update_bkt`.  Its output
    must never be persisted into ``next_prior`` or used as the next BKT prior.
    """

    raw = _validate_probability(raw_mastery, name="raw_mastery")
    alpha = _validate_probability(alpha, name="alpha")
    if previous_display is None:
        return raw
    previous = _validate_probability(previous_display, name="previous_display")
    return alpha * raw + (1.0 - alpha) * previous


__all__ = [
    "BKT_PARAMETER_VERSION_V1",
    "BKT_PARAMETERS_V1",
    "DISPLAY_MASTERY_POLICY_VERSION_V1",
    "BKTParameters",
    "BKTUpdate",
    "smooth_display_mastery",
    "update_bkt",
]
