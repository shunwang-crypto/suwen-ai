"""0038 upgrade/downgrade and FORCE RLS with the actual application role."""

import asyncio
import os
import uuid

import pytest

from .test_migration_0012 import _alembic, _fetch, _run_in_database
from .test_migration_0036_0037 import _connect_as_app, _execute_as_migration, _set_context

pytestmark = pytest.mark.skipif(
    os.environ.get("MIGRATION_TESTS") != "1", reason="MIGRATION_TESTS=1 required"
)


def test_0038_upgrade_rls_constraints_and_downgrade():
    def run(database_url):
        _alembic(database_url, "upgrade", "0037")
        owner, outsider, quiz, question, attempt = [str(uuid.uuid4()) for _ in range(5)]

        async def seed_history():
            await _execute_as_migration(
                database_url,
                "INSERT INTO users (id,email,password_hash,role,status,guardian_consent,plan) "
                "VALUES ($1,$2,'hash','student','active',false,'free')",
                owner,
                f"{owner}@test.invalid",
            )
            await _execute_as_migration(
                database_url,
                "INSERT INTO quizzes (id,tenant_id,user_id,title,status,blueprint_json) "
                "VALUES ($1,$2,$2,'history','ready','{}')",
                quiz,
                owner,
            )
            await _execute_as_migration(
                database_url,
                "INSERT INTO questions (id,tenant_id,quiz_id,type,stem,options_json,answer,explanation,kc_ids,difficulty) "
                "VALUES ($1,$2,$3,'short','stem','[]','answer','reason','[]','mid')",
                question,
                owner,
                quiz,
            )
            await _execute_as_migration(
                database_url,
                "INSERT INTO attempts (id,tenant_id,user_id,question_id,response,is_correct) "
                "VALUES ($1,$2,$2,$3,'historical answer',false)",
                attempt,
                owner,
                question,
            )

        asyncio.run(seed_history())
        _alembic(database_url, "upgrade", "head")

        async def check_rls():
            row = await _fetch(
                database_url,
                "SELECT relrowsecurity,relforcerowsecurity FROM pg_class WHERE relname='assessment_submissions'",
            )
            assert row[0]["relrowsecurity"] and row[0]["relforcerowsecurity"]
            conn = await _connect_as_app(database_url)
            try:
                await _set_context(conn, owner)
                await conn.execute(
                    "INSERT INTO assessment_submissions "
                    "(id,tenant_id,user_id,kind,quiz_id,idempotency_key,request_fingerprint,request_json,result_json,grading_attempts) "
                    "VALUES ($1,$2,$2,'quiz',$3,'stable-key','fingerprint','{}','{}',1)",
                    str(uuid.uuid4()),
                    owner,
                    quiz,
                )
                assert await conn.fetchval("SELECT count(*) FROM assessment_submissions") == 1
                await _set_context(conn, outsider)
                assert await conn.fetchval("SELECT count(*) FROM assessment_submissions") == 0
                await _set_context(conn, "")
                assert await conn.fetchval("SELECT count(*) FROM assessment_submissions") == 0
            finally:
                await conn.close()

        asyncio.run(check_rls())
        _alembic(database_url, "downgrade", "0037")
        historical = asyncio.run(
            _fetch(database_url, "SELECT response,is_correct FROM attempts WHERE id=$1", attempt)
        )
        assert (
            historical[0]["response"] == "historical answer"
            and historical[0]["is_correct"] is False
        )
        _alembic(database_url, "upgrade", "head")

    _run_in_database(run)
