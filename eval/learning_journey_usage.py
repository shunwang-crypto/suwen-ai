"""Read gateway usage for a single learner's API evaluation from PostgreSQL."""

from __future__ import annotations

import uuid
from collections import Counter
from datetime import datetime

from eduagents_api.db import tenant_session
from eduagents_api.models import FsrsReviewEvent, MasteryLog, UsageLog
from sqlalchemy import func, or_, select

_REAL_MODES = {"live", "record"}


def _summary(rows):
    modes, features, requests = Counter(), Counter(), Counter()
    logged = real = prompt = completion = 0
    for request, feature, mode, count, input_tokens, output_tokens in rows:
        logged += count
        modes[mode] += count
        if mode in _REAL_MODES:
            real += count
            prompt += input_tokens
            completion += output_tokens
            features[feature] += count
            requests[request or "(empty)"] += count
    return {
        "logged_rows": logged,
        "real_calls": real,
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "by_feature": dict(sorted(features.items())),
        "by_mode": dict(sorted(modes.items())),
        "by_request": dict(sorted(requests.items())),
    }


async def read_model_usage(
    *,
    tenant_id: uuid.UUID | str,
    user_id: uuid.UUID | str,
    request_ids,
    started_at: datetime | None = None,
    finished_at: datetime | None = None,
) -> dict:
    """Count persisted real-model usage, with optional unmatched rows for an exclusive learner."""
    ids = {request_id for request_id in request_ids if request_id}
    if (started_at is None) != (finished_at is None):
        raise ValueError("both usage window bounds are required")
    windowed = started_at is not None and finished_at is not None
    if windowed and (
        started_at.utcoffset() is None
        or finished_at.utcoffset() is None
        or started_at >= finished_at
    ):
        raise ValueError("the usage window must be timezone-aware and increasing")
    tenant, learner = uuid.UUID(str(tenant_id)), uuid.UUID(str(user_id))
    columns = UsageLog.request_id, UsageLog.feature, UsageLog.mode
    selection = (
        select(
            *columns,
            func.count(),
            func.sum(UsageLog.prompt_tokens),
            func.sum(UsageLog.completion_tokens),
        )
        .where(UsageLog.tenant_id == tenant, UsageLog.user_id == learner)
        .group_by(*columns)
    )
    condition = UsageLog.request_id.in_(ids)
    if windowed:
        condition = or_(condition, UsageLog.at.between(started_at, finished_at))
    async with tenant_session(tenant) as session:
        rows = (await session.execute(selection.where(condition))).all()
    return {
        "scope": "request_ids_and_learner_window" if windowed else "request_ids",
        "matched": _summary(row for row in rows if row[0] in ids),
        "unmatched": _summary(row for row in rows if row[0] not in ids) if windowed else None,
        "limitations": [
            "Counts committed model usage rows in live or record mode. Token totals use those same rows.",
            "Failed provider attempts, HTTP retries, embeddings and uncommitted usage are outside this count.",
            "Unmatched rows have no observed stage attribution. The optional window requires a dedicated learner.",
        ],
    }


async def read_mastery_audit(
    *,
    learner_id,
    kc_id,
    assessments,
    quiz_attempt_id,
    review_key,
    review_card_id,
    started_at: datetime,
    finished_at: datetime,
) -> dict:
    """Match this exclusive student's committed mastery evidence to independently observed submissions."""
    learner = uuid.UUID(learner_id)
    topic = uuid.UUID(kc_id)
    expected = Counter(
        (topic, "assessment_answer", uuid.UUID(row["answer_id"])) for row in assessments
    )
    expected[topic, "quiz_attempt", uuid.UUID(quiz_attempt_id)] += 1
    async with tenant_session(learner) as session:
        review_event = await session.scalar(
            select(FsrsReviewEvent.id).where(
                FsrsReviewEvent.tenant_id == learner,
                FsrsReviewEvent.user_id == learner,
                FsrsReviewEvent.idempotency_key == review_key,
                FsrsReviewEvent.error_item_id == uuid.UUID(review_card_id),
            )
        )
        actual = Counter(
            tuple(row)
            for row in (
                await session.execute(
                    select(
                        MasteryLog.kc_id, MasteryLog.evidence_type, MasteryLog.evidence_id
                    ).where(
                        MasteryLog.tenant_id == learner,
                        MasteryLog.user_id == learner,
                        MasteryLog.at.between(started_at, finished_at),
                    )
                )
            )
        )
    missing_review = int(review_event is None)
    if review_event is not None:
        expected[topic, "review", review_event] += 1
    by_source = Counter()
    for (_, kind, _), count in actual.items():
        by_source[kind] += count
    return {
        "expected_records": expected.total() + missing_review,
        "observed_records": actual.total(),
        "unexpected_records": (actual - expected).total(),
        "missing_records": (expected - actual).total() + missing_review,
        "observed_by_source": dict(sorted(by_source.items())),
    }
