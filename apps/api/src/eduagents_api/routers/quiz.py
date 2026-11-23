from __future__ import annotations

import uuid
from typing import Literal

from eduagents_core.logging import get_logger
from eduagents_core.mastery.thresholds import DIFFICULTIES, difficulty_for_mastery
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import or_, select

from .. import timectl
from ..assessment_submissions import GradingRetryIn, load_submission, submission_key
from ..db import tenant_session
from ..deps import AuthContext, api_rate_limited
from ..models import (
    ERROR_IMPORT_QUIZ_KIND,
    AssessmentSubmission,
    Attempt,
    ErrorItem,
    Question,
    Quiz,
)
from ..plan_adjust import PlanTaskAttributionError, validate_plan_task_attribution
from ..practice_recommendation import (
    PracticeAdjustmentUnavailable,
    _difficulty_mix_from_reasons,
)
from ..profile_summary import build_profile_context
from ..services import QUIZ_PLAN_MODE_STANDALONE, quiz_kc_readiness, record_attempts
from .practice import PracticeRequest, load_recommendation
from .threads import stream_graph_run

router = APIRouter(prefix="/api/quiz", tags=["quiz"])
log = get_logger("quiz")

_WEAK_TOPIC = "薄弱点强化练习"


def adaptive_difficulty_mix(weak: list[dict], num_questions: int) -> dict:
    """按各薄弱 KC 的掌握度计算整卷难度分布。

    档位与阈值统一来自 thresholds.difficulty_for_mastery。题目按薄弱点
    轮转分配（蓝图要求覆盖均衡），第 i 题难度 = 对应 KC 的匹配档，故各档之和恒等于
    num_questions。由后端计算, 不信任前端传的 difficulty_mix (可篡改)。

    Assessment Mode 的 KC 可能没有 mastery_record (p_mastery=None): 这类 KC 的目的
    是"量出水平", 没有可信 p 值可依, 一律走中档 —— 照未验证的先验值调难度会把
    诊断题也偏到极端档, 反而测不准。
    """
    counts = dict.fromkeys(DIFFICULTIES, 0)
    for i in range(num_questions):
        p = weak[i % len(weak)].get("p_mastery")
        counts["mid" if p is None else difficulty_for_mastery(float(p))] += 1
    return counts


class QuizCreate(BaseModel):
    # from_weak_points=True 时 topic 可省略 (后端填 _WEAK_TOPIC); 否则必填
    topic: str = Field(default="", max_length=255)
    kb_id: uuid.UUID | None = None
    num_questions: int = Field(default=5, ge=1, le=20)
    difficulty_mix: dict = Field(default_factory=lambda: {"easy": 2, "mid": 2, "hard": 1})
    kc_names: list[str] = Field(default_factory=list)
    # 画像联动出题 (M4-D2): 由后端按掌握度自动填 kc_names, 不信任前端传的薄弱点
    from_weak_points: bool = False
    plan_id: uuid.UUID | None = None
    task_id: str | None = Field(default=None, max_length=96)
    confirmation_mode: Literal["auto", "required"] = "required"

    @model_validator(mode="after")
    def _topic_required(self) -> QuizCreate:
        if not self.from_weak_points and not self.topic.strip():
            raise ValueError("topic 不能为空")
        if (self.plan_id is None) != (self.task_id is None):
            raise ValueError("plan_id 与 task_id 必须同时提供")
        if self.task_id is not None:
            self.task_id = self.task_id.strip()
            if not self.task_id:
                raise ValueError("task_id 不能为空")
        return self


class AttemptAnswerIn(BaseModel):
    question_id: uuid.UUID
    response: str = Field(default="", max_length=8000)
    predicted_confidence: int | None = Field(default=None, ge=1, le=5, strict=True)
    predicted_correct: bool | None = Field(default=None, strict=True)


class AttemptIn(BaseModel):
    answers: list[AttemptAnswerIn] = Field(min_length=1, max_length=20)
    idempotency_key: str | None = Field(default=None, max_length=128)


@router.post("/recommended")
async def create_recommended_quiz(
    body: PracticeRequest,
    request: Request,
    ctx: AuthContext = Depends(api_rate_limited),
):
    """Start the server-selected student practice flow.

    The recommendation is re-evaluated inside the same request.  A stale
    browser decision never silently becomes a different quiz.
    """
    try:
        recommendation = await load_recommendation(request, ctx, body)
    except PracticeAdjustmentUnavailable as exc:
        return JSONResponse(
            status_code=409,
            content={
                "code": exc.code,
                "message": str(exc),
                "recommendation": exc.recommendation.to_dict()
                if exc.recommendation is not None
                else None,
            },
        )
    if recommendation.status != "ready" or recommendation.action != "quiz":
        barrier_code = (
            "QUIZ_KC_PREPARING"
            if recommendation.status == "preparing"
            else recommendation.readiness_code
        )
        return JSONResponse(
            status_code=409,
            content={
                "code": barrier_code or "PRACTICE_DECISION_CHANGED",
                "message": "练习建议已变化，请使用最新建议。",
                "recommendation": recommendation.to_dict(),
            },
        )
    kb_id = uuid.UUID(recommendation.kb["id"]) if recommendation.kb else None
    quiz_body = QuizCreate(
        topic=recommendation.title,
        kb_id=kb_id,
        num_questions=recommendation.question_count,
        difficulty_mix=recommendation.difficulty_mix,
        kc_names=list(recommendation.target_kc_names),
        plan_id=uuid.UUID(recommendation.plan_id) if recommendation.plan_id else None,
        task_id=recommendation.task_id,
        confirmation_mode="auto",
    )
    return await create_quiz(quiz_body, request, ctx)


@router.post("")
async def create_quiz(
    body: QuizCreate, request: Request, ctx: AuthContext = Depends(api_rate_limited)
):
    """出题: Blueprint 预览 → interrupt 等确认 → resume 后生成+校验+落库 (F5)。

    from_weak_points=True 走画像定向通道 (M4-D2): 后端读掌握度取薄弱 KC 填 kc_names,
    topic 缺省为"薄弱点强化练习"; 难度分布同样由后端按掌握度算 (P1-1, 覆盖前端传值),
    逐 KC 分档理由随蓝图 interrupt 下发。选中的薄弱点作为 trace 事件先行下发, 可核对。
    """
    runtime = getattr(request.app.state, "harness", None)
    if runtime is None:
        raise HTTPException(status_code=503, detail="GRAPHS_UNAVAILABLE")
    effective_kb_id = body.kb_id
    effective_plan_id = body.plan_id
    effective_task_id = body.task_id
    evidence_policy = "mastery_eligible"
    # Plan mode describes an actual attributed plan.  An ordinary quiz with
    # no plan attribution is standalone even when no KB was selected; missing
    # kb_id alone must never turn it into goal-only calibration.
    plan_mode = "course_bound" if effective_kb_id is not None else QUIZ_PLAN_MODE_STANDALONE
    # Wave 1B barrier: a KB-backed cold-start Quiz must not enter the graph
    # while its usable KC set is still being built.  Personalized and ordinary
    # Quiz paths share this barrier; weak-point requests must not spend
    # generation budget before their KB can support learning evidence.
    if effective_kb_id is not None:
        gateway_mode = getattr(getattr(request.app.state, "settings", None), "gateway_mode", None)
        if not isinstance(gateway_mode, str) or not gateway_mode:
            from eduagents_core.config import get_settings

            gateway_mode = get_settings().gateway_mode
        async with tenant_session(ctx.tenant_id, org_ids=ctx.org_ids) as session:
            readiness = await quiz_kc_readiness(
                session,
                tenant_id=ctx.tenant_id,
                kb_id=effective_kb_id,
                gateway_mode=gateway_mode,
                org_ids=ctx.org_ids,
            )
        if readiness.code == "KB_NOT_FOUND":
            raise HTTPException(status_code=404, detail="KB_NOT_FOUND")
        if readiness.status in {"preparing", "not_ready"}:
            raise HTTPException(
                status_code=409,
                detail=readiness.code or "QUIZ_KC_NOT_READY",
            )
    topic = body.topic.strip()
    kc_names = list(body.kc_names)
    difficulty_mix = dict(body.difficulty_mix)
    difficulty_reasons: list[dict] = []
    trace_prelude: list[dict] = []
    quiz_mode = "practice"
    effective_count = body.num_questions
    if body.from_weak_points:
        # Compatibility entry point: use the same server-owned selector as
        # GET /api/practice/next and POST /api/quiz/recommended. This keeps
        # due-review/plan/cold-start priority in one place.
        recommendation = await load_recommendation(
            request,
            ctx,
            PracticeRequest(scope_kb_id=body.kb_id),
        )
        if recommendation.status != "ready" or recommendation.action != "quiz":
            if recommendation.status == "needs_upload":
                code = recommendation.readiness_code or "NO_KC_AVAILABLE"
            elif recommendation.status == "preparing":
                code = "QUIZ_KC_PREPARING"
            else:
                code = "PRACTICE_DECISION_CHANGED"
            return JSONResponse(
                status_code=409,
                content={
                    "code": code,
                    "message": recommendation.reason,
                    "recommendation": recommendation.to_dict(),
                },
            )
        if recommendation.source == "plan_task":
            recommendation_plan_id = recommendation.plan_id
            recommendation_task_id = recommendation.task_id
            if not recommendation_plan_id or not recommendation_task_id:
                return JSONResponse(
                    status_code=409,
                    content={
                        "code": "PRACTICE_DECISION_CHANGED",
                        "message": "练习建议已变化，请使用最新建议。",
                        "recommendation": recommendation.to_dict(),
                    },
                )
            # A legacy browser may send the task context it saw before this
            # server-side decision was refreshed.  Omitted context is allowed,
            # but a stale pair must not silently become another plan task.
            if (
                effective_plan_id is not None
                and (
                    str(effective_plan_id) != recommendation_plan_id
                    or effective_task_id != recommendation_task_id
                )
            ):
                return JSONResponse(
                    status_code=409,
                    content={
                        "code": "PRACTICE_DECISION_CHANGED",
                        "message": "练习建议已变化，请使用最新建议。",
                        "recommendation": recommendation.to_dict(),
                    },
                )
            try:
                effective_plan_id = uuid.UUID(recommendation_plan_id)
            except (TypeError, ValueError):
                return JSONResponse(
                    status_code=409,
                    content={
                        "code": "PRACTICE_DECISION_CHANGED",
                        "message": "练习建议已变化，请使用最新建议。",
                        "recommendation": recommendation.to_dict(),
                    },
                )
            effective_task_id = recommendation_task_id
        elif effective_plan_id is not None:
            # Ordinary weak/cold-start/maintenance recommendations own no
            # plan attribution.  Browser-supplied context is stale or forged.
            return JSONResponse(
                status_code=409,
                content={
                    "code": "PRACTICE_DECISION_CHANGED",
                    "message": "练习建议已变化，请使用最新建议。",
                    "recommendation": recommendation.to_dict(),
                },
            )
        goal_only_plan_task = (
            recommendation.source == "plan_task"
            and recommendation.plan_id is not None
            and recommendation.task_id is not None
            and recommendation.kb is None
        )
        if (not recommendation.kb and not goal_only_plan_task) or not recommendation.target_kc_names:
            return JSONResponse(
                status_code=409,
                content={
                    "code": "NO_KC_AVAILABLE",
                    "message": "当前没有可用于练习的知识点。",
                    "recommendation": recommendation.to_dict(),
                },
            )
        effective_kb_id = uuid.UUID(recommendation.kb["id"]) if recommendation.kb else None
        kc_names = list(recommendation.target_kc_names)
        topic = topic or _WEAK_TOPIC
        quiz_mode = {
            "cold_start": "assessment",
            "weak_kc": "weak_kc",
            "mastery": "mastery",
        }.get(recommendation.source, "practice")
        # Preserve the historical explicit question-count input for weak-point
        # callers. Cold-start assessment remains a fixed three-question mode.
        effective_count = (
            recommendation.question_count
            if recommendation.source == "cold_start"
            else body.num_questions
        )
        difficulty_mix = _difficulty_mix_from_reasons(
            recommendation.difficulty_reasons,
            effective_count,
            recommendation.difficulty_mix,
        )
        difficulty_reasons = list(recommendation.difficulty_reasons)
        trace_prelude.append(
            {
                "node": "weak_points",
                "event": "weak_points_selected",
                "mode": quiz_mode,
                "count": len(kc_names),
                "kc_names": kc_names,
                "difficulty_mix": difficulty_mix,
                "rationale": (
                    "资料已经准备好，先做一次快速验证摸清起点"
                    if quiz_mode == "assessment"
                    else "根据近期作答挑出需要巩固的知识点，并按难度安排短练习"
                    if quiz_mode == "weak_kc"
                    else "已有作答记录，先做一组短练习保持熟练度"
                ),
            }
        )
    if (effective_plan_id is None) != (effective_task_id is None):
        raise HTTPException(status_code=409, detail="PRACTICE_DECISION_CHANGED")
    if effective_plan_id is not None and effective_task_id is not None:
        async with tenant_session(ctx.tenant_id) as session:
            try:
                attribution = await validate_plan_task_attribution(
                    session,
                    tenant_id=ctx.tenant_id,
                    user_id=ctx.user_id,
                    plan_id=effective_plan_id,
                    task_id=effective_task_id,
                    source_type="quiz_attempt",
                )
            except PlanTaskAttributionError as exc:
                status = 404 if exc.code == "PLAN_TASK_NOT_FOUND" else 409
                raise HTTPException(status_code=status, detail=exc.code) from None
        evidence_policy = str(attribution["evidence_policy"])
        plan_mode = str(attribution["plan_mode"])
        authoritative_kb_id = attribution.get("kb_id")
        if evidence_policy == "calibration_only":
            effective_kb_id = None
            difficulty_reasons = []
        elif authoritative_kb_id is None or str(effective_kb_id) != authoritative_kb_id:
            raise HTTPException(status_code=409, detail="PLAN_KB_MISMATCH")
    # 画像注入 (P1-2): 摘要已按 P2-7 脱敏 (定性词, 无题干原文), 供 generate 调整
    # 干扰项设计; 读画像失败一律降级为"无画像"继续出题, 不拖垮主链路
    profile_summary = ""
    if evidence_policy == "mastery_eligible":
        try:
            profile = await build_profile_context(
                ctx.tenant_id,
                ctx.user_id,
                [str(effective_kb_id)] if effective_kb_id else [],
                redis=getattr(request.app.state, "redis", None),
                role="quiz",
                org_ids=ctx.org_ids,
            )
            profile_summary = profile.summary
        except Exception:  # noqa: BLE001
            log.exception("quiz_profile_summary_failed")
    local_id = str(uuid.uuid4())
    thread_id = runtime.thread_id("quiz", str(ctx.user_id), local_id)
    payload = {
        "tenant_id": str(ctx.tenant_id),
        "user_id": str(ctx.user_id),
        "request_id": getattr(request.state, "request_id", ""),
        "thread_id": thread_id,
        "confirmation_mode": body.confirmation_mode,
        "evidence_policy": evidence_policy,
        "plan_mode": plan_mode,
        "kb_id": str(effective_kb_id) if effective_kb_id else "",
        "topic": topic,
        "num_questions": effective_count if body.from_weak_points else body.num_questions,
        "difficulty_mix": difficulty_mix,
        "kc_names": kc_names,
        "profile_summary": profile_summary,
        "difficulty_reasons": difficulty_reasons,
    }
    if effective_plan_id is not None and effective_task_id is not None:
        payload["plan_id"] = str(effective_plan_id)
        payload["task_id"] = effective_task_id
    return await stream_graph_run(
        runtime, "quiz", thread_id, ctx, payload, trace_prelude=trace_prelude
    )


@router.get("")
async def list_quizzes(ctx: AuthContext = Depends(api_rate_limited)):
    async with tenant_session(ctx.tenant_id) as session:
        rows = (
            await session.scalars(
                select(Quiz)
                .where(
                    Quiz.user_id == ctx.user_id,
                    or_(
                        Quiz.blueprint_json["kind"].astext.is_(None),
                        Quiz.blueprint_json["kind"].astext != ERROR_IMPORT_QUIZ_KIND,
                    ),
                )
                .order_by(Quiz.created_at.desc())
            )
        ).all()
        if not rows:
            return []
        quiz_ids = [row.id for row in rows]
        questions = (
            await session.scalars(select(Question).where(Question.quiz_id.in_(quiz_ids)))
        ).all()
        question_by_quiz: dict[uuid.UUID, list[Question]] = {}
        for question in questions:
            if question.quiz_id is None:
                continue
            question_by_quiz.setdefault(question.quiz_id, []).append(question)
        attempt_rows = (
            await session.execute(
                select(Question.quiz_id, Attempt.is_correct, Attempt.at)
                .join(Attempt, Attempt.question_id == Question.id)
                .where(Question.quiz_id.in_(quiz_ids), Attempt.user_id == ctx.user_id)
            )
        ).all()
        attempts_by_quiz: dict[uuid.UUID, list[tuple[bool | None, object]]] = {}
        for quiz_id, is_correct, at in attempt_rows:
            if quiz_id is not None:
                attempts_by_quiz.setdefault(quiz_id, []).append((is_correct, at))
        error_rows = (
            await session.execute(
                select(Question.quiz_id, ErrorItem.question_id)
                .join(ErrorItem, ErrorItem.question_id == Question.id)
                .where(
                    Question.quiz_id.in_(quiz_ids),
                    ErrorItem.user_id == ctx.user_id,
                    ErrorItem.verification_status == "verified",
                )
            )
        ).all()
        errors_by_quiz: dict[uuid.UUID, set[uuid.UUID]] = {}
        for quiz_id, question_id in error_rows:
            if quiz_id is not None:
                errors_by_quiz.setdefault(quiz_id, set()).add(question_id)
        result = []
        for row in rows:
            quiz_questions = question_by_quiz.get(row.id, [])
            attempts = attempts_by_quiz.get(row.id, [])
            valid_attempts = [item for item in attempts if item[0] is not None]
            score = (
                round(sum(1 for item in valid_attempts if item[0]) / len(valid_attempts), 4)
                if valid_attempts
                else None
            )
            kc_ids = {
                str(kc_id)
                for question in quiz_questions
                for kc_id in (question.kc_ids or [])
                if kc_id
            }
            blueprint = row.blueprint_json or {}
            evidence_policy = blueprint.get("_evidence_policy", "mastery_eligible")
            raw_plan_mode = blueprint.get("_plan_mode")
            attribution = blueprint.get("_plan_task_attribution")
            has_plan_attribution = bool(
                isinstance(attribution, dict)
                and attribution.get("plan_id")
                and attribution.get("task_id")
            )
            plan_mode = (
                raw_plan_mode
                if isinstance(raw_plan_mode, str)
                and raw_plan_mode in {"course_bound", "goal_only", QUIZ_PLAN_MODE_STANDALONE}
                else (
                    "goal_only"
                    if has_plan_attribution and evidence_policy == "calibration_only"
                    else ("course_bound" if row.kb_id else QUIZ_PLAN_MODE_STANDALONE)
                )
            )
            result.append(
                {
                    "id": str(row.id),
                    "title": row.title,
                    "status": row.status,
                    "created_at": row.created_at,
                    "kb_id": str(row.kb_id) if row.kb_id else None,
                    "question_count": len(quiz_questions),
                    "attempt_count": len(valid_attempts),
                    "score": score,
                    "error_count": len(errors_by_quiz.get(row.id, set())),
                    "error_question_ids": [
                        str(question_id) for question_id in errors_by_quiz.get(row.id, set())
                    ],
                    "kc_count": len(kc_ids),
                    "evidence_policy": evidence_policy,
                    "plan_mode": plan_mode,
                }
            )
        return result


@router.get("/{quiz_id}")
async def get_quiz(quiz_id: uuid.UUID, ctx: AuthContext = Depends(api_rate_limited)):
    async with tenant_session(ctx.tenant_id) as session:
        quiz = await session.get(Quiz, quiz_id)
        if (
            quiz is None
            or quiz.user_id != ctx.user_id
            or (quiz.blueprint_json or {}).get("kind") == ERROR_IMPORT_QUIZ_KIND
        ):
            raise HTTPException(status_code=404, detail="NOT_FOUND")
        questions = (
            await session.scalars(
                select(Question)
                .where(Question.quiz_id == quiz_id)
                .order_by(Question.source_meta_json["quiz_ordinal"].as_integer(), Question.id)
            )
        ).all()
        blueprint = dict(quiz.blueprint_json or {})
        blueprint.pop("_plan_task_attribution", None)
        return {
            "id": str(quiz.id),
            "title": quiz.title,
            "blueprint": blueprint,
            "questions": [
                {
                    "id": str(q.id),
                    "type": q.type,
                    "stem": q.stem,
                    "options": q.options_json,
                    "difficulty": q.difficulty,
                    "kc_ids": q.kc_ids,
                }
                for q in questions
            ],
        }


@router.post("/{quiz_id}/attempts")
async def submit_attempts(
    quiz_id: uuid.UUID,
    body: AttemptIn,
    request: Request,
    ctx: AuthContext = Depends(api_rate_limited),
):
    async with tenant_session(ctx.tenant_id) as session:
        quiz = await session.get(Quiz, quiz_id)
        if (
            quiz is None
            or quiz.user_id != ctx.user_id
            or (quiz.blueprint_json or {}).get("kind") == ERROR_IMPORT_QUIZ_KIND
        ):
            raise HTTPException(status_code=404, detail="NOT_FOUND")
    # 业务时间走虚拟时钟 (§9): 错题入本的 FSRS 卡 due 随 time-travel 前进
    now_dt = await timectl.now(request.app.state.redis)
    return await record_attempts(
        ctx,
        request.app.state.gateway,
        quiz_id,
        [answer.model_dump(mode="json", exclude_none=True) for answer in body.answers],
        now_dt=now_dt,
        idempotency_key=submission_key(body.idempotency_key, request.headers.get("Idempotency-Key")),
    )


@router.get("/{quiz_id}/pending-submission")
async def pending_submission(quiz_id: uuid.UUID, ctx: AuthContext = Depends(api_rate_limited)):
    async with tenant_session(ctx.tenant_id) as session:
        row = await session.scalar(select(AssessmentSubmission).where(
            AssessmentSubmission.quiz_id == quiz_id,
            AssessmentSubmission.user_id == ctx.user_id,
            AssessmentSubmission.result_json["grading_status"].astext == "pending",
        ).order_by(AssessmentSubmission.created_at.desc(), AssessmentSubmission.id.desc()).limit(1))
        return {"submission": {"request": row.request_json, "result": row.result_json} if row else None}


@router.post("/{quiz_id}/submissions/{submission_id}/retry-grading")
async def retry_quiz_grading(
    quiz_id: uuid.UUID, submission_id: uuid.UUID, body: GradingRetryIn,
    request: Request, ctx: AuthContext = Depends(api_rate_limited),
):
    async with tenant_session(ctx.tenant_id) as session:
        row = await load_submission(session, ctx, "quiz", quiz_id, submission_id)
        answers, key = row.request_json["answers"], row.idempotency_key
    return await record_attempts(
        ctx, request.app.state.gateway, quiz_id, answers,
        now_dt=await timectl.now(request.app.state.redis), idempotency_key=key,
        retry_submission_id=submission_id, expected_revision=body.expected_revision,
    )
