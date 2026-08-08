"""B3/B4/B5/B7 offline and shadow contracts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from eduagents_core.learning_models import (
    CalibratedPracticeItem,
    EloTracker,
    KcEdgeValidationConfig,
    LearningEventRecord,
    PracticeSelectionConfig,
    build_time_series_features,
    evaluate_shadow_models,
    identity_from_question,
    select_practice_items,
    validate_kc_edges,
)

T0 = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)


def _event(
    index: int,
    *,
    user: str = "u1",
    kc_ids: tuple[str, ...] = ("kc",),
    correct: bool = True,
    canonical: str | None = "item-1",
    reusable: bool = True,
    subject: str = "math",
    difficulty: str = "mid",
) -> LearningEventRecord:
    return LearningEventRecord(
        event_id=f"e{index}",
        user_id=user,
        source_type="quiz_attempt",
        source_id=f"a{index}",
        question_id=f"q{index}",
        occurred_at=T0 + timedelta(minutes=index),
        correct=correct,
        kc_ids=kc_ids,
        subject=subject,
        difficulty_label=difficulty,
        canonical_item_id=canonical,
        item_family_id=canonical if canonical else None,
        item_reusable=reusable,
        identity_reason="test",
    )


def test_identity_is_stable_and_one_off_generated_question_is_ineligible():
    manual = {
        "id": "q-1",
        "source_type": "manual",
        "source_meta_json": {"canonical_item_id": "algebra-1"},
    }
    assert identity_from_question(manual) == identity_from_question(dict(manual))
    generated = {"id": "q-2", "source_type": "generated"}
    identity = identity_from_question(generated)
    assert identity.canonical_item_id is None
    assert not identity.eligible_for_elo


def test_elo_uses_low_n_prior_and_does_not_create_global_ability():
    tracker = EloTracker()
    event = _event(0)
    assert tracker.predict(event) == 0.5
    update = tracker.update(event)
    assert update.updated
    snapshot = tracker.snapshots()[0]
    assert snapshot.elo_n == 1
    assert snapshot.ability_n == 1
    assert snapshot.difficulty_elo_shrunk != snapshot.difficulty_elo
    assert snapshot.subject == "math"
    assert snapshot.kc_id == "kc"
    assert snapshot.user_id == "u1"
    assert tracker.update(event).reason == "duplicate_event"
    assert tracker.update(_event(1, canonical=None, reusable=False)).updated is False


def test_features_are_prior_only_and_multi_kc_credit_is_explicit():
    rows = build_time_series_features(
        [_event(0, kc_ids=("a", "b")), _event(1, kc_ids=("a", "b"), correct=False)]
    )
    first = [row for row in rows if row.event_id == "e0"]
    second = [row for row in rows if row.event_id == "e1"]
    assert {row.kc_credit for row in first} == {0.5}
    assert all(row.opportunities_before == 0 for row in first)
    assert all(row.opportunities_before == 0.5 for row in second)
    assert all(row.correct is True for row in first)
    assert all(row.correct is False for row in second)
    result = evaluate_shadow_models([_event(0), _event(1, correct=False)])
    assert result["mode"] == "shadow"
    assert set(result["models"]) == {"afm", "bkt", "elo", "pfa", "simple_logistic"}
    assert result["split"] == "prequential_time_order"


def test_practice_target_is_separate_and_requires_calibrated_pool():
    items = [
        CalibratedPracticeItem("i-1", "f-1", 0.74, 10, metadata={"label": "near"}),
        CalibratedPracticeItem("i-2", "f-2", 0.91, 10),
    ]
    disabled = select_practice_items(
        items,
        requested_count=1,
        config=PracticeSelectionConfig(enabled=False),
    )
    assert not disabled.used_target_success
    enabled = select_practice_items(
        items,
        requested_count=1,
        config=PracticeSelectionConfig(enabled=True, fsrs_desired_retention=0.9),
    )
    assert enabled.used_target_success
    assert enabled.items[0].canonical_item_id == "i-1"
    assert enabled.practice_target_success == 0.75
    assert enabled.fsrs_desired_retention == 0.9
    no_pool = select_practice_items(
        [CalibratedPracticeItem(None, None, 0.75, 10, calibrated=False)],
        requested_count=1,
        config=PracticeSelectionConfig(enabled=True),
    )
    assert no_pool.fallback_reason == "no_calibrated_item_pool"
    optional_pool = select_practice_items(
        [CalibratedPracticeItem(None, None, 0.75, 10, calibrated=False)],
        requested_count=1,
        config=PracticeSelectionConfig(enabled=True, require_calibrated_pool=False),
    )
    assert optional_pool.used_target_success is False
    assert optional_pool.fallback_reason == "no_calibrated_item_pool"


def test_kc_edge_validation_matches_non_edge_and_applies_fdr_without_mutation():
    events: list[LearningEventRecord] = []
    for index in range(5):
        events.append(_event(index * 3, user=f"treated-{index}", kc_ids=("source",), correct=True))
        events.append(
            _event(
                index * 3 + 1,
                user=f"treated-{index}",
                kc_ids=("target",),
                correct=True,
                canonical=f"target-treated-{index}",
            )
        )
        events.append(
            _event(
                index * 3 + 2,
                user=f"control-{index}",
                kc_ids=("target",),
                correct=False,
                canonical=f"target-control-{index}",
            )
        )
    report = validate_kc_edges(
        events,
        [{"from_kc": "source", "to_kc": "target"}],
        config=KcEdgeValidationConfig(min_pairs=5, min_group_observations=5),
    )
    assert report["mutated_kc_edges"] is False
    result = report["results"][0]
    assert result["status"] == "supported"
    assert result["matched_pairs"] == 5
    assert result["q_value"] is not None


def test_kc_edge_prerequisite_history_is_scoped_to_subject():
    events = [
        _event(0, user="same-user", subject="math", kc_ids=("source",)),
        _event(1, user="same-user", subject="physics", kc_ids=("target",)),
        _event(2, user="control", subject="physics", kc_ids=("target",), correct=False),
    ]
    report = validate_kc_edges(
        events,
        [{"from_kc": "source", "to_kc": "target"}],
        config=KcEdgeValidationConfig(min_pairs=1, min_group_observations=1),
    )
    result = report["results"][0]
    assert result["exposed_count"] == 0
    assert result["control_count"] == 2
    assert result["matched_pairs"] == 0
