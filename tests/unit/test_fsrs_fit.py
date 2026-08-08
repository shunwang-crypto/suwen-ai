"""B1 FSRS-6 offline fit and shadow-gate contracts."""

from datetime import UTC, datetime, timedelta

import pytest
from eduagents_core.mastery.fsrs_fit import (
    FSRS6_DEFAULT_PARAMETERS,
    MIN_REVIEWS_FOR_SHADOW,
    ReviewObservation,
    activation_eligibility,
    activation_eligibility_across_folds,
    fit_fsrs6,
    rolling_origin_splits,
    validate_fsrs6_parameters,
)

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _observations(count: int) -> list[ReviewObservation]:
    return [
        ReviewObservation(
            occurred_at=T0 + timedelta(days=index),
            correct=index % 2 == 0,
            card_id="card-1",
        )
        for index in range(count)
    ]


def test_fsrs6_rejects_fsrs7_shape_and_keeps_exact_default_vector():
    assert len(FSRS6_DEFAULT_PARAMETERS) == 21
    assert validate_fsrs6_parameters(FSRS6_DEFAULT_PARAMETERS) == FSRS6_DEFAULT_PARAMETERS
    with pytest.raises(ValueError, match="exactly 21"):
        validate_fsrs6_parameters([0.1] * 35)


def test_rolling_origin_never_places_future_observation_in_training():
    folds = rolling_origin_splits(_observations(16), min_train=8, holdout_size=4)
    assert folds
    for fold in folds:
        assert max(item.occurred_at for item in fold.train) < min(
            item.occurred_at for item in fold.holdout
        )


def test_rolling_origin_does_not_split_equal_timestamps():
    observations = [
        ReviewObservation(occurred_at=T0, correct=True, card_id="card-1"),
        ReviewObservation(occurred_at=T0, correct=False, card_id="card-2"),
        ReviewObservation(occurred_at=T0 + timedelta(days=1), correct=True, card_id="card-1"),
        ReviewObservation(occurred_at=T0 + timedelta(days=1), correct=False, card_id="card-2"),
        ReviewObservation(occurred_at=T0 + timedelta(days=2), correct=True, card_id="card-1"),
        ReviewObservation(occurred_at=T0 + timedelta(days=2), correct=False, card_id="card-2"),
    ]
    folds = rolling_origin_splits(observations, min_train=2, holdout_size=1)
    assert folds
    for fold in folds:
        assert max(item.occurred_at for item in fold.train) < min(
            item.occurred_at for item in fold.holdout
        )


def test_optimizer_sees_only_past_folds_before_full_artifact_fit():
    observations = _observations(20)
    optimizer_inputs: list[tuple[ReviewObservation, ...]] = []

    def optimizer(training: tuple[ReviewObservation, ...]) -> list[float]:
        optimizer_inputs.append(training)
        return list(FSRS6_DEFAULT_PARAMETERS)

    result = fit_fsrs6(
        observations,
        optimizer=optimizer,
        predictor=lambda _parameters, holdout: [0.5] * len(holdout),
    )

    assert result.parameters == FSRS6_DEFAULT_PARAMETERS
    assert optimizer_inputs[-1] == tuple(observations)
    assert optimizer_inputs[:-1]
    assert all(len(training) < len(observations) for training in optimizer_inputs[:-1])


def test_sixteen_reviews_are_shadow_only_and_cross_day_is_counted():
    observations = _observations(MIN_REVIEWS_FOR_SHADOW)
    result = fit_fsrs6(observations)
    assert result.status == "shadow"
    assert result.training_review_count == MIN_REVIEWS_FOR_SHADOW
    assert result.valid_cross_day_review_count == MIN_REVIEWS_FOR_SHADOW - 1


def test_activation_requires_both_predefined_improvements_and_more_than_sixteen():
    comparison = {
        "delta": {
            "brier_improvement": 0.02,
            "log_loss_improvement": 0.01,
            "rmse_improvement": 0.01,
        }
    }
    eligible, reason = activation_eligibility(
        training_review_count=16,
        valid_cross_day_count=16,
        comparison=comparison,
    )
    assert not eligible and reason == "INSUFFICIENT_REVIEWS"


def test_activation_rejects_non_finite_holdout_metrics():
    eligible, reason = activation_eligibility(
        training_review_count=17,
        valid_cross_day_count=16,
        comparison={
            "delta": {
                "brier_improvement": float("nan"),
                "log_loss_improvement": 0.01,
                "rmse_improvement": 0.01,
            }
        },
    )
    assert not eligible and reason == "INCOMPLETE_METRICS"


def test_activation_requires_each_time_series_fold_to_pass():
    passing = {
        "delta": {
            "brier_improvement": 0.02,
            "log_loss_improvement": 0.01,
            "rmse_improvement": 0.01,
        }
    }
    failing = {
        "delta": {
            "brier_improvement": 0.02,
            "log_loss_improvement": 0.001,
            "rmse_improvement": 0.01,
        }
    }
    eligible, reason = activation_eligibility_across_folds(
        training_review_count=17,
        valid_cross_day_count=16,
        comparisons=(passing, failing),
    )
    assert not eligible and reason == "NO_PREDEFINED_IMPROVEMENT"
