from __future__ import annotations

import json
import uuid
from typing import Literal

from eduagents_core.kc.dag import topological_order
from eduagents_core.kc.types import PrereqEdge, normalize_kc_name
from eduagents_core.logging import get_logger
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import select

from .. import timectl
from ..budget import CN_TZ
from ..db import tenant_session
from ..deps import AuthContext, api_rate_limited
from ..models import (
    ErrorItem,
    KcEdge,
    KnowledgeBase,
    KnowledgeComponent,
    MasteryRecord,
    Plan,
    PlanAdjustment,
    PlanResource,
    Question,
)
from ..plan_adjust import (
    PlanTaskAttributionError,
    adjust_plan,
    complete_plan_task,
    compute_task_locks,
    enrich_structure,
    iter_tasks,
    latest_plan,
    make_task_key,
    plan_progress,
    plan_schedule_snapshot,
    set_task_status,
    task_status,
    validate_plan_task_attribution,
)
from ..profile_summary import (
    ProfileContext,
    build_profile_context,
    problem_mastery_band,
    weak_point_kcs,
)
from ..services import _plan_resource_public, apply_tutor_summary
from .threads import stream_graph_run

log = get_logger("plans")

router = APIRouter(prefix="/api/plans", tags=["plans"])

_ADJUSTMENT_LIMIT = 50  # 调整历史返回上限
_PLAN_WEAK_LIMIT = 8


class PlanCreate(BaseModel):
    goal: str = Field(min_length=1, max_length=2000)
    current_level: str | None = Field(default=None, max_length=200)
    target_outcome: str | None = Field(default=None, max_length=500)
    success_criteria: str | None = Field(default=None, max_length=500)
    kb_id: uuid.UUID | None = None
    budget_hours_per_day: float = Field(..., gt=0, le=12)
    total_days: int = Field(..., ge=1, le=90)

    @field_validator("goal")
    @classmethod
    def non_blank_goal(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("goal must not be blank")
        return value

    @field_validator("current_level", "target_outcome", "success_criteria")
    @classmethod
    def strip_optional_context(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None


class TaskToggle(BaseModel):
    """Three-state update with the old boolean field kept for compatibility."""

    status: Literal["pending", "done", "skipped"] | None = None
    done: bool | None = None


class TutorTurn(BaseModel):
    problem: str = Field(default="", max_length=4000)
    message: str = Field(min_length=1, max_length=4000)
    thread_local_id: uuid.UUID | None = None
    tutor_draft_id: uuid.UUID | None = None
    plan_id: uuid.UUID | None = None
    task_key: str | None = Field(default=None, max_length=96)

    @field_validator("task_key")
    @classmethod
    def _normalize_task_key(cls, value: str | None) -> str | None:
        return value.strip() if value else None

    @model_validator(mode="after")
    def _plan_attribution_pair(self) -> TutorTurn:
        if (self.plan_id is None) != (self.task_key is None):
            raise ValueError("plan_id 与 task_key 必须同时提供")
        return self


def _diagnosis_from_rows(rows: list[tuple[str, float, str]], kb_id: str) -> dict[str, float]:
    """(kc_name, p_mastery, kb_id) 行 -> {kc_name: p_mastery}。

    绑定 kb 时按库过滤; 过滤后为空则回退全量 (跨库学习者不该因绑库而丢真实掌握度,
    与 profile_summary 的取材口径一致)。
    """
    scoped = [(name, p) for name, p, kb in rows if not kb_id or kb == kb_id]
    if not scoped:
        scoped = [(name, p) for name, p, _kb in rows]
    # 跨库同名 KC 取保守低值 (掌握度存疑时宁可多排复习, 不可乐观跳过)
    out: dict[str, float] = {}
    for name, p in scoped:
        value = round(float(p), 4)
        if name not in out or value < out[name]:
            out[name] = value
    return out


def _qualitative_weak_kcs(items: object) -> list[dict[str, str]]:
    """weak_point_kcs 出参 -> checkpoint 安全切片。

    丢弃 p_mastery、KC id、难度与错题状态；计划图只需 KC 名和定性区间。
    """
    if not isinstance(items, list):
        return []
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in items:
        if len(result) >= _PLAN_WEAK_LIMIT or not isinstance(item, dict):
            continue
        name = item.get("name")
        zone = item.get("zone")
        if not isinstance(name, str) or not name.strip() or zone not in {"较弱", "待巩固"}:
            continue
        clean_name = name.strip()[:120]
        normalized = normalize_kc_name(clean_name)
        if not normalized or normalized in seen:
            continue
        result.append({"name": clean_name, "zone": zone})
        seen.add(normalized)
    return result


def _kc_order_from_edges(
    names_by_id: dict[str, str], edge_pairs: list[tuple[str, str]]
) -> list[str]:
    """先修边 -> KC 名拓扑序 (kc.dag.topological_order, F3-A2)。

    只把**被先修边约束的** KC 放进序里: plan_graph._fix_kc_order 对序内 KC 施加
    全序约束, 无先修关系的 KC 进序会被人为强排; 不进序则其任务顺序保持不变。
    names_by_id 的插入顺序 (查询按 created_at) 即无约束时的稳定并列序。
    """
    edges = [
        PrereqEdge(from_kc=names_by_id[f], to_kc=names_by_id[t])
        for f, t in edge_pairs
        if f in names_by_id and t in names_by_id and f != t
    ]
    if not edges:
        return []
    constrained = {name for e in edges for name in (e.from_kc, e.to_kc)}
    ordered_names = [name for name in names_by_id.values() if name in constrained]
    return topological_order(ordered_names, edges)


async def _load_personalization(ctx: AuthContext, kb_id: str) -> tuple[dict[str, float], list[str]]:
    """计划个性化预查 (一次事务): 真实 BKT 掌握度 + 先修 DAG 拓扑序。

    - diagnosis: 该用户 mastery_records join KC 名 -> 注入 plan 图的 diagnosis 通道,
      LLM 摸底只对无记录的主题兜底 (G1);
    - kc_order: 相关 KC (绑库时 = 该库全部 KC; 未绑库时 = 该用户有掌握度记录的 KC)
      之间的 prerequisite 边 -> 拓扑序注入 kc_order 通道 (G2)。
    """
    async with tenant_session(ctx.tenant_id, org_ids=ctx.org_ids) as session:
        mastery_rows = (
            await session.execute(
                select(
                    KnowledgeComponent.canonical_name,
                    MasteryRecord.p_mastery,
                    KnowledgeComponent.kb_id,
                )
                .join(KnowledgeComponent, KnowledgeComponent.id == MasteryRecord.kc_id)
                .where(MasteryRecord.user_id == ctx.user_id)
                .order_by(MasteryRecord.p_mastery.asc())
            )
        ).all()
        kc_query = select(KnowledgeComponent.id, KnowledgeComponent.canonical_name)
        if kb_id:
            kc_query = kc_query.where(KnowledgeComponent.kb_id == uuid.UUID(kb_id))
        else:
            kc_query = kc_query.join(
                MasteryRecord, MasteryRecord.kc_id == KnowledgeComponent.id
            ).where(MasteryRecord.user_id == ctx.user_id)
        kc_rows = (
            await session.execute(kc_query.order_by(KnowledgeComponent.created_at.asc()))
        ).all()
        kc_ids = [kc_id for kc_id, _name in kc_rows]
        edge_rows = []
        if kc_ids:
            edge_rows = (
                await session.execute(
                    select(KcEdge.from_kc, KcEdge.to_kc).where(
                        KcEdge.type == "prerequisite",
                        KcEdge.from_kc.in_(kc_ids),
                        KcEdge.to_kc.in_(kc_ids),
                    )
                )
            ).all()
    diagnosis = _diagnosis_from_rows(
        [(name, float(p), str(kb)) for name, p, kb in mastery_rows], kb_id
    )
    names_by_id = {str(kc_id): name for kc_id, name in kc_rows}
    kc_order = _kc_order_from_edges(names_by_id, [(str(f), str(t)) for f, t in edge_rows])
    return diagnosis, kc_order


@router.post("")
async def create_plan(
    body: PlanCreate, request: Request, ctx: AuthContext = Depends(api_rate_limited)
):
    """学习规划: 目标解析→诊断→检索资源→草案→interrupt 确认 (F3)。"""
    runtime = getattr(request.app.state, "graphs", None)
    if runtime is None:
        raise HTTPException(status_code=503, detail="GRAPHS_UNAVAILABLE")
    if body.kb_id is not None:
        # The graph receives only a validated KB id.  This check is repeated
        # at finalize time because a KB can be deleted while the draft waits.
        async with tenant_session(ctx.tenant_id, org_ids=ctx.org_ids) as session:
            kb = await session.get(KnowledgeBase, body.kb_id)
            if (
                kb is None
                or (kb.owner_type == "user" and kb.tenant_id != ctx.user_id)
                or (kb.owner_type == "org" and str(kb.tenant_id) not in set(ctx.org_ids))
            ):
                raise HTTPException(status_code=404, detail="KB_NOT_FOUND")
    local_id = str(uuid.uuid4())
    thread_id = runtime.thread_id("plan", str(ctx.user_id), local_id)
    # 个性化预查失败一律降级为空 (图内 LLM 摸底兜底), 不让个性化故障拖垮规划主链路
    diagnosis: dict[str, float] = {}
    kc_order: list[str] = []
    profile = ProfileContext()
    try:
        diagnosis, kc_order = await _load_personalization(
            ctx, str(body.kb_id) if body.kb_id else ""
        )
    except Exception:  # noqa: BLE001
        log.exception("plan_personalization_load_failed")
    try:
        profile = await build_profile_context(
            ctx.tenant_id,
            ctx.user_id,
            [str(body.kb_id)] if body.kb_id else [],
            redis=getattr(request.app.state, "redis", None),
            role="plan",
        )
    except Exception:  # noqa: BLE001
        log.exception("plan_profile_summary_failed")
    weak_kcs: list[dict[str, str]] = []
    try:
        weak_kcs = _qualitative_weak_kcs(
            await weak_point_kcs(
                ctx.tenant_id,
                ctx.user_id,
                [str(body.kb_id)] if body.kb_id else [],
                limit=_PLAN_WEAK_LIMIT,
            )
        )
    except Exception:  # noqa: BLE001
        # 技能差距是可选个性化；读侧失败不阻断基础计划。
        log.exception("plan_weak_kcs_load_failed")
    payload = {
        "tenant_id": str(ctx.tenant_id),
        "user_id": str(ctx.user_id),
        "request_id": getattr(request.state, "request_id", ""),
        "kb_id": str(body.kb_id) if body.kb_id else "",
        "goal": body.goal,
        "current_level": body.current_level or "",
        "target_outcome": body.target_outcome or "",
        "success_criteria": body.success_criteria or "",
        "budget_hours_per_day": body.budget_hours_per_day,
        "total_days": body.total_days,
        # 真实 BKT 掌握度注入 (G1): diagnose 节点只对无记录主题走 LLM 摸底
        "diagnosis": diagnosis,
        # 先修 DAG 拓扑序注入 (G2): draft_plan 的 _fix_kc_order 据此顺延违序任务
        "kc_order": kc_order,
        "weak_kcs": weak_kcs,
        "profile_summary": profile.summary,
        "profile_weak_points": profile.weak_points,
    }
    return await stream_graph_run(runtime, "plan", thread_id, ctx, payload)


@router.get("")
async def list_plans(ctx: AuthContext = Depends(api_rate_limited)):
    async with tenant_session(ctx.tenant_id, org_ids=ctx.org_ids) as session:
        rows = (
            await session.scalars(
                select(Plan).where(Plan.user_id == ctx.user_id).order_by(Plan.created_at.desc())
            )
        ).all()
        kb_ids = [row.kb_id for row in rows if row.kb_id is not None]
        kb_names = {}
        if kb_ids:
            kb_names = {
                row.id: row.name
                for row in (
                    await session.scalars(select(KnowledgeBase).where(KnowledgeBase.id.in_(kb_ids)))
                ).all()
            }
        return [
            {
                "id": str(r.id),
                "goal": r.goal,
                "budget_hours": r.budget_hours,
                "kb_id": str(r.kb_id) if r.kb_id else None,
                "kb_name": kb_names.get(r.kb_id),
                "structure": r.structure_json,
                "created_at": r.created_at,
                "is_current": index == 0,
            }
            for index, r in enumerate(rows)
        ]


async def _load_plan(session, plan_id: uuid.UUID, ctx: AuthContext) -> Plan:
    plan = await session.get(Plan, plan_id)
    if plan is None or plan.user_id != ctx.user_id:
        raise HTTPException(status_code=404, detail="NOT_FOUND")
    return plan


async def _load_task_locks(session, plan: Plan) -> dict[str, dict]:
    """先修未达标 -> 任务锁定信息 (派生态, 不落库; 判据见 plan_adjust.compute_task_locks)。

    只查该用户已有掌握度记录的 KC 之间的 prerequisite 边: 没有掌握度记录的先修 KC
    不算阻塞, 所以也不必把整库 KC 拉进来。
    """
    mastery_rows = (
        await session.execute(
            select(KnowledgeComponent.id, KnowledgeComponent.canonical_name, MasteryRecord.p_mastery)
            .join(KnowledgeComponent, KnowledgeComponent.id == MasteryRecord.kc_id)
            .where(MasteryRecord.user_id == plan.user_id)
        )
    ).all()
    if not mastery_rows:
        return {}
    mastery_by_kc = {
        normalize_kc_name(name): float(score) for _kc_id, name, score in mastery_rows
    }
    names_by_id = {str(kc_id): name for kc_id, name, _score in mastery_rows}
    edge_rows = (
        await session.execute(
            select(KcEdge.from_kc, KcEdge.to_kc).where(
                KcEdge.type == "prerequisite",
                KcEdge.to_kc.in_([kc_id for kc_id, _n, _s in mastery_rows]),
            )
        )
    ).all()
    prereq_by_kc: dict[str, list[str]] = {}
    for from_kc, to_kc in edge_rows:
        target = names_by_id.get(str(to_kc))
        source = names_by_id.get(str(from_kc))
        if not target or not source:
            continue
        prereq_by_kc.setdefault(normalize_kc_name(target), []).append(
            normalize_kc_name(source)
        )
    return compute_task_locks(plan.structure_json, mastery_by_kc, prereq_by_kc)


async def _plan_detail(session, plan: Plan, today) -> dict:
    resource_rows = (
        await session.scalars(
            select(PlanResource)
            .where(PlanResource.plan_id == plan.id)
            .order_by(PlanResource.created_at)
        )
    ).all()
    resources = {str(row.id): _plan_resource_public(row) for row in resource_rows}
    kb = await session.get(KnowledgeBase, plan.kb_id) if plan.kb_id else None
    locks = await _load_task_locks(session, plan)
    structure = enrich_structure(plan.structure_json, resources, locks)
    schedule = plan_schedule_snapshot(
        plan.created_at.astimezone(CN_TZ).date(), plan.structure_json, today
    )
    return {
        "id": str(plan.id),
        "goal": plan.goal,
        "budget_hours": plan.budget_hours,
        "kb_id": str(plan.kb_id) if plan.kb_id else None,
        "kb_name": kb.name if kb else None,
        "version": plan.version,
        "structure": structure,
        "resources": list(resources.values()),
        "schedule": schedule,
        "progress": plan_progress(plan.structure_json),
        "created_at": plan.created_at,
    }


@router.get("/{plan_id}")
async def get_plan(
    plan_id: uuid.UUID,
    request: Request,
    ctx: AuthContext = Depends(api_rate_limited),
):
    """计划详情 (F3): 每个任务带 task_key 与 done, 附任务粒度进度。"""
    async with tenant_session(ctx.tenant_id, org_ids=ctx.org_ids) as session:
        plan = await _load_plan(session, plan_id, ctx)
        now_dt = await timectl.now(request.app.state.redis)
        return await _plan_detail(session, plan, now_dt.astimezone(CN_TZ).date())


@router.get("/{plan_id}/tasks/{task_key}/tutor-context")
async def get_plan_task_tutor_context(
    plan_id: uuid.UUID,
    task_key: str,
    ctx: AuthContext = Depends(api_rate_limited),
):
    """Resolve a plan task for Tutor without putting task text in a URL."""
    async with tenant_session(ctx.tenant_id, org_ids=ctx.org_ids) as session:
        plan = await _load_plan(session, plan_id, ctx)
        for stage_index, day_index, task_index, _stage, _day, task in iter_tasks(
            plan.structure_json or {}
        ):
            if make_task_key(stage_index, day_index, task_index) != task_key:
                continue
            problem = str(task.get("objective") or task.get("title") or "").strip()
            if not problem:
                raise HTTPException(status_code=404, detail="TASK_NOT_FOUND")
            return {
                "problem": problem[:4000],
                "kb_id": str(plan.kb_id) if plan.kb_id else None,
                "plan_id": str(plan.id),
                "task_key": task_key,
            }
    raise HTTPException(status_code=404, detail="TASK_NOT_FOUND")


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
        requested = body.status if body else None
        if requested is None and body is not None and body.done is not None:
            requested = "done" if body.done else "pending"
        try:
            new_structure, final_status = set_task_status(plan.structure_json, task_key, requested)
        except KeyError:
            raise HTTPException(status_code=404, detail="TASK_NOT_FOUND") from None
        plan.structure_json = new_structure  # JSONB 未挂 MutableDict, 必须整体重新赋值
        return {
            "task_key": task_key,
            "status": final_status,
            "done": final_status == "done",
            "progress": plan_progress(new_structure),
        }


@router.post("/{plan_id}/tasks/{task_key}/complete")
async def complete_reading_task(
    plan_id: uuid.UUID,
    task_key: str,
    ctx: AuthContext = Depends(api_rate_limited),
):
    """Explicitly complete a reading task; merely opening a link never completes it."""
    async with tenant_session(ctx.tenant_id) as session:
        try:
            return await complete_plan_task(
                session,
                tenant_id=ctx.tenant_id,
                user_id=ctx.user_id,
                plan_id=plan_id,
                task_key=task_key,
                source_type="manual_read",
                source_id=f"{plan_id}:{task_key}",
            )
        except PlanTaskAttributionError as exc:
            status = 404 if exc.code == "PLAN_TASK_NOT_FOUND" else 409
            raise HTTPException(status_code=status, detail=exc.code) from None


@router.post("/{plan_id}/adjust")
async def adjust(plan_id: uuid.UUID, ctx: AuthContext = Depends(api_rate_limited)):
    """依据当前学情调整计划 (F3-A3): 薄弱 KC 无对应任务 → 插补强任务 + 写调整审计。"""
    async with tenant_session(ctx.tenant_id) as session:
        plan = await _load_plan(session, plan_id, ctx)
        result = await adjust_plan(session, ctx.user_id, ctx.tenant_id, plan, trigger="manual")
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


@tutor_router.get("/entrypoints")
async def tutor_entrypoints(request: Request, ctx: AuthContext = Depends(api_rate_limited)):
    """Return safe IDs for context-aware Tutor entry cards."""
    now_dt = await timectl.now(request.app.state.redis)
    async with tenant_session(ctx.tenant_id, org_ids=ctx.org_ids) as session:
        plan = await latest_plan(session, ctx.user_id)
        today_task = None
        if plan is not None:
            snapshot = plan_schedule_snapshot(
                plan.created_at.astimezone(CN_TZ).date(),
                plan.structure_json,
                now_dt.astimezone(CN_TZ).date(),
            )
            due_keys = set(snapshot["today_tasks"]) | set(snapshot["overdue_tasks"])
            for stage_idx, day_idx, task_idx, _stage, _day, task in iter_tasks(
                plan.structure_json
            ):
                task_key = make_task_key(stage_idx, day_idx, task_idx)
                if task_key in due_keys and task_status(task) == "pending":
                    today_task = {
                        "status": "ready",
                        "plan_id": str(plan.id),
                        "task_key": task_key,
                    }
                    break
        if today_task is None:
            today_task = {
                "status": "empty",
                "reason": "NO_PENDING_TODAY_TASK",
            }
        error_ids = [
            str(error_id)
            for error_id in (
                await session.scalars(
                    select(ErrorItem.id)
                    .join(Question, Question.id == ErrorItem.question_id)
                    .where(
                        ErrorItem.user_id == ctx.user_id,
                        ErrorItem.verification_status == "verified",
                        ErrorItem.state.in_(("new", "reviewing")),
                    )
                    .order_by(ErrorItem.created_at.desc(), ErrorItem.id.desc())
                    .limit(20)
                )
            ).all()
        ]
    errors = (
        {"status": "ready", "error_ids": error_ids}
        if error_ids
        else {"status": "empty", "reason": "NO_AVAILABLE_ERRORS", "error_ids": []}
    )
    return {"today_task": today_task, "errors": errors}


@tutor_router.get("/{thread_local_id}")
async def get_tutor_session(
    thread_local_id: uuid.UUID,
    request: Request,
    ctx: AuthContext = Depends(api_rate_limited),
):
    """从 LangGraph checkpoint 恢复当前用户的一次辅导现场。"""
    runtime = getattr(request.app.state, "graphs", None)
    if runtime is None:
        raise HTTPException(status_code=503, detail="GRAPHS_UNAVAILABLE")
    thread_id = runtime.thread_id("tutor", str(ctx.user_id), str(thread_local_id))
    graph = runtime.graph("tutor")
    state = await graph.aget_state({"configurable": {"thread_id": thread_id}})
    values = dict(state.values)
    if not values:
        raise HTTPException(status_code=404, detail="NOT_FOUND")

    reply_levels = iter(
        int(event.get("hint_level") or 0)
        for event in values.get("trace") or []
        if event.get("node") == "respond" and event.get("event") == "replied"
    )
    messages = []
    for message in values.get("messages") or []:
        role = message.get("role")
        content = message.get("content")
        if role not in {"user", "assistant"} or not isinstance(content, str):
            continue
        item = {"role": role, "content": content}
        if role == "assistant":
            item["hint_level"] = next(reply_levels, int(values.get("hint_level") or 0))
        messages.append(item)
    return {
        "thread_local_id": str(thread_local_id),
        "problem": values.get("problem", ""),
        "messages": messages,
        "hint_level": int(values.get("hint_level") or 0),
        "stuck_type": values.get("stuck_type", ""),
        "session_summary": values.get("session_summary", ""),
        "pending": bool(state.next),
    }


@tutor_router.post("")
async def tutor_turn(
    body: TutorTurn, request: Request, ctx: AuthContext = Depends(api_rate_limited)
):
    """苏格拉底辅导单轮 (F4): 同 thread 多轮推进三层提示状态机。"""
    runtime = getattr(request.app.state, "graphs", None)
    if runtime is None:
        raise HTTPException(status_code=503, detail="GRAPHS_UNAVAILABLE")
    local_id = str(body.thread_local_id or uuid.uuid4())
    problem = body.problem
    draft_id = ""
    attachment_id = ""
    problem_page_no = None
    if body.tutor_draft_id is not None:
        from ..models import TutorProblemDraft

        async with tenant_session(ctx.tenant_id) as session:
            draft = await session.scalar(
                select(TutorProblemDraft).where(
                    TutorProblemDraft.id == body.tutor_draft_id,
                    TutorProblemDraft.user_id == ctx.user_id,
                    TutorProblemDraft.tenant_id == ctx.tenant_id,
                )
            )
            if draft is None:
                raise HTTPException(status_code=404, detail="TUTOR_DRAFT_NOT_FOUND")
            if draft.status != "confirmed" or not draft.confirmed_problem_text:
                raise HTTPException(status_code=409, detail="TUTOR_DRAFT_NOT_READY")
            if draft.thread_local_id != local_id:
                raise HTTPException(status_code=409, detail="TUTOR_THREAD_MISMATCH")
            problem = draft.confirmed_problem_text
            draft_id = str(draft.id)
            attachment_id = str(draft.resource_id or "")
            problem_page_no = draft.confirmed_page_no
    thread_id = runtime.thread_id("tutor", str(ctx.user_id), local_id)
    graph = runtime.graph("tutor")
    config = {"configurable": {"thread_id": thread_id}}
    checkpoint = await graph.aget_state(config)
    existing = dict(checkpoint.values)
    stored_plan_id = str(existing.get("plan_id") or "")
    stored_task_key = str(existing.get("task_key") or "")
    requested_plan_id = str(body.plan_id or "")
    requested_task_key = body.task_key or ""
    if existing and not stored_plan_id and requested_plan_id:
        raise HTTPException(status_code=409, detail="TUTOR_PLAN_TASK_LATE_BIND")
    if stored_plan_id and requested_plan_id and (
        stored_plan_id != requested_plan_id or stored_task_key != requested_task_key
    ):
        raise HTTPException(status_code=409, detail="TUTOR_PLAN_TASK_MISMATCH")
    plan_id = stored_plan_id or requested_plan_id
    task_key = stored_task_key or requested_task_key
    if plan_id:
        async with tenant_session(ctx.tenant_id) as session:
            try:
                await validate_plan_task_attribution(
                    session,
                    tenant_id=ctx.tenant_id,
                    user_id=ctx.user_id,
                    plan_id=uuid.UUID(plan_id),
                    task_key=task_key,
                    source_type="tutor_summary",
                )
            except (ValueError, PlanTaskAttributionError) as exc:
                code = getattr(exc, "code", "PLAN_TASK_NOT_FOUND")
                status = 404 if code == "PLAN_TASK_NOT_FOUND" else 409
                raise HTTPException(status_code=status, detail=code) from None
    # 画像注入 (G9, P2-7 脱敏口径同问答): 读画像失败一律降级为"无画像"继续辅导
    profile = ProfileContext()
    band = ""
    try:
        profile = await build_profile_context(
            ctx.tenant_id,
            ctx.user_id,
            redis=getattr(request.app.state, "redis", None),
            role="tutor",
        )
    except Exception:  # noqa: BLE001
        log.exception("tutor_profile_summary_failed")
    try:
        band = await problem_mastery_band(ctx.tenant_id, ctx.user_id, problem)
    except Exception:  # noqa: BLE001
        # 与画像同样的降级口径: 算不出档位就按无画像走原有起始层级, 不拖垮辅导主链路
        log.exception("tutor_mastery_band_failed")
    payload = {
        "tenant_id": str(ctx.tenant_id),
        "user_id": str(ctx.user_id),
        "request_id": getattr(request.state, "request_id", ""),
        "problem": problem,
        "tutor_draft_id": draft_id,
        "attachment_id": attachment_id,
        "problem_page_no": problem_page_no,
        "messages": [{"role": "user", "content": body.message}],
        # 画像摘要进 respond 的 system prompt; 薄弱 KC 名供开场关联卡点 (仅 KC 名, 无数值)
        "profile_summary": profile.summary,
        "profile_weak_points": profile.weak_points,
        # 掌握度档只决定起始 hint_level, 不进 prompt (P2-7: 数值与档位都不出境)
        "mastery_band": band,
    }
    if plan_id:
        payload["plan_id"] = plan_id
        payload["task_key"] = task_key
    state = await graph.ainvoke(payload, config)
    # F4→F8 联动: 仅当本轮走了 summarize (trace 末项) 才把小结写入画像 —
    # trace/session_summary 会随 checkpointer 跨轮保留, 不能只看非空, 否则同 thread 续聊会重复计分。
    mastery_changes: list[dict] = []
    plan_task_completion = None
    summary_raw = state.get("session_summary", "")
    trace = state.get("trace") or []
    if summary_raw and trace and trace[-1].get("node") == "summarize":
        try:
            summary = json.loads(summary_raw)
        except ValueError:
            summary = None
        if isinstance(summary, dict):
            mastery_changes = await apply_tutor_summary(ctx, summary)
        if plan_id and task_key:
            try:
                async with tenant_session(ctx.tenant_id) as session:
                    plan_task_completion = await complete_plan_task(
                        session,
                        tenant_id=ctx.tenant_id,
                        user_id=ctx.user_id,
                        plan_id=uuid.UUID(plan_id),
                        task_key=task_key,
                        source_type="tutor_summary",
                        source_id=local_id,
                    )
            except (ValueError, PlanTaskAttributionError) as exc:
                code = getattr(exc, "code", "PLAN_TASK_NOT_FOUND")
                log.warning(
                    "tutor_plan_task_completion_failed",
                    plan_id=plan_id,
                    task_key=task_key,
                    code=code,
                )
                plan_task_completion = {"completed": False, "error_code": code}
    return {
        "thread_local_id": local_id,
        "reply": state.get("reply", ""),
        "hint_level": state.get("hint_level", 0),
        "stuck_type": state.get("stuck_type", ""),
        "session_summary": summary_raw,
        "mastery_changes": mastery_changes,
        "plan_task_completion": plan_task_completion,
    }
