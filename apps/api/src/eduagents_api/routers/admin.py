"""管理员时间偏移 API (§9) + 故障注入开关 (M5-D2)。

时间偏移: 推进 Redis 里的全局虚拟时钟偏移, FSRS 到期/BKT 衰减/复习队列随之前进;
禁止改容器系统时间的方案。

故障注入: 现场演示降级链的操作台 —— 关主模型/加延迟/按概率报错 + 一键熔断,
只作用于 live/record 网关路径 (stub/replay 是兜底通道, 按设计不受影响)。
"""

from __future__ import annotations

import uuid

from eduagents_core.config import get_settings
from eduagents_core.gateway import Tier, get_fault_injector
from eduagents_core.logging import get_logger
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import update

from .. import timectl
from ..db import plain_session
from ..deps import AuthContext, account_status_key, require_admin
from ..models import RefreshToken, User

router = APIRouter(prefix="/api/admin", tags=["admin"])
log = get_logger("admin")


class TimeTravelIn(BaseModel):
    days: float = Field(ge=-365.0, le=365.0, description="虚拟时钟前进天数 (可为负/小数)")


@router.post("/time-travel")
async def time_travel(
    body: TimeTravelIn, request: Request, ctx: AuthContext = Depends(require_admin)
):
    # P1-13: 生产禁用虚拟时钟 (误触会整体前移全体 FSRS 到期; 演示/开发环境专用)
    if not timectl.is_enabled():
        raise HTTPException(status_code=403, detail="TIME_TRAVEL_DISABLED")
    new_offset = await timectl.advance(request.app.state.redis, body.days)
    return {"offset_days": new_offset / 86400.0, "offset_seconds": new_offset}


@router.get("/time-travel")
async def get_time_travel(request: Request, ctx: AuthContext = Depends(require_admin)):
    offset = await timectl.get_offset(request.app.state.redis)
    now_dt = await timectl.now(request.app.state.redis)
    return {
        "offset_days": offset / 86400.0,
        "offset_seconds": offset,
        "virtual_now": now_dt.isoformat(),
    }


# --------------------------------------------------------------------------
# 账号封禁/解封 (P1-3)
# --------------------------------------------------------------------------


async def _set_user_status(user_id: uuid.UUID, status: str) -> None:
    async with plain_session() as session:
        user = await session.get(User, user_id)
        if user is None:
            raise HTTPException(status_code=404, detail="NOT_FOUND")
        user.status = status
        if status != "active":
            # 全部 refresh 撤销: 无法枚举在途 access jti, 由 deps 的状态检查
            # (Redis 标记 + 60s 缓存) 立即拦截
            await session.execute(
                update(RefreshToken)
                .where(RefreshToken.user_id == user_id, RefreshToken.revoked.is_(False))
                .values(revoked=True)
            )


@router.post("/users/{user_id}/disable")
async def disable_user(
    user_id: uuid.UUID, request: Request, ctx: AuthContext = Depends(require_admin)
):
    """封禁: users.status='disabled' + 撤销全部 refresh + Redis 标记立即生效。"""
    if user_id == ctx.user_id:
        raise HTTPException(status_code=400, detail="CANNOT_DISABLE_SELF")
    await _set_user_status(user_id, "disabled")
    await request.app.state.redis.setex(account_status_key(user_id), 60, "disabled")
    log.warning("admin_user_disabled", target=str(user_id), by=str(ctx.user_id))
    return {"user_id": str(user_id), "status": "disabled"}


@router.post("/users/{user_id}/enable")
async def enable_user(
    user_id: uuid.UUID, request: Request, ctx: AuthContext = Depends(require_admin)
):
    await _set_user_status(user_id, "active")
    # 删标记而非写 active: 下一请求查库回填, 以 DB 为唯一事实源
    await request.app.state.redis.delete(account_status_key(user_id))
    log.warning("admin_user_enabled", target=str(user_id), by=str(ctx.user_id))
    return {"user_id": str(user_id), "status": "active"}


# --------------------------------------------------------------------------
# 故障注入 (M5-D2)
# --------------------------------------------------------------------------

_FAULT_NOTE = (
    "故障注入只作用于 live/record 网关路径; stub/replay 不受影响 "
    "(它们是降级链最后一环的兜底通道, 演示必须可靠)。"
)


class FaultsIn(BaseModel):
    """全字段可选: 省略 = 保持原值 (面板上三个旋钮各自独立提交)。"""

    provider_down: bool | None = None
    latency_ms: int | None = Field(default=None, ge=0, le=30_000)
    error_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    # 一键熔断: 直接把断路器拉开 (不必等错误率累积), "all" = 三档全开
    trip_breaker: str | None = Field(default=None, pattern="^(cheap|standard|strong|all)$")
    reset_breakers: bool = False


def _faults_payload(request: Request) -> dict:
    injector = get_fault_injector()
    gateway = getattr(request.app.state, "gateway", None)
    if gateway is not None and hasattr(gateway, "breaker_states"):
        states = gateway.breaker_states()
    else:  # 网关未就绪 (极早期/测试替身): 如实报 unknown, 不假装 closed
        states = {t.value: "unknown" for t in Tier}
    return {
        **injector.snapshot(),
        "gateway_mode": get_settings().gateway_mode,
        "note": _FAULT_NOTE,
        "breakers": [{"tier": tier, "state": state} for tier, state in states.items()],
    }


@router.get("/faults")
async def get_faults(request: Request, ctx: AuthContext = Depends(require_admin)):
    if not get_settings().enable_fault_injection:
        raise HTTPException(status_code=404, detail="NOT_FOUND")
    return _faults_payload(request)


@router.post("/faults")
async def set_faults(
    body: FaultsIn, request: Request, ctx: AuthContext = Depends(require_admin)
):
    if not get_settings().enable_fault_injection:
        raise HTTPException(status_code=404, detail="NOT_FOUND")
    injector = get_fault_injector()
    injector.update(
        provider_down=body.provider_down,
        latency_ms=body.latency_ms,
        error_rate=body.error_rate,
    )
    gateway = getattr(request.app.state, "gateway", None)
    # reset 先于 trip: 允许一次请求里 "全恢复 + 只熔断 standard"
    if body.reset_breakers and gateway is not None and hasattr(gateway, "reset_breakers"):
        gateway.reset_breakers()
    if body.trip_breaker and gateway is not None and hasattr(gateway, "trip_breaker"):
        tiers = list(Tier) if body.trip_breaker == "all" else [Tier(body.trip_breaker)]
        for tier in tiers:
            gateway.trip_breaker(tier)
    return _faults_payload(request)
