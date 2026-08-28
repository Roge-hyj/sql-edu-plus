"""Persistent, auditable Phase 3 skill-learning state."""

from datetime import datetime
from enum import Enum

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class SkillObservationResult(str, Enum):
    CORRECT = "CORRECT"
    INCORRECT = "INCORRECT"


class SkillObservationSource(str, Enum):
    QUESTION_QMATRIX = "QUESTION_QMATRIX"
    PHASE2_RULE = "PHASE2_RULE"


class StudentSkillState(Base):
    """Raw BKT state, keyed by user *and* taxonomy version.

    ``posterior_mastery`` and ``next_prior`` are deliberately distinct.  A
    display-only smoothed value does not belong in this table and cannot feed
    the next Bayesian update by accident.
    """

    __tablename__ = "student_skill_states"
    __table_args__ = (
        CheckConstraint(
            "posterior_mastery >= 0 AND posterior_mastery <= 1",
            name="posterior_probability_bounds",
        ),
        CheckConstraint(
            "next_prior >= 0 AND next_prior <= 1",
            name="next_prior_probability_bounds",
        ),
        CheckConstraint(
            "observation_count >= 1",
            name="observation_count_positive",
        ),
        CheckConstraint("state_version >= 1", name="state_version_positive"),
    )

    user_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    taxonomy_version: Mapped[str] = mapped_column(
        String(64),
        primary_key=True,
    )
    skill_id: Mapped[str] = mapped_column(String(128), primary_key=True)

    posterior_mastery: Mapped[float] = mapped_column(Float, nullable=False)
    next_prior: Mapped[float] = mapped_column(Float, nullable=False)
    observation_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
    )
    bkt_parameter_version: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )
    state_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )


class SkillObservationEvent(Base):
    """Immutable audit record for one trusted skill observation."""

    __tablename__ = "skill_observation_events"
    __table_args__ = (
        UniqueConstraint(
            "submission_id",
            "taxonomy_version",
            "skill_id",
            name="uq_skill_observation_events_submission_taxonomy_skill",
        ),
        CheckConstraint(
            "observation_result IN ('CORRECT', 'INCORRECT')",
            name="result_allowed",
        ),
        CheckConstraint(
            "source_type IN ('QUESTION_QMATRIX', 'PHASE2_RULE')",
            name="source_allowed",
        ),
        CheckConstraint(
            "assistance_level >= 1 AND assistance_level <= 4",
            name="assistance_level_bounds",
        ),
        CheckConstraint(
            "prior_mastery >= 0 AND prior_mastery <= 1",
            name="prior_probability_bounds",
        ),
        CheckConstraint(
            "posterior_mastery >= 0 AND posterior_mastery <= 1",
            name="posterior_probability_bounds",
        ),
        CheckConstraint(
            "next_prior >= 0 AND next_prior <= 1",
            name="next_prior_probability_bounds",
        ),
        Index(
            "ix_skill_observation_events_user_skill_recent",
            "user_id",
            "taxonomy_version",
            "skill_id",
            "created_at",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    submission_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("submissions.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    question_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("questions.id", ondelete="CASCADE"),
        nullable=False,
    )
    taxonomy_version: Mapped[str] = mapped_column(String(64), nullable=False)
    skill_id: Mapped[str] = mapped_column(String(128), nullable=False)
    observation_result: Mapped[str] = mapped_column(String(16), nullable=False)
    source_type: Mapped[str] = mapped_column(String(24), nullable=False)
    source_version: Mapped[str] = mapped_column(String(64), nullable=False)

    evidence_grade: Mapped[str | None] = mapped_column(String(32), nullable=True)
    phase2_candidate_id: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
    )
    rule_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_role: Mapped[str | None] = mapped_column(String(32), nullable=True)
    logical_stage: Mapped[str | None] = mapped_column(String(32), nullable=True)
    source_provenance: Mapped[str | None] = mapped_column(
        String(32),
        nullable=True,
    )

    assistance_level: Mapped[int] = mapped_column(
        SmallInteger,
        nullable=False,
        default=1,
    )
    answer_revealed: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
    )
    prior_mastery: Mapped[float] = mapped_column(Float, nullable=False)
    posterior_mastery: Mapped[float] = mapped_column(Float, nullable=False)
    next_prior: Mapped[float] = mapped_column(Float, nullable=False)
    bkt_parameter_version: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )
    state_version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
    )


class Phase3BehaviorEventKind(str, Enum):
    """Non-semantic submission outcomes kept outside the BKT event stream."""

    SYNTAX_ERROR = "SYNTAX_ERROR"
    PLATFORM_ERROR = "PLATFORM_ERROR"
    SAFETY_BLOCKED = "SAFETY_BLOCKED"
    UNDECIDED = "UNDECIDED"


class Phase3BehaviorEvent(Base):
    """Audited behavioral event that must not become a skill observation.

    Syntax, platform, safety, and undecided outcomes are useful for explaining
    the bounded support proxy, but none of them proves or disproves an atomic
    skill.  Keeping them in a separate table prevents database constraints and
    repository callers from accidentally treating them as BKT evidence.
    """

    __tablename__ = "phase3_behavior_events"
    __table_args__ = (
        UniqueConstraint(
            "submission_id",
            name="uq_phase3_behavior_events_submission",
        ),
        CheckConstraint(
            "event_kind IN ('SYNTAX_ERROR', 'PLATFORM_ERROR', "
            "'SAFETY_BLOCKED', 'UNDECIDED')",
            name="event_kind_allowed",
        ),
        Index(
            "ix_phase3_behavior_events_user_recent",
            "user_id",
            "created_at",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    submission_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("submissions.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    question_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("questions.id", ondelete="CASCADE"),
        nullable=False,
    )
    event_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
    )


__all__ = [
    "Phase3BehaviorEvent",
    "Phase3BehaviorEventKind",
    "SkillObservationEvent",
    "SkillObservationResult",
    "SkillObservationSource",
    "StudentSkillState",
]
