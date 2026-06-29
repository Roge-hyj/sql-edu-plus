"""
认证相关 Schemas（邮箱验证码）

用于邮箱验证码的创建/返回结构；具体发送逻辑在 `routers/auth.py`。
"""

from datetime import datetime

from pydantic import BaseModel, EmailStr


class EmailCaptchaBase(BaseModel):
    email: EmailStr
    captcha: str


class EmailCaptchaCreate(EmailCaptchaBase):
    pass


class EmailCaptchaOut(EmailCaptchaBase):
    id: int
    used: bool
    created_at: datetime

    class Config:
        from_attributes = True


__all__ = ["EmailCaptchaBase", "EmailCaptchaCreate", "EmailCaptchaOut"]





