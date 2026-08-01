"""Isolated upgrade checks for email ownership verification."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys

import asyncpg
import pytest
from eduagents_core.config import get_settings
from sqlalchemy.engine import make_url

from .test_migration_0012 import (
    REPO_ROOT,
    _alembic,
    _fetch,
    _run_in_database,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("MIGRATION_TESTS") != "1",
    reason="set MIGRATION_TESTS=1 to run isolated PostgreSQL migration checks",
)


async def _insert_users(database_url: str, emails: tuple[str, ...]) -> None:
    source_url = make_url(get_settings().migration_database_url or get_settings().database_url)
    target_url = make_url(database_url)
    conn = await asyncpg.connect(
        user=source_url.username,
        password=source_url.password,
        host=source_url.host,
        port=source_url.port or 5432,
        database=target_url.database,
    )
    try:
        for email in emails:
            await conn.execute(
                "INSERT INTO users (id,email,password_hash,role,status,guardian_consent,plan) "
                "VALUES (gen_random_uuid(),$1,'hash','student','active',false,'free')",
                email,
            )
    finally:
        await conn.close()


def test_0013_adds_verified_timestamp_and_case_insensitive_index():
    def run(database_url: str) -> None:
        _alembic(database_url, "upgrade", "head")
        rows = asyncio.run(
            _fetch(
                database_url,
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name='users' AND column_name='email_verified_at'",
            )
        )
        assert rows
        indexes = asyncio.run(
            _fetch(
                database_url,
                "SELECT indexname FROM pg_indexes WHERE tablename='users' "
                "AND indexname='uq_users_email_lower'",
            )
        )
        assert indexes

    _run_in_database(run)


def test_0013_fails_on_historical_case_collision():
    def run(database_url: str) -> None:
        _alembic(database_url, "upgrade", "0012")

        asyncio.run(_insert_users(database_url, ("Case@Example.com", "case@example.com")))
        env = os.environ.copy()
        env["MIGRATION_DATABASE_URL"] = database_url
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0
        assert "0013 cannot normalize" in (result.stdout + result.stderr)

    _run_in_database(run)


def test_0013_marks_existing_users_historically_verified():
    def run(database_url: str) -> None:
        _alembic(database_url, "upgrade", "0012")
        asyncio.run(_insert_users(database_url, ("Legacy@Example.com",)))
        _alembic(database_url, "upgrade", "head")
        rows = asyncio.run(
            _fetch(
                database_url,
                "SELECT email, email_verified_at, created_at FROM users "
                "WHERE email = 'legacy@example.com'",
            )
        )
        assert len(rows) == 1
        assert rows[0]["email"] == "legacy@example.com"
        assert rows[0]["email_verified_at"] is not None

    _run_in_database(run)
