"""Offline-only FSRS-6 personalization contracts.

This module deliberately uses only the Python standard library.  It defines
the data contract and evaluation discipline used by a separate fit job; it
does not import torch, pandas, a database client, or a model gateway.  A fit
job may provide an optimizer and a predictor, while this module remains safe
to import from the API process.

FSRS-6 has exactly 21 parameters.  The 16-review boundary is a shadow-only
boundary: a candidate needs strictly more than 16 reviews and a positive,
predefined holdout improvement before it can be activated.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

FSRS6_ALGORITHM = "fsrs-6"
FSRS6_PARAMETER_COUNT = 21
FSRS6_DEFAULT_VERSION = "fsrs-6-default"
FSRS6_FIT_VERSION = "fsrs-6-fit-skeleton-v1"

# The published FSRS-6 default vector.  Keeping this tuple here makes the
# offline contract independent from the py-fsrs implementation details.
FSRS6_DEFAULT_PARAMETERS: tuple[float, ...] = (
    0.212,
    1.2931,
    2.3065,
    8.2956,
    6.4133,
    0.8334,
    3.0194,
    0.001,
    1.8722,
    0.1666,
    0.796,
    1.4835,
    0.0614,
    0.2629,
    1.6483,
    0.6014,
    1.8729,
    0.5425,
    0.0912,
    0.0658,
    0.1542,
)

MIN_REVIEWS_FOR_SHADOW = 16
MIN_REVIEWS_FOR_ACTIVATION = MIN_REVIEWS_FOR_SHADOW + 1
MIN_VALID_CROSS_DAY_FOR_ACTIVATION = 16
MIN_BRIER_IMPROVEMENT = 0.01
MIN_LOG_LOSS_IMPROVEMENT = 0.005
MAX_RMSE_REGRESSION = 0.0


def validate_fsrs6_parameters(parameters: Sequence[float]) -> tuple[float, ...]:
    """Validate and normalize one FSRS-6 parameter vector.

    Rejecting every vector other than 21 finite numbers prevents accidentally
    loading an FSRS-7/35-parameter artifact into the FSRS-6 scheduler.
    """

    if len(parameters) != FSRS6_PARAMETER_COUNT:
        raise ValueError(
            f"FSRS-6 requires exactly {FSRS6_PARAMETER_COUNT} parameters; got {len(parameters)}"
        )
    normalized_values: list[float] = []
    for value in parameters:
        if isinstance(value, bool):
            raise ValueError("FSRS-6 parameters must be numeric, not boolean")
        try:
            normalized_values.append(float(value))
        except (TypeError, ValueError) as exc:
            raise ValueError("FSRS-6 parameters must be numeric") from exc
    normalized = tuple(normalized_values)
    if not all(math.isfinite(value) for value in normalized):
        raise ValueError("FSRS-6 parameters must all be finite")
    return normalized


@dataclass(frozen=True)
class ReviewObservation:
    """Minimal, redaction-friendly input to an offline fit.

    ``predicted_probability`` is optional because an offline job may compute
    probabilities from the card history.  ``elapsed_days`` is used only as a
    fallback for cross-day validation when a card identifier is unavailable.
    """

    occurred_at: datetime
    correct: bool
    predicted_probability: float | None = None
    elapsed_days: float | None = None
    card_id: str | None = None


@dataclass(frozen=True)
class TimeSeriesFold:
    train: tuple[ReviewObservation, ...]
    holdout: tuple[ReviewObservation, ...]


@dataclass(frozen=True)
class EvaluationMetrics:
    sample_n: int
    log_loss: float | None
    brier: float | None
    rmse: float | None
    calibration_bins: tuple[dict[str, float | int | None], ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "sample_n": self.sample_n,
            "log_loss": self.log_loss,
            "brier": self.brier,
            "rmse": self.rmse,
            "calibration_bins": [dict(item) for item in self.calibration_bins],
        }


@dataclass(frozen=True)
class FitResult:
    algorithm: str
    parameters: tuple[float, ...]
    training_review_count: int
    valid_cross_day_review_count: int
    data_cutoff: datetime | None
    optimizer_version: str
    status: str
    metrics: dict[str, Any]

    def as_record(self, *, params_version: str) -> dict[str, Any]:
        """Return a DB-independent payload for the versioned model table."""

        return {
            "algorithm": self.algorithm,
            "params_version": params_version,
            "params_json": list(self.parameters),
            "training_review_count": self.training_review_count,
            "valid_cross_day_review_count": self.valid_cross_day_review_count,
            "data_cutoff": self.data_cutoff,
            "optimizer_version": self.optimizer_version,
            "fit_metrics_json": dict(self.metrics),
            "status": self.status,
        }


def _as_aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def chronological_observations(
    observations: Iterable[ReviewObservation],
) -> tuple[ReviewObservation, ...]:
    """Stable chronological ordering; never randomizes or shuffles input."""

    return tuple(sorted(observations, key=lambda item: _as_aware(item.occurred_at)))


def rolling_origin_splits(
    observations: Iterable[ReviewObservation],
    *,
    min_train: int = 8,
    holdout_size: int = 4,
    max_folds: int | None = None,
) -> tuple[TimeSeriesFold, ...]:
    """Create expanding-window, chronological train/holdout folds.

    The first holdout item always occurs after every training item.  This is
    intentionally a small stdlib implementation so a fit job cannot silently
    switch to random train/test splitting.
    """

    if min_train < 1 or holdout_size < 1:
        raise ValueError("min_train and holdout_size must be positive")
    if max_folds is not None and max_folds < 1:
        raise ValueError("max_folds must be positive when provided")
    ordered = chronological_observations(observations)
    folds: list[TimeSeriesFold] = []
    start = min_train
    while start < len(ordered):
        # Do not put an observation with the same timestamp into the holdout
        # as the last training observation.  The input has no event sequence
        # number, so a same-time boundary cannot establish which observation
        # is truly in the future.
        train_last_at = _as_aware(ordered[start - 1].occurred_at)
        while start < len(ordered) and _as_aware(ordered[start].occurred_at) <= train_last_at:
            start += 1
        if start >= len(ordered):
            break
        end = min(start + holdout_size, len(ordered))
        folds.append(TimeSeriesFold(train=ordered[:start], holdout=ordered[start:end]))
        if max_folds is not None and len(folds) >= max_folds:
            break
        start = end
    return tuple(folds)


def valid_cross_day_review_count(observations: Iterable[ReviewObservation]) -> int:
    """Count observations that prove a review happened on a later day.

    For a known card, the first review is not a cross-day transition.  Without
    a card ID, only an explicit positive elapsed interval is accepted.
    """

    last_day: dict[str, date] = {}
    count = 0
    for item in chronological_observations(observations):
        if item.card_id:
            current_day = _as_aware(item.occurred_at).date()
            previous_day = last_day.get(item.card_id)
            if previous_day is not None and current_day > previous_day:
                count += 1
            last_day[item.card_id] = current_day
        elif item.elapsed_days is not None and item.elapsed_days > 0:
            count += 1
    return count


def _probability(value: float | None) -> float:
    if value is None or not math.isfinite(value):
        return 0.5
    return min(1.0 - 1e-7, max(1e-7, float(value)))


def evaluate_predictions(
    observations: Sequence[ReviewObservation],
    probabilities: Sequence[float],
    *,
    bin_count: int = 10,
) -> EvaluationMetrics:
    """Compute log loss, Brier, RMSE and calibration bins."""

    if len(observations) != len(probabilities):
        raise ValueError("observations and probabilities must have equal length")
    if bin_count < 1:
        raise ValueError("bin_count must be positive")
    if not observations:
        return EvaluationMetrics(0, None, None, None, ())

    probs = [_probability(value) for value in probabilities]
    outcomes = [1.0 if item.correct else 0.0 for item in observations]
    log_loss = -sum(
        outcome * math.log(probability)
        + (1.0 - outcome) * math.log(1.0 - probability)
        for outcome, probability in zip(outcomes, probs, strict=True)
    ) / len(outcomes)
    brier = sum((probability - outcome) ** 2 for probability, outcome in zip(probs, outcomes, strict=True)) / len(outcomes)
    rmse = math.sqrt(brier)

    bins: list[dict[str, float | int | None]] = []
    for index in range(bin_count):
        lower = index / bin_count
        upper = (index + 1) / bin_count
        selected = [
            position
            for position, probability in enumerate(probs)
            if lower <= probability < upper or (index == bin_count - 1 and probability <= upper)
        ]
        bins.append(
            {
                "lower": round(lower, 6),
                "upper": round(upper, 6),
                "sample_n": len(selected),
                "predicted_mean": (
                    round(sum(probs[position] for position in selected) / len(selected), 6)
                    if selected
                    else None
                ),
                "observed_rate": (
                    round(sum(outcomes[position] for position in selected) / len(selected), 6)
                    if selected
                    else None
                ),
            }
        )
    return EvaluationMetrics(
        sample_n=len(observations),
        log_loss=round(log_loss, 8),
        brier=round(brier, 8),
        rmse=round(rmse, 8),
        calibration_bins=tuple(bins),
    )


def _metric_delta(default: float | None, candidate: float | None) -> float | None:
    if default is None or candidate is None:
        return None
    return round(default - candidate, 8)


def _finite_float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        normalized = float(value)
    except (TypeError, ValueError):
        return None
    return normalized if math.isfinite(normalized) else None


def compare_predictions(
    observations: Sequence[ReviewObservation],
    default_probabilities: Sequence[float],
    candidate_probabilities: Sequence[float],
) -> dict[str, Any]:
    """Compare default and candidate on the same chronological holdout."""

    default = evaluate_predictions(observations, default_probabilities)
    candidate = evaluate_predictions(observations, candidate_probabilities)
    return {
        "default": default.as_dict(),
        "candidate": candidate.as_dict(),
        "delta": {
            "log_loss_improvement": _metric_delta(default.log_loss, candidate.log_loss),
            "brier_improvement": _metric_delta(default.brier, candidate.brier),
            "rmse_improvement": _metric_delta(default.rmse, candidate.rmse),
        },
    }


def activation_eligibility(
    *,
    training_review_count: int,
    valid_cross_day_count: int,
    comparison: dict[str, Any] | None,
) -> tuple[bool, str]:
    """Apply the pre-declared activation gate.

    Exactly 16 reviews can be represented and evaluated in shadow, but never
    passes the activation gate.  ``comparison`` is deliberately required so
    an artifact without an honest chronological holdout cannot be activated.
    """

    if training_review_count < MIN_REVIEWS_FOR_ACTIVATION:
        return False, "INSUFFICIENT_REVIEWS"
    if valid_cross_day_count < MIN_VALID_CROSS_DAY_FOR_ACTIVATION:
        return False, "INSUFFICIENT_CROSS_DAY_REVIEWS"
    if not isinstance(comparison, Mapping):
        return False, "NO_HOLDOUT_EVALUATION"
    delta = comparison.get("delta")
    if not isinstance(delta, Mapping):
        return False, "INCOMPLETE_METRICS"
    brier = _finite_float(delta.get("brier_improvement"))
    log_loss = _finite_float(delta.get("log_loss_improvement"))
    rmse = _finite_float(delta.get("rmse_improvement"))
    if brier is None or log_loss is None or rmse is None:
        return False, "INCOMPLETE_METRICS"
    if brier < MIN_BRIER_IMPROVEMENT or log_loss < MIN_LOG_LOSS_IMPROVEMENT:
        return False, "NO_PREDEFINED_IMPROVEMENT"
    if rmse < MAX_RMSE_REGRESSION:
        return False, "RMSE_REGRESSION"
    return True, "ELIGIBLE"


def activation_eligibility_across_folds(
    *,
    training_review_count: int,
    valid_cross_day_count: int,
    comparisons: Sequence[Mapping[str, Any]],
) -> tuple[bool, str]:
    """Require the predefined gate on every chronological holdout fold.

    A single favorable final fold is too weak a basis for activation.  Keep
    the single-comparison function public for artifact compatibility, while
    making a fit result conservative when any evaluated fold is incomplete,
    regresses, or misses a threshold.
    """

    if training_review_count < MIN_REVIEWS_FOR_ACTIVATION:
        return False, "INSUFFICIENT_REVIEWS"
    if valid_cross_day_count < MIN_VALID_CROSS_DAY_FOR_ACTIVATION:
        return False, "INSUFFICIENT_CROSS_DAY_REVIEWS"
    if not comparisons:
        return False, "NO_HOLDOUT_EVALUATION"
    for comparison in comparisons:
        eligible, reason = activation_eligibility(
            training_review_count=training_review_count,
            valid_cross_day_count=valid_cross_day_count,
            comparison=comparison,
        )
        if not eligible:
            return False, reason
    return True, "ELIGIBLE"


def fit_fsrs6(
    observations: Iterable[ReviewObservation],
    *,
    optimizer: Callable[[tuple[ReviewObservation, ...]], Sequence[float]] | None = None,
    predictor: Callable[[tuple[float, ...], tuple[ReviewObservation, ...]], Sequence[float]]
    | None = None,
    data_cutoff: datetime | None = None,
    optimizer_version: str = FSRS6_FIT_VERSION,
) -> FitResult:
    """Fit/evaluate a versioned FSRS-6 candidate without random leakage.

    The default path is intentionally a safe shadow artifact: it validates
    the data and emits the default vector when an offline optimizer is not
    supplied.  Production fitting can inject an official optimizer in a
    separate dependency group, while the API never needs that dependency.
    """

    ordered = chronological_observations(observations)
    if data_cutoff is not None:
        cutoff = _as_aware(data_cutoff)
        ordered = tuple(item for item in ordered if _as_aware(item.occurred_at) <= cutoff)
    else:
        cutoff = _as_aware(ordered[-1].occurred_at) if ordered else None

    folds = rolling_origin_splits(ordered, min_train=8, holdout_size=4, max_folds=5)
    comparisons: list[dict[str, Any]] = []
    if predictor is not None:
        for fold in folds:
            # Fit each candidate on the fold's past only.  Fitting once on all
            # observations before evaluating every holdout would leak future
            # outcomes into the candidate metrics.
            candidate_parameters = validate_fsrs6_parameters(
                optimizer(fold.train) if optimizer is not None else FSRS6_DEFAULT_PARAMETERS
            )
            default_parameters = FSRS6_DEFAULT_PARAMETERS
            default_probabilities = predictor(default_parameters, fold.holdout)
            candidate_probabilities = predictor(candidate_parameters, fold.holdout)
            comparisons.append(
                compare_predictions(
                    fold.holdout,
                    default_probabilities,
                    candidate_probabilities,
                )
            )

    # The artifact persisted for a later activation decision is fit on all
    # observations only after honest fold evaluation has completed.
    parameters = validate_fsrs6_parameters(
        optimizer(ordered) if optimizer is not None else FSRS6_DEFAULT_PARAMETERS
    )

    eligible, reason = activation_eligibility_across_folds(
        training_review_count=len(ordered),
        valid_cross_day_count=valid_cross_day_review_count(ordered),
        comparisons=comparisons,
    )
    status = "active_candidate" if eligible else "shadow"
    if len(ordered) < MIN_REVIEWS_FOR_SHADOW:
        status = "shadow"
    metrics = {
        "evaluation_protocol": "rolling-origin",
        "folds": comparisons,
        "activation": {"eligible": eligible, "reason": reason},
        "optimizer_configured": optimizer is not None,
    }
    return FitResult(
        algorithm=FSRS6_ALGORITHM,
        parameters=parameters,
        training_review_count=len(ordered),
        valid_cross_day_review_count=valid_cross_day_review_count(ordered),
        data_cutoff=cutoff,
        optimizer_version=optimizer_version,
        status=status,
        metrics=metrics,
    )
