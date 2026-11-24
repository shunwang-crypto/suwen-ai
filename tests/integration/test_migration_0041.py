"""Memory controls migrate from 0040 and replay safely on a fresh database."""

import asyncio
import os
import uuid

import pytest

from .test_migration_0012 import _alembic, _fetch, _run_in_database
from .test_migration_0036_0037 import _connect_as_app, _execute_as_migration, _set_context

pytestmark = pytest.mark.skipif(os.environ.get("MIGRATION_TESTS") != "1", reason="MIGRATION_TESTS=1 required")


def test_memory_migration_upgrade_downgrade_and_tenant_isolation():
    def run(url):
        _alembic(url, "upgrade", "head")
        _alembic(url, "downgrade", "0040")
        # Downgrade removes columns that 0005's current ORM can create early;
        # now exercise the actual upgrade ALTER path with a retained old row.
        owner, outsider = str(uuid.uuid4()), str(uuid.uuid4())

        async def seed():
            await _execute_as_migration(url,
                "INSERT INTO users(id,email,password_hash,role,status,guardian_consent,plan) VALUES($1,$2,'hash','student','active',false,'free')",
                owner, f"{owner}@test.invalid")
            await _execute_as_migration(url,
                "INSERT INTO learner_insights(id,tenant_id,user_id,source,kind,text,created_at) VALUES($1,$2,$2,'chat_reflection','confused','preserved',now())",
                str(uuid.uuid4()), owner)

        asyncio.run(seed())
        _alembic(url, "upgrade", "head")

        async def verify():
            rows = await _fetch(url, "SELECT text,observed_at,expires_at,revision,status FROM learner_insights")
            assert len(rows) == 1 and rows[0]["text"] == "preserved"
            assert rows[0]["observed_at"] and rows[0]["expires_at"]
            assert rows[0]["revision"] == 1 and rows[0]["status"] == "active"
            conn = await _connect_as_app(url)
            try:
                await _set_context(conn, owner)
                await conn.execute("INSERT INTO learner_memory_policies(user_id,tenant_id) VALUES($1,$1)", uuid.UUID(owner))
                assert await conn.fetchval("SELECT count(*) FROM learner_memory_policies") == 1
                await _set_context(conn, outsider)
                assert await conn.fetchval("SELECT count(*) FROM learner_memory_policies") == 0
            finally:
                await conn.close()

        asyncio.run(verify())
        _alembic(url, "downgrade", "0040")
        assert len(asyncio.run(_fetch(url, "SELECT text FROM learner_insights"))) == 1
        _alembic(url, "upgrade", "head")

    _run_in_database(run)
