from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class EmailCaptcha(Base):
    """邮箱验证码表，用于登录/注册/找回密码等场景。"""

    __tablename__ = "email_captchas"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True,autoincrement=True)
    email: Mapped[str] = mapped_column(String(100), index=True, nullable=False,unique=True)
    captcha: Mapped[str] = mapped_column(String(10), nullable=False)
    used: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )


__all__ = ["EmailCaptcha"]






