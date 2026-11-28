"""F3 "计划是活的": 任务状态 + 依据学情建议补强任务并在确认后应用。

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
from eduagents_core.mastery.thresholds import (
    REINFORCE_THRESHOLD,
    VALID_MASTERY_EVIDENCE_TYPES,
    evidence_state,
    zone_label,
)
from sqlalchemy import func, select

from .budget import CN_TZ
from .models import (
    AssessmentSubmission,
    Attempt,
    Chunk,
    Document,
    KnowledgeComponent,
    MasteryLog,
    MasteryRecord,
    Plan,
    PlanAdjustment,
    PlanResource,
    Question,
    Quiz,
)

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
_LOW_SCORE_TRIGGER = "low_score_attempt"
REINFORCE_MINUTES = 30  # 每个知识点的补强时间上限
MAX_TOPICS_PER_ADJUST = 5  # 每个知识点可按剩余预算分到多个学习日


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
) -> dict[str, Any]:
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
    if plan.plan_mode == "course_bound" and plan.kb_id is None:
        raise PlanTaskAttributionError("PLAN_KB_UNAVAILABLE")
    return {
        "plan_id": str(plan.id),
        "task_id": task_id,
        "plan_mode": plan.plan_mode,
        "kb_id": str(plan.kb_id) if plan.kb_id else None,
        "evidence_policy": (
            "calibration_only" if plan.plan_mode == "goal_only" else "mastery_eligible"
        ),
    }


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
            "plan_status": plan.status,
            "version": plan.version,
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
    plan.version = (plan.version or 1) + 1
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
        "plan_status": plan.status,
        "version": plan.version,
    }


def _existing_kc_keys(structure: dict | None) -> set[str]:
    """计划里已被任务覆盖的 KC 名 (归一化)。"""
    keys: set[str] = set()
    for *_idx, task in iter_tasks(structure):
        if task_status(task) != "pending" or task.get("action_type") not in {"practice", "mixed", "review_errors"}:
            continue
        for name in task.get("kc_names") or []:
            if isinstance(name, str) and name.strip():
                keys.add(normalize_kc_name(name))
    return keys


def _pending_day_slots(structure: dict | None) -> list[tuple[int, int]]:
    """未完成日 (有未勾选任务或空任务的日) 的 (stage_idx, day_idx), 按时间先后。

    已完成的日程不再追加任务。
    """
    pending: list[tuple[int, int]] = []
    for si, stage in enumerate(_stages(structure)):
        if not isinstance(stage, dict):
            continue
        days = stage.get("days")
        for di, day in enumerate(days if isinstance(days, list) else []):
            if not isinstance(day, dict):
                continue
            tasks = day.get("tasks")
            tasks = tasks if isinstance(tasks, list) else []
            if not tasks or any(
                isinstance(t, dict) and task_status(t) == "pending" for t in tasks
            ):
                pending.append((si, di))
    return sorted(pending, key=lambda slot: _day_number(structure, *slot))


def _day_number(structure: dict, stage_idx: int, day_idx: int) -> int:
    day = _stages(structure)[stage_idx]["days"][day_idx]
    value = day.get("day")
    return int(value) if isinstance(value, int) else day_idx + 1


async def _weak_kcs(
    session, user_id: uuid.UUID, kb_id: uuid.UUID
) -> list[tuple[str, str, float]]:
    """Only observed, below-threshold KC rows, ordered from weak to strong."""
    rows = (
        await session.execute(
            select(
                MasteryRecord.kc_id,
                KnowledgeComponent.canonical_name,
                MasteryRecord.p_mastery,
                func.count(func.distinct(MasteryLog.evidence_id)),
            )
            .join(KnowledgeComponent, KnowledgeComponent.id == MasteryRecord.kc_id)
            .join(
                MasteryLog,
                (MasteryLog.user_id == MasteryRecord.user_id)
                & (MasteryLog.kc_id == MasteryRecord.kc_id)
                & MasteryLog.evidence_type.in_(VALID_MASTERY_EVIDENCE_TYPES)
                & MasteryLog.evidence_id.is_not(None),
            )
            .where(
                MasteryRecord.user_id == user_id,
                MasteryRecord.p_mastery < REINFORCE_THRESHOLD,
                KnowledgeComponent.kb_id == kb_id,
                KnowledgeComponent.superseded_by_id.is_(None),
                KnowledgeComponent.source_kind != "chapter_fallback",
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


def _stored_uuid(value: object) -> uuid.UUID | None:
    if isinstance(value, str):
        try:
            return uuid.UUID(value)
        except ValueError:
            pass
    return None


async def _wrong_quiz_evidence(session, plan, quiz_results):
    """Return (submission id, attempt id, KC id) for results that still support reinforcement."""
    submissions_by_attempt: dict[tuple[uuid.UUID, uuid.UUID, uuid.UUID], set[uuid.UUID]] = {}
    for submission_id, quiz_id, results in quiz_results:
        if not isinstance(results, list):
            continue
        for result in results:
            if not isinstance(result, dict) or result.get("correct") is not False:
                continue
            attempt_id = _stored_uuid(result.get("attempt_id"))
            question_id = _stored_uuid(result.get("question_id"))
            if attempt_id is not None and question_id is not None:
                key = (attempt_id, question_id, quiz_id)
                submissions_by_attempt.setdefault(key, set()).add(submission_id)
    if not submissions_by_attempt:
        return set()

    # The quiz mastery log records the normalized KC mapping used at grading time.
    query = (
        select(Attempt.id, Question.id, Quiz.id, MasteryLog.kc_id)
        .select_from(Attempt)
        .join(Question, Question.id == Attempt.question_id)
        .join(Quiz, Quiz.id == Question.quiz_id)
        .join(MasteryLog, MasteryLog.evidence_id == Attempt.id)
        .where(
            Attempt.id.in_({attempt_id for attempt_id, _, _ in submissions_by_attempt}),
            Attempt.is_correct.is_(False),
            Attempt.user_id == plan.user_id,
            Attempt.tenant_id == plan.tenant_id,
            Question.tenant_id == plan.tenant_id,
            Quiz.user_id == plan.user_id,
            Quiz.tenant_id == plan.tenant_id,
            Quiz.kb_id == plan.kb_id,
            MasteryLog.user_id == plan.user_id,
            MasteryLog.tenant_id == plan.tenant_id,
            MasteryLog.evidence_type == "quiz_attempt",
        )
    )
    rows = (await session.execute(query)).all()
    return {
        (submission_id, attempt_id, str(kc_id))
        for attempt_id, question_id, quiz_id, kc_id in rows
        for submission_id in submissions_by_attempt.get((attempt_id, question_id, quiz_id), ())
    }


async def active_plan(session, tenant_id: uuid.UUID, user_id: uuid.UUID) -> Plan | None:
    """Return the explicitly active plan; creation time is not a lifecycle rule."""
    return await session.scalar(
        select(Plan).where(
            Plan.tenant_id == tenant_id,
            Plan.user_id == user_id,
            Plan.status == "active",
        )
    )


def reinforcement_slots(structure: dict, daily_minutes: int, first_day: int = 1):
    """Allocate only free time, or explicitly propose replacing an optional leaf."""
    dependencies = {
        dep for *_, task in iter_tasks(structure) for dep in (task.get("depends_on") or [])
    }
    for si, di in _pending_day_slots(structure):
        day = structure["stages"][si]["days"][di]
        number = _day_number(structure, si, di)
        if number < first_day:
            continue
        active = [
            task
            for task in day.get("tasks", [])
            if isinstance(task, dict) and task_status(task) != "skipped"
        ]
        if any(
            not isinstance(task.get("minutes"), (int, float)) or task["minutes"] < 0
            for task in active
        ):
            continue
        free = int(daily_minutes - sum(task["minutes"] for task in active))
        replacement = None
        if free < 5:
            optional = [
                task
                for task in active
                if task_status(task) == "pending"
                and (task.get("optional") is True or task.get("learning_phase") == "review")
                and task_identity(task)
                and task.get("step_key") not in dependencies
                and free + task["minutes"] >= 5
            ]
            if optional:
                replacement = min(optional, key=lambda task: task["minutes"])
                free += int(replacement["minutes"])
        if free >= 5:
            yield number, structure["stages"][si].get("name", ""), free, replacement


async def adjust_plan(
    session,
    user_id,
    tenant_id,
    plan,
    *,
    trigger="manual",
    reason_prefix="",
    submission_id=None,
    wrong_attempts_by_kc=None,
    first_day=1,
):
    """Save a versioned proposal. Only explicit confirmation changes the plan."""

    def empty(message):
        return {
            "inserted": [],
            "reason": reason_prefix + message,
            "adjustment_id": None,
            "status": "none",
        }

    if plan.plan_mode != "course_bound" or plan.kb_id is None:
        return empty("目标计划处于校准阶段，计划无需调整。")
    automatic = trigger == _LOW_SCORE_TRIGGER
    wrong_attempts_by_kc = wrong_attempts_by_kc or {}
    weak = await _weak_kcs(session, user_id, plan.kb_id)
    covered = _existing_kc_keys(plan.structure_json)
    candidates = [
        (kc, name, score)
        for kc, name, score in weak
        if (not automatic or kc in wrong_attempts_by_kc) and normalize_kc_name(name) not in covered
    ]
    if not candidates:
        if automatic:
            return empty("本次测评没有可安排补强的错题知识点，计划无需调整。")
        return empty("当前没有未覆盖的薄弱知识点，计划无需调整。")

    slots = list(
        reinforcement_slots(plan.structure_json, int(round(plan.budget_hours * 60)), first_day)
    )
    inserted = []
    for kc, name, score in candidates[:MAX_TOPICS_PER_ADJUST]:
        remaining = REINFORCE_MINUTES
        if automatic:
            evidence = {"type": "quiz_attempt", "id": wrong_attempts_by_kc[kc]}
        else:
            latest = (
                await session.execute(
                    select(MasteryLog.evidence_type, MasteryLog.evidence_id)
                    .where(
                        MasteryLog.user_id == user_id,
                        MasteryLog.kc_id == uuid.UUID(kc),
                        MasteryLog.evidence_id.is_not(None),
                        MasteryLog.evidence_type.in_(VALID_MASTERY_EVIDENCE_TYPES),
                    )
                    .order_by(MasteryLog.at.desc(), MasteryLog.id)
                    .limit(1)
                )
            ).first()
            evidence = {"type": latest[0], "id": str(latest[1])} if latest else None
        source = (
            await session.execute(
                select(Chunk.id, Document.id, Document.filename, Chunk.heading_path)
                .join(Document, Chunk.doc_id == Document.id)
                .where(
                    Document.kb_id == plan.kb_id,
                    Chunk.parent_id.is_not(None),
                    Chunk.kc_ids.contains([kc]),
                )
                .order_by(Chunk.id)
                .limit(1)
            )
        ).first()
        for index, (day, stage, free, replacement) in enumerate(slots):
            minutes = min(remaining, free)
            if minutes < 5:
                continue
            count = max(1, minutes // 10)
            inserted.append(
                {
                    "task_id": new_task_id(),
                    "day": day,
                    "stage_name": stage,
                    "kc_id": kc,
                    "kc_names": [name],
                    "title": f"补强: {name}",
                    "minutes": minutes,
                    "practice_count": count,
                    "action_type": "practice",
                    "difficulty": "mid",
                    "objective": f"独立应用{name}",
                    "instructions": [f"回顾{name}的适用条件", f"完成 {count} 道练习并订正"],
                    "completion_criteria": "能说明解题依据并订正错误",
                    "p_mastery": round(score, 4),
                    "evidence": evidence,
                    "material": {
                        "chunk_id": str(source[0]),
                        "doc_id": str(source[1]),
                        "title": source[2],
                        "heading": source[3],
                    }
                    if source
                    else None,
                    "replaces": {"id": replacement["task_id"], "title": replacement["title"]}
                    if replacement
                    else None,
                }
            )
            slots[index] = (day, stage, free - minutes, None)
            remaining -= minutes
            if remaining < 5:
                break
    if not inserted:
        return empty("剩余日程没有足够时间安排补强，请先调整每日预算或腾出时间。")
    diff = {
        "status": "proposed",
        "base_version": plan.version,
        "trigger": trigger,
        "submission_id": str(submission_id) if submission_id else None,
        "inserted": inserted,
    }
    existing = await session.scalar(
        select(PlanAdjustment)
        .where(
            PlanAdjustment.plan_id == plan.id,
            PlanAdjustment.diff_json["status"].astext == "proposed",
            PlanAdjustment.diff_json["base_version"].astext == str(plan.version),
            PlanAdjustment.diff_json["trigger"].astext == trigger,
        )
        .order_by(PlanAdjustment.at.desc())
        .limit(1)
    )
    if existing:

        def content(tasks):
            return [{k: v for k, v in task.items() if k != "task_id"} for task in tasks]

        if existing.diff_json.get("submission_id") == diff["submission_id"] and content(
            existing.diff_json["inserted"]
        ) == content(inserted):
            return {
                "inserted": existing.diff_json["inserted"],
                "reason": existing.reason,
                "adjustment_id": str(existing.id),
                "status": "proposed",
            }
    names = "、".join(dict.fromkeys(t["kc_names"][0] for t in inserted))
    reason = (
        reason_prefix
        + f"针对{names}，建议安排 {len(inserted)} 个补强任务，共 {sum(t['minutes'] for t in inserted)} 分钟。"
    )
    adjustment = PlanAdjustment(tenant_id=tenant_id, plan_id=plan.id, reason=reason, diff_json=diff)
    session.add(adjustment)
    await session.flush()
    return {
        "inserted": inserted,
        "reason": reason,
        "adjustment_id": str(adjustment.id),
        "status": "proposed",
    }


def adjustment_status(diff, plan, first_day, weak_ids, material_ids, quiz_evidence):
    status = diff.get("status", "applied")
    if status != "proposed":
        return status
    if plan.status != "active" or diff.get("base_version") != plan.version:
        return "stale"
    tasks = diff.get("inserted")
    if not isinstance(tasks, list) or not tasks:
        return "stale"
    automatic = diff.get("trigger") == _LOW_SCORE_TRIGGER
    submission_id = _stored_uuid(diff.get("submission_id")) if automatic else None
    for task in tasks:
        if not isinstance(task, dict):
            return "stale"
        day = task.get("day")
        kc_id = task.get("kc_id")
        material = task.get("material")
        if (
            not isinstance(day, int) or day < first_day
            or not isinstance(kc_id, str) or kc_id not in weak_ids
        ):
            return "stale"
        if material and (
            not isinstance(material, dict) or _stored_uuid(material.get("chunk_id")) not in material_ids
        ):
            return "stale"
        if automatic:
            evidence = task.get("evidence")
            if not isinstance(evidence, dict) or evidence.get("type") != "quiz_attempt":
                return "stale"
            if (submission_id, _stored_uuid(evidence.get("id")), kc_id) not in quiz_evidence:
                return "stale"
    return "proposed"


async def adjustment_evidence(session, plan, diffs):
    pending = [
        diff for diff in diffs
        if diff.get("status") == "proposed" and diff.get("base_version") == plan.version
    ]
    if (
        not pending or plan.status != "active"
        or plan.plan_mode != "course_bound" or plan.kb_id is None
    ):
        return set(), set(), set()

    submission_ids = {
        submission_id
        for diff in pending
        if diff.get("trigger") == _LOW_SCORE_TRIGGER
        and (submission_id := _stored_uuid(diff.get("submission_id"))) is not None
    }
    quiz_evidence = set()
    if submission_ids:
        quiz_results = (
            await session.execute(
                select(
                    AssessmentSubmission.id,
                    AssessmentSubmission.quiz_id,
                    AssessmentSubmission.result_json["results"],
                ).where(
                    AssessmentSubmission.id.in_(submission_ids),
                    AssessmentSubmission.tenant_id == plan.tenant_id,
                    AssessmentSubmission.user_id == plan.user_id,
                    AssessmentSubmission.kind == "quiz",
                )
            )
        ).all()
        quiz_evidence = await _wrong_quiz_evidence(session, plan, quiz_results)

    weak_ids = {kc for kc, _name, _score in await _weak_kcs(session, plan.user_id, plan.kb_id)}
    referenced = set()
    for diff in pending:
        tasks = diff.get("inserted")
        if not isinstance(tasks, list):
            continue
        for task in tasks:
            material = task.get("material") if isinstance(task, dict) else None
            if isinstance(material, dict) and (chunk_id := _stored_uuid(material.get("chunk_id"))):
                referenced.add(chunk_id)
    material_ids = (
        await session.scalars(
            select(Chunk.id)
            .join(Document)
            .where(Document.kb_id == plan.kb_id, Chunk.id.in_(referenced))
        )
        if referenced
        else []
    )
    return weak_ids, set(material_ids), quiz_evidence


async def confirm_adjustment(session, plan, adjustment_id, *, first_day=1):
    # The caller holds the plan row lock, including for idempotent replay.
    row = await session.scalar(
        select(PlanAdjustment).where(
            PlanAdjustment.plan_id == plan.id, PlanAdjustment.id == adjustment_id
        )
    )
    if row is None:
        raise PlanTaskAttributionError("PLAN_ADJUSTMENT_NOT_FOUND")
    diff = row.diff_json or {}
    if diff.get("status", "applied") != "applied":
        evidence = await adjustment_evidence(session, plan, [diff])
        if adjustment_status(diff, plan, first_day, *evidence) != "proposed":
            raise PlanTaskAttributionError("PLAN_ADJUSTMENT_STALE")
        structure = copy.deepcopy(plan.structure_json)
        days = {
            _day_number(structure, si, di): structure["stages"][si]["days"][di]
            for si, di in _pending_day_slots(structure)
        }
        for proposed in diff["inserted"]:
            tasks = days[proposed["day"]]["tasks"]
            if proposed["replaces"]:
                previous = next(t for t in tasks if t["task_id"] == proposed["replaces"]["id"])
                previous["status"] = "skipped"
            task = {
                key: proposed[key]
                for key in (
                    "task_id",
                    "title",
                    "objective",
                    "instructions",
                    "completion_criteria",
                    "minutes",
                    "kc_names",
                    "action_type",
                    "difficulty",
                    "practice_count",
                )
            }
            material = proposed.get("material")
            if material:
                source = await session.scalar(
                    select(Chunk)
                    .join(Document)
                    .where(
                        Chunk.id == uuid.UUID(material["chunk_id"]), Document.kb_id == plan.kb_id
                    )
                )
                if source is None:
                    raise PlanTaskAttributionError("PLAN_ADJUSTMENT_STALE")
                resource = PlanResource(
                    tenant_id=plan.tenant_id,
                    plan_id=plan.id,
                    source_type="kb_chunk",
                    kb_id=plan.kb_id,
                    doc_id=source.doc_id,
                    chunk_id=source.id,
                    title=material["title"],
                )
                session.add(resource)
                await session.flush()
                task["resource_id"] = str(resource.id)
            tasks.append(
                dict(task, status="pending", source="adjust", evidence=proposed.get("evidence"))
            )
        plan.structure_json = structure
        plan.version += 1
        row.diff_json = {**diff, "status": "applied"}
        await session.flush()
    return {
        "inserted": diff.get("inserted", []),
        "adjustment_id": str(row.id),
        "reason": row.reason,
        "status": "applied",
        "progress": plan_progress(plan.structure_json),
    }


async def adjust_on_low_score(
    ctx,
    accuracy: float,
    *,
    quiz_id: uuid.UUID,
    quiz_kb_id: uuid.UUID | None,
    attributed_plan_id: uuid.UUID | None,
    evidence_policy: str,
    submission_id: uuid.UUID,
    grading_results: list[dict],
    business_date=None,
    session=None,
) -> dict | None:
    """低分测评 (正确率 < 0.6) 自动触发一次计划调整; 无活跃计划或异常时返回 None。

    与提交结果共用事务时使用 savepoint；调整失败不回滚已保存作答。
    没有传入 session 的调用方仍使用独立事务。
    """
    from contextlib import asynccontextmanager

    from .db import tenant_session

    @asynccontextmanager
    async def adjustment_session():
        if session is None:
            async with tenant_session(ctx.tenant_id) as own_session:
                yield own_session
        else:
            async with session.begin_nested():
                yield session

    if evidence_policy != "mastery_eligible":
        return None
    try:
        async with adjustment_session() as adjust_session:
            plan = await active_plan(adjust_session, ctx.tenant_id, ctx.user_id)
            if (
                plan is None
                or plan.plan_mode != "course_bound"
                or plan.kb_id is None
                or quiz_kb_id != plan.kb_id
                or (attributed_plan_id is not None and attributed_plan_id != plan.id)
            ):
                return None
            await adjust_session.refresh(plan, with_for_update=True)
            if plan.status != "active":
                return None
            quiz_evidence = await _wrong_quiz_evidence(
                adjust_session, plan, [(submission_id, quiz_id, grading_results)]
            )
            wrong_attempts_by_kc = {}
            for _, attempt_id, kc_id in sorted(quiz_evidence):
                wrong_attempts_by_kc.setdefault(kc_id, str(attempt_id))
            result = await adjust_plan(
                adjust_session,
                ctx.user_id,
                ctx.tenant_id,
                plan,
                trigger=_LOW_SCORE_TRIGGER,
                submission_id=submission_id,
                wrong_attempts_by_kc=wrong_attempts_by_kc,
                first_day=max(1, (business_date - plan.created_at.astimezone(CN_TZ).date()).days + 1) if business_date else 1,
                reason_prefix=f"本次测评正确率 {round(accuracy * 100)}% 低于 60% 阈值; ",
            )
            result["plan_id"] = str(plan.id)
            return result
    except Exception:  # noqa: BLE001 — 计划调整失败不影响测评判分结果
        log.exception("plan_auto_adjust_failed")
        return None
