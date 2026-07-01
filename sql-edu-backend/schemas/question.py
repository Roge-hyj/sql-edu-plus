from pydantic import BaseModel, field_validator


class QuestionBase(BaseModel):
    title: str
    content: str
    difficulty: int


class QuestionCreate(BaseModel):
    title: str
    content: str
    title_en: str | None = None
    content_en: str | None = None
    title_zh_tw: str | None = None
    content_zh_tw: str | None = None
    correct_sql: str
    difficulty: int | None = None  # 留空则由 AI 根据题目内容与 SQL 自动判断；1～10
    time_limit_seconds: int | None = None
    schema_preview: str | None = None  # JSON：tables[{name,columns,rows}]，供学生查看
    required_output_columns: str | None = None  # 要求的结果列名或完整说明，供学生端显著展示

    @field_validator("difficulty")
    @classmethod
    def difficulty_range(cls, v: int | None) -> int | None:
        if v is not None and (v < 1 or v > 10):
            raise ValueError("难度必须在 1～10 之间")
        return v


class QuestionOut(QuestionBase):
    id: int
    correct_sql: str
    time_limit_seconds: int | None = None
    schema_preview: str | None = None  # JSON：tables[{name,columns,rows}]，供学生查看
    required_output_columns: str | None = None  # 要求的结果列名或完整说明，供学生端显著展示
    display_difficulty: float | None = None  # 动态计算 1～10，仅列表/详情返回时填充
    suggested_time_seconds: int | None = None  # 限时挑战建议秒数，仅列表/详情返回时填充
    # 多语言题面（可选；未填写则前端回退到 title/content）
    title_en: str | None = None
    content_en: str | None = None
    title_zh_tw: str | None = None
    content_zh_tw: str | None = None

    class Config:
        from_attributes = True


class DifficultyFeedbackIn(BaseModel):
    rating: int  # 1～10


__all__ = ["QuestionBase", "QuestionCreate", "QuestionOut", "DifficultyFeedbackIn"]





