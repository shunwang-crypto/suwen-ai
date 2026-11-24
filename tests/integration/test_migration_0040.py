"""Native archives/lifecycle migrate without deleting existing education state."""

import asyncio
import os
import uuid

import pytest

from .test_migration_0012 import _alembic, _fetch, _run_in_database
from .test_migration_0036_0037 import _connect_as_app, _execute_as_migration, _set_context

pytestmark = pytest.mark.skipif(
    os.environ.get("MIGRATION_TESTS") != "1", reason="MIGRATION_TESTS=1 required"
)


def test_0040_preserves_confirmations_and_rls_and_supports_native_archive():

    def run(url):
        _alembic(url, "upgrade", "0039")
        owner, outsider = (str(uuid.uuid4()), str(uuid.uuid4()))

        async def seed():
            await _execute_as_migration(
                url,
                "INSERT INTO users(id,email,password_hash,role,status,guardian_consent,plan) VALUES($1,$2,'hash','student','active',false,'free')",
                owner,
                f"{owner}@test.invalid",
            )
            for local, phase, pending in [
                ("pending", "confirm_plan", '{"kind":"plan_confirm","plan":{"stages":[]}}'),
                ("finished", None, None),
                ("partial", "draft_plan", None),
            ]:
                await _execute_as_migration(
                    url,
                    "INSERT INTO harness_sessions(thread_id,tenant_id,user_id,kind,state,next_phase,pending) VALUES($1,$2,$2,'plan','{\"goal\":\"preserved\"}',$3,$4::jsonb)",
                    f"plan:{owner}:{local}",
                    owner,
                    phase,
                    pending,
                )

        asyncio.run(seed())
        _alembic(url, "upgrade", "head")

        async def verify():
            rows = await _fetch(
                url, "SELECT status,state->>'goal' AS goal FROM harness_sessions ORDER BY thread_id"
            )
            assert {r["status"] for r in rows} == {"completed", "idle", "awaiting_confirmation"}
            assert all(r["goal"] == "preserved" for r in rows)
            conn = await _connect_as_app(url)
            try:
                await _set_context(conn, owner)
                await conn.execute(
                    "UPDATE harness_sessions SET native_session_id='native',native_archive='{\"sessions/log\":\"bG9n\"}' WHERE status='completed'"
                )
                assert await conn.fetchval("SELECT count(*) FROM harness_sessions") == 3
                with pytest.raises(Exception, match="ck_harness_status"):
                    await conn.execute("UPDATE harness_sessions SET status='invalid'")
                await _set_context(conn, outsider)
                assert await conn.fetchval("SELECT count(*) FROM harness_sessions") == 0
            finally:
                await conn.close()

        asyncio.run(verify())
        _alembic(url, "downgrade", "0039")
        assert len(asyncio.run(_fetch(url, "SELECT state FROM harness_sessions"))) == 3
        _alembic(url, "upgrade", "head")

    _run_in_database(run)
