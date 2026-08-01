"""定期回收所有租户的过期错题导入草稿。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from eduagents_core.config import get_settings
from eduagents_core.logging import get_logger
from eduagents_core.storage import create_storage
from sqlalchemy import and_, or_, select

log = get_logger("worker_error_import_cleanup")


async def reap_stale_error_imports(ctx: dict) -> int:
    settings = ctx.get("settings") or get_settings()
    storage = create_storage(settings)
    from eduagents_api.db import internal_cleanup_session
    from eduagents_api.models import ErrorImport, LearningResource, ResourceQuestion
    from eduagents_api.resource_utils import resource_storage_prefix

    cutoff = datetime.now(UTC) - timedelta(hours=max(1, settings.error_import_draft_ttl_hours))
    # One bounded page over lifecycle-owned rows; do not enumerate every User
    # and open one RLS transaction per account on every cron tick.
    async with internal_cleanup_session() as session:
        imports = (
            await session.scalars(
                select(ErrorImport)
                .where(
                    ErrorImport.status.in_(("draft", "deleting")),
                    ErrorImport.created_at < cutoff,
                )
                .order_by(ErrorImport.created_at.asc())
                .limit(100)
            )
        ).all()
        resources = (
            await session.scalars(
                select(LearningResource)
                .where(
                    or_(
                        and_(
                            LearningResource.status.in_(("uploaded", "extracting")),
                            ~select(ResourceQuestion.id)
                            .where(ResourceQuestion.resource_id == LearningResource.id)
                            .exists(),
                        ),
                        and_(
                            LearningResource.status == "failed",
                            LearningResource.metadata_json["delete_pending"].as_boolean().is_(True),
                            ~select(ResourceQuestion.id)
                            .where(ResourceQuestion.resource_id == LearningResource.id)
                            .exists(),
                        ),
                    ),
                    LearningResource.created_at < cutoff,
                )
                .order_by(LearningResource.created_at.asc())
                .limit(100)
            )
        ).all()
        targets = [
            ("import", row.id, row.tenant_id, bool(row.storage_key)) for row in imports
        ] + [
            ("resource", row.id, row.tenant_id, bool(row.storage_key)) for row in resources
        ]
        for row in imports:
            row.status = "deleting"
        for row in resources:
            row.status = "failed"
            row.metadata_json = {**(row.metadata_json or {}), "cleanup_pending": True}

    cleaned = 0
    for kind, row_id, tenant_id, has_storage in targets:
        try:
            if has_storage:
                prefix = (
                    f"{tenant_id}/error-imports/{row_id}"
                    if kind == "import"
                    else resource_storage_prefix(tenant_id, row_id)
                )
                await storage.delete_prefix(prefix)
        except Exception:  # noqa: BLE001 - leave a retryable database marker
            log.warning("stale_source_delete_failed", kind=kind, row_id=str(row_id))
            continue
        async with internal_cleanup_session() as session:
            if kind == "import":
                row = await session.get(ErrorImport, row_id)
                if row is not None and row.status == "deleting":
                    await session.delete(row)
                    cleaned += 1
            else:
                row = await session.get(LearningResource, row_id)
                if row is not None and (row.metadata_json or {}).get("cleanup_pending"):
                    await session.delete(row)
                    cleaned += 1
    if cleaned:
        log.info("stale_error_imports_reaped", cleaned=cleaned)
    return cleaned
