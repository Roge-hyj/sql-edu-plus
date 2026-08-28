"""Immutable Phase 4/5 teaching-delivery audit for one submission."""

from datetime import datetime
from enum import Enum

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    SmallInteger,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base


SUBMISSION_TEACHING_AUDIT_SCHEMA_VERSION = (
    "phase6.submission_teaching_audit.v1"
)


class SupportRecommendationStatus(str, Enum):
    """How the Phase 3 support recommendation reached Phase 5."""

    APPLIED = "APPLIED"
    OVERRIDDEN = "OVERRIDDEN"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class TeachingFeedbackStatus(str, Enum):
    """Outcome of the learner-facing feedback renderer."""

    PRIMARY = "PRIMARY"
    FALLBACK = "FALLBACK"
    BYPASS = "BYPASS"


class SubmissionTeachingAudit(Base):
    """One immutable decision-and-delivery record per ``Submission``.

    The learner-facing text remains on ``Submission.ai_hint``.  This row binds
    that text to the Phase 3 recommendation, Phase 4 action decision, Phase 5
    renderer, and the support level that was actually delivered.
    """

    __tablename__ = "submission_teaching_audits"
    __table_args__ = (
        CheckConstraint(
            "support_need IS NULL OR "
            "(support_need >= 0 AND support_need <= 1)",
            name="support_need_bounds",
        ),
        CheckConstraint(
            "recommended_support_level IS NULL OR "
            "(recommended_support_level >= 1 AND "
            "recommended_support_level <= 4)",
            name="recommended_level_bounds",
        ),
        CheckConstraint(
            "delivered_support_level >= 1 AND delivered_support_level <= 4",
            name="delivered_level_bounds",
        ),
        CheckConstraint(
            "recommendation_status IN "
            "('APPLIED', 'OVERRIDDEN', 'NOT_APPLICABLE')",
            name="recommendation_status_allowed",
        ),
        CheckConstraint(
            "feedback_status IN "
            "('PRIMARY', 'FALLBACK', 'BYPASS')",
            name="feedback_status_allowed",
        ),
        CheckConstraint(
            "((recommended_support_level IS NULL AND support_need IS NULL) OR "
            "(recommended_support_level IS NOT NULL AND support_need IS NOT NULL))",
            name="recommendation_signal_pair",
        ),
        CheckConstraint(
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
        CheckConstraint(
            "((feedback_status = 'FALLBACK' AND "
            "degradation_code IS NOT NULL) OR "
            "(feedback_status IN ('PRIMARY', 'BYPASS') AND "
            "degradation_code IS NULL))",
            name="degradation_provenance",
        ),
        CheckConstraint(
            "length(feedback_sha256) = 64",
            name="feedback_sha256_length",
        ),
    )

    # The shared primary/foreign key enforces at most one audit row for each
    # idempotent submission without duplicating attempt/user/question identity.
    submission_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("submissions.id", ondelete="CASCADE"),
        primary_key=True,
    )

    audit_schema_version: Mapped[str] = mapped_column(String(64), nullable=False)

    support_need: Mapped[float | None] = mapped_column(Float, nullable=True)
    recommended_support_level: Mapped[int | None] = mapped_column(
        SmallInteger,
        nullable=True,
    )
    delivered_support_level: Mapped[int] = mapped_column(
        SmallInteger,
        nullable=False,
    )
    support_recommendation_applied: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
    )
    recommendation_status: Mapped[str] = mapped_column(String(24), nullable=False)

    # Versions belong to the policy that produced each stage, not to the
    # currently deployed policy.  Replays therefore retain historical lineage.
    support_policy_version: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
    )
    action_policy_version: Mapped[str] = mapped_column(String(64), nullable=False)
    feedback_policy_version: Mapped[str] = mapped_column(String(64), nullable=False)

    generation_source: Mapped[str] = mapped_column(String(64), nullable=False)
    feedback_status: Mapped[str] = mapped_column(String(24), nullable=False)
    degradation_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    answer_revealed: Mapped[bool] = mapped_column(Boolean, nullable=False)

    # Internal causal target lineage.  These fields and action_snapshot must
    # never be copied into a learner-facing response without sanitization.
    target_candidate_id: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
    )
    target_rule_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    target_observation_id: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
    )
    target_skill_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    target_taxonomy_version: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
    )
    target_logical_stage: Mapped[str | None] = mapped_column(
        String(32),
        nullable=True,
    )
    target_source_role: Mapped[str | None] = mapped_column(
        String(32),
        nullable=True,
    )
    target_evidence_grade: Mapped[str | None] = mapped_column(
        String(32),
        nullable=True,
    )

    feedback_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    action_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False,
    )

    submission = relationship("Submission")


__all__ = [
    "SUBMISSION_TEACHING_AUDIT_SCHEMA_VERSION",
    "SubmissionTeachingAudit",
    "SupportRecommendationStatus",
    "TeachingFeedbackStatus",
]
