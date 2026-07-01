from pydantic import BaseModel, EmailStr, Field, model_validator
from typing import Annotated, Literal
from datetime import datetime

Usernamestr = Annotated[str, Field(min_length=3, max_length=20, description="用户名，3-20 字符")]
# 说明：这里密码最大长度放宽到 72 字符，以兼容 openssl rand -hex 16/32 等强密码，
# 同时仍然符合 bcrypt 72 bytes 的安全上限（ASCII 场景下一字节一个字符）。
Passwordstr = Annotated[str, Field(min_length=6, max_length=72, description="密码，6-72 字符")]
RoleStr = Annotated[str, Field(min_length=3, max_length=20, description="用户角色，student 或 teacher")]


class RegisterIn(BaseModel):
    email: EmailStr
    username: Usernamestr
    password: Passwordstr
    confirm_password: Passwordstr
    captcha: Annotated[str, Field(min_length=6, max_length=6, description="邮箱验证码，6 位数字")]
    invite_code: str | None = None

    @model_validator(mode="after")
    def check_passwords_match(self):
        if self.password != self.confirm_password:
            raise ValueError("密码和确认密码不匹配")
        return self
    
class UserCreateSchema(BaseModel):
    email: EmailStr
    username: Usernamestr
    password: Passwordstr
    role: RoleStr = "student"

# 1. 登录请求：前端传用户名/邮箱和密码
class LoginIn(BaseModel):
    email: Annotated[str, Field(..., description="用户名或邮箱")]  # 虽然字段名是email，但实际可以是用户名或邮箱
    password: Passwordstr


class UserSchema(BaseModel):
    id: Annotated[int, Field(...)]
    email: EmailStr
    username: Usernamestr
    role: Literal["student", "teacher"] = "student"
    # 等级系统（学生端展示，教师可为 1/0/100）
    level: int = 1
    experience_in_level: int = 0
    xp_to_next_level: int = 100

# 2. 登录响应：后端回传双 Token
# 注意：这跟 core/auth.py 里 encode_login_token 返回的字典结构必须一致
class LoginOut(BaseModel):
    user:UserSchema
    token:str  # Access Token
    refresh_token:str  # Refresh Token，用于刷新 Access Token


class UserBase(BaseModel):
    username: str
    email: EmailStr


class UserCreate(UserBase):
    password: str


class UserOut(UserBase):
    id: int
    created_at: datetime

    class Config:
        from_attributes = True


class DeleteAccountIn(BaseModel):
    """注销账户请求，需要密码确认。"""
    password: Passwordstr


class ChangePasswordIn(BaseModel):
    """修改密码请求。"""
    old_password: Passwordstr
    new_password: Passwordstr
    confirm_password: Passwordstr

    @model_validator(mode="after")
    def check_passwords_match(self):
        if self.new_password != self.confirm_password:
            raise ValueError("新密码和确认密码不匹配")
        if self.old_password == self.new_password:
            raise ValueError("新密码不能与旧密码相同")
        return self


class UserProfileUpdate(BaseModel):
    """更新用户信息请求。"""
    username: Usernamestr | None = None


__all__ = [
    "UserBase", "UserCreate", "UserOut", "RegisterIn", "UserCreateSchema",
    "LoginIn", "UserSchema", "LoginOut", "DeleteAccountIn", "ChangePasswordIn",
    "UserProfileUpdate"
]





