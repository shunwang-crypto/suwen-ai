"""0020 metacognition tables upgrade/downgrade/from-zero coverage."""

from __future__ import annotations

import asyncio
import os

import pytest

from .test_migration_0012 import _alembic, _fetch, _run_in_database

pytestmark = pytest.mark.skipif(
    os.environ.get("MIGRATION_TESTS") != "1",
    reason="set MIGRATION_TESTS=1 to run isolated PostgreSQL migration checks",
)


def test_0020_metacognition_upgrade_downgrade_and_replay():
    def run(database_url: str) -> None:
        _alembic(database_url, "upgrade", "0019")
        _alembic(database_url, "upgrade", "0020")
        tables = asyncio.run(
            _fetch(
                database_url,
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema='public' AND table_name IN "
                "('metacognition_predictions', 'explain_backs') ORDER BY table_name",
            )
        )
        assert [row["table_name"] for row in tables] == [
            "explain_backs",
            "metacognition_predictions",
        ]
        policies = asyncio.run(
            _fetch(
                database_url,
                "SELECT tablename, policyname FROM pg_policies WHERE schemaname='public' "
                "AND tablename IN ('metacognition_predictions', 'explain_backs')",
            )
        )
        assert {(row["tablename"], row["policyname"]) for row in policies} == {
            ("metacognition_predictions", "tenant_isolation"),
            ("explain_backs", "tenant_isolation"),
        }

        _alembic(database_url, "downgrade", "0019")
        assert not asyncio.run(
            _fetch(
                database_url,
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema='public' AND table_name IN "
                "('metacognition_predictions', 'explain_backs')",
            )
        )

        _alembic(database_url, "downgrade", "base")
        _alembic(database_url, "upgrade", "0020")
        replayed = asyncio.run(
            _fetch(
                database_url,
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema='public' AND table_name IN "
                "('metacognition_predictions', 'explain_backs')",
            )
        )
        assert {row["table_name"] for row in replayed} == {
            "metacognition_predictions",
            "explain_backs",
        }

    _run_in_database(run)
