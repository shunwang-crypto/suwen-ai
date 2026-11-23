"""0039 upgrade, legacy import, FORCE RLS and historical-data preservation."""

import asyncio
import os
import uuid
from typing import TypedDict

import pytest

from .test_migration_0012 import _alembic, _fetch, _run_in_database
from .test_migration_0036_0037 import _connect_as_app, _execute_as_migration, _set_context

pytestmark = pytest.mark.skipif(
    os.environ.get("MIGRATION_TESTS") != "1", reason="MIGRATION_TESTS=1 required"
)


def test_0039_upgrade_legacy_confirmation_rls_and_downgrade():
    def run(database_url):
        _alembic(database_url, "upgrade", "0038")
        owner, outsider = str(uuid.uuid4()), str(uuid.uuid4())
        thread_id = f"quiz:{owner}:{uuid.uuid4()}"

        async def seed():
            await _execute_as_migration(
                database_url,
                "INSERT INTO users (id,email,password_hash,role,status,guardian_consent,plan) "
                "VALUES ($1,$2,'hash','student','active',false,'free')",
                owner,
                f"{owner}@test.invalid",
            )
            # Only the migration fixture imports the retired engine to create
            # authentic old bytes. The new runtime never imports/executes it.
            from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
            from langgraph.graph import END, START, StateGraph
            from langgraph.types import interrupt
            from psycopg.rows import dict_row
            from psycopg_pool import AsyncConnectionPool

            class LegacyState(TypedDict):
                user_id: str
                blueprint: dict

            async def confirm(state):
                interrupt({"kind": "blueprint_confirm", "blueprint": state["blueprint"]})

            async with AsyncConnectionPool(
                database_url.replace("postgresql+asyncpg://", "postgresql://"),
                kwargs={"autocommit": True, "row_factory": dict_row},
                open=False,
            ) as pool:
                saver = AsyncPostgresSaver(pool)
                await saver.setup()
                old = StateGraph(LegacyState)
                old.add_node("confirm_blueprint", confirm)
                old.add_edge(START, "confirm_blueprint")
                old.add_edge("confirm_blueprint", END)
                await old.compile(checkpointer=saver).ainvoke(
                    {"user_id": owner, "blueprint": {"items": [], "summary": "legacy draft"}},
                    {"configurable": {"thread_id": thread_id}},
                )

        asyncio.run(seed())
        _alembic(database_url, "upgrade", "head")

        async def inspect_import():
            import sys
            from pathlib import Path

            from psycopg.rows import dict_row
            from psycopg_pool import AsyncConnectionPool

            sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
            from migrate_legacy_harness import migrate

            async with AsyncConnectionPool(
                database_url.replace("postgresql+asyncpg://", "postgresql://"),
                kwargs={"autocommit": True, "row_factory": dict_row},
                open=False,
            ) as pool:
                assert await migrate(pool) == 1
                assert await migrate(pool) == 0
            flags = await _fetch(
                database_url,
                "SELECT relrowsecurity,relforcerowsecurity FROM pg_class WHERE relname='harness_sessions'",
            )
            assert flags[0]["relrowsecurity"] and flags[0]["relforcerowsecurity"]
            conn = await _connect_as_app(database_url)
            try:
                await _set_context(conn, owner)
                row = await conn.fetchrow(
                    "SELECT state->'blueprint'->>'summary' AS summary,next_phase,pending->>'kind' AS kind FROM harness_sessions"
                )
                assert dict(row) == {
                    "summary": "legacy draft",
                    "next_phase": "confirm_blueprint",
                    "kind": "blueprint_confirm",
                }
                await _set_context(conn, outsider)
                assert await conn.fetchval("SELECT count(*) FROM harness_sessions") == 0
                await _set_context(conn, "")
                assert await conn.fetchval("SELECT count(*) FROM harness_sessions") == 0
            finally:
                await conn.close()
            assert (await _fetch(database_url, "SELECT count(*) AS n FROM checkpoints"))[0]["n"] > 0

        asyncio.run(inspect_import())
        _alembic(database_url, "downgrade", "0038")
        assert (
            asyncio.run(_fetch(database_url, "SELECT count(*) AS n FROM checkpoints"))[0]["n"] > 0
        )
        _alembic(database_url, "upgrade", "head")

    _run_in_database(run)
