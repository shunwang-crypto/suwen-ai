"""Request-level replay and bounded grading retries share the evidence transaction."""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime

import orjson
from fastapi import HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select, text

from .models import AssessmentSubmission

MAX_GRADING_ATTEMPTS = 3


class GradingRetryIn(BaseModel):
    expected_revision: int = Field(ge=1, le=MAX_GRADING_ATTEMPTS)


def submission_key(body_key: str | None, header_key: str | None) -> str:
    body_key = (body_key or "").strip()
    header_key = (header_key or "").strip()
    if max(len(body_key), len(header_key)) > 128:
        raise HTTPException(status_code=422, detail="INVALID_IDEMPOTENCY_KEY")
    if body_key and header_key and body_key != header_key:
        raise HTTPException(status_code=409, detail="IDEMPOTENCY_KEY_MISMATCH")
    return body_key or header_key or str(uuid.uuid4())


async def prepare_submission(
    session,
    ctx,
    *,
    kind: str,
    source_id: uuid.UUID,
    payload: dict,
    key: str,
    now_dt: datetime,
    retry_id: uuid.UUID | None = None,
    expected_revision: int | None = None,
) -> tuple[AssessmentSubmission, bool]:
    payload = orjson.loads(orjson.dumps(payload))
    # Serialize even the first insert. A row lock alone cannot lock an absent row.
    lock_identity = f"assessment:{ctx.user_id}:{kind}:{key}"
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:identity, 0))"),
        {"identity": lock_identity},
    )
    row = await session.scalar(
        select(AssessmentSubmission)
        .where(
            AssessmentSubmission.user_id == ctx.user_id,
            AssessmentSubmission.kind == kind,
            AssessmentSubmission.idempotency_key == key,
        )
        .with_for_update()
    )
    fingerprint = hashlib.sha256(orjson.dumps(payload, option=orjson.OPT_SORT_KEYS)).hexdigest()
    if row is not None:
        row_source = row.quiz_id if kind == "quiz" else row.error_item_id
        if row_source != source_id or row.request_fingerprint != fingerprint:
            raise HTTPException(status_code=409, detail=f"{kind.upper()}_IDEMPOTENCY_CONFLICT")
        if retry_id is None:
            return row, False
        if row.id != retry_id:
            raise HTTPException(status_code=404, detail="NOT_FOUND")
        if expected_revision is None or expected_revision > row.grading_attempts:
            raise HTTPException(status_code=409, detail="GRADING_REVISION_CONFLICT")
        # A lost retry response must not consume another grading attempt.
        if (
            expected_revision < row.grading_attempts
            or row.result_json.get("grading_status") != "pending"
        ):
            return row, False
        if row.grading_attempts >= MAX_GRADING_ATTEMPTS:
            raise HTTPException(status_code=409, detail="GRADING_RETRY_LIMIT")
        row.grading_attempts += 1
        return row, True
    if retry_id is not None:
        raise HTTPException(status_code=404, detail="NOT_FOUND")
    row = AssessmentSubmission(
        tenant_id=ctx.tenant_id,
        user_id=ctx.user_id,
        kind=kind,
        quiz_id=source_id if kind == "quiz" else None,
        error_item_id=source_id if kind == "review" else None,
        idempotency_key=key,
        request_fingerprint=fingerprint,
        request_json=payload,
        grading_attempts=1,
        submitted_at=now_dt,
    )
    session.add(row)
    await session.flush()
    return row, True


def save_submission_result(row: AssessmentSubmission, result: dict) -> dict:
    result = {
        **result,
        "submission_id": str(row.id),
        "grading_revision": row.grading_attempts,
        "retry_available": result.get("grading_status") == "pending"
        and row.grading_attempts < MAX_GRADING_ATTEMPTS,
    }
    row.result_json = result
    return result


async def load_submission(session, ctx, kind: str, source_id: uuid.UUID, submission_id: uuid.UUID):
    source_column = (
        AssessmentSubmission.quiz_id if kind == "quiz" else AssessmentSubmission.error_item_id
    )
    row = await session.scalar(
        select(AssessmentSubmission).where(
            AssessmentSubmission.id == submission_id,
            AssessmentSubmission.user_id == ctx.user_id,
            AssessmentSubmission.kind == kind,
            source_column == source_id,
        )
    )
    if row is None:
        raise HTTPException(status_code=404, detail="NOT_FOUND")
    return row
