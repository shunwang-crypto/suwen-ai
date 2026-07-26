from __future__ import annotations

import uuid

from eduagents_core.gateway import UsageEvent
from eduagents_core.logging import get_logger

from .db import tenant_session
from .models import UsageLog

log = get_logger("usage")


async def persist_usage(event: UsageEvent) -> None:
    """Gateway usage sink -> usage_log row. 每次 LLM 调用都可按 user/feature/model
    三维归因 (F10-A1 / M1-D4)。失败只记日志, 不打断业务链路。"""
    if not event.tenant_id:
        log.warning("usage_event_without_tenant", feature=event.feature)
        return
    try:
        async with tenant_session(event.tenant_id) as session:
            session.add(
                UsageLog(
                    tenant_id=uuid.UUID(event.tenant_id),
                    request_id=event.request_id or "",
                    user_id=uuid.UUID(event.user_id),
                    feature=event.feature,
                    model=event.model,
                    tier=event.tier,
                    prompt_tokens=event.prompt_tokens,
                    completion_tokens=event.completion_tokens,
                    cached_tokens=event.cached_tokens,
                    cost_cny=event.cost_cny,
                    latency_ms=event.latency_ms,
                    mode=event.mode,
                )
            )
    except Exception:
        log.exception("usage_persist_failed")
