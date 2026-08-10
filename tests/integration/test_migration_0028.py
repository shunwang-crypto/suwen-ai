"""0028 KnowledgeComponent provenance and stable-key schema coverage."""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, datetime

import asyncpg
import pytest
from sqlalchemy.engine import make_url

from .test_migration_0012 import _alembic, _fetch, _run_in_database

pytestmark = pytest.mark.skipif(
    os.environ.get("MIGRATION_TESTS") != "1",
    reason="set MIGRATION_TESTS=1 to run isolated PostgreSQL migration checks",
)


_KC_COLUMNS = {"source_kind", "stable_key", "superseded_by_id"}
_KC_CONSTRAINTS = {
    "ck_knowledge_components_source_kind",
    "fk_knowledge_components_superseded_by_id",
}
_KC_INDEXES = {
    "uq_knowledge_components_kb_stable_key",
    "ix_knowledge_components_superseded_by_id",
}


async def _columns(database_url: str) -> set[str]:
    rows = await _fetch(
        database_url,
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name='knowledge_components'",
    )
    return {row["column_name"] for row in rows}


async def _seed_user_and_kb(database_url: str) -> tuple[str, str]:
    user_id = str(uuid.uuid4())
    kb_id = str(uuid.uuid4())
    url = make_url(database_url)
    conn = await asyncpg.connect(
        user=url.username,
        password=url.password,
        host=url.host,
        port=url.port or 5432,
        database=url.database,
    )
    try:
        await conn.execute(
            "INSERT INTO users (id, email, password_hash, role, status, guardian_consent, plan) "
            "VALUES ($1, $2, 'migration-hash', 'student', 'active', false, 'free')",
            user_id,
            f"migration-{user_id}@test.invalid",
        )
        await conn.execute(
            "INSERT INTO knowledge_bases "
            "(id, tenant_id, owner_type, name, subject, kb_version, config_json) "
            "VALUES ($1, $2, 'user', 'migration-kb', 'math', 1, '{}'::jsonb)",
            kb_id,
            user_id,
        )
    finally:
        await conn.close()
    return user_id, kb_id


async def _assert_edu_app_can_write_new_columns(
    database_url: str, *, tenant_id: str, kb_id: str
) -> None:
    """Exercise the unchanged 0001 tenant policy with the new columns."""

    url = make_url(database_url)
    conn = await asyncpg.connect(
        user="edu_app",
        password="edu",
        host=url.host,
        port=url.port or 5432,
        database=url.database,
    )
    kc_id = str(uuid.uuid4())
    try:
        await conn.execute("SELECT set_config('app.tenant_id', $1, false)", tenant_id)
        await conn.execute(
            "INSERT INTO knowledge_components "
            "(id, tenant_id, kb_id, canonical_name, aliases, description, stable_key) "
            "VALUES ($1, $2, $3, 'rls-kc', '[]'::jsonb, '', 'chapter:rls')",
            kc_id,
            tenant_id,
            kb_id,
        )
        row = await conn.fetchrow(
            "SELECT source_kind, stable_key FROM knowledge_components WHERE id=$1", kc_id
        )
        assert (row["source_kind"], row["stable_key"]) == ("enrichment", "chapter:rls")
    finally:
        await conn.close()


async def _insert_kc(
    database_url: str,
    *,
    tenant_id: str,
    kb_id: str,
    kc_id: str | None = None,
    name: str = "chapter",
    source_kind: str | None = None,
    stable_key: str | None = None,
    superseded_by_id: str | None = None,
) -> str:
    kc_id = kc_id or str(uuid.uuid4())
    source_kind = source_kind or "enrichment"
    url = make_url(database_url)
    conn = await asyncpg.connect(
        user=url.username,
        password=url.password,
        host=url.host,
        port=url.port or 5432,
        database=url.database,
    )
    try:
        await conn.execute(
            "INSERT INTO knowledge_components "
            "(id, tenant_id, kb_id, canonical_name, aliases, description, source_kind, "
            "stable_key, superseded_by_id) "
            "VALUES ($1, $2, $3, $4, '[]'::jsonb, '', $5, $6, $7)",
            kc_id,
            tenant_id,
            kb_id,
            name,
            source_kind,
            stable_key,
            superseded_by_id,
        )
    finally:
        await conn.close()
    return kc_id


async def _expect_database_error(database_url: str, query: str, *args) -> None:
    url = make_url(database_url)
    conn = await asyncpg.connect(
        user=url.username,
        password=url.password,
        host=url.host,
        port=url.port or 5432,
        database=url.database,
    )
    try:
        with pytest.raises(asyncpg.PostgresError):
            await conn.execute(query, *args)
    finally:
        await conn.close()


async def _exercise_superseded_delete(
    database_url: str, *, tenant_id: str, source_id: str, target_id: str
) -> str:
    url = make_url(database_url)
    conn = await asyncpg.connect(
        user=url.username,
        password=url.password,
        host=url.host,
        port=url.port or 5432,
        database=url.database,
    )
    question_id = str(uuid.uuid4())
    try:
        await conn.execute(
            "INSERT INTO mastery_records "
            "(id, tenant_id, user_id, kc_id, p_mastery, confidence, fsrs_card_json) "
            "VALUES ($1, $2, $2, $3, 0.3, 0.5, '{}'::jsonb)",
            str(uuid.uuid4()),
            tenant_id,
            source_id,
        )
        await conn.execute(
            "INSERT INTO questions "
            "(id, tenant_id, quiz_id, type, stem, options_json, answer, explanation, kc_ids, "
            "source_type, source_meta_json, difficulty) "
            "VALUES ($1, $2, NULL, 'short', 'source-question', '[]'::jsonb, 'answer', '', $3::jsonb, "
            "'generated', '{}'::jsonb, 'mid')",
            question_id,
            tenant_id,
            f"[{source_id!r}]".replace("'", '"'),
        )
        await conn.execute("DELETE FROM knowledge_components WHERE id = $1", target_id)
    finally:
        await conn.close()
    return question_id


async def _seed_historical_rows(database_url: str) -> dict[str, str]:
    user_id, kb_id = await _seed_user_and_kb(database_url)
    kc_id = str(uuid.uuid4())
    quiz_id = str(uuid.uuid4())
    question_id = str(uuid.uuid4())
    event_id = str(uuid.uuid4())
    url = make_url(database_url)
    conn = await asyncpg.connect(
        user=url.username,
        password=url.password,
        host=url.host,
        port=url.port or 5432,
        database=url.database,
    )
    try:
        await conn.execute(
            "INSERT INTO knowledge_components "
            "(id, tenant_id, kb_id, canonical_name, aliases, description) "
            "VALUES ($1, $2, $3, 'historical-kc', '[]'::jsonb, '')",
            kc_id,
            user_id,
            kb_id,
        )
        await conn.execute(
            "INSERT INTO mastery_records "
            "(id, tenant_id, user_id, kc_id, p_mastery, confidence, fsrs_card_json) "
            "VALUES ($1, $2, $2, $3, 0.2, 0.4, '{}'::jsonb)",
            str(uuid.uuid4()),
            user_id,
            kc_id,
        )
        await conn.execute(
            "INSERT INTO quizzes (id, tenant_id, user_id, title, blueprint_json, status) "
            "VALUES ($1, $2, $2, 'historical-quiz', '{}'::jsonb, 'ready')",
            quiz_id,
            user_id,
        )
        await conn.execute(
            "INSERT INTO questions "
            "(id, tenant_id, quiz_id, type, stem, options_json, answer, explanation, kc_ids, "
            "source_type, source_meta_json, difficulty) "
            "VALUES ($1, $2, $3, 'short', 'historical-question', '[]'::jsonb, 'answer', '', $4::jsonb, "
            "'generated', '{}'::jsonb, 'mid')",
            question_id,
            user_id,
            quiz_id,
            f"[{kc_id!r}]".replace("'", '"'),
        )
        await conn.execute(
            "INSERT INTO learning_events "
            "(id, tenant_id, user_id, source_type, source_id, question_id, item_key, kc_ids_json, "
            "correct, score, difficulty_label, occurred_at, idempotency_key) "
            "VALUES ($1, $2, $2, 'quiz_attempt', $3, $4, 'historical', $5::jsonb, false, 0, 'mid', $6, $7)",
            event_id,
            user_id,
            str(uuid.uuid4()),
            question_id,
            f"[{kc_id!r}]".replace("'", '"'),
            datetime.now(UTC),
            f"historical-{event_id}",
        )
    finally:
        await conn.close()
    return {
        "user_id": user_id,
        "kb_id": kb_id,
        "kc_id": kc_id,
        "question_id": question_id,
        "event_id": event_id,
    }


async def _assert_schema(database_url: str) -> None:
    assert _KC_COLUMNS <= await _columns(database_url)

    constraints = await _fetch(
        database_url,
        "SELECT conname, contype, pg_get_constraintdef(oid) AS definition "
        "FROM pg_constraint WHERE conrelid='public.knowledge_components'::regclass",
    )
    by_name = {row["conname"]: row for row in constraints}
    assert _KC_CONSTRAINTS <= by_name.keys()
    assert "source_kind" in by_name["ck_knowledge_components_source_kind"]["definition"]
    assert "'enrichment'" in by_name["ck_knowledge_components_source_kind"]["definition"]
    assert "'chapter_fallback'" in by_name["ck_knowledge_components_source_kind"]["definition"]
    assert "ON DELETE SET NULL" in by_name["fk_knowledge_components_superseded_by_id"]["definition"]

    indexes = await _fetch(
        database_url,
        "SELECT indexname, indexdef FROM pg_indexes "
        "WHERE schemaname='public' AND tablename='knowledge_components'",
    )
    index_defs = {row["indexname"]: row["indexdef"] for row in indexes}
    assert _KC_INDEXES <= index_defs.keys()
    assert "UNIQUE" in index_defs["uq_knowledge_components_kb_stable_key"]
    assert "stable_key IS NOT NULL" in index_defs["uq_knowledge_components_kb_stable_key"]

    rls = await _fetch(
        database_url,
        "SELECT c.relrowsecurity, c.relforcerowsecurity, p.policyname, "
        "p.qual, p.with_check "
        "FROM pg_class c JOIN pg_policies p ON p.tablename='knowledge_components' "
        "WHERE c.oid='public.knowledge_components'::regclass "
        "AND p.schemaname='public'",
    )
    assert len(rls) == 1
    assert (rls[0]["relrowsecurity"], rls[0]["relforcerowsecurity"]) == (True, True)
    assert rls[0]["policyname"] == "tenant_isolation"
    assert "tenant_id" in rls[0]["qual"]
    assert "tenant_id" in rls[0]["with_check"]


def test_0028_from_zero_legacy_upgrade_constraints_rls_and_downgrade():
    def run(database_url: str) -> None:
        # Empty database: 0001 may pre-create the new ORM columns, so 0028
        # must still be safe and leave the complete schema behind.
        _alembic(database_url, "upgrade", "head")
        asyncio.run(_assert_schema(database_url))
        user_id, kb_id = asyncio.run(_seed_user_and_kb(database_url))
        asyncio.run(
            _assert_edu_app_can_write_new_columns(
                database_url, tenant_id=user_id, kb_id=kb_id
            )
        )

        target_id = asyncio.run(
            _insert_kc(
                database_url,
                tenant_id=user_id,
                kb_id=kb_id,
                name="target",
                source_kind="enrichment",
                stable_key="chapter:target",
            )
        )
        source_id = asyncio.run(
            _insert_kc(
                database_url,
                tenant_id=user_id,
                kb_id=kb_id,
                name="source",
                source_kind="chapter_fallback",
                stable_key="chapter:source",
                superseded_by_id=target_id,
            )
        )
        asyncio.run(
            _insert_kc(
                database_url,
                tenant_id=user_id,
                kb_id=kb_id,
                name="null-key-a",
            )
        )
        asyncio.run(
            _insert_kc(
                database_url,
                tenant_id=user_id,
                kb_id=kb_id,
                name="null-key-b",
            )
        )
        asyncio.run(
            _expect_database_error(
                database_url,
                "INSERT INTO knowledge_components "
                "(id, tenant_id, kb_id, canonical_name, aliases, description, source_kind, stable_key) "
                "VALUES ($1, $2, $3, 'duplicate', '[]'::jsonb, '', 'enrichment', 'chapter:source')",
                str(uuid.uuid4()),
                user_id,
                kb_id,
            )
        )
        asyncio.run(
            _expect_database_error(
                database_url,
                "INSERT INTO knowledge_components "
                "(id, tenant_id, kb_id, canonical_name, aliases, description, source_kind) "
                "VALUES ($1, $2, $3, 'invalid', '[]'::jsonb, '', 'not-a-source')",
                str(uuid.uuid4()),
                user_id,
                kb_id,
            )
        )

        # A target KC can disappear without deleting the source KC or the
        # historical rows that point at the source KC.
        question_id = asyncio.run(
            _exercise_superseded_delete(
                database_url,
                tenant_id=user_id,
                source_id=source_id,
                target_id=target_id,
            )
        )

        rows = asyncio.run(
            _fetch(
                database_url,
                "SELECT superseded_by_id FROM knowledge_components WHERE id=$1",
                source_id,
            )
        )
        assert rows[0]["superseded_by_id"] is None
        assert asyncio.run(
            _fetch(database_url, "SELECT count(*) AS n FROM mastery_records WHERE kc_id=$1", source_id)
        )[0]["n"] == 1
        assert asyncio.run(
            _fetch(database_url, "SELECT count(*) AS n FROM questions WHERE id=$1", question_id)
        )[0]["n"] == 1

        # RLS metadata and actual read/write of the new columns use the policy
        # already installed by 0001; no replacement policy is introduced.
        _alembic(database_url, "downgrade", "0027")
        assert not _KC_COLUMNS & asyncio.run(_columns(database_url))
        legacy = asyncio.run(_seed_historical_rows(database_url))
        _alembic(database_url, "upgrade", "head")
        asyncio.run(_assert_schema(database_url))

        migrated = asyncio.run(
            _fetch(
                database_url,
                "SELECT source_kind, stable_key FROM knowledge_components WHERE id=$1",
                legacy["kc_id"],
            )
        )
        assert (migrated[0]["source_kind"], migrated[0]["stable_key"]) == ("enrichment", None)
        assert asyncio.run(
            _fetch(
                database_url,
                "SELECT count(*) AS n FROM mastery_records WHERE kc_id=$1",
                legacy["kc_id"],
            )
        )[0]["n"] == 1
        assert asyncio.run(
            _fetch(
                database_url,
                "SELECT count(*) AS n FROM learning_events WHERE id=$1",
                legacy["event_id"],
            )
        )[0]["n"] == 1

        _alembic(database_url, "downgrade", "0027")
        assert not _KC_COLUMNS & asyncio.run(_columns(database_url))
        assert asyncio.run(
            _fetch(database_url, "SELECT count(*) AS n FROM questions WHERE id=$1", legacy["question_id"])
        )[0]["n"] == 1
        assert asyncio.run(
            _fetch(database_url, "SELECT count(*) AS n FROM learning_events WHERE id=$1", legacy["event_id"])
        )[0]["n"] == 1

    _run_in_database(run)
