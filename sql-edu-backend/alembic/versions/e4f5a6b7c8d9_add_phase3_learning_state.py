"""add raw Phase 3 BKT state and observation audit events

Revision ID: e4f5a6b7c8d9
Revises: d3e4f5a6b7c8
Create Date: 2026-08-24

The state table stores the raw standard-BKT posterior and next prior.  No
display smoothing value is written back into the Bayesian state.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e4f5a6b7c8d9"
down_revision: Union[str, Sequence[str], None] = "d3e4f5a6b7c8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "student_skill_states",
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("taxonomy_version", sa.String(length=64), nullable=False),
        sa.Column("skill_id", sa.String(length=128), nullable=False),
        sa.Column("posterior_mastery", sa.Float(), nullable=False),
        sa.Column("next_prior", sa.Float(), nullable=False),
        sa.Column("observation_count", sa.Integer(), nullable=False),
        sa.Column("bkt_parameter_version", sa.String(length=64), nullable=False),
        sa.Column("state_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "posterior_mastery >= 0 AND posterior_mastery <= 1",
            name="posterior_probability_bounds",
        ),
        sa.CheckConstraint(
            "next_prior >= 0 AND next_prior <= 1",
            name="next_prior_probability_bounds",
        ),
        sa.CheckConstraint(
            "observation_count >= 1",
            name="observation_count_positive",
        ),
        sa.CheckConstraint("state_version >= 1", name="state_version_positive"),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_student_skill_states_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "user_id",
            "taxonomy_version",
            "skill_id",
            name="pk_student_skill_states",
        ),
    )

    op.create_table(
        "skill_observation_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("submission_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("question_id", sa.Integer(), nullable=False),
        sa.Column("taxonomy_version", sa.String(length=64), nullable=False),
        sa.Column("skill_id", sa.String(length=128), nullable=False),
        sa.Column("observation_result", sa.String(length=16), nullable=False),
        sa.Column("source_type", sa.String(length=24), nullable=False),
        sa.Column("source_version", sa.String(length=64), nullable=False),
        sa.Column("evidence_grade", sa.String(length=32), nullable=True),
        sa.Column("phase2_candidate_id", sa.String(length=128), nullable=True),
        sa.Column("rule_id", sa.String(length=64), nullable=True),
        sa.Column("source_role", sa.String(length=32), nullable=True),
        sa.Column("logical_stage", sa.String(length=32), nullable=True),
        sa.Column("source_provenance", sa.String(length=32), nullable=True),
        sa.Column("assistance_level", sa.SmallInteger(), nullable=False),
        sa.Column("answer_revealed", sa.Boolean(), nullable=False),
        sa.Column("prior_mastery", sa.Float(), nullable=False),
        sa.Column("posterior_mastery", sa.Float(), nullable=False),
        sa.Column("next_prior", sa.Float(), nullable=False),
        sa.Column("bkt_parameter_version", sa.String(length=64), nullable=False),
        sa.Column("state_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "observation_result IN ('CORRECT', 'INCORRECT')",
            name="result_allowed",
        ),
        sa.CheckConstraint(
            "source_type IN ('QUESTION_QMATRIX', 'PHASE2_RULE')",
            name="source_allowed",
        ),
        sa.CheckConstraint(
            "assistance_level >= 1 AND assistance_level <= 4",
            name="assistance_level_bounds",
        ),
        sa.CheckConstraint(
            "prior_mastery >= 0 AND prior_mastery <= 1",
            name="prior_probability_bounds",
        ),
        sa.CheckConstraint(
            "posterior_mastery >= 0 AND posterior_mastery <= 1",
            name="posterior_probability_bounds",
        ),
        sa.CheckConstraint(
            "next_prior >= 0 AND next_prior <= 1",
            name="next_prior_probability_bounds",
        ),
        sa.ForeignKeyConstraint(
            ["question_id"],
            ["questions.id"],
            name="fk_skill_observation_events_question_id_questions",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["submission_id"],
            ["submissions.id"],
            name="fk_skill_observation_events_submission_id_submissions",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_skill_observation_events_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_skill_observation_events"),
        sa.UniqueConstraint(
            "submission_id",
            "taxonomy_version",
            "skill_id",
            name="uq_skill_observation_events_submission_taxonomy_skill",
        ),
    )
    op.create_index(
        "ix_skill_observation_events_user_skill_recent",
        "skill_observation_events",
        ["user_id", "taxonomy_version", "skill_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_skill_observation_events_user_skill_recent",
        table_name="skill_observation_events",
    )
    op.drop_table("skill_observation_events")
    op.drop_table("student_skill_states")
