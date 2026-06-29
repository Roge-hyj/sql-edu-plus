"""
Pydantic Schemas 汇总入口

本模块集中导出路由层常用的请求/响应模型（schemas），避免在路由里写大量导入路径。
"""

from pydantic import BaseModel, Field
from typing import Annotated, Literal

class ResponseOut(BaseModel):
    """通用操作结果返回结构（用于仅返回 success/failure 的接口）。"""
    result: Annotated[Literal["success", "failure"], Field("success", description="操作结果")]
    detail: str | None = None  # 可选错误原因，便于前端提示

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




