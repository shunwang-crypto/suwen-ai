"""0022 learning-event schema upgrade/downgrade coverage."""

from __future__ import annotations

import asyncio
import os

import pytest

from .test_migration_0012 import _alembic, _fetch, _run_in_database

pytestmark = pytest.mark.skipif(
    os.environ.get("MIGRATION_TESTS") != "1",
    reason="set MIGRATION_TESTS=1 to run isolated PostgreSQL migration checks",
)


def test_0022_learning_events_upgrade_downgrade_and_rls():
    def run(database_url: str) -> None:
        _alembic(database_url, "upgrade", "0021")
        _alembic(database_url, "upgrade", "0022")

        tables = asyncio.run(
            _fetch(
                database_url,
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema='public' AND table_name IN "
                "('fsrs_review_events', 'learning_events') ORDER BY table_name",
            )
        )
        assert [row["table_name"] for row in tables] == [
            "fsrs_review_events",
            "learning_events",
        ]

        columns = asyncio.run(
            _fetch(
                database_url,
                "SELECT table_name, column_name FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name IN "
                "('fsrs_review_events', 'learning_events') "
                "ORDER BY table_name, ordinal_position",
            )
        )
        by_table: dict[str, set[str]] = {}
        for row in columns:
            by_table.setdefault(row["table_name"], set()).add(row["column_name"])
        assert {
            "tenant_id",
            "user_id",
            "error_item_id",
            "rating",
            "correct",
            "reviewed_at",
            "review_duration_ms",
            "card_before_json",
            "card_after_json",
            "review_log_json",
            "params_version",
            "idempotency_key",
        } <= by_table["fsrs_review_events"]
        assert {
            "tenant_id",
            "user_id",
            "source_type",
            "source_id",
            "question_id",
            "item_key",
            "kc_ids_json",
            "correct",
            "score",
            "rating",
            "difficulty_label",
            "occurred_at",
            "idempotency_key",
        } <= by_table["learning_events"]

        policies = asyncio.run(
            _fetch(
                database_url,
                "SELECT tablename, policyname FROM pg_policies "
                "WHERE schemaname='public' AND tablename IN "
                "('fsrs_review_events', 'learning_events')",
            )
        )
        assert {(row["tablename"], row["policyname"]) for row in policies} == {
            ("fsrs_review_events", "tenant_isolation"),
            ("learning_events", "tenant_isolation"),
        }

        _alembic(database_url, "downgrade", "0021")
        assert not asyncio.run(
            _fetch(
                database_url,
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema='public' AND table_name IN "
                "('fsrs_review_events', 'learning_events')",
            )
        )

    _run_in_database(run)
