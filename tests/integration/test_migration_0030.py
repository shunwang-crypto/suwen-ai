"""0030 installs lifecycle and mastery-evidence database invariants."""

from sqlalchemy import text

from .conftest import requires_services

pytestmark = requires_services


async def test_0030_mastery_evidence_identity_is_unique():
    from eduagents_api.db import plain_session

    async with plain_session() as session:
        row = (
            await session.execute(
                text(
                    """
                    SELECT indexdef
                    FROM pg_indexes
                    WHERE tablename = 'mastery_log'
                      AND indexname = 'uq_mastery_log_evidence'
                    """
                )
            )
        ).scalar_one()
    normalized = " ".join(row.lower().split())
    assert "unique index" in normalized
    for column in ("tenant_id", "user_id", "kc_id", "evidence_type", "evidence_id"):
        assert column in normalized
    assert "where (evidence_id is not null)" in normalized
