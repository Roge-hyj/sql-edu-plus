"""add versioned Question-Skill Q-matrix

Revision ID: d3e4f5a6b7c8
Revises: c2d3e4f5a6b7
Create Date: 2026-08-24

The migration intentionally leaves existing questions unmapped.  Reference
SQL is not an authoritative assessment-design source and is never backfilled.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d3e4f5a6b7c8"
down_revision: Union[str, Sequence[str], None] = "c2d3e4f5a6b7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "question_skills",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("question_id", sa.Integer(), nullable=False),
        sa.Column("skill_id", sa.String(length=128), nullable=False),
        sa.Column("taxonomy_version", sa.String(length=64), nullable=False),
        sa.Column("skill_role", sa.String(length=16), nullable=False),
        sa.Column("observable_on_correct", sa.Boolean(), nullable=False),
        sa.Column("provenance", sa.String(length=24), nullable=False),
        sa.CheckConstraint(
            "skill_role IN ('PRIMARY', 'SUPPORTING')",
            name="role_allowed",
        ),
        sa.CheckConstraint(
            "provenance IN "
            "('AUTHOR_DECLARED', 'GENERATED', 'INFERRED')",
            name="provenance_allowed",
        ),
        sa.CheckConstraint(
            "skill_role = 'PRIMARY' OR NOT observable_on_correct",
            name="supporting_not_observable",
        ),
        sa.ForeignKeyConstraint(
            ["question_id"],
            ["questions.id"],
            name="fk_question_skills_question_id_questions",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_question_skills"),
        sa.UniqueConstraint(
            "question_id",
            "skill_id",
            "taxonomy_version",
            name="uq_question_skills_question_skill_taxonomy",
        ),
    )
    op.create_index(
        "ix_question_skills_question_id",
        "question_skills",
        ["question_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_question_skills_question_id",
        table_name="question_skills",
    )
    op.drop_table("question_skills")
