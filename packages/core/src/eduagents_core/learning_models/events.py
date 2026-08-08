"""A privacy-conscious, JSON-friendly view of ``learning_events`` rows."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .identity import ItemIdentity, identity_from_question


def _value(row: Mapping[str, Any] | Any, *names: str, default: Any = None) -> Any:
    for name in names:
        if isinstance(row, Mapping):
            value = row.get(name)
        else:
            value = getattr(row, name, None)
        if value is not None:
            return value
    return default


def _utc(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text) if text else datetime.min.replace(tzinfo=UTC)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _unique_strings(values: Any) -> tuple[str, ...]:
    if not isinstance(values, (list, tuple, set)):
        return ()
    return tuple(dict.fromkeys(str(value) for value in values if str(value).strip()))


@dataclass(frozen=True)
class LearningEventRecord:
    """Stable event contract used by all offline/shadow evaluators."""

    event_id: str
    user_id: str
    source_type: str
    source_id: str
    question_id: str | None
    occurred_at: datetime
    correct: bool
    kc_ids: tuple[str, ...]
    subject: str
    difficulty_label: str | None
    canonical_item_id: str | None
    item_family_id: str | None
    item_reusable: bool
    identity_reason: str

    @property
    def eligible_for_elo(self) -> bool:
        return self.item_reusable and self.canonical_item_id is not None and bool(self.kc_ids)

    def as_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "user_id": self.user_id,
            "source_type": self.source_type,
            "source_id": self.source_id,
            "question_id": self.question_id,
            "occurred_at": self.occurred_at.isoformat(),
            "correct": self.correct,
            "kc_ids": list(self.kc_ids),
            "subject": self.subject,
            "difficulty_label": self.difficulty_label,
            "canonical_item_id": self.canonical_item_id,
            "item_family_id": self.item_family_id,
            "item_reusable": self.item_reusable,
            "identity_reason": self.identity_reason,
        }


def load_learning_events(
    rows: Iterable[Mapping[str, Any] | Any],
    *,
    questions: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[LearningEventRecord]:
    """Convert ORM rows or redacted JSON rows into deterministic event records.

    A question catalog is optional.  When present, its source metadata supplies
    the stable item identity and subject/KC defaults; no question body is read
    or copied into the returned records.
    """

    question_catalog = questions or {}
    result: list[LearningEventRecord] = []
    for row in rows:
        question_id_raw = _value(row, "question_id")
        question_id = str(question_id_raw) if question_id_raw is not None else None
        question = question_catalog.get(question_id or "", {})
        if not isinstance(question, Mapping):
            question = {}

        identity: ItemIdentity
        explicit_identity = {
            key: _value(row, key)
            for key in ("canonical_item_id", "item_family_id", "item_reusable")
            if _value(row, key) is not None
        }
        identity = identity_from_question({**question, **explicit_identity})
        event_id = str(_value(row, "id", "event_id", "source_id", default=""))
        source_id = str(_value(row, "source_id", default=event_id))
        kc_values = _value(row, "kc_ids_json", "kc_ids")
        if not kc_values:
            kc_values = question.get("kc_ids", [])
        subject = str(_value(row, "subject", default=question.get("subject", "general")) or "general")
        difficulty = _value(row, "difficulty_label", "difficulty", default=question.get("difficulty"))
        correct = _value(row, "correct", "is_correct")
        if not isinstance(correct, bool):
            raise ValueError(f"learning event {event_id or '<unknown>'} has non-boolean correct")
        result.append(
            LearningEventRecord(
                event_id=event_id,
                user_id=str(_value(row, "user_id", default="")),
                source_type=str(_value(row, "source_type", default="")),
                source_id=source_id,
                question_id=question_id,
                occurred_at=_utc(_value(row, "occurred_at", "at")),
                correct=correct,
                kc_ids=_unique_strings(kc_values),
                subject=subject.strip() or "general",
                difficulty_label=str(difficulty) if difficulty is not None else None,
                canonical_item_id=identity.canonical_item_id,
                item_family_id=identity.item_family_id,
                item_reusable=identity.reusable,
                identity_reason=identity.reason,
            )
        )
    return result


def sort_learning_events(events: Iterable[LearningEventRecord]) -> list[LearningEventRecord]:
    """Sort by business event time with a stable ID tie-breaker."""

    return sorted(events, key=lambda event: (event.occurred_at, event.event_id, event.source_id))
