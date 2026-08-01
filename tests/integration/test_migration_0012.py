"""Isolated migration checks for the structured resource backfill."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import uuid
from pathlib import Path

import asyncpg
import pytest
from eduagents_core.config import get_settings
from sqlalchemy.engine import make_url

pytestmark = pytest.mark.skipif(
    os.environ.get("MIGRATION_TESTS") != "1",
    reason="set MIGRATION_TESTS=1 to run isolated PostgreSQL migration checks",
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def _admin_parameters() -> dict:
    url = make_url(get_settings().migration_database_url or get_settings().database_url)
    return {
        "user": url.username,
        "password": url.password,
        "host": url.host,
        "port": url.port or 5432,
    }


async def _create_database(name: str) -> None:
    conn = await asyncpg.connect(database="postgres", **_admin_parameters())
    try:
        await conn.execute(f'CREATE DATABASE "{name}"')
    finally:
        await conn.close()


async def _drop_database(name: str) -> None:
    conn = await asyncpg.connect(database="postgres", **_admin_parameters())
    try:
        await conn.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = $1 AND pid <> pg_backend_pid()",
            name,
        )
        await conn.execute(f'DROP DATABASE IF EXISTS "{name}"')
    finally:
        await conn.close()


def _database_url(name: str) -> str:
    url = make_url(get_settings().migration_database_url or get_settings().database_url)
    return str(url.set(database=name))


def _alembic(database_url: str, *args: str) -> None:
    env = os.environ.copy()
    env["MIGRATION_DATABASE_URL"] = database_url
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        pytest.fail(
            f"alembic {' '.join(args)} failed ({result.returncode}):\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )


async def _fetch(database_url: str, query: str, *args):
    url = make_url(database_url)
    conn = await asyncpg.connect(
        user=url.username,
        password=url.password,
        host=url.host,
        port=url.port or 5432,
        database=url.database,
    )
    try:
        return await conn.fetch(query, *args)
    finally:
        await conn.close()


async def _seed_0011_fixture(database_url: str) -> dict[str, str]:
    url = make_url(database_url)
    conn = await asyncpg.connect(
        user=url.username,
        password=url.password,
        host=url.host,
        port=url.port or 5432,
        database=url.database,
    )
    ids = {name: str(uuid.uuid4()) for name in ("user", "pending_import", "confirmed_import", "confirmed_item", "shared_question", "hidden_quiz", "hidden_question", "hidden_attempt", "mastery")}
    try:
        await conn.execute(
            """
            INSERT INTO users (id, email, password_hash, role, status, guardian_consent, plan)
            VALUES ($1, $2, 'hash', 'student', 'active', false, 'free')
            """,
            ids["user"],
            f"migration-{ids['user']}@test.invalid",
        )
        for key, status in (("pending_import", "draft"), ("confirmed_import", "confirmed")):
            await conn.execute(
                """
                INSERT INTO error_imports
                    (id, tenant_id, user_id, source_type, filename, mime_type, size_bytes,
                     storage_key, draft_json, status)
                VALUES ($1, $2, $2, 'document', $3, 'text/plain', 12, $4, '{}', $5)
                """,
                ids[key],
                ids["user"],
                f"{key}.txt",
                f"{ids['user']}/error-imports/{ids[key]}/{key}.txt",
                status,
            )
        for _ in range(2):
            await conn.execute(
                """
                INSERT INTO questions
                    (id, tenant_id, quiz_id, type, stem, options_json, answer, explanation,
                     kc_ids, source_type, source_meta_json, difficulty)
                VALUES ($1, $2, NULL, 'short', '共享历史题', '[]', '答案', '', '[]',
                        'document', '{"question_no":"1"}', 'mid')
                """,
                ids["shared_question"] if _ == 0 else str(uuid.uuid4()),
                ids["user"],
            )
        # The second insert above intentionally exercises a separate ordinary
        # resource-less question; only the named question receives two ErrorItems.
        await conn.execute(
            """
            INSERT INTO questions
                (id, tenant_id, quiz_id, type, stem, options_json, answer, explanation,
                 kc_ids, source_type, source_meta_json, difficulty)
            VALUES ($1, $2, NULL, 'short', '共享历史题', '[]', '答案', '', '[]',
                    'document', '{"question_no":"1"}', 'mid')
            ON CONFLICT (id) DO NOTHING
            """,
            ids["shared_question"],
            ids["user"],
        )
        await conn.execute(
            """
            INSERT INTO error_items
                (id, tenant_id, user_id, question_id, kc_ids, cause, state,
                 verification_status, source_import_id, original_response, fsrs_card_json)
            VALUES ($1, $2, $2, $3, '[]', 'knowledge_gap', 'new', 'verified', $4,
                    '来源中的原始答案', '{"stability":2.0}')
            """,
            ids["confirmed_item"],
            ids["user"],
            ids["shared_question"],
            ids["confirmed_import"],
        )
        second_item = str(uuid.uuid4())
        await conn.execute(
            """
            INSERT INTO error_items
                (id, tenant_id, user_id, question_id, kc_ids, cause, state,
                 verification_status, source_import_id, original_response, fsrs_card_json)
            VALUES ($1, $2, $2, $3, '[]', 'knowledge_gap', 'new', 'verified', $4,
                    '来源中的原始答案', '{"stability":3.0}')
            """,
            second_item,
            ids["user"],
            ids["shared_question"],
            ids["confirmed_import"],
        )
        await conn.execute(
            "UPDATE error_imports SET confirmed_error_item_id = $1 WHERE id = $2",
            ids["confirmed_item"],
            ids["confirmed_import"],
        )
        await conn.execute(
            """
            INSERT INTO attempts (id, tenant_id, user_id, question_id, response, is_correct)
            VALUES ($1, $2, $2, $3, '来源中的原始答案', false)
            """,
            str(uuid.uuid4()),
            ids["user"],
            ids["shared_question"],
        )
        await conn.execute(
            """
            INSERT INTO mastery_log
                (id, tenant_id, user_id, kc_id, old_p, new_p, evidence_type, evidence_id, weight)
            VALUES ($1, $2, $2, $1, 0.2, 0.3, 'quiz_attempt', NULL, 1.0)
            """,
            ids["mastery"],
            ids["user"],
        )
        await conn.execute(
            """
            INSERT INTO quizzes (id, tenant_id, user_id, title, blueprint_json, status)
            VALUES ($1, $2, $2, '历史隐藏卷', '{"kind":"error_import"}', 'ready')
            """,
            ids["hidden_quiz"],
            ids["user"],
        )
        await conn.execute(
            """
            INSERT INTO questions
                (id, tenant_id, quiz_id, type, stem, options_json, answer, explanation,
                 kc_ids, source_type, source_meta_json, difficulty)
            VALUES ($1, $2, $3, 'short', '没有导入行的隐藏题', '[]', '答案', '', '[]',
                    'document', '{}', 'mid')
            """,
            ids["hidden_question"],
            ids["user"],
            ids["hidden_quiz"],
        )
        await conn.execute(
            """
            INSERT INTO attempts (id, tenant_id, user_id, question_id, response, is_correct)
            VALUES ($1, $2, $2, $3, '历史测验答案', true)
            """,
            ids["hidden_attempt"],
            ids["user"],
            ids["hidden_question"],
        )
    finally:
        await conn.close()
    return ids


def _run_in_database(callback) -> None:
    name = f"eduagents_migration_test_{uuid.uuid4().hex[:12]}"
    asyncio.run(_create_database(name))
    database_url = _database_url(name)
    try:
        callback(database_url)
    finally:
        asyncio.run(_drop_database(name))


def test_0012_upgrade_paths_and_historical_backfill():
    def run(database_url: str) -> None:
        _alembic(database_url, "upgrade", "head")
        tables = asyncio.run(
            _fetch(
                database_url,
                "SELECT to_regclass('learning_resources') AS resources, "
                "to_regclass('assessment_answers') AS answers",
            )
        )
        assert tables[0]["resources"] == "learning_resources"
        assert tables[0]["answers"] == "assessment_answers"
        _alembic(database_url, "downgrade", "0011")
        _alembic(database_url, "upgrade", "head")

        _alembic(database_url, "downgrade", "base")
        _alembic(database_url, "upgrade", "0010")
        _alembic(database_url, "upgrade", "head")

    _run_in_database(run)

    def run_fixture(database_url: str) -> None:
        _alembic(database_url, "upgrade", "0011")
        ids = asyncio.run(_seed_0011_fixture(database_url))
        _alembic(database_url, "upgrade", "head")
        rows = asyncio.run(
            _fetch(
                database_url,
                """
                SELECT
                    (SELECT count(*) FROM learning_resources) AS resources,
                    (SELECT count(*) FROM resource_questions) AS resource_questions,
                    (SELECT count(*) FROM quizzes WHERE blueprint_json->>'kind' = 'error_import') AS hidden_quizzes,
                    (SELECT count(*) FROM attempts) AS attempts,
                    (SELECT count(*) FROM mastery_log WHERE id = $1) AS mastery,
                    (SELECT count(*) FROM error_items WHERE resource_question_id IS NOT NULL) AS linked_errors,
                    (SELECT count(*) FROM error_items WHERE original_response = '来源中的原始答案') AS copied_answer,
                    (SELECT count(*) FROM questions WHERE id = $2 AND quiz_id IS NULL) AS detached_question,
                    (SELECT resource_id FROM error_imports WHERE id = $3) AS linked_resource
                """,
                ids["mastery"],
                ids["hidden_question"],
                ids["confirmed_import"],
            )
        )
        row = rows[0]
        assert row["resources"] == 3
        assert row["resource_questions"] == 2
        assert row["hidden_quizzes"] == 0
        assert row["attempts"] == 2
        assert row["mastery"] == 1
        assert row["linked_errors"] == 2
        assert row["copied_answer"] == 2
        assert row["detached_question"] == 1
        assert str(row["linked_resource"]) == ids["confirmed_import"]

    _run_in_database(run_fixture)
