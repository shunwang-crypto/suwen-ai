"""错题导入草稿的容量和对象存储生命周期辅助函数。"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from eduagents_core.logging import get_logger
from sqlalchemy import func, select

from .db import tenant_session
from .models import ErrorImport

log = get_logger("error_import_cleanup")

ERROR_IMPORT_PREFIX = "error-imports"
ERROR_IMPORT_DRAFT_STATES = ("draft", "deleting")


def error_import_storage_prefix(tenant_id: uuid.UUID | str, import_id: uuid.UUID | str) -> str:
    return f"{tenant_id}/{ERROR_IMPORT_PREFIX}/{import_id}"


async def pending_error_import_count(
    tenant_id: uuid.UUID | str, user_id: uuid.UUID | str
) -> int:
    async with tenant_session(tenant_id) as session:
        return int(
            await session.scalar(
                select(func.count())
                .select_from(ErrorImport)
                .where(
                    ErrorImport.user_id == user_id,
                    ErrorImport.status.in_(ERROR_IMPORT_DRAFT_STATES),
                )
            )
            or 0
        )


async def cleanup_expired_error_imports(
    *,
    tenant_id: uuid.UUID | str,
    user_id: uuid.UUID | str,
    storage,
    ttl_hours: int,
    now: datetime | None = None,
) -> int:
    """清理本人过期草稿。

    删除分两阶段提交：先把行标成 ``deleting``，对象删除成功后才删除数据库行。
    对象存储失败会保留该行，后续 API/worker 可重试，不会出现错误的“已删除”状态。
    """

    current = now or datetime.now(UTC)
    cutoff = current - timedelta(hours=max(1, ttl_hours))
    async with tenant_session(tenant_id) as session:
        rows = (
            await session.scalars(
                select(ErrorImport).where(
                    ErrorImport.user_id == user_id,
                    ErrorImport.status.in_(ERROR_IMPORT_DRAFT_STATES),
                    ErrorImport.created_at < cutoff,
                )
            )
        ).all()
        targets = [(row.id, bool(row.storage_key)) for row in rows]
        for row in rows:
            row.status = "deleting"

    cleaned = 0
    for import_id, has_storage in targets:
        try:
            if has_storage:
                await storage.delete_prefix(error_import_storage_prefix(tenant_id, import_id))
        except Exception:  # noqa: BLE001 - 留行供下一轮重试
            log.warning("error_import_expiry_delete_failed", import_id=str(import_id))
            continue
        async with tenant_session(tenant_id) as session:
            row = await session.get(ErrorImport, import_id)
            if row is not None and row.user_id == user_id and row.status == "deleting":
                await session.delete(row)
                cleaned += 1
    return cleaned
