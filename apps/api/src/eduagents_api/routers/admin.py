"""管理员时间偏移 API (§9): 演示与 D-A1 验收的载体。

推进 Redis 里的全局虚拟时钟偏移, FSRS 到期/BKT 衰减/复习队列随之前进;
禁止改容器系统时间的方案。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from .. import timectl
from ..deps import AuthContext, require_admin

router = APIRouter(prefix="/api/admin", tags=["admin"])


class TimeTravelIn(BaseModel):
    days: float = Field(ge=-365.0, le=365.0, description="虚拟时钟前进天数 (可为负/小数)")


@router.post("/time-travel")
async def time_travel(
    body: TimeTravelIn, request: Request, ctx: AuthContext = Depends(require_admin)
):
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
