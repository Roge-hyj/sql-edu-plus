"""Normalize Question-Skill provenance vocabulary.

Revision ID: h8i9j0k1l2m3
Revises: g7h8i9j0k1l2

The first draft used ``AI_GENERATED`` and ``INFERRED_REVIEWED``.  Phase 3's
machine-readable contract uses the shorter ``GENERATED`` and ``INFERRED``
values, while retaining the same trust rule: only AUTHOR_DECLARED and
GENERATED rows can produce a positive learning observation.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "h8i9j0k1l2m3"
down_revision: Union[str, Sequence[str], None] = "g7h8i9j0k1l2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Drop the old constraint before changing values.  A database that was
    # upgraded through the first d3 revision still has the old
    # AI_GENERATED/INFERRED_REVIEWED check, so updating first would be rejected
    # by MySQL.  Dropping first is also safe for a fresh database whose d3
    # migration already uses the canonical vocabulary.
    op.drop_constraint(
        op.f("ck_question_skills_provenance_allowed"),
        "question_skills",
        type_="check",
    )
    # Normalize existing rows after the old constraint is gone.  This is
    # idempotent with respect to the canonical values and safe for an empty
    # table, which is the normal state for legacy questions.
    op.execute(
        sa.text(
            "UPDATE question_skills "
            "SET provenance = 'GENERATED' "
            "WHERE provenance = 'AI_GENERATED'"
        )
    )
    op.execute(
        sa.text(
            "UPDATE question_skills "
            "SET provenance = 'INFERRED' "
            "WHERE provenance = 'INFERRED_REVIEWED'"
        )
    )
    op.create_check_constraint(
        op.f("ck_question_skills_provenance_allowed"),
        "question_skills",
        "provenance IN ('AUTHOR_DECLARED', 'GENERATED', 'INFERRED')",
    )


def downgrade() -> None:
    # The reverse conversion has the same ordering requirement: canonical
    # values cannot be changed back while the canonical CHECK is active.
    op.drop_constraint(
        op.f("ck_question_skills_provenance_allowed"),
        "question_skills",
        type_="check",
    )
    op.execute(
        sa.text(
            "UPDATE question_skills "
            "SET provenance = 'AI_GENERATED' "
            "WHERE provenance = 'GENERATED'"
        )
    )
    op.execute(
        sa.text(
            "UPDATE question_skills "
            "SET provenance = 'INFERRED_REVIEWED' "
            "WHERE provenance = 'INFERRED'"
        )
    )
    op.create_check_constraint(
        op.f("ck_question_skills_provenance_allowed"),
        "question_skills",
        "provenance IN "
        "('AUTHOR_DECLARED', 'AI_GENERATED', 'INFERRED_REVIEWED')",
    )
