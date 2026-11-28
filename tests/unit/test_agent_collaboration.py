"""Real native DSH workers, independent contexts, reviewed handoffs and cancellation."""

import asyncio
import json
from dataclasses import replace

import pytest
from eduagents_core.config import Settings
from eduagents_core.education.context import EducationDeps
from eduagents_core.education.plan_checks import CHECKS, plan_digest, plan_issues
from eduagents_core.education.team_tools import (
    PlanCoordinatorTools,
    PlannerTools,
    PlanReviewerTools,
)
from eduagents_core.gateway import OpenAIGateway
from eduagents_core.gateway.base import GatewayError
from eduagents_core.harness.collaboration import AgentCollaboration
from eduagents_core.harness.task import AgentTask, MemoryTaskStore, TaskRecord


class TeamModel:
    def __init__(self, *, reject=0, block=False, fail_once=False):
        self.provider = OpenAIGateway(Settings(_env_file=None, gateway_mode="stub"))
        self.requests, self.reject, self.block, self.fail_once = [], reject, block, fail_once
        self.started = asyncio.Event()

    async def chat(self, req):
        self.requests.append(req)
        if req.feature == "agent_researcher":
            self.started.set()
            if self.block:
                await asyncio.Event().wait()
            if self.fail_once:
                self.fail_once = False
                raise GatewayError("TEST_TRANSIENT_FAILURE", "临时失败")
        result = await self.provider.chat(req)
        if req.feature == "agent_planner" and result.tool_calls:
            result = replace(result, text="PLANNER_PRIVATE_CONTEXT")
        if req.feature == "agent_reviewer" and result.tool_calls and self.reject:
            fn = result.tool_calls[0]["function"]
            if fn["name"] == "submit_plan_review":
                self.reject -= 1
                args = json.loads(fn["arguments"])
                args.update(approved=False, summary="任务文字需要修订",
                            issues=[{"code": "TEACHING_CONTENT", "message": "教学步骤需要更明确", "task_keys": ["learn"], "suggestion": "补充教学步骤"}])
                args["checks"]["outcomes"] = "fail"
                fn["arguments"] = json.dumps(args, ensure_ascii=False)
        return result


def dependencies(model, **settings):
    return EducationDeps(Settings(
        _env_file=None, education_prompt_version="edu-v2", plan_generation_mode="team", **settings,
    ), model)


def payload():
    return {"user_id": "offline", "tenant_id": "offline", "request_id": "team-test",
            "goal": "三天学习全等与勾股", "kc_order": ["全等", "勾股"],
            "total_days": 3, "budget_hours_per_day": 0.75}


async def test_native_team_has_separate_sessions_and_review_context_and_idempotent_confirmation():
    model, store = TeamModel(), MemoryTaskStore()
    task = AgentTask("plan", dependencies(model), store)
    record = await task.run("plan:offline:root", payload())
    assert record.status == "awaiting_confirmation" and record.pending["review"]["approved"]
    children = list(record.state["team_tasks"].values())
    assert {v["role"] for v in children} == {"researcher", "planner", "reviewer"}
    assert len({record.native_session_id, *(v["native_session_id"] for v in children)}) == 4
    assert record.state["team_budget"]["calls"] == len(model.requests)
    assert all("PLANNER_PRIVATE_CONTEXT" not in json.dumps([m.to_wire() for m in req.messages])
               for req in model.requests if req.feature == "agent_reviewer")
    assert all(v.state["_parent_thread_id"] == "plan:offline:root" for key, v in store.records.items() if key != "plan:offline:root")
    before = sum(req.feature != "agent_plan_coordinator" for req in model.requests)
    results = await asyncio.gather(*(task.run("plan:offline:root", resume={"approved": True}) for _ in range(2)))
    assert all(r.status == "completed" for r in results)
    assert sum(req.feature != "agent_plan_coordinator" for req in model.requests) == before


async def test_rejected_review_reaches_a_fresh_planner_and_is_reviewed_again():
    model = TeamModel(reject=1)
    record = await AgentTask("plan", dependencies(model)).run("plan:offline:repair", payload())
    assert record.status == "awaiting_confirmation"
    assert len(record.state["team_tasks"]) == 5
    assert record.state["plan_review"]["approved"]
    assert any("教学步骤需要更明确" in json.dumps([m.to_wire() for m in req.messages], ensure_ascii=False)
               for req in model.requests if req.feature == "agent_planner")


async def test_team_stops_after_revision_limit_without_publishing_failed_plan():
    record = await AgentTask("plan", dependencies(TeamModel(reject=9), agent_collaboration_max_revisions=1)).run("plan:offline:bounded", payload())
    assert record.status == "failed" and record.pending is None
    assert record.state["failure_code"] == "PLAN_COLLABORATION_FAILED"
    assert len(record.state["team_tasks"]) == 5


async def test_cancellation_propagates_to_native_children_and_preserves_cancelled_state():
    model, store = TeamModel(block=True), MemoryTaskStore()
    running = asyncio.create_task(AgentTask("plan", dependencies(model), store).run("plan:offline:cancel", payload()))
    await asyncio.wait_for(model.started.wait(), 20)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert (await store.load("plan:offline:cancel")).status == "cancelled"
    assert all(record.status == "cancelled" for record in store.records.values())


async def test_failed_worker_restarts_under_same_identity_and_owner():
    model, store = TeamModel(fail_once=True), MemoryTaskStore()
    record = TaskRecord(state=payload(), status="running")
    first = AgentCollaboration(dependencies(model), store, record, "plan:offline:retry", lambda _: None)
    spec = {"tasks": [{"role": "researcher", "name": "research", "instructions": "收集资料", "input_task_ids": []}]}
    delegated = await first.delegate(spec)
    task_id = delegated["tasks"][0]["id"]
    result = await first.collect({"task_ids": ["research"]})
    assert result["tasks"][0]["status"] == "failed"
    await first.close()
    second = AgentCollaboration(dependencies(model), store, record, "plan:offline:retry", lambda _: None)
    await second.delegate(spec)
    result = await second.collect({"task_ids": [task_id]})
    assert result["tasks"][0]["status"] == "completed"
    assert len(record.state["team_tasks"]) == 1
    await second.close()


async def test_invalid_delegation_batch_and_foreign_results_are_rejected_before_admission():
    record, store = TaskRecord(state=payload(), status="running"), MemoryTaskStore()
    team = AgentCollaboration(dependencies(TeamModel()), store, record, "root", lambda _: None)
    result = await team.delegate({"tasks": [{"role": "researcher", "name": "valid", "instructions": "资料"},
                                            {"role": "planner", "name": "invalid", "instructions": "计划", "input_task_ids": []}]})
    assert not result["accepted"] and not team.tasks and not team.running
    assert not (await team.collect({"task_ids": ["other-root-task"]}))["accepted"]


async def test_short_handoff_names_and_ids_use_the_same_persisted_artifacts():
    store, record = MemoryTaskStore(), TaskRecord(state=payload(), status="running")
    team = AgentCollaboration(dependencies(TeamModel()), store, record, "root", lambda _: None)
    research = await team.delegate({"tasks": [
        {"role": "researcher", "name": name, "instructions": "收集可核验资料"}
        for name in ("research-a", "research-b")
    ]})
    ids = [task["id"] for task in research["tasks"]]
    try:
        collected = await team.collect({"task_ids": ["research-a", ids[1]]})
        assert [task["id"] for task in collected["tasks"]] == ids
        assert all(task["status"] == "completed" for task in collected["tasks"])
        spec = {"role": "planner", "name": "draft", "instructions": "分析两份资料并制订计划"}
        unspecified = await team.delegate({"tasks": [spec]})
        assert not unspecified["accepted"]
        assert {item["name"] for item in unspecified["available_inputs"]} == {"research-a", "research-b"}
        duplicate = await team.delegate({"tasks": [{**spec, "input_task_ids": ["research-a", ids[0]]}]})
        assert not duplicate["accepted"]
        draft = await team.delegate({"tasks": [{**spec, "input_task_ids": ["research-a", ids[1]]}]})
        assert draft["accepted"] and draft["tasks"][0]["input_task_ids"] == ids
        for repetition in (spec, {**spec, "input_task_ids": ids}):
            repeated = await team.delegate({"tasks": [repetition]})
            assert repeated["tasks"][0]["id"] == draft["tasks"][0]["id"]
            assert repeated["tasks"][0]["input_task_ids"] == ids
    finally:
        await team.close()


async def test_blank_handoff_uses_one_completed_upstream_and_stops_for_running_competition():
    store, record = MemoryTaskStore(), TaskRecord(state=payload(), status="running")
    team = AgentCollaboration(dependencies(TeamModel()), store, record, "root", lambda _: None)
    research = await team.delegate({"tasks": [
        {"role": "researcher", "name": "research", "instructions": "找到可核验资料"},
    ]})
    research_id = research["tasks"][0]["id"]
    try:
        await team.collect({"task_ids": ["research"]})
        planner = {"role": "planner", "name": "draft", "instructions": "按研究资料编写计划"}
        explicit_wrong = await team.delegate({"tasks": [{**planner, "input_task_ids": ["missing"]}]})
        assert not explicit_wrong["accepted"]
        team.tasks["researcher:pending"] = {
            "id": "researcher:pending", "name": "pending", "role": "researcher", "status": "running",
        }
        competing = await team.delegate({"tasks": [planner]})
        assert not competing["accepted"] and competing["awaiting_collection"] == ["pending"]
        team.tasks["researcher:pending"]["status"] = "failed"
        result = await team.delegate({"tasks": [planner]})
        assert result["accepted"] and result["tasks"][0]["input_task_ids"] == [research_id]
        await team.collect({"task_ids": ["draft"]})
        reviewer = {"role": "reviewer", "name": "review", "instructions": "独立审查该草案"}
        result = await team.delegate({"tasks": [reviewer]})
        assert result["accepted"] and result["tasks"][0]["input_task_ids"] == [team.lookup("draft")["id"]]
        revision = await team.delegate({"tasks": [{**planner, "name": "revision"}]})
        assert not revision["accepted"]
    finally:
        await team.close()


async def test_legacy_duplicate_names_require_an_explicit_id():
    record = TaskRecord(state=payload(), status="running")
    team = AgentCollaboration(dependencies(TeamModel()), MemoryTaskStore(), record, "root", lambda _: None)
    for role in ("researcher", "planner"):
        task_id = f"{role}:old"
        team.tasks[task_id] = {"id": task_id, "name": "legacy", "role": role, "status": "completed"}
    ambiguous = await team.collect({"task_ids": ["legacy"]})
    assert not ambiguous["accepted"]
    assert {task["id"] for task in ambiguous["available_inputs"]} == set(team.tasks)
    assert not (await team.collect({"task_ids": ["researcher"]}))["accepted"]
    assert (await team.collect({"task_ids": ["planner:old"]}))["tasks"][0]["role"] == "planner"


def phased_plan():
    def task(key, phase, names, deps=()):
        return {"step_key": key, "learning_phase": phase, "kc_names": names, "depends_on": list(deps)}
    return {"stages": [{"name": "学习", "days": [{"day": 1, "tasks": [
        task("baseline", "baseline", ["全等", "勾股"]), task("learn-a", "learn", ["全等"], ["baseline"]),
        task("mixed", "practice", ["全等", "勾股"], ["learn-a"]), task("learn-b", "learn", ["勾股"], ["baseline"]),
    ]}]}]}


async def test_confirmation_handoff_can_only_reference_the_current_confirmed_digest():
    state = {**payload(), "plan_confirmed": True, "plan": phased_plan()}
    record = TaskRecord(state=state, status="running")
    team = AgentCollaboration(dependencies(TeamModel()), MemoryTaskStore(), record, "root", lambda _: None)
    team.confirmed_input()
    old_id = state["confirmed_plan_task_id"]
    state["plan"]["stages"][0]["name"] = "编辑后的计划"
    team.confirmed_input()
    current_id = state["confirmed_plan_task_id"]
    assert current_id != old_id and team.lookup("confirmed-plan")["id"] == current_id

    spec = {"role": "reviewer", "name": "confirm-review", "instructions": "审查确认后的计划"}
    try:
        stale = await team.delegate({"tasks": [{**spec, "input_task_ids": [old_id]}]})
        assert not stale["accepted"]
        artifact = team.tasks[current_id]["artifact"]
        digest = artifact["digest"]
        artifact["digest"] = plan_digest(team.tasks[old_id]["artifact"]["plan"])
        assert not (await team.delegate({"tasks": [spec]}))["accepted"]
        artifact["digest"] = digest
        result = await team.delegate({"tasks": [spec]})
        assert result["accepted"] and result["tasks"][0]["input_task_ids"] == [current_id]
    finally:
        await team.close()


def test_all_topics_in_mixed_practice_require_earlier_learning_and_dependencies_cannot_cycle():
    plan = phased_plan()
    issues = plan_issues(plan, payload())
    assert any(v["code"] == "PRACTICE_BEFORE_LEARNING" and "勾股" in v["message"] for v in issues)
    tasks = plan["stages"][0]["days"][0]["tasks"]
    tasks[2], tasks[3] = tasks[3], tasks[2]
    tasks[3]["depends_on"].append("learn-b")
    assert not plan_issues(plan, payload())
    tasks[1]["depends_on"] = ["mixed"]
    assert any(v["code"] == "DEPENDENCY_NOT_EARLIER" for v in plan_issues(plan, payload()))


async def test_planner_rejects_missing_step_fields_and_keeps_valid_mixed_practice_after_both_lessons():
    record = TaskRecord(state={**payload(), "total_days": 1}, status="running")
    tools = PlannerTools(dependencies(TeamModel()), record, lambda _: None)
    await tools.handlers["read_assignment"]({})
    plan = phased_plan()
    tasks = plan["stages"][0]["days"][0]["tasks"]
    tasks[2], tasks[3] = tasks[3], tasks[2]
    tasks[3]["depends_on"].append("learn-b")
    for task in tasks:
        task.update(title="写出该任务的解题思路", objective="说明推理依据",
                    instructions=["列出已知条件", "解释每一步用了什么性质"],
                    completion_criteria="能独立逐步解释推理",
                    resource_ref=None, action_type="practice", minutes=10)

    missing = json.loads(json.dumps(plan))
    missing_task = missing["stages"][0]["days"][0]["tasks"][0]
    fields = ("step_key", "learning_phase", "depends_on")
    for field in fields:
        del missing_task[field]
    rejected = await tools.handlers["submit_plan_draft"]({"plan": missing})
    assert not rejected["accepted"]
    assert {f"arguments.plan.stages[0].days[0].tasks[0].{field}: required" for field in fields} <= set(rejected["errors"])
    assert record.status == "running" and "artifact" not in record.state

    draft = await tools.handlers["submit_plan_draft"]({"plan": plan})
    assert draft["accepted"] and record.status == "completed"
    reviewed_tasks = record.state["artifact"]["plan"]["stages"][0]["days"][0]["tasks"]
    assert [task["step_key"] for task in reviewed_tasks] == ["baseline", "learn-a", "learn-b", "mixed"]


async def test_planner_requests_missing_days_and_invalid_resources_without_filling_them():
    record = TaskRecord(state=payload(), status="running")
    tools = PlannerTools(dependencies(TeamModel()), record, lambda _: None)
    await tools.read_assignment({})
    plan = phased_plan()
    plan["stages"][0]["days"][0]["tasks"][0]["resource_ref"] = 42
    result = await tools.submit({"plan": plan})
    assert not result["accepted"] and record.pending is None
    assert {issue["code"] for issue in result["issues"]} >= {"PLAN_DAY_COUNT", "RESOURCE_REF_UNAVAILABLE"}
    assert "plan" not in record.state


async def test_reviewer_cannot_approve_program_errors_or_skip_mandatory_checks():
    plan = phased_plan()
    state = {**payload(), "team_inputs": [{"role": "planner", "artifact": {"plan": plan, "digest": plan_digest(plan)}}]}
    record = TaskRecord(state=state, status="running")
    tools = PlanReviewerTools(dependencies(TeamModel()), record, lambda _: None)
    args = {"approved": True, "checks": dict.fromkeys(CHECKS, "pass"), "issues": [], "summary": "通过"}
    assert not (await tools.submit(args))["accepted"]
    await tools.read({})
    assert not (await tools.submit(args))["accepted"]
    assert record.status == "running"


async def test_coordinator_cannot_submit_a_review_for_another_draft_version():
    model, store = TeamModel(), MemoryTaskStore()
    record = TaskRecord(state=payload(), status="running")
    team = AgentCollaboration(dependencies(model), store, record, "root", lambda _: None)
    team.tasks.update(draft={"id": "draft", "name": "planner", "role": "planner", "status": "completed", "artifact": {"digest": "new"}},
                      review={"id": "review", "name": "reviewer", "role": "reviewer", "status": "completed", "artifact": {"approved": True, "draft_digest": "old"}})
    tools = PlanCoordinatorTools(team.deps, record, lambda _: None, team)
    assert not (await tools.submit_reviewed({"draft_task_id": "planner", "review_task_id": "review"}))["accepted"]
    assert record.status == "running" and record.pending is None


async def test_publishing_an_edited_plan_with_multiple_approvals_requires_a_selected_review():
    store, plan = MemoryTaskStore(), phased_plan()
    record = TaskRecord(state={**payload(), "plan_confirmed": True, "plan": plan}, status="running")
    team = AgentCollaboration(dependencies(TeamModel()), store, record, "root", lambda _: None)
    for name in ("review-a", "review-b"):
        task_id = f"reviewer:{name}"
        team.tasks[task_id] = {"id": task_id, "name": name, "role": "reviewer", "status": "completed",
                               "artifact": {"approved": True, "draft_digest": plan_digest(plan), "summary": name}}
    tools = PlanCoordinatorTools(team.deps, record, lambda _: None, team)
    result = await tools.publish({})
    assert not result["accepted"] and record.status == "running"
    assert {task["name"] for task in result["reviews"]} == {"review-a", "review-b"}
    assert not (await tools.publish({"review_task_id": "foreign"}))["accepted"]
    assert (await tools.publish({"review_task_id": "review-b"}))["accepted"]
    assert record.state["plan_review"]["summary"] == "review-b"


async def test_user_edit_is_reviewed_in_a_fresh_session_before_publication():
    model, store = TeamModel(), MemoryTaskStore()
    task = AgentTask("plan", dependencies(model), store)
    first = await task.run("plan:offline:edit", payload())
    edited = json.loads(json.dumps(first.state["plan"]))
    edited["stages"][0]["days"][0]["tasks"][0]["title"] += "（用户调整）"
    before = sum(req.feature == "agent_reviewer" for req in model.requests)
    result = await task.run("plan:offline:edit", resume={"approved": True, "plan": edited})
    assert result.status == "completed"
    assert sum(req.feature == "agent_reviewer" for req in model.requests) > before
    assert result.state["plan_review"]["draft_digest"] == plan_digest(result.state["plan"])


async def test_cancelled_child_can_restart_and_lost_running_child_is_readmitted():
    model, store = TeamModel(block=True), MemoryTaskStore()
    record = TaskRecord(state=payload(), status="running")
    first = AgentCollaboration(dependencies(model), store, record, "plan:offline:readmit", lambda _: None)
    spec = {"tasks": [{"role": "researcher", "name": "research", "instructions": "收集资料"}]}
    task_id = (await first.delegate(spec))["tasks"][0]["id"]
    await asyncio.wait_for(model.started.wait(), 20)
    await first.close()
    model.block = False
    # A process loss can leave the parent roster running while the child has
    # a cancelled checkpoint. Recovery must reconcile both records.
    record.state["team_tasks"][task_id]["status"] = "running"
    second = AgentCollaboration(dependencies(model), store, record, "plan:offline:readmit", lambda _: None)
    result = await second.collect({"task_ids": [task_id]})
    assert result["tasks"][0]["status"] == "completed"
    assert len(record.state["team_tasks"]) == 1
    await second.close()


async def test_global_child_limit_applies_across_parent_tasks_and_budget_caps_admission():
    from eduagents_core.gateway.base import BudgetExceeded, ChatRequest
    from eduagents_core.harness.collaboration import TeamBudgetGateway

    model, store = TeamModel(block=True), MemoryTaskStore()
    store.child_slots = asyncio.Semaphore(1)
    teams = [AgentCollaboration(dependencies(model), store, TaskRecord(state=payload(), status="running"),
                                f"plan:offline:global-{i}", lambda _: None) for i in range(2)]
    spec = {"tasks": [{"role": "researcher", "name": "research", "instructions": "收集资料"}]}
    try:
        for team in teams:
            await team.delegate(spec)
        await asyncio.wait_for(model.started.wait(), 20)
        await asyncio.sleep(0)
        assert sum(req.feature == "agent_researcher" for req in model.requests) == 1
    finally:
        for team in teams:
            await team.close()
    gateway = TeamBudgetGateway(model, {}, dependencies(model, agent_collaboration_max_calls=12).settings)
    for _ in range(12):
        await gateway.reserve()
    with pytest.raises(BudgetExceeded):
        await gateway.chat(ChatRequest(messages=()))


async def test_shared_budget_reservation_is_persisted_before_the_provider_call():
    from eduagents_core.gateway.base import ChatRequest
    from eduagents_core.harness.collaboration import TeamBudgetGateway

    saved = []
    state = {}
    async def checkpoint():
        saved.append(dict(state["team_budget"]))
    class FailingModel:
        async def chat(self, request):
            assert saved[-1]["calls"] == 1
            raise GatewayError("TEST_FAILURE", "调用失败")
    gateway = TeamBudgetGateway(FailingModel(), state, dependencies(TeamModel()).settings, checkpoint)
    with pytest.raises(GatewayError, match="TEST_FAILURE"):
        await gateway.chat(ChatRequest(messages=()))
    assert state["team_budget"]["calls"] == saved[-1]["calls"] == 1
