"""merge heads and allow automatic question dialect

Revision ID: c2d3e4f5a6b7
Revises: aa1b2c3d4e5f, b1c2d3e4f5a6
Create Date: 2026-08-12
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c2d3e4f5a6b7"
down_revision: Union[str, Sequence[str], None] = ("aa1b2c3d4e5f", "b1c2d3e4f5a6")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("questions") as batch_op:
        batch_op.alter_column(
            "sql_dialect",
            existing_type=sa.String(length=20),
            nullable=True,
            server_default=None,
        )


def downgrade() -> None:
    op.execute("UPDATE questions SET sql_dialect = 'mysql' WHERE sql_dialect IS NULL")
    with op.batch_alter_table("questions") as batch_op:
        batch_op.alter_column(
            "sql_dialect",
            existing_type=sa.String(length=20),
            nullable=False,
            server_default="mysql",
        )
