"""A real SDK activity resumes a PostgreSQL confirmation after runtime restart."""

import json
import uuid

from eduagents_api.harness_runtime import HarnessRuntime
from eduagents_core.config import get_settings
from eduagents_core.workflows.state import WorkflowDeps

from .conftest import requires_services

pytestmark = requires_services


class FakeGateway:
    def __init__(self):
        self.features = []

    async def chat(self, req):
        from eduagents_core.gateway.base import ChatResult

        self.features.append(req.feature)
        if req.feature == "quiz_blueprint":
            data = {
                "items": [{"kc_name": "加法", "type": "single", "difficulty": "easy"}],
                "summary": "基础练习",
            }
        elif req.feature == "quiz_verify":
            data = {"issues": []}
        else:
            data = {
                "questions": [
                    {
                        "type": "single",
                        "stem": "1+1=?",
                        "options": ["2", "3"],
                        "answer": "A",
                        "explanation": "两个一相加得二",
                        "kc_names": ["加法"],
                        "difficulty": "easy",
                        "source_hint": None,
                    }
                ]
            }
        return ChatResult(json.dumps(data), "fake", req.tier.value, 1, 1, 0, 0, "stub")


async def test_restart_and_duplicate_resume_do_not_recreate_quiz(client):
    from eduagents_api.db import tenant_session
    from eduagents_api.deps import AuthContext
    from eduagents_api.models import Question
    from eduagents_api.services import finalize_quiz
    from sqlalchemy import func, select

    # Register through the same authenticated lifecycle as production.
    email = f"harness-recovery-{uuid.uuid4().hex[:12]}@test.eduagents.cn"
    registered = await client.post(
        "/api/auth/register", json={"email": email, "password": "pass-12345"}
    )
    assert registered.status_code == 201
    owner = registered.json()["id"]
    local_id = str(uuid.uuid4())
    thread = HarnessRuntime.thread_id("quiz", owner, local_id)
    settings = get_settings()
    gateway = FakeGateway()
    deps = WorkflowDeps(settings=settings, gateway=gateway)
    first = HarnessRuntime(settings, deps)
    await first.start()
    try:
        events = [
            event
            async for event in first.astream_updates(
                "quiz",
                thread,
                {
                    "tenant_id": owner,
                    "user_id": owner,
                    "request_id": uuid.uuid4().hex,
                    "topic": "导数",
                    "num_questions": 1,
                    "difficulty_mix": {"easy": 1},
                    "confirmation_mode": "required",
                    "evidence_policy": "mastery_eligible",
                },
            )
        ]
        assert events[-1]["kind"] == "interrupt"
        assert gateway.features.count("quiz_blueprint") == 1
    finally:
        await first.stop()
    second = HarnessRuntime(settings, deps)
    await second.start()
    try:
        events = [
            event
            async for event in second.astream_updates(
                "quiz", thread, None, resume={"approved": True}
            )
        ]
        assert events[-1]["kind"] == "end"
        state = events[-1]["state"]
        assert state["blueprint_confirmed"] and len(state["questions"]) == 1
        assert gateway.features.count("quiz_blueprint") == 1
        model_calls = len(gateway.features)
        replay = [
            event
            async for event in second.astream_updates(
                "quiz", thread, None, resume={"approved": True}
            )
        ]
        assert replay == [{"kind": "end", "state": state}]
        assert len(gateway.features) == model_calls
        ctx = AuthContext(
            user_id=uuid.UUID(owner), tenant_id=uuid.UUID(owner), role="student", jti="test"
        )
        saved = await finalize_quiz(ctx, local_id, state)
        assert await finalize_quiz(ctx, local_id, state) == saved
        async with tenant_session(owner) as session:
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(Question)
                    .where(Question.quiz_id == uuid.UUID(local_id))
                )
                == 1
            )
    finally:
        await second.stop()


async def test_quiz_read_export_and_finalize_preserve_generated_order(client):
    from eduagents_api.db import tenant_session
    from eduagents_api.deps import AuthContext
    from eduagents_api.models import Question, Quiz
    from eduagents_api.routers.quiz import get_quiz
    from eduagents_api.routers.teacher import _load_quiz
    from eduagents_api.services import finalize_quiz

    email = f"harness-order-{uuid.uuid4().hex[:12]}@test.eduagents.cn"
    response = await client.post(
        "/api/auth/register", json={"email": email, "password": "pass-12345"}
    )
    assert response.status_code == 201
    owner = uuid.UUID(response.json()["id"])
    quiz_id = uuid.uuid4()
    # Both physical insertion and UUID order oppose the generated order.
    question_ids = sorted([uuid.uuid4(), uuid.uuid4()], reverse=True)
    async with tenant_session(owner) as session:
        session.add(
            Quiz(id=quiz_id, tenant_id=owner, user_id=owner, title="有序题卷", status="ready")
        )
        await session.flush()
        for ordinal in (1, 0):
            session.add(
                Question(
                    id=question_ids[ordinal],
                    tenant_id=owner,
                    quiz_id=quiz_id,
                    type="single",
                    stem=f"题目 {ordinal + 1}",
                    options_json=["2", "3"],
                    answer="A",
                    explanation="",
                    kc_ids=[],
                    difficulty="easy",
                    source_meta_json={"quiz_ordinal": ordinal},
                )
            )
            await session.flush()
    ctx = AuthContext(user_id=owner, tenant_id=owner, role="teacher", jti="test")
    expected = [str(question_id) for question_id in question_ids]
    detail = await get_quiz(quiz_id, ctx)
    assert [question["id"] for question in detail["questions"]] == expected
    _quiz, exported = await _load_quiz(ctx, quiz_id)
    assert [str(question.id) for question in exported] == expected
    state = {"questions": [{"type": "single", "stem": "旧快照", "answer": "A"}]}
    replay = await finalize_quiz(ctx, str(quiz_id), state)
    assert replay["questions"] == detail["questions"]
