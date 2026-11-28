"""Durable native teams, tenant boundaries and cancellation on real PostgreSQL."""

import asyncio
import uuid
from dataclasses import replace

import pytest
from eduagents_api.db import tenant_session
from eduagents_api.harness_runtime import HarnessRuntime
from eduagents_api.models import HarnessSession
from eduagents_core.education.prompts import load_prompt_bundle
from eduagents_core.harness.task import TaskRecord
from sqlalchemy import select

from tests.unit.test_agent_collaboration import TeamModel, payload

from .conftest import requires_services
from .test_insights_flow import _register_login

pytestmark = requires_services


def runtime_for(client, model):
    original = client._transport.app.state.harness
    settings = original.settings.model_copy(update={
        "education_prompt_version": "edu-v2", "plan_generation_mode": "team",
    })
    return HarnessRuntime(settings, replace(original.deps, settings=settings, gateway=model,
                                            memory=None, prompt_bundle=load_prompt_bundle("edu-v2")))


async def test_persisted_team_survives_runtime_restart_and_owned_activity_and_plan_lineage(client):
    uid, headers = await _register_login(client, "team-restart")
    _, foreign = await _register_login(client, "team-foreign")
    local_id = str(uuid.uuid4())
    thread = HarnessRuntime.thread_id("plan", uid, local_id)
    first = runtime_for(client, TeamModel())
    await first.start()
    try:
        result = await first.run_task("plan", thread, {**payload(), "user_id": uid, "tenant_id": uid})
        assert result.status == "awaiting_confirmation"
        async with tenant_session(uid) as session:
            children = (await session.scalars(select(HarnessSession).where(HarnessSession.parent_thread_id == thread))).all()
            assert {c.kind for c in children} == {"researcher", "planner", "reviewer"}
            assert all(c.agent_name and c.native_archive for c in children)
            assert len({c.native_session_id for c in children}) == 3
        activity = await client.get(f"/api/threads/{thread}/agents", headers=headers)
        assert activity.status_code == 200 and activity.json()["review"]["approved"]
        assert (await client.get(f"/api/threads/{thread}/agents", headers=foreign)).status_code == 404
        child = children[0].thread_id
        assert (await client.post(f"/api/threads/{child}/retry", headers=headers, json={})).status_code == 404
    finally:
        await first.stop()
    model, second = TeamModel(), None
    second = runtime_for(client, model)
    await second.start()
    try:
        final = await second.run_task("plan", thread, resume={"approved": True})
        assert final.status == "completed" and final.native_session_id == result.native_session_id
        assert all(req.feature == "agent_plan_coordinator" for req in model.requests)
        quiz_thread = HarnessRuntime.thread_id("quiz", uid, str(uuid.uuid4()))
        quiz = await second.task_for("quiz", quiz_thread, {"plan_id": local_id})
        assert quiz.parent_thread_id == thread and quiz.agent_name.startswith("quiz-")
        # The persisted parent, not a model-supplied roster, governs lineage.
        await second._store.save(quiz_thread, TaskRecord(state={"user_id": uid, "_parent_thread_id": thread,
                                                              "_agent_name": quiz.agent_name}, status="completed"))
        assert (await second.learning_children(thread))[0]["id"] == quiz_thread
    finally:
        await second.stop()


async def test_cancel_and_retry_preserve_admitted_identity_and_completed_records(client):
    uid, headers = await _register_login(client, "team-cancel")
    thread = HarnessRuntime.thread_id("plan", uid, str(uuid.uuid4()))
    app = client._transport.app
    original = app.state.harness
    model, runtime = TeamModel(block=True), None
    runtime = runtime_for(client, model)
    await runtime.start()
    app.state.harness = runtime
    job = asyncio.create_task(runtime.run_task("plan", thread, {**payload(), "user_id": uid, "tenant_id": uid}))
    try:
        await asyncio.wait_for(model.started.wait(), 30)
        cancelled = await client.post(f"/api/threads/{thread}/cancel", headers=headers)
        assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
        with pytest.raises(asyncio.CancelledError):
            await job
        assert not runtime._active
        assert all(v["status"] == "cancelled" for v in (await runtime.load_task(thread)).state["team_tasks"].values())
        model.block = False
        retried = await client.post(f"/api/threads/{thread}/retry", headers=headers, json={"user_id": "foreign"})
        assert retried.status_code == 200 and "event: interrupt" in retried.text
        record = await runtime.load_task(thread)
        assert record.status == "awaiting_confirmation" and record.state["user_id"] == uid
        assert (await client.post(f"/api/threads/{thread}/retry", headers=headers, json={})).status_code == 409
    finally:
        if not job.done():
            job.cancel()
        await asyncio.gather(job, return_exceptions=True)
        app.state.harness = original
        await runtime.stop()
