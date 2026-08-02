"""0017 BYOK schema upgrade/downgrade coverage on isolated PostgreSQL."""

from __future__ import annotations

import asyncio
import os

import pytest

from .test_migration_0012 import _alembic, _fetch, _run_in_database

pytestmark = pytest.mark.skipif(
    os.environ.get("MIGRATION_TESTS") != "1",
    reason="set MIGRATION_TESTS=1 to run isolated PostgreSQL migration checks",
)


def test_0017_credentials_and_usage_audit_columns_upgrade_and_downgrade():
    def run(database_url: str) -> None:
        _alembic(database_url, "upgrade", "0016")
        _alembic(database_url, "upgrade", "0017")
        table = asyncio.run(
            _fetch(
                database_url,
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_name='user_model_credentials'",
            )
        )
        assert table
        columns = asyncio.run(
            _fetch(
                database_url,
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name IN ('user_model_credentials','usage_log') "
                "AND column_name IN ('encrypted_api_key','encryption_nonce','credential_source',"
                "'provider','platform_billed_cost_cny')",
            )
        )
        assert {row["column_name"] for row in columns} == {
            "encrypted_api_key",
            "encryption_nonce",
            "credential_source",
            "provider",
            "platform_billed_cost_cny",
        }
        constraints = asyncio.run(
            _fetch(
                database_url,
                "SELECT constraint_name, delete_rule FROM information_schema.referential_constraints "
                "WHERE constraint_name='fk_user_model_credentials_user'",
            )
        )
        assert constraints[0]["delete_rule"] == "CASCADE"
        policies = asyncio.run(
            _fetch(
                database_url,
                "SELECT policyname FROM pg_policies WHERE tablename='user_model_credentials'",
            )
        )
        assert {row["policyname"] for row in policies} >= {
            "credential_owner_all",
            "credential_admin_read",
        }

        _alembic(database_url, "downgrade", "0016")
        assert not asyncio.run(
            _fetch(
                database_url,
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_name='user_model_credentials'",
            )
        )
        usage_columns = asyncio.run(
            _fetch(
                database_url,
                "SELECT column_name FROM information_schema.columns WHERE table_name='usage_log' "
                "AND column_name='credential_source'",
            )
        )
        assert not usage_columns

    _run_in_database(run)
