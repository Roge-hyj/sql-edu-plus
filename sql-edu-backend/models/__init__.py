from .base import Base
from .user import User
from .question import Question
from .submission import Submission
from .auth import EmailCaptcha
from .chat import ChatMessage
from .question_feedback import QuestionDifficultyFeedback
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from settings.config import settings

# 1. 创建异步引擎对象
# 它负责管理连接池、翻译 SQL 语句
engine_kwargs = {
    "echo": True,
    "pool_recycle": 3600,
    "pool_pre_ping": True,
}
if not settings.DB_URL.startswith("sqlite"):
    engine_kwargs.update({
        "pool_size": 10,
        "max_overflow": 20,
        "pool_timeout": 10,
    })

engine = create_async_engine(
    settings.DB_URL,
    **engine_kwargs
)

# 2. 创建异步会话工厂 (Session Factory)
# 以后我们在 repository 里操作数据库，都靠它生产"业务员"
# 以后我们在 dependencies.py 里拿到的 session 都是由这个工厂生产的
AsyncSessionFactory = async_sessionmaker(
    bind=engine,
    autoflush=True,  # 对齐视频：查询前刷新缓存
    expire_on_commit=False,  # 异步开发必选 False，防止数据过期报错
)


__all__ = ["Base", "User", "Question", "Submission", "EmailCaptcha", "ChatMessage", "QuestionDifficultyFeedback"]



