from datetime import datetime

from pydantic import BaseModel


class SubmissionBase(BaseModel):
    user_id: int
    question_id: int
    student_sql: str


class SubmissionCreate(SubmissionBase):
    ai_hint: str | None = None
    is_correct: bool = False
    hint_level: int = 1


class SubmissionOut(SubmissionBase):
    id: int
    ai_hint: str | None
    is_correct: bool
    hint_level: int
    created_at: datetime

    class Config:
        from_attributes = True


__all__ = ["SubmissionBase", "SubmissionCreate", "SubmissionOut"]





