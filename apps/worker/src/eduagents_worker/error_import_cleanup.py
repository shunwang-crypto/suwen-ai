"""定期回收所有租户的过期错题导入草稿。"""

from __future__ import annotations

from eduagents_core.config import get_settings
from eduagents_core.logging import get_logger
from eduagents_core.storage import create_storage

log = get_logger("worker_error_import_cleanup")


async def reap_stale_error_imports(ctx: dict) -> int:
    from eduagents_api.db import plain_session
    from eduagents_api.error_import_cleanup import cleanup_expired_error_imports
    from eduagents_api.models import User
    from sqlalchemy import select

    settings = ctx.get("settings") or get_settings()
    storage = create_storage(settings)
    async with plain_session() as session:
        user_ids = (await session.scalars(select(User.id))).all()

    cleaned = 0
    for user_id in user_ids:
        cleaned += await cleanup_expired_error_imports(
            tenant_id=user_id,
            user_id=user_id,
            storage=storage,
            ttl_hours=settings.error_import_draft_ttl_hours,
        )
    if cleaned:
        log.info("stale_error_imports_reaped", cleaned=cleaned)
    return cleaned
