from __future__ import annotations

import json
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select

from ..db import tenant_session
from ..deps import AuthContext, api_rate_limited
from ..models import Plan, PlanAdjustment
from ..plan_adjust import adjust_plan, enrich_structure, plan_progress, set_task_done
from ..services import apply_tutor_summary
from .threads import stream_graph_run

router = APIRouter(prefix="/api/plans", tags=["plans"])

_ADJUSTMENT_LIMIT = 50  # 调整历史返回上限


class PlanCreate(BaseModel):
    goal: str = Field(min_length=1, max_length=2000)
    kb_id: uuid.UUID | None = None
    budget_hours_per_day: float = Field(default=1.5, gt=0, le=12)
    total_days: int = Field(default=14, ge=1, le=90)


class TaskToggle(BaseModel):
    """done 缺省 (None) = 取反当前状态; 显式传 true/false 则幂等置位。"""

    done: bool | None = None


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


async def _load_plan(session, plan_id: uuid.UUID, ctx: AuthContext) -> Plan:
    plan = await session.get(Plan, plan_id)
    if plan is None or plan.user_id != ctx.user_id:
        raise HTTPException(status_code=404, detail="NOT_FOUND")
    return plan


def _plan_detail(plan: Plan) -> dict:
    structure = enrich_structure(plan.structure_json)
    return {
        "id": str(plan.id),
        "goal": plan.goal,
        "budget_hours": plan.budget_hours,
        "version": plan.version,
        "structure": structure,
        "progress": plan_progress(plan.structure_json),
        "created_at": plan.created_at,
    }


@router.get("/{plan_id}")
async def get_plan(plan_id: uuid.UUID, ctx: AuthContext = Depends(api_rate_limited)):
    """计划详情 (F3): 每个任务带 task_key 与 done, 附任务粒度进度。"""
    async with tenant_session(ctx.tenant_id) as session:
        plan = await _load_plan(session, plan_id, ctx)
        return _plan_detail(plan)


@router.post("/{plan_id}/tasks/{task_key}/toggle")
async def toggle_task(
    plan_id: uuid.UUID,
    task_key: str,
    body: TaskToggle | None = None,
    ctx: AuthContext = Depends(api_rate_limited),
):
    """勾选/取消勾选一个任务 (F3 计划是活的)。task_key 见 plan_adjust 模块文档。"""
    async with tenant_session(ctx.tenant_id) as session:
        plan = await _load_plan(session, plan_id, ctx)
        try:
            new_structure, done = set_task_done(
                plan.structure_json, task_key, body.done if body else None
            )
        except KeyError:
            raise HTTPException(status_code=404, detail="TASK_NOT_FOUND") from None
        plan.structure_json = new_structure  # JSONB 未挂 MutableDict, 必须整体重新赋值
        return {"task_key": task_key, "done": done, "progress": plan_progress(new_structure)}


@router.post("/{plan_id}/adjust")
async def adjust(plan_id: uuid.UUID, ctx: AuthContext = Depends(api_rate_limited)):
    """依据当前学情调整计划 (F3-A3): 薄弱 KC 无对应任务 → 插补强任务 + 写调整审计。"""
    async with tenant_session(ctx.tenant_id) as session:
        plan = await _load_plan(session, plan_id, ctx)
        result = await adjust_plan(
            session, ctx.user_id, ctx.tenant_id, plan, trigger="manual"
        )
        result["progress"] = plan_progress(plan.structure_json)
        return result


@router.get("/{plan_id}/adjustments")
async def list_adjustments(plan_id: uuid.UUID, ctx: AuthContext = Depends(api_rate_limited)):
    """调整历史 (时间倒序): 每条含中文理由与插入明细。"""
    async with tenant_session(ctx.tenant_id) as session:
        await _load_plan(session, plan_id, ctx)
        rows = (
            await session.scalars(
                select(PlanAdjustment)
                .where(PlanAdjustment.plan_id == plan_id)
                .order_by(PlanAdjustment.at.desc())
                .limit(_ADJUSTMENT_LIMIT)
            )
        ).all()
        return [
            {
                "id": str(r.id),
                "reason": r.reason,
                "trigger": (r.diff_json or {}).get("trigger", ""),
                "inserted": (r.diff_json or {}).get("inserted", []),
                "at": r.at,
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
    # F4→F8 联动: 仅当本轮走了 summarize (trace 末项) 才把小结写入画像 —
    # trace/session_summary 会随 checkpointer 跨轮保留, 不能只看非空, 否则同 thread 续聊会重复计分。
    mastery_changes: list[dict] = []
    summary_raw = state.get("session_summary", "")
    trace = state.get("trace") or []
    if summary_raw and trace and trace[-1].get("node") == "summarize":
        try:
            summary = json.loads(summary_raw)
        except ValueError:
            summary = None
        if isinstance(summary, dict):
            mastery_changes = await apply_tutor_summary(ctx, summary)
    return {
        "thread_local_id": local_id,
        "reply": state.get("reply", ""),
        "hint_level": state.get("hint_level", 0),
        "stuck_type": state.get("stuck_type", ""),
        "session_summary": summary_raw,
        "mastery_changes": mastery_changes,
    }
