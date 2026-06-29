"""
ORM 基类与命名约定（SQLAlchemy）

本模块定义所有 ORM 模型继承的 `Base`，并统一约束：
- index / unique / foreign key / primary key 等命名规则，便于迁移与数据库一致性
"""

from sqlalchemy.orm import DeclarativeBase
from sqlalchemy import MetaData

convention = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """所有 ORM 模型的基类。"""

    metadata = MetaData(naming_convention=convention)


__all__ = ["Base"]





