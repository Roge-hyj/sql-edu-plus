from functools import lru_cache
from typing import List, Literal
from pydantic import Field
from pydantic_settings import BaseSettings,SettingsConfigDict
from datetime import timedelta

from settings.ai_provider import load_cc_switch_provider

class Settings(BaseSettings):
    """全局配置。

    可以通过环境变量或 .env 文件覆盖默认值。
    """
    # --- 1. 基础信息 ---
    PROJECT_NAME: str = "SQL Edu Backend"
    DEBUG: bool = True  # 开发模式默认为 True
    API_V1_STR: str = "/api/v1"

    # --- 2. 数据库连接 ---
    # 业务持久化层固定使用 MySQL 8.0.46；判题层的 PARSEVAL_* 才是多方言连接。
    DB_URL: str
    BUSINESS_DB_DIALECT: Literal["mysql"] = "mysql"
    BUSINESS_DB_VERSION: str = "8.0.46"
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
    PARSEVAL_MYSQL_VERSION: str = "8.0.46"
    PARSEVAL_POSTGRES_VERSION: str = "16"
    PARSEVAL_TSQL_VERSION: str = "2022"
    PARSEVAL_ORACLE_VERSION: str = "23ai"
    # Optional offline BKT artifact.  Empty keeps the conservative
    # uncalibrated MVP parameters active.  The artifact loader verifies the
    # source digest and rejects synthetic or malformed calibration results.
    PHASE3_BKT_CALIBRATION_ARTIFACT: str = ""
    PHASE3_BKT_CALIBRATION_SOURCE: str = ""
    # Phase 1 jobs run in a killable child process in production.  The
    # limits protect the API worker from a wedged parser/witness workload;
    # tests may force the legacy thread adapter around a test double.
    PARSEVAL_WORKER_MODE: Literal["process", "thread"] = "process"
    # ``spawn`` avoids forking an already multi-threaded API process.  A
    # deployment may opt into ``forkserver`` or ``fork`` only after auditing
    # its runtime and native drivers.
    PARSEVAL_WORKER_START_METHOD: Literal["spawn", "forkserver", "fork"] = "spawn"
    PARSEVAL_WORKER_MAX_CONCURRENCY: int = Field(default=2, ge=1, le=64)
    # Number of requests allowed to wait behind active workers. Requests
    # beyond this bound fail closed instead of accumulating unbounded tasks.
    PARSEVAL_WORKER_QUEUE_LIMIT: int = Field(default=8, ge=0, le=1024)
    PARSEVAL_WORKER_MEMORY_MB: int = Field(default=2048, ge=128, le=8192)
    PARSEVAL_WORKER_CPU_SECONDS: int = Field(default=50, ge=1, le=600)

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
    # OpenAI-compatible wire protocol.  Chat Completions remains the default;
    # CC Switch Codex providers may explicitly use the Responses API.
    AI_WIRE_API: Literal["chat_completions", "responses"] = "chat_completions"
    AI_TEMPERATURE: float = 0.7
    # Optional, explicit CC Switch provider source for local development.  The
    # selected provider is read-only loaded at startup; its secret is never
    # written into this project or emitted in logs.
    AI_CC_SWITCH_DB_PATH: str = ""
    AI_CC_SWITCH_PROVIDER_ID: str = ""
    AI_CC_SWITCH_APP_TYPE: str = "codex"
    # Optional model alias override.  CC Switch's Codex profile may declare a
    # default model while its successful request history uses another alias.
    AI_CC_SWITCH_MODEL: str = ""
    # Evidence-bounded teaching calls.  Keep the feature opt-in so a
    # configured key never causes an unexpected paid request during tests or
    # a deployment rollout.  Once enabled, Phase 2 and Phase 5 can still be
    # disabled independently for staged rollout.
    LLM_TEACHING_ENABLED: bool = False
    LLM_PHASE2_ENABLED: bool = True
    LLM_PHASE5_ENABLED: bool = True
    LLM_TIMEOUT_SECONDS: float = Field(default=8.0, ge=1.0, le=60.0)
    LLM_MAX_OUTPUT_TOKENS: int = Field(default=1200, ge=128, le=8192)
    LLM_MAX_INPUT_BYTES: int = Field(default=48 * 1024, ge=4096, le=256 * 1024)

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


def _apply_cc_switch_ai_provider(current: Settings) -> None:
    """Apply an explicitly selected CC Switch provider without copying secrets."""

    database_path = str(current.AI_CC_SWITCH_DB_PATH or "").strip()
    provider_id = str(current.AI_CC_SWITCH_PROVIDER_ID or "").strip()
    if not database_path and not provider_id:
        return
    if not database_path or not provider_id:
        raise RuntimeError(
            "CC Switch AI 配置必须同时设置 AI_CC_SWITCH_DB_PATH 和 "
            "AI_CC_SWITCH_PROVIDER_ID。"
        )
    try:
        provider = load_cc_switch_provider(
            database_path=database_path,
            provider_id=provider_id,
            app_type=current.AI_CC_SWITCH_APP_TYPE,
        )
    except ValueError as exc:
        raise RuntimeError(f"CC Switch AI provider 配置无效：{exc}") from exc
    current.AI_API_KEY = provider.api_key
    current.AI_BASE_URL = provider.base_url
    current.AI_MODEL_NAME = (
        str(current.AI_CC_SWITCH_MODEL or "").strip() or provider.model
    )
    current.AI_WIRE_API = provider.wire_api


_apply_cc_switch_ai_provider(settings)

def validate_phase1_worker_config(
    *,
    mode: str,
    debug: bool,
    start_method: str,
    max_concurrency: int,
    queue_limit: int,
    memory_mb: int,
    cpu_seconds: int,
) -> None:
    """Reject unsafe worker configurations before the service starts."""
    normalized_mode = str(mode).strip().lower()
    if normalized_mode not in {"process", "thread"}:
        raise RuntimeError("Phase 1 worker mode must be process or thread")
    if normalized_mode == "thread" and not debug:
        raise RuntimeError(
            "生产/预发布禁止 PARSEVAL_WORKER_MODE=thread；"
            "请使用可强制终止的 process worker。"
        )
    if str(start_method).strip().lower() not in {"spawn", "forkserver", "fork"}:
        raise RuntimeError("Phase 1 worker start method is unsupported")
    if max_concurrency < 1 or max_concurrency > 64:
        raise RuntimeError("PARSEVAL_WORKER_MAX_CONCURRENCY must be between 1 and 64")
    if queue_limit < 0 or queue_limit > 1024:
        raise RuntimeError("PARSEVAL_WORKER_QUEUE_LIMIT must be between 0 and 1024")
    if memory_mb < 128 or memory_mb > 8192:
        raise RuntimeError("PARSEVAL_WORKER_MEMORY_MB must be between 128 and 8192")
    if cpu_seconds < 1 or cpu_seconds > 600:
        raise RuntimeError("PARSEVAL_WORKER_CPU_SECONDS must be between 1 and 600")


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
            "mysql+aiomysql:// 连接 MySQL 8.0.46；SQLite 仅允许 DEBUG 本地测试。"
        )


validate_business_db_url(settings.DB_URL, debug=settings.DEBUG)
validate_phase1_worker_config(
    mode=settings.PARSEVAL_WORKER_MODE,
    debug=settings.DEBUG,
    start_method=settings.PARSEVAL_WORKER_START_METHOD,
    max_concurrency=settings.PARSEVAL_WORKER_MAX_CONCURRENCY,
    queue_limit=settings.PARSEVAL_WORKER_QUEUE_LIMIT,
    memory_mb=settings.PARSEVAL_WORKER_MEMORY_MB,
    cpu_seconds=settings.PARSEVAL_WORKER_CPU_SECONDS,
)


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

    return settings


__all__ = ["Settings", "get_settings", "validate_business_db_url", "validate_phase1_worker_config"]
