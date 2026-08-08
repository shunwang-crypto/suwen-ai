"""B2/B6 pure contracts: confidence snapshots, due ordering and rubric gate."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from eduagents_api.metacognition import confidence_snapshot
from eduagents_api.routers.reviews import filter_due
from eduagents_core.metacognition_quality import (
    EXPLAIN_BACK_DIMENSIONS,
    build_offline_eval_record,
    human_agreement,
    numeric_display_allowed,
    validate_score_json,
)

NOW = datetime(2026, 8, 8, 12, tzinfo=UTC)


def _row(row_id: str, due: datetime, confidence: float | None):
    return SimpleNamespace(
        id=row_id,
        fsrs_card_json={"due": due.isoformat()},
        created_at=due,
        confidence_probability_at_error=confidence,
    )


def test_quiz_confidence_snapshot_is_versioned_and_other_sources_stay_null():
    score, probability, version = confidence_snapshot(5)
    assert (score, probability, version) == (5, 0.9, "quiz-confidence-5-v1")
    assert confidence_snapshot(None, None) == (None, None, None)
    assert confidence_snapshot(None, False) == (None, 0.0, "quiz-predicted-correct-v1")


def test_overcorrection_never_moves_a_later_due_card_ahead():
    early = _row("early", NOW, 0.1)
    later_high_confidence = _row("later", NOW + timedelta(hours=1), 0.99)
    default = filter_due([later_high_confidence, early], NOW, overcorrection_enabled=False)
    ranked = filter_due([later_high_confidence, early], NOW, overcorrection_enabled=True)
    assert [row.id for row in default] == ["early"]
    assert [row.id for row in ranked] == ["early"]


def test_quality_score_requires_all_dimensions_and_human_gate():
    valid = {dimension: 0.8 for dimension in EXPLAIN_BACK_DIMENSIONS}
    assert validate_score_json({"score": valid}) == valid
    assert validate_score_json({"score": {"correctness": 0.8}}) is None
    record = build_offline_eval_record(
        record_id="redacted-1",
        model_score=valid,
        annotator_a=valid,
        annotator_b=valid,
    )
    agreement = human_agreement([record])
    assert agreement["sample_n"] == 1
    assert agreement["display_ready"] is False
    assert numeric_display_allowed(operator_enabled=True, agreement=agreement) is False


def test_numeric_display_gate_rejects_non_finite_agreement():
    assert numeric_display_allowed(
        operator_enabled=True,
        agreement={
            "sample_n": 100,
            "within_tolerance_agreement": float("inf"),
            "display_ready": True,
        },
    ) is False
