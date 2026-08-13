"""F3 "计划是活的": 任务状态 + 依据学情自动插入补强任务。

任务状态内嵌在 structure_json 任务对象的 ``status`` 字段中，计划结构是任务与进度的
唯一权威载体。每次变更都深拷贝并整体回写 JSONB，避免原地修改无法被 ORM 识别。

每个任务使用服务端生成的 UUID task_id。结构位置只用于遍历，不能成为任务身份；
前端只回传服务端下发的 UUID，非法或不匹配一律 404。

注: JSONB 列未挂 MutableDict, 原地改 dict 不会被 SQLAlchemy 标记为脏 —— 本模块
一律深拷贝后整体重新赋值 (structure_json = new_structure)。
"""

from __future__ import annotations

import copy
import uuid
from datetime import UTC, date, datetime
from typing import Any

from eduagents_core.kc.types import normalize_kc_name
from eduagents_core.logging import get_logger
from eduagents_core.mastery.thresholds import REINFORCE_THRESHOLD, evidence_state, zone_label
from sqlalchemy import select

from .models import KnowledgeComponent, MasteryLog, MasteryRecord, Plan, PlanAdjustment

log = get_logger("plan_adjust")

TASK_STATUSES: tuple[str, ...] = ("pending", "done", "skipped")
PLAN_TASK_SOURCE_ACTIONS: dict[str, set[str]] = {
    "quiz_attempt": {"practice", "mixed"},
    "tutor_summary": {"tutor", "mixed", "read"},
    "review_answer": {"review_errors", "mixed"},
    "manual_read": {"read", "mixed"},
}


class PlanTaskAttributionError(Exception):
    """A stable, user-safe failure raised while validating plan attribution."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code

# 薄弱判据 = REINFORCE_THRESHOLD (掌握度阈值唯一来源 eduagents_core.mastery.thresholds)
_REINFORCE_PCT = round(REINFORCE_THRESHOLD * 100)  # 中文理由里的百分数表述
LOW_SCORE_THRESHOLD = 0.6  # 单次测评正确率 < 0.6 视为低分, 触发自动调整 (非掌握度阈值)
REINFORCE_MINUTES = 30  # 补强任务时长 (固定, 与 F3-A1 日预算校验解耦)
MAX_INSERTS_PER_ADJUST = 5  # 单次调整最多插入的补强任务数 (防一次灌满计划)


def new_task_id() -> str:
    """Create the only task identity format accepted by the plan service."""
    return str(uuid.uuid4())


def task_identity(task: dict) -> str | None:
    value = task.get("task_id")
    if not isinstance(value, str):
        return None
    try:
        return str(uuid.UUID(value))
    except (TypeError, ValueError):
        return None


def _stages(structure: dict | None) -> list:
    stages = (structure or {}).get("stages")
    return stages if isinstance(stages, list) else []


def iter_tasks(structure: dict | None):
    """遍历 (stage_idx, day_idx, task_idx, stage, day, task); 非法层级静默跳过。"""
    for si, stage in enumerate(_stages(structure)):
        if not isinstance(stage, dict):
            continue
        days = stage.get("days")
        for di, day in enumerate(days if isinstance(days, list) else []):
            if not isinstance(day, dict):
                continue
            tasks = day.get("tasks")
            for ti, task in enumerate(tasks if isinstance(tasks, list) else []):
                if isinstance(task, dict):
                    yield si, di, ti, stage, day, task


def task_status(task: dict) -> str:
    """Return the persisted task status; every stored task has an explicit status."""
    raw = task.get("status")
    if isinstance(raw, str) and raw in TASK_STATUSES:
        return raw
    return "pending"


def plan_schedule_snapshot(created: date, structure: dict | None, today: date) -> dict:
    """Return the server-owned day buckets used by plan detail and today.

    The plan clock is a calendar clock: the creation date is day 1.  Tasks
    whose malformed structure has no usable ``day`` are deliberately put in the
    overdue bucket, never silently guessed as today's task.  Once the plan is
    past its last numbered day, every unfinished numbered task is overdue;
    future-looking schedule entries must not be promoted to today.

    Bucket values are stable task UUIDs. The task payload itself remains in
    ``structure`` and carries the identity used by every downstream action.
    """
    current_day = max(1, (today - created).days + 1)
    numbered_days = [
        day.get("day")
        for _si, _di, _ti, _stage, day, _task in iter_tasks(structure)
        if isinstance(day.get("day"), int) and not isinstance(day.get("day"), bool)
    ]
    past_schedule = bool(numbered_days) and current_day > max(numbered_days)
    today_tasks: list[str] = []
    overdue_tasks: list[str] = []
    upcoming_tasks: list[str] = []
    for _stage_idx, _day_idx, _task_idx, _stage, day, task in iter_tasks(structure):
        current_task_id = task_identity(task)
        if current_task_id is None:
            continue
        day_number = day.get("day")
        if not isinstance(day_number, int) or isinstance(day_number, bool) or day_number <= 0:
            overdue_tasks.append(current_task_id)
        elif past_schedule or day_number < current_day:
            overdue_tasks.append(current_task_id)
        elif day_number == current_day:
            today_tasks.append(current_task_id)
        else:
            upcoming_tasks.append(current_task_id)
    return {
        "current_day": current_day,
        "today_tasks": today_tasks,
        "overdue_tasks": overdue_tasks,
        "upcoming_tasks": upcoming_tasks,
    }


def compute_task_locks(
    structure: dict | None,
    mastery_by_kc: dict[str, float] | None,
    prereq_by_kc: dict[str, list[str]] | None,
    evidence_state_by_kc: dict[str, str] | None = None,
) -> dict[str, dict]:
    """任务 -> 先修未达标信息 (task_id -> {"blockers": [...]})。纯函数, 零 IO。

    锁定是**派生态, 不落库**: 它依赖实时 p_mastery, 掌握度一变就该自动解锁。写进
    structure_json 会立刻陈旧, 还会污染 plan_progress 的分母口径 (locked 任务仍是
    "待完成", 不是被跳过)。因此只在读取侧计算, TASK_STATUSES 保持三态不变。

    判据: 任务任一 KC 的先修 KC 掌握度 < REINFORCE_THRESHOLD 即视为未达标。
    无掌握度记录的先修 KC 一律不算阻塞 —— 没学过不等于没掌握, 冷启动时把整棵树
    锁死会让新用户寸步难行 (F3 计划必须可执行)。
    已完成/已跳过的任务不锁: 学习者已经做过的事不该被事后判定为"不可做"。
    """
    if not mastery_by_kc or not prereq_by_kc:
        return {}
    # The explicit map is required for a lock decision. A caller that has no
    # evidence snapshot must not infer observed weakness from p_mastery.
    if evidence_state_by_kc is None:
        return {}
    locks: dict[str, dict] = {}
    for _si, _di, _ti, _stage, _day, task in iter_tasks(structure):
        if task_status(task) != "pending":
            continue
        blockers: list[dict] = []
        seen: set[str] = set()
        for name in task.get("kc_names") or []:
            if not isinstance(name, str):
                continue
            for prereq in prereq_by_kc.get(normalize_kc_name(name), ()):
                score = mastery_by_kc.get(prereq)
                if (
                    score is None
                    or score >= REINFORCE_THRESHOLD
                    or prereq in seen
                    or evidence_state_by_kc.get(prereq) != "observed"
                ):
                    continue
                seen.add(prereq)
                blockers.append({"kc_name": prereq, "zone": zone_label(score)})
        current_task_id = task_identity(task)
        if current_task_id is None:
            # Structure creation is the only place that should produce plan
            # tasks. Read-side helpers still expose normalized state for a
            # malformed in-memory object without inventing an identity.
            continue
        if blockers:
            locks[current_task_id] = {"blockers": blockers}
    return locks


def enrich_structure(
    structure: dict | None,
    resources: dict[str, dict] | None = None,
    locks: dict[str, dict] | None = None,
) -> dict:
    """读取侧加工 task_id/status and a same-plan resource object.

    Missing resource ids are represented as ``resource: None``.
    ``locked`` 是派生展示态 (见 compute_task_locks), 不进 status 也不落库。
    """
    out = copy.deepcopy(structure or {})
    if not isinstance(out, dict):
        return {"stages": []}
    for _si, _di, _ti, _stage, _day, task in iter_tasks(out):
        current_task_id = task_identity(task)
        task["task_id"] = current_task_id
        status = task_status(task)
        task["status"] = status
        lock = (locks or {}).get(current_task_id)
        task["locked"] = bool(lock)
        task["lock_blockers"] = lock["blockers"] if lock else []
        resource_id = task.get("resource_id")
        task["resource"] = resources.get(str(resource_id)) if resources and resource_id else None
    out.setdefault("stages", [])
    return out


def plan_progress(structure: dict | None) -> dict:
    """Return completion and skip-aware effective progress."""
    total = 0
    done = 0
    skipped = 0
    for *_idx, task in iter_tasks(structure):
        total += 1
        status = task_status(task)
        if status == "done":
            done += 1
        elif status == "skipped":
            skipped += 1
    pending = total - done - skipped
    effective_total = total - skipped
    effective_ratio = done / effective_total if effective_total else (1.0 if total else 0.0)
    return {
        "total": total,
        "done": done,
        "skipped": skipped,
        "pending": pending,
        "ratio": round(done / total, 4) if total else 0.0,
        "effective_ratio": round(effective_ratio, 4),
    }


def set_task_status(
    structure: dict | None, task_id: str, status: str
) -> tuple[dict, str]:
    """Set a task to pending/done/skipped and return an immutable copy."""
    if status not in TASK_STATUSES:
        raise ValueError("invalid task status")
    try:
        normalized_id = str(uuid.UUID(task_id))
    except (TypeError, ValueError):
        raise KeyError(task_id) from None
    new_structure = copy.deepcopy(structure or {})
    for _csi, _cdi, _cti, _stage, _day, task in iter_tasks(new_structure):
        if task_id_value := task_identity(task):
            if task_id_value != normalized_id:
                continue
        else:
            continue
        final = status
        task["status"] = final
        if final != "done":
            # A reopened/skipped task no longer has valid completion evidence.
            task.pop("completion", None)
        return new_structure, final
    raise KeyError(task_id)


def _task_for_id(structure: dict | None, requested_id: str) -> dict | None:
    try:
        normalized_id = str(uuid.UUID(requested_id))
    except (TypeError, ValueError):
        return None
    for _si, _di, _ti, _stage, _day, task in iter_tasks(structure):
        if task_identity(task) == normalized_id:
            return task
    return None


def _validate_completion_source(task: dict, source_type: str) -> None:
    allowed_actions = PLAN_TASK_SOURCE_ACTIONS.get(source_type)
    if allowed_actions is None:
        raise PlanTaskAttributionError("PLAN_TASK_SOURCE_INVALID")
    action_type = str(task.get("action_type") or "tutor")
    if action_type not in allowed_actions:
        raise PlanTaskAttributionError("PLAN_TASK_ACTIVITY_MISMATCH")
    if source_type == "manual_read" and not (task.get("resource_id") or task.get("resource")):
        raise PlanTaskAttributionError("PLAN_TASK_READING_RESOURCE_REQUIRED")


async def validate_plan_task_attribution(
    session,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    plan_id: uuid.UUID,
    task_id: str,
    source_type: str,
) -> None:
    """Validate an attribution without mutating the plan.

    Both tenant and user ownership are checked even when RLS is enabled.  A
    missing/foreign plan and a missing task intentionally share 404-style
    codes so callers do not disclose another learner's plan structure.
    """
    plan = await session.scalar(
        select(Plan).where(
            Plan.id == plan_id,
            Plan.tenant_id == tenant_id,
            Plan.user_id == user_id,
        )
    )
    if plan is None:
        raise PlanTaskAttributionError("PLAN_TASK_NOT_FOUND")
    if plan.status != "active":
        raise PlanTaskAttributionError("PLAN_NOT_ACTIVE")
    task = _task_for_id(plan.structure_json, task_id)
    if task is None:
        raise PlanTaskAttributionError("PLAN_TASK_NOT_FOUND")
    _validate_completion_source(task, source_type)


async def complete_plan_task(
    session,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    plan_id: uuid.UUID,
    task_id: str,
    source_type: str,
    source_id: str,
    completed_at: datetime | None = None,
) -> dict:
    """Idempotently mark one owned task done with bounded completion evidence.

    Only safe identifiers, the source kind and a server timestamp are written
    to ``structure_json``.  The plan row is locked to avoid losing concurrent
    task updates.
    """
    plan = await session.scalar(
        select(Plan)
        .where(
            Plan.id == plan_id,
            Plan.tenant_id == tenant_id,
            Plan.user_id == user_id,
        )
        .with_for_update()
    )
    if plan is None:
        raise PlanTaskAttributionError("PLAN_TASK_NOT_FOUND")
    task = _task_for_id(plan.structure_json, task_id)
    if task is None:
        raise PlanTaskAttributionError("PLAN_TASK_NOT_FOUND")
    previous = task.get("completion") if isinstance(task.get("completion"), dict) else None
    if task_status(task) == "done" and previous:
        return {
            "task_id": task_id,
            "status": "done",
            "completed": True,
            "changed": False,
            "completion": previous,
            "progress": plan_progress(plan.structure_json),
        }
    if plan.status != "active":
        raise PlanTaskAttributionError("PLAN_NOT_ACTIVE")
    _validate_completion_source(task, source_type)

    new_structure = copy.deepcopy(plan.structure_json or {})
    target = _task_for_id(new_structure, task_id)
    if target is None:  # Defensive: structure changed between copy and lookup.
        raise PlanTaskAttributionError("PLAN_TASK_NOT_FOUND")
    completion = {
        "source_type": source_type,
        "source_id": str(source_id)[:128],
        "completed_at": (completed_at or datetime.now(UTC)).astimezone(UTC).isoformat(),
    }
    target["status"] = "done"
    target["completion"] = completion
    plan.structure_json = new_structure
    progress = plan_progress(new_structure)
    if progress["pending"] == 0 and progress["total"] > 0:
        plan.status = "completed"
    await session.flush()
    return {
        "task_id": task_id,
        "status": "done",
        "completed": True,
        "changed": True,
        "completion": completion,
        "progress": plan_progress(new_structure),
    }


def _existing_kc_keys(structure: dict | None) -> set[str]:
    """计划里已被任务覆盖的 KC 名 (归一化)。"""
    keys: set[str] = set()
    for *_idx, task in iter_tasks(structure):
        for name in task.get("kc_names") or []:
            if isinstance(name, str) and name.strip():
                keys.add(normalize_kc_name(name))
    return keys


def _pending_day_slots(structure: dict | None) -> list[tuple[int, int]]:
    """未完成日 (有未勾选任务或空任务的日) 的 (stage_idx, day_idx), 按时间先后。

    全部日程都已完成时退回最后一日 —— 补强任务宁可挤在最后一天, 也不能凭空丢弃。
    """
    pending: list[tuple[int, int]] = []
    last: tuple[int, int] | None = None
    for si, stage in enumerate(_stages(structure)):
        if not isinstance(stage, dict):
            continue
        days = stage.get("days")
        for di, day in enumerate(days if isinstance(days, list) else []):
            if not isinstance(day, dict):
                continue
            last = (si, di)
            tasks = day.get("tasks")
            tasks = tasks if isinstance(tasks, list) else []
            if not tasks or any(
                isinstance(t, dict) and task_status(t) == "pending" for t in tasks
            ):
                pending.append((si, di))
    if pending:
        return pending
    return [last] if last is not None else []


def _day_number(structure: dict, stage_idx: int, day_idx: int) -> int:
    day = _stages(structure)[stage_idx]["days"][day_idx]
    value = day.get("day")
    return int(value) if isinstance(value, int) else day_idx + 1


async def _weak_kcs(session, user_id: uuid.UUID) -> list[tuple[str, str, float]]:
    """Only observed, below-threshold KC rows, ordered from weak to strong."""
    from sqlalchemy import func

    rows = (
        await session.execute(
            select(
                MasteryRecord.kc_id,
                KnowledgeComponent.canonical_name,
                MasteryRecord.p_mastery,
                func.count(MasteryLog.id),
            )
            .join(KnowledgeComponent, KnowledgeComponent.id == MasteryRecord.kc_id)
            .join(
                MasteryLog,
                (MasteryLog.user_id == MasteryRecord.user_id)
                & (MasteryLog.kc_id == MasteryRecord.kc_id),
            )
            .where(
                MasteryRecord.user_id == user_id,
                MasteryRecord.p_mastery < REINFORCE_THRESHOLD,
            )
            .group_by(MasteryRecord.kc_id, KnowledgeComponent.canonical_name, MasteryRecord.p_mastery)
            .order_by(MasteryRecord.p_mastery.asc())
        )
    ).all()
    return [
        (str(kc_id), name, float(p))
        for kc_id, name, p, count in rows
        if evidence_state(int(count)) == "observed"
    ]


async def active_plan(session, user_id: uuid.UUID) -> Plan | None:
    """Return the explicitly active plan; creation time is not a lifecycle rule."""
    return await session.scalar(
        select(Plan).where(
            Plan.tenant_id == user_id,
            Plan.user_id == user_id,
            Plan.status == "active",
        )
    )


async def adjust_plan(
    session,
    user_id: uuid.UUID,
    tenant_id: uuid.UUID,
    plan: Plan,
    *,
    trigger: str = "manual",
    reason_prefix: str = "",
) -> dict:
    """依据当前学情调整计划: 薄弱 KC 无对应任务 → 在最近未完成日插入补强任务。

    - 薄弱判据: mastery_records.p_mastery < REINFORCE_THRESHOLD (由弱到强, 最多插 5 个);
    - 已覆盖判据: 计划中任一任务的 kc_names 归一化后命中该 KC 名 → 跳过 (不重复插);
    - 落点: 未完成日按时间先后轮转分配 (避免全部堆在同一天); 计划已全部完成则落最后一日;
    - 有插入才写 plan_adjustments (无插入不是一次调整, 不留噪声审计)。

    调用方负责事务 (session 由外部 tenant_session 提供)。
    """
    structure = plan.structure_json or {}
    weak = await _weak_kcs(session, user_id)
    covered = _existing_kc_keys(structure)
    candidates = [
        (kc_id, name, p) for kc_id, name, p in weak if normalize_kc_name(name) not in covered
    ][:MAX_INSERTS_PER_ADJUST]

    if not candidates:
        reason = (
            f"当前没有掌握度低于 {_REINFORCE_PCT}% 且计划未覆盖的知识点, 计划无需调整。"
            if weak
            else f"当前没有掌握度低于 {_REINFORCE_PCT}% 的知识点, 计划无需调整。"
        )
        return {"inserted": [], "reason": reason_prefix + reason, "adjustment_id": None}

    new_structure = copy.deepcopy(structure)
    slots = _pending_day_slots(new_structure)
    if not slots:  # 空计划 (无任何日) → 无处可插, 如实返回
        return {
            "inserted": [],
            "reason": reason_prefix + "计划中没有可插入的日程, 未做调整。",
            "adjustment_id": None,
        }

    inserted: list[dict[str, Any]] = []
    for i, (kc_id, name, p) in enumerate(candidates):
        stage_idx, day_idx = slots[i % len(slots)]
        tasks = new_structure["stages"][stage_idx]["days"][day_idx].setdefault("tasks", [])
        task = {
            "task_id": new_task_id(),
            "title": f"补强: {name}",
            "objective": f"补强{name}，把掌握度从当前薄弱状态提升到可以独立应用",
            "instructions": [
                f"回顾{name}的定义、条件和典型错误",
                "完成 3 道由易到难的针对性练习并订正",
                "用一句话说明仍然容易出错的地方",
            ],
            "completion_criteria": f"能独立完成至少 2 道{name}相关题，并解释关键步骤",
            "minutes": REINFORCE_MINUTES,
            "kc_names": [name],
            "resource_ref": None,
            "action_type": "practice",
            "practice_count": 3,
            "difficulty": "mid",
            "status": "pending",
            "source": "adjust",  # 前端据此高亮"本次插入"
        }
        tasks.append(task)
        inserted.append(
            {
                "task_id": task["task_id"],
                "day": _day_number(new_structure, stage_idx, day_idx),
                "stage_name": new_structure["stages"][stage_idx].get("name", ""),
                "title": task["title"],
                "objective": task["objective"],
                "instructions": task["instructions"],
                "completion_criteria": task["completion_criteria"],
                "minutes": REINFORCE_MINUTES,
                "kc_id": kc_id,
                "kc_names": [name],
                "action_type": task["action_type"],
                "practice_count": task["practice_count"],
                "difficulty": task["difficulty"],
                "p_mastery": round(p, 4),
            }
        )

    detail = "、".join(f"{it['kc_names'][0]}({round(it['p_mastery'] * 100)}%)" for it in inserted)
    reason = (
        f"{reason_prefix}检测到 {len(inserted)} 个掌握度低于 {_REINFORCE_PCT}% 的知识点: {detail}; "
        f"已在最近未完成的日程中插入 {len(inserted)} 个补强任务 (每个 {REINFORCE_MINUTES} 分钟)。"
    )
    plan.structure_json = new_structure
    plan.version = (plan.version or 1) + 1
    adjustment = PlanAdjustment(
        tenant_id=tenant_id,
        plan_id=plan.id,
        reason=reason,
        diff_json={
            "trigger": trigger,
            "threshold": REINFORCE_THRESHOLD,
            "inserted": inserted,
        },
    )
    session.add(adjustment)
    await session.flush()
    log.info(
        "plan_adjusted", plan_id=str(plan.id), trigger=trigger, inserted=len(inserted)
    )
    return {"inserted": inserted, "reason": reason, "adjustment_id": str(adjustment.id)}


async def adjust_on_low_score(ctx, accuracy: float) -> dict | None:
    """低分测评 (正确率 < 0.6) 自动触发一次计划调整; 无活跃计划或异常时返回 None。

    本函数是 services.record_attempts 的尾部挂载点: 判分主流程已完成并提交,
    调整失败绝不能反噬作答结果, 因此整体吞异常只记日志 (向后兼容第一)。
    """
    from .db import tenant_session

    try:
        async with tenant_session(ctx.tenant_id) as session:
            plan = await active_plan(session, ctx.user_id)
            if plan is None:
                return None
            result = await adjust_plan(
                session,
                ctx.user_id,
                ctx.tenant_id,
                plan,
                trigger="low_score_attempt",
                reason_prefix=f"本次测评正确率 {round(accuracy * 100)}% 低于 60% 阈值; ",
            )
            result["plan_id"] = str(plan.id)
            return result
    except Exception:  # noqa: BLE001 — 计划调整失败不影响测评判分结果
        log.exception("plan_auto_adjust_failed")
        return None
