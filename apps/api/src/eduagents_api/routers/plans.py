from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select

from ..db import tenant_session
from ..deps import AuthContext, api_rate_limited
from ..models import Plan
from .threads import stream_graph_run

router = APIRouter(prefix="/api/plans", tags=["plans"])


class PlanCreate(BaseModel):
    goal: str = Field(min_length=1, max_length=2000)
    kb_id: uuid.UUID | None = None
    budget_hours_per_day: float = Field(default=1.5, gt=0, le=12)
    total_days: int = Field(default=14, ge=1, le=90)


class TutorTurn(BaseModel):
    problem: str = Field(default="", max_length=4000)
    message: str = Field(min_length=1, max_length=4000)
    thread_local_id: uuid.UUID | None = None


@router.post("")
async def create_plan(
    body: PlanCreate, request: Request, ctx: AuthContext = Depends(api_rate_limited)
):
    """学习规划: 目标解析→诊断→检索资源→草案→interrupt 确认 (F3)。"""
    runtime = getattr(request.app.state, "graphs", None)
    if runtime is None:
        raise HTTPException(status_code=503, detail="GRAPHS_UNAVAILABLE")
    local_id = str(uuid.uuid4())
    thread_id = runtime.thread_id("plan", str(ctx.user_id), local_id)
    payload = {
        "tenant_id": str(ctx.tenant_id),
        "user_id": str(ctx.user_id),
        "request_id": getattr(request.state, "request_id", ""),
        "kb_id": str(body.kb_id) if body.kb_id else "",
        "goal": body.goal,
        "budget_hours_per_day": body.budget_hours_per_day,
        "total_days": body.total_days,
    }
    return await stream_graph_run(runtime, "plan", thread_id, ctx, payload)


@router.get("")
async def list_plans(ctx: AuthContext = Depends(api_rate_limited)):
    async with tenant_session(ctx.tenant_id) as session:
        rows = (
            await session.scalars(
                select(Plan).where(Plan.user_id == ctx.user_id).order_by(Plan.created_at.desc())
            )
        ).all()
        return [
            {
                "id": str(r.id),
                "goal": r.goal,
                "budget_hours": r.budget_hours,
                "structure": r.structure_json,
                "created_at": r.created_at,
            }
            for r in rows
        ]


tutor_router = APIRouter(prefix="/api/tutor", tags=["tutor"])


@tutor_router.post("")
async def tutor_turn(
    body: TutorTurn, request: Request, ctx: AuthContext = Depends(api_rate_limited)
):
    """苏格拉底辅导单轮 (F4): 同 thread 多轮推进三层提示状态机。"""
    runtime = getattr(request.app.state, "graphs", None)
    if runtime is None:
        raise HTTPException(status_code=503, detail="GRAPHS_UNAVAILABLE")
    local_id = str(body.thread_local_id or uuid.uuid4())
    thread_id = runtime.thread_id("tutor", str(ctx.user_id), local_id)
    payload = {
        "tenant_id": str(ctx.tenant_id),
        "user_id": str(ctx.user_id),
        "request_id": getattr(request.state, "request_id", ""),
        "problem": body.problem,
        "messages": [{"role": "user", "content": body.message}],
    }
    graph = runtime.graph("tutor")
    config = {"configurable": {"thread_id": thread_id}}
    state = await graph.ainvoke(payload, config)
    return {
        "thread_local_id": local_id,
        "reply": state.get("reply", ""),
        "hint_level": state.get("hint_level", 0),
        "stuck_type": state.get("stuck_type", ""),
        "session_summary": state.get("session_summary", ""),
    }
