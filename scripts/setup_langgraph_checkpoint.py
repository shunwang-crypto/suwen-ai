#!/usr/bin/env python3
"""Create LangGraph checkpoint tables with the migration role (dev or prod).

Idempotent: AsyncPostgresSaver.setup() tracks applied steps in checkpoint_migrations,
so re-running is a no-op. Wired into `make migrate` so a fresh dev/prod DB is fully
provisioned; skipped in other envs (tests build their own tables in conftest)."""

from __future__ import annotations

import asyncio

from eduagents_core.config import get_settings
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool


def _pg_conninfo(url: str) -> str:
    return url.replace("postgresql+asyncpg://", "postgresql://")


async def main() -> None:
    settings = get_settings()
    if settings.app_env not in ("dev", "prod"):
        print(f"LangGraph checkpoint setup skipped (APP_ENV={settings.app_env}; dev/prod only)")
        return
    if not settings.migration_database_url:
        raise RuntimeError("MIGRATION_DATABASE_URL is required for checkpoint setup")

    pool = AsyncConnectionPool(
        _pg_conninfo(settings.migration_database_url),
        open=False,
        min_size=1,
        max_size=2,
        kwargs={"autocommit": True, "row_factory": dict_row},
    )
    await pool.open()
    try:
        await AsyncPostgresSaver(pool).setup()
    finally:
        await pool.close()
    print("LangGraph checkpoint schema is ready")


if __name__ == "__main__":
    asyncio.run(main())
