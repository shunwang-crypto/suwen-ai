"""Isolated upgrade/downgrade checks for executable plan resources."""

from __future__ import annotations

import asyncio
import os

import pytest

from .test_migration_0012 import _alembic, _fetch, _run_in_database

pytestmark = pytest.mark.skipif(
    os.environ.get("MIGRATION_TESTS") != "1",
    reason="set MIGRATION_TESTS=1 to run isolated PostgreSQL migration checks",
)


def test_0016_upgrade_adds_plan_binding_and_resource_table_then_downgrades():
    def run(database_url: str) -> None:
        _alembic(database_url, "upgrade", "0015")
        _alembic(database_url, "upgrade", "0016")
        columns = asyncio.run(
            _fetch(
                database_url,
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name='plans' AND column_name='kb_id'",
            )
        )
        assert columns
        table = asyncio.run(
            _fetch(
                database_url,
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_name='plan_resources'",
            )
        )
        assert table
        fks = asyncio.run(
            _fetch(
                database_url,
                "SELECT constraint_name, delete_rule FROM information_schema.referential_constraints "
                "WHERE constraint_name IN ('fk_plans_kb_id','fk_plan_resources_kb','fk_plan_resources_plan')",
            )
        )
        rules = {row["constraint_name"]: row["delete_rule"] for row in fks}
        assert rules["fk_plans_kb_id"] == "SET NULL"
        assert rules["fk_plan_resources_kb"] == "SET NULL"
        assert rules["fk_plan_resources_plan"] == "CASCADE"

        _alembic(database_url, "downgrade", "0015")
        assert not asyncio.run(
            _fetch(
                database_url,
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_name='plan_resources'",
            )
        )
        assert not asyncio.run(
            _fetch(
                database_url,
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name='plans' AND column_name='kb_id'",
            )
        )

    _run_in_database(run)
