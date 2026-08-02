"""0019 document recognition metadata upgrade/downgrade coverage."""

from __future__ import annotations

import asyncio
import os

import pytest

from .test_migration_0012 import _alembic, _fetch, _run_in_database

pytestmark = pytest.mark.skipif(
    os.environ.get("MIGRATION_TESTS") != "1",
    reason="set MIGRATION_TESTS=1 to run isolated PostgreSQL migration checks",
)


def test_0019_document_recognition_metadata_upgrade_and_downgrade():
    def run(database_url: str) -> None:
        _alembic(database_url, "upgrade", "0018")
        _alembic(database_url, "upgrade", "0019")
        columns = asyncio.run(
            _fetch(
                database_url,
                "SELECT column_name, is_nullable, column_default "
                "FROM information_schema.columns "
                "WHERE table_name='documents' AND column_name='recognition_meta_json'",
            )
        )
        assert len(columns) == 1
        assert columns[0]["is_nullable"] == "NO"
        assert "jsonb" in columns[0]["column_default"]

        _alembic(database_url, "downgrade", "0018")
        assert not asyncio.run(
            _fetch(
                database_url,
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name='documents' AND column_name='recognition_meta_json'",
            )
        )

        # 0001 creates the frozen documents table from current ORM metadata,
        # which already includes this future column.  Replaying from zero
        # must therefore normalize it instead of trying to add it twice.
        _alembic(database_url, "downgrade", "base")
        _alembic(database_url, "upgrade", "0019")
        replayed = asyncio.run(
            _fetch(
                database_url,
                "SELECT column_name, is_nullable, column_default "
                "FROM information_schema.columns "
                "WHERE table_name='documents' AND column_name='recognition_meta_json'",
            )
        )
        assert len(replayed) == 1
        assert replayed[0]["is_nullable"] == "NO"
        assert "jsonb" in replayed[0]["column_default"]

    _run_in_database(run)
