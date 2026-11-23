"""Real exam/practice attempts and verified answer evidence."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from math import isfinite

from eduagents_core.mastery.scheduler import new_card_json
from eduagents_core.workflows.quiz import grade_objective
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import delete, select

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
from ..services import apply_mastery_evidence, write_learning_event

router = APIRouter(prefix="/api/assessment-attempts", tags=["assessment-attempts"])

_ATTEMPT_TYPES = {"formal_exam", "mock_exam", "homework", "practice", "imported_record"}


def _event_time(value: datetime | None, fallback: datetime) -> datetime:
    """Normalize an optional historical taken_at to the service UTC contract."""

    candidate = value or fallback
    if candidate.tzinfo is None:
        return candidate.replace(tzinfo=UTC)
    return candidate.astimezone(UTC)


def _attempt_type_for_resource(resource_type: str) -> str:
    return {
        "exam_paper": "formal_exam",
        "mock_paper": "mock_exam",
        "homework": "homework",
    }.get(resource_type, "practice")


class AssessmentCreateIn(BaseModel):
    resource_id: uuid.UUID | None = None
    attempt_type: str | None = None
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
    unanswered: bool = False
    score: float | None = None
    max_score: float | None = None
    is_correct: bool | None = None
    grading_status: str | None = None
    teacher_comment: str | None = Field(default=None, max_length=4000)


class AnswersIn(BaseModel):
    answers: list[AnswerIn] = Field(default_factory=list, max_length=500)


class GradeIn(AnswersIn):
    verify: bool = False


def _default_max_score(rq: ResourceQuestion) -> float:
    points = rq.points
    return float(points) if points is not None and isfinite(float(points)) and points > 0 else 1.0


def _validate_assessment_score(
    score: float | None,
    max_score: float | None,
    *,
    default_max_score: float,
) -> float:
    """Validate score semantics before any answer or evidence is changed."""
    if score is not None and (not isfinite(float(score)) or score < 0):
        raise HTTPException(status_code=422, detail="INVALID_ASSESSMENT_SCORE")
    if max_score is not None and (not isfinite(float(max_score)) or max_score <= 0):
        raise HTTPException(status_code=422, detail="INVALID_ASSESSMENT_SCORE")
    effective_max_score = max_score if max_score is not None else default_max_score
    if effective_max_score <= 0 or (
        score is not None and score > effective_max_score
    ):
        raise HTTPException(status_code=422, detail="INVALID_ASSESSMENT_SCORE")
    return float(effective_max_score)


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
    if body.attempt_type is not None and body.attempt_type not in _ATTEMPT_TYPES:
        raise HTTPException(status_code=422, detail="INVALID_ATTEMPT_TYPE")
    async with tenant_session(ctx.tenant_id, org_ids=ctx.org_ids) as session:
        attempt_type = body.attempt_type or "practice"
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
            # The resource is the source of truth.  A stale client value such as
            # ``practice`` must not turn a real paper into an undifferentiated
            # practice record.
            attempt_type = _attempt_type_for_resource(resource.resource_type)
        row = AssessmentAttempt(
            tenant_id=ctx.tenant_id,
            resource_id=body.resource_id,
            student_id=ctx.user_id,
            attempt_type=attempt_type,
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


@router.delete("/{attempt_id}", status_code=204)
async def delete_assessment(attempt_id: uuid.UUID, ctx: AuthContext = Depends(api_rate_limited)):
    async with tenant_session(ctx.tenant_id, org_ids=ctx.org_ids) as session:
        attempt = await _owned_attempt(session, attempt_id, ctx, lock=True)
        if attempt.status != "draft":
            raise HTTPException(status_code=409, detail="ASSESSMENT_NOT_DRAFT")
        await session.delete(attempt)


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


async def _save_assessment_answers(
    attempt_id: uuid.UUID,
    body: AnswersIn,
    ctx: AuthContext = Depends(api_rate_limited),
    *,
    replace: bool,
):
    async with tenant_session(ctx.tenant_id, org_ids=ctx.org_ids) as session:
        attempt = await _owned_attempt(session, attempt_id, ctx, lock=True)
        if attempt.status != "draft":
            raise HTTPException(status_code=409, detail="ASSESSMENT_NOT_EDITABLE")
        question_map = await _validate_resource_questions(session, attempt, body.answers, ctx)
        if replace:
            retained_ids = {
                item.resource_question_id
                for item in body.answers
                if item.unanswered or item.response.strip()
            }
            stale_query = delete(AssessmentAnswer).where(AssessmentAnswer.attempt_id == attempt.id)
            if retained_ids:
                stale_query = stale_query.where(
                    AssessmentAnswer.resource_question_id.not_in(retained_ids)
                )
            await session.execute(stale_query)
        for item in body.answers:
            rq, _question = question_map[item.resource_question_id]
            answer = await session.scalar(
                select(AssessmentAnswer).where(
                    AssessmentAnswer.attempt_id == attempt.id,
                    AssessmentAnswer.resource_question_id == rq.id,
                )
            )
            response = "未作答" if item.unanswered else item.response.strip()
            if not response:
                if answer is not None:
                    await session.delete(answer)
                continue
            if answer is None:
                answer = AssessmentAnswer(
                    tenant_id=ctx.tenant_id,
                    attempt_id=attempt.id,
                    resource_question_id=rq.id,
                )
                session.add(answer)
            answer.response = response
            if item.max_score is not None:
                answer.max_score = item.max_score
            if item.teacher_comment is not None:
                answer.teacher_comment = item.teacher_comment
        await session.flush()
        answer_count = len(
            (
                await session.scalars(
                    select(AssessmentAnswer.id).where(AssessmentAnswer.attempt_id == attempt.id)
                )
            ).all()
        )
        return {"attempt_id": str(attempt.id), "status": attempt.status, "answer_count": answer_count}


@router.put("/{attempt_id}/answers")
async def replace_assessment_answers(
    attempt_id: uuid.UUID,
    body: AnswersIn,
    ctx: AuthContext = Depends(api_rate_limited),
):
    """Replace the complete answer set for a draft assessment."""
    return await _save_assessment_answers(attempt_id, body, ctx, replace=True)


@router.patch("/{attempt_id}/answers")
async def patch_assessment_answers(
    attempt_id: uuid.UUID,
    body: AnswersIn,
    ctx: AuthContext = Depends(api_rate_limited),
):
    """Partially update only the answers named by the request."""
    return await _save_assessment_answers(attempt_id, body, ctx, replace=False)


@router.post("/{attempt_id}/submit")
async def submit_assessment(attempt_id: uuid.UUID, ctx: AuthContext = Depends(api_rate_limited)):
    async with tenant_session(ctx.tenant_id, org_ids=ctx.org_ids) as session:
        attempt = await _owned_attempt(session, attempt_id, ctx, lock=True)
        if attempt.status == "graded":
            return {"attempt_id": str(attempt.id), "status": attempt.status}
        if attempt.status == "submitted":
            return {"attempt_id": str(attempt.id), "status": attempt.status}
        if await session.scalar(
            select(AssessmentAnswer.id).where(AssessmentAnswer.attempt_id == attempt.id).limit(1)
        ) is None:
            raise HTTPException(status_code=409, detail="ASSESSMENT_ANSWER_REQUIRED")
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
                answer.resource_question_id: (answer, rq, question)
                for answer, rq, question in await _answer_rows(session, attempt.id)
            }
            for item in body.answers:
                row = existing.get(item.resource_question_id)
                if row is None:
                    raise HTTPException(status_code=409, detail="ASSESSMENT_ALREADY_GRADED")
                answer, rq, question = row
                _validate_assessment_score(
                    item.score,
                    item.max_score if item.max_score is not None else answer.max_score,
                    default_max_score=_default_max_score(rq),
                )
                requested_response = "未作答" if item.unanswered else item.response.strip()
                if answer.response != requested_response:
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
        if body.verify:
            # 最终核验的主观题必须在本次请求中明确给出正确性；不能用先前
            # 仅保存了分数的 graded 草稿悄悄完成核验。
            submitted_by_question = {
                item.resource_question_id: item for item in body.answers
            }
            for _answer, rq, question in await _answer_rows(session, attempt.id):
                if question.type == "short":
                    submitted = submitted_by_question.get(rq.id)
                    if submitted is None or submitted.is_correct is None:
                        raise HTTPException(status_code=409, detail="ANSWER_CORRECTNESS_REQUIRED")
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
                # 提交后的评分只能处理实际保存过的作答；不能借评分接口
                # 为“未录入”的题目补造答案记录。
                raise HTTPException(status_code=409, detail="ASSESSMENT_ANSWER_NOT_FOUND")
            default_max_score = _default_max_score(rq)
            requested_max_score = (
                item.max_score if item.max_score is not None else answer.max_score
            )
            effective_max_score = _validate_assessment_score(
                item.score,
                requested_max_score,
                default_max_score=default_max_score,
            )
            response = "未作答" if item.unanswered else item.response.strip()
            if not response or answer.response != response:
                # submitted 后 response 已经是学生提交的事实；graded/verified
                # 也不能借重试修改它。
                raise HTTPException(status_code=409, detail="ASSESSMENT_ANSWER_LOCKED")
            if answer.grading_status in {"graded", "verified"}:
                # ``verify=false`` may have persisted a subjective score while
                # correctness was still NULL.  Final verification is allowed to
                # fill that missing fact explicitly; once correctness exists it
                # is immutable.
                if (
                    item.is_correct is not None
                    and answer.is_correct is not None
                    and answer.is_correct != item.is_correct
                ):
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
            answer.response = response
            if question.type in {"single", "multi", "blank"}:
                # 客观题始终以服务端标准答案自动判分，不能由客户端覆盖正确性。
                answer.is_correct = grade_objective(
                    {"type": question.type, "answer": question.answer}, answer.response
                )
                answer.max_score = effective_max_score
                answer.score = answer.max_score if answer.is_correct else 0.0
            else:
                if item.is_correct is not None:
                    answer.is_correct = item.is_correct
                if item.score is not None:
                    answer.score = item.score
                if item.max_score is not None:
                    answer.max_score = item.max_score
                # 主观题的原始分数不是 0~1 比例；没有显式正确性时保持
                # NULL，直到最终核验接口拒绝或收到明确 is_correct。
            if answer.max_score is None:
                answer.max_score = effective_max_score
            if answer.is_correct is not None and answer.score is None:
                answer.score = answer.max_score if answer.is_correct else 0.0
            if body.verify:
                if answer.is_correct is None:
                    raise HTTPException(status_code=409, detail="ANSWER_CORRECTNESS_REQUIRED")
                if question.type == "short" and item.is_correct is None:
                    raise HTTPException(status_code=409, detail="ANSWER_CORRECTNESS_REQUIRED")
                answer.grading_status = "verified"
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
                        await apply_mastery_evidence(
                            session,
                            ctx,
                            [str(k) for k in (question.kc_ids or [])],
                            bool(answer.is_correct),
                            1.0,
                            answer.id,
                            evidence_type="assessment_answer",
                            now_dt=now_dt,
                        )
                    )
                score = None
                if answer.score is not None and answer.max_score:
                    score = max(0.0, min(1.0, float(answer.score / answer.max_score)))
                elif answer.is_correct is not None:
                    score = 1.0 if answer.is_correct else 0.0
                await write_learning_event(
                    session,
                    tenant_id=ctx.tenant_id,
                    user_id=ctx.user_id,
                    source_type="assessment",
                    source_id=answer.id,
                    question_id=question.id,
                    item_key=str(rq.id),
                    kc_ids=[str(k) for k in (question.kc_ids or [])],
                    correct=bool(answer.is_correct),
                    score=score,
                    rating=3 if answer.is_correct else 1,
                    difficulty_label=question.difficulty,
                    occurred_at=_event_time(attempt.taken_at, now_dt),
                    idempotency_key=f"assessment-answer:{answer.id}",
                )
                if answer.is_correct is False:
                    existing_error = await session.scalar(
                        select(ErrorItem).where(ErrorItem.assessment_answer_id == answer.id)
                    )
                    if existing_error is None:
                        error_item_id = uuid.uuid4()
                        session.add(
                            ErrorItem(
                                id=error_item_id,
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
                                fsrs_card_json=new_card_json(
                                    now_dt, business_card_id=error_item_id
                                ),
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
