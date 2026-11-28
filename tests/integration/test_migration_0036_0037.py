"""0036/0037 anchor, organization-RLS, and index migration contracts."""

from __future__ import annotations

import asyncio
import os
import uuid

import asyncpg
import pytest
from sqlalchemy.engine import make_url

from .test_migration_0012 import _alembic, _fetch, _run_in_database

pytestmark = pytest.mark.skipif(
    os.environ.get("MIGRATION_TESTS") != "1",
    reason="set MIGRATION_TESTS=1 to run isolated PostgreSQL migration checks",
)


async def _seed_fixture(database_url: str) -> dict[str, str]:
    """Create one org graph plus a promoted plan concept at the 0035 shape."""

    ids = {name: str(uuid.uuid4()) for name in (
        "owner",
        "member",
        "outsider",
        "org",
        "kb",
        "kc_prerequisite",
        "kc_target",
        "edge",
        "plan",
        "concept",
    )}
    url = make_url(database_url)
    conn = await asyncpg.connect(
        user=url.username,
        password=url.password,
        host=url.host,
        port=url.port or 5432,
        database=url.database,
    )
    try:
        for key in ("owner", "member", "outsider"):
            await conn.execute(
                "INSERT INTO users "
                "(id, email, password_hash, role, status, guardian_consent, plan) "
                "VALUES ($1, $2, 'migration-hash', 'student', 'active', false, 'free')",
                ids[key],
                f"migration-{ids[key]}@test.invalid",
            )
        await conn.execute(
            "INSERT INTO orgs (id, name, invite_code, created_by, storage_quota_mb) "
            "VALUES ($1, 'migration-org', $2, $3, 512)",
            ids["org"],
            f"INV{ids['org'][:8]}",
            ids["owner"],
        )
        await conn.execute(
            "INSERT INTO org_members (org_id, user_id, role) VALUES ($1, $2, 'member')",
            ids["org"],
            ids["member"],
        )
        await conn.execute(
            "INSERT INTO knowledge_bases "
            "(id, tenant_id, owner_type, name, subject, kb_version, config_json) "
            "VALUES ($1, $2, 'org', 'migration-org-kb', 'math', 1, '{}'::jsonb)",
            ids["kb"],
            ids["org"],
        )
        for key, name in (("kc_prerequisite", "Prerequisite"), ("kc_target", "Target")):
            await conn.execute(
                "INSERT INTO knowledge_components "
                "(id, tenant_id, kb_id, canonical_name, aliases, description, stable_key) "
                "VALUES ($1, $2, $3, $4, '[]'::jsonb, '', $5)",
                ids[key],
                ids["org"],
                ids["kb"],
                name,
                f"chapter:{key}",
            )
        await conn.execute(
            "INSERT INTO kc_edges "
            "(id, tenant_id, from_kc, to_kc, type) "
            "VALUES ($1, $2, $3, $4, 'prerequisite')",
            ids["edge"],
            ids["org"],
            ids["kc_prerequisite"],
            ids["kc_target"],
        )
        await conn.execute(
            "INSERT INTO plans "
            "(id, tenant_id, user_id, goal, budget_hours, kb_id, plan_mode, "
            "grounding_status, status, structure_json, version) "
            "VALUES ($1, $2, $2, 'migration plan', 1, $3, 'course_bound', "
            "'kb_grounded', 'active', '{}'::jsonb, 1)",
            ids["plan"],
            ids["owner"],
            ids["kb"],
        )
        await conn.execute(
            "INSERT INTO plan_concepts "
            "(id, tenant_id, plan_id, display_name, ordinal, normalized_name, status) "
            "VALUES ($1, $2, $3, 'Target', 0, 'target', 'promoted')",
            ids["concept"],
            ids["owner"],
            ids["plan"],
        )
    finally:
        await conn.close()
    return ids


async def _connect_as_app(database_url: str) -> asyncpg.Connection:
    url = make_url(database_url)
    return await asyncpg.connect(
        user="edu_app",
        password="edu",
        host=url.host,
        port=url.port or 5432,
        database=url.database,
    )


async def _set_context(
    conn: asyncpg.Connection,
    tenant_id: str,
    org_ids: tuple[str, ...] = (),
) -> None:
    await conn.execute("SELECT set_config('app.tenant_id', $1, false)", tenant_id)
    await conn.execute(
        "SELECT set_config('app.org_ids', $1, false)",
        ",".join(org_ids),
    )


async def _execute_as_migration(database_url: str, query: str, *args: str) -> str:
    url = make_url(database_url)
    conn = await asyncpg.connect(
        user=url.username,
        password=url.password,
        host=url.host,
        port=url.port or 5432,
        database=url.database,
    )
    try:
        return await conn.execute(query, *args)
    finally:
        await conn.close()


async def _assert_org_graph_rls(database_url: str, ids: dict[str, str]) -> None:
    conn = await _connect_as_app(database_url)
    try:
        await _set_context(conn, ids["member"], (ids["org"],))
        kc_rows = await conn.fetch(
            "SELECT id FROM knowledge_components WHERE id IN ($1, $2) "
            "ORDER BY id",
            ids["kc_prerequisite"],
            ids["kc_target"],
        )
        edge_rows = await conn.fetch(
            "SELECT id FROM kc_edges WHERE id = $1",
            ids["edge"],
        )
        assert {str(row["id"]) for row in kc_rows} == {
            ids["kc_prerequisite"],
            ids["kc_target"],
        }
        assert {str(row["id"]) for row in edge_rows} == {ids["edge"]}
        assert await conn.execute(
            "UPDATE knowledge_components SET canonical_name = 'member-write' WHERE id = $1",
            ids["kc_target"],
        ) == "UPDATE 0"
        assert await conn.execute(
            "UPDATE kc_edges SET type = 'member-write' WHERE id = $1",
            ids["edge"],
        ) == "UPDATE 0"

        await _set_context(conn, ids["outsider"])
        assert not await conn.fetch(
            "SELECT id FROM knowledge_components WHERE id = $1",
            ids["kc_target"],
        )
        assert not await conn.fetch(
            "SELECT id FROM kc_edges WHERE id = $1",
            ids["edge"],
        )
    finally:
        await conn.close()


def test_0036_0037_anchor_rls_index_and_reversible_downgrade():
    def run(database_url: str) -> None:
        _alembic(database_url, "upgrade", "0035")
        ids = asyncio.run(_seed_fixture(database_url))

        _alembic(database_url, "upgrade", "0036")
        _alembic(database_url, "upgrade", "0037")

        assert asyncio.run(
            _execute_as_migration(
                database_url,
                "UPDATE plan_concepts SET kc_id = $1 WHERE id = $2",
                ids["kc_target"],
                ids["concept"],
            )
        ) == "UPDATE 1"
        fk_rows = asyncio.run(
            _fetch(
                database_url,
                "SELECT pg_get_constraintdef(oid) AS definition "
                "FROM pg_constraint WHERE conrelid='public.plan_concepts'::regclass "
                "AND conname LIKE '%kc_id%'",
            )
        )
        assert any("knowledge_components" in row["definition"] for row in fk_rows)
        assert any("SET NULL" in row["definition"] for row in fk_rows)
        indexes = asyncio.run(
            _fetch(
                database_url,
                "SELECT indexname FROM pg_indexes WHERE tablename='error_items' "
                "AND indexname='ix_error_items_resource_question_id'",
            )
        )
        assert len(indexes) == 1
        asyncio.run(_assert_org_graph_rls(database_url, ids))
        assert asyncio.run(
            _execute_as_migration(
                database_url,
                "DELETE FROM knowledge_components WHERE id = $1",
                ids["kc_target"],
            )
        ) == "DELETE 1"
        anchor = asyncio.run(
            _fetch(database_url, "SELECT kc_id FROM plan_concepts WHERE id=$1", ids["concept"])
        )
        assert anchor[0]["kc_id"] is None

        _alembic(database_url, "downgrade", "0035")
        assert not asyncio.run(
            _fetch(
                database_url,
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name='plan_concepts' AND column_name='kc_id'",
            )
        )
        assert not asyncio.run(
            _fetch(
                database_url,
                "SELECT indexname FROM pg_indexes WHERE tablename='error_items' "
                "AND indexname='ix_error_items_resource_question_id'",
            )
        )
        status = asyncio.run(
            _fetch(database_url, "SELECT status FROM plan_concepts WHERE id=$1", ids["concept"])
        )
        assert status[0]["status"] == "promoted"
        policies = asyncio.run(
            _fetch(
                database_url,
                "SELECT policyname FROM pg_policies "
                "WHERE schemaname='public' AND tablename='knowledge_components'",
            )
        )
        assert {row["policyname"] for row in policies} == {"tenant_isolation"}

        # Both revisions must be replayable after a downgrade.
        _alembic(database_url, "upgrade", "0036")
        _alembic(database_url, "upgrade", "0037")
        assert asyncio.run(
            _fetch(
                database_url,
                "SELECT indexname FROM pg_indexes WHERE tablename='error_items' "
                "AND indexname='ix_error_items_resource_question_id'",
            )
        )

    _run_in_database(run)
