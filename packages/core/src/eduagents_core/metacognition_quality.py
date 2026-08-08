"""Explain-back rubric and offline human-agreement gate.

The rubric is deliberately separate from learning-state code.  Its numeric
output is an observation/shadow artifact and cannot be used as BKT or FSRS
evidence.  This module contains no model or network dependency, so offline
evaluation can run on redacted/synthetic records without invoking an LLM.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

EXPLAIN_BACK_RUBRIC_VERSION = "explain-back-rubric-v1"
EXPLAIN_BACK_DIMENSIONS = (
    "correctness",
    "reasoning",
    "completeness",
    "evidence",
    "misconception",
)
MIN_HUMAN_LABELS_FOR_DISPLAY = 100
MIN_HUMAN_AGREEMENT_FOR_DISPLAY = 0.8


def validate_score_json(value: object) -> dict[str, float] | None:
    """Validate all five rubric dimensions and return a normalized mapping."""

    if not isinstance(value, Mapping):
        return None
    candidate = value.get("score") if isinstance(value.get("score"), Mapping) else value
    if not isinstance(candidate, Mapping):
        return None
    if any(dimension not in candidate for dimension in EXPLAIN_BACK_DIMENSIONS):
        return None
    output: dict[str, float] = {}
    for dimension in EXPLAIN_BACK_DIMENSIONS:
        raw = candidate.get(dimension)
        if isinstance(raw, bool):
            return None
        try:
            score = float(raw)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(score) or not 0.0 <= score <= 1.0:
            return None
        output[dimension] = round(score, 6)
    return output


def build_offline_eval_record(
    *,
    record_id: str,
    model_score: Mapping[str, float] | None,
    annotator_a: Mapping[str, float],
    annotator_b: Mapping[str, float],
    rubric_version: str = EXPLAIN_BACK_RUBRIC_VERSION,
) -> dict[str, Any]:
    """Build the redacted JSONL shape consumed by an offline evaluator.

    The function stores only a caller-provided opaque ID and rubric numbers;
    it intentionally has no student text field and cannot manufacture a
    100-row evidence set.
    """

    a = validate_score_json(annotator_a)
    b = validate_score_json(annotator_b)
    if a is None or b is None:
        raise ValueError("both annotator labels must contain all rubric dimensions")
    model = validate_score_json(model_score) if model_score is not None else None
    return {
        "record_id": str(record_id),
        "rubric_version": rubric_version,
        "model_score": model,
        "annotator_a": a,
        "annotator_b": b,
    }


def human_agreement(
    records: Sequence[Mapping[str, object]],
) -> dict[str, float | int | bool | None]:
    """Compute simple exact/within-tolerance agreement for the rubric.

    This is an offline gate, not a claim about real quality.  Empty or
    incomplete input returns an unready gate; callers must supply at least
    100 real double-labelled records before enabling numeric display.
    """

    usable = 0
    exact = 0
    within_tolerance = 0
    for record in records:
        a = validate_score_json(record.get("annotator_a"))
        b = validate_score_json(record.get("annotator_b"))
        if a is None or b is None:
            continue
        usable += 1
        differences = [abs(a[dimension] - b[dimension]) for dimension in EXPLAIN_BACK_DIMENSIONS]
        if all(value == 0.0 for value in differences):
            exact += 1
        if all(value <= 0.1 for value in differences):
            within_tolerance += 1
    agreement = within_tolerance / usable if usable else None
    ready = bool(
        usable >= MIN_HUMAN_LABELS_FOR_DISPLAY
        and agreement is not None
        and agreement >= MIN_HUMAN_AGREEMENT_FOR_DISPLAY
    )
    return {
        "sample_n": usable,
        "exact_agreement": round(exact / usable, 6) if usable else None,
        "within_tolerance_agreement": round(agreement, 6) if agreement is not None else None,
        "display_ready": ready,
    }


def numeric_display_allowed(
    *,
    operator_enabled: bool,
    agreement: Mapping[str, object] | None,
) -> bool:
    """Require both an explicit flag and an offline human gate."""

    if not operator_enabled or not isinstance(agreement, Mapping):
        return False
    sample_n = agreement.get("sample_n")
    score = agreement.get("within_tolerance_agreement")
    finite_score = (
        float(score)
        if isinstance(score, (int, float)) and not isinstance(score, bool)
        else None
    )
    return (
        isinstance(sample_n, int)
        and sample_n >= MIN_HUMAN_LABELS_FOR_DISPLAY
        and finite_score is not None
        and math.isfinite(finite_score)
        and finite_score >= MIN_HUMAN_AGREEMENT_FOR_DISPLAY
        and agreement.get("display_ready") is True
    )
