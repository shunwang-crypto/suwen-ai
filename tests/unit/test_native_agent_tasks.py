"""Real DSH runtime drives educational tools and restores native sessions."""

import asyncio
import base64
import json
from dataclasses import replace

import pytest
from eduagents_core.config import Settings
from eduagents_core.education.context import EducationDeps
from eduagents_core.gateway.base import (
    ChatMessage,
    ChatRequest,
    ChatResult,
    GatewayError,
    StreamDelta,
)
from eduagents_core.harness.gateway import HarnessGateway
from eduagents_core.harness.task import AgentTask, MemoryTaskStore

from .test_dsh_runtime import Gateway


class ScriptedModel:
    def __init__(self, steps):
        self.steps = list(steps)
        self.requests = []

    async def chat(self, req):
        self.requests.append(req)
        if not req.feature.startswith("agent_"):
            return ChatResult(
                json.dumps({"supported": True, "issues": []}),
                "fake",
                req.tier.value,
                1,
                1,
                0,
                0,
                "stub",
            )
        step = self.steps.pop(0) if self.steps else "finished"
        calls = ()
        text = step if isinstance(step, str) else ""
        if isinstance(step, tuple):
            name, args = step
            calls = (
                {
                    "id": f"call-{len(self.requests)}",
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
                },
            )
        return ChatResult(text, "fake", req.tier.value, 1, 1, 0, 0, "stub", tool_calls=calls)


def deps(model, **kw):
    return EducationDeps(Settings(_env_file=None), model, **kw)


QUESTION = {
    "type": "single",
    "stem": "1+1=?",
    "options": ["1", "2", "3", "4"],
    "answer": "B",
    "explanation": "两个一相加为二",
    "kc_names": ["加法"],
    "difficulty": "easy",
}
BLUEPRINT = {
    "items": [{"kc_name": "加法", "type": "single", "difficulty": "easy"}],
    "summary": "加法练习",
}


async def test_native_quiz_confirmation_restores_same_conversation_and_is_idempotent():
    store = MemoryTaskStore()
    first_model = ScriptedModel([("submit_blueprint", {"blueprint": BLUEPRINT}), "finished"])
    first = await AgentTask("quiz", deps(first_model), store).run(
        "quiz-owner-task",
        {"topic": "加法", "num_questions": 1, "user_id": "owner", "confirmation_mode": "required"},
    )
    assert first.status == "awaiting_confirmation"
    assert first.native_archive
    assert not first.state.get("questions")
    assert "submit_quiz" not in {t["function"]["name"] for t in first_model.requests[0].tools}
    second_model = ScriptedModel([("submit_quiz", {"questions": [QUESTION]}), "finished"])
    second = await AgentTask("quiz", deps(second_model), store).run(
        "quiz-owner-task", resume={"approved": True}
    )
    assert second.status == "completed"
    assert second.native_session_id == first.native_session_id
    assert second.state["questions"][0]["answer"] == "B"
    assert any(
        m.role == "tool" and "awaiting_confirmation" in m.content
        for m in second_model.requests[0].messages
    )
    calls = len(second_model.requests)
    replay = await AgentTask("quiz", deps(second_model), store).run(
        "quiz-owner-task", resume={"approved": True}
    )
    assert replay.state == second.state
    assert len(second_model.requests) == calls


async def test_quiz_generation_tool_is_unavailable_before_confirmation():
    model = ScriptedModel([("submit_quiz", {"questions": [QUESTION]})])
    with pytest.raises(GatewayError, match="HARNESS_TOOL_DENIED"):
        await AgentTask("quiz", deps(model)).run("unconfirmed", {"num_questions": 1})


async def test_native_loop_repairs_invalid_question_without_a_host_phase_loop():
    bad = {**QUESTION, "options": ["1", "2"]}
    model = ScriptedModel(
        [
            ("submit_blueprint", {"blueprint": BLUEPRINT}),
            ("submit_quiz", {"questions": [bad]}),
            ("submit_quiz", {"questions": [QUESTION]}),
            "finished",
        ]
    )
    record = await AgentTask("quiz", deps(model)).run(
        "repair", {"num_questions": 1, "topic": "加法", "confirmation_mode": "auto"}
    )
    assert record.status == "completed"
    assert len(record.state["questions"][0]["options"]) == 4
    assert any(
        m.role == "tool" and "options" in m.content and ("errors" in m.content)
        for req in model.requests
        if req.feature.startswith("agent_")
        for m in req.messages
    )


async def test_native_question_answer_can_search_read_and_submit_in_one_session():

    async def web(query):
        return [{"title": "教材", "url": "https://example.com/math", "content": "1+1=2"}]

    model = ScriptedModel(
        [
            ("web_search", {"queries": ["加法"]}),
            ("read_material", {"ids": ["https://example.com/math"]}),
            (
                "submit_answer",
                {"answer": "两个一相加为二。", "source_ids": ["https://example.com/math"]},
            ),
            "finished",
        ]
    )
    record = await AgentTask("qa", deps(model, web_search=web)).run(
        "qa-owner", {"query": "1+1?", "requested_route": "web"}
    )
    assert record.status == "completed"
    assert record.state["route"] == "web"
    assert any(m.role == "tool" and "1+1=2" in m.content for m in model.requests[2].messages)


async def test_native_tutor_session_keeps_history_and_advances_hint_only_once_per_turn():
    store = MemoryTaskStore()
    model = ScriptedModel(
        [
            ("assess_student_turn", {"stuck_type": "unknown"}),
            ("assess_student_turn", {"stuck_type": "unknown"}),
            ("submit_tutor_reply", {"reply": "你先说说思路？"}),
            "finished",
        ]
    )
    first = await AgentTask("tutor", deps(model), store).run(
        "tutor-owner", {"problem": "1+1?", "messages": [{"role": "user", "content": "不会"}]}
    )
    assert first.state["hint_level"] == 0
    model.steps = [
        ("assess_student_turn", {"stuck_type": "calculation"}),
        ("submit_tutor_reply", {"reply": "先数出一个，再添一个。"}),
        "finished",
    ]
    before = len(model.requests)
    second = await AgentTask("tutor", deps(model), store).run(
        "tutor-owner", {"messages": [{"role": "user", "content": "还是不会"}]}
    )
    assert second.native_session_id == first.native_session_id
    assert second.state["hint_level"] == 1
    assert any(
        "你先说说思路" in json.dumps(m.to_wire(), ensure_ascii=False)
        for m in model.requests[before].messages
    )
    assert all(
        "绝对禁止给出最终答案" in req.messages[0].content
        for req in model.requests
        if req.feature == "agent_tutor"
    )


async def test_native_agent_must_submit_result_and_cannot_finish_with_unchecked_text():
    model = ScriptedModel(["I am done"])
    with pytest.raises(GatewayError, match="HARNESS_RESULT_REQUIRED"):
        await AgentTask("qa", deps(model)).run("unchecked", {"query": "hello"})


async def test_native_task_requires_tools_until_submission_is_accepted():
    model = ScriptedModel([
        ("submit_answer", {"answer": ""}),
        ("submit_answer", {"answer": "42"}),
        "finished",
    ])
    record = await AgentTask("qa", deps(model)).run("required", {"query": "answer 42"})
    assert record.status == "completed" and record.state["answer"] == "42"
    assert [request.tool_choice for request in model.requests] == ["required", "required", None]
    assert all(request.tools for request in model.requests[:2])
    assert not model.requests[-1].tools
    assert any(message.role == "tool" and '"accepted":false' in message.content.replace(" ", "")
               for message in model.requests[1].messages)


async def test_durable_native_chat_restores_text_and_never_archives_image_bytes():
    gateway = Gateway(["first answer", "second answer"])
    request = ChatRequest(
        messages=(
            ChatMessage("system", "teaching policy"),
            ChatMessage(
                "user",
                [
                    {"type": "text", "text": "解释图片"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64,c2VjcmV0LWltYWdl"},
                    },
                ],
            ),
        ),
        request_id="first",
        credential_policy="platform",
    )
    async with HarnessGateway(gateway) as runtime:
        runtime.configure_chat(
            request,
            session_id="0123456789abcdef0123456789abcdef",
            archive={},
            checkpoint=None,
            context_window=32768,
        )
        first = [e async for e in runtime.stream_chat(request)]
    archive = runtime.native_archive
    raw = "\n".join(base64.b64decode(v).decode() for v in archive.values())
    assert "c2VjcmV0LWltYWdl" not in raw and "data:image" not in raw
    assert "first answer" in raw
    assert gateway.requests[0].messages[-1].content == request.messages[-1].content
    second = replace(
        request,
        messages=(ChatMessage("system", "new policy"), ChatMessage("user", "继续")),
        request_id="second",
    )
    async with HarnessGateway(gateway) as runtime:
        runtime.configure_chat(
            second,
            session_id="0123456789abcdef0123456789abcdef",
            archive=archive,
            checkpoint=None,
            context_window=32768,
        )
        events = [e async for e in runtime.stream_chat(second)]
    assert events[-1].text == "second answer"
    assert any(
        m.role == "assistant" and "first answer" in m.content for m in gateway.requests[1].messages
    )
    assert first[-1].text == "first answer"


async def test_native_compaction_is_metered_without_education_tools_or_user_tokens():

    class Model(Gateway):
        async def chat(self, req):
            self.requests.append(req)
            text = "older turns summarized" if req.feature == "harness_compaction" else "a" * 4000
            from eduagents_core.gateway.base import ChatResult

            return ChatResult(text, "fake", req.tier.value, 2000, 1000, 0, 0, "stub")

    model = Model()
    archive = {}
    for turn in range(5):
        req = ChatRequest(
            messages=(
                ChatMessage("system", "policy"),
                ChatMessage("user", f"turn {turn}:" + "x" * 2500),
            ),
            request_id=str(turn),
            max_tokens=1000,
        )
        async with HarnessGateway(model) as runtime:
            runtime.configure_chat(
                req,
                session_id="fedcba9876543210fedcba9876543210",
                archive=archive,
                checkpoint=None,
                context_window=8192,
            )
            events = [e async for e in runtime.stream_chat(req)]
        archive = runtime.native_archive
        assert "older turns summarized" not in "".join(
            e.text for e in events if isinstance(e, StreamDelta)
        )
    compact = [r for r in model.requests if r.feature == "harness_compaction"]
    assert compact and all(not r.tools and r.max_tokens == 2048 for r in compact)
    assert len({r.billing_reference for r in model.requests}) == len(model.requests)


async def test_duplicate_request_and_parallel_invocation_preserve_one_artifact():
    store = MemoryTaskStore()
    model = ScriptedModel([("submit_answer", {"answer": "answer"}), "finished"])
    task = AgentTask("qa", deps(model), store)
    payload = {"query": "hello", "request_id": "same", "user_id": "owner"}
    first, second = await asyncio.gather(task.run("thread", payload), task.run("thread", payload))
    assert first.state == second.state and len(model.requests) == 2
    with pytest.raises(GatewayError, match="HARNESS_OWNER_MISMATCH"):
        await task.run("thread", {**payload, "request_id": "other", "user_id": "outsider"})


async def test_checked_result_survives_final_acknowledgment_failure():

    class Model(ScriptedModel):
        async def chat(self, req):
            if len(self.requests):
                raise GatewayError("OFFLINE", "offline")
            return await super().chat(req)

    model = Model([("submit_answer", {"answer": "checked"})])
    store = MemoryTaskStore()
    result = await AgentTask("qa", deps(model), store).run(
        "ack", {"query": "hello", "request_id": "r"}
    )
    assert result.status == "completed" and result.native_archive
    replay = await AgentTask("qa", deps(model), store).run(
        "ack", {"query": "hello", "request_id": "r"}
    )
    assert replay.state == result.state


async def test_failed_tutor_turn_can_resume_without_duplicate_hint_or_messages():
    from eduagents_core.gateway.base import BudgetExceeded

    class FailingModel(ScriptedModel):
        async def chat(self, req):
            if len(self.requests) == 1:
                raise BudgetExceeded()
            return await super().chat(req)

    store = MemoryTaskStore()
    payload = {
        "problem": "1+1?",
        "request_id": "retry",
        "user_id": "owner",
        "messages": [{"role": "user", "content": "不会"}],
    }
    model = FailingModel([("assess_student_turn", {"stuck_type": "concept"})])
    with pytest.raises(BudgetExceeded):
        await AgentTask("tutor", deps(model), store).run("recover-tutor", payload)
    failed = await store.load("recover-tutor")
    assert failed.status == "failed" and failed.native_archive
    assert failed.state["hint_level"] == 0
    retry_model = ScriptedModel(
        [
            ("assess_student_turn", {"stuck_type": "concept"}),
            ("submit_tutor_reply", {"reply": "先数一个"}),
            "finished",
        ]
    )
    recovered = await AgentTask("tutor", deps(retry_model), store).run("recover-tutor", payload)
    assert recovered.status == "completed" and recovered.state["hint_level"] == 0
    assert [m["role"] for m in recovered.state["messages"]] == ["user", "assistant"]
    with pytest.raises(GatewayError, match="HARNESS_OWNER_MISMATCH"):
        await AgentTask("tutor", deps(retry_model), store).run(
            "recover-tutor", {**payload, "user_id": "outsider"}
        )


@pytest.mark.parametrize(
    "archive",
    [
        {"../outside": "eA=="},
        {"/tmp/outside": "eA=="},
        {"sessions/scope/outsider/session.v4.jsonl": "eA=="},
        {"sessions/scope/0123456789abcdef0123456789abcdef/session.v4.jsonl": "invalid!"},
    ],
)
async def test_native_archive_rejects_traversal_other_session_and_invalid_bytes(archive):
    async with HarnessGateway(Gateway()) as runtime:
        with pytest.raises(GatewayError, match="HARNESS_SESSION_INVALID"):
            await runtime.run_task(
                ChatRequest(messages=()),
                session_id="0123456789abcdef0123456789abcdef",
                archive=archive,
                prompt="hello",
                schemas=[],
                handlers={},
            )
    assert runtime._home is None and runtime._runner is None


async def test_validation_failure_keeps_explicit_outcome_when_final_acknowledgment_fails():
    class Model(ScriptedModel):
        async def chat(self, req):
            if len(self.requests) == 4:
                raise GatewayError("OFFLINE", "offline")
            return await super().chat(req)

    bad = {**QUESTION, "answer": "E"}
    model = Model(
        [
            ("submit_blueprint", {"blueprint": BLUEPRINT}),
            *[("submit_quiz", {"questions": [bad]})] * 3,
        ]
    )
    record = await AgentTask("quiz", deps(model)).run(
        "invalid-terminal", {"num_questions": 1, "topic": "加法", "confirmation_mode": "auto"}
    )
    assert record.status == "failed"
    assert record.state["terminal_outcome"] == "validation_failed"
    assert record.state["failure_code"] == "QUIZ_VALIDATION_FAILED"
