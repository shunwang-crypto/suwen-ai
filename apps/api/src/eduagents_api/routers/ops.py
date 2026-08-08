"""F10 运营与成本面板 (仅管理员)。

usage_log 的 RLS 策略额外放行 current_setting('app.is_admin')='true' (alembic 0001),
跨租户聚合必须在事务内先 set_config('app.is_admin','true',true) — 事务级 GUC,
连接归还后自动失效, 不会泄漏给下一个请求。仅本文件的 admin 端点允许设置。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from eduagents_core.config import get_settings
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import Date, cast, func, select, text

from ..db import tenant_session
from ..deps import AuthContext, require_admin
from ..models import UsageLog
from ..vector_reconcile import reconcile_vectors

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

        # 缓存节省口径 (Wave1-包B, 唯一实现): 语义缓存命中时主线写入
        # usage_log(mode='cache', cost_cny=0, cached_tokens=原答案 completion_tokens,
        # tier=原答案 tier)。节省额 = Σ cached_tokens × 对应 tier 输出单价 / 1e6 (CNY)。
        # 未计入原请求的 prompt_tokens 与嵌入费用 (命中行未存), 故为保守下界估算。
        settings = get_settings()
        savings_calls = savings_tokens = 0
        saved_cny = 0.0
        for tier_name, calls, cached_tokens in (
            await session.execute(
                select(
                    UsageLog.tier,
                    func.count(),
                    func.coalesce(func.sum(UsageLog.cached_tokens), 0),
                )
                .where(in_window, UsageLog.mode == "cache")
                .group_by(UsageLog.tier)
            )
        ).all():
            savings_calls += int(calls)
            savings_tokens += int(cached_tokens)
            if tier_name in ("cheap", "standard", "strong", "plan"):
                saved_cny += (
                    int(cached_tokens) / 1e6 * settings.tier(tier_name).output_price_per_m
                )
        cache_savings = {
            "calls": savings_calls,
            "cached_tokens": savings_tokens,
            "saved_cny": round(saved_cny, 6),
        }

    return {
        "days": days,
        "totals": totals,
        "by_feature": by_feature,
        "by_model": by_model,
        "by_mode": by_mode,
        "daily": daily,
        "top_users": top_users,
        "cache_savings": cache_savings,
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


@router.get("/vector-reconcile")
async def vector_reconcile(
    request: Request,
    kb_id: str | None = Query(default=None, description="只对账单个知识库"),
    include_all: bool = Query(default=False, description="连健康的库一起返回"),
    ctx: AuthContext = Depends(require_admin),
):
    """PG chunks(done) 与 Qdrant 点数对账。只读诊断, 不写任何一侧。

    ready 探针只测 Qdrant 连通性, 数据丢失时 RAG 会静默降级而无告警; 这里把差异显式
    暴露出来。``missing_vectors`` > 0 意味着有子块标了 done 却检索不到。
    """
    retrieval = getattr(request.app.state, "retrieval", None)
    store = getattr(retrieval, "_store", None)
    if store is None:
        raise HTTPException(status_code=503, detail="retrieval_stack_unavailable")
    report = await reconcile_vectors(store, ctx.tenant_id, kb_id=kb_id)
    return report.as_dict(include_all=include_all)
