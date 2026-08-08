"""Prequential, low-N-shrunk Elo for reusable question items.

This is intentionally an offline/shadow implementation.  It keeps item
difficulty and learner ability in separate in-memory keyed states:
``(subject, kc_id, canonical_item_id)`` and ``(user_id, subject, kc_id)``.
There is no user-wide ability parameter.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .events import LearningEventRecord, sort_learning_events

ELO_MODEL_VERSION = "elo-v1-low-n-prior"


@dataclass(frozen=True)
class EloConfig:
    """Versioned Elo constants; prior shrinkage is applied to estimates."""

    scale: float = 400.0
    item_k: float = 24.0
    ability_k: float = 24.0
    prior_elo: float = 0.0
    prior_strength: float = 8.0
    version: str = ELO_MODEL_VERSION

    def __post_init__(self) -> None:
        if self.scale <= 0 or self.item_k <= 0 or self.ability_k <= 0:
            raise ValueError("Elo scale and K factors must be positive")
        if self.prior_strength < 0:
            raise ValueError("Elo prior strength cannot be negative")


@dataclass
class _ItemState:
    subject: str
    kc_id: str
    canonical_item_id: str
    item_family_id: str
    difficulty_elo: float = 0.0
    elo_n: int = 0
    updated_at: datetime | None = None


@dataclass
class _AbilityState:
    user_id: str
    subject: str
    kc_id: str
    ability_elo: float = 0.0
    ability_n: int = 0
    updated_at: datetime | None = None


@dataclass(frozen=True)
class EloSnapshot:
    """A serializable view containing the required raw and shrunken values."""

    subject: str
    kc_id: str
    canonical_item_id: str
    item_family_id: str
    difficulty_elo: float
    elo_n: int
    ability_elo: float
    ability_n: int
    difficulty_elo_shrunk: float
    ability_elo_shrunk: float
    version: str
    updated_at: datetime | None
    user_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "subject": self.subject,
            "kc_id": self.kc_id,
            "canonical_item_id": self.canonical_item_id,
            "item_family_id": self.item_family_id,
            "difficulty_elo": self.difficulty_elo,
            "elo_n": self.elo_n,
            "ability_elo": self.ability_elo,
            "ability_n": self.ability_n,
            "difficulty_elo_shrunk": self.difficulty_elo_shrunk,
            "ability_elo_shrunk": self.ability_elo_shrunk,
            "version": self.version,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "user_id": self.user_id,
        }


@dataclass(frozen=True)
class EloUpdate:
    event_id: str
    updated: bool
    reason: str
    expected_probability: float | None = None
    delta: float | None = None


def _shrink(value: float, n: int, *, prior: float, strength: float) -> float:
    if n < 0:
        raise ValueError("Elo observation count cannot be negative")
    denominator = n + strength
    return (n * value + strength * prior) / denominator if denominator else value


def _expected(ability: float, difficulty: float, scale: float) -> float:
    exponent = (difficulty - ability) / scale
    # Clamp only the exponent to keep malformed/very large shadow values
    # finite; it does not change normal Elo probabilities.
    exponent = min(max(exponent, -60.0), 60.0)
    return 1.0 / (1.0 + 10.0**exponent)


def _safe_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


class EloTracker:
    """In-memory, replayable Elo state keyed by subject and KC."""

    def __init__(self, config: EloConfig = EloConfig()) -> None:
        self.config = config
        self._items: dict[tuple[str, str, str], _ItemState] = {}
        self._abilities: dict[tuple[str, str, str], _AbilityState] = {}
        self._processed_events: dict[str, tuple[Any, ...]] = {}

    @staticmethod
    def _key_subject(subject: str) -> str:
        return subject.strip() or "general"

    def _ability(self, event: LearningEventRecord, kc_id: str) -> _AbilityState:
        key = (event.user_id, self._key_subject(event.subject), kc_id)
        state = self._abilities.get(key)
        if state is None:
            state = _AbilityState(user_id=key[0], subject=key[1], kc_id=key[2])
            self._abilities[key] = state
        return state

    def _item(self, event: LearningEventRecord, kc_id: str) -> _ItemState:
        assert event.canonical_item_id is not None
        key = (self._key_subject(event.subject), kc_id, event.canonical_item_id)
        state = self._items.get(key)
        if state is None:
            state = _ItemState(
                subject=key[0],
                kc_id=kc_id,
                canonical_item_id=event.canonical_item_id,
                item_family_id=event.item_family_id or event.canonical_item_id,
            )
            self._items[key] = state
        return state

    def _effective_ability(self, state: _AbilityState) -> float:
        return _shrink(
            state.ability_elo,
            state.ability_n,
            prior=self.config.prior_elo,
            strength=self.config.prior_strength,
        )

    def _effective_item(self, state: _ItemState) -> float:
        return _shrink(
            state.difficulty_elo,
            state.elo_n,
            prior=self.config.prior_elo,
            strength=self.config.prior_strength,
        )

    def predict(self, event: LearningEventRecord) -> float:
        """Predict before observing this event; no state is changed."""

        if not event.eligible_for_elo:
            return 0.5
        probabilities: list[float] = []
        for kc_id in event.kc_ids:
            ability = self._ability(event, kc_id)
            item = self._item(event, kc_id)
            probabilities.append(
                _expected(self._effective_ability(ability), self._effective_item(item), self.config.scale)
            )
        return sum(probabilities) / len(probabilities) if probabilities else 0.5

    def update(self, event: LearningEventRecord) -> EloUpdate:
        """Apply one event once in the caller-provided chronological order."""

        fingerprint = (
            event.user_id,
            event.source_type,
            event.source_id,
            event.occurred_at,
            event.correct,
            event.kc_ids,
            event.subject,
            event.canonical_item_id,
            event.item_family_id,
        )
        existing_fingerprint = self._processed_events.get(event.event_id)
        if existing_fingerprint is not None:
            if existing_fingerprint != fingerprint:
                raise ValueError(f"conflicting duplicate Elo event: {event.event_id}")
            return EloUpdate(event.event_id, False, "duplicate_event")
        self._processed_events[event.event_id] = fingerprint
        if not event.item_reusable or event.canonical_item_id is None:
            return EloUpdate(event.event_id, False, event.identity_reason)
        if not event.kc_ids:
            return EloUpdate(event.event_id, False, "missing_kc")

        expected_probability = self.predict(event)
        outcome = 1.0 if event.correct else 0.0
        kc_weight = 1.0 / len(event.kc_ids)
        deltas: list[float] = []
        for kc_id in event.kc_ids:
            ability = self._ability(event, kc_id)
            item = self._item(event, kc_id)
            expected = _expected(
                self._effective_ability(ability),
                self._effective_item(item),
                self.config.scale,
            )
            delta = self.config.ability_k * kc_weight * (outcome - expected)
            ability.ability_elo += delta
            ability.ability_n += 1
            ability.updated_at = _safe_utc(event.occurred_at)
            item.difficulty_elo -= self.config.item_k * kc_weight * (outcome - expected)
            item.elo_n += 1
            item.updated_at = _safe_utc(event.occurred_at)
            deltas.append(delta)
        return EloUpdate(
            event_id=event.event_id,
            updated=True,
            reason="updated",
            expected_probability=expected_probability,
            delta=sum(deltas) / len(deltas),
        )

    def snapshots(self) -> list[EloSnapshot]:
        """Return deterministic item/KC snapshots with low-N shrinkage."""

        snapshots: list[EloSnapshot] = []
        for key in sorted(self._items):
            item = self._items[key]
            abilities = [
                state
                for state_key, state in self._abilities.items()
                if state_key[1:] == (item.subject, item.kc_id)
            ]
            if not abilities:
                abilities = [None]
            for ability in abilities:
                snapshots.append(
                    EloSnapshot(
                        subject=item.subject,
                        kc_id=item.kc_id,
                        canonical_item_id=item.canonical_item_id,
                        item_family_id=item.item_family_id,
                        difficulty_elo=item.difficulty_elo,
                        elo_n=item.elo_n,
                        ability_elo=ability.ability_elo if ability else self.config.prior_elo,
                        ability_n=ability.ability_n if ability else 0,
                        difficulty_elo_shrunk=self._effective_item(item),
                        ability_elo_shrunk=(
                            self._effective_ability(ability)
                            if ability
                            else self.config.prior_elo
                        ),
                        version=self.config.version,
                        updated_at=item.updated_at,
                        user_id=ability.user_id if ability else None,
                    )
                )
        return snapshots


def _metric(values: list[tuple[float, bool]]) -> dict[str, float | int]:
    if not values:
        return {"n": 0, "log_loss": 0.0, "brier": 0.0}
    log_loss = 0.0
    brier = 0.0
    for probability, correct in values:
        p = min(max(probability, 1e-12), 1.0 - 1e-12)
        target = 1.0 if correct else 0.0
        log_loss -= target * math.log(p) + (1.0 - target) * math.log(1.0 - p)
        brier += (p - target) ** 2
    return {"n": len(values), "log_loss": log_loss / len(values), "brier": brier / len(values)}


def prequential_elo_evaluation(
    events: list[LearningEventRecord],
    *,
    config: EloConfig = EloConfig(),
) -> dict[str, Any]:
    """Evaluate Elo before each update and return only reproducible metrics."""

    tracker = EloTracker(config)
    observed: list[tuple[float, bool]] = []
    fixed: list[tuple[float, bool]] = []
    skipped = defaultdict(int)
    for event in sort_learning_events(events):
        prediction = tracker.predict(event)
        if event.eligible_for_elo:
            observed.append((prediction, event.correct))
            fixed.append((0.5, event.correct))
        else:
            skipped[event.identity_reason] += 1
        tracker.update(event)
    return {
        "version": config.version,
        "split": "prequential_time_order",
        "event_count": len(events),
        "eligible_event_count": len(observed),
        "skipped_by_reason": dict(sorted(skipped.items())),
        "elo": _metric(observed),
        "fixed_prior": _metric(fixed),
        "snapshots": [snapshot.as_dict() for snapshot in tracker.snapshots()],
    }
