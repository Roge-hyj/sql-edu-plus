"""
SQL 智能教学系统 — 后端入口。

挂载路由：/auth（认证）、/ai（判题与对话）、/questions（题目管理）。
依赖：数据库会话 get_session、邮件 get_mail、JWT 在各路由内使用。
"""
from fastapi import FastAPI
from fastapi_mail import FastMail, MessageSchema, MessageType
from fastapi import Depends
from dependencies import get_mail
from aiosmtplib import SMTPException

from fastapi.middleware.cors import CORSMiddleware

from routers import ai as ai_router
from routers import question as question_router
from routers.auth import router as auth_router
from settings.config import settings

app = FastAPI(title="SQL 智能教学系统后端")

app.include_router(auth_router)

async def mail_test(
    email: str,
    mail:FastMail=Depends(get_mail)
):  
    message=MessageSchema(
        subject="测试邮件",
        recipients=[email],
        body="这是一封测试邮件，来自 SQL 智能教学系统。",
        subtype=MessageType.plain
    )
    
    try:
        await mail.send_message(message)
    except SMTPException as e:
        return {"message":"邮件发送失败","error":str(e)}
    return {"message":"邮件发送成功"}


if settings.ENABLE_MAIL_TEST:
    app.add_api_route("/mail/test", mail_test, methods=["GET"])



# CORS 配置
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.BACKEND_CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "Accept"],
)




# 挂载路由
app.include_router(ai_router.router)
app.include_router(question_router.router)
# 注意：/users 路由已移除，统一使用 /auth 路由进行用户管理


@app.get("/")
async def root():
    return {"message": "SQL 智能教学系统后端运行中"}


__all__ = ["app"]






