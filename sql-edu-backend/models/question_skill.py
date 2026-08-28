"""Authoritative question-to-skill assessment mappings.

The rows in this table are assessment design metadata, not facts inferred from
the reference SQL at submission time.  Phase 3 may therefore use an
``observable_on_correct`` primary mapping as a positive learning observation.
"""

from enum import Enum

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base


SQL_KNOWLEDGE_TAXONOMY_VERSION = "sql_knowledge_points.v1"


class QuestionSkillRole(str, Enum):
    PRIMARY = "PRIMARY"
    SUPPORTING = "SUPPORTING"


class QuestionSkillProvenance(str, Enum):
    # The short names are the v1 contract vocabulary.  The longer members are
    # source-compatible aliases kept for callers that used the first draft of
    # the Q-matrix API; storage uses the canonical short values below.
    AUTHOR_DECLARED = "AUTHOR_DECLARED"
    GENERATED = "GENERATED"
    INFERRED = "INFERRED"
    AI_GENERATED = "GENERATED"
    INFERRED_REVIEWED = "INFERRED"


class QuestionSkill(Base):
    """A versioned, auditable member of a question's Q-matrix."""

    __tablename__ = "question_skills"
    __table_args__ = (
        UniqueConstraint(
            "question_id",
            "skill_id",
            "taxonomy_version",
            name="uq_question_skills_question_skill_taxonomy",
        ),
        CheckConstraint(
            "skill_role IN ('PRIMARY', 'SUPPORTING')",
            name="role_allowed",
        ),
        CheckConstraint(
            "provenance IN "
            "('AUTHOR_DECLARED', 'GENERATED', 'INFERRED')",
            name="provenance_allowed",
        ),
        CheckConstraint(
            "skill_role = 'PRIMARY' OR NOT observable_on_correct",
            name="supporting_not_observable",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    question_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("questions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    skill_id: Mapped[str] = mapped_column(String(128), nullable=False)
    taxonomy_version: Mapped[str] = mapped_column(String(64), nullable=False)
    role: Mapped[str] = mapped_column("skill_role", String(16), nullable=False)
    observable_on_correct: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
    )
    provenance: Mapped[str] = mapped_column(String(24), nullable=False)

    question = relationship("Question", back_populates="skill_mappings")


__all__ = [
    "QuestionSkill",
    "QuestionSkillRole",
    "QuestionSkillProvenance",
    "SQL_KNOWLEDGE_TAXONOMY_VERSION",
]
