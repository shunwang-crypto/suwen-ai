"""Time-ordered AFM/PFA features and shadow-only model comparison."""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from eduagents_core.mastery.bkt import bkt_update

from .elo import EloTracker, _metric
from .events import LearningEventRecord, sort_learning_events

FEATURE_MODEL_VERSION = "afm-pfa-v1-prequential"
_NO_KC = "__no_kc__"


@dataclass(frozen=True)
class EventFeatureRow:
    """One event/KC row.  Counts are credit-weighted for multi-KC events."""

    event_id: str
    user_id: str
    subject: str
    kc_id: str
    kc_credit: float
    occurred_at: datetime
    correct: bool
    difficulty_label: str | None
    canonical_item_id: str | None
    opportunities_before: float
    successes_before: float
    failures_before: float
    time_since_last_days: float | None

    @property
    def prior_success_rate(self) -> float:
        # The fixed 1/2 pseudo-count prevents low-N divisions from becoming
        # a raw proportion-correct feature.
        return (self.successes_before + 0.5) / (self.opportunities_before + 1.0)

    def as_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "user_id": self.user_id,
            "subject": self.subject,
            "kc_id": self.kc_id,
            "kc_credit": self.kc_credit,
            "occurred_at": self.occurred_at.isoformat(),
            "correct": self.correct,
            "difficulty_label": self.difficulty_label,
            "canonical_item_id": self.canonical_item_id,
            "opportunities_before": self.opportunities_before,
            "successes_before": self.successes_before,
            "failures_before": self.failures_before,
            "time_since_last_days": self.time_since_last_days,
            "prior_success_rate": self.prior_success_rate,
        }


@dataclass(frozen=True)
class _EventAggregate:
    event: LearningEventRecord
    rows: tuple[EventFeatureRow, ...]

    def mean(self, attr: str, default: float = 0.0) -> float:
        if not self.rows:
            return default
        return sum(float(getattr(row, attr) or 0.0) * row.kc_credit for row in self.rows)


class _OnlineLogistic:
    """Small deterministic online logistic learner for shadow comparison."""

    def __init__(self, dimensions: int, *, prior_probability: float = 0.3, rate: float = 0.05) -> None:
        self.coefficients = [0.0] * dimensions
        p = min(max(prior_probability, 1e-6), 1.0 - 1e-6)
        self.coefficients[0] = math.log(p / (1.0 - p))
        self.rate = rate

    def predict(self, values: list[float]) -> float:
        score = sum(
            coefficient * value
            for coefficient, value in zip(self.coefficients, values, strict=True)
        )
        score = min(max(score, -60.0), 60.0)
        return 1.0 / (1.0 + math.exp(-score))

    def update(self, values: list[float], target: bool) -> None:
        prediction = self.predict(values)
        error = (1.0 if target else 0.0) - prediction
        for index, value in enumerate(values):
            self.coefficients[index] += self.rate * error * value


def _aggregate(rows: list[EventFeatureRow], event: LearningEventRecord) -> _EventAggregate:
    return _EventAggregate(event=event, rows=tuple(rows))


def build_time_series_features(events: list[LearningEventRecord]) -> list[EventFeatureRow]:
    """Build features strictly from events earlier than the current event.

    For an event tagged with N KC IDs, each KC receives ``1/N`` opportunity,
    success, and failure credit.  The target remains the single observed
    event outcome; no row reads later events or the current outcome while
    constructing a feature.
    """

    opportunities: dict[tuple[str, str], float] = defaultdict(float)
    successes: dict[tuple[str, str], float] = defaultdict(float)
    failures: dict[tuple[str, str], float] = defaultdict(float)
    last_seen: dict[tuple[str, str], datetime] = {}
    result: list[EventFeatureRow] = []
    for event in sort_learning_events(events):
        kc_ids = event.kc_ids or (_NO_KC,)
        credit = 1.0 / len(kc_ids)
        for kc_id in kc_ids:
            key = (event.user_id, kc_id)
            previous = last_seen.get(key)
            elapsed_days = (
                max((event.occurred_at - previous).total_seconds(), 0.0) / 86400.0
                if previous is not None
                else None
            )
            result.append(
                EventFeatureRow(
                    event_id=event.event_id,
                    user_id=event.user_id,
                    subject=event.subject,
                    kc_id=kc_id,
                    kc_credit=credit,
                    occurred_at=event.occurred_at,
                    correct=event.correct,
                    difficulty_label=event.difficulty_label,
                    canonical_item_id=event.canonical_item_id,
                    opportunities_before=opportunities[key],
                    successes_before=successes[key],
                    failures_before=failures[key],
                    time_since_last_days=elapsed_days,
                )
            )
        for kc_id in kc_ids:
            key = (event.user_id, kc_id)
            opportunities[key] += credit
            if event.correct:
                successes[key] += credit
            else:
                failures[key] += credit
            last_seen[key] = event.occurred_at
    return result


def _group_aggregates(events: list[LearningEventRecord], rows: list[EventFeatureRow]) -> list[_EventAggregate]:
    rows_by_event: dict[str, list[EventFeatureRow]] = defaultdict(list)
    for row in rows:
        rows_by_event[row.event_id].append(row)
    return [
        _aggregate(rows_by_event.get(event.event_id, []), event)
        for event in sort_learning_events(events)
    ]


def _prediction_metrics(predictions: list[tuple[float, bool]]) -> dict[str, float | int]:
    return _metric(predictions)


def evaluate_shadow_models(
    events: list[LearningEventRecord],
    *,
    include_feature_rows: bool = False,
) -> dict[str, Any]:
    """Compare BKT, a simple logistic baseline, AFM, PFA, and Elo.

    Every prediction is emitted before that event updates its model.  The
    result is explicitly marked ``shadow`` and contains no activation or
    persistence operation.
    """

    rows = build_time_series_features(events)
    aggregates = _group_aggregates(events, rows)
    bkt_state: dict[tuple[str, str], float] = {}
    simple_logistic = _OnlineLogistic(2)
    afm = _OnlineLogistic(3)
    pfa = _OnlineLogistic(3)
    elo = EloTracker()
    predictions: dict[str, list[tuple[float, bool]]] = defaultdict(list)
    elo_skipped: dict[str, int] = defaultdict(int)

    for aggregate in aggregates:
        event = aggregate.event
        prior_rate = aggregate.mean("prior_success_rate", 0.3)
        opportunity = aggregate.mean("opportunities_before")
        success = aggregate.mean("successes_before")
        failure = aggregate.mean("failures_before")
        elapsed = aggregate.mean("time_since_last_days")
        simple_values = [1.0, prior_rate]
        afm_values = [1.0, math.log1p(opportunity), math.log1p(elapsed)]
        pfa_values = [1.0, math.log1p(success), math.log1p(failure)]

        bkt_probability = aggregate.mean(
            "prior_success_rate", 0.3
        )  # overwritten below when KC state exists
        if aggregate.rows:
            bkt_probability = 0.0
            for row in aggregate.rows:
                bkt_probability += bkt_state.get((event.user_id, row.kc_id), 0.3) * row.kc_credit

        predictions["bkt"].append((bkt_probability, event.correct))
        predictions["simple_logistic"].append((simple_logistic.predict(simple_values), event.correct))
        predictions["afm"].append((afm.predict(afm_values), event.correct))
        predictions["pfa"].append((pfa.predict(pfa_values), event.correct))
        if event.eligible_for_elo:
            predictions["elo"].append((elo.predict(event), event.correct))
        else:
            elo_skipped[event.identity_reason] += 1

        for row in aggregate.rows:
            key = (event.user_id, row.kc_id)
            bkt_state[key] = bkt_update(
                bkt_state.get(key, 0.3),
                event.correct,
                weight=row.kc_credit,
            )
        simple_logistic.update(simple_values, event.correct)
        afm.update(afm_values, event.correct)
        pfa.update(pfa_values, event.correct)
        elo.update(event)

    result: dict[str, Any] = {
        "version": FEATURE_MODEL_VERSION,
        "mode": "shadow",
        "split": "prequential_time_order",
        "event_count": len(events),
        "multi_kc_credit_assignment": "equal_event_credit",
        "models": {name: _prediction_metrics(values) for name, values in sorted(predictions.items())},
        "elo_skipped_by_reason": dict(sorted(elo_skipped.items())),
    }
    if include_feature_rows:
        result["feature_rows"] = [row.as_dict() for row in rows]
    return result
