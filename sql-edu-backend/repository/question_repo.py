from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from models.question import Question


class QuestionRepository:
    """题目数据访问层，负责查询题目信息。"""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_by_id(self, question_id: int) -> Question | None:
        """根据 ID 查询题目。

        :param question_id: 题目 ID
        :return: Question 对象，如果不存在则返回 None
        """
        stmt = select(Question).where(Question.id == question_id)
        question = await self.session.scalar(stmt)
        return question

    async def get_all(self, skip: int = 0, limit: int = 100) -> list[Question]:
        """查询所有题目（分页），按 ID 倒序（新题在前）。

        :param skip: 跳过数量
        :param limit: 限制数量
        :return: 题目列表
        """
        stmt = select(Question).order_by(Question.id.desc()).offset(skip).limit(limit)
        result = await self.session.scalars(stmt)
        return list(result.all())


__all__ = ["QuestionRepository"]
