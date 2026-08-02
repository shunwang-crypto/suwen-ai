from __future__ import annotations

import uuid

from eduagents_core.gateway import UsageEvent
from eduagents_core.logging import get_logger

from .db import tenant_session
from .models import UsageLog

log = get_logger("usage")


async def persist_usage(event: UsageEvent) -> None:
    """Gateway usage sink -> usage_log row. 每次 LLM 调用都可按 user/feature/model
    三维归因 (F10-A1 / M1-D4)。失败只记日志, 不打断业务链路。

    0006 预付额度: 钱包扣费挂在**同一事务**内 (不新开事务) —— usage_log 行与
    wallet_ledger/wallet_accounts 一起提交/一起回滚, 不出现"记了用量没扣钱"或
    反之的单边落账。cost_cny <= 0 (stub/replay/缓存命中/失败返还) 零成本直接跳过;
    余额允许扣到负值 (进行中调用不打断), 拦截在下一次请求的网关入口 precheck
    (wallet.WalletBudgetGuard -> 402 INSUFFICIENT_CREDITS)。
    """
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
                    credential_source=event.credential_source,
                    provider=event.provider or event.credential_source,
                    platform_billed_cost_cny=event.platform_billed_cost_cny,
                )
            )
            if (
                event.user_id
                and event.credential_source == "platform"
                and event.platform_billed_cost_cny > 0
            ):
                from .wallet import get_wallet_service

                await get_wallet_service().consume(
                    session,
                    user_id=uuid.UUID(event.user_id),
                    cost_cny=event.platform_billed_cost_cny,
                    request_id=event.request_id,
                    feature=event.feature,
                )
    except Exception:
        log.exception("usage_persist_failed")
