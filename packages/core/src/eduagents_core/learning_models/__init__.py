"""Offline/shadow learning models built from ``learning_events``.

The package deliberately has no database, model-provider, embedding, or
feature-activation dependency.  API/worker code can use the typed contracts
when a later rollout is explicitly approved, while the default application
path remains unchanged.
"""

from .elo import (
    ELO_MODEL_VERSION,
    EloConfig,
    EloSnapshot,
    EloTracker,
    prequential_elo_evaluation,
)
from .events import LearningEventRecord, load_learning_events, sort_learning_events
from .features import (
    FEATURE_MODEL_VERSION,
    EventFeatureRow,
    build_time_series_features,
    evaluate_shadow_models,
)
from .identity import (
    ITEM_IDENTITY_VERSION,
    ItemIdentity,
    identity_from_question,
)
from .kc_edges import (
    KC_EDGE_VALIDATION_VERSION,
    KcEdge,
    KcEdgeValidationConfig,
    KcEdgeValidationResult,
    validate_kc_edges,
)
from .practice import (
    FSRS_DESIRED_RETENTION,
    PRACTICE_TARGET_SUCCESS,
    CalibratedPracticeItem,
    PracticeSelectionConfig,
    PracticeSelectionResult,
    select_practice_items,
)

__all__ = [
    "CalibratedPracticeItem",
    "ELO_MODEL_VERSION",
    "FSRS_DESIRED_RETENTION",
    "FEATURE_MODEL_VERSION",
    "EloConfig",
    "EloSnapshot",
    "EloTracker",
    "EventFeatureRow",
    "ITEM_IDENTITY_VERSION",
    "ItemIdentity",
    "KC_EDGE_VALIDATION_VERSION",
    "KcEdge",
    "KcEdgeValidationConfig",
    "KcEdgeValidationResult",
    "LearningEventRecord",
    "PRACTICE_TARGET_SUCCESS",
    "PracticeSelectionConfig",
    "PracticeSelectionResult",
    "build_time_series_features",
    "evaluate_shadow_models",
    "identity_from_question",
    "load_learning_events",
    "prequential_elo_evaluation",
    "select_practice_items",
    "sort_learning_events",
    "validate_kc_edges",
]
