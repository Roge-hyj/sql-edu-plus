"""
认证相关模型（EmailCaptcha）

用于邮箱验证码的发放与校验，常见于：
- 注册
- 登录/找回密码（如未来扩展）

说明：
- 本表只记录验证码与是否使用；有效期校验在 repository 层完成。
"""

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






