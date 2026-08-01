"""Real exam/practice attempts and verified answer evidence."""

from __future__ import annotations

import uuid
from datetime import datetime

from eduagents_core.graphs.quiz_graph import grade_objective
from eduagents_core.mastery.scheduler import new_card_json
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select

from .. import timectl
from ..db import tenant_session
from ..deps import AuthContext, api_rate_limited
from ..models import (
    AssessmentAnswer,
    AssessmentAttempt,
    ErrorItem,
    LearningResource,
    MasteryLog,
    Question,
    ResourceQuestion,
)
from ..services import _update_mastery

router = APIRouter(prefix="/api/assessment-attempts", tags=["assessment-attempts"])

_ATTEMPT_TYPES = {"formal_exam", "mock_exam", "homework", "practice", "imported_record"}
_ATTEMPT_STATUSES = {"draft", "submitted", "graded"}


class AssessmentCreateIn(BaseModel):
    resource_id: uuid.UUID | None = None
    attempt_type: str = "practice"
    title: str = Field(default="", max_length=255)
    taken_at: datetime | None = None
    duration_minutes: int | None = Field(default=None, ge=0)
    metadata: dict = Field(default_factory=dict)


class AssessmentPatchIn(BaseModel):
    title: str | None = Field(default=None, max_length=255)
    taken_at: datetime | None = None
    duration_minutes: int | None = Field(default=None, ge=0)
    metadata: dict | None = None


class AnswerIn(BaseModel):
    resource_question_id: uuid.UUID
    response: str = Field(default="", max_length=12000)
    score: float | None = Field(default=None, ge=0)
    max_score: float | None = Field(default=None, ge=0)
    is_correct: bool | None = None
    grading_status: str | None = None
    teacher_comment: str | None = Field(default=None, max_length=4000)


class AnswersIn(BaseModel):
    answers: list[AnswerIn] = Field(default_factory=list, max_length=500)


class GradeIn(AnswersIn):
    verify: bool = False


def _attempt_dict(row: AssessmentAttempt) -> dict:
    return {
        "id": str(row.id),
        "resource_id": str(row.resource_id) if row.resource_id else None,
        "student_id": str(row.student_id),
        "attempt_type": row.attempt_type,
        "title": row.title,
        "taken_at": row.taken_at,
        "duration_minutes": row.duration_minutes,
        "score": row.score,
        "total_score": row.total_score,
        "status": row.status,
        "grader_id": str(row.grader_id) if row.grader_id else None,
        "metadata": row.metadata_json or {},
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _answer_dict(answer: AssessmentAnswer, question: Question, *, reveal_answer: bool) -> dict:
    return {
        "id": str(answer.id),
        "resource_question_id": str(answer.resource_question_id),
        "question_id": str(question.id),
        "question": {
            "type": question.type,
            "stem": question.stem,
            "options": question.options_json or [],
            **({"answer": question.answer, "explanation": question.explanation} if reveal_answer else {}),
        },
        "response": answer.response,
        "score": answer.score,
        "max_score": answer.max_score,
        "is_correct": answer.is_correct,
        "grading_status": answer.grading_status,
        "teacher_comment": answer.teacher_comment,
        "created_at": answer.created_at,
        "updated_at": answer.updated_at,
    }


async def _owned_attempt(session, attempt_id: uuid.UUID, ctx: AuthContext, *, lock: bool = False):
    query = select(AssessmentAttempt).where(
        AssessmentAttempt.id == attempt_id,
        AssessmentAttempt.tenant_id == ctx.tenant_id,
        AssessmentAttempt.student_id == ctx.user_id,
    )
    if lock:
        query = query.with_for_update()
    row = await session.scalar(query)
    if row is None:
        raise HTTPException(status_code=404, detail="NOT_FOUND")
    return row


async def _answer_rows(session, attempt_id: uuid.UUID):
    return (
        await session.execute(
            select(AssessmentAnswer, ResourceQuestion, Question)
            .join(ResourceQuestion, ResourceQuestion.id == AssessmentAnswer.resource_question_id)
            .join(Question, Question.id == ResourceQuestion.question_id)
            .where(AssessmentAnswer.attempt_id == attempt_id)
            .order_by(AssessmentAnswer.created_at.asc())
        )
    ).all()


async def _validate_resource_questions(session, attempt: AssessmentAttempt, answers: list[AnswerIn], ctx: AuthContext):
    ids = {item.resource_question_id for item in answers}
    if len(ids) != len(answers):
        raise HTTPException(status_code=422, detail="DUPLICATE_ANSWER")
    if not ids:
        return {}
    rows = (
        await session.execute(
            select(ResourceQuestion, Question)
            .join(Question, Question.id == ResourceQuestion.question_id)
            .join(LearningResource, LearningResource.id == ResourceQuestion.resource_id)
            .where(
                ResourceQuestion.id.in_(ids),
                ResourceQuestion.tenant_id == ctx.tenant_id,
                LearningResource.owner_id == ctx.user_id,
                *([ResourceQuestion.resource_id == attempt.resource_id] if attempt.resource_id else []),
            )
        )
    ).all()
    by_id = {rq.id: (rq, question) for rq, question in rows}
    if len(by_id) != len(ids):
        raise HTTPException(status_code=404, detail="RESOURCE_QUESTION_NOT_FOUND")
    if any(rq.verification_status != "verified" for rq, _question in by_id.values()):
        raise HTTPException(status_code=409, detail="RESOURCE_QUESTION_NOT_VERIFIED")
    return by_id


@router.post("", status_code=201)
async def create_assessment(
    body: AssessmentCreateIn,
    ctx: AuthContext = Depends(api_rate_limited),
):
    if body.attempt_type not in _ATTEMPT_TYPES:
        raise HTTPException(status_code=422, detail="INVALID_ATTEMPT_TYPE")
    async with tenant_session(ctx.tenant_id, org_ids=ctx.org_ids) as session:
        if body.resource_id is not None:
            resource = await session.scalar(
                select(LearningResource).where(
                    LearningResource.id == body.resource_id,
                    LearningResource.owner_id == ctx.user_id,
                    LearningResource.tenant_id == ctx.tenant_id,
                )
            )
            if resource is None:
                raise HTTPException(status_code=404, detail="NOT_FOUND")
        row = AssessmentAttempt(
            tenant_id=ctx.tenant_id,
            resource_id=body.resource_id,
            student_id=ctx.user_id,
            attempt_type=body.attempt_type,
            title=body.title,
            taken_at=body.taken_at,
            duration_minutes=body.duration_minutes,
            metadata_json=body.metadata,
        )
        session.add(row)
        await session.flush()
        return _attempt_dict(row)


@router.get("")
async def list_assessments(ctx: AuthContext = Depends(api_rate_limited)):
    async with tenant_session(ctx.tenant_id, org_ids=ctx.org_ids) as session:
        rows = (
            await session.scalars(
                select(AssessmentAttempt)
                .where(
                    AssessmentAttempt.tenant_id == ctx.tenant_id,
                    AssessmentAttempt.student_id == ctx.user_id,
                )
                .order_by(AssessmentAttempt.created_at.desc())
            )
        ).all()
        return [_attempt_dict(row) for row in rows]


@router.get("/{attempt_id}")
async def get_assessment(attempt_id: uuid.UUID, ctx: AuthContext = Depends(api_rate_limited)):
    async with tenant_session(ctx.tenant_id, org_ids=ctx.org_ids) as session:
        attempt = await _owned_attempt(session, attempt_id, ctx)
        reveal = attempt.status in {"submitted", "graded"}
        answers = [
            _answer_dict(answer, question, reveal_answer=reveal)
            for answer, _rq, question in await _answer_rows(session, attempt.id)
        ]
        return {**_attempt_dict(attempt), "answers": answers, "answers_revealed": reveal}


@router.patch("/{attempt_id}")
async def update_assessment(
    attempt_id: uuid.UUID,
    body: AssessmentPatchIn,
    ctx: AuthContext = Depends(api_rate_limited),
):
    async with tenant_session(ctx.tenant_id, org_ids=ctx.org_ids) as session:
        attempt = await _owned_attempt(session, attempt_id, ctx, lock=True)
        if attempt.status != "draft":
            raise HTTPException(status_code=409, detail="ASSESSMENT_NOT_EDITABLE")
        values = body.model_dump(exclude_unset=True)
        if "metadata" in values:
            attempt.metadata_json = values.pop("metadata") or {}
        for key, value in values.items():
            setattr(attempt, key, value)
        return _attempt_dict(attempt)


@router.put("/{attempt_id}/answers")
@router.patch("/{attempt_id}/answers")
async def put_assessment_answers(
    attempt_id: uuid.UUID,
    body: AnswersIn,
    ctx: AuthContext = Depends(api_rate_limited),
):
    async with tenant_session(ctx.tenant_id, org_ids=ctx.org_ids) as session:
        attempt = await _owned_attempt(session, attempt_id, ctx, lock=True)
        if attempt.status != "draft":
            raise HTTPException(status_code=409, detail="ASSESSMENT_NOT_EDITABLE")
        question_map = await _validate_resource_questions(session, attempt, body.answers, ctx)
        for item in body.answers:
            rq, _question = question_map[item.resource_question_id]
            answer = await session.scalar(
                select(AssessmentAnswer).where(
                    AssessmentAnswer.attempt_id == attempt.id,
                    AssessmentAnswer.resource_question_id == rq.id,
                )
            )
            if answer is None:
                answer = AssessmentAnswer(
                    tenant_id=ctx.tenant_id,
                    attempt_id=attempt.id,
                    resource_question_id=rq.id,
                )
                session.add(answer)
            answer.response = item.response
            if item.max_score is not None:
                answer.max_score = item.max_score
            if item.teacher_comment is not None:
                answer.teacher_comment = item.teacher_comment
        await session.flush()
        return {"attempt_id": str(attempt.id), "status": attempt.status, "answer_count": len(body.answers)}


@router.post("/{attempt_id}/submit")
async def submit_assessment(attempt_id: uuid.UUID, ctx: AuthContext = Depends(api_rate_limited)):
    async with tenant_session(ctx.tenant_id, org_ids=ctx.org_ids) as session:
        attempt = await _owned_attempt(session, attempt_id, ctx, lock=True)
        if attempt.status == "graded":
            return {"attempt_id": str(attempt.id), "status": attempt.status}
        if attempt.status == "submitted":
            return {"attempt_id": str(attempt.id), "status": attempt.status}
        attempt.status = "submitted"
        return {"attempt_id": str(attempt.id), "status": attempt.status}


@router.post("/{attempt_id}/grade-or-verify")
async def grade_or_verify_assessment(
    attempt_id: uuid.UUID,
    body: GradeIn,
    request: Request,
    ctx: AuthContext = Depends(api_rate_limited),
):
    now_dt = await timectl.now(request.app.state.redis)
    async with tenant_session(ctx.tenant_id, org_ids=ctx.org_ids) as session:
        attempt = await _owned_attempt(session, attempt_id, ctx, lock=True)
        if attempt.status == "draft":
            raise HTTPException(status_code=409, detail="ASSESSMENT_NOT_SUBMITTED")

        # graded 是终态。允许完全相同的请求重试，避免网络重试制造第二份
        # mastery/error 证据；任何语义字段变化都必须走后续的纠错/重算能力，
        # 本阶段明确返回 409，不静默覆盖历史。
        if attempt.status == "graded":
            existing = {
                answer.resource_question_id: answer
                for answer, _rq, _question in await _answer_rows(session, attempt.id)
            }
            for item in body.answers:
                answer = existing.get(item.resource_question_id)
                if answer is None or answer.response != item.response:
                    raise HTTPException(status_code=409, detail="ASSESSMENT_ALREADY_GRADED")
                if item.is_correct is not None and answer.is_correct != item.is_correct:
                    raise HTTPException(status_code=409, detail="ASSESSMENT_ALREADY_GRADED")
                if item.score is not None and answer.score != item.score:
                    raise HTTPException(status_code=409, detail="ASSESSMENT_ALREADY_GRADED")
                if item.max_score is not None and answer.max_score != item.max_score:
                    raise HTTPException(status_code=409, detail="ASSESSMENT_ALREADY_GRADED")
            return {
                "attempt_id": str(attempt.id),
                "status": attempt.status,
                "score": attempt.score,
                "total_score": attempt.total_score,
                "mastery_changes": [],
            }

        if not body.verify and any(item.grading_status == "verified" for item in body.answers):
            # 单题的 verified 不能绕过整次 attempt 的最终核验。
            raise HTTPException(status_code=409, detail="ATTEMPT_VERIFICATION_REQUIRED")
        question_map = await _validate_resource_questions(session, attempt, body.answers, ctx)
        changes: list[dict] = []
        for item in body.answers:
            rq, question = question_map[item.resource_question_id]
            answer = await session.scalar(
                select(AssessmentAnswer).where(
                    AssessmentAnswer.attempt_id == attempt.id,
                    AssessmentAnswer.resource_question_id == rq.id,
                )
            )
            if item.grading_status not in {None, "pending", "graded", "verified"}:
                raise HTTPException(status_code=422, detail="INVALID_GRADING_STATUS")
            if answer is None:
                # 提交后只能评分已经保存的学生作答；空白题可以在评分时补建
                # 一条空答，不能借评分接口伪造一份新的作答记录。
                if attempt.status != "draft" and item.response:
                    raise HTTPException(status_code=409, detail="ASSESSMENT_ANSWER_LOCKED")
                answer = AssessmentAnswer(
                    tenant_id=ctx.tenant_id,
                    attempt_id=attempt.id,
                    resource_question_id=rq.id,
                    response=item.response,
                )
                session.add(answer)
                await session.flush()
            elif attempt.status != "draft" and answer.response != item.response:
                # submitted 后 response 已经是学生提交的事实；graded/verified
                # 也不能借重试修改它。
                raise HTTPException(status_code=409, detail="ASSESSMENT_ANSWER_LOCKED")
            if answer.grading_status in {"graded", "verified"}:
                if item.is_correct is not None and answer.is_correct != item.is_correct:
                    raise HTTPException(status_code=409, detail="ASSESSMENT_ANSWER_LOCKED")
                if item.score is not None and answer.score != item.score:
                    raise HTTPException(status_code=409, detail="ASSESSMENT_ANSWER_LOCKED")
                if item.max_score is not None and answer.max_score != item.max_score:
                    raise HTTPException(status_code=409, detail="ASSESSMENT_ANSWER_LOCKED")
                if item.grading_status == "pending":
                    raise HTTPException(status_code=409, detail="ASSESSMENT_ANSWER_LOCKED")
            if answer.grading_status == "verified":
                # verified 只能通过同一请求重试保持幂等；若 attempt 状态因
                # 历史脏数据仍非 graded，拒绝静默修复证据链。
                raise HTTPException(status_code=409, detail="ASSESSMENT_ANSWER_LOCKED")
            answer.response = item.response
            if question.type in {"single", "multi", "blank"} and item.is_correct is None:
                answer.is_correct = grade_objective(
                    {"type": question.type, "answer": question.answer}, answer.response
                )
                answer.max_score = item.max_score if item.max_score is not None else (rq.points or 1.0)
                answer.score = answer.max_score if answer.is_correct else 0.0
            else:
                if item.is_correct is not None:
                    answer.is_correct = item.is_correct
                if item.score is not None:
                    answer.score = item.score
                if item.max_score is not None:
                    answer.max_score = item.max_score
                if item.score is not None and item.is_correct is None:
                    answer.is_correct = item.score >= 0.6
                if answer.is_correct is not None and answer.max_score is not None and answer.score is None:
                    answer.score = answer.max_score if answer.is_correct else 0.0
            if body.verify:
                answer.grading_status = "verified"
                if answer.is_correct is None:
                    raise HTTPException(status_code=409, detail="ANSWER_CORRECTNESS_REQUIRED")
            else:
                answer.grading_status = "graded"
            if item.teacher_comment is not None:
                answer.teacher_comment = item.teacher_comment

        if body.verify:
            # body.answers 可以只提交本次刚评分的题；最终核验会检查 attempt
            # 中此前已经保存的答案，并把它们一起推进到 verified，避免出现
            # attempt=graded 但仍有 pending/graded answer 的裂缝。
            all_rows = await _answer_rows(session, attempt.id)
            for answer, rq, question in all_rows:
                if answer.grading_status == "pending" or answer.is_correct is None:
                    raise HTTPException(status_code=409, detail="ANSWER_CORRECTNESS_REQUIRED")
                answer.grading_status = "verified"
                has_mastery = await session.scalar(
                    select(MasteryLog.id)
                    .where(
                        MasteryLog.user_id == ctx.user_id,
                        MasteryLog.evidence_type == "assessment_answer",
                        MasteryLog.evidence_id == answer.id,
                    )
                    .limit(1)
                )
                if has_mastery is None:
                    changes.extend(
                        await _update_mastery(
                            session,
                            ctx,
                            [str(k) for k in (question.kc_ids or [])],
                            bool(answer.is_correct),
                            1.0,
                            answer.id,
                            evidence_type="assessment_answer",
                        )
                    )
                if answer.is_correct is False:
                    existing_error = await session.scalar(
                        select(ErrorItem).where(ErrorItem.assessment_answer_id == answer.id)
                    )
                    if existing_error is None:
                        session.add(
                            ErrorItem(
                                tenant_id=ctx.tenant_id,
                                user_id=ctx.user_id,
                                question_id=question.id,
                                kc_ids=list(question.kc_ids or []),
                                cause="knowledge_gap",
                                state="new",
                                verification_status="verified",
                                resource_question_id=rq.id,
                                assessment_answer_id=answer.id,
                                original_response=answer.response,
                                fsrs_card_json=new_card_json(now_dt),
                            )
                        )
            attempt.status = "graded"
            attempt.grader_id = None
            attempt.metadata_json = {
                **(attempt.metadata_json or {}),
                "self_verified": True,
                "verified_by": str(ctx.user_id),
            }
        else:
            attempt.status = "submitted"
        scores = [a.score for a, _rq, _q in await _answer_rows(session, attempt.id) if a.score is not None]
        max_scores = [a.max_score for a, _rq, _q in await _answer_rows(session, attempt.id) if a.max_score is not None]
        if scores:
            attempt.score = sum(scores)
        if max_scores:
            attempt.total_score = sum(max_scores)
        return {
            "attempt_id": str(attempt.id),
            "status": attempt.status,
            "score": attempt.score,
            "total_score": attempt.total_score,
            "mastery_changes": changes,
        }
