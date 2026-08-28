from __future__ import annotations

import itertools
import math

import pytest

from core.causal_priority_scheduler import (
    CausalPriorityCandidate,
    EVIDENCE_STRENGTH_BY_GRADE,
    INSTRUCTIONAL_IMPACT_BY_SKILL,
    PRIORITY_CALIBRATION_STATUS,
    PRIORITY_POLICY_VERSION,
    PRIORITY_WEIGHTS,
    PriorityPolicyError,
    PrioritySchedule,
    evidence_strength_for_grade,
    instructional_impact_for_skill,
    schedule_causal_priorities,
)
from core.error_diagnosis import LOGICAL_STAGE_ORDER
from core.phase3_skill_catalog import (
    ATOMIC_SKILL_TAXONOMY_VERSION,
    RULE_SKILL_CATALOG,
)


def _candidate(
    number: int,
    skill_id: str = "filter.boundary",
    *,
    role: str = "PRIMARY",
    stage: str = "S2",
    recurrence: float = 0.0,
    mastery_deficit: float = 0.0,
    question_alignment: float = 0.0,
    evidence_strength: float = 1.0,
    trusted: bool = True,
) -> CausalPriorityCandidate:
    return CausalPriorityCandidate(
        skill_id=skill_id,
        taxonomy_version=ATOMIC_SKILL_TAXONOMY_VERSION,
        source_role=role,
        logical_stage=stage,
        phase2_candidate_id=f"candidate_{number:016x}",
        trusted_atomic_observation=trusted,
        instructional_impact=instructional_impact_for_skill(skill_id),
        recurrence=recurrence,
        mastery_deficit=mastery_deficit,
        question_alignment=question_alignment,
        evidence_strength=evidence_strength,
    )


def test_policy_is_versioned_complete_and_explicitly_uncalibrated():
    expected_skills = {item.skill_id for item in RULE_SKILL_CATALOG}

    assert PRIORITY_POLICY_VERSION == "phase3.priority_policy.v1"
    assert PRIORITY_CALIBRATION_STATUS == "UNCALIBRATED_MVP"
    assert set(INSTRUCTIONAL_IMPACT_BY_SKILL) == expected_skills
    assert sum(PRIORITY_WEIGHTS.values()) == pytest.approx(1.0)
    assert all(0.0 <= value <= 1.0 for value in PRIORITY_WEIGHTS.values())
    assert all(
        0.0 <= value <= 1.0
        for value in INSTRUCTIONAL_IMPACT_BY_SKILL.values()
    )


def test_score_uses_five_independent_components_exactly():
    candidate = _candidate(
        1,
        recurrence=0.8,
        mastery_deficit=0.7,
        question_alignment=0.6,
        evidence_strength=0.9,
    )
    expected = (
        0.30 * instructional_impact_for_skill("filter.boundary")
        + 0.25 * 0.8
        + 0.20 * 0.7
        + 0.15 * 0.6
        + 0.10 * 0.9
    )

    assert candidate.priority_score == pytest.approx(expected)
    assert candidate.to_dict()["components"] == {
        "instructional_impact": 0.60,
        "recurrence": 0.8,
        "mastery_deficit": 0.7,
        "question_alignment": 0.6,
        "evidence_strength": 0.9,
    }


def test_primary_or_fdp_hard_tier_always_precedes_secondary_score():
    weakest_primary = _candidate(1, recurrence=0.0, mastery_deficit=0.0)
    strongest_secondary = _candidate(
        2,
        "join.bridge_path",
        role="SECONDARY",
        stage="S1",
        recurrence=1.0,
        mastery_deficit=1.0,
        question_alignment=1.0,
    )
    fdp = _candidate(
        3,
        "group.key_redundancy",
        role="FDP",
        stage="S3",
        recurrence=0.0,
        mastery_deficit=0.0,
    )

    schedule = schedule_causal_priorities(
        [strongest_secondary, weakest_primary, fdp]
    )

    assert strongest_secondary.priority_score > weakest_primary.priority_score
    assert [item.source_role.value for item in schedule.ordered] == [
        "PRIMARY",
        "FDP",
        "SECONDARY",
    ]
    assert schedule.selected is weakest_primary


def test_within_tier_score_descends_before_stage_and_identity_tiebreakers():
    low_early = _candidate(1, role="SECONDARY", stage="S1")
    high_late = _candidate(
        2,
        role="SECONDARY",
        stage="S6",
        recurrence=1.0,
        mastery_deficit=1.0,
        question_alignment=1.0,
    )

    schedule = schedule_causal_priorities([low_early, high_late])

    assert schedule.ordered == (high_late, low_early)


def test_tie_breaks_are_stable_across_every_input_permutation():
    stage3_later_id = _candidate(3, role="SECONDARY", stage="S3")
    stage2_later_id = _candidate(2, role="SECONDARY", stage="S2")
    stage2_earlier_id = _candidate(1, role="SECONDARY", stage="S2")
    expected = (
        stage2_earlier_id.phase2_candidate_id,
        stage2_later_id.phase2_candidate_id,
        stage3_later_id.phase2_candidate_id,
    )

    observed = {
        tuple(
            item.phase2_candidate_id
            for item in schedule_causal_priorities(permutation).ordered
        )
        for permutation in itertools.permutations(
            [stage3_later_id, stage2_later_id, stage2_earlier_id]
        )
    }

    assert observed == {expected}


def test_every_canonical_phase2_logical_stage_is_accepted_and_ranked_in_order():
    candidates = [
        _candidate(
            index + 1,
            role="SECONDARY",
            stage=logical_stage,
        )
        for index, logical_stage in enumerate(reversed(LOGICAL_STAGE_ORDER))
    ]

    schedule = schedule_causal_priorities(candidates)

    assert [item.logical_stage for item in schedule.ordered] == list(
        LOGICAL_STAGE_ORDER
    )
    assert [item.logical_stage_rank for item in schedule.ordered] == list(
        range(len(LOGICAL_STAGE_ORDER))
    )


def test_old_lambda_half_degenerates_but_new_score_does_not():
    def old_priority(severity: float, mastery: float, lambda_value: float) -> float:
        challenge = severity + 1.0 - mastery
        scaffolding = 1.0 - severity + mastery
        return lambda_value * challenge + (1.0 - lambda_value) * scaffolding

    old_scores = {
        old_priority(severity, mastery, 0.5)
        for severity, mastery in ((0.0, 0.0), (0.2, 0.9), (1.0, 0.0), (1.0, 1.0))
    }
    new_scores = {
        _candidate(1, recurrence=0.0, mastery_deficit=0.0).priority_score,
        _candidate(
            2,
            recurrence=1.0,
            mastery_deficit=1.0,
            question_alignment=1.0,
        ).priority_score,
    }

    assert old_scores == {1.0}
    assert len(new_scores) == 2


def test_grade_and_impact_helpers_fail_closed():
    assert evidence_strength_for_grade("CAUSAL_VERIFIED") == 1.0
    assert evidence_strength_for_grade("REPAIR_VERIFIED") == 0.9
    assert set(EVIDENCE_STRENGTH_BY_GRADE) == {
        "CAUSAL_VERIFIED",
        "REPAIR_VERIFIED",
    }

    with pytest.raises(PriorityPolicyError, match="not trusted"):
        evidence_strength_for_grade("STRUCTURAL_ONLY")
    with pytest.raises(PriorityPolicyError, match="no phase3.priority_policy.v1"):
        instructional_impact_for_skill("attacker.chosen_skill")


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"trusted_atomic_observation": False}, "only trusted"),
        ({"taxonomy_version": "broad.skills.v1"}, "taxonomy"),
        ({"source_role": "UNKNOWN"}, "source_role"),
        ({"logical_stage": "S7"}, "logical_stage"),
        ({"recurrence": math.nan}, "recurrence"),
        ({"mastery_deficit": -0.01}, "mastery_deficit"),
        ({"question_alignment": 1.01}, "question_alignment"),
        ({"evidence_strength": 0.5}, "trusted evidence grade"),
        ({"instructional_impact": 0.0}, "versioned Phase 3 policy"),
    ],
)
def test_malformed_or_untrusted_candidate_is_rejected(overrides, message):
    kwargs = {
        "skill_id": "filter.boundary",
        "taxonomy_version": ATOMIC_SKILL_TAXONOMY_VERSION,
        "source_role": "PRIMARY",
        "logical_stage": "S2",
        "phase2_candidate_id": "candidate_0000000000000001",
        "trusted_atomic_observation": True,
        "instructional_impact": 0.60,
        "recurrence": 0.0,
        "mastery_deficit": 0.0,
        "question_alignment": 0.0,
        "evidence_strength": 1.0,
    }
    kwargs.update(overrides)

    with pytest.raises(PriorityPolicyError, match=message):
        CausalPriorityCandidate(**kwargs)


def test_schedule_rejects_non_candidates_duplicates_and_excessive_input():
    candidate = _candidate(1)

    with pytest.raises(PriorityPolicyError, match="every item"):
        schedule_causal_priorities([candidate, object()])
    with pytest.raises(PriorityPolicyError, match="duplicate"):
        schedule_causal_priorities([candidate, candidate])
    with pytest.raises(PriorityPolicyError, match="too many"):
        schedule_causal_priorities([_candidate(i + 1) for i in range(65)])


def test_empty_schedule_is_safe_and_json_auditable():
    schedule = schedule_causal_priorities([])

    assert schedule.selected is None
    assert schedule.ordered == ()
    assert schedule.to_dict() == {
        "policy_version": PRIORITY_POLICY_VERSION,
        "calibration_status": "UNCALIBRATED_MVP",
        "candidate_count": 0,
        "selected_skill_id": None,
        "ordered": [],
    }


def test_priority_schedule_constructor_cannot_bypass_selection_constraints():
    primary = _candidate(20, recurrence=0.1)
    secondary = _candidate(21, "join.bridge_path", role="SECONDARY", stage="S1")
    with pytest.raises(PriorityPolicyError, match="canonical priority order"):
        PrioritySchedule(
            ordered=(secondary, primary),
            selected_targets=(secondary, primary),
            secondary_budget=1,
        )

    ordered = (primary, secondary)
    with pytest.raises(PriorityPolicyError, match="primary/FDP"):
        PrioritySchedule(
            ordered=ordered,
            selected_targets=(secondary,),
            secondary_budget=1,
        )
