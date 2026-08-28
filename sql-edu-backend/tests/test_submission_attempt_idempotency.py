"""Submission attempt-idempotency schema and migration contract tests."""

import importlib.util
import io
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
from sqlalchemy import JSON, UniqueConstraint
from sqlalchemy.dialects import mysql

from models import Base
from models.submission import Submission
from repository.chat_repo import ChatRepository
from repository.submission_repo import SubmissionRepository


def test_submission_model_has_nullable_legacy_fields_and_scoped_unique_attempt():
    table = Submission.__table__

    assert table.c.attempt_id.type.length == 36
    assert table.c.attempt_id.nullable is True
    assert table.c.request_fingerprint.type.length == 64
    assert table.c.request_fingerprint.nullable is True
    assert isinstance(table.c.response_snapshot.type, JSON)
    assert table.c.response_snapshot.nullable is True

    unique = next(
        constraint
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
        and constraint.name == "uq_submissions_user_question_attempt"
    )
    assert [column.name for column in unique.columns] == [
        "user_id",
        "question_id",
        "attempt_id",
    ]


def test_mysql_attempt_idempotency_migration_matches_submission_model():
    migration_path = (
        Path(__file__).parents[1]
        / "alembic/versions/f5a6b7c8d9e0_add_submission_attempt_idempotency.py"
    )
    spec = importlib.util.spec_from_file_location(
        "submission_attempt_idempotency_migration",
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

    assert migration.down_revision == "e4f5a6b7c8d9"
    assert "ADD COLUMN attempt_id VARCHAR(36)" in ddl
    assert "ADD COLUMN request_fingerprint VARCHAR(64)" in ddl
    assert "ADD COLUMN response_snapshot JSON" in ddl
    assert "uq_submissions_user_question_attempt" in ddl
    assert "UNIQUE (user_id, question_id, attempt_id)" in ddl

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
    downgrade_ddl = downgrade_output.getvalue()
    assert "DROP INDEX uq_submissions_user_question_attempt" in downgrade_ddl
    assert "DROP COLUMN response_snapshot" in downgrade_ddl
    assert "DROP COLUMN request_fingerprint" in downgrade_ddl
    assert "DROP COLUMN attempt_id" in downgrade_ddl


@pytest.mark.asyncio
async def test_post_lock_xp_counts_compile_as_mysql_current_reads():
    compiled: list[str] = []

    class CapturingSession:
        async def scalar(self, statement):
            compiled.append(str(statement.compile(dialect=mysql.dialect())))
            return 0

    session = CapturingSession()
    submissions = SubmissionRepository(session)
    chats = ChatRepository(session)

    await submissions.get_failure_count(1, 2, for_update=True)
    await submissions.get_correct_count(1, 2, for_update=True)
    await chats.count_messages_for_user_question(1, 2, for_update=True)

    assert len(compiled) == 3
    assert all(sql.rstrip().endswith("FOR UPDATE") for sql in compiled)
