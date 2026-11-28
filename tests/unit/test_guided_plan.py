"""Default planning compiles authored activities and confirms the persisted draft."""

import json
from copy import deepcopy
from dataclasses import replace

import pytest
from eduagents_core.config import Settings
from eduagents_core.education.context import EducationDeps
from eduagents_core.education.guided_plan import _edited_plan, compile_activities
from eduagents_core.education.plan import _iter_days
from eduagents_core.education.plan_checks import plan_issues
from eduagents_core.gateway.base import GatewayError
from eduagents_core.harness.task import AgentTask, MemoryTaskStore
from eduagents_core.retrieval.base import RetrievalResult, RetrievedChunk

from .test_native_agent_tasks import ScriptedModel

UNITS = [
    {
        "topic": "对照解释",
        "learn": "区分两组观察值并说明只更改一个条件",
        "practice": "设计只更改一个条件的比较",
        "check": "提交两组条件和差异解释",
    },
    {
        "topic": "记录观察",
        "learn": "制作填写观察时间和结果的表格",
        "practice": "按观察顺序记下两条记录",
        "check": "核对表中顺序与原观察一致",
        "resource_ref": 1,
    },
]


def test_activities_are_scheduled_in_prerequisite_order_with_real_durations():
    state = {
        "total_days": 12,
        "budget_hours_per_day": 1 / 3,
        "kc_order": ["记录观察", "对照解释"],
        "diagnosis": {"记录观察": {"evidence_state": "observed"}},
    }
    plan = compile_activities(UNITS, "提交一张记录表及比较说明", state)
    assert not plan_issues(plan, state, resources=[{}, {}])
    days = _iter_days(plan)
    assert [day["day"] for day in days] == list(range(1, 13))
    assert all(0 < sum(task["minutes"] for task in day["tasks"]) <= 15 for day in days)
    tasks = [task for day in days for task in day["tasks"]]
    assert [task["step_key"] for task in tasks[:5]] == [
        "learn-1",
        "practice-1",
        "baseline-2",
        "learn-2",
        "practice-2",
    ]
    assert tasks[3]["depends_on"] == ["learn-1", "baseline-2"]
    assert tasks[-1]["step_key"] == "assessment" and tasks[-1]["depends_on"] == [
        "practice-1",
        "practice-2",
    ]
    assert all(task["depends_on"] in (["practice-1"], ["practice-2"]) for task in tasks[5:-1])


@pytest.mark.parametrize(
    "field,value",
    [
        ("minutes", True),
        ("resource_ref", "0"),
        ("completion_criteria", " "),
        ("instructions", ["保留一条", " "]),
        ("kc_names", []),
    ],
)
def test_confirm_edit_rejects_invalid_raw_fields(field, value):
    candidate = compile_activities([UNITS[0]], None, {"total_days": 1})
    _iter_days(candidate)[0]["tasks"][-1][field] = value
    with pytest.raises(GatewayError, match="PLAN_EDIT_INVALID"):
        _edited_plan(candidate)


def test_confirm_edit_rejects_dropped_practice_wrong_day_numbers_and_malformed_extra_task():
    candidate = compile_activities([UNITS[0]], None, {"total_days": 1})
    day = _iter_days(candidate)[0]
    day["tasks"].append(None)
    with pytest.raises(GatewayError, match="PLAN_EDIT_INVALID"):
        _edited_plan(candidate)
    day["tasks"].pop()
    day["day"] = 2
    with pytest.raises(GatewayError, match="PLAN_EDIT_INVALID"):
        _edited_plan(candidate)
    day["day"] = 1
    day["tasks"].pop()
    with pytest.raises(GatewayError, match="PLAN_EDIT_INVALID"):
        _edited_plan(candidate)


class PlanRetrieval:
    def __init__(self):
        self.calls = []

    async def retrieve(self, owner, kb, query):
        self.calls.append((owner, kb, query))
        return RetrievalResult(
            chunks=[
                RetrievedChunk(
                    chunk_id,
                    excerpt,
                    title,
                    "real-doc",
                    1,
                    1,
                    0.9,
                    "fused",
                    payload={"kb_id": kb_id},
                )
                for chunk_id, excerpt, title, kb_id in (
                    ("other-course", "没有授权的内容", "另一个课程", "other-kb"),
                    ("unused", "不相干的示例", "未引用材料", kb),
                    ("read", "逐条记录，避免事后补写。", "实验记录课程讲义", kb),
                )
            ]
        )


async def test_native_default_draft_and_edited_confirmation_use_trusted_resources_only(monkeypatch):
    monkeypatch.delenv("PLAN_GENERATION_MODE", raising=False)
    model = ScriptedModel([("submit_learning_activities", {"units": UNITS}), "finished"])
    retrieval, store = PlanRetrieval(), MemoryTaskStore()
    deps = EducationDeps(Settings(_env_file=None), model, retrieval=retrieval)
    task = AgentTask("plan", deps, store)
    thread = "plan:owner:guided"
    payload = {
        "goal": "记录并比较两次观察",
        "total_days": 2,
        "budget_hours_per_day": 0.5,
        "user_id": "owner",
        "tenant_id": "tenant",
        "kb_id": "authorized-kb",
        "plan_mode": "course_bound",
        "kc_order": ["记录观察", "对照解释"],
    }
    draft = await task.run(thread, payload)
    assert draft.state["_architecture"] == "guided" and draft.status == "awaiting_confirmation"
    assert retrieval.calls == [("tenant", "authorized-kb", payload["goal"])]
    context = " ".join(message.content for message in model.requests[0].messages)
    assert "逐条记录，避免事后补写。" in context and "没有授权的内容" not in context
    assert [resource["chunk_id"] for resource in draft.pending["resources"]] == ["read"]
    assert not draft.state.get("plan_confirmed")
    assert not draft.pending.get("review")
    days = _iter_days(draft.pending["plan"])
    lesson = next(work for work in days[0]["tasks"] if work["learning_phase"] == "learn")
    assert lesson["resource_ref"] == 0 and lesson["action_type"] == "read"

    invalid = deepcopy(draft.pending["plan"])
    _iter_days(invalid)[0]["tasks"][0]["minutes"] = 31
    with pytest.raises(GatewayError, match="PLAN_EDIT_INVALID"):
        await task.run(thread, resume={"approved": True, "plan": invalid})
    assert (await store.load(thread)).status == "awaiting_confirmation"

    edited = deepcopy(draft.pending["plan"])
    _iter_days(edited)[0]["tasks"][0]["title"] = "编辑起点题标题"
    resume_model = ScriptedModel([])
    team_settings = deps.settings.model_copy(update={"plan_generation_mode": "team"})
    resume = AgentTask("plan", replace(deps, settings=team_settings, gateway=resume_model), store)
    confirmed = await resume.run(thread, resume={"approved": True, "plan": edited})
    assert confirmed.status == "completed" and confirmed.state["plan_confirmed"]
    assert confirmed.state["_architecture"] == "guided"
    assert not resume_model.requests
    assert _iter_days(confirmed.state["plan"])[0]["tasks"][0]["title"] == "编辑起点题标题"


async def test_native_legacy_checkpoint_still_uses_original_publish_tool():
    store = MemoryTaskStore()
    original = ScriptedModel(
        [
            (
                "submit_plan_draft",
                {
                    "plan": {
                        "stages": [{"days": [{"tasks": [{"title": "尝试旧练习", "minutes": 15}]}]}],
                    }
                },
            ),
            "finished",
        ]
    )
    settings = Settings(_env_file=None, plan_generation_mode="single")
    thread = "plan:owner:legacy"
    first = await AgentTask("plan", EducationDeps(settings, original), store).run(
        thread,
        {"goal": "说明实验记录", "budget_hours_per_day": 0.25, "total_days": 1},
    )
    assert first.status == "awaiting_confirmation"
    del first.state["_architecture"]
    await store.save(thread, first)
    upgraded = ScriptedModel([("publish_plan", {}), "finished"])
    new_settings = settings.model_copy(update={"plan_generation_mode": "guided"})
    confirmed = await AgentTask("plan", EducationDeps(new_settings, upgraded), store).run(
        thread,
        resume={"approved": True},
    )
    assert (
        confirmed.status == "completed"
        and "step_key" not in _iter_days(confirmed.state["plan"])[0]["tasks"][0]
    )


async def test_local_model_prose_gets_schema_repair_and_normal_tool_validation():
    content = {"units": [UNITS[0]]}
    model = ScriptedModel(["以下是教学活动。", json.dumps(content, ensure_ascii=False), "finished"])
    settings = Settings(
        _env_file=None, plan_generation_mode="guided", ollama_chat_model="local-test"
    )
    task = AgentTask("plan", EducationDeps(settings, model))
    draft = await task.run(
        "plan:owner:repair",
        {
            "goal": "解释一项对照实验",
            "budget_hours_per_day": 0.5,
            "total_days": 1,
        },
    )
    assert draft.status == "awaiting_confirmation"
    first, repaired, _ = model.requests
    assert first.tools and first.response_schema is None
    assert (
        not repaired.tools
        and repaired.json_mode
        and repaired.response_schema["required"] == ["units"]
    )
    tasks = _iter_days(draft.pending["plan"])[0]["tasks"]
    assert [item["learning_phase"] for item in tasks] == ["baseline", "learn", "practice"]
    assert tasks[-1]["completion_criteria"] == UNITS[0]["check"]
