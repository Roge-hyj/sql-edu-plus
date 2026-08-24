"""add immutable Phase 4/5 teaching feedback audit

Revision ID: f6a7b8c9d0e1
Revises: f5a6b7c8d9e0
Create Date: 2026-08-24

Legacy submissions intentionally receive no fabricated audit row.  New
Phase4/5-enabled submissions create exactly one row in the same transaction as
their response snapshot and other Phase6 side effects.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f6a7b8c9d0e1"
down_revision: Union[str, Sequence[str], None] = "f5a6b7c8d9e0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "submission_teaching_audits",
        sa.Column("submission_id", sa.Integer(), nullable=False),
        sa.Column("audit_schema_version", sa.String(length=64), nullable=False),
        sa.Column("support_need", sa.Float(), nullable=True),
        sa.Column("recommended_support_level", sa.SmallInteger(), nullable=True),
        sa.Column("delivered_support_level", sa.SmallInteger(), nullable=False),
        sa.Column(
            "support_recommendation_applied",
            sa.Boolean(),
            nullable=False,
        ),
        sa.Column("recommendation_status", sa.String(length=24), nullable=False),
        sa.Column("support_policy_version", sa.String(length=64), nullable=True),
        sa.Column("action_policy_version", sa.String(length=64), nullable=False),
        sa.Column("feedback_policy_version", sa.String(length=64), nullable=False),
        sa.Column("generation_source", sa.String(length=64), nullable=False),
        sa.Column("feedback_status", sa.String(length=24), nullable=False),
        sa.Column("degradation_code", sa.String(length=64), nullable=True),
        sa.Column("answer_revealed", sa.Boolean(), nullable=False),
        sa.Column("target_candidate_id", sa.String(length=128), nullable=True),
        sa.Column("target_rule_id", sa.String(length=64), nullable=True),
        sa.Column("target_observation_id", sa.String(length=128), nullable=True),
        sa.Column("target_skill_id", sa.String(length=128), nullable=True),
        sa.Column("target_taxonomy_version", sa.String(length=64), nullable=True),
        sa.Column("target_logical_stage", sa.String(length=32), nullable=True),
        sa.Column("target_source_role", sa.String(length=32), nullable=True),
        sa.Column("target_evidence_grade", sa.String(length=32), nullable=True),
        sa.Column("feedback_sha256", sa.String(length=64), nullable=False),
        sa.Column("action_snapshot", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "support_need IS NULL OR "
            "(support_need >= 0 AND support_need <= 1)",
            name="support_need_bounds",
        ),
        sa.CheckConstraint(
            "recommended_support_level IS NULL OR "
            "(recommended_support_level >= 1 AND "
            "recommended_support_level <= 4)",
            name="recommended_level_bounds",
        ),
        sa.CheckConstraint(
            "delivered_support_level >= 1 AND delivered_support_level <= 4",
            name="delivered_level_bounds",
        ),
        sa.CheckConstraint(
            "recommendation_status IN "
            "('APPLIED', 'OVERRIDDEN', 'NOT_APPLICABLE')",
            name="recommendation_status_allowed",
        ),
        sa.CheckConstraint(
            "feedback_status IN ('PRIMARY', 'FALLBACK', 'BYPASS')",
            name="feedback_status_allowed",
        ),
        sa.CheckConstraint(
            "((recommended_support_level IS NULL AND support_need IS NULL) OR "
            "(recommended_support_level IS NOT NULL AND support_need IS NOT NULL))",
            name="recommendation_signal_pair",
        ),
        sa.CheckConstraint(
            "((recommendation_status = 'APPLIED' AND "
            "support_recommendation_applied IS TRUE AND "
            "recommended_support_level IS NOT NULL AND "
            "recommended_support_level = delivered_support_level) OR "
            "(recommendation_status = 'OVERRIDDEN' AND "
            "support_recommendation_applied IS FALSE AND "
            "recommended_support_level IS NOT NULL) OR "
            "(recommendation_status = 'NOT_APPLICABLE' AND "
            "support_recommendation_applied IS FALSE AND "
            "recommended_support_level IS NULL))",
            name="recommendation_consistency",
        ),
        sa.CheckConstraint(
            "((feedback_status = 'FALLBACK' AND "
            "degradation_code IS NOT NULL) OR "
            "(feedback_status IN ('PRIMARY', 'BYPASS') AND "
            "degradation_code IS NULL))",
            name="degradation_provenance",
        ),
        sa.CheckConstraint(
            "length(feedback_sha256) = 64",
            name="feedback_sha256_length",
        ),
        sa.ForeignKeyConstraint(
            ["submission_id"],
            ["submissions.id"],
            name="fk_submission_teaching_audits_submission_id_submissions",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "submission_id",
            name="pk_submission_teaching_audits",
        ),
    )


def downgrade() -> None:
    op.drop_table("submission_teaching_audits")
