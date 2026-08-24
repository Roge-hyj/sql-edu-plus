"""Harden email captcha history and abuse controls.

Revision ID: g7h8i9j0k1l2
Revises: f6a7b8c9d0e1
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "g7h8i9j0k1l2"
down_revision: Union[str, Sequence[str], None] = "f6a7b8c9d0e1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_index("ix_email_captchas_email", table_name="email_captchas")
    op.create_index("ix_email_captchas_email", "email_captchas", ["email"], unique=False)
    op.add_column("email_captchas", sa.Column("ip_address", sa.String(length=45), nullable=True))
    op.add_column(
        "email_captchas",
        sa.Column("failed_attempts", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("email_captchas", sa.Column("used_at", sa.DateTime(), nullable=True))
    op.create_index(
        "ix_email_captchas_ip_created_at",
        "email_captchas",
        ["ip_address", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_email_captchas_ip_created_at", table_name="email_captchas")
    op.drop_column("email_captchas", "used_at")
    op.drop_column("email_captchas", "failed_attempts")
    op.drop_column("email_captchas", "ip_address")
    op.drop_index("ix_email_captchas_email", table_name="email_captchas")
    op.create_index("ix_email_captchas_email", "email_captchas", ["email"], unique=True)
