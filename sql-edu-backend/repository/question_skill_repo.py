"""Repository for authoritative Question-Skill Q-matrix rows."""

from dataclasses import dataclass
import re
from collections.abc import Sequence

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.sql_knowledge_points import get_knowledge_point_by_id
from core.phase3_skill_catalog import (
    ATOMIC_SKILL_TAXONOMY_VERSION,
    RULE_SKILL_CATALOG,
)
from models.question_skill import (
    SQL_KNOWLEDGE_TAXONOMY_VERSION,
    QuestionSkill,
    QuestionSkillProvenance,
    QuestionSkillRole,
)


_IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
_MAX_SKILLS_PER_QUESTION = 8
_ATOMIC_SKILL_IDS = frozenset(item.skill_id for item in RULE_SKILL_CATALOG)


@dataclass(frozen=True, slots=True)
class QuestionSkillSpec:
    skill_id: str
    taxonomy_version: str = SQL_KNOWLEDGE_TAXONOMY_VERSION
    role: QuestionSkillRole = QuestionSkillRole.PRIMARY
    observable_on_correct: bool = True


def _validate_specs(skills: Sequence[QuestionSkillSpec]) -> None:
    if len(skills) > _MAX_SKILLS_PER_QUESTION:
        raise ValueError("每道题最多声明 8 个技能")
    seen: set[tuple[str, str]] = set()
    primary_count = 0
    for item in skills:
        if not isinstance(item, QuestionSkillSpec):
            raise TypeError("skills must contain QuestionSkillSpec values")
        try:
            role = QuestionSkillRole(item.role)
        except (TypeError, ValueError) as exc:
            raise ValueError("role 必须是 PRIMARY 或 SUPPORTING") from exc
        if type(item.observable_on_correct) is not bool:
            raise TypeError("observable_on_correct 必须是 bool")
        if (
            len(item.skill_id) > 128
            or not _IDENTIFIER_RE.fullmatch(item.skill_id)
        ):
            raise ValueError("skill_id 格式非法")
        if (
            len(item.taxonomy_version) > 64
            or not _IDENTIFIER_RE.fullmatch(item.taxonomy_version)
        ):
            raise ValueError("taxonomy_version 格式非法")
        if item.taxonomy_version == SQL_KNOWLEDGE_TAXONOMY_VERSION:
            if get_knowledge_point_by_id(item.skill_id) is None:
                raise ValueError("skill_id 不属于课程知识点分类版本")
        elif item.taxonomy_version == ATOMIC_SKILL_TAXONOMY_VERSION:
            if item.skill_id not in _ATOMIC_SKILL_IDS:
                raise ValueError("skill_id 不属于 Phase3 原子技能分类版本")
        else:
            raise ValueError("不支持的 taxonomy_version")
        key = (item.skill_id, item.taxonomy_version)
        if key in seen:
            raise ValueError("同一题目的技能与 taxonomy 版本不得重复")
        seen.add(key)
        if role is QuestionSkillRole.PRIMARY:
            primary_count += 1
        elif item.observable_on_correct:
            raise ValueError("SUPPORTING 技能不能由一次正确提交直接证明")
    if primary_count > 3:
        raise ValueError("每道题最多声明 3 个 PRIMARY 技能")


class QuestionSkillRepository:
    """Q-matrix access with transaction ownership left to the route/service."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def list_by_question_id(self, question_id: int) -> list[QuestionSkill]:
        stmt = (
            select(QuestionSkill)
            .where(QuestionSkill.question_id == question_id)
            .order_by(QuestionSkill.id)
        )
        result = await self.session.scalars(stmt)
        return list(result.all())

    async def replace_for_question(
        self,
        question_id: int,
        skills: Sequence[QuestionSkillSpec],
        *,
        provenance: QuestionSkillProvenance,
    ) -> list[QuestionSkill]:
        """Atomically replace mappings inside the caller's current transaction.

        This method deliberately never commits.  The question mutation and its
        Q-matrix must succeed or roll back as one unit.
        """

        try:
            provenance_value = QuestionSkillProvenance(provenance).value
        except (TypeError, ValueError) as exc:
            raise ValueError("provenance 必须是受支持的 Q-matrix 来源") from exc
        _validate_specs(skills)
        await self.session.execute(
            delete(QuestionSkill).where(QuestionSkill.question_id == question_id)
        )
        rows = [
            QuestionSkill(
                question_id=question_id,
                skill_id=item.skill_id,
                taxonomy_version=item.taxonomy_version,
                role=QuestionSkillRole(item.role).value,
                observable_on_correct=item.observable_on_correct,
                provenance=provenance_value,
            )
            for item in skills
        ]
        self.session.add_all(rows)
        await self.session.flush()
        return rows

    async def add_ai_generated_primary(
        self,
        question_id: int,
        skill_id: str,
        *,
        taxonomy_version: str = SQL_KNOWLEDGE_TAXONOMY_VERSION,
    ) -> QuestionSkill:
        rows = await self.replace_for_question(
            question_id,
            [
                QuestionSkillSpec(
                    skill_id=skill_id,
                    taxonomy_version=taxonomy_version,
                    role=QuestionSkillRole.PRIMARY,
                    observable_on_correct=True,
                )
            ],
            provenance=QuestionSkillProvenance.GENERATED,
        )
        return rows[0]


__all__ = ["QuestionSkillRepository", "QuestionSkillSpec"]
