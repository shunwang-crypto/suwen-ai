from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from eduagents_core.config import get_settings
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    global _engine, _sessionmaker
    if _engine is None:
        _engine = create_async_engine(
            get_settings().database_url, pool_pre_ping=True, pool_size=10, max_overflow=20
        )
        _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    get_engine()
    assert _sessionmaker is not None
    return _sessionmaker


async def dispose_engine() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _sessionmaker = None


@asynccontextmanager
async def tenant_session(tenant_id: uuid.UUID | str) -> AsyncIterator[AsyncSession]:
    """Transaction-scoped session with the RLS tenant GUC set.

    必须用 set_config(..., is_local=true)（事务内 SET LOCAL 语义）：asyncpg 连接池
    复用连接，session 级 SET 会把上一个租户的身份泄漏给下一个请求（U-A1 显式测试）。
    """
    async with get_sessionmaker()() as session:
        async with session.begin():
            await session.execute(
                text("SELECT set_config('app.tenant_id', :tid, true)"),
                {"tid": str(tenant_id)},
            )
            yield session


@asynccontextmanager
async def plain_session() -> AsyncIterator[AsyncSession]:
    """No tenant context: only for auth-layer tables (users/refresh_tokens)
    which are not RLS-protected. Never touch tenant tables through this."""
    async with get_sessionmaker()() as session:
        async with session.begin():
            yield session
