from pydantic import BaseModel,Field
from typing import Annotated,Literal

class ResponseOut(BaseModel):
    #用于一些视图函数，只要返回操作结果的模型
    result:Annotated[Literal["success","failure"], Field("success",description="操作的结果!")]
    # 加上这一行，允许返回具体的错误原因，给前端看
    detail: str | None = None

"""Pydantic 模型入口。"""

from .user import RegisterIn, UserCreateSchema
from schemas.question import QuestionBase, QuestionCreate, QuestionOut
from schemas.submission import SubmissionBase, SubmissionCreate, SubmissionOut
from schemas.auth import EmailCaptchaBase, EmailCaptchaCreate, EmailCaptchaOut
from schemas.chat import ChatMessageOut, ChatSendIn, ChatSendOut

__all__ = [
    "RegisterIn",
    "UserCreateSchema",
    "QuestionBase",
    "QuestionCreate",
    "QuestionOut",
    "SubmissionBase",
    "SubmissionCreate",
    "SubmissionOut",
    "EmailCaptchaBase",
    "EmailCaptchaCreate",
    "EmailCaptchaOut",
    "ChatMessageOut",
    "ChatSendIn",
    "ChatSendOut",
]




