from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from core.sql_dialect_resolver import normalize_sql_dialect
from core.sql_knowledge_points import get_knowledge_point_by_id
from core.phase3_skill_catalog import (
    ATOMIC_SKILL_TAXONOMY_VERSION,
    RULE_SKILL_CATALOG,
)
from models.question_skill import (
    SQL_KNOWLEDGE_TAXONOMY_VERSION,
    QuestionSkillProvenance,
    QuestionSkillRole,
)


_VERSIONED_IDENTIFIER_PATTERN = r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$"
_ATOMIC_SKILL_IDS = frozenset(item.skill_id for item in RULE_SKILL_CATALOG)


class QuestionSkillDeclaration(BaseModel):
    """Teacher-authored Q-matrix member.

    Provenance is intentionally absent: teacher-facing clients cannot claim
    that a row was AI-generated or reviewed inference.  The route fixes it to
    ``AUTHOR_DECLARED``.
    """

    model_config = ConfigDict(extra="forbid")

    skill_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=_VERSIONED_IDENTIFIER_PATTERN,
    )
    taxonomy_version: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        pattern=_VERSIONED_IDENTIFIER_PATTERN,
    )
    role: QuestionSkillRole = QuestionSkillRole.PRIMARY
    observable_on_correct: bool | None = None

    @field_validator("skill_id", "taxonomy_version", mode="before")
    @classmethod
    def strip_identifier(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @model_validator(mode="after")
    def default_observability_by_role(self) -> "QuestionSkillDeclaration":
        in_curriculum = get_knowledge_point_by_id(self.skill_id) is not None
        in_atomic = self.skill_id in _ATOMIC_SKILL_IDS
        if self.taxonomy_version is None:
            matched_versions = (
                int(in_curriculum),
                int(in_atomic),
            )
            if sum(matched_versions) != 1:
                raise ValueError("skill_id 无法唯一解析到冻结知识分类版本")
            self.taxonomy_version = (
                SQL_KNOWLEDGE_TAXONOMY_VERSION
                if in_curriculum
                else ATOMIC_SKILL_TAXONOMY_VERSION
            )
        elif self.taxonomy_version == SQL_KNOWLEDGE_TAXONOMY_VERSION:
            if not in_curriculum:
                raise ValueError("skill_id 不属于课程知识点分类版本")
        elif self.taxonomy_version == ATOMIC_SKILL_TAXONOMY_VERSION:
            if not in_atomic:
                raise ValueError("skill_id 不属于 Phase3 原子技能分类版本")
        else:
            raise ValueError("不支持的 taxonomy_version")
        if self.observable_on_correct is None:
            self.observable_on_correct = self.role is QuestionSkillRole.PRIMARY
        if (
            self.role is QuestionSkillRole.SUPPORTING
            and self.observable_on_correct
        ):
            raise ValueError("SUPPORTING 技能不能由一次正确提交直接证明")
        return self


class QuestionSkillOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    skill_id: str
    taxonomy_version: str
    role: QuestionSkillRole
    observable_on_correct: bool
    provenance: QuestionSkillProvenance


class QuestionBase(BaseModel):
    title: str
    content: str
    difficulty: int


class QuestionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    content: str
    title_en: str | None = None
    content_en: str | None = None
    title_zh_tw: str | None = None
    content_zh_tw: str | None = None
    correct_sql: str
    sql_dialect: str | None = None
    engine_version: str | None = None
    difficulty: int | None = None  # 留空则由 AI 根据题目内容与 SQL 自动判断；1～10
    schema_preview: str | None = None  # JSON：tables[{name,columns,rows}]，供学生查看
    required_output_columns: str | None = None  # 要求的结果列名或完整说明，供学生端显著展示
    # ``None``/省略：PUT 时保留现有映射；显式 []：清空映射。
    skills: list[QuestionSkillDeclaration] | None = Field(
        default=None,
        max_length=8,
    )

    @field_validator("difficulty")
    @classmethod
    def difficulty_range(cls, v: int | None) -> int | None:
        if v is not None and (v < 1 or v > 10):
            raise ValueError("难度必须在 1～10 之间")
        return v

    @field_validator("sql_dialect")
    @classmethod
    def sql_dialect_supported(cls, v: str | None) -> str | None:
        return normalize_sql_dialect(v)

    @field_validator("skills")
    @classmethod
    def deduplicate_skills(
        cls,
        value: list[QuestionSkillDeclaration] | None,
    ) -> list[QuestionSkillDeclaration] | None:
        if value is None:
            return None
        unique: list[QuestionSkillDeclaration] = []
        seen: dict[tuple[str, str], QuestionSkillDeclaration] = {}
        for item in value:
            key = (item.skill_id, item.taxonomy_version)
            previous = seen.get(key)
            if previous is None:
                seen[key] = item
                unique.append(item)
            elif previous != item:
                raise ValueError(
                    "同一 skill_id 与 taxonomy_version 不能有冲突声明"
                )
        if sum(item.role is QuestionSkillRole.PRIMARY for item in unique) > 3:
            raise ValueError("每道题最多声明 3 个 PRIMARY 技能")
        return unique


class QuestionOut(QuestionBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    correct_sql: str
    sql_dialect: str | None = None
    engine_version: str | None = None
    schema_preview: str | None = None  # JSON：tables[{name,columns,rows}]，供学生查看
    required_output_columns: str | None = None  # 要求的结果列名或完整说明，供学生端显著展示
    display_difficulty: float | None = None  # 动态计算 1～10，仅列表/详情返回时填充
    # 多语言题面（可选；未填写则前端回退到 title/content）
    title_en: str | None = None
    content_en: str | None = None
    title_zh_tw: str | None = None
    content_zh_tw: str | None = None
    skills: list[QuestionSkillOut] = Field(default_factory=list, max_length=8)


class QuestionPublicOut(QuestionBase):
    """Learner-facing question metadata.

    The reference SQL is deliberately absent from this schema.  Keeping a
    separate type (instead of relying on ``exclude`` at individual call
    sites) makes FastAPI's response validation a final defence against an ORM
    object or teacher DTO accidentally exposing the answer.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    sql_dialect: str | None = None
    engine_version: str | None = None
    schema_preview: str | None = None
    required_output_columns: str | None = None
    display_difficulty: float | None = None
    title_en: str | None = None
    content_en: str | None = None
    title_zh_tw: str | None = None
    content_zh_tw: str | None = None


class DifficultyFeedbackIn(BaseModel):
    rating: int  # 1～10


__all__ = [
    "QuestionBase",
    "QuestionCreate",
    "QuestionSkillDeclaration",
    "QuestionSkillOut",
    "QuestionOut",
    "QuestionPublicOut",
    "DifficultyFeedbackIn",
]
