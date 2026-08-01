from __future__ import annotations

import uuid

from eduagents_core.config import Settings, get_settings
from eduagents_core.logging import get_logger

log = get_logger("worker.reports")


async def refresh_learning_report(
    ctx: dict,
    tenant_id: str,
    user_id: str,
    snapshot_id: str,
    signature: str,
    request_id: str = "",
    *,
    gateway=None,
    settings: Settings | None = None,
) -> str:
    """生成学情诊断并回写快照；签名变化时丢弃陈旧任务结果。"""
    from eduagents_api.db import tenant_session
    from eduagents_api.deps import AuthContext
    from eduagents_api.models import LearningReportSnapshot
    from eduagents_api.routers.reports import _narrative

    from .enrichment import get_worker_gateway

    settings = settings or ctx.get("settings") or get_settings()
    if gateway is None:
        gateway = get_worker_gateway(settings)
    auth = AuthContext(
        user_id=uuid.UUID(user_id),
        tenant_id=uuid.UUID(tenant_id),
        role="student",
        jti="worker",
    )
    async with tenant_session(tenant_id) as session:
        snapshot = await session.get(LearningReportSnapshot, uuid.UUID(snapshot_id))
        if (
            snapshot is None
            or snapshot.data_signature != signature
            or snapshot.status != "generating"
        ):
            return "stale"
        payload = dict(snapshot.payload_json or {})
        actions = list(payload.get("actions") or [])

    try:
        narrative, note = await _narrative(
            gateway, settings, auth, request_id, payload, actions
        )
        status = "completed" if narrative else "failed"
    except Exception:  # noqa: BLE001 — worker 必须把 generating 收口
        log.exception("learning_report_refresh_failed", snapshot_id=snapshot_id)
        narrative = None
        note = "AI 诊断更新失败，当前报告数据仍可正常查看"
        status = "failed"

    async with tenant_session(tenant_id) as session:
        snapshot = await session.get(LearningReportSnapshot, uuid.UUID(snapshot_id))
        if snapshot is None or snapshot.data_signature != signature:
            return "stale"
        snapshot.narrative = narrative
        snapshot.narrative_note = note
        snapshot.status = status
    log.info("learning_report_refreshed", snapshot_id=snapshot_id, status=status)
    return status
