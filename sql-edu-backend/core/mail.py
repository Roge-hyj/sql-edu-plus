"""
邮件发送配置（FastMail）

本模块负责创建 FastMail 实例，供路由通过依赖注入使用。
注意：
- 邮件服务器参数来自 `settings.config.settings`
- 生产环境建议开启证书校验（VALIDATE_CERTS=True），开发环境可临时关闭便于调试
"""

from fastapi_mail import FastMail, ConnectionConfig
from settings.config import settings

def create_mail_instance() -> FastMail:
    mail_config = ConnectionConfig(
        MAIL_USERNAME=settings.MAIL_USERNAME,
        MAIL_PASSWORD=settings.MAIL_PASSWORD, # 确保 config.py 里是 str 类型
        MAIL_FROM=settings.MAIL_FROM,
        MAIL_PORT=settings.MAIL_PORT,
        MAIL_SERVER=settings.MAIL_SERVER,
        MAIL_FROM_NAME=settings.MAIL_FROM_NAME,
        
        # 关键是这两行要跟着 config 变
        MAIL_STARTTLS=settings.MAIL_STARTTLS, # False
        MAIL_SSL_TLS=settings.MAIL_SSL_TLS,   # True
        
        USE_CREDENTIALS=True,
        VALIDATE_CERTS=False # 开发环境可以先关掉，防止 SSL 报错
    )
    return FastMail(mail_config)