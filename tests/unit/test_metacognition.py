from datetime import UTC, datetime, timedelta

from eduagents_api.metacognition import _cutoff, compute_calibration, prediction_probability
from eduagents_worker.retention import _statements


def test_five_point_confidence_probability_mapping_is_non_extreme():
    assert [prediction_probability(value) for value in range(1, 6)] == [
        0.1,
        0.3,
        0.5,
        0.7,
        0.9,
    ]
    assert prediction_probability(0) is None
    assert prediction_probability(6) is None
    assert prediction_probability(True) is None


def test_calibration_brier_and_high_confidence_error_rate():
    result = compute_calibration(
        [
            {"predicted_confidence": 5, "is_correct": True},
            {"predicted_confidence": 4, "is_correct": False},
            {"predicted_confidence": 2, "is_correct": False},
        ]
    )
    assert result == {
        "sample_n": 3,
        "brier_score": 0.196667,
        "overconfidence_rate": 0.5,
    }


def test_calibration_ignores_unscored_and_invalid_predictions():
    assert compute_calibration(
        [
            {"predicted_confidence": 5, "is_correct": None},
            {"predicted_confidence": 8, "is_correct": True},
            {"is_correct": False},
        ]
    ) == {"sample_n": 0, "brier_score": None, "overconfidence_rate": None}


def test_binary_prediction_backwards_compatibility():
    assert prediction_probability(None, True) == 1.0
    assert prediction_probability(None, False) == 0.0


def test_summary_windows_use_supplied_business_clock():
    now = datetime(2026, 8, 7, 12, tzinfo=UTC)
    assert _cutoff(now, "day") == now - timedelta(days=1)
    assert _cutoff(now, "week") == now - timedelta(days=7)
    assert _cutoff(now, "all") is None


def test_retention_includes_both_metacognition_observation_tables():
    names = [name for name, *_ in _statements(datetime(2026, 8, 7, tzinfo=UTC))]
    assert "metacognition_predictions" in names
    assert "explain_backs" in names
