"""
对话消息模型（ChatMessage）

本模块保存学生与 AI 教师的多轮对话历史（按 user_id + question_id 聚合）。
该历史会用于：
- 前端展示聊天记录
- 多轮对话时作为 LLM 上下文（通常只取最近 N 条）
"""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class ChatMessage(Base):
    """与 AI 教师的多轮对话消息（按用户+题目维度存储）。"""

    __tablename__ = "chat_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)

    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    question_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("questions.id", ondelete="CASCADE"), nullable=False, index=True
    )

    # "user" | "assistant" | "system"
    role: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )


__all__ = ["ChatMessage"]

