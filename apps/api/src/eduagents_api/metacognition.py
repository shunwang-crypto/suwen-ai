"""Deterministic confidence calibration for quiz predictions.

These metrics describe prediction calibration only.  They never write BKT,
FSRS, learner profiles, error books, or reports.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from sqlalchemy import select

from .models import Attempt, MetacognitionPrediction

WINDOWS = ("day", "week", "all")
CONFIDENCE_PROBABILITIES = {1: 0.1, 2: 0.3, 3: 0.5, 4: 0.7, 5: 0.9}
CONFIDENCE_SCALE_VERSION = "quiz-confidence-5-v1"
PREDICTED_CORRECT_SCALE_VERSION = "quiz-predicted-correct-v1"
HIGH_CONFIDENCE_MIN = 0.7


def prediction_probability(
    predicted_confidence: int | None, predicted_correct: bool | None = None
) -> float | None:
    """Map the product's five-point scale to a non-extreme probability."""
    if (
        isinstance(predicted_confidence, int)
        and not isinstance(predicted_confidence, bool)
        and predicted_confidence in CONFIDENCE_PROBABILITIES
    ):
        return CONFIDENCE_PROBABILITIES[predicted_confidence]
    if isinstance(predicted_correct, bool):
        return 1.0 if predicted_correct else 0.0
    return None


def confidence_snapshot(
    predicted_confidence: int | None,
    predicted_correct: bool | None = None,
) -> tuple[int | None, float | None, str | None]:
    """Return the immutable confidence snapshot for an ErrorItem.

    The five-point confidence value is the only comparable confidence scale.
    A binary ``predicted_correct`` is retained as a probability observation
    with its own version, but never masquerades as a five-point score.  Other
    sources (assessment/import/manual) must call this with ``None`` and keep
    all three fields NULL.
    """

    if (
        isinstance(predicted_confidence, int)
        and not isinstance(predicted_confidence, bool)
        and predicted_confidence in CONFIDENCE_PROBABILITIES
    ):
        return (
            predicted_confidence,
            CONFIDENCE_PROBABILITIES[predicted_confidence],
            CONFIDENCE_SCALE_VERSION,
        )
    if isinstance(predicted_correct, bool):
        return None, (1.0 if predicted_correct else 0.0), PREDICTED_CORRECT_SCALE_VERSION
    return None, None, None


def compute_calibration(rows: list[dict]) -> dict:
    scored: list[tuple[float, int]] = []
    high_confidence = 0
    high_confidence_wrong = 0
    for row in rows:
        actual = row.get("is_correct")
        if not isinstance(actual, bool):
            continue
        probability = prediction_probability(
            row.get("predicted_confidence"), row.get("predicted_correct")
        )
        if probability is None:
            continue
        outcome = 1 if actual else 0
        scored.append((probability, outcome))
        if probability >= HIGH_CONFIDENCE_MIN:
            high_confidence += 1
            if not actual:
                high_confidence_wrong += 1
    if not scored:
        return {"sample_n": 0, "brier_score": None, "overconfidence_rate": None}
    return {
        "sample_n": len(scored),
        "brier_score": round(
            sum((probability - outcome) ** 2 for probability, outcome in scored)
            / len(scored),
            6,
        ),
        "overconfidence_rate": (
            round(high_confidence_wrong / high_confidence, 6)
            if high_confidence
            else None
        ),
    }


def _cutoff(now: datetime, window: str) -> datetime | None:
    if window == "day":
        return now - timedelta(days=1)
    if window == "week":
        return now - timedelta(days=7)
    return None


async def calibration_summary(
    session,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    window: str,
    now: datetime,
) -> dict:
    normalized_window = window if window in WINDOWS else "all"
    stmt = (
        select(
            MetacognitionPrediction.predicted_confidence,
            MetacognitionPrediction.predicted_correct,
            Attempt.is_correct,
        )
        .join(Attempt, Attempt.id == MetacognitionPrediction.attempt_id)
        .where(
            MetacognitionPrediction.tenant_id == tenant_id,
            MetacognitionPrediction.user_id == user_id,
            Attempt.user_id == user_id,
            Attempt.question_id == MetacognitionPrediction.question_id,
            Attempt.is_correct.isnot(None),
        )
    )
    cutoff = _cutoff(now, normalized_window)
    if cutoff is not None:
        stmt = stmt.where(MetacognitionPrediction.created_at >= cutoff)
    rows = (await session.execute(stmt)).all()
    stats = compute_calibration(
        [
            {
                "predicted_confidence": row.predicted_confidence,
                "predicted_correct": row.predicted_correct,
                "is_correct": row.is_correct,
            }
            for row in rows
        ]
    )
    return {"window": normalized_window, **stats}
