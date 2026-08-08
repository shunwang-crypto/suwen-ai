"""Versioned FSRS-6 selection and testable activation/rollback paths.

The API only reads an already-fitted artifact when the explicit feature flag is
enabled.  Fit jobs can use :mod:`eduagents_core.mastery.fsrs_fit` without
pulling an optimizer into the API dependency set.  Activation and rollback
rebuild cards from immutable ``fsrs_review_events`` while the caller's
transaction is still open, so a parameter status change cannot be committed
without its corresponding card state.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from eduagents_core.mastery import scheduler
from eduagents_core.mastery.fsrs_fit import (
    FSRS6_DEFAULT_PARAMETERS,
    FSRS6_DEFAULT_VERSION,
    activation_eligibility_across_folds,
    validate_fsrs6_parameters,
)
from sqlalchemy import select

from .models import ErrorItem, FsrsParams, FsrsReviewEvent


@dataclass(frozen=True)
class ScheduleParams:
    """Explicit scheduler input used by one review transaction."""

    params: tuple[float, ...]
    version: str


DEFAULT_SCHEDULE_PARAMS = ScheduleParams(FSRS6_DEFAULT_PARAMETERS, FSRS6_DEFAULT_VERSION)


def _as_aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def schedule_params_from_row(row: FsrsParams | None) -> ScheduleParams:
    if row is None:
        return DEFAULT_SCHEDULE_PARAMS
    if row.algorithm != "fsrs-6":
        raise ValueError("unsupported FSRS algorithm")
    return ScheduleParams(
        params=validate_fsrs6_parameters(row.params_json),
        version=row.params_version,
    )


async def resolve_schedule_params(
    session,
    *,
    tenant_id: UUID,
    user_id: UUID,
    enabled: bool,
) -> ScheduleParams:
    """Resolve one active user model, failing closed to the default model."""

    if not enabled:
        return DEFAULT_SCHEDULE_PARAMS
    row = await session.scalar(
        select(FsrsParams)
        .where(
            FsrsParams.tenant_id == tenant_id,
            FsrsParams.user_id == user_id,
            FsrsParams.status == "active",
        )
        .order_by(FsrsParams.activated_at.desc().nullslast(), FsrsParams.created_at.desc())
        .limit(1)
    )
    try:
        return schedule_params_from_row(row)
    except (TypeError, ValueError):
        # A malformed artifact must never change live scheduling.  The fit
        # migration has a 21-element JSON check; this is defense in depth for
        # old/manual rows and a safe rollback path.
        return DEFAULT_SCHEDULE_PARAMS


def fsrs_params_from_fit(*, tenant_id: UUID, user_id: UUID, fit_result, params_version: str) -> FsrsParams:
    """Convert an offline ``FitResult`` into a pending shadow row."""

    return FsrsParams(
        tenant_id=tenant_id,
        user_id=user_id,
        algorithm=fit_result.algorithm,
        params_version=params_version,
        params_json=list(validate_fsrs6_parameters(fit_result.parameters)),
        training_review_count=fit_result.training_review_count,
        valid_cross_day_review_count=fit_result.valid_cross_day_review_count,
        data_cutoff=fit_result.data_cutoff,
        optimizer_version=fit_result.optimizer_version,
        fit_metrics_json=dict(fit_result.metrics),
        status="shadow",
    )


def _activation_reason(row: FsrsParams) -> str:
    activation = (row.fit_metrics_json or {}).get("activation")
    if not isinstance(activation, dict):
        return "NO_HOLDOUT_EVALUATION"
    folds = (row.fit_metrics_json or {}).get("folds")
    if not isinstance(folds, list) or not folds:
        return "NO_HOLDOUT_EVALUATION"
    eligible, reason = activation_eligibility_across_folds(
        training_review_count=row.training_review_count,
        valid_cross_day_count=row.valid_cross_day_review_count,
        comparisons=folds,
    )
    if not eligible:
        return reason
    if activation.get("eligible") is not True:
        return "FIT_ARTIFACT_NOT_MARKED_ELIGIBLE"
    return "ELIGIBLE"


async def _rebuild_user_cards(
    session,
    *,
    tenant_id: UUID,
    user_id: UUID,
    schedule: ScheduleParams,
    now: datetime,
) -> int:
    """Replay immutable review outcomes into every verified user card."""

    items = (
        await session.scalars(
            select(ErrorItem)
            .where(
                ErrorItem.tenant_id == tenant_id,
                ErrorItem.user_id == user_id,
                ErrorItem.verification_status == "verified",
            )
            .with_for_update()
        )
    ).all()
    item_ids = [item.id for item in items]
    events_by_item: dict[UUID, list[FsrsReviewEvent]] = defaultdict(list)
    if item_ids:
        events = (
            await session.scalars(
                select(FsrsReviewEvent)
                .where(
                    FsrsReviewEvent.tenant_id == tenant_id,
                    FsrsReviewEvent.user_id == user_id,
                    FsrsReviewEvent.error_item_id.in_(item_ids),
                )
                .order_by(FsrsReviewEvent.error_item_id, FsrsReviewEvent.reviewed_at, FsrsReviewEvent.created_at)
            )
        ).all()
        for event in events:
            events_by_item[event.error_item_id].append(event)

    for item in items:
        history = events_by_item.get(item.id, [])
        replay_at = _as_aware(history[0].reviewed_at) if history else _as_aware(now)
        card = scheduler.new_card_json(
            replay_at,
            business_card_id=item.id,
            params_version=schedule.version,
        )
        streak = 0
        for event in history:
            card, _due, _log = scheduler.review_with_log(
                card,
                bool(event.correct),
                _as_aware(event.reviewed_at),
                business_card_id=item.id,
                review_duration_ms=event.review_duration_ms,
                params=schedule.params,
                params_version=schedule.version,
            )
            streak = streak + 1 if event.correct else 0
            card["streak"] = streak
        item.fsrs_card_json = card
    return len(items)


async def activate_fsrs_params(
    session,
    *,
    tenant_id: UUID,
    user_id: UUID,
    candidate_id: UUID,
    now: datetime,
) -> int:
    """Activate an eligible shadow artifact and atomically replay cards."""

    candidate = await session.scalar(
        select(FsrsParams)
        .where(
            FsrsParams.id == candidate_id,
            FsrsParams.tenant_id == tenant_id,
            FsrsParams.user_id == user_id,
        )
        .with_for_update()
    )
    if candidate is None:
        raise ValueError("FSRS_PARAMS_NOT_FOUND")
    if candidate.status not in {"shadow", "retired"}:
        raise ValueError("FSRS_PARAMS_NOT_ACTIVATABLE")
    reason = _activation_reason(candidate)
    if reason != "ELIGIBLE":
        raise ValueError(f"FSRS_PARAMS_NOT_ELIGIBLE:{reason}")
    schedule = schedule_params_from_row(candidate)

    active_rows = (
        await session.scalars(
            select(FsrsParams)
            .where(
                FsrsParams.tenant_id == tenant_id,
                FsrsParams.user_id == user_id,
                FsrsParams.status == "active",
                FsrsParams.id != candidate.id,
            )
            .with_for_update()
        )
    ).all()
    for row in active_rows:
        row.status = "retired"
        row.retired_at = now
    # Retire the previous active row in the database before activating the
    # candidate.  Otherwise SQLAlchemy may batch both UPDATEs in primary-key
    # order and transiently violate the partial unique index that permits only
    # one active artifact per user.
    if active_rows:
        await session.flush()
    candidate.status = "active"
    candidate.activated_at = now
    candidate.retired_at = None
    return await _rebuild_user_cards(
        session,
        tenant_id=tenant_id,
        user_id=user_id,
        schedule=schedule,
        now=now,
    )


async def rollback_fsrs_params(
    session,
    *,
    tenant_id: UUID,
    user_id: UUID,
    target_id: UUID,
    now: datetime,
) -> int:
    """Rollback through the same activation/replay transaction path."""

    target = await session.scalar(
        select(FsrsParams).where(
            FsrsParams.id == target_id,
            FsrsParams.tenant_id == tenant_id,
            FsrsParams.user_id == user_id,
            FsrsParams.status == "retired",
        )
    )
    if target is None:
        raise ValueError("FSRS_ROLLBACK_TARGET_NOT_FOUND")
    # A rollback is a previously accepted artifact; its fit gate was already
    # evaluated at its original activation.  Reuse the exact same function by
    # restoring the eligibility marker only in memory, without changing the
    # persisted evidence.
    original_metrics = dict(target.fit_metrics_json or {})
    metrics = dict(original_metrics)
    activation = dict(metrics.get("activation") or {})
    activation["eligible"] = True
    metrics["activation"] = activation
    target.fit_metrics_json = metrics
    try:
        return await activate_fsrs_params(
            session,
            tenant_id=tenant_id,
            user_id=user_id,
            candidate_id=target.id,
            now=now,
        )
    finally:
        target.fit_metrics_json = original_metrics
