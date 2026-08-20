"""Plan provenance input must be explicit and internally consistent."""

import uuid

import pytest
from eduagents_api.routers.plans import PlanCreate
from pydantic import ValidationError


def _payload(**overrides):
    payload = {
        "goal": "学习导数",
        "plan_mode": "goal_only",
        "budget_hours_per_day": 1.0,
        "total_days": 7,
    }
    payload.update(overrides)
    return payload


def test_plan_mode_is_required_instead_of_inferred_from_kb_id():
    payload = _payload()
    payload.pop("plan_mode")

    with pytest.raises(ValidationError) as exc_info:
        PlanCreate.model_validate(payload)

    assert any(error["loc"] == ("plan_mode",) for error in exc_info.value.errors())


@pytest.mark.parametrize(
    "payload",
    [
        _payload(plan_mode="course_bound", kb_id=None),
        _payload(plan_mode="goal_only", kb_id=uuid.uuid4()),
        _payload(plan_mode="legacy"),
    ],
)
def test_plan_mode_rejects_ambiguous_or_legacy_scope(payload):
    with pytest.raises(ValidationError):
        PlanCreate.model_validate(payload)


def test_plan_mode_accepts_only_the_two_explicit_scope_shapes():
    kb_id = uuid.uuid4()

    course_bound = PlanCreate.model_validate(
        _payload(plan_mode="course_bound", kb_id=kb_id)
    )
    goal_only = PlanCreate.model_validate(_payload(plan_mode="goal_only", kb_id=None))

    assert course_bound.kb_id == kb_id
    assert goal_only.kb_id is None
