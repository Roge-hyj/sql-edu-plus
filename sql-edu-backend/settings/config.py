from functools import lru_cache
from typing import List, Literal
from pydantic_settings import BaseSettings,SettingsConfigDict
from datetime import timedelta

class Settings(BaseSettings):
    """全局配置。

    可以通过环境变量或 .env 文件覆盖默认值。
    """
    # --- 1. 基础信息 ---
    PROJECT_NAME: str = "SQL Edu Backend"
    DEBUG: bool = True  # 开发模式默认为 True
    API_V1_STR: str = "/api/v1"

    # --- 2. 数据库连接 ---
    # 业务持久化层固定使用 MySQL 8.4；判题层的 PARSEVAL_* 才是多方言连接。
    DB_URL: str
    BUSINESS_DB_DIALECT: Literal["mysql"] = "mysql"
    BUSINESS_DB_VERSION: str = "8.4"
    BUSINESS_DB_CHARSET: str = "utf8mb4"
    # SQLAlchemy SQL/parameter logging is opt-in and must stay disabled in production.
    DB_ECHO: bool = False

    # --- 2.1 ParSEval 判题执行器 ---
    # auto/native: 按最终解析出的方言选择原生引擎；连接缺失时返回 ENGINE_ERROR
    # sqlite: 仅用于本地兼容测试，不用于还原题目声明的原生方言
    # mysql/postgres/tsql/oracle: 强制指定原生判题执行器
    PARSEVAL_EXECUTION_BACKEND: str = "auto"
    # 题目未声明方言且无唯一专属语法特征时使用的系统默认引擎。
    PARSEVAL_DEFAULT_DIALECT: str = "mysql"
    PARSEVAL_MYSQL_URL: str = ""
    PARSEVAL_POSTGRES_URL: str = ""
    PARSEVAL_TSQL_URL: str = ""
    PARSEVAL_ORACLE_URL: str = ""
    # Runner 实际版本。题目声明 engine_version 时必须与对应值兼容。
    PARSEVAL_MYSQL_VERSION: str = "8.4"
    PARSEVAL_POSTGRES_VERSION: str = "16"
    PARSEVAL_TSQL_VERSION: str = "2022"
    PARSEVAL_ORACLE_VERSION: str = "23ai"

    # --- 3. 安全与认证 (JWT) ---
    # 在终端运行 `openssl rand -hex 32` 可以生成一个安全的随机字符串，需在 .env 中设置
    SECRET_KEY: str = ""
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24 * 7  # Token 过期时间，这里设为 7 天

    # --- 4. CORS 跨域配置 ---
    # 允许访问后端的来源列表，注意这里是 List[str]
    BACKEND_CORS_ORIGINS: List[str] = [
        "http://localhost",
        "http://localhost:3000",  # React/Next.js 默认端口
        "http://localhost:5173",  # Vite/Vue 默认端口
        "http://127.0.0.1:5173",
    ]
    # --- 5. AI 大模型配置 ---
    AI_API_KEY: str = ""
    AI_BASE_URL: str = ""
    # 模型名称，统一用于判题提示、对话、题目生成等
    AI_MODEL_NAME: str = "gpt-3.5-turbo"
    AI_TEMPERATURE: float = 0.7

    # --- 6. 邮件服务器配置 (Outlook) ---
    MAIL_USERNAME: str
    MAIL_PASSWORD: str
    MAIL_FROM: str
    MAIL_PORT: int = 465             # Gmail 推荐 465
    MAIL_SERVER: str = "smtp.gmail.com"
    MAIL_FROM_NAME: str = "SQL-Edu System"

    # Gmail 465 端口的加密组合：
    MAIL_STARTTLS: bool = False      # 关闭 STARTTLS
    MAIL_SSL_TLS: bool = True        # 开启 SSL/TLS
    
    MAIL_USE_CREDENTIALS: bool = True
    VALIDATE_CERTS: bool = True
    # The diagnostic mail endpoint is never enabled implicitly in production.
    ENABLE_MAIL_TEST: bool = False
    # Optional teacher registration secret. Empty means teacher invite registration is disabled.
    TEACHER_INVITE_CODE: str = ""
    # Email captcha abuse controls. Values are deliberately conservative defaults.
    CAPTCHA_EXPIRE_MINUTES: int = 10
    CAPTCHA_SEND_INTERVAL_SECONDS: int = 60
    CAPTCHA_DAILY_EMAIL_LIMIT: int = 5
    CAPTCHA_DAILY_IP_LIMIT: int = 20
    CAPTCHA_MAX_VERIFY_ATTEMPTS: int = 5

    #7.配置JWT密钥和时间 (Settings) - 必须通过 .env 设置，勿写死在代码中
    JWT_SECRET_KEY: str = ""  # 请在 .env 中设置，例如：openssl rand -hex 32 

    # Token 过期时间
    JWT_ACCESS_TOKEN_EXPIRES : timedelta = timedelta(minutes=60)      # Access Token 1小时过期
    JWT_REFRESH_TOKEN_EXPIRES: timedelta = timedelta(days=7)         # Refresh Token 7天过期
    # --- 8. 配置加载项 (Pydantic V2 新写法) ---
    model_config = SettingsConfigDict(
        env_file=".env",
        case_sensitive=True,
        extra="ignore" # 忽略 .env 中多余的字段
    )

from sys import exit as _exit


# 实例化对象并做关键配置校验
settings = Settings()

def validate_business_db_url(db_url: str, *, debug: bool) -> None:
    """Enforce the single business-database contract.

    SQLite remains available for unit tests; every deployed environment must
    use the async MySQL driver.
    """
    if debug and db_url.startswith("sqlite"):
        return
    if not db_url.startswith("mysql+aiomysql://"):
        raise RuntimeError(
            "业务数据库配置不受支持：生产/预发布必须使用 "
            "mysql+aiomysql:// 连接 MySQL 8.4；SQLite 仅允许 DEBUG 本地测试。"
        )


validate_business_db_url(settings.DB_URL, debug=settings.DEBUG)

# 启动时强制校验安全关键配置，避免使用空密钥
missing_secrets: list[str] = []
if not settings.SECRET_KEY:
    missing_secrets.append("SECRET_KEY")
if not settings.JWT_SECRET_KEY:
    missing_secrets.append("JWT_SECRET_KEY")
if missing_secrets:
    missing = ", ".join(missing_secrets)
    raise RuntimeError(
        f"安全配置缺失：{missing} 未在环境变量或 .env 中正确设置。"
        " 请参考 .env.example 生成随机密钥（例如使用 `openssl rand -hex 32`）后重启服务。"
    )

@lru_cache
def get_settings() -> Settings:
    """获取单例 Settings 实例。"""

    return Settings()


__all__ = ["Settings", "get_settings", "validate_business_db_url"]
