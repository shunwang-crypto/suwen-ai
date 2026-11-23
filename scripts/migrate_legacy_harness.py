#!/usr/bin/env python3
"""Import legacy checkpoints once during cutover, without executing old graphs.

Stop API/worker jobs first. Fresh databases need no legacy dependency. For an
upgrade with old checkpoint tables, install the core legacy-migration extra.
The importer preserves old tables and never overwrites a new harness session.
"""

from __future__ import annotations

import asyncio
import json
import uuid

from eduagents_core.config import get_settings
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool


def legacy_snapshot(checkpoint, pending_writes):
    channels = checkpoint.get("channel_values") or {}
    state = {key: value for key, value in channels.items() if not key.startswith(("__", "branch:"))}
    phases = [key.removeprefix("branch:to:") for key in channels if key.startswith("branch:to:")]
    pending = None
    for _task_id, channel, value in pending_writes or []:
        if channel == "__interrupt__" and value:
            pending = value[0].value
    if len(phases) > 1:
        raise ValueError("legacy session has multiple pending phases")
    # Validate JSON before writing anything. Never fall back to stringifying bytes.
    json.dumps(state)
    json.dumps(pending)
    return state, phases[0] if phases else None, pending


async def migrate(pool):
    async with pool.connection() as connection:
        exists = await (
            await connection.execute("SELECT to_regclass('public.checkpoints') AS name")
        ).fetchone()
        if not exists["name"]:
            return 0
        rows = await (
            await connection.execute(
                "SELECT DISTINCT thread_id FROM checkpoints WHERE checkpoint_ns = ''"
            )
        ).fetchall()
    if not rows:
        return 0
    try:
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    except ImportError as exc:
        raise RuntimeError(
            "Existing checkpoints require: pip install -e 'packages/core[legacy-migration]'"
        ) from exc
    saver = AsyncPostgresSaver(pool)
    imported = 0
    for row in rows:
        thread_id = row["thread_id"]
        parts = thread_id.split(":", 2)
        if len(parts) != 3 or parts[0] not in {"qa", "quiz", "plan", "tutor"}:
            continue
        user_id = str(uuid.UUID(parts[1]))
        saved = await saver.aget_tuple({"configurable": {"thread_id": thread_id}})
        state, phase, pending = legacy_snapshot(saved.checkpoint, saved.pending_writes)
        if state.get("user_id", user_id) != user_id:
            raise ValueError("legacy checkpoint owner mismatch")
        async with pool.connection() as connection, connection.transaction():
            await connection.execute("SELECT set_config('app.tenant_id', %s, true)", (user_id,))
            result = await connection.execute(
                """
                INSERT INTO harness_sessions (thread_id, tenant_id, user_id, kind, state, next_phase, pending, updated_at)
                SELECT %s, %s::uuid, id, %s, %s::jsonb, %s, %s::jsonb, %s::timestamptz
                FROM users WHERE id = %s::uuid
                ON CONFLICT (thread_id) DO NOTHING
            """,
                (
                    thread_id,
                    user_id,
                    parts[0],
                    json.dumps(state),
                    phase,
                    json.dumps(pending) if pending is not None else None,
                    saved.checkpoint["ts"],
                    user_id,
                ),
            )
            imported += result.rowcount
    return imported


async def main():
    settings = get_settings()
    if not settings.migration_database_url:
        raise RuntimeError("MIGRATION_DATABASE_URL is required")
    async with AsyncConnectionPool(
        settings.migration_database_url.replace("postgresql+asyncpg://", "postgresql://"),
        kwargs={"autocommit": True, "row_factory": dict_row},
        open=False,
    ) as pool:
        count = await migrate(pool)
    print(f"Legacy checkpoints imported: {count}")


if __name__ == "__main__":
    asyncio.run(main())
