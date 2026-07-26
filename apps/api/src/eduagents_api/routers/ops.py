"""F10 运营与成本面板 (仅管理员)。

usage_log 的 RLS 策略额外放行 current_setting('app.is_admin')='true' (alembic 0001),
跨租户聚合必须在事务内先 set_config('app.is_admin','true',true) — 事务级 GUC,
连接归还后自动失效, 不会泄漏给下一个请求。仅本文件的 admin 端点允许设置。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy import Date, cast, func, select, text

from ..db import tenant_session
from ..deps import AuthContext, require_admin
from ..models import UsageLog

router = APIRouter(prefix="/api/admin", tags=["admin"])

_ADMIN_GUC = text("SELECT set_config('app.is_admin', 'true', true)")


@router.get("/usage")
async def usage_overview(
    days: int = Query(default=7, ge=1, le=90),
    ctx: AuthContext = Depends(require_admin),
):
    """跨租户成本/token 聚合: 总量 / 按 feature / 按 model+tier / 按 mode / 日趋势 / top5 用户。
    F10-A1: 每次调用可归因到 user/feature/model 三维。"""
    since = datetime.now(UTC) - timedelta(days=days)
    in_window = UsageLog.at >= since
    async with tenant_session(ctx.tenant_id) as session:
        await session.execute(_ADMIN_GUC)  # 管理员跨租户放行 (仅本事务)

        totals_row = (
            await session.execute(
                select(
                    func.count(),
                    func.coalesce(func.sum(UsageLog.cost_cny), 0.0),
                    func.coalesce(func.sum(UsageLog.prompt_tokens), 0),
                    func.coalesce(func.sum(UsageLog.completion_tokens), 0),
                    func.coalesce(func.sum(UsageLog.cached_tokens), 0),
                ).where(in_window)
            )
        ).one()
        totals = {
            "calls": int(totals_row[0]),
            "cost_cny": round(float(totals_row[1]), 6),
            "prompt_tokens": int(totals_row[2]),
            "completion_tokens": int(totals_row[3]),
            "cached_tokens": int(totals_row[4]),
        }

        by_feature = [
            {
                "feature": feature,
                "calls": int(calls),
                "cost_cny": round(float(cost), 6),
                "tokens": int(tokens),
            }
            for feature, calls, cost, tokens in (
                await session.execute(
                    select(
                        UsageLog.feature,
                        func.count(),
                        func.sum(UsageLog.cost_cny),
                        func.sum(UsageLog.prompt_tokens + UsageLog.completion_tokens),
                    )
                    .where(in_window)
                    .group_by(UsageLog.feature)
                    .order_by(func.sum(UsageLog.cost_cny).desc())
                )
            ).all()
        ]

        by_model = [
            {
                "model": model,
                "tier": tier,
                "calls": int(calls),
                "cost_cny": round(float(cost), 6),
            }
            for model, tier, calls, cost in (
                await session.execute(
                    select(UsageLog.model, UsageLog.tier, func.count(), func.sum(UsageLog.cost_cny))
                    .where(in_window)
                    .group_by(UsageLog.model, UsageLog.tier)
                    .order_by(func.sum(UsageLog.cost_cny).desc())
                )
            ).all()
        ]

        by_mode = [
            {"mode": mode, "calls": int(calls), "cost_cny": round(float(cost), 6)}
            for mode, calls, cost in (
                await session.execute(
                    select(UsageLog.mode, func.count(), func.sum(UsageLog.cost_cny))
                    .where(in_window)
                    .group_by(UsageLog.mode)
                )
            ).all()
        ]

        day_col = cast(func.timezone("UTC", UsageLog.at), Date)
        daily = [
            {
                "date": day.isoformat(),
                "calls": int(calls),
                "cost_cny": round(float(cost), 6),
                "tokens": int(tokens),
            }
            for day, calls, cost, tokens in (
                await session.execute(
                    select(
                        day_col.label("day"),
                        func.count(),
                        func.sum(UsageLog.cost_cny),
                        func.sum(UsageLog.prompt_tokens + UsageLog.completion_tokens),
                    )
                    .where(in_window)
                    .group_by(day_col)
                    .order_by(day_col.asc())
                )
            ).all()
        ]

        top_users = [
            # 脱敏: 仅暴露 user_id 前 8 位, 面板可辨识但不可反查
            {"user": str(user_id)[:8], "calls": int(calls), "cost_cny": round(float(cost), 6)}
            for user_id, calls, cost in (
                await session.execute(
                    select(UsageLog.user_id, func.count(), func.sum(UsageLog.cost_cny))
                    .where(in_window)
                    .group_by(UsageLog.user_id)
                    .order_by(func.sum(UsageLog.cost_cny).desc())
                    .limit(5)
                )
            ).all()
        ]

    return {
        "days": days,
        "totals": totals,
        "by_feature": by_feature,
        "by_model": by_model,
        "by_mode": by_mode,
        "daily": daily,
        "top_users": top_users,
    }


@router.get("/usage/latency")
async def usage_latency(
    days: int = Query(default=7, ge=1, le=90),
    ctx: AuthContext = Depends(require_admin),
):
    """按 feature 的 latency_ms p50/p95 (percentile_cont, 跨租户)。"""
    since = datetime.now(UTC) - timedelta(days=days)
    async with tenant_session(ctx.tenant_id) as session:
        await session.execute(_ADMIN_GUC)
        rows = (
            await session.execute(
                select(
                    UsageLog.feature,
                    func.count(),
                    func.percentile_cont(0.5).within_group(UsageLog.latency_ms.asc()),
                    func.percentile_cont(0.95).within_group(UsageLog.latency_ms.asc()),
                )
                .where(UsageLog.at >= since)
                .group_by(UsageLog.feature)
                .order_by(UsageLog.feature.asc())
            )
        ).all()
    return {
        "days": days,
        "by_feature": [
            {
                "feature": feature,
                "calls": int(calls),
                "p50_ms": round(float(p50), 2) if p50 is not None else None,
                "p95_ms": round(float(p95), 2) if p95 is not None else None,
            }
            for feature, calls, p50, p95 in rows
        ],
    }
