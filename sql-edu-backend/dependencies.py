from core.mail import create_mail_instance
from fastapi_mail import FastMail
from fastapi import Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession 
from models import AsyncSessionFactory
from repository.user_repo import UserRepository
from core.auth import AuthHandler

async def get_session() -> AsyncSession:
    # 每次只拿一个干净的工人
    async with AsyncSessionFactory() as session:
        yield session

async def get_mail()-> FastMail:
    """FastAPI 依赖，提供邮件发送实例。"""
    return create_mail_instance()

# 教师权限检查依赖注入
async def require_teacher(
    user_id: int = Depends(AuthHandler().auth_access_dependency),
    session: AsyncSession = Depends(get_session),
):
    """要求用户必须是教师角色。
    
    使用方法：
    @router.post("/some-endpoint")
    async def some_endpoint(user_id: int = Depends(require_teacher)):
        # user_id 已经确保是教师
        ...
    """
    user_repo = UserRepository(session)
    user = await user_repo.get_by_id(user_id)
    
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="用户不存在"
        )
    
    if user.role != "teacher":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="只有教师可以执行此操作"
        )
    
    return user_id

__all__ = ["get_session", "get_mail", "require_teacher"]





