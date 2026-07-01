from fastapi import APIRouter, Depends, Query,HTTPException,status 
from pydantic import EmailStr
from typing import Annotated
from dependencies import get_mail, get_session
from fastapi_mail import FastMail, MessageSchema, MessageType
from sqlalchemy.ext.asyncio import AsyncSession
import string
import random
from repository.user_repo import EmailCodeRepository, UserRepository
from schemas import ResponseOut
from schemas.user import (
    RegisterIn, UserCreateSchema, LoginIn, LoginOut, DeleteAccountIn,
    ChangePasswordIn, UserProfileUpdate, UserSchema
)
from pydantic import BaseModel
from models.user import User
from core.auth import AuthHandler
from core.experience_service import get_level_from_total

router = APIRouter(prefix="/auth", tags=["user"])
auth_handler=AuthHandler()
#发送验证码
@router.get("/code", response_model=ResponseOut)
async def get_email_captcha(
    email: Annotated[EmailStr, Query(description="收件人邮箱")],
    mail: FastMail = Depends(get_mail),
    session: AsyncSession = Depends(get_session)
):
    # 1. 生成验证码
    code = "".join(random.sample(string.digits * 6, 6))
    
    email_repo = EmailCodeRepository(session)
    
    # 2. 先存库
    try:
        await email_repo.add_email_captcha(email, code)
        await session.commit() # 确保验证码入库
    except Exception as e:
        print(f"验证码入库失败: {e}")
        return ResponseOut(result="failure", detail="系统错误")

    # 3. 发送邮件
    message = MessageSchema(
        subject="SQL-Edu 验证码",
        recipients=[email],
        body=f"【SQL-Edu】您的验证码为：{code}，有效期为 10 分钟。",
        subtype=MessageType.plain
    )
    
    try:
        await mail.send_message(message)
    except Exception as e:
        error_str = str(e)
        # 兼容腾讯/Foxmail的非标准成功响应
        if "-1" in error_str and "\\x00" in error_str:
            pass 
        else:
            print(f"邮件发送失败: {error_str}")
            # 发送失败，回滚数据库（删除刚才存的码）
            await email_repo.delete_captcha_record(email, code)
            await session.commit()
            return ResponseOut(result="failure", detail="邮件发送失败")

    return ResponseOut(result="success")

#注册
@router.post("/register", response_model=ResponseOut)
async def register_user(
    data: RegisterIn,
    session: AsyncSession = Depends(get_session),
):
    email_repo = EmailCodeRepository(session)
    user_repo = UserRepository(session)
    
    # 1. 校验验证码
    if not await email_repo.check_email_captcha(data.email, data.captcha):
        return ResponseOut(result="failure", detail="验证码无效或已过期")
    
    # 2. 校验密码
    if data.password != data.confirm_password:
        return ResponseOut(result="failure", detail="两次密码不一致")
    
    # 3. 校验邮箱是否已存在
    if await user_repo.email_is_exist(data.email):
        return ResponseOut(result="failure", detail="该邮箱已被注册")
    
    try:
        # 4. 执行注册逻辑
        # 根据邀请码决定角色
        role = "teacher" if (data.invite_code and data.invite_code == "ILOVESQL") else "student"
        # 创建用户对象
        user_schema = UserCreateSchema(
            email=data.email, 
            username=data.username, 
            password=data.password,
            role=role,
        )
        await user_repo.create_user(user_schema)
        
        # 标记验证码已使用
        await email_repo.mark_captcha_used(data.email, data.captcha)
        
        # 提交事务（这一步最重要，没有它数据库里是空的）
        await session.commit()
        
        return ResponseOut(result="success")
        
    except Exception as e:
        # 发生错误回滚
        await session.rollback()
        print(f"注册崩溃: {str(e)}")
        return ResponseOut(result="failure", detail=f"注册失败: {str(e)}")
    

@router.post("/login", response_model=LoginOut)
async def Login(
    data:LoginIn,
    session:AsyncSession=Depends(get_session),
):
    #1.创建user_repo对象 
    user_repo=UserRepository(session)
    #2.根据邮箱或用户名查找用户
    user:User|None =await user_repo.get_by_email_or_username(str(data.email))
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,detail="该用户不存在！")
    if not user.verify_password(data.password):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,detail="用户名/邮箱或密码错误！")
    #3.生成JWToken
    tokens = auth_handler.encode_login_token(user.id)
    total_xp = getattr(user, "total_experience", 0) or 0
    level, exp_in_level, xp_to_next = get_level_from_total(total_xp)
    user_schema = UserSchema(
        id=user.id,
        email=user.email,
        username=user.username,
        role=user.role,
        level=level,
        experience_in_level=exp_in_level,
        xp_to_next_level=xp_to_next,
    )
    return {
        "user": user_schema,
        "token": tokens["access_token"],
        "refresh_token": tokens["refresh_token"],
    }


class RefreshTokenRequest(BaseModel):
    """刷新 Token 请求。"""
    refresh_token: str


@router.post("/refresh", response_model=dict)
async def refresh_token(
    data: RefreshTokenRequest,
):
    """使用 Refresh Token 刷新 Access Token。

    当 Access Token 过期时，可以使用 Refresh Token 获取新的 Access Token，
    无需重新登录。
    """
    try:
        # 验证 Refresh Token
        user_id = auth_handler.decode_refresh_token(data.refresh_token)
        
        # 生成新的 Access Token
        new_token = auth_handler.encode_update_token(user_id)
        
        return {
            "access_token": new_token["access_token"],
            "token_type": "bearer"
        }
    except HTTPException as e:
        # 重新抛出 HTTP 异常
        raise e
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"刷新 Token 失败: {str(e)}"
        )


@router.delete("/delete-account", response_model=ResponseOut)
async def delete_account(
    data: DeleteAccountIn,
    user_id: int = Depends(auth_handler.auth_access_dependency),
    session: AsyncSession = Depends(get_session),
):
    """注销账户（需要密码确认）。

    注意：此操作不可逆，将删除：
    - 用户账户
    - 所有提交记录（级联删除）
    - 所有学习数据

    流程：
    1. 验证用户身份（通过 Token）
    2. 验证密码确认
    3. 删除用户及其所有相关数据
    """
    user_repo = UserRepository(session)
    
    # 1. 查询用户
    user = await user_repo.get_by_id(user_id)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="用户不存在"
        )
    
    # 2. 验证密码
    if not user.verify_password(data.password):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="密码错误，无法注销账户"
        )
    
    try:
        # 3. 删除用户（级联删除相关数据）
        success = await user_repo.delete_user(user_id)
        if not success:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="删除用户失败"
            )
        
        # 4. 提交事务
        await session.commit()
        
        return ResponseOut(result="success", detail="账户已成功注销，所有数据已删除")
        
    except Exception as e:
        # 发生错误回滚
        await session.rollback()
        print(f"注销账户失败: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"注销账户失败: {str(e)}"
        )


@router.get("/profile", response_model=UserSchema)
async def get_profile(
    user_id: int = Depends(auth_handler.auth_access_dependency),
    session: AsyncSession = Depends(get_session),
):
    """获取当前用户信息（含等级经验，供学生端展示）。"""
    user_repo = UserRepository(session)
    user = await user_repo.get_by_id(user_id)
    
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="用户不存在"
        )
    
    total_xp = getattr(user, "total_experience", 0) or 0
    level, exp_in_level, xp_to_next = get_level_from_total(total_xp)
    return UserSchema(
        id=user.id,
        email=user.email,
        username=user.username,
        role=user.role,
        level=level,
        experience_in_level=exp_in_level,
        xp_to_next_level=xp_to_next,
    )


@router.put("/profile", response_model=UserSchema)
async def update_profile(
    data: UserProfileUpdate,
    user_id: int = Depends(auth_handler.auth_access_dependency),
    session: AsyncSession = Depends(get_session),
):
    """更新当前用户信息（用户名等）。"""
    from sqlalchemy import update
    
    user_repo = UserRepository(session)
    user = await user_repo.get_by_id(user_id)
    
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="用户不存在"
        )
    
    try:
        # 更新用户名（如果有提供）
        if data.username is not None:
            # 检查用户名是否已被其他用户使用
            existing_user = await user_repo.get_by_username(data.username)
            if existing_user and existing_user.id != user_id:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="该用户名已被使用"
                )
            
            from sqlalchemy import update
            stmt = (
                update(User)
                .where(User.id == user_id)
                .values(username=data.username)
            )
            await session.execute(stmt)
        
        await session.commit()
        await session.refresh(user)
        total_xp = getattr(user, "total_experience", 0) or 0
        level, exp_in_level, xp_to_next = get_level_from_total(total_xp)
        return UserSchema(
            id=user.id,
            email=user.email,
            username=user.username,
            role=user.role,
            level=level,
            experience_in_level=exp_in_level,
            xp_to_next_level=xp_to_next,
        )
        
    except Exception as e:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"更新用户信息失败: {str(e)}"
        )


@router.post("/change-password", response_model=ResponseOut)
async def change_password(
    data: ChangePasswordIn,
    user_id: int = Depends(auth_handler.auth_access_dependency),
    session: AsyncSession = Depends(get_session),
):
    """修改密码。

    需要提供旧密码进行验证。
    """
    user_repo = UserRepository(session)
    user = await user_repo.get_by_id(user_id)
    
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="用户不存在"
        )
    
    # 验证旧密码
    if not user.verify_password(data.old_password):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="旧密码错误"
        )
    
    try:
        # 更新密码
        user.password = data.new_password
        await session.commit()
        
        return ResponseOut(result="success", detail="密码修改成功")
        
    except Exception as e:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"修改密码失败: {str(e)}"
        )


@router.post("/logout", response_model=ResponseOut)
async def logout(
    user_id: int = Depends(auth_handler.auth_access_dependency),
):
    """退出登录（保留数据）。

    注意：
    - 此操作不会删除任何用户数据
    - 只是让当前 Token 失效（前端需要删除本地存储的 Token）
    - 由于 JWT 是无状态的，服务端无法直接使 Token 失效
    - 建议前端在收到成功响应后，立即删除本地存储的 Token 和 Refresh Token
    
    如果需要更严格的 Token 失效控制，可以考虑：
    1. 维护 Token 黑名单（使用 Redis）
    2. 在 Token 验证时检查黑名单
    """
    # JWT 是无状态的，服务端无法直接使 Token 失效
    # 这里只是返回成功，提示前端删除 Token
    # 如果需要更严格的失效控制，可以在这里将 Token 加入黑名单（需要 Redis 等存储）
    
    return ResponseOut(
        result="success", 
        detail="退出登录成功，请前端删除本地存储的 Token"
    )
