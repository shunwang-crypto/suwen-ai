"""FSRS parameter activation rechecks every chronological holdout fold."""

from types import SimpleNamespace

from eduagents_api.fsrs_params import _activation_reason


def _comparison(log_loss: float) -> dict:
    return {
        "delta": {
            "brier_improvement": 0.02,
            "log_loss_improvement": log_loss,
            "rmse_improvement": 0.01,
        }
    }


def test_activation_rejects_a_failed_earlier_fold():
    row = SimpleNamespace(
        training_review_count=20,
        valid_cross_day_review_count=20,
        fit_metrics_json={
            "activation": {"eligible": True},
            "folds": [_comparison(0.001), _comparison(0.01)],
        },
    )
    assert _activation_reason(row) == "NO_PREDEFINED_IMPROVEMENT"


def test_activation_requires_recorded_folds_even_when_marker_is_true():
    row = SimpleNamespace(
        training_review_count=20,
        valid_cross_day_review_count=20,
        fit_metrics_json={"activation": {"eligible": True}, "folds": []},
    )
    assert _activation_reason(row) == "NO_HOLDOUT_EVALUATION"
