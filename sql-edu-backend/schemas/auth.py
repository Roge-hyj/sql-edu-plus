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





