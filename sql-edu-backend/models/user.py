from .base import Base
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy import Integer, String, DateTime,func
from datetime import datetime
from passlib.context import CryptContext

# 1. 初始化加密上下文
# 使用 bcrypt_sha256 以支持超过 72 bytes 的口令输入（先 SHA256 再 bcrypt）。
# 同时保留 bcrypt 以兼容旧数据。
pwd_context = CryptContext(schemes=["bcrypt_sha256", "bcrypt"], deprecated="auto")

class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(50), nullable=False)
    email: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    # 角色：student / teacher，默认学生
    role: Mapped[str] = mapped_column(String(20), nullable=False, default="student")
    # 学生等级经验：累计经验值，用于计算等级（仅学生有意义，教师可忽略）
    total_experience: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # 数据库里存的是这个字段
    _password: Mapped[str] = mapped_column(String(200), nullable=False)

    # 2. 使用 property 伪装密码字段
    @property
    def password(self):
        raise AttributeError('密码不可读！') # 处于安全考虑，不允许直接查看加密后的字符串

    @password.setter
    def password(self, plain_password):
        # 当你执行 user.password = "123456" 时，自动触发加密存入 _password
        self._password = pwd_context.hash(plain_password)

    # 3. 验证密码的方法（以后登录用）
    def verify_password(self, plain_password):
        return pwd_context.verify(plain_password, self._password)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    # 当记录被修改时，onupdate 里的函数会自动运行，更新时间
    updated_at: Mapped[datetime] = mapped_column(
    DateTime, 
    default=datetime.utcnow, 
    )


