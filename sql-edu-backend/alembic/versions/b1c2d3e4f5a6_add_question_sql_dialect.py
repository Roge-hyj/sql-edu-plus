"""add question sql dialect metadata

Revision ID: b1c2d3e4f5a6
Revises: f2a3b4c5d6e7
Create Date: 2026-07-24

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "b1c2d3e4f5a6"
down_revision: Union[str, Sequence[str], None] = "f2a3b4c5d6e7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "questions",
        sa.Column("sql_dialect", sa.String(length=20), nullable=False, server_default="mysql"),
    )
    op.add_column(
        "questions",
        sa.Column("engine_version", sa.String(length=50), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("questions", "engine_version")
    op.drop_column("questions", "sql_dialect")
