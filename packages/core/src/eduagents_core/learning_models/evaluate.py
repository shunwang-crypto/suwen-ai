"""Orchestration for the JSON-only learning-model evaluation CLI."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from typing import Any

from .elo import prequential_elo_evaluation
from .events import load_learning_events
from .features import evaluate_shadow_models
from .identity import ITEM_IDENTITY_VERSION
from .kc_edges import KcEdgeValidationConfig, validate_kc_edges
from .practice import CalibratedPracticeItem, PracticeSelectionConfig, select_practice_items


def _question_catalog(value: Any) -> dict[str, Mapping[str, Any]]:
    if isinstance(value, Mapping):
        return {
            str(key): item
            for key, item in value.items()
            if isinstance(item, Mapping)
        }
    if isinstance(value, list):
        return {
            str(item.get("id", item.get("question_id"))): item
            for item in value
            if isinstance(item, Mapping) and item.get("id", item.get("question_id")) is not None
        }
    return {}


def _practice_items(value: Any) -> list[CalibratedPracticeItem]:
    if not isinstance(value, list):
        return []
    result: list[CalibratedPracticeItem] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        result.append(
            CalibratedPracticeItem(
                canonical_item_id=(
                    str(item["canonical_item_id"]) if item.get("canonical_item_id") else None
                ),
                item_family_id=str(item["item_family_id"]) if item.get("item_family_id") else None,
                estimated_success=float(item.get("estimated_success", 0.5)),
                calibration_observations=int(item.get("calibration_observations", 0)),
                calibrated=bool(item.get("calibrated", False)),
                metadata=dict(item.get("metadata", {})) if isinstance(item.get("metadata"), Mapping) else {},
            )
        )
    return result


def run_offline_evaluation(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Evaluate all requested components without external services or writes."""

    events = load_learning_events(
        payload.get("events", []),
        questions=_question_catalog(payload.get("questions", {})),
    )
    identity_reasons = Counter(event.identity_reason for event in events)
    elo = prequential_elo_evaluation(events)
    shadow = evaluate_shadow_models(events)

    practice_config_data = payload.get("practice_config", {})
    if not isinstance(practice_config_data, Mapping):
        practice_config_data = {}
    practice_config = PracticeSelectionConfig(
        practice_target_success=float(
            practice_config_data.get("practice_target_success", 0.75)
        ),
        fsrs_desired_retention=float(
            practice_config_data.get("fsrs_desired_retention", 0.90)
        ),
        enabled=bool(practice_config_data.get("enabled", False)),
        require_calibrated_pool=bool(practice_config_data.get("require_calibrated_pool", True)),
        min_calibration_observations=int(
            practice_config_data.get("min_calibration_observations", 1)
        ),
    )
    practice = select_practice_items(
        _practice_items(payload.get("calibrated_items", [])),
        requested_count=max(int(payload.get("practice_count", 0)), 0),
        config=practice_config,
    )

    edge_config_data = payload.get("kc_edge_config", {})
    if not isinstance(edge_config_data, Mapping):
        edge_config_data = {}
    edge_config = KcEdgeValidationConfig(
        min_pairs=int(edge_config_data.get("min_pairs", 5)),
        min_group_observations=int(edge_config_data.get("min_group_observations", 5)),
        fdr_alpha=float(edge_config_data.get("fdr_alpha", 0.05)),
        min_effect=float(edge_config_data.get("min_effect", 0.05)),
    )
    edge_result = validate_kc_edges(events, payload.get("kc_edges", []), config=edge_config)
    return {
        "schema_version": "learning-models-evaluation-v1",
        "mode": "offline_shadow",
        "identity_version": ITEM_IDENTITY_VERSION,
        "event_count": len(events),
        "eligible_item_event_count": sum(event.eligible_for_elo for event in events),
        "identity_reasons": dict(sorted(identity_reasons.items())),
        "elo": elo,
        "afm_pfa": shadow,
        "practice": practice.as_dict(),
        "kc_edge_validation": edge_result,
        "external_calls": {
            "database": False,
            "embedding": False,
            "llm": False,
            "paid_service": False,
        },
    }
