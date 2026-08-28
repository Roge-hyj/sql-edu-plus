"""add non-semantic Phase 3 behavior event audit

Revision ID: i9j0k1l2m3n4
Revises: h8i9j0k1l2m3

Syntax, platform, safety, and undecided outcomes are tracked separately from
skill observation events.  They may inform a bounded behavioral support proxy,
but they can never become a BKT observation.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "i9j0k1l2m3n4"
down_revision: Union[str, Sequence[str], None] = "h8i9j0k1l2m3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "phase3_behavior_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("submission_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("question_id", sa.Integer(), nullable=False),
        sa.Column("event_kind", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "event_kind IN ('SYNTAX_ERROR', 'PLATFORM_ERROR', "
            "'SAFETY_BLOCKED', 'UNDECIDED')",
            name="event_kind_allowed",
        ),
        sa.ForeignKeyConstraint(
            ["submission_id"],
            ["submissions.id"],
            name="fk_phase3_behavior_events_submission_id_submissions",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_phase3_behavior_events_user_id_users",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["question_id"],
            ["questions.id"],
            name="fk_phase3_behavior_events_question_id_questions",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_phase3_behavior_events"),
        sa.UniqueConstraint(
            "submission_id",
            name="uq_phase3_behavior_events_submission",
        ),
    )
    op.create_index(
        "ix_phase3_behavior_events_user_recent",
        "phase3_behavior_events",
        ["user_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_phase3_behavior_events_user_recent",
        table_name="phase3_behavior_events",
    )
    op.drop_table("phase3_behavior_events")
