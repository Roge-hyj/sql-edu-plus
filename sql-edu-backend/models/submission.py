from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base


class Submission(Base):
    """学生对某道题目的 SQL 提交记录，是教学系统的核心行为数据。"""

    __tablename__ = "submissions"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "question_id",
            "attempt_id",
            name="uq_submissions_user_question_attempt",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)

    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    question_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("questions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Client-generated UUID identifying one button action / transport retry.
    # Existing rows remain NULL; every new /ai/check-sql request must provide it.
    attempt_id: Mapped[str | None] = mapped_column(
        String(36),
        nullable=True,
    )
    request_fingerprint: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
    )

    student_sql: Mapped[str] = mapped_column(Text, nullable=False)
    ai_hint: Mapped[str] = mapped_column(Text, nullable=True)
    is_correct: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # 实际交付的支架等级：1-最轻提示，2/3-递进提示，4-最高支架。
    # Phase 3 的推荐值在 Phase 4 真正执行前不能冒充这里的交付值。
    hint_level: Mapped[int] = mapped_column(SmallInteger, default=1, nullable=False)
    # Learner-safe response snapshot makes an idempotent replay return the
    # original verdict and teaching summary without re-running side effects.
    response_snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )

    # 关系字段，方便以后做联表查询（非必须，但有用）
    user = relationship("User", backref="submissions")
    question = relationship("Question", backref="submissions")


__all__ = ["Submission"]






