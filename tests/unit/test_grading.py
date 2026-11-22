import pytest
from eduagents_api.grading import parse_grade


@pytest.mark.parametrize(
    "raw",
    [
        "broken",
        "[]",
        "null",
        "{}",
        '{"score": null}',
        '{"score": true}',
        '{"score": "0.8"}',
        '{"score": -0.1}',
        '{"score": 1.1}',
        '{"score": NaN}',
        '{"score": Infinity}',
        '{"score": 1e999}',
        '{"score": 0.8, "reason": []}',
    ],
)
def test_invalid_grading_is_unknown(raw):
    assert parse_grade(raw) is None


@pytest.mark.parametrize("score", [0, 0.6, 1])
def test_valid_grade_preserves_score(score):
    assert parse_grade(f'{{"score": {score}, "reason": "解释"}}') == (score, "解释")
