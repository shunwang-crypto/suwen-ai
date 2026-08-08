"""Target-success practice selection, kept separate from FSRS retention."""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

PRACTICE_TARGET_SUCCESS = 0.75
FSRS_DESIRED_RETENTION = 0.90


@dataclass(frozen=True)
class PracticeSelectionConfig:
    """B5 configuration; activation is opt-in and disabled by default."""

    practice_target_success: float = PRACTICE_TARGET_SUCCESS
    fsrs_desired_retention: float = FSRS_DESIRED_RETENTION
    enabled: bool = False
    require_calibrated_pool: bool = True
    min_calibration_observations: int = 1
    version: str = "practice-target-v1"

    def __post_init__(self) -> None:
        if not 0.0 < self.practice_target_success < 1.0:
            raise ValueError("practice_target_success must be between 0 and 1")
        if not 0.0 < self.fsrs_desired_retention <= 1.0:
            raise ValueError("fsrs_desired_retention must be between 0 and 1")
        if self.min_calibration_observations < 0:
            raise ValueError("min_calibration_observations cannot be negative")


@dataclass(frozen=True)
class CalibratedPracticeItem:
    """A question eligible for target-success selection only after calibration."""

    canonical_item_id: str | None
    item_family_id: str | None
    estimated_success: float
    calibration_observations: int
    calibrated: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not math.isfinite(self.estimated_success) or not 0.0 <= self.estimated_success <= 1.0:
            raise ValueError("estimated_success must be a finite probability")
        if self.calibration_observations < 0:
            raise ValueError("calibration_observations cannot be negative")


@dataclass(frozen=True)
class PracticeSelectionResult:
    items: tuple[CalibratedPracticeItem, ...]
    used_target_success: bool
    fallback_reason: str | None
    practice_target_success: float
    fsrs_desired_retention: float
    version: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "items": [item.metadata | {
                "canonical_item_id": item.canonical_item_id,
                "item_family_id": item.item_family_id,
                "estimated_success": item.estimated_success,
                "calibration_observations": item.calibration_observations,
                "calibrated": item.calibrated,
            } for item in self.items],
            "used_target_success": self.used_target_success,
            "fallback_reason": self.fallback_reason,
            "practice_target_success": self.practice_target_success,
            "fsrs_desired_retention": self.fsrs_desired_retention,
            "version": self.version,
        }


def _fallback_items(
    items: list[CalibratedPracticeItem],
    requested_count: int,
    fallback_selector: Callable[[list[CalibratedPracticeItem], int], Iterable[CalibratedPracticeItem]] | None,
) -> tuple[CalibratedPracticeItem, ...]:
    selected = (
        list(fallback_selector(items, requested_count))
        if fallback_selector is not None
        else items[:requested_count]
    )
    return tuple(selected[:requested_count])


def select_practice_items(
    items: Iterable[CalibratedPracticeItem],
    *,
    requested_count: int,
    config: PracticeSelectionConfig = PracticeSelectionConfig(),
    fallback_selector: Callable[[list[CalibratedPracticeItem], int], Iterable[CalibratedPracticeItem]] | None = None,
) -> PracticeSelectionResult:
    """Select calibrated items near 0.75, or return the current-strategy pool.

    The FSRS retention value is returned as configuration metadata only; it is
    never used as the practice success target or as a sort key.  A pool item
    must carry both stable IDs and enough calibration observations.  Generated
    one-off questions therefore cannot enter this path by accident.
    """

    if requested_count < 0:
        raise ValueError("requested_count cannot be negative")
    all_items = list(items)
    if not config.enabled:
        return PracticeSelectionResult(
            items=_fallback_items(all_items, requested_count, fallback_selector),
            used_target_success=False,
            fallback_reason="feature_disabled",
            practice_target_success=config.practice_target_success,
            fsrs_desired_retention=config.fsrs_desired_retention,
            version=config.version,
        )

    calibrated = [
        item
        for item in all_items
        if item.calibrated
        and item.canonical_item_id
        and item.item_family_id
        and item.calibration_observations >= config.min_calibration_observations
    ]
    # Regardless of the optional compatibility knob, an empty calibrated pool
    # cannot support a target-success decision.  Returning an empty
    # ``used_target_success=True`` result would silently drop the caller's
    # practice items instead of falling back to the current strategy.
    if not calibrated:
        return PracticeSelectionResult(
            items=_fallback_items(all_items, requested_count, fallback_selector),
            used_target_success=False,
            fallback_reason="no_calibrated_item_pool",
            practice_target_success=config.practice_target_success,
            fsrs_desired_retention=config.fsrs_desired_retention,
            version=config.version,
        )

    selected = sorted(
        calibrated,
        key=lambda item: (
            abs(item.estimated_success - config.practice_target_success),
            -item.calibration_observations,
            item.canonical_item_id or "",
        ),
    )[:requested_count]
    return PracticeSelectionResult(
        items=tuple(selected),
        used_target_success=True,
        fallback_reason=None,
        practice_target_success=config.practice_target_success,
        fsrs_desired_retention=config.fsrs_desired_retention,
        version=config.version,
    )
