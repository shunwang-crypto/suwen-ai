from __future__ import annotations

import uuid

from eduagents_core.logging import get_logger
from eduagents_core.mastery.thresholds import DIFFICULTIES, difficulty_for_mastery
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import or_, select

from .. import timectl
from ..db import tenant_session
from ..deps import AuthContext, api_rate_limited
from ..models import ERROR_IMPORT_QUIZ_KIND, Question, Quiz
from ..profile_summary import build_profile_context, weak_point_kcs
from ..services import record_attempts
from .threads import stream_graph_run

router = APIRouter(prefix="/api/quiz", tags=["quiz"])
log = get_logger("quiz")

_WEAK_TOPIC = "薄弱点强化练习"


def adaptive_difficulty_mix(weak: list[dict], num_questions: int) -> dict:
    """按各薄弱 KC 的掌握度算整卷难度分布 (P1-1 ZPD 自适应产品化)。

    档位与阈值同源仿真自适应臂 (thresholds.difficulty_for_mastery): 题目按薄弱点
    轮转分配 (蓝图要求覆盖均衡), 第 i 题难度 = 对应 KC 的匹配档, 故各档之和恒等于
    num_questions。由后端计算, 不信任前端传的 difficulty_mix (可篡改)。
    """
    counts = dict.fromkeys(DIFFICULTIES, 0)
    for i in range(num_questions):
        counts[difficulty_for_mastery(float(weak[i % len(weak)]["p_mastery"]))] += 1
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

    @model_validator(mode="after")
    def _topic_required(self) -> QuizCreate:
        if not self.from_weak_points and not self.topic.strip():
            raise ValueError("topic 不能为空")
        return self


class AttemptIn(BaseModel):
    answers: list[dict]  # [{question_id, response}]


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
    topic = body.topic.strip()
    kc_names = list(body.kc_names)
    difficulty_mix = dict(body.difficulty_mix)
    difficulty_reasons: list[dict] = []
    trace_prelude: list[dict] = []
    if body.from_weak_points:
        weak = await weak_point_kcs(
            ctx.tenant_id,
            ctx.user_id,
            [str(body.kb_id)] if body.kb_id else [],
        )
        if not weak:
            # 无薄弱点不静默降级成普通出题: 前端据此提示"先做一次练习积累数据"
            raise HTTPException(status_code=400, detail="NO_WEAK_POINTS")
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
                "count": len(weak),
                "kc_names": kc_names,
                "p_mastery": [w["p_mastery"] for w in weak],
                "recent_error": [w["name"] for w in weak if w["recent_error"]],
                "difficulty_mix": difficulty_mix,
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
        "kb_id": str(body.kb_id) if body.kb_id else "",
        "topic": topic,
        "num_questions": body.num_questions,
        "difficulty_mix": difficulty_mix,
        "kc_names": kc_names,
        "profile_summary": profile_summary,
        "difficulty_reasons": difficulty_reasons,
    }
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
        return {
            "id": str(quiz.id),
            "title": quiz.title,
            "blueprint": quiz.blueprint_json,
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
        ctx, request.app.state.gateway, quiz_id, body.answers, now_dt=now_dt
    )
