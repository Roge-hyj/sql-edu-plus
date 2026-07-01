from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, delete

from models.chat import ChatMessage


class ChatRepository:
    """对话消息数据访问层。"""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def add_message(self, user_id: int, question_id: int, role: str, content: str) -> ChatMessage:
        msg = ChatMessage(user_id=user_id, question_id=question_id, role=role, content=content)
        self.session.add(msg)
        await self.session.flush()
        return msg

    async def list_messages(self, user_id: int, question_id: int, limit: int = 50) -> list[ChatMessage]:
        stmt = (
            select(ChatMessage)
            .where(ChatMessage.user_id == user_id)
            .where(ChatMessage.question_id == question_id)
            .order_by(ChatMessage.created_at.asc())
            .limit(limit)
        )
        result = await self.session.scalars(stmt)
        return list(result.all())

    async def count_messages_for_user_question(self, user_id: int, question_id: int) -> int:
        """统计该用户在该题目下的对话条数（用于经验投入度）。"""
        stmt = (
            select(func.count(ChatMessage.id))
            .where(ChatMessage.user_id == user_id)
            .where(ChatMessage.question_id == question_id)
        )
        return await self.session.scalar(stmt) or 0

    async def count_messages_by_question(self, question_id: int) -> int:
        """统计某题下所有用户的对话条数（客观数据，用于难度评估）。"""
        stmt = select(func.count(ChatMessage.id)).where(ChatMessage.question_id == question_id)
        return await self.session.scalar(stmt) or 0

    async def count_messages_by_question_ids(self, question_ids: list[int]) -> dict[int, int]:
        """批量统计多题的对话条数。返回 { question_id: count }"""
        if not question_ids:
            return {}
        stmt = (
            select(ChatMessage.question_id, func.count(ChatMessage.id).label("cnt"))
            .where(ChatMessage.question_id.in_(question_ids))
            .group_by(ChatMessage.question_id)
        )
        rows = (await self.session.execute(stmt)).all()
        return {r.question_id: r.cnt for r in rows}

    async def delete_messages_by_user_question(self, user_id: int, question_id: int) -> int:
        """删除该用户在该题目下的所有对话消息，返回删除条数。"""
        stmt = (
            delete(ChatMessage)
            .where(ChatMessage.user_id == user_id)
            .where(ChatMessage.question_id == question_id)
        )
        result = await self.session.execute(stmt)
        return result.rowcount or 0


__all__ = ["ChatRepository"]

