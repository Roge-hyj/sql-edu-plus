from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, SmallInteger, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base


class Submission(Base):
    """学生对某道题目的 SQL 提交记录，是教学系统的核心行为数据。"""

    __tablename__ = "submissions"

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

    student_sql: Mapped[str] = mapped_column(Text, nullable=False)
    ai_hint: Mapped[str] = mapped_column(Text, nullable=True)
    is_correct: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # 1-低支架, 2-中支架, 3-高支架
    hint_level: Mapped[int] = mapped_column(SmallInteger, default=1, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )

    # 关系字段，方便以后做联表查询（非必须，但有用）
    user = relationship("User", backref="submissions")
    question = relationship("Question", backref="submissions")


__all__ = ["Submission"]






