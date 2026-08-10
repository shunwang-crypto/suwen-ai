"""Field-level OCR confidence cleaning and server eligibility."""

from eduagents_api.ocr_confidence import (
    CONFIDENCE_FIELDS,
    CONFIDENCE_SCHEMA_VERSION,
    clean_field_confidence,
    evaluate_question_eligibility,
)
from eduagents_api.resource_utils import normalize_extracted_question


def _high(source: str = "ocr_provider"):
    return {"confidence": 0.95, "source": source, "warnings": []}


def test_cleaner_keeps_whitelist_and_bounds_warnings():
    result = clean_field_confidence(
        {
            "stem": _high(),
            "unknown": {"confidence": 1.0, "source": "model_reported"},
            "answer": {"confidence": "not-a-number", "source": "ocr_provider", "warnings": ["x"] * 20},
        }
    )

    assert result["schema_version"] == CONFIDENCE_SCHEMA_VERSION
    assert "unknown" not in result["fields"]
    assert result["fields"]["stem"] == _high()
    assert result["fields"]["answer"]["confidence"] is None
    assert len(result["fields"]["answer"]["warnings"]) <= 3


def test_missing_or_invalid_confidence_is_unknown_not_one():
    result = clean_field_confidence(
        {"stem": {"confidence": 2, "source": "ocr_provider"}, "type": {}}
    )

    assert result["fields"]["stem"]["confidence"] is None
    assert result["fields"]["stem"]["source"] == "unknown"
    assert result["fields"]["type"]["confidence"] is None
    assert result["fields"]["type"]["source"] == "unknown"


def test_model_reported_high_values_are_not_auto_eligible():
    fields = {name: _high("model_reported") for name in (
        "stem", "detected_student_answer", "answer", "type", "options", "question_no", "page_no"
    )}
    result = evaluate_question_eligibility({"fields": fields})

    assert result["eligible"] is False
    assert "model_reported_not_calibrated" in result["reasons"]


def test_unknown_and_threshold_boundaries_are_low_confidence():
    fields = {
        "stem": _high(),
        "detected_student_answer": {"confidence": 0.9, "source": "ocr_provider"},
        "answer": _high(),
        "type": {"confidence": 0.85, "source": "parser"},
        "options": {"confidence": 0.84, "source": "parser"},
        "question_no": _high(),
        "page_no": _high(),
    }
    result = evaluate_question_eligibility({"fields": fields})

    assert result["eligible"] is False
    assert any("options" in reason for reason in result["reasons"])


def test_normalizer_uses_versioned_per_field_confidence_and_forces_model_source():
    raw = {
        "question_no": "7",
        "page_no": 2,
        "type": "single",
        "stem": "函数的定义域是什么？",
        "options": ["A", "B"],
        "answer": "A",
        "field_confidence": {
            field: {"confidence": 0.97, "source": "ocr_provider"}
            for field in CONFIDENCE_FIELDS
        },
        "confidence": 0.97,
    }

    normalized = normalize_extracted_question(
        raw, 1, confidence_source="model_reported", paginated=True
    )

    assert normalized["confidence"] == 0.97
    assert set(normalized["field_confidence"]["fields"]) == set(CONFIDENCE_FIELDS)
    assert all(
        entry["source"] == "model_reported"
        for entry in normalized["field_confidence"]["fields"].values()
        if entry["confidence"] is not None
    )
    assert "options" in normalized["field_confidence"]["applicable_fields"]


def test_parser_rule_evidence_can_pass_and_model_reported_cannot():
    parsed = normalize_extracted_question(
        {
            "question_no": "1",
            "type": "short",
            "stem": "1+1 等于多少？",
            "answer": "2",
        },
        1,
        confidence_source="rule",
        paginated=False,
    )
    assert evaluate_question_eligibility(
        parsed["field_confidence"],
        question_type=parsed["type"],
        paginated=False,
        has_student_answer=False,
        stem=parsed["stem"],
        answer=parsed["detected_reference_answer"],
        options=parsed["options"],
    )["eligible"] is True

    spoofed = evaluate_question_eligibility(
        normalize_extracted_question(
            {
                "question_no": "1",
                "type": "short",
                "stem": "1+1 等于多少？",
                "answer": "2",
                "field_confidence": {
                    "stem": {"confidence": 0.99, "source": "parser"},
                    "type": {"confidence": 0.99, "source": "parser"},
                    "question_no": {"confidence": 0.99, "source": "parser"},
                    "answer": {"confidence": 0.99, "source": "parser"},
                },
            },
            1,
            confidence_source="model_reported",
            paginated=False,
        )["field_confidence"],
        question_type="short",
        paginated=False,
        has_student_answer=False,
        stem="1+1 等于多少？",
        answer="2",
        options=[],
    )
    assert spoofed["eligible"] is False
    assert "model_reported_not_calibrated" in spoofed["reasons"]


def test_eligibility_applies_options_only_to_choice_questions():
    high = {
        field: _high("parser")
        for field in ("stem", "type", "question_no", "answer")
    }
    short = evaluate_question_eligibility(
        {"fields": {**high, "options": {"confidence": None, "source": "unknown"}}},
        question_type="short",
        paginated=False,
        has_student_answer=False,
        stem="简答题",
        answer="答案",
        options=[],
    )
    assert short["eligible"] is True

    single = evaluate_question_eligibility(
        {"fields": {**high, "options": _high("parser")}},
        question_type="single",
        paginated=False,
        has_student_answer=False,
        stem="单选题",
        answer="A",
        options=[],
    )
    assert single["eligible"] is False
    assert "options_missing" in single["reasons"]
