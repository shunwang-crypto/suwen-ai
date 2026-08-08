"""Offline KC prerequisite-edge validation with matched non-edge controls.

The validator only consumes time-ordered learning events and candidate edges.
It controls matching on target-KC opportunity count, a prior user/KC ability
band, and item difficulty label.  Benjamini-Hochberg FDR is applied across
candidate edges.  The result is an immutable report; it never inserts,
deletes, or updates ``kc_edges``.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .events import LearningEventRecord, sort_learning_events

KC_EDGE_VALIDATION_VERSION = "kc-edge-validation-v1"


@dataclass(frozen=True, order=True)
class KcEdge:
    from_kc: str
    to_kc: str
    type: str = "prerequisite"

    def as_dict(self) -> dict[str, str]:
        return {"from_kc": self.from_kc, "to_kc": self.to_kc, "type": self.type}


@dataclass(frozen=True)
class KcEdgeValidationConfig:
    min_pairs: int = 5
    min_group_observations: int = 5
    fdr_alpha: float = 0.05
    min_effect: float = 0.05
    match_ratio_threshold: float = 0.5
    ability_prior_strength: float = 4.0
    ability_band_width: float = 0.2
    opportunity_band_cap: int = 5
    version: str = KC_EDGE_VALIDATION_VERSION

    def __post_init__(self) -> None:
        if self.min_pairs < 1 or self.min_group_observations < 1:
            raise ValueError("KC edge minimum observations must be positive")
        if not 0.0 < self.fdr_alpha < 1.0:
            raise ValueError("fdr_alpha must be between 0 and 1")
        if self.min_effect < 0.0 or self.match_ratio_threshold <= 0.0:
            raise ValueError("KC edge effect and match thresholds are invalid")
        if self.ability_prior_strength < 0.0 or self.ability_band_width <= 0.0:
            raise ValueError("KC edge ability matching settings are invalid")


@dataclass(frozen=True)
class KcEdgeValidationResult:
    edge: KcEdge
    status: str
    exposed_count: int
    control_count: int
    matched_pairs: int
    exposed_success_rate: float | None
    control_success_rate: float | None
    effect: float | None
    p_value: float | None
    q_value: float | None
    matched_ratio: float | None
    temporal_effects: tuple[float, ...]
    reason: str
    version: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "edge": self.edge.as_dict(),
            "status": self.status,
            "exposed_count": self.exposed_count,
            "control_count": self.control_count,
            "matched_pairs": self.matched_pairs,
            "exposed_success_rate": self.exposed_success_rate,
            "control_success_rate": self.control_success_rate,
            "effect": self.effect,
            "p_value": self.p_value,
            "q_value": self.q_value,
            "matched_ratio": self.matched_ratio,
            "temporal_effects": list(self.temporal_effects),
            "reason": self.reason,
            "version": self.version,
        }


@dataclass(frozen=True)
class _Observation:
    event_id: str
    occurred_at: datetime
    exposed: bool
    correct: bool
    stratum: tuple[str, str, str, int, int]


def _coerce_edge(edge: KcEdge | dict[str, Any] | tuple[str, str]) -> KcEdge:
    if isinstance(edge, KcEdge):
        return edge
    if isinstance(edge, dict):
        return KcEdge(str(edge["from_kc"]), str(edge["to_kc"]), str(edge.get("type", "prerequisite")))
    return KcEdge(str(edge[0]), str(edge[1]))


def _rate(successes: int, count: int) -> float | None:
    return successes / count if count else None


def _normal_two_proportion_p_value(
    exposed_successes: int,
    exposed_count: int,
    control_successes: int,
    control_count: int,
) -> float | None:
    if not exposed_count or not control_count:
        return None
    exposed_rate = exposed_successes / exposed_count
    control_rate = control_successes / control_count
    pooled = (exposed_successes + control_successes) / (exposed_count + control_count)
    variance = pooled * (1.0 - pooled) * (1.0 / exposed_count + 1.0 / control_count)
    if variance <= 0.0:
        return 0.0 if exposed_rate != control_rate else 1.0
    z = (exposed_rate - control_rate) / math.sqrt(variance)
    return math.erfc(abs(z) / math.sqrt(2.0))


def _fdr_q_values(p_values: dict[KcEdge, float]) -> dict[KcEdge, float]:
    ordered = sorted(p_values.items(), key=lambda item: (item[1], item[0]))
    total = len(ordered)
    result: dict[KcEdge, float] = {}
    running = 1.0
    for index in range(total, 0, -1):
        edge, p_value = ordered[index - 1]
        running = min(running, p_value * total / index)
        result[edge] = min(max(running, 0.0), 1.0)
    return result


def _ability_band(successes: float, opportunities: float, config: KcEdgeValidationConfig) -> int:
    probability = (
        (successes + 0.5 * config.ability_prior_strength)
        / (opportunities + config.ability_prior_strength)
        if opportunities + config.ability_prior_strength
        else 0.5
    )
    return int(math.floor(probability / config.ability_band_width))


def _temporal_effects(pairs: list[tuple[_Observation, _Observation]]) -> tuple[float, ...]:
    if len(pairs) < 2:
        return ()
    ordered = sorted(pairs, key=lambda pair: (pair[0].occurred_at, pair[0].event_id))
    chunks = (ordered[: len(ordered) // 2], ordered[len(ordered) // 2 :])
    effects: list[float] = []
    for chunk in chunks:
        if not chunk:
            continue
        treated = sum(pair[0].correct for pair in chunk)
        control = sum(pair[1].correct for pair in chunk)
        effects.append(treated / len(chunk) - control / len(chunk))
    return tuple(effects)


def _is_unstable(effects: tuple[float, ...]) -> bool:
    if len(effects) < 2:
        return False
    return effects[0] * effects[1] < 0 and abs(effects[0] - effects[1]) > 0.05


def validate_kc_edges(
    events: Iterable[LearningEventRecord],
    edges: Iterable[KcEdge | dict[str, Any] | tuple[str, str]],
    *,
    config: KcEdgeValidationConfig = KcEdgeValidationConfig(),
) -> dict[str, Any]:
    """Return versioned edge evidence with statuses and BH-FDR q-values."""

    ordered_events = sort_learning_events(list(events))
    candidates = sorted({_coerce_edge(edge) for edge in edges})
    observations: dict[KcEdge, list[_Observation]] = defaultdict(list)
    # Subject is part of the model stratum.  Keep it in the prerequisite
    # history key as well, otherwise a source-KC event in one subject exposes
    # a same-named target-KC event in another subject.
    history: dict[tuple[str, str, str], list[tuple[datetime, bool]]] = defaultdict(list)
    target_counts: dict[tuple[str, str, str], tuple[float, float]] = defaultdict(lambda: (0.0, 0.0))

    for event in ordered_events:
        event_kcs = set(event.kc_ids)
        for edge in candidates:
            if edge.to_kc not in event_kcs or edge.from_kc == edge.to_kc:
                continue
            history_source = history.get((event.user_id, event.subject, edge.from_kc), [])
            exposed = any(at < event.occurred_at for at, _correct in history_source)
            opportunities, successes = target_counts[(event.user_id, event.subject, edge.to_kc)]
            ability_band = _ability_band(successes, opportunities, config)
            difficulty = event.difficulty_label or "unknown"
            stratum = (
                event.subject,
                edge.to_kc,
                difficulty,
                ability_band,
                min(int(opportunities), config.opportunity_band_cap),
            )
            observations[edge].append(
                _Observation(
                    event_id=event.event_id,
                    occurred_at=event.occurred_at,
                    exposed=exposed,
                    correct=event.correct,
                    stratum=stratum,
                )
            )
        for kc_id in event_kcs:
            key = (event.user_id, event.subject, kc_id)
            opportunities, successes = target_counts[key]
            target_counts[key] = (
                opportunities + 1.0,
                successes + (1.0 if event.correct else 0.0),
            )
            history[(event.user_id, event.subject, kc_id)].append(
                (event.occurred_at, event.correct)
            )

    interim: dict[KcEdge, dict[str, Any]] = {}
    p_values: dict[KcEdge, float] = {}
    for edge in candidates:
        edge_observations = observations.get(edge, [])
        exposed = [observation for observation in edge_observations if observation.exposed]
        controls = [observation for observation in edge_observations if not observation.exposed]
        controls_by_stratum: dict[tuple[str, str, str, int, int], list[_Observation]] = defaultdict(list)
        for control in controls:
            controls_by_stratum[control.stratum].append(control)
        used_controls: set[str] = set()
        pairs: list[tuple[_Observation, _Observation]] = []
        for treatment in sorted(exposed, key=lambda item: (item.occurred_at, item.event_id)):
            choices = [
                control
                for control in controls_by_stratum[treatment.stratum]
                if control.event_id not in used_controls
            ]
            if not choices:
                continue
            control = min(
                choices,
                key=lambda item: (
                    abs((item.occurred_at - treatment.occurred_at).total_seconds()),
                    item.event_id,
                ),
            )
            used_controls.add(control.event_id)
            pairs.append((treatment, control))

        exposed_successes = sum(item.correct for item in exposed)
        control_successes = sum(item.correct for item in controls)
        exposed_count = len(exposed)
        control_count = len(controls)
        pair_count = len(pairs)
        effects = _temporal_effects(pairs)
        effect = (
            _rate(exposed_successes, exposed_count) - _rate(control_successes, control_count)
            if exposed_count and control_count
            else None
        )
        p_value = (
            _normal_two_proportion_p_value(
                exposed_successes,
                exposed_count,
                control_successes,
                control_count,
            )
            if exposed_count >= config.min_group_observations
            and control_count >= config.min_group_observations
            and pair_count >= config.min_pairs
            else None
        )
        if p_value is not None:
            p_values[edge] = p_value
        interim[edge] = {
            "exposed": exposed,
            "controls": controls,
            "pairs": pairs,
            "exposed_successes": exposed_successes,
            "control_successes": control_successes,
            "effect": effect,
            "p_value": p_value,
            "effects": effects,
        }

    q_values = _fdr_q_values(p_values)
    results: list[KcEdgeValidationResult] = []
    for edge in candidates:
        data = interim[edge]
        exposed = data["exposed"]
        controls = data["controls"]
        pairs = data["pairs"]
        exposed_count = len(exposed)
        control_count = len(controls)
        pair_count = len(pairs)
        effect = data["effect"]
        p_value = data["p_value"]
        q_value = q_values.get(edge)
        ratio = pair_count / exposed_count if exposed_count else None
        effects = data["effects"]
        if pair_count < config.min_pairs or exposed_count < config.min_group_observations or control_count < config.min_group_observations:
            status = "insufficient"
            reason = "not_enough_matched_observations"
        elif ratio is not None and ratio < config.match_ratio_threshold:
            status = "unstable"
            reason = "low_match_ratio"
        elif _is_unstable(effects):
            status = "unstable"
            reason = "temporal_effect_sign_changed"
        elif q_value is not None and effect is not None and q_value <= config.fdr_alpha and effect >= config.min_effect:
            status = "supported"
            reason = "positive_effect_passed_fdr"
        elif q_value is not None and effect is not None and q_value <= config.fdr_alpha and effect <= -config.min_effect:
            status = "contradicted"
            reason = "negative_effect_passed_fdr"
        else:
            status = "insufficient"
            reason = "effect_not_supported_after_fdr"
        results.append(
            KcEdgeValidationResult(
                edge=edge,
                status=status,
                exposed_count=exposed_count,
                control_count=control_count,
                matched_pairs=pair_count,
                exposed_success_rate=_rate(data["exposed_successes"], exposed_count),
                control_success_rate=_rate(data["control_successes"], control_count),
                effect=effect,
                p_value=p_value,
                q_value=q_value,
                matched_ratio=ratio,
                temporal_effects=effects,
                reason=reason,
                version=config.version,
            )
        )
    return {
        "version": config.version,
        "mode": "offline_only",
        "fdr_method": "benjamini_hochberg",
        "fdr_alpha": config.fdr_alpha,
        "control_design": "matched_non_edge_target_opportunities",
        "matching_controls": [
            "target_kc_opportunity_band",
            "prior_user_kc_ability_band",
            "item_difficulty_label",
        ],
        "candidate_edge_count": len(candidates),
        "tested_edge_count": len(p_values),
        "mutated_kc_edges": False,
        "results": [result.as_dict() for result in results],
    }
