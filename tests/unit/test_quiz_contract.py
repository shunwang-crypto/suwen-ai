"""Stable API boundary tests for quiz answer batches."""

import uuid

import pytest
from eduagents_api.routers.quiz import AttemptIn
from pydantic import ValidationError


def test_quiz_accepts_twenty_answers():
    body = AttemptIn(
        answers=[{"question_id": uuid.uuid4(), "response": "ok"} for _ in range(20)]
    )
    assert len(body.answers) == 20


def test_quiz_rejects_twenty_one_answers():
    with pytest.raises(ValidationError):
        AttemptIn(
            answers=[{"question_id": uuid.uuid4(), "response": "ok"} for _ in range(21)]
        )
