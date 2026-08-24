"""add idempotent SQL-check attempt identity and response snapshot

Revision ID: f5a6b7c8d9e0
Revises: e4f5a6b7c8d9
Create Date: 2026-08-24

Existing submissions remain NULL and are intentionally not guessed or
deduplicated.  New /ai/check-sql requests require a client-generated UUID.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f5a6b7c8d9e0"
down_revision: Union[str, Sequence[str], None] = "e4f5a6b7c8d9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "submissions",
        sa.Column("attempt_id", sa.String(length=36), nullable=True),
    )
    op.add_column(
        "submissions",
        sa.Column("response_snapshot", sa.JSON(), nullable=True),
    )
    op.add_column(
        "submissions",
        sa.Column("request_fingerprint", sa.String(length=64), nullable=True),
    )
    op.create_unique_constraint(
        "uq_submissions_user_question_attempt",
        "submissions",
        ["user_id", "question_id", "attempt_id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_submissions_user_question_attempt",
        "submissions",
        type_="unique",
    )
    op.drop_column("submissions", "response_snapshot")
    op.drop_column("submissions", "request_fingerprint")
    op.drop_column("submissions", "attempt_id")
