"""Executable-plan task and resource input boundaries."""

from eduagents_core.graphs.plan_graph import _coerce_task, _fix_resource_refs, is_safe_public_url


def test_task_coercion_adds_executable_fields_and_clamps_public_values():
    task = _coerce_task(
        {
            "title": "  阅读极限  ",
            "minutes": 20,
            "kc_names": ["极限"],
            "resource_ref": 0,
            "action_type": "read",
            "instructions": ["第一步", "第二步", "第三步", "第四步", "第五步", "多余步骤"],
            "practice_count": 999,
            "difficulty": "hard",
            "task_id": "stage9.day9.task9",
            "resource_id": "not-trusted-yet",
        }
    )
    assert task is not None
    assert task["objective"]
    assert len(task["instructions"]) == 5
    assert task["completion_criteria"]
    assert task["action_type"] == "read"
    assert task["practice_count"] == 20
    assert task["task_id"] != "stage9.day9.task9" and "resource_id" not in task


def test_task_coercion_fallback_is_executable_without_a_resource():
    task = _coerce_task({"title": "完成基础练习", "minutes": 30, "kc_names": []})
    assert task is not None
    assert task["action_type"] == "practice"
    assert len(task["instructions"]) == 2
    assert task["resource_ref"] is None


def test_web_resource_url_allows_public_http_and_rejects_unsafe_schemes():
    assert is_safe_public_url("https://example.com/course/1")
    assert is_safe_public_url("http://8.8.8.8/guide")
    assert not is_safe_public_url("javascript:alert(1)")
    assert not is_safe_public_url("http://127.0.0.1/private")
    assert not is_safe_public_url("http://user:pass@example.com/a")


def test_resource_hardening_attaches_retrieved_resource_to_readable_tasks():
    plan = {
        "stages": [{
            "name": "基础学习",
            "days": [{
                "day": 1,
                "tasks": [{
                    "title": "阅读资料",
                    "minutes": 30,
                    "kc_names": [],
                    "objective": "理解核心概念",
                    "instructions": ["阅读", "复述"],
                    "completion_criteria": "能解释核心概念",
                    "action_type": "mixed",
                    "resource_ref": None,
                }],
            }],
        }],
    }
    fixes = []
    _fix_resource_refs(plan, 2, fixes)
    task = plan["stages"][0]["days"][0]["tasks"][0]
    assert task["resource_ref"] == 0
    assert any(item["fix"] == "resource_ref_attached" for item in fixes)
