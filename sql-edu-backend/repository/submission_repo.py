from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from models.submission import Submission
from schemas.submission import SubmissionCreate


class SubmissionRepository:
    """提交记录数据访问层，负责创建和查询提交记录。"""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def create(self, submission_data: SubmissionCreate) -> Submission:
        """创建一条提交记录。

        :param submission_data: 提交数据 Schema
        :return: 创建的 Submission 对象
        """
        submission = Submission(
            user_id=submission_data.user_id,
            question_id=submission_data.question_id,
            student_sql=submission_data.student_sql,
            ai_hint=submission_data.ai_hint,
            is_correct=submission_data.is_correct,
            hint_level=submission_data.hint_level,
        )
        self.session.add(submission)
        await self.session.flush()  # 刷新以获取 ID
        return submission

    async def get_correct_count(self, user_id: int, question_id: int) -> int:
        """统计该用户在该题目上已有的正确提交次数（用于判断是否首次正确、是否发经验）。"""
        stmt = (
            select(func.count(Submission.id))
            .where(Submission.user_id == user_id)
            .where(Submission.question_id == question_id)
            .where(Submission.is_correct == True)
        )
        count = await self.session.scalar(stmt)
        return count or 0

    async def get_failure_count(
        self, user_id: int, question_id: int
    ) -> int:
        """统计该用户在该题目上的失败次数（is_correct=False 的记录数）。

        :param user_id: 用户 ID
        :param question_id: 题目 ID
        :return: 失败次数
        """
        stmt = (
            select(func.count(Submission.id))
            .where(Submission.user_id == user_id)
            .where(Submission.question_id == question_id)
            .where(Submission.is_correct == False)
        )
        count = await self.session.scalar(stmt)
        return count or 0

    async def get_by_id(self, submission_id: int) -> Submission | None:
        """根据 ID 查询提交记录。

        :param submission_id: 提交记录 ID
        :return: Submission 对象，如果不存在则返回 None
        """
        stmt = select(Submission).where(Submission.id == submission_id)
        submission = await self.session.scalar(stmt)
        return submission

    async def get_user_submissions(
        self, user_id: int, question_id: int | None = None, limit: int = 100
    ) -> list[Submission]:
        """查询用户的提交记录。

        :param user_id: 用户 ID
        :param question_id: 题目 ID（可选，如果提供则只查询该题目的提交）
        :param limit: 限制数量
        :return: 提交记录列表
        """
        stmt = select(Submission).where(Submission.user_id == user_id)
        if question_id is not None:
            stmt = stmt.where(Submission.question_id == question_id)
        stmt = stmt.order_by(Submission.created_at.desc()).limit(limit)
        result = await self.session.scalars(stmt)
        return list(result.all())

    async def get_user_overall_stats(self, user_id: int) -> dict:
        """统计用户在所有题目上的整体表现，用于动态调整支架。

        :param user_id: 用户 ID
        :return: {"total": int, "correct": int, "success_rate": float}
        """
        total_stmt = select(func.count(Submission.id)).where(Submission.user_id == user_id)
        total = await self.session.scalar(total_stmt) or 0
        if total == 0:
            return {"total": 0, "correct": 0, "success_rate": 0.5}

        correct_stmt = (
            select(func.count(Submission.id))
            .where(Submission.user_id == user_id)
            .where(Submission.is_correct == True)
        )
        correct = await self.session.scalar(correct_stmt) or 0
        success_rate = correct / total
        return {"total": total, "correct": correct, "success_rate": success_rate}

    async def get_question_submission_stats(self, question_id: int) -> dict:
        """统计某题的全用户提交情况：总提交数、正确提交数。"""
        total_stmt = select(func.count(Submission.id)).where(Submission.question_id == question_id)
        total = await self.session.scalar(total_stmt) or 0
        correct_stmt = (
            select(func.count(Submission.id))
            .where(Submission.question_id == question_id)
            .where(Submission.is_correct == True)
        )
        correct = await self.session.scalar(correct_stmt) or 0
        return {"total_submissions": total, "correct_submissions": correct}

    async def get_submission_stats_by_question_ids(
        self, question_ids: list[int]
    ) -> dict[int, dict]:
        """批量查询多题的提交统计。返回 { question_id: { total_submissions, correct_submissions } }"""
        if not question_ids:
            return {}
        from sqlalchemy import case
        total_stmt = (
            select(Submission.question_id, func.count(Submission.id).label("total"))
            .where(Submission.question_id.in_(question_ids))
            .group_by(Submission.question_id)
        )
        total_rows = (await self.session.execute(total_stmt)).all()
        correct_stmt = (
            select(Submission.question_id, func.count(Submission.id).label("correct"))
            .where(Submission.question_id.in_(question_ids))
            .where(Submission.is_correct == True)
            .group_by(Submission.question_id)
        )
        correct_rows = (await self.session.execute(correct_stmt)).all()
        total_map = {r.question_id: r.total for r in total_rows}
        correct_map = {r.question_id: r.correct for r in correct_rows}
        return {
            qid: {
                "total_submissions": total_map.get(qid, 0),
                "correct_submissions": correct_map.get(qid, 0),
            }
            for qid in question_ids
        }


__all__ = ["SubmissionRepository"]
