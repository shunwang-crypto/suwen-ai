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
async def tenant_session(
    tenant_id: uuid.UUID | str,
    org_ids: tuple[str, ...] | list[str] = (),
    is_admin: bool = False,
) -> AsyncIterator[AsyncSession]:
    """Transaction-scoped session with the RLS GUCs set (唯一注入通道, D14)。

    - app.tenant_id: 必设。个人请求 = user_id; org 库写路径/worker = 该库 owner_id(org_id)
      (service 层先校验 org admin, 见 routers/kb.py / routers/orgs.py)。
    - app.org_ids: 可选, 用户所属机构 id 逗号表 -> kb/documents/chunks 的 org 只读放行。
    - app.is_admin: 可选, 运营聚合(usage_log)与 orgs 凭码查询的事务级放行 (D7 惯例)。

    必须用 set_config(..., is_local=true)（事务内 SET LOCAL 语义）：asyncpg 连接池
    复用连接，session 级 SET 会把上一个租户的身份泄漏给下一个请求（U-A1 显式测试）。
    """
    async with get_sessionmaker()() as session:
        async with session.begin():
            await session.execute(
                text("SELECT set_config('app.tenant_id', :tid, true)"),
                {"tid": str(tenant_id)},
            )
            if org_ids:
                await session.execute(
                    text("SELECT set_config('app.org_ids', :oids, true)"),
                    {"oids": ",".join(str(o) for o in org_ids)},
                )
            if is_admin:
                await session.execute(
                    text("SELECT set_config('app.is_admin', 'true', true)")
                )
            yield session


@asynccontextmanager
async def plain_session() -> AsyncIterator[AsyncSession]:
    """No tenant context: only for auth-layer tables (users/refresh_tokens)
    which are not RLS-protected. Never touch tenant tables through this."""
    async with get_sessionmaker()() as session:
        async with session.begin():
            yield session
