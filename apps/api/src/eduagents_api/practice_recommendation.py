"""One server-owned decision policy for the student's next practice action.

The web client may render this result, but it must not recreate the ordering or
trust any topic/KC context sent back by the browser.  This module keeps the
pure policy separate from the database loader so every branch can be tested
without a model, a worker, or a browser.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Literal

from eduagents_core.mastery.thresholds import difficulty_for_mastery
from sqlalchemy import select

from .models import (
    KnowledgeBase,
    KnowledgeComponent,
    MasteryRecord,
    Plan,
    Question,
    Quiz,
)
from .plan_adjust import (
    iter_tasks,
    make_task_key,
    plan_schedule_snapshot,
    task_status,
)
from .profile_summary import (
    ASSESSMENT_CONFIDENCE_CEILING,
    _load_weak_point_candidates,
)
from .services import quiz_kc_readiness

Adjustment = Literal["easier", "harder", "switch_kc", "switch_kb"]
RecommendationStatus = Literal["ready", "needs_scope", "preparing", "needs_upload"]
PracticeAction = Literal["review", "quiz", "choose_scope", "upload", "sample_diagnostic"]
PracticeSource = Literal[
    "due_review", "plan_task", "weak_kc", "cold_start", "mastery", "choose_scope", "sample"
]

ALLOWED_ADJUSTMENTS = frozenset({"easier", "harder", "switch_kc", "switch_kb"})
FIXED_COLD_START_MIX = {"easy": 1, "mid": 2, "hard": 0}


class PracticeAdjustmentUnavailable(ValueError):
    """The requested relative adjustment cannot produce a new safe practice."""

    code = "PRACTICE_DECISION_CHANGED"

    def __init__(self, message: str, recommendation: PracticeRecommendation | None = None):
        super().__init__(message)
        self.recommendation = recommendation


def validate_adjustment(value: str | None) -> str | None:
    if value is None:
        return None
    if value not in ALLOWED_ADJUSTMENTS:
        raise ValueError("adjustment must be easier, harder, switch_kc, or switch_kb")
    return value


def _bounded_count(value: object, default: int) -> int:
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        number = default
    return max(1, min(20, number))


def _adjust_mix(mix: dict[str, int], adjustment: str | None) -> dict[str, int]:
    result = {key: max(0, int(mix.get(key, 0))) for key in ("easy", "mid", "hard")}
    original = dict(result)
    if adjustment == "easier":
        moved = min(result["hard"], 1)
        result["hard"] -= moved
        result["mid"] += moved
        moved = min(result["mid"], 1)
        result["mid"] -= moved
        result["easy"] += moved
    elif adjustment == "harder":
        moved = min(result["easy"], 1)
        result["easy"] -= moved
        result["mid"] += moved
        moved = min(result["mid"], 1)
        result["mid"] -= moved
        result["hard"] += moved
    if adjustment in {"easier", "harder"} and result == original:
        raise PracticeAdjustmentUnavailable("无法继续调整当前难度")
    return result


def _weak_mix(points: list[dict]) -> dict[str, int]:
    counts = {"easy": 0, "mid": 0, "hard": 0}
    for point in (points or [])[:5]:
        difficulty = point.get("difficulty")
        if difficulty not in counts:
            difficulty = difficulty_for_mastery(float(point.get("p_mastery", 0.0)))
        counts[difficulty] += 1
    while sum(counts.values()) < 5:
        counts["mid"] += 1
    return counts


def _difficulty_mix_from_reasons(
    reasons: tuple[dict[str, str], ...],
    count: int,
    fallback: dict[str, int] | None = None,
) -> dict[str, int]:
    """Resize an internal qualitative target list without exposing source facts."""
    counts = {key: 0 for key in ("easy", "mid", "hard")}
    if reasons:
        for index in range(max(1, count)):
            difficulty = reasons[index % len(reasons)].get("difficulty")
            counts[difficulty if difficulty in counts else "mid"] += 1
        return counts
    if fallback:
        source = {key: max(0, int(fallback.get(key, 0))) for key in counts}
        if sum(source.values()) == count:
            return source
    counts["mid"] = max(1, count)
    return counts


@dataclass(frozen=True, slots=True)
class PracticeContext:
    due_count: int = 0
    plan_task: dict[str, Any] | None = None
    weak_points: list[dict[str, Any]] = field(default_factory=list)
    ready_kbs: list[dict[str, Any]] = field(default_factory=list)
    preparing_kbs: list[dict[str, Any]] = field(default_factory=list)
    empty_kbs: list[dict[str, Any]] = field(default_factory=list)
    scope_options: list[dict[str, str]] = field(default_factory=list)
    adjustment: str | None = None


@dataclass(frozen=True, slots=True)
class PracticeRecommendation:
    status: RecommendationStatus
    action: PracticeAction
    source: PracticeSource
    title: str
    reason: str
    question_count: int
    difficulty_mix: dict[str, int] = field(default_factory=dict)
    kb: dict[str, str] | None = None
    scope_options: tuple[dict[str, str], ...] = ()
    adjustments: tuple[str, ...] = ()
    target_kc_ids: tuple[str, ...] = ()
    target_kc_names: tuple[str, ...] = ()
    # Server-only qualitative detail for the legacy Quiz adapter.  It is
    # deliberately absent from ``to_dict`` and contains no mastery/confidence.
    difficulty_reasons: tuple[dict[str, str], ...] = ()
    plan_id: str | None = None
    task_key: str | None = None
    # Internal readiness fact used by compatibility Quiz adapters.  It is not
    # part of the public recommendation JSON.
    readiness_code: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Public, bounded JSON shape; no task text or mastery decimals."""
        return {
            "status": self.status,
            "action": self.action,
            "source": self.source,
            "title": self.title,
            "reason": self.reason,
            "question_count": self.question_count,
            "difficulty_mix": dict(self.difficulty_mix),
            "kb": self.kb,
            "scope_options": list(self.scope_options),
            "adjustments": list(self.adjustments),
            "plan_id": self.plan_id,
            "task_key": self.task_key,
        }


def _kb_public(kb: dict[str, Any]) -> dict[str, str]:
    return {"id": str(kb["id"]), "name": str(kb.get("name") or "")}


def _quiz_adjustments(kb_count: int) -> tuple[str, ...]:
    # A subject switch is only actionable when there is another ready KB.  The
    # client renders this server-owned list verbatim.
    values = ["easier", "harder", "switch_kc"]
    if kb_count > 1:
        values.append("switch_kb")
    return tuple(values)


def _without_adjustment(
    recommendation: PracticeRecommendation, adjustment: str
) -> PracticeRecommendation:
    """Return a safe latest recommendation after a one-shot action is unavailable."""
    return replace(
        recommendation,
        adjustments=tuple(
            value for value in recommendation.adjustments if value != adjustment
        ),
    )


def build_recommendation(context: PracticeContext) -> PracticeRecommendation:
    """Apply the product priority to already loaded, server-owned facts."""
    adjustment = validate_adjustment(context.adjustment)
    if context.due_count > 0:
        return PracticeRecommendation(
            status="ready",
            action="review",
            source="due_review",
            title=f"{context.due_count} 道错题到期了",
            reason="FSRS 间隔复习排程显示这些错题今天到期，先复习能巩固记忆。",
            question_count=0,
        )

    task = context.plan_task
    if task is not None:
        kb = task.get("kb")
        count = _bounded_count(task.get("count"), 5)
        mix = _adjust_mix(
            {"easy": count // 3, "mid": count - count // 3, "hard": 0}, adjustment
        )
        return PracticeRecommendation(
            status="ready",
            action="quiz",
            source="plan_task",
            title=f"继续今天的任务：{str(task.get('title') or '计划练习')[:80]}",
            reason=f"今天的学习计划还有这个任务未完成：{str(task.get('goal') or '按计划练习')[:80]}",
            question_count=count,
            difficulty_mix=mix,
            kb=_kb_public(kb) if isinstance(kb, dict) else None,
            scope_options=tuple(context.scope_options),
            adjustments=_quiz_adjustments(len(context.ready_kbs)),
            target_kc_ids=tuple(str(x) for x in task.get("kc_ids") or []),
            target_kc_names=tuple(str(x) for x in task.get("kc_names") or []),
            plan_id=str(task["plan_id"]) if task.get("plan_id") else None,
            task_key=str(task["task_key"]) if task.get("task_key") else None,
        )

    if context.weak_points:
        points = list(context.weak_points[:5])
        reasons = tuple(
            {
                "kc_name": str(point.get("name") or "")[:120],
                "zone": str(point.get("zone") or "待巩固"),
                "difficulty": str(point.get("difficulty") or "mid"),
                "reason": str(point.get("reason") or "按近期作答情况安排练习")[:240],
            }
            for point in points
            if point.get("name")
        )
        return PracticeRecommendation(
            status="ready",
            action="quiz",
            source="weak_kc",
            title="用 5 道题巩固薄弱知识点",
            reason="近期作答显示这些知识点还不稳定，先用一组短练习巩固。",
            question_count=5,
            difficulty_mix=_adjust_mix(_weak_mix(points), adjustment),
            kb=_kb_public(context.ready_kbs[0]) if len(context.ready_kbs) == 1 else None,
            scope_options=tuple(context.scope_options),
            adjustments=_quiz_adjustments(len(context.ready_kbs)),
            target_kc_ids=tuple(str(p["kc_id"]) for p in points if p.get("kc_id")),
            target_kc_names=tuple(str(p["name"]) for p in points if p.get("name")),
            difficulty_reasons=reasons,
        )

    if len(context.ready_kbs) == 1:
        kb = context.ready_kbs[0]
        names = tuple(str(x) for x in (kb.get("kc_names") or [])[:3])
        ids = tuple(str(x) for x in (kb.get("kc_ids") or [])[:3])
        assessment = [
            item
            for item in (kb.get("assessment_kcs") or [])
            if isinstance(item, dict) and item.get("id") and item.get("name")
        ][:3]
        if assessment:
            ids = tuple(str(item["id"]) for item in assessment)
            names = tuple(str(item["name"]) for item in assessment)
        if kb.get("has_mastery") and not assessment:
            all_names = tuple(str(x) for x in (kb.get("kc_names") or [])[:5])
            all_ids = tuple(str(x) for x in (kb.get("kc_ids") or [])[:5])
            return PracticeRecommendation(
                status="ready",
                action="quiz",
                source="mastery",
                title="继续巩固这份资料",
                reason="已有作答记录，但目前没有需要优先补强的薄弱知识点；先做一组短练习保持熟练度。",
                question_count=5,
                difficulty_mix=_adjust_mix({"easy": 1, "mid": 3, "hard": 1}, adjustment),
                kb=_kb_public(kb),
                scope_options=tuple(context.scope_options),
                adjustments=_quiz_adjustments(len(context.ready_kbs)),
                target_kc_ids=all_ids,
                target_kc_names=all_names,
            )
        return PracticeRecommendation(
            status="ready",
            action="quiz",
            source="cold_start",
            title="用 3 道题建立第一版画像",
            reason="这份资料已经准备好，先从真实章节知识点各取一道题了解你的起点。",
            question_count=3,
            difficulty_mix=_adjust_mix(FIXED_COLD_START_MIX, adjustment),
            kb=_kb_public(kb),
            scope_options=tuple(context.scope_options),
            adjustments=_quiz_adjustments(len(context.ready_kbs)),
            target_kc_ids=ids,
            target_kc_names=names,
            difficulty_reasons=tuple(
                {
                    "kc_name": name,
                    "zone": "未验证",
                    "difficulty": "mid",
                    "reason": f"「{name}」尚未测过，先做一次快速验证",
                }
                for name in names
            ),
        )

    if len(context.ready_kbs) > 1:
        options = tuple(
            {"id": str(kb["id"]), "name": str(kb.get("name") or "")}
            for kb in context.ready_kbs
        )
        return PracticeRecommendation(
            status="needs_scope",
            action="choose_scope",
            source="choose_scope",
            title="先选这次要练的科目",
            reason="你有多份已准备好的资料，但当前没有足够上下文判断练哪一科。",
            question_count=0,
            scope_options=options,
        )

    if context.preparing_kbs:
        kb = context.preparing_kbs[0]
        return PracticeRecommendation(
            status="preparing",
            action="quiz",
            source="cold_start",
            title="正在理解你的资料",
            reason="资料已收到，系统正在整理知识点；完成后会自动准备第一组练习。",
            question_count=0,
            kb=_kb_public(kb),
        )

    if context.empty_kbs:
        kb = context.empty_kbs[0]
        return PracticeRecommendation(
            status="needs_upload",
            action="upload",
            source="sample",
            title="先上传这份资料",
            reason="这份知识库还没有可用资料；上传后才能建立章节知识点和练习。",
            question_count=0,
            kb=_kb_public(kb),
            readiness_code=kb.get("_readiness_code"),
        )

    return PracticeRecommendation(
        status="needs_upload",
        action="upload",
        source="sample",
        title="先上传一份正在学的资料",
        reason="目前还没有可用的个人资料；上传后才能按你的内容建立练习。",
        question_count=0,
        adjustments=(),
    )


async def _load_plan_task(
    session,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    now_dt: datetime,
    plan_id=None,
    task_key=None,
):
    query = select(Plan).where(Plan.tenant_id == tenant_id, Plan.user_id == user_id)
    if plan_id is not None:
        query = query.where(Plan.id == plan_id)
    else:
        query = query.order_by(Plan.created_at.desc()).limit(1)
    plan = await session.scalar(query)
    if plan is None:
        return None
    structure = plan.structure_json or {}
    snapshot = plan_schedule_snapshot(plan.created_at.date(), structure, now_dt.date())
    due_keys = set(snapshot["today_tasks"]) | set(snapshot["overdue_tasks"])
    candidates = []
    for si, di, ti, _stage, _day, task in iter_tasks(structure):
        key = make_task_key(si, di, ti)
        if task_key and key != task_key:
            continue
        if task_status(task) != "pending":
            continue
        if not task_key and key not in due_keys:
            continue
        if str(task.get("action_type") or "practice") not in {"practice", "mixed"}:
            continue
        candidates.append((key, task))
    if not candidates:
        return None
    key, task = candidates[0]
    return plan, key, task, snapshot["current_day"]


async def _ready_kb_snapshots(session, *, gateway_mode: str, org_ids=()):
    rows = (await session.scalars(select(KnowledgeBase).order_by(KnowledgeBase.created_at))).all()
    ready: list[dict[str, Any]] = []
    preparing: list[dict[str, Any]] = []
    empty: list[dict[str, Any]] = []
    for kb in rows:
        readiness = await quiz_kc_readiness(
            session,
            tenant_id=session.info["tenant_id"],
            kb_id=kb.id,
            gateway_mode=gateway_mode,
            org_ids=org_ids,
        )
        public = {"id": str(kb.id), "name": kb.name, "subject": kb.subject}
        if readiness.status == "ready":
            kc_rows = (
                await session.scalars(
                    select(KnowledgeComponent).where(
                        KnowledgeComponent.id.in_([uuid.UUID(x) for x in readiness.eligible_kc_ids])
                    )
                )
            ).all()
            kc_rows.sort(key=lambda row: (row.created_at, str(row.id)))
            public["kc_ids"] = [str(row.id) for row in kc_rows]
            public["kc_names"] = [row.canonical_name for row in kc_rows]
            mastery_rows = []
            if session.info.get("user_id"):
                mastery_rows = (
                    await session.execute(
                        select(MasteryRecord.kc_id, MasteryRecord.confidence).where(
                            MasteryRecord.user_id == session.info["user_id"],
                            MasteryRecord.kc_id.in_([row.id for row in kc_rows]),
                        )
                    )
                ).all()
            confidence_by_kc = {
                str(kc_id): float(confidence or 0.0)
                for kc_id, confidence in mastery_rows
            }
            public["has_mastery"] = bool(mastery_rows)
            # Internal selector facts only.  PracticeRecommendation.to_dict()
            # never serializes this list or the confidence values behind it.
            public["assessment_kcs"] = [
                {"id": str(row.id), "name": row.canonical_name}
                for row in kc_rows
                if confidence_by_kc.get(str(row.id), 0.0) < ASSESSMENT_CONFIDENCE_CEILING
            ]
            ready.append(public)
        elif readiness.status == "preparing":
            preparing.append(public)
        elif readiness.status == "not_applicable":
            empty.append(public)
        elif readiness.status == "not_ready":
            public["_readiness_code"] = readiness.code or "QUIZ_KC_NOT_READY"
            empty.append(public)
    return ready, preparing, empty


async def recommend_practice(
    session,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    gateway_mode: str,
    org_ids=(),
    now_dt: datetime,
    scope_kb_id: uuid.UUID | None = None,
    plan_id: uuid.UUID | None = None,
    task_key: str | None = None,
    previous_quiz_id: uuid.UUID | None = None,
    adjustment: str | None = None,
) -> PracticeRecommendation:
    """Load tenant-scoped facts and apply :func:`build_recommendation`."""
    validate_adjustment(adjustment)
    if (plan_id is None) != (task_key is None):
        raise ValueError("plan_id and task_key must be provided together")
    if adjustment and previous_quiz_id is None:
        raise ValueError("previous_quiz_id is required for adjustment")
    previous_quiz = None
    if previous_quiz_id is not None:
        previous_quiz = await session.scalar(
            select(Quiz).where(
                Quiz.id == previous_quiz_id,
                Quiz.tenant_id == tenant_id,
                Quiz.user_id == user_id,
            )
        )
        if previous_quiz is None:
            raise LookupError("PREVIOUS_QUIZ_NOT_FOUND")

    due_rows = await _due_rows(session, user_id, now_dt)
    # Store auth facts on the session only for this loader; no user text enters
    # the recommendation response.
    session.info["user_id"] = user_id
    session.info["tenant_id"] = tenant_id
    ready, preparing, empty = await _ready_kb_snapshots(
        session, gateway_mode=gateway_mode, org_ids=org_ids
    )
    if scope_kb_id is not None:
        selected = next((kb for kb in ready if kb["id"] == str(scope_kb_id)), None)
        if selected is None:
            if any(kb["id"] == str(scope_kb_id) for kb in preparing):
                return build_recommendation(
                    PracticeContext(preparing_kbs=[next(kb for kb in preparing if kb["id"] == str(scope_kb_id))])
                )
            if any(kb["id"] == str(scope_kb_id) for kb in empty):
                return build_recommendation(
                    PracticeContext(empty_kbs=[next(kb for kb in empty if kb["id"] == str(scope_kb_id))])
                )
            raise LookupError("KB_NOT_FOUND")
        ready_for_decision = [selected]
    else:
        ready_for_decision = ready

    plan_task = await _load_plan_task(
        session,
        tenant_id=tenant_id,
        user_id=user_id,
        now_dt=now_dt,
        plan_id=plan_id,
        task_key=task_key,
    )
    task_context = None
    if plan_task:
        plan, key, task, day_no = plan_task
        kb = next((item for item in ready if item["id"] == str(plan.kb_id)), None)
        task_context = {
            "plan_id": str(plan.id),
            "task_key": key,
            "kb": kb,
            "goal": plan.goal,
            "title": task.get("title") or task.get("objective") or "计划练习",
            "count": task.get("practice_count") or 5,
            "kc_names": list(task.get("kc_names") or []),
            "day_no": day_no,
        }
        if kb:
            by_name = {str(name).strip(): kid for kid, name in zip(kb.get("kc_ids", []), kb.get("kc_names", []), strict=False)}
            task_context["kc_ids"] = [by_name[name] for name in task_context["kc_names"] if name in by_name]
        else:
            # A plan cannot silently cross a readiness barrier.  Let the
            # normal weak/cold/upload branch explain the current state.
            task_context = None

    weak_points = await _load_weak_points(
        session, tenant_id, user_id, ready_for_decision
    )
    scope_options = tuple({"id": kb["id"], "name": kb["name"]} for kb in ready)
    base_context = PracticeContext(
        due_count=len(due_rows),
        plan_task=task_context,
        weak_points=weak_points,
        ready_kbs=ready_for_decision,
        preparing_kbs=preparing,
        empty_kbs=empty,
        scope_options=scope_options,
    )

    # An explicit post-quiz adjustment is a one-shot override of the normal
    # plan-task priority.  Easier/harder can keep the plan attribution, while
    # changing KC or KB must leave the original task scope behind.
    decision_task_context = task_context
    decision_ready_for_decision = ready_for_decision
    decision_weak_points = weak_points
    if adjustment in {"switch_kc", "switch_kb"}:
        decision_task_context = None

    if adjustment == "switch_kc" and previous_quiz is not None:
        previous_questions = (
            await session.scalars(
                select(Question).where(
                    Question.quiz_id == previous_quiz.id,
                    Question.tenant_id == tenant_id,
                )
            )
        ).all()
        previous_kc_ids = {
            str(kc_id)
            for question in previous_questions
            for kc_id in (question.kc_ids or [])
        }
        # switch_kc stays inside the previous Quiz's KB.  This both prevents
        # a cross-subject surprise and makes the exclusion auditable from the
        # persisted Question.kc_ids rather than browser-supplied names.
        previous_kb = next(
            (
                kb
                for kb in ready
                if previous_quiz.kb_id is not None
                and kb["id"] == str(previous_quiz.kb_id)
            ),
            None,
        )
        filtered_kbs = []
        if previous_kb is not None:
            filtered_kbs = [
                {
                    **previous_kb,
                    "kc_ids": [
                        kc_id
                        for kc_id in previous_kb.get("kc_ids", [])
                        if str(kc_id) not in previous_kc_ids
                    ],
                    "kc_names": [
                        name
                        for kc_id, name in zip(
                            previous_kb.get("kc_ids", []),
                            previous_kb.get("kc_names", []),
                            strict=False,
                        )
                        if str(kc_id) not in previous_kc_ids
                    ],
                    "assessment_kcs": [
                        item
                        for item in previous_kb.get("assessment_kcs", [])
                        if isinstance(item, dict)
                        and str(item.get("id")) not in previous_kc_ids
                    ],
                }
            ]
        decision_weak_points = [
            point
            for point in weak_points
            if str(point.get("kc_id")) not in previous_kc_ids
        ]
        decision_ready_for_decision = [
            {
                **kb,
                "kc_ids": list(kb.get("kc_ids", [])),
                "kc_names": list(kb.get("kc_names", [])),
            }
            for kb in filtered_kbs
        ]
        if not any(kb.get("kc_ids") for kb in decision_ready_for_decision):
            raise PracticeAdjustmentUnavailable(
                "没有可切换到的知识点",
                recommendation=_without_adjustment(
                    build_recommendation(base_context), "switch_kc"
                ),
            )
        decision_weak_points = await _load_weak_points(
            session, tenant_id, user_id, decision_ready_for_decision
        )

    if adjustment == "switch_kb" and previous_quiz is not None:
        alternatives = [
            kb
            for kb in ready
            if previous_quiz.kb_id is not None
            and kb["id"] != str(previous_quiz.kb_id)
            and kb.get("kc_ids")
        ]
        decision_ready_for_decision = alternatives[:1]
        if not decision_ready_for_decision:
            raise PracticeAdjustmentUnavailable(
                "没有可切换到的科目",
                recommendation=_without_adjustment(
                    build_recommendation(base_context), "switch_kb"
                ),
            )
        decision_weak_points = await _load_weak_points(
            session, tenant_id, user_id, decision_ready_for_decision
        )

    try:
        return build_recommendation(
            PracticeContext(
                due_count=len(due_rows),
                plan_task=decision_task_context,
                weak_points=decision_weak_points,
                ready_kbs=decision_ready_for_decision,
                preparing_kbs=preparing,
                empty_kbs=empty,
                scope_options=scope_options,
                adjustment=adjustment,
            )
        )
    except PracticeAdjustmentUnavailable as exc:
        # The client gets the newest server recommendation alongside the
        # stable 409, so it cannot retry the same unchanged distribution.
        raise PracticeAdjustmentUnavailable(
            str(exc), recommendation=build_recommendation(base_context)
        ) from None


async def _due_rows(session, user_id, now_dt):
    from .routers.reviews import due_error_items

    return await due_error_items(session, user_id, now_dt)


async def _load_weak_points(session, tenant_id, user_id, ready_kbs):
    if not ready_kbs:
        return []
    return await _load_weak_point_candidates(
        session,
        tenant_id=tenant_id,
        user_id=user_id,
        kb_ids=[kb["id"] for kb in ready_kbs],
    )
