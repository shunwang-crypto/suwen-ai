"""0023-0027 schema-chain and compatibility coverage."""

from __future__ import annotations

import asyncio
import os

import pytest

from .test_migration_0012 import _alembic, _fetch, _run_in_database

pytestmark = pytest.mark.skipif(
    os.environ.get("MIGRATION_TESTS") != "1",
    reason="set MIGRATION_TESTS=1 to run isolated PostgreSQL migration checks",
)


_ERROR_LINK_COLUMNS = {
    "source_attempt_id",
    "confidence_at_error",
    "confidence_probability_at_error",
    "confidence_scale_version",
}


async def _columns(database_url: str, table: str) -> set[str]:
    rows = await _fetch(
        database_url,
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name=$1",
        table,
    )
    return {row["column_name"] for row in rows}


def test_0023_0024_policy_and_error_linkage_replay_paths():
    def run(database_url: str) -> None:
        _alembic(database_url, "upgrade", "0023")

        # 0001 uses the current ORM metadata for its frozen core tables.  The
        # new B2 columns therefore already exist on this from-zero path.
        assert _ERROR_LINK_COLUMNS <= asyncio.run(_columns(database_url, "error_items"))
        policies = asyncio.run(
            _fetch(
                database_url,
                "SELECT policyname FROM pg_policies "
                "WHERE schemaname='public' AND tablename='knowledge_bases' "
                "AND policyname='vector_audit_admin_select'",
            )
        )
        assert len(policies) == 1

        _alembic(database_url, "upgrade", "0024")
        constraints = asyncio.run(
            _fetch(
                database_url,
                "SELECT conname FROM pg_constraint "
                "WHERE conrelid='public.error_items'::regclass "
                "AND conname IN ("
                "'uq_error_items_source_attempt_id',"
                "'ck_error_items_confidence_at_error',"
                "'ck_error_items_confidence_probability_at_error'"
                ")",
            )
        )
        assert {row["conname"] for row in constraints} == {
            "uq_error_items_source_attempt_id",
            "ck_error_items_confidence_at_error",
            "ck_error_items_confidence_probability_at_error",
        }
        # On the from-zero path the ORM has already created a semantically
        # equivalent, unnamed FK.  0024 intentionally keeps it instead of
        # adding a second FK with its migration name.
        foreign_keys = asyncio.run(
            _fetch(
                database_url,
                "SELECT pg_get_constraintdef(oid) AS definition "
                "FROM pg_constraint "
                "WHERE conrelid='public.error_items'::regclass "
                "AND contype='f' "
                "AND confrelid='public.attempts'::regclass",
            )
        )
        assert any("source_attempt_id" in row["definition"] for row in foreign_keys)

        # The downgrade removes the columns.  Re-upgrading then exercises the
        # legacy database path where 0024 must add them itself.
        _alembic(database_url, "downgrade", "0023")
        assert not _ERROR_LINK_COLUMNS & asyncio.run(_columns(database_url, "error_items"))
        _alembic(database_url, "upgrade", "0024")
        assert _ERROR_LINK_COLUMNS <= asyncio.run(_columns(database_url, "error_items"))

    _run_in_database(run)


def test_0025_0027_upgrade_downgrade_and_rls():
    def run(database_url: str) -> None:
        _alembic(database_url, "upgrade", "0024")
        _alembic(database_url, "upgrade", "0025")

        table = asyncio.run(
            _fetch(
                database_url,
                "SELECT to_regclass('public.fsrs_params') AS table_name",
            )
        )
        assert table[0]["table_name"] == "fsrs_params"
        fsrs_columns = asyncio.run(_columns(database_url, "fsrs_params"))
        assert {
            "tenant_id",
            "user_id",
            "algorithm",
            "params_version",
            "params_json",
            "training_review_count",
            "valid_cross_day_review_count",
            "status",
            "fit_metrics_json",
            "activated_at",
            "retired_at",
        } <= fsrs_columns
        rls = asyncio.run(
            _fetch(
                database_url,
                "SELECT relrowsecurity, relforcerowsecurity "
                "FROM pg_class WHERE oid='public.fsrs_params'::regclass",
            )
        )
        assert (rls[0]["relrowsecurity"], rls[0]["relforcerowsecurity"]) == (True, True)
        policies = asyncio.run(
            _fetch(
                database_url,
                "SELECT policyname FROM pg_policies "
                "WHERE schemaname='public' AND tablename='fsrs_params'",
            )
        )
        assert {row["policyname"] for row in policies} == {"tenant_isolation"}

        _alembic(database_url, "upgrade", "0027")
        explain_columns = asyncio.run(_columns(database_url, "explain_backs"))
        assert {
            "source_attempt_id",
            "question_id",
            "rubric_version",
            "score_json",
            "quality_shadow",
        } <= explain_columns
        explain_constraints = asyncio.run(
            _fetch(
                database_url,
                "SELECT conname FROM pg_constraint "
                "WHERE conrelid='public.explain_backs'::regclass "
                "AND conname IN ("
                "'fk_explain_backs_source_attempt_id',"
                "'fk_explain_backs_question_id',"
                "'ck_explain_backs_score_json_object'"
                ")",
            )
        )
        assert {row["conname"] for row in explain_constraints} == {
            "fk_explain_backs_source_attempt_id",
            "fk_explain_backs_question_id",
            "ck_explain_backs_score_json_object",
        }

        _alembic(database_url, "downgrade", "0025")
        assert not {
            "source_attempt_id",
            "question_id",
            "rubric_version",
            "score_json",
            "quality_shadow",
        } & asyncio.run(_columns(database_url, "explain_backs"))
        _alembic(database_url, "downgrade", "0024")
        assert not asyncio.run(
            _fetch(
                database_url,
                "SELECT to_regclass('public.fsrs_params') AS table_name",
            )
        )[0]["table_name"]

    _run_in_database(run)
