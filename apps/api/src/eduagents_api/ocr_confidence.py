"""Server-owned field confidence contract for structured question OCR."""

from __future__ import annotations

import math
from typing import Any

CONFIDENCE_SCHEMA_VERSION = "resource-question-field-confidence-v1"
CONFIDENCE_FIELDS = (
    "question_no",
    "page_no",
    "type",
    "stem",
    "options",
    "detected_student_answer",
    "detected_reference_answer",
    "answer",
    "explanation",
    "kc_names",
    "difficulty",
    "points",
)
CRITICAL_FIELDS = ("stem", "detected_student_answer", "answer")
STRUCTURAL_FIELDS = ("type", "options", "question_no", "page_no")
CRITICAL_THRESHOLD = 0.90
STRUCTURAL_THRESHOLD = 0.85
ALLOWED_SOURCES = frozenset(
    {"parser", "ocr_provider", "rule", "model_reported", "unknown", "user_correction"}
)
MAX_WARNINGS = 3
MAX_WARNING_CHARS = 160


def _entry(value: Any, *, source_override: str | None = None) -> dict[str, Any]:
    if not isinstance(value, dict):
        value = {}
    confidence = value.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        confidence = None
    elif not math.isfinite(float(confidence)) or not 0 <= float(confidence) <= 1:
        confidence = None
    else:
        confidence = round(float(confidence), 4)
    source = value.get("source")
    if confidence is None:
        # A source claim without a usable numeric confidence is not evidence.
        source = "unknown"
    elif source_override in ALLOWED_SOURCES:
        source = source_override
    elif source not in ALLOWED_SOURCES:
        source = "unknown"
    warnings: list[str] = []
    raw_warnings = value.get("warnings")
    if isinstance(raw_warnings, list):
        for warning in raw_warnings:
            text = str(warning).strip()
            if text:
                warnings.append(text[:MAX_WARNING_CHARS])
            if len(warnings) >= MAX_WARNINGS:
                break
    return {"confidence": confidence, "source": source, "warnings": warnings}


def clean_field_confidence(
    raw: Any, *, source_override: str | None = None
) -> dict[str, Any]:
    """Return a bounded versioned object; unknown fields are discarded."""
    values = raw.get("fields", raw) if isinstance(raw, dict) else {}
    if not isinstance(values, dict):
        values = {}
    result = {
        "schema_version": CONFIDENCE_SCHEMA_VERSION,
        "fields": {
            field: _entry(values.get(field), source_override=source_override)
            for field in CONFIDENCE_FIELDS
        },
    }
    if isinstance(raw, dict) and isinstance(raw.get("applicable_fields"), list):
        result["applicable_fields"] = [
            field for field in raw["applicable_fields"] if field in CONFIDENCE_FIELDS
        ]
    return result


def evaluate_question_eligibility(
    confidence: Any,
    *,
    question_type: str | None = None,
    paginated: bool | None = None,
    has_student_answer: bool | None = None,
    stem: str | None = None,
    answer: str | None = None,
    options: list[Any] | None = None,
) -> dict[str, Any]:
    """Compute whether a question may be auto-confirmed.

    This function only reasons over the persisted server contract.  A missing
    entry is low confidence, and an uncalibrated model-reported value never
    qualifies regardless of its numeric value.
    """
    cleaned = clean_field_confidence(confidence)
    fields = cleaned["fields"]
    reasons: list[str] = []
    declared_applicable = confidence.get("applicable_fields") if isinstance(confidence, dict) else None
    applicable = (
        set(declared_applicable)
        if isinstance(declared_applicable, list)
        else None
    )

    def applies(field: str) -> bool:
        if field == "options" and question_type is not None:
            return question_type in {"single", "multi"}
        if field == "page_no" and paginated is not None:
            return paginated
        if field == "detected_student_answer" and has_student_answer is not None:
            return has_student_answer
        if applicable is not None:
            return field in applicable
        return True

    required_fields = ["stem", "answer", "type", "question_no"]
    if applies("options"):
        required_fields.append("options")
    if applies("page_no"):
        required_fields.append("page_no")
    if applies("detected_student_answer"):
        required_fields.append("detected_student_answer")

    for field in required_fields:
        entry = fields[field]
        if entry["source"] == "model_reported":
            reasons.append("model_reported_not_calibrated")
            break

    for field in required_fields:
        entry = fields[field]
        threshold = CRITICAL_THRESHOLD if field in CRITICAL_FIELDS else STRUCTURAL_THRESHOLD
        if entry["confidence"] is None or entry["confidence"] < threshold:
            reasons.append(f"{field}_below_threshold")

    if stem is not None and not stem.strip():
        reasons.append("stem_missing")
    if answer is not None and not answer.strip():
        reasons.append("answer_missing")
    if options is not None and question_type in {"single", "multi"} and not options:
        reasons.append("options_missing")

    for field in required_fields:
        entry = fields[field]
        if entry["warnings"]:
            reasons.append(f"{field}_has_warnings")
    # Stable ordering and bounded output avoid turning provider output into an
    # unbounded API/log channel.
    reasons = list(dict.fromkeys(reasons))[: len(CONFIDENCE_FIELDS) + 1]
    return {
        "schema_version": CONFIDENCE_SCHEMA_VERSION,
        "eligible": not reasons,
        "reasons": reasons,
    }
