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
from ..db import tenant_session
from ..deps import AuthContext, api_rate_limited
from ..models import ERROR_IMPORT_QUIZ_KIND, Question, Quiz
from ..plan_adjust import PlanTaskAttributionError, validate_plan_task_attribution
from ..practice_recommendation import PracticeAdjustmentUnavailable
from ..profile_summary import (
    assessment_mode_kcs,
    build_profile_context,
    has_any_kc,
    weak_point_kcs,
)
from ..services import quiz_kc_readiness, record_attempts
from .practice import PracticeRequest, load_recommendation
from .threads import stream_graph_run

router = APIRouter(prefix="/api/quiz", tags=["quiz"])
log = get_logger("quiz")

_WEAK_TOPIC = "薄弱点强化练习"


def adaptive_difficulty_mix(weak: list[dict], num_questions: int) -> dict:
    """按各薄弱 KC 的掌握度算整卷难度分布 (P1-1 ZPD 自适应产品化)。

    档位与阈值同源仿真自适应臂 (thresholds.difficulty_for_mastery): 题目按薄弱点
    轮转分配 (蓝图要求覆盖均衡), 第 i 题难度 = 对应 KC 的匹配档, 故各档之和恒等于
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
    task_key: str | None = Field(default=None, max_length=96)
    confirmation_mode: Literal["auto", "required"] = "required"

    @model_validator(mode="after")
    def _topic_required(self) -> QuizCreate:
        if not self.from_weak_points and not self.topic.strip():
            raise ValueError("topic 不能为空")
        if (self.plan_id is None) != (self.task_key is None):
            raise ValueError("plan_id 与 task_key 必须同时提供")
        if self.task_key is not None:
            self.task_key = self.task_key.strip()
            if not self.task_key:
                raise ValueError("task_key 不能为空")
        return self


class AttemptAnswerIn(BaseModel):
    question_id: uuid.UUID
    response: str = Field(default="", max_length=8000)
    predicted_confidence: int | None = Field(default=None, ge=1, le=5, strict=True)
    predicted_correct: bool | None = Field(default=None, strict=True)


class AttemptIn(BaseModel):
    answers: list[AttemptAnswerIn] = Field(min_length=1, max_length=20)


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
        return JSONResponse(
            status_code=409,
            content={
                "code": "PRACTICE_DECISION_CHANGED",
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
        task_key=recommendation.task_key,
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
    runtime = getattr(request.app.state, "graphs", None)
    if runtime is None:
        raise HTTPException(status_code=503, detail="GRAPHS_UNAVAILABLE")
    # Wave 1B barrier: a KB-backed cold-start Quiz must not enter the graph
    # while its usable KC set is still being built.  Personalized and ordinary
    # Quiz paths share this barrier; weak-point requests must not spend
    # generation budget before their KB can support learning evidence.
    if body.kb_id is not None:
        gateway_mode = getattr(getattr(request.app.state, "settings", None), "gateway_mode", None)
        if not isinstance(gateway_mode, str) or not gateway_mode:
            from eduagents_core.config import get_settings

            gateway_mode = get_settings().gateway_mode
        async with tenant_session(ctx.tenant_id, org_ids=ctx.org_ids) as session:
            readiness = await quiz_kc_readiness(
                session,
                tenant_id=ctx.tenant_id,
                kb_id=body.kb_id,
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
    if body.plan_id is not None and body.task_key is not None:
        async with tenant_session(ctx.tenant_id) as session:
            try:
                await validate_plan_task_attribution(
                    session,
                    tenant_id=ctx.tenant_id,
                    user_id=ctx.user_id,
                    plan_id=body.plan_id,
                    task_key=body.task_key,
                    source_type="quiz_attempt",
                )
            except PlanTaskAttributionError as exc:
                status = 404 if exc.code == "PLAN_TASK_NOT_FOUND" else 409
                raise HTTPException(status_code=status, detail=exc.code) from None
    topic = body.topic.strip()
    kc_names = list(body.kc_names)
    difficulty_mix = dict(body.difficulty_mix)
    difficulty_reasons: list[dict] = []
    trace_prelude: list[dict] = []
    quiz_mode = "practice"
    if body.from_weak_points:
        weak = await weak_point_kcs(
            ctx.tenant_id,
            ctx.user_id,
            [str(body.kb_id)] if body.kb_id else [],
        )
        if not weak:
            # 证据不足不是"无事可做": 转 Assessment Mode 出诊断题 (§5.3 / §15)。
            # 这里曾直接 400 NO_WEAK_POINTS, 前端据此禁用入口 —— 新用户因此永远
            # 迈不出第一步 (要有薄弱点才能出题, 要出题才能产生薄弱点)。
            weak = await assessment_mode_kcs(
                ctx.tenant_id,
                ctx.user_id,
                [str(body.kb_id)] if body.kb_id else [],
            )
            quiz_mode = "assessment"
        if not weak:
            # 两种"出不了题"含义相反, 必须分开: KC 图为空 = 前置缺失 (去传资料);
            # KC 都已验证掌握 = 成功状态 (确实没什么可练的)。
            scope = [str(body.kb_id)] if body.kb_id else []
            code = (
                "ALL_KC_MASTERED"
                if await has_any_kc(ctx.tenant_id, scope)
                else "NO_KC_AVAILABLE"
            )
            raise HTTPException(status_code=409, detail=code)
        kc_names = [w["name"] for w in weak]
        topic = topic or _WEAK_TOPIC
        # ZPD 难度自适应 (P1-1): 分布由后端按掌握度算; 理由只带定性词 (P2-7)
        difficulty_mix = adaptive_difficulty_mix(weak, body.num_questions)
        difficulty_reasons = [
            {
                "kc_name": w["name"],
                "zone": w["zone"],
                "difficulty": w["difficulty"],
                "reason": w["reason"],
            }
            for w in weak
        ]
        trace_prelude.append(
            {
                "node": "weak_points",
                "event": "weak_points_selected",
                "mode": quiz_mode,
                "count": len(weak),
                "kc_names": kc_names,
                "p_mastery": [w["p_mastery"] for w in weak],
                "recent_error": [w["name"] for w in weak if w["recent_error"]],
                "difficulty_mix": difficulty_mix,
                # §31 rationale: 让前端能直接展示"为什么是这些题", 不必自己编理由
                "rationale": (
                    "这些知识点还没有测过，先做一次快速验证摸清水平"
                    if quiz_mode == "assessment"
                    else "根据你的掌握度挑出最薄弱的知识点，并按难度自适应配比"
                ),
            }
        )
    # 画像注入 (P1-2): 摘要已按 P2-7 脱敏 (定性词, 无题干原文), 供 generate 调整
    # 干扰项设计; 读画像失败一律降级为"无画像"继续出题, 不拖垮主链路
    profile_summary = ""
    try:
        profile = await build_profile_context(
            ctx.tenant_id,
            ctx.user_id,
            [str(body.kb_id)] if body.kb_id else [],
            redis=getattr(request.app.state, "redis", None),
            role="quiz",
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
        "kb_id": str(body.kb_id) if body.kb_id else "",
        "topic": topic,
        "num_questions": body.num_questions,
        "difficulty_mix": difficulty_mix,
        "kc_names": kc_names,
        "profile_summary": profile_summary,
        "difficulty_reasons": difficulty_reasons,
    }
    if body.plan_id is not None and body.task_key is not None:
        payload["plan_id"] = str(body.plan_id)
        payload["task_key"] = body.task_key
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
        return [
            {"id": str(r.id), "title": r.title, "status": r.status, "created_at": r.created_at}
            for r in rows
        ]


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
            await session.scalars(select(Question).where(Question.quiz_id == quiz_id))
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
        [answer.model_dump() for answer in body.answers],
        now_dt=now_dt,
    )
