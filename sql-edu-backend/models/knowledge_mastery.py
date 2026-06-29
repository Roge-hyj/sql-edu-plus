"""
学习画像模型（KnowledgeMastery）

该表为每个用户、每个知识点维护一份 BKT（Bayesian Knowledge Tracing）状态：
- `p_mastery`: 当前掌握概率
- `p_transit/p_guess/p_slip`: BKT 参数（可按需调整/个性化）
- `total_attempts/correct_attempts/last_updated`: 追踪信息，便于分析与可视化（如能力雷达）
"""

from sqlalchemy import Column, Integer, String, Float, DateTime, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from datetime import datetime

from models.base import Base

class KnowledgeMastery(Base):
    """Per-user, per-knowledge-point Bayesian Knowledge Tracing state."""
    __tablename__ = "knowledge_mastery"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False, index=True
    )
    # The string ID matching the taxonomy in core/sql_knowledge_points.py
    # e.g., "group-by", "join-inner"
    knowledge_point_id: Mapped[str] = mapped_column(
        String(50), nullable=False, index=True
    )

    # BKT Current State: Overall probability of knowing this specific concept P(L_n)
    p_mastery: Mapped[float] = mapped_column(Float, default=0.1, nullable=False)

    # BKT Model parameters (customizable per-student-per-skill as they progress)
    # P(T): Transition probability (Probability of learning it after attempting)
    p_transit: Mapped[float] = mapped_column(Float, default=0.1, nullable=False)
    # P(G): Guess probability (Probability of getting it right if they don't know it)
    p_guess: Mapped[float] = mapped_column(Float, default=0.2, nullable=False)
    # P(S): Slip probability (Probability of getting it wrong if they do know it)
    p_slip: Mapped[float] = mapped_column(Float, default=0.1, nullable=False)

    # Tracking metadata
    total_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    correct_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_updated: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    __table_args__ = (
        UniqueConstraint("user_id", "knowledge_point_id", name="uq_user_kp"),
    )
