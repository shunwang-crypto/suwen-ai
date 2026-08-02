from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from eduagents_core.config import get_settings
from eduagents_core.logging import get_logger
from sqlalchemy import select

log = get_logger("worker.tutor_cleanup")


async def reap_stale_tutor_drafts(ctx: dict) -> int:
    """Delete unconfirmed expired drafts and enqueue source cleanup atomically."""
    from eduagents_api.db import internal_cleanup_session, tenant_session
    from eduagents_api.models import (
        LearningResource,
        PendingCleanup,
        TutorProblemDraft,
    )

    settings = ctx.get("settings") or get_settings()
    cutoff = datetime.now(UTC) - timedelta(hours=max(1, settings.tutor_draft_ttl_hours))
    async with internal_cleanup_session() as session:
        targets = (
            await session.execute(
                select(TutorProblemDraft.id, TutorProblemDraft.tenant_id)
                .where(
                    TutorProblemDraft.status != "confirmed",
                    TutorProblemDraft.updated_at < cutoff,
                )
                .order_by(TutorProblemDraft.updated_at)
                .limit(100)
            )
        ).all()
    cleaned = 0
    for draft_id, tenant_id in targets:
        async with tenant_session(tenant_id) as session:
            draft = await session.get(TutorProblemDraft, draft_id, with_for_update=True)
            if draft is None or draft.status == "confirmed" or draft.updated_at >= cutoff:
                continue
            resource = await session.get(LearningResource, draft.resource_id) if draft.resource_id else None
            if resource is not None:
                session.add(
                    PendingCleanup(
                        id=uuid.uuid4(),
                        kind="object_prefix",
                        payload={"prefix": f"{tenant_id}/learning-resources/{resource.id}"},
                    )
                )
                await session.delete(resource)
            else:
                await session.delete(draft)
            await session.flush()
            cleaned += 1
    if cleaned:
        log.info("stale_tutor_drafts_reaped", cleaned=cleaned)
    return cleaned
