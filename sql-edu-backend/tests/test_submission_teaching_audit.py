"""Phase 6 teaching-delivery audit validation and persistence tests."""

import importlib.util
from hashlib import sha256
import io
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
from sqlalchemy import JSON, func, select

from models import Base
from models.submission import Submission
from models.submission_teaching_audit import (
    SUBMISSION_TEACHING_AUDIT_SCHEMA_VERSION,
    SubmissionTeachingAudit,
)
from repository.submission_teaching_audit_repo import (
    SubmissionTeachingAuditInput,
    SubmissionTeachingAuditRepository,
)


_HINT = "请重新核对临界值是否应当包含边界。"
_TARGET_FIELDS = {
    "target_candidate_id": "candidate-1",
    "target_rule_id": "S2_BOUNDARY",
    "target_observation_id": "observation-1",
    "target_skill_id": "filter.boundary",
    "target_taxonomy_version": "phase3.atomic_sql_skills.v1",
    "target_logical_stage": "S2",
    "target_source_role": "FDP",
    "target_evidence_grade": "CAUSAL_VERIFIED",
}


def _audit_input(hint: str = _HINT, **overrides) -> SubmissionTeachingAuditInput:
    values = {
        "recommendation_status": "APPLIED",
        "support_need": 0.38,
        "recommended_support_level": 2,
        "delivered_support_level": 2,
        "support_recommendation_applied": True,
        "support_policy_version": "phase3.support_policy.v2",
        "action_policy_version": "phase4.action_selector.v1",
        "feedback_policy_version": "phase5.safe_renderer.v1",
        "generation_source": "PHASE5_LOCAL_TEMPLATE",
        "feedback_status": "PRIMARY",
        "degradation_code": None,
        "answer_revealed": False,
        "feedback_sha256": sha256(hint.encode("utf-8")).hexdigest(),
        **_TARGET_FIELDS,
    }
    values.update(overrides)
    if "action_snapshot" not in overrides:
        values["action_snapshot"] = {
            "schema_version": "phase4.teaching_action.v1",
            "policy_version": values["action_policy_version"],
            "status": "ADAPTIVE_READY",
            "verdict": "INCORRECT",
            "language": "zh-CN",
            "support_need": values["support_need"],
            "support_policy_version": values["support_policy_version"],
            "recommended_support_level": values["recommended_support_level"],
            "delivered_support_level": values["delivered_support_level"],
            "support_recommendation_applied": values[
                "support_recommendation_applied"
            ],
            "adaptive_target_selected": True,
            **{
                field_name: values[field_name]
                for field_name in _TARGET_FIELDS
            },
            "actions": [],
        }
    return SubmissionTeachingAuditInput(**values)


async def _submission(
    session,
    test_user,
    test_question,
    *,
    hint: str | None = _HINT,
    hint_level: int = 2,
) -> Submission:
    row = Submission(
        user_id=test_user.id,
        question_id=test_question.id,
        student_sql="SELECT 1",
        ai_hint=hint,
        is_correct=False,
        hint_level=hint_level,
    )
    session.add(row)
    await session.flush()
    return row


def test_input_rejects_inconsistent_or_unbounded_audit_metadata() -> None:
    valid = _audit_input()
    assert valid.audit_schema_version == SUBMISSION_TEACHING_AUDIT_SCHEMA_VERSION
    assert valid.feedback_sha256 == sha256(_HINT.encode("utf-8")).hexdigest()
    assert valid.answer_revealed is False

    with pytest.raises(ValueError, match="APPLIED recommendation"):
        _audit_input(support_recommendation_applied=False)

    with pytest.raises(ValueError, match="degradation_code"):
        _audit_input(feedback_status="FALLBACK", degradation_code=None)

    with pytest.raises(ValueError, match="stable uppercase code"):
        _audit_input(
            feedback_status="FALLBACK",
            degradation_code="raw provider exception",
        )

    with pytest.raises(TypeError, match="answer_revealed"):
        _audit_input(answer_revealed=0)

    conflicting_snapshot = dict(valid.action_snapshot)
    conflicting_snapshot["target_skill_id"] = "aggregate.fanout"
    with pytest.raises(ValueError, match="action_snapshot conflicts"):
        _audit_input(action_snapshot=conflicting_snapshot)


def test_model_and_mysql_migration_encode_one_to_one_audit_contract() -> None:
    table = SubmissionTeachingAudit.__table__
    assert [column.name for column in table.primary_key.columns] == ["submission_id"]
    submission_fk = next(iter(table.c.submission_id.foreign_keys))
    assert submission_fk.target_fullname == "submissions.id"
    assert submission_fk.ondelete == "CASCADE"
    assert table.c.answer_revealed.nullable is False
    assert isinstance(table.c.action_snapshot.type, JSON)
    assert table.c.action_snapshot.nullable is False
    assert table.c.feedback_sha256.type.length == 64
    constraint_names = {item.name for item in table.constraints}
    assert "ck_submission_teaching_audits_recommendation_consistency" in (
        constraint_names
    )
    assert "ck_submission_teaching_audits_degradation_provenance" in (
        constraint_names
    )

    migration_path = (
        Path(__file__).parents[1]
        / "alembic/versions/f6a7b8c9d0e1_add_submission_teaching_audit.py"
    )
    spec = importlib.util.spec_from_file_location(
        "submission_teaching_audit_migration",
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

    assert migration.down_revision == "f5a6b7c8d9e0"
    assert "CREATE TABLE submission_teaching_audits" in ddl
    assert "PRIMARY KEY (submission_id)" in ddl
    assert "FOREIGN KEY(submission_id) REFERENCES submissions (id) ON DELETE CASCADE" in ddl
    assert "answer_revealed BOOL NOT NULL" in ddl
    assert "action_snapshot JSON NOT NULL" in ddl
    assert "feedback_status IN ('PRIMARY', 'FALLBACK', 'BYPASS')" in ddl

    downgrade_output = io.StringIO()
    downgrade_context = MigrationContext.configure(
        dialect_name="mysql",
        opts={
            "as_sql": True,
            "output_buffer": downgrade_output,
            "target_metadata": Base.metadata,
        },
    )
    migration.op = Operations(downgrade_context)
    migration.downgrade()
    assert "DROP TABLE submission_teaching_audits" in downgrade_output.getvalue()


@pytest.mark.asyncio
async def test_create_and_same_value_retry_are_idempotent(
    test_db_session,
    test_user,
    test_question,
) -> None:
    submission = await _submission(
        test_db_session,
        test_user,
        test_question,
    )
    audit = _audit_input()
    repository = SubmissionTeachingAuditRepository(test_db_session)

    created = await repository.create_once_or_validate(submission.id, audit)
    replayed = await repository.create_once_or_validate(submission.id, audit)

    assert created is replayed
    assert created.submission_id == submission.id
    assert created.recommended_support_level == 2
    assert created.delivered_support_level == submission.hint_level == 2
    assert created.answer_revealed is False
    assert created.action_snapshot == audit.action_snapshot
    assert await test_db_session.scalar(
        select(func.count()).select_from(SubmissionTeachingAudit)
    ) == 1


@pytest.mark.asyncio
async def test_existing_audit_rejects_conflicting_retry(
    test_db_session,
    test_user,
    test_question,
) -> None:
    submission = await _submission(
        test_db_session,
        test_user,
        test_question,
    )
    repository = SubmissionTeachingAuditRepository(test_db_session)
    await repository.create_once_or_validate(submission.id, _audit_input())

    conflicting = _audit_input(generation_source="PHASE5_FALLBACK_TEMPLATE")
    with pytest.raises(ValueError, match="conflicts with retry"):
        await repository.create_once_or_validate(submission.id, conflicting)

    assert await test_db_session.scalar(
        select(func.count()).select_from(SubmissionTeachingAudit)
    ) == 1


@pytest.mark.asyncio
async def test_repository_rejects_hint_digest_and_level_mismatches(
    test_db_session,
    test_user,
    test_question,
) -> None:
    repository = SubmissionTeachingAuditRepository(test_db_session)

    wrong_level = await _submission(
        test_db_session,
        test_user,
        test_question,
        hint_level=1,
    )
    with pytest.raises(ValueError, match="Submission.hint_level"):
        await repository.create_once_or_validate(wrong_level.id, _audit_input())

    wrong_digest = await _submission(
        test_db_session,
        test_user,
        test_question,
    )
    with pytest.raises(ValueError, match="digest"):
        await repository.create_once_or_validate(
            wrong_digest.id,
            _audit_input(feedback_sha256="0" * 64),
        )

    missing_hint = await _submission(
        test_db_session,
        test_user,
        test_question,
        hint=None,
    )
    with pytest.raises(ValueError, match="must be text"):
        await repository.create_once_or_validate(
            missing_hint.id,
            _audit_input(feedback_sha256="0" * 64),
        )

    assert await test_db_session.scalar(
        select(func.count()).select_from(SubmissionTeachingAudit)
    ) == 0
