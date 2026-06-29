"""
模型与数据库会话工厂汇总（models package）

本模块聚合：
- 所有 ORM 模型的 import（便于 Alembic 自动发现 metadata）
- async engine 与 `AsyncSessionFactory` 的初始化

注意：
- `engine` 使用 `settings.DB_URL`
- `AsyncSessionFactory` 用于依赖注入（见 `dependencies.get_session`）
"""

from .base import Base
from .user import User
from .question import Question
from .submission import Submission
from .auth import EmailCaptcha
from .chat import ChatMessage
from .question_feedback import QuestionDifficultyFeedback
from .knowledge_mastery import KnowledgeMastery
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from settings.config import settings

# 1. 创建异步引擎对象
# 它负责管理连接池、翻译 SQL 语句
engine = create_async_engine(
    settings.DB_URL,
    # 使用你 config.py 里的 3307 链接
    echo=True,  # 打印 SQL 日志，方便你观察 AI 生成的语句
    pool_size=10,  # 初始连接池大小
    max_overflow=20,  # 最大溢出连接数
    pool_timeout=10,  # 连接超时时间
    pool_recycle=3600,  # 1小时回收一次连接，防止被 MySQL 踢掉
    pool_pre_ping=True,  # 每次使用前检查连接是否活着
)

# 2. 创建异步会话工厂 (Session Factory)
# 以后我们在 repository 里操作数据库，都靠它生产"业务员"
# 以后我们在 dependencies.py 里拿到的 session 都是由这个工厂生产的
AsyncSessionFactory = async_sessionmaker(
    bind=engine,
    autoflush=True,  # 对齐视频：查询前刷新缓存
    expire_on_commit=False,  # 异步开发必选 False，防止数据过期报错
)


__all__ = ["Base", "User", "Question", "Submission", "EmailCaptcha", "ChatMessage", "QuestionDifficultyFeedback", "KnowledgeMastery"]



