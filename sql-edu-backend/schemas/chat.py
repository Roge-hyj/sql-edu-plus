from datetime import datetime
from pydantic import BaseModel, Field
from typing import Annotated, Literal


ChatRole = Literal["system", "user", "assistant"]


class ChatMessageOut(BaseModel):
    id: int
    role: ChatRole
    content: str
    created_at: datetime

    class Config:
        from_attributes = True


class ChatSendIn(BaseModel):
    question_id: int
    message: Annotated[str, Field(min_length=1, max_length=2000)]
    language: str = "zh-CN"  # 回答语言：zh-CN(简体中文)、en(英文)、zh-TW(繁体中文)


class ChatSendOut(BaseModel):
    reply: str


__all__ = ["ChatMessageOut", "ChatSendIn", "ChatSendOut", "ChatRole"]

