"""0029 per-field resource question confidence migration checks."""

from __future__ import annotations

import asyncio
import os

import pytest

from .test_migration_0012 import _alembic, _fetch, _run_in_database

pytestmark = pytest.mark.skipif(
    os.environ.get("MIGRATION_TESTS") != "1",
    reason="set MIGRATION_TESTS=1 to run isolated PostgreSQL migration checks",
)


def test_0029_upgrade_downgrade_upgrade_preserves_previous_schema():
    def run(database_url: str) -> None:
        _alembic(database_url, "upgrade", "0028")
        before = asyncio.run(
            _fetch(
                database_url,
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name='resource_questions' AND column_name='field_confidence_json'",
            )
        )
        assert before == []

        _alembic(database_url, "upgrade", "0029")
        after = asyncio.run(
            _fetch(
                database_url,
                "SELECT column_name, is_nullable, column_default "
                "FROM information_schema.columns "
                "WHERE table_name='resource_questions' AND column_name='field_confidence_json'",
            )
        )
        assert len(after) == 1
        assert after[0]["is_nullable"] == "NO"
        assert "{}" in after[0]["column_default"]

        _alembic(database_url, "downgrade", "0028")
        assert asyncio.run(
            _fetch(
                database_url,
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name='resource_questions' AND column_name='field_confidence_json'",
            )
        ) == []
        _alembic(database_url, "upgrade", "0029")

    _run_in_database(run)
