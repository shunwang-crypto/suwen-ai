"""Wave 2 practice recommendation policy tests."""

from __future__ import annotations

import uuid

import pytest
from eduagents_api.practice_recommendation import (
    FIXED_COLD_START_MIX,
    PracticeContext,
    _adjust_mix,
    build_recommendation,
    validate_adjustment,
)


def _kb(name: str = "高等数学") -> dict:
    return {
        "id": str(uuid.uuid4()),
        "name": name,
        "subject": "math",
        "kc_ids": [str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())],
        "kc_names": ["极限", "导数", "积分"],
        "has_mastery": False,
    }


def test_priority_review_beats_plan_weak_and_cold_start():
    task_id = str(uuid.uuid4())
    recommendation = build_recommendation(
        PracticeContext(
            due_count=2,
            plan_task={"plan_id": str(uuid.uuid4()), "task_id": task_id},
            weak_points=[{"name": "极限", "p_mastery": 0.2}],
            ready_kbs=[_kb()],
        )
    )

    assert recommendation.action == "review"
    assert recommendation.source == "due_review"
    assert recommendation.question_count == 0


def test_plan_task_beats_weak_point_practice():
    task_id = str(uuid.uuid4())
    recommendation = build_recommendation(
        PracticeContext(
            plan_task={
                "plan_id": str(uuid.uuid4()),
                "task_id": task_id,
                "kb_id": str(uuid.uuid4()),
                "goal": "期末及格",
                "count": 4,
                "kc_names": ["函数极限"],
            },
            weak_points=[{"name": "导数", "p_mastery": 0.2}],
            ready_kbs=[_kb()],
        )
    )

    assert recommendation.action == "quiz"
    assert recommendation.source == "plan_task"
    assert recommendation.question_count == 4
    assert recommendation.plan_id
    assert recommendation.task_id == task_id


def test_cold_start_is_three_questions_with_fixed_mix_and_real_kcs():
    kb = _kb()
    recommendation = build_recommendation(
        PracticeContext(ready_kbs=[kb])
    )

    assert recommendation.source == "cold_start"
    assert recommendation.question_count == 3
    assert recommendation.difficulty_mix == FIXED_COLD_START_MIX
    assert recommendation.target_kc_ids == tuple(kb["kc_ids"])
    assert recommendation.adjustments == ("easier", "harder", "switch_kc")


def test_low_confidence_kcs_stay_in_assessment_mode_before_maintenance():
    kb = _kb()
    kb["has_mastery"] = True
    kb["assessment_kcs"] = [
        {"id": kb["kc_ids"][1], "name": kb["kc_names"][1]},
        {"id": kb["kc_ids"][2], "name": kb["kc_names"][2]},
    ]

    recommendation = build_recommendation(PracticeContext(ready_kbs=[kb]))

    assert recommendation.source == "cold_start"
    assert recommendation.question_count == 3
    assert recommendation.target_kc_ids == tuple(kb["kc_ids"][1:])
    public = recommendation.to_dict()
    assert "p_mastery" not in public
    assert "confidence" not in public
    assert "mastery_band" not in public


def test_adjustment_cannot_repeat_when_no_difficulty_step_remains():
    with pytest.raises(ValueError, match="无法继续"):
        _adjust_mix({"easy": 3, "mid": 0, "hard": 0}, "easier")

    with pytest.raises(ValueError, match="无法继续"):
        _adjust_mix({"easy": 0, "mid": 0, "hard": 3}, "harder")


def test_kb_with_mastery_but_no_weak_point_is_not_called_cold_start():
    kb = _kb()
    kb["has_mastery"] = True

    recommendation = build_recommendation(PracticeContext(ready_kbs=[kb]))

    assert recommendation.source == "mastery"
    assert recommendation.action == "quiz"
    assert recommendation.question_count == 5
    assert recommendation.kb == {"id": kb["id"], "name": kb["name"]}
    assert recommendation.target_kc_ids == tuple(kb["kc_ids"])


def test_multiple_ready_kbs_only_asks_for_scope_once():
    recommendation = build_recommendation(
        PracticeContext(ready_kbs=[_kb("数学"), _kb("物理")])
    )

    assert recommendation.status == "needs_scope"
    assert recommendation.action == "choose_scope"
    assert len(recommendation.scope_options) == 2
    assert recommendation.question_count == 0


def test_single_ready_kb_does_not_offer_subject_switch():
    recommendation = build_recommendation(PracticeContext(ready_kbs=[_kb()]))

    assert "switch_kb" not in recommendation.adjustments


def test_adjustment_that_changes_decision_returns_the_new_review_action():
    recommendation = build_recommendation(
        PracticeContext(due_count=1, ready_kbs=[_kb()], adjustment="easier")
    )

    assert recommendation.action == "review"
    assert recommendation.source == "due_review"


def test_no_materials_is_an_explicit_upload_state():
    recommendation = build_recommendation(PracticeContext())

    assert recommendation.status == "needs_upload"
    assert recommendation.action == "upload"
    assert recommendation.source == "sample"


@pytest.mark.parametrize("value", ["easier", "harder", "switch_kc", "switch_kb", None])
def test_adjustment_allowlist(value):
    assert validate_adjustment(value) == value


def test_unknown_adjustment_is_rejected():
    with pytest.raises(ValueError, match="adjustment"):
        validate_adjustment("random_topic")
