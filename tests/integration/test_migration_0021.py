"""0021 cleanup outbox schema upgrade, downgrade and from-zero replay."""

from __future__ import annotations

import asyncio
import os

import pytest

from .test_migration_0012 import _alembic, _fetch, _run_in_database

pytestmark = pytest.mark.skipif(
    os.environ.get("MIGRATION_TESTS") != "1",
    reason="set MIGRATION_TESTS=1 to run isolated PostgreSQL migration checks",
)


async def _cleanup_columns(database_url: str) -> set[str]:
    rows = await _fetch(
        database_url,
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name='pending_cleanup'",
    )
    return {row["column_name"] for row in rows}


def test_0021_pending_cleanup_upgrade_downgrade_and_replay():
    def run(database_url: str) -> None:
        _alembic(database_url, "upgrade", "0020")
        _alembic(database_url, "upgrade", "0021")
        columns = asyncio.run(_cleanup_columns(database_url))
        assert {
            "last_attempt_at",
            "next_attempt_at",
            "last_error_code",
            "last_error_text",
            "completed_at",
            "locked_at",
            "locked_by",
            "progress_json",
        } <= columns

        indexes = asyncio.run(
            _fetch(
                database_url,
                "SELECT indexname FROM pg_indexes WHERE schemaname='public' "
                "AND tablename='pending_cleanup'",
            )
        )
        index_names = {row["indexname"] for row in indexes}
        assert {
            "ix_pending_cleanup_next_attempt_at",
            "ix_pending_cleanup_last_error_code",
            "ix_pending_cleanup_locked_at",
        } <= index_names

        _alembic(database_url, "downgrade", "0020")
        assert not {
            "last_attempt_at",
            "next_attempt_at",
            "last_error_code",
            "last_error_text",
            "completed_at",
            "locked_at",
            "locked_by",
            "progress_json",
        } & asyncio.run(_cleanup_columns(database_url))

        _alembic(database_url, "upgrade", "0021")
        assert {
            "last_attempt_at",
            "next_attempt_at",
            "last_error_code",
            "last_error_text",
            "completed_at",
            "locked_at",
            "locked_by",
            "progress_json",
        } <= asyncio.run(_cleanup_columns(database_url))

        _alembic(database_url, "downgrade", "base")
        _alembic(database_url, "upgrade", "0021")
        assert {
            "last_attempt_at",
            "next_attempt_at",
            "last_error_code",
            "last_error_text",
            "completed_at",
            "locked_at",
            "locked_by",
            "progress_json",
        } <= asyncio.run(_cleanup_columns(database_url))

    _run_in_database(run)
