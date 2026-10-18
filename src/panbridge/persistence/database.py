"""数据库会话与引擎管理。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from panbridge.persistence.models import Base


def create_engine(database_url: str) -> AsyncEngine:
    engine = create_async_engine(database_url, echo=False)
    return engine


def create_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


async def init_db(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # 轻量 schema 迁移：create_all 只建缺失的表，不会给已有表补列。
        # 这里对已知后加的列做幂等 ALTER TABLE，避免旧库缺列导致查询报错。
        await conn.run_sync(_migrate_columns)


def _migrate_columns(conn) -> None:
    """补充历史版本缺失的列（幂等，SQLite 下用 PRAGMA 探测）。"""
    from sqlalchemy import inspect as sa_inspect
    from sqlalchemy import text

    inspector = sa_inspect(conn)
    if "tasks" not in inspector.get_table_names():
        return
    existing = {col["name"] for col in inspector.get_columns("tasks")}
    # 后加的列：result（任务最终结果 JSON）。
    if "result" not in existing:
        conn.execute(text("ALTER TABLE tasks ADD COLUMN result TEXT"))


@asynccontextmanager
async def session_scope(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    async with sessionmaker() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
