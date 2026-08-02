"""0018 tutor problem draft schema upgrade/downgrade coverage."""

from __future__ import annotations

import asyncio
import os

import pytest

from .test_migration_0012 import _alembic, _fetch, _run_in_database

pytestmark = pytest.mark.skipif(
    os.environ.get("MIGRATION_TESTS") != "1",
    reason="set MIGRATION_TESTS=1 to run isolated PostgreSQL migration checks",
)


def test_0018_tutor_drafts_upgrade_and_downgrade():
    def run(database_url: str) -> None:
        _alembic(database_url, "upgrade", "0017")
        _alembic(database_url, "upgrade", "0018")
        columns = asyncio.run(
            _fetch(
                database_url,
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name='tutor_problem_drafts'",
            )
        )
        names = {row["column_name"] for row in columns}
        assert {
            "resource_id",
            "selected_question_index",
            "recognized_questions_json",
            "selected_pages_json",
            "confirmed_problem_text",
            "thread_local_id",
        } <= names
        policies = asyncio.run(
            _fetch(
                database_url,
                "SELECT policyname FROM pg_policies WHERE tablename='tutor_problem_drafts'",
            )
        )
        assert {row["policyname"] for row in policies} == {
            "tenant_isolation",
            "internal_cleanup_manage",
        }
        _alembic(database_url, "downgrade", "0017")
        assert not asyncio.run(
            _fetch(
                database_url,
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_name='tutor_problem_drafts'",
            )
        )

    _run_in_database(run)
