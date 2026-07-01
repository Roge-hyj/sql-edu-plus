from sqlalchemy import Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class Question(Base):
    """练习题模型，用于存储 SQL 题目与标准答案。"""

    __tablename__ = "questions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)  # 题目描述
    # 多语言题面（可选；未填写则前端回退到 title/content）
    title_en: Mapped[str | None] = mapped_column(String(200), nullable=True)
    content_en: Mapped[str | None] = mapped_column(Text, nullable=True)
    title_zh_tw: Mapped[str | None] = mapped_column(String(200), nullable=True)
    content_zh_tw: Mapped[str | None] = mapped_column(Text, nullable=True)
    difficulty: Mapped[int] = mapped_column(Integer, default=1, nullable=False)  # 教师设定难度 1～10，作为基础
    correct_sql: Mapped[str] = mapped_column(Text, nullable=False)
    # 限时挑战可选时长（秒），为空则可由前端根据难度推算
    time_limit_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 表结构预览（JSON：tables[{name,columns,rows}]），供学生查看列名与示例数据
    schema_preview: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 要求的结果列名（如「order_id, user_id, order_amount, cumulative_amount」或完整说明），供学生端显著展示，避免列名不规范错误
    required_output_columns: Mapped[str | None] = mapped_column(Text, nullable=True)


__all__ = ["Question"]






