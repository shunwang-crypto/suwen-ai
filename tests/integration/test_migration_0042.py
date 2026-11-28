"""Collaborator identities migrate safely without discarding native history."""

import asyncio
import os
import uuid

import pytest

from .test_migration_0012 import _alembic, _fetch, _run_in_database
from .test_migration_0036_0037 import _connect_as_app, _execute_as_migration, _set_context

pytestmark = pytest.mark.skipif(os.environ.get("MIGRATION_TESTS") != "1", reason="MIGRATION_TESTS=1 required")


def test_collaboration_upgrade_downgrade_retains_roots_and_protects_children():
    def run(url):
        _alembic(url, "upgrade", "head")
        _alembic(url, "downgrade", "0041")
        owner, outsider = str(uuid.uuid4()), str(uuid.uuid4())
        root = f"plan:{owner}:{uuid.uuid4()}"
        child = f"researcher:{owner}:{uuid.uuid4()}"

        async def seed():
            await _execute_as_migration(url,
                "INSERT INTO users(id,email,password_hash,role,status,guardian_consent,plan) VALUES($1,$2,'hash','student','active',false,'free')",
                owner, f"{owner}@test.invalid")
            await _execute_as_migration(url,
                "INSERT INTO harness_sessions(thread_id,tenant_id,user_id,kind,state,status) VALUES($1,$2,$2,'plan','{}','completed')",
                root, owner)
        asyncio.run(seed())
        _alembic(url, "upgrade", "head")

        async def verify():
            conn = await _connect_as_app(url)
            try:
                await _set_context(conn, owner)
                await conn.execute(
                    "INSERT INTO harness_sessions(thread_id,tenant_id,user_id,kind,state,status,parent_thread_id,agent_name) "
                    "VALUES($1,$2,$2,'researcher','{}','completed',$3,'research')",
                    child, uuid.UUID(owner), root)
                assert await conn.fetchval("SELECT parent_thread_id FROM harness_sessions WHERE thread_id=$1", child) == root
                await _set_context(conn, outsider)
                assert await conn.fetchval("SELECT count(*) FROM harness_sessions") == 0
            finally:
                await conn.close()
        asyncio.run(verify())
        with pytest.raises(pytest.fail.Exception, match="remove collaborator histories before downgrade"):
            _alembic(url, "downgrade", "0041")
        asyncio.run(_execute_as_migration(url, "DELETE FROM harness_sessions WHERE thread_id=$1", child))
        _alembic(url, "downgrade", "0041")
        assert asyncio.run(_fetch(url, "SELECT thread_id FROM harness_sessions"))[0]["thread_id"] == root
        _alembic(url, "upgrade", "head")
    _run_in_database(run)
