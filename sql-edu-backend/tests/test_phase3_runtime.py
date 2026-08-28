"""End-to-end Phase 3 runtime policy tests below the HTTP route."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from core.error_diagnosis import (
    DIAGNOSIS_VERSION,
    PUBLIC_SCHEMA_VERSION,
    RULE_CATALOG_VERSION,
)
from core.phase3_bkt import BKT_PARAMETERS_V1, update_bkt
from core.phase3_runtime import (
    apply_phase3_attempt,
    prepare_phase3_attempt,
    summarize_skill_history,
)
from core.phase3_skill_catalog import ATOMIC_SKILL_TAXONOMY_VERSION
from models.question_skill import (
    SQL_KNOWLEDGE_TAXONOMY_VERSION,
    QuestionSkillProvenance,
    QuestionSkillRole,
)
from repository.phase3_learning_repo import Phase3LearningRepository
from repository.question_skill_repo import (
    QuestionSkillRepository,
    QuestionSkillSpec,
)
from repository.submission_repo import SubmissionRepository
from schemas.submission import SubmissionCreate


def _correct_package() -> dict:
    return {
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


def _incorrect_package(*, knowledge_points=None) -> dict:
    return {
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
                "diff_id": "diff_runtime_primary",
                "evidence_grade": "CAUSAL_VERIFIED",
            }
        ],
        "primary": {
            "candidate_id": "candidate_0000000000000001",
            "rule_id": "S2_BOUNDARY",
            "stage": "S2",
            "logical_stage": "ROW_FILTER",
            "evidence_grade": "CAUSAL_VERIFIED",
            "evidence_refs": {
                "diff_ids": ["diff_runtime_primary"],
                "verified_diff_ids": ["diff_runtime_primary"],
                "unverified_diff_ids": [],
                "obligation_ids": [],
                "mutation_test_ids": [],
            },
            "knowledge_points": list(knowledge_points or []),
        },
        "secondary": [],
        "secondary_count": 0,
    }


def _event(*, correct: bool, assistance: int = 1, revealed: bool = False):
    return SimpleNamespace(
        observation_result="CORRECT" if correct else "INCORRECT",
        source_type="PHASE2_RULE",
        assistance_level=assistance,
        answer_revealed=revealed,
    )


def test_history_signals_use_only_bounded_audit_events_and_newest_failure_streak():
    signals = summarize_skill_history(
        [
            _event(correct=False, assistance=3),
            _event(correct=False, assistance=1),
            _event(correct=True, assistance=1),
            _event(correct=True, assistance=4),
        ]
    )

    assert signals.failure_streak_norm == pytest.approx(2 / 3)
    assert signals.recent_hint_ratio == pytest.approx(0.5)
    # Event assistance is feedback delivered after the answer, so it cannot
    # retroactively prove an unassisted success.
    assert signals.recent_unassisted_success == 0.0
    assert 0.0 < signals.recurrence < 1.0


def test_nonsemantic_events_are_separate_counters_and_do_not_dilute_semantics():
    syntax = SimpleNamespace(event_kind="SYNTAX_ERROR")
    safety = SimpleNamespace(event_kind="SAFETY_BLOCKED")
    platform = SimpleNamespace(event_kind="PLATFORM_ERROR")
    semantic_failure = _event(correct=False)

    signals = summarize_skill_history(
        [syntax, safety, platform, semantic_failure]
    )

    assert signals.syntax_error_count == 1
    assert signals.active_event_count == 1
    assert signals.semantic_failure_count == 1
    assert signals.recurrence == pytest.approx(1.0)
    assert signals.behavioral_support_need == pytest.approx(
        0.65 + 0.35 / 3
    )

    # A correct attempt ends the current failure streak, but it does not erase
    # the preceding semantic failure from the bounded behavior proxy.
    after_correct = summarize_skill_history(
        [_event(correct=True), syntax, semantic_failure]
    )
    assert after_correct.failure_streak_norm == 0.0
    assert after_correct.semantic_failure_count == 1
    assert after_correct.behavioral_support_need is not None
    assert after_correct.behavioral_support_need > 0.0


def test_long_idle_interval_starts_a_new_behavior_window():
    now = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)
    old_failure = SimpleNamespace(
        **_event(correct=False).__dict__,
        created_at=now - timedelta(minutes=31),
        id=1,
    )

    signals = summarize_skill_history([old_failure], now=now)

    assert signals.session_reset is True
    assert signals.active_event_count == 0
    assert signals.semantic_failure_count == 0
    assert signals.behavioral_support_need is None


@pytest.mark.asyncio
async def test_correct_runtime_updates_only_authoritative_observable_primary(
    test_db_session,
    test_user,
    test_question,
):
    await QuestionSkillRepository(test_db_session).replace_for_question(
        test_question.id,
        [
            QuestionSkillSpec(
                skill_id="filter.boundary",
                taxonomy_version=ATOMIC_SKILL_TAXONOMY_VERSION,
                role=QuestionSkillRole.PRIMARY,
                observable_on_correct=True,
            ),
            QuestionSkillSpec(
                skill_id="where",
                taxonomy_version=SQL_KNOWLEDGE_TAXONOMY_VERSION,
                role=QuestionSkillRole.SUPPORTING,
                observable_on_correct=False,
            ),
        ],
        provenance=QuestionSkillProvenance.AUTHOR_DECLARED,
    )
    plan = await prepare_phase3_attempt(
        test_db_session,
        user_id=test_user.id,
        question_id=test_question.id,
        expected_is_correct=True,
        diagnostic_package=_correct_package(),
    )

    assert plan.status == "READY"
    assert [(item.taxonomy_version, item.skill_id) for item in plan.persistence_inputs] == [
        (ATOMIC_SKILL_TAXONOMY_VERSION, "filter.boundary")
    ]
    submission = await SubmissionRepository(test_db_session).create(
        SubmissionCreate(
            user_id=test_user.id,
            question_id=test_question.id,
            student_sql="SELECT title FROM course WHERE credits > 3",
            is_correct=True,
            hint_level=1,
        )
    )
    summary = await apply_phase3_attempt(
        test_db_session,
        plan=plan,
        submission_id=submission.id,
        user_id=test_user.id,
        question_id=test_question.id,
    )

    expected = update_bkt(
        BKT_PARAMETERS_V1.initial_mastery,
        is_correct=True,
    )
    state = await Phase3LearningRepository(test_db_session).get_state(
        test_user.id,
        ATOMIC_SKILL_TAXONOMY_VERSION,
        "filter.boundary",
    )
    assert state is not None
    assert state.posterior_mastery == pytest.approx(expected.posterior_mastery)
    assert state.next_prior == pytest.approx(expected.next_prior)
    assert state.observation_count == 1
    assert await Phase3LearningRepository(test_db_session).get_state(
        test_user.id,
        SQL_KNOWLEDGE_TAXONOMY_VERSION,
        "where",
    ) is None
    assert summary.status == "UPDATED"
    assert summary.state_update_count == 1
    assert summary.challenge_readiness == pytest.approx(
        0.5 * expected.next_prior
    )

    retry = await apply_phase3_attempt(
        test_db_session,
        plan=plan,
        submission_id=submission.id,
        user_id=test_user.id,
        question_id=test_question.id,
    )
    assert retry.status == "ALREADY_APPLIED"
    assert retry.state_update_count == 0
    assert state.observation_count == 1


@pytest.mark.asyncio
async def test_correct_unmapped_question_is_accepted_without_bkt_write(
    test_db_session,
    test_user,
    test_question,
):
    plan = await prepare_phase3_attempt(
        test_db_session,
        user_id=test_user.id,
        question_id=test_question.id,
        expected_is_correct=True,
        diagnostic_package=_correct_package(),
    )

    assert plan.status == "SKIP_NO_ASSESSMENT_MAP"
    assert plan.persistence_inputs == ()
    assert plan.no_update_summary().to_public_dict()["status"] == (
        "SKIP_NO_ASSESSMENT_MAP"
    )
    assert await Phase3LearningRepository(test_db_session).list_states(
        test_user.id
    ) == []


@pytest.mark.asyncio
async def test_incorrect_runtime_uses_atomic_rule_not_display_knowledge_union(
    test_db_session,
    test_user,
    test_question,
):
    await QuestionSkillRepository(test_db_session).replace_for_question(
        test_question.id,
        [
            QuestionSkillSpec(
                skill_id="filter.boundary",
                taxonomy_version=ATOMIC_SKILL_TAXONOMY_VERSION,
                role=QuestionSkillRole.SUPPORTING,
                observable_on_correct=False,
            )
        ],
        provenance=QuestionSkillProvenance.AUTHOR_DECLARED,
    )
    plan = await prepare_phase3_attempt(
        test_db_session,
        user_id=test_user.id,
        question_id=test_question.id,
        expected_is_correct=False,
        diagnostic_package=_incorrect_package(
            knowledge_points=["aggregate.fanout", "attacker.skill"]
        ),
    )

    assert [item.skill_id for item in plan.persistence_inputs] == [
        "filter.boundary"
    ]
    assert plan.selected is not None
    assert plan.selected.skill_id == "filter.boundary"
    assert plan.selected.question_alignment == pytest.approx(0.5)
    # First trusted failure: .35*(1-.2) + .30*(1/3) = .38.
    assert plan.support is not None
    assert plan.support.support_need == pytest.approx(0.38)
    assert plan.support.support_level == 2

    submission = await SubmissionRepository(test_db_session).create(
        SubmissionCreate(
            user_id=test_user.id,
            question_id=test_question.id,
            student_sql="SELECT title FROM course WHERE credits >= 3",
            is_correct=False,
            hint_level=1,
        )
    )
    summary = await apply_phase3_attempt(
        test_db_session,
        plan=plan,
        submission_id=submission.id,
        user_id=test_user.id,
        question_id=test_question.id,
    )

    expected = update_bkt(
        BKT_PARAMETERS_V1.initial_mastery,
        is_correct=False,
    )
    state = await Phase3LearningRepository(test_db_session).get_state(
        test_user.id,
        ATOMIC_SKILL_TAXONOMY_VERSION,
        "filter.boundary",
    )
    assert state is not None
    assert state.posterior_mastery == pytest.approx(expected.posterior_mastery)
    assert state.next_prior == pytest.approx(expected.next_prior)
    assert summary.status == "UPDATED"
    assert summary.recommended_support_level == 2
    history = summarize_skill_history(
        await Phase3LearningRepository(test_db_session).list_recent_events(
            test_user.id,
            ATOMIC_SKILL_TAXONOMY_VERSION,
            "filter.boundary",
        )
    )
    assert summary.challenge_index == pytest.approx(
        0.5 * expected.next_prior
        + 0.2 * (1.0 - history.behavioral_support_need)
    )
    assert "aggregate.fanout" not in {
        item.skill_id
        for item in await Phase3LearningRepository(test_db_session).list_states(
            test_user.id
        )
    }


@pytest.mark.asyncio
async def test_runtime_fails_closed_when_phase1_verdict_and_package_disagree(
    test_db_session,
    test_user,
    test_question,
):
    await QuestionSkillRepository(test_db_session).replace_for_question(
        test_question.id,
        [
            QuestionSkillSpec(
                skill_id="filter.boundary",
                taxonomy_version=ATOMIC_SKILL_TAXONOMY_VERSION,
            )
        ],
        provenance=QuestionSkillProvenance.AUTHOR_DECLARED,
    )
    plan = await prepare_phase3_attempt(
        test_db_session,
        user_id=test_user.id,
        question_id=test_question.id,
        expected_is_correct=False,
        diagnostic_package=_correct_package(),
    )

    assert plan.status == "NO_ELIGIBLE_OBSERVATION"
    assert plan.persistence_inputs == ()
