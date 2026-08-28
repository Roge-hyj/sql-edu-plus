"""Persistence and idempotency tests for Phase 3 raw BKT state."""

import importlib.util
import io
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
from sqlalchemy import select

from core.phase3_bkt import (
    BKT_PARAMETERS_V1,
    BKTParameters,
    smooth_display_mastery,
    update_bkt,
)
from models import Base
from models.phase3_learning import (
    Phase3BehaviorEvent,
    Phase3BehaviorEventKind,
    SkillObservationEvent,
    SkillObservationSource,
    StudentSkillState,
)
from models.question_skill import (
    SQL_KNOWLEDGE_TAXONOMY_VERSION,
    QuestionSkillProvenance,
)
from models.submission import Submission
from repository.phase3_learning_repo import (
    Phase3LearningRepository,
    TrustedSkillObservationInput,
)
from repository.phase3_behavior_repo import Phase3BehaviorEventRepository
from repository.question_skill_repo import QuestionSkillRepository, QuestionSkillSpec


ATOMIC_TAXONOMY = "phase3.atomic_sql_skills.v1"


def _negative(skill_id: str = "filter.boundary") -> TrustedSkillObservationInput:
    return TrustedSkillObservationInput(
        taxonomy_version=ATOMIC_TAXONOMY,
        skill_id=skill_id,
        is_correct=False,
        source_type=SkillObservationSource.PHASE2_RULE,
        source_version="phase3.rule_skill_map.v1",
        evidence_grade="CAUSAL_VERIFIED",
        phase2_candidate_id="candidate-1",
        rule_id="S2_BOUNDARY",
        source_role="FDP",
        logical_stage="ROW_FILTER",
    )


def _positive(skill_id: str = "filter.boundary") -> TrustedSkillObservationInput:
    return TrustedSkillObservationInput(
        taxonomy_version=ATOMIC_TAXONOMY,
        skill_id=skill_id,
        is_correct=True,
        source_type=SkillObservationSource.QUESTION_QMATRIX,
        source_version="question_skill_mapping.v1",
        source_provenance="AUTHOR_DECLARED",
        source_role="PRIMARY",
    )


async def _map_question(session, question, *, include_broad: bool = False) -> None:
    skills = [
        QuestionSkillSpec(
            skill_id="filter.boundary",
            taxonomy_version=ATOMIC_TAXONOMY,
        )
    ]
    if include_broad:
        skills.append(
            QuestionSkillSpec(
                skill_id="where",
                taxonomy_version=SQL_KNOWLEDGE_TAXONOMY_VERSION,
            )
        )
    await QuestionSkillRepository(session).replace_for_question(
        question.id,
        skills,
        provenance=QuestionSkillProvenance.AUTHOR_DECLARED,
    )


def _new_submission(test_user, test_question, *, is_correct: bool) -> Submission:
    return Submission(
        user_id=test_user.id,
        question_id=test_question.id,
        student_sql="SELECT 1",
        ai_hint=None,
        is_correct=is_correct,
        hint_level=1,
    )


def test_learning_models_encode_composite_identity_unique_event_and_cascade():
    state = StudentSkillState.__table__
    event = SkillObservationEvent.__table__

    assert [column.name for column in state.primary_key.columns] == [
        "user_id",
        "taxonomy_version",
        "skill_id",
    ]
    state_fk = next(iter(state.c.user_id.foreign_keys))
    assert state_fk.ondelete == "CASCADE"
    submission_fk = next(iter(event.c.submission_id.foreign_keys))
    assert submission_fk.ondelete == "CASCADE"
    names = {constraint.name for constraint in event.constraints}
    assert (
        "uq_skill_observation_events_submission_taxonomy_skill" in names
    )
    assert "ck_skill_observation_events_prior_probability_bounds" in names
    assert event.c.logical_stage.type.length == 32

    behavior = Phase3BehaviorEvent.__table__
    assert behavior.c.event_kind.type.length == 32
    behavior_fk = next(iter(behavior.c.submission_id.foreign_keys))
    assert behavior_fk.ondelete == "CASCADE"
    behavior_constraints = {constraint.name for constraint in behavior.constraints}
    assert "uq_phase3_behavior_events_submission" in behavior_constraints
    assert "ck_phase3_behavior_events_event_kind_allowed" in behavior_constraints


def test_behavior_event_migration_is_after_qmatrix_normalization_and_has_audit_ddl():
    migration_path = (
        Path(__file__).parents[1]
        / "alembic/versions/i9j0k1l2m3n4_add_phase3_behavior_events.py"
    )
    spec = importlib.util.spec_from_file_location(
        "phase3_behavior_events_migration",
        migration_path,
    )
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    output = io.StringIO()
    context = MigrationContext.configure(
        dialect_name="mysql",
        opts={
            "as_sql": True,
            "output_buffer": output,
            "target_metadata": Base.metadata,
        },
    )
    migration.op = Operations(context)
    migration.upgrade()
    ddl = output.getvalue()

    assert migration.down_revision == "h8i9j0k1l2m3"
    assert "CREATE TABLE phase3_behavior_events" in ddl
    assert "UNIQUE (submission_id)" in ddl
    assert "event_kind IN ('SYNTAX_ERROR', 'PLATFORM_ERROR'" in ddl
    assert "ix_phase3_behavior_events_user_recent" in ddl


def test_qmatrix_provenance_normalization_migration_preserves_constraint_name():
    migration_path = (
        Path(__file__).parents[1]
        / "alembic/versions/h8i9j0k1l2m3_normalize_qmatrix_provenance.py"
    )
    spec = importlib.util.spec_from_file_location(
        "qmatrix_provenance_normalization_migration",
        migration_path,
    )
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    output = io.StringIO()
    context = MigrationContext.configure(
        dialect_name="mysql",
        opts={
            "as_sql": True,
            "output_buffer": output,
            "target_metadata": Base.metadata,
        },
    )
    migration.op = Operations(context)
    migration.upgrade()
    ddl = output.getvalue()

    assert "DROP CHECK ck_question_skills_provenance_allowed" in ddl
    assert "ADD CONSTRAINT ck_question_skills_provenance_allowed CHECK" in ddl
    assert "ck_question_skills_ck_question_skills" not in ddl
    assert "AI_GENERATED" in ddl
    assert "INFERRED_REVIEWED" in ddl
    assert ddl.index("DROP CHECK ck_question_skills_provenance_allowed") < ddl.index(
        "UPDATE question_skills SET provenance = 'GENERATED'"
    )


@pytest.mark.asyncio
async def test_nonsemantic_behavior_event_is_idempotent_and_never_creates_bkt_state(
    test_db_session,
    test_user,
    test_question,
):
    submission = _new_submission(test_user, test_question, is_correct=False)
    test_db_session.add(submission)
    await test_db_session.flush()
    repo = Phase3BehaviorEventRepository(test_db_session)

    first = await repo.record_once(
        submission_id=submission.id,
        user_id=test_user.id,
        question_id=test_question.id,
        event_kind=Phase3BehaviorEventKind.SYNTAX_ERROR,
    )
    retry = await repo.record_once(
        submission_id=submission.id,
        user_id=test_user.id,
        question_id=test_question.id,
        event_kind=Phase3BehaviorEventKind.SYNTAX_ERROR,
    )

    assert first.id == retry.id
    assert await test_db_session.scalar(select(Phase3BehaviorEvent)) is not None
    assert await Phase3LearningRepository(test_db_session).list_states(
        test_user.id
    ) == []
    with pytest.raises(ValueError, match="conflicting behavior event"):
        await repo.record_once(
            submission_id=submission.id,
            user_id=test_user.id,
            question_id=test_question.id,
            event_kind=Phase3BehaviorEventKind.SAFETY_BLOCKED,
        )


def test_mysql_migration_is_direct_successor_and_matches_model_contract():
    migration_path = (
        Path(__file__).parents[1]
        / "alembic/versions/e4f5a6b7c8d9_add_phase3_learning_state.py"
    )
    spec = importlib.util.spec_from_file_location(
        "phase3_learning_state_migration",
        migration_path,
    )
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    output = io.StringIO()
    context = MigrationContext.configure(
        dialect_name="mysql",
        opts={
            "as_sql": True,
            "output_buffer": output,
            "target_metadata": Base.metadata,
        },
    )
    migration.op = Operations(context)
    migration.upgrade()
    ddl = output.getvalue()

    assert migration.down_revision == "d3e4f5a6b7c8"
    assert "CREATE TABLE student_skill_states" in ddl
    assert "CREATE TABLE skill_observation_events" in ddl
    assert "PRIMARY KEY (user_id, taxonomy_version, skill_id)" in ddl
    assert "UNIQUE (submission_id, taxonomy_version, skill_id)" in ddl
    assert "ck_skill_observation_events_result_allowed" in ddl
    assert "logical_stage VARCHAR(32)" in ddl


@pytest.mark.asyncio
async def test_first_observation_creates_raw_state_and_audit_event(
    test_db_session,
    test_user,
    test_question,
):
    submission = _new_submission(test_user, test_question, is_correct=False)
    submission.hint_level = 3
    test_db_session.add(submission)
    await test_db_session.flush()
    repo = Phase3LearningRepository(test_db_session)

    applied = await repo.apply_trusted_observations(
        submission_id=submission.id,
        user_id=test_user.id,
        question_id=test_question.id,
        observations=[_negative()],
        assistance_level=3,
    )

    expected = update_bkt(
        BKT_PARAMETERS_V1.initial_mastery,
        is_correct=False,
    )
    state = await repo.get_state(test_user.id, ATOMIC_TAXONOMY, "filter.boundary")
    assert state is not None
    assert state.posterior_mastery == pytest.approx(expected.posterior_mastery)
    assert state.next_prior == pytest.approx(expected.next_prior)
    assert state.observation_count == 1
    assert state.state_version == 1
    assert applied[0].created is True

    event = await test_db_session.scalar(select(SkillObservationEvent))
    assert event is not None
    assert event.prior_mastery == pytest.approx(0.20)
    assert event.posterior_mastery == pytest.approx(expected.posterior_mastery)
    assert event.next_prior == pytest.approx(expected.next_prior)
    assert event.assistance_level == 3
    assert event.rule_id == "S2_BOUNDARY"


@pytest.mark.asyncio
async def test_event_assistance_must_match_the_delivered_submission_level(
    test_db_session,
    test_user,
    test_question,
):
    submission = _new_submission(test_user, test_question, is_correct=False)
    test_db_session.add(submission)
    await test_db_session.flush()

    with pytest.raises(ValueError, match="delivered submission hint level"):
        await Phase3LearningRepository(test_db_session).apply_trusted_observations(
            submission_id=submission.id,
            user_id=test_user.id,
            question_id=test_question.id,
            observations=[_negative()],
            assistance_level=2,
        )


@pytest.mark.asyncio
async def test_idempotent_retry_does_not_double_update_state(
    test_db_session,
    test_user,
    test_question,
):
    submission = _new_submission(test_user, test_question, is_correct=False)
    test_db_session.add(submission)
    await test_db_session.flush()
    repo = Phase3LearningRepository(test_db_session)
    kwargs = dict(
        submission_id=submission.id,
        user_id=test_user.id,
        question_id=test_question.id,
        observations=[_negative()],
    )

    first = await repo.apply_trusted_observations(**kwargs)
    retry = await repo.apply_trusted_observations(**kwargs)
    state = await repo.get_state(test_user.id, ATOMIC_TAXONOMY, "filter.boundary")
    events = list((await test_db_session.scalars(select(SkillObservationEvent))).all())

    assert first[0].created is True
    assert retry[0].created is False
    assert retry[0].event_id == first[0].event_id
    assert state is not None and state.observation_count == 1
    assert state.state_version == 1
    assert len(events) == 1


@pytest.mark.asyncio
async def test_idempotent_key_rejects_changed_causal_provenance(
    test_db_session,
    test_user,
    test_question,
):
    submission = _new_submission(test_user, test_question, is_correct=False)
    test_db_session.add(submission)
    await test_db_session.flush()
    repo = Phase3LearningRepository(test_db_session)
    await repo.apply_trusted_observations(
        submission_id=submission.id,
        user_id=test_user.id,
        question_id=test_question.id,
        observations=[_negative()],
    )
    changed = TrustedSkillObservationInput(
        taxonomy_version=ATOMIC_TAXONOMY,
        skill_id="filter.boundary",
        is_correct=False,
        source_type=SkillObservationSource.PHASE2_RULE,
        source_version="phase3.rule_skill_map.v1",
        evidence_grade="REPAIR_VERIFIED",
        phase2_candidate_id="candidate-1",
        rule_id="S2_BOUNDARY",
        source_role="FDP",
        logical_stage="ROW_FILTER",
    )

    with pytest.raises(ValueError, match="idempotent retry"):
        await repo.apply_trusted_observations(
            submission_id=submission.id,
            user_id=test_user.id,
            question_id=test_question.id,
            observations=[changed],
        )


@pytest.mark.asyncio
async def test_second_submission_uses_previous_raw_next_prior_not_display_value(
    test_db_session,
    test_user,
    test_question,
):
    await _map_question(test_db_session, test_question)
    repo = Phase3LearningRepository(test_db_session)
    first_submission = _new_submission(test_user, test_question, is_correct=False)
    test_db_session.add(first_submission)
    await test_db_session.flush()
    first = (
        await repo.apply_trusted_observations(
            submission_id=first_submission.id,
            user_id=test_user.id,
            question_id=test_question.id,
            observations=[_negative()],
        )
    )[0]

    display_only = smooth_display_mastery(0.95, first.posterior_mastery)
    second_submission = _new_submission(test_user, test_question, is_correct=True)
    test_db_session.add(second_submission)
    await test_db_session.flush()
    second = (
        await repo.apply_trusted_observations(
            submission_id=second_submission.id,
            user_id=test_user.id,
            question_id=test_question.id,
            observations=[_positive()],
        )
    )[0]

    assert second.prior_mastery == pytest.approx(first.next_prior)
    assert second.prior_mastery != pytest.approx(display_only)
    state = await repo.get_state(test_user.id, ATOMIC_TAXONOMY, "filter.boundary")
    assert state is not None and state.observation_count == 2
    assert state.state_version == 2


@pytest.mark.asyncio
async def test_parameter_version_change_requires_explicit_state_migration(
    test_db_session,
    test_user,
    test_question,
):
    await _map_question(test_db_session, test_question)
    repo = Phase3LearningRepository(test_db_session)
    first_submission = _new_submission(test_user, test_question, is_correct=False)
    test_db_session.add(first_submission)
    await test_db_session.flush()
    await repo.apply_trusted_observations(
        submission_id=first_submission.id,
        user_id=test_user.id,
        question_id=test_question.id,
        observations=[_negative()],
    )

    second_submission = _new_submission(test_user, test_question, is_correct=True)
    test_db_session.add(second_submission)
    await test_db_session.flush()
    future_parameters = BKTParameters(
        version="phase3.bkt_parameters.v2",
        initial_mastery=0.25,
        slip=0.10,
        guess=0.20,
        transition=0.12,
    )
    with pytest.raises(ValueError, match="explicit migration"):
        await repo.apply_trusted_observations(
            submission_id=second_submission.id,
            user_id=test_user.id,
            question_id=test_question.id,
            observations=[_positive()],
            parameters=future_parameters,
        )


@pytest.mark.asyncio
async def test_observation_admission_is_fail_closed(
    test_db_session,
    test_user,
    test_question,
):
    await _map_question(test_db_session, test_question)
    repo = Phase3LearningRepository(test_db_session)
    correct_submission = _new_submission(test_user, test_question, is_correct=True)
    test_db_session.add(correct_submission)
    await test_db_session.flush()

    with pytest.raises(ValueError, match="answer-revealed"):
        await repo.apply_trusted_observations(
            submission_id=correct_submission.id,
            user_id=test_user.id,
            question_id=test_question.id,
            observations=[_positive()],
            answer_revealed=True,
        )

    wrong_source = TrustedSkillObservationInput(
        taxonomy_version=ATOMIC_TAXONOMY,
        skill_id="filter.boundary",
        is_correct=True,
        source_type=SkillObservationSource.QUESTION_QMATRIX,
        source_version="question_skill_mapping.v1",
        source_provenance="GENERATED",
        source_role="PRIMARY",
    )
    with pytest.raises(ValueError, match="Q-matrix"):
        await repo.apply_trusted_observations(
            submission_id=correct_submission.id,
            user_id=test_user.id,
            question_id=test_question.id,
            observations=[wrong_source],
        )

    wrong_submission = _new_submission(test_user, test_question, is_correct=False)
    test_db_session.add(wrong_submission)
    await test_db_session.flush()
    with pytest.raises(ValueError, match="answer-revealed"):
        await repo.apply_trusted_observations(
            submission_id=wrong_submission.id,
            user_id=test_user.id,
            question_id=test_question.id,
            observations=[_negative()],
            answer_revealed=True,
        )


@pytest.mark.asyncio
async def test_state_and_recent_event_queries_keep_taxonomies_separate(
    test_db_session,
    test_user,
    test_question,
):
    await _map_question(test_db_session, test_question, include_broad=True)
    repo = Phase3LearningRepository(test_db_session)
    submission = _new_submission(test_user, test_question, is_correct=True)
    test_db_session.add(submission)
    await test_db_session.flush()
    broad = TrustedSkillObservationInput(
        taxonomy_version="sql_knowledge_points.v1",
        skill_id="where",
        is_correct=True,
        source_type=SkillObservationSource.QUESTION_QMATRIX,
        source_version="question_skill_mapping.v1",
        source_provenance="AUTHOR_DECLARED",
        source_role="PRIMARY",
    )
    await repo.apply_trusted_observations(
        submission_id=submission.id,
        user_id=test_user.id,
        question_id=test_question.id,
        observations=[_positive(), broad],
    )

    states = await repo.list_states(test_user.id)
    assert [(item.taxonomy_version, item.skill_id) for item in states] == [
        (ATOMIC_TAXONOMY, "filter.boundary"),
        ("sql_knowledge_points.v1", "where"),
    ]
    atomic_events = await repo.list_recent_events(
        test_user.id,
        ATOMIC_TAXONOMY,
        "filter.boundary",
    )
    assert len(atomic_events) == 1
    assert atomic_events[0].taxonomy_version == ATOMIC_TAXONOMY
