"""Fail-closed admission tests for trusted Phase 3 skill observations."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
import json

import pytest

from core.error_diagnosis import (
    DIAGNOSIS_VERSION,
    PUBLIC_SCHEMA_VERSION,
    RULE_CATALOG_VERSION,
)
from core.phase3_observation import (
    OBSERVATION_SCHEMA_VERSION,
    QUESTION_QMATRIX_SOURCE_VERSION,
    ObservationResult,
    ObservationSkipReason,
    ObservationSource,
    build_skill_observations,
    build_trusted_skill_observations,
)
from core.phase3_skill_catalog import (
    ATOMIC_SKILL_TAXONOMY_VERSION,
    ProjectionReasonCode,
    RULE_SKILL_MAP_VERSION,
)
from models.question_skill import SQL_KNOWLEDGE_TAXONOMY_VERSION
from repository.phase3_learning_repo import TrustedSkillObservationInput


def _correct_package(**overrides):
    package = {
        "schema_version": PUBLIC_SCHEMA_VERSION,
        "diagnosis_version": DIAGNOSIS_VERSION,
        "rule_catalog_version": RULE_CATALOG_VERSION,
        "verdict": "CORRECT",
        "diagnosis_status": "OPERATIONALLY_ACCEPTED",
        "phase1": {
            "status": "SUPPORTED",
            "equivalence_conclusion": "NO_COUNTEREXAMPLE_FOUND",
            "judge_status": "CORRECT",
        },
        "primary": None,
        "secondary": [],
        "secondary_count": 0,
    }
    package.update(overrides)
    return package


def _candidate(
    number: int,
    rule_id: str,
    stage: str,
    logical_stage: str,
    *,
    evidence_grade: str = "CAUSAL_VERIFIED",
    knowledge_points=None,
):
    return {
        "candidate_id": f"candidate_{number:016x}",
        "rule_id": rule_id,
        "stage": stage,
        "logical_stage": logical_stage,
        "scope_id": "root",
        "knowledge_points": (
            list(knowledge_points) if knowledge_points is not None else []
        ),
        "evidence_grade": evidence_grade,
        "evidence_refs": {
            "diff_ids": [f"diff_{number}"],
            "verified_diff_ids": [f"diff_{number}"],
            "unverified_diff_ids": [],
            "obligation_ids": [],
            "mutation_test_ids": [],
        },
    }


def _incorrect_package(primary, secondary=(), **overrides):
    candidates = [primary, *secondary]
    package = {
        "schema_version": PUBLIC_SCHEMA_VERSION,
        "diagnosis_version": DIAGNOSIS_VERSION,
        "rule_catalog_version": RULE_CATALOG_VERSION,
        "verdict": "INCORRECT",
        "diagnosis_status": "SUPPORTED",
        "phase1": {
            "status": "SUPPORTED",
            "equivalence_conclusion": "NOT_EQUIVALENT",
            "judge_status": "WRONG",
        },
        "ordered_diff_pipeline": [
            {
                "diff_id": item["evidence_refs"]["diff_ids"][0],
                "evidence_grade": item["evidence_grade"],
            }
            for item in candidates
            if isinstance(item, dict)
            and item.get("evidence_refs", {}).get("diff_ids")
        ],
        "primary": primary,
        "secondary": list(secondary),
        "secondary_count": len(secondary),
    }
    package.update(overrides)
    return package


def _qmatrix(
    skill_id: str,
    *,
    taxonomy_version: str = SQL_KNOWLEDGE_TAXONOMY_VERSION,
    role: str = "PRIMARY",
    observable_on_correct: bool = True,
    provenance: str = "AUTHOR_DECLARED",
):
    return {
        "skill_id": skill_id,
        "taxonomy_version": taxonomy_version,
        "role": role,
        "observable_on_correct": observable_on_correct,
        "provenance": provenance,
    }


def _skip_reasons(result):
    return [item.reason_code for item in result.skipped]


def test_correct_observes_only_explicit_primary_qmatrix_skills():
    result = build_skill_observations(
        _correct_package(),
        [
            _qmatrix("where"),
            _qmatrix(
                "filter.boundary",
                taxonomy_version=ATOMIC_SKILL_TAXONOMY_VERSION,
                provenance="INFERRED_REVIEWED",
            ),
            _qmatrix("group-by", role="SUPPORTING", observable_on_correct=False),
            _qmatrix("having", observable_on_correct=False),
        ],
    )

    assert result.status == "READY_WITH_SKIPS"
    assert {
        (item.taxonomy_version, item.skill_id)
        for item in result.observations
    } == {
        (SQL_KNOWLEDGE_TAXONOMY_VERSION, "where"),
    }
    assert all(item.result is ObservationResult.CORRECT for item in result.observations)
    assert all(item.source is ObservationSource.QUESTION_QMATRIX for item in result.observations)
    assert all(item.source_role == "PRIMARY" for item in result.observations)
    assert _skip_reasons(result) == [
        ObservationSkipReason.QMATRIX_NOT_OBSERVABLE.value,
        ObservationSkipReason.QMATRIX_PROVENANCE_UNTRUSTED.value,
        ObservationSkipReason.QMATRIX_SUPPORTING.value,
    ]


def test_explicitly_generated_primary_qmatrix_row_can_produce_positive_observation():
    result = build_skill_observations(
        _correct_package(),
        [
            _qmatrix(
                "filter.boundary",
                taxonomy_version=ATOMIC_SKILL_TAXONOMY_VERSION,
                provenance="GENERATED",
            )
        ],
    )

    assert result.status == "READY"
    atomic = result.observations[0]
    assert atomic.qmatrix_provenance == "GENERATED"
    assert atomic.trusted_atomic_observation is True
    persisted_input = TrustedSkillObservationInput(
        **atomic.to_persistence_kwargs()
    )
    assert persisted_input.source_provenance == "GENERATED"


def test_answer_revealed_blocks_positive_before_qmatrix_is_touched():
    class ExplosiveQMatrix:
        def __getattribute__(self, name):
            raise AssertionError(f"Q-matrix must not be read after reveal: {name}")

    result = build_skill_observations(
        _correct_package(),
        ExplosiveQMatrix(),
        answer_revealed=True,
    )

    assert result.status == "SKIPPED"
    assert result.observations == ()
    assert _skip_reasons(result) == [ObservationSkipReason.ANSWER_REVEALED.value]


def test_answer_revealed_blocks_negative_rule_observations_too():
    result = build_skill_observations(
        _incorrect_package(
            _candidate(18, "S2_BOUNDARY", "S2", "ROW_FILTER")
        ),
        answer_revealed=True,
    )

    assert result.status == "SKIPPED"
    assert result.observations == ()
    assert _skip_reasons(result) == [ObservationSkipReason.ANSWER_REVEALED.value]


@pytest.mark.parametrize(
    ("package", "question_skills", "expected_reason"),
    [
        (
            _correct_package(),
            None,
            ObservationSkipReason.SKIP_NO_ASSESSMENT_MAP.value,
        ),
        (
            _correct_package(
                phase1={
                    "status": "SUPPORTED",
                    "equivalence_conclusion": "UNDECIDED",
                    "judge_status": "UNDECIDED",
                }
            ),
            [_qmatrix("where")],
            ObservationSkipReason.PHASE2_CORRECT_CONTRACT_INVALID.value,
        ),
        (
            _correct_package(diagnosis_status="PARTIAL"),
            [_qmatrix("where")],
            ObservationSkipReason.PHASE2_CORRECT_CONTRACT_INVALID.value,
        ),
    ],
)
def test_positive_evidence_fails_closed_when_unmapped_or_not_operationally_accepted(
    package,
    question_skills,
    expected_reason,
):
    result = build_skill_observations(package, question_skills)

    assert result.status == (
        ObservationSkipReason.SKIP_NO_ASSESSMENT_MAP.value
        if expected_reason == ObservationSkipReason.SKIP_NO_ASSESSMENT_MAP.value
        else "SKIPPED"
    )
    assert result.observations == ()
    assert _skip_reasons(result) == [expected_reason]


def test_invalid_qmatrix_rows_are_individually_skipped_without_skill_inference():
    result = build_skill_observations(
        _correct_package(),
        [
            _qmatrix("future-skill", taxonomy_version="future.taxonomy.v9"),
            _qmatrix("attacker.chosen_skill", taxonomy_version=ATOMIC_SKILL_TAXONOMY_VERSION),
            _qmatrix("where", provenance="CLIENT_ASSERTED"),
            _qmatrix("where", observable_on_correct=1),
        ],
    )

    assert result.status == "SKIPPED"
    assert result.observations == ()
    assert _skip_reasons(result) == [
        ObservationSkipReason.QMATRIX_NOT_OBSERVABLE.value,
        ObservationSkipReason.QMATRIX_PROVENANCE_UNTRUSTED.value,
        ObservationSkipReason.QMATRIX_SKILL_UNKNOWN.value,
        ObservationSkipReason.QMATRIX_TAXONOMY_UNSUPPORTED.value,
    ]


def test_incorrect_observes_only_strong_rule_projected_atomic_skills():
    primary = _candidate(
        1,
        "S2_BOUNDARY",
        "S2",
        "ROW_FILTER",
        knowledge_points=["attacker.chosen_skill", "aggregate.fanout"],
    )
    secondary = _candidate(
        2,
        "S5_FANOUT_AGGREGATE",
        "S5",
        "PROJECTION",
        evidence_grade="REPAIR_VERIFIED",
        knowledge_points=["filter.boundary"],
    )

    class ExplosiveQMatrix:
        def __getattribute__(self, name):
            raise AssertionError(f"incorrect path must ignore Q-matrix: {name}")

    result = build_skill_observations(
        _incorrect_package(primary, [secondary]),
        ExplosiveQMatrix(),
    )

    assert result.status == "READY"
    assert [item.skill_id for item in result.observations] == [
        "filter.boundary",
        "aggregate.fanout",
    ]
    assert [item.source_role for item in result.observations] == [
        "PRIMARY",
        "SECONDARY",
    ]
    assert [item.logical_stage for item in result.observations] == [
        "ROW_FILTER",
        "PROJECTION",
    ]
    assert all(item.result is ObservationResult.INCORRECT for item in result.observations)
    assert all(item.source is ObservationSource.PHASE2_RULE for item in result.observations)
    assert all(
        item.taxonomy_version == ATOMIC_SKILL_TAXONOMY_VERSION
        for item in result.observations
    )
    assert all(item.trusted_atomic_observation is True for item in result.observations)
    assert "attacker.chosen_skill" not in repr(result.to_dict())

    persistence = result.observations[0].to_persistence_kwargs()
    assert persistence["is_correct"] is False
    assert persistence["source_type"] == "PHASE2_RULE"
    assert persistence["source_version"] == RULE_SKILL_MAP_VERSION
    assert persistence["phase2_candidate_id"] == "candidate_0000000000000001"
    assert persistence["rule_id"] == "S2_BOUNDARY"
    assert persistence["logical_stage"] == "ROW_FILTER"
    assert persistence["source_provenance"] is None


def test_incorrect_path_never_traverses_display_knowledge_points():
    class ExplosiveKnowledgePoints:
        def __iter__(self):
            raise AssertionError("display knowledge_points must not be consumed")

        def __repr__(self):
            raise AssertionError("display knowledge_points must not be rendered")

    primary = _candidate(3, "S3_GROUP_KEY_MISSING", "S3", "GROUP_AGG")
    primary["knowledge_points"] = ExplosiveKnowledgePoints()

    result = build_skill_observations(_incorrect_package(primary))

    assert result.status == "READY"
    assert result.observations[0].skill_id == "group.key_completeness"


@pytest.mark.parametrize(
    ("package", "expected_reason"),
    [
        (
            _incorrect_package(
                _candidate(4, "S2_NULL_LOGIC", "S2", "ROW_FILTER"),
                diagnosis_status="PARTIAL",
            ),
            "PHASE2_DIAGNOSIS_PARTIAL",
        ),
        (
            _incorrect_package(
                _candidate(
                    5,
                    "S2_NULL_LOGIC",
                    "S2",
                    "ROW_FILTER",
                    evidence_grade="PAIR_DISTINGUISHED",
                )
            ),
            "CANDIDATE_EVIDENCE_NOT_STRONG",
        ),
        (
            {
                **_correct_package(),
                "verdict": "UNDECIDED",
                "diagnosis_status": "UNDECIDED",
            },
            ObservationSkipReason.PHASE2_VERDICT_UNDECIDED.value,
        ),
    ],
)
def test_partial_weak_and_undecided_evidence_never_create_observations(
    package,
    expected_reason,
):
    result = build_skill_observations(package, [_qmatrix("where")])

    assert result.status == "SKIPPED"
    assert result.observations == ()
    assert _skip_reasons(result) == [expected_reason]


def test_missing_or_noncanonical_logical_stage_is_not_schedulable_evidence():
    result = build_skill_observations(
        _incorrect_package(
            _candidate(6, "S2_BOOLEAN_LOGIC", "S2", "S2")
        )
    )

    assert result.status == "SKIPPED"
    assert result.observations == ()
    assert _skip_reasons(result) == [
        ProjectionReasonCode.CANDIDATE_LOGICAL_STAGE_MISMATCH.value
    ]


def test_duplicate_qmatrix_skill_keeps_best_provenance_once_and_is_replay_stable():
    skills = [
        _qmatrix("where", provenance="AI_GENERATED"),
        _qmatrix("where", provenance="AUTHOR_DECLARED"),
        _qmatrix("group-by"),
    ]

    first = build_skill_observations(_correct_package(), skills)
    second = build_trusted_skill_observations(_correct_package(), skills)

    assert first.to_dict() == second.to_dict()
    assert [item.skill_id for item in first.observations] == ["group-by", "where"]
    where = next(item for item in first.observations if item.skill_id == "where")
    assert where.qmatrix_provenance == "AUTHOR_DECLARED"
    assert _skip_reasons(first) == [
        ObservationSkipReason.DUPLICATE_SKILL_OBSERVATION.value
    ]


def test_observation_contract_is_frozen_json_safe_and_taxonomy_versioned_once():
    result = build_skill_observations(
        _incorrect_package(
            _candidate(7, "S6_ORDER_OFFSET", "S6", "PAGINATION")
        )
    )
    observation = result.observations[0]

    with pytest.raises(FrozenInstanceError):
        observation.skill_id = "changed"

    payload = result.to_dict()
    assert payload["schema_version"] == OBSERVATION_SCHEMA_VERSION
    assert payload["observation_count"] == 1
    assert payload["observations"][0]["taxonomy_version"] == ATOMIC_SKILL_TAXONOMY_VERSION
    assert payload["observations"][0]["source_version"] == RULE_SKILL_MAP_VERSION
    assert json.loads(json.dumps(payload, ensure_ascii=False)) == payload


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        (
            {"schema_version": "phase2.public.future"},
            ObservationSkipReason.PHASE2_SCHEMA_VERSION_UNSUPPORTED.value,
        ),
        (
            {"diagnosis_version": "phase2.future"},
            ObservationSkipReason.PHASE2_DIAGNOSIS_VERSION_UNSUPPORTED.value,
        ),
        (
            {"rule_catalog_version": "phase2.rules.future"},
            ObservationSkipReason.PHASE2_RULE_CATALOG_VERSION_UNSUPPORTED.value,
        ),
    ],
)
def test_phase2_contract_version_drift_fails_closed(overrides, reason):
    result = build_skill_observations(
        _correct_package(**overrides),
        [_qmatrix("where")],
    )

    assert result.status == "SKIPPED"
    assert result.observations == ()
    assert _skip_reasons(result) == [reason]


def test_non_boolean_answer_revealed_is_rejected_instead_of_coerced():
    result = build_skill_observations(
        _correct_package(),
        [_qmatrix("where")],
        answer_revealed=1,
    )

    assert result.status == "SKIPPED"
    assert result.observations == ()
    assert _skip_reasons(result) == [
        ObservationSkipReason.ANSWER_REVEALED_INVALID.value
    ]
