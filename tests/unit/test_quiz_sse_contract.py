"""Quiz SSE terminal contract tests."""

from __future__ import annotations

import json
import uuid

import pytest
from eduagents_api.deps import AuthContext
from eduagents_api.routers import threads


class EndRuntime:
    def __init__(self, state: dict):
        self.state = state

    def parse_thread_id(self, thread_id: str):
        kind, user_id, local_id = thread_id.split(":", 2)
        return kind, user_id, local_id

    async def astream_updates(self, kind, thread_id, payload, resume=None):
        yield {"kind": "end", "state": self.state}


def auth_context() -> AuthContext:
    user_id = uuid.uuid4()
    return AuthContext(
        user_id=user_id,
        tenant_id=user_id,
        role="student",
        jti="test-jti",
    )


async def collect_events(response) -> list[dict]:
    events = []
    async for event in response.body_iterator:
        events.append(event)
    return events


def event_data(events: list[dict], event_name: str) -> dict:
    raw = next(event["data"] for event in events if event["event"] == event_name)
    return json.loads(raw)


@pytest.mark.asyncio
async def test_quiz_failure_done_v2_classifies_user_cancellation_at_confirmation():
    state = {
        "blueprint_confirmed": False,
        "terminal_outcome": "cancelled_by_user",
        "failure_code": "QUIZ_CANCELLED_BY_USER",
        "retry_count": 0,
    }
    response = await threads.stream_graph_run(
        EndRuntime(state),
        "quiz",
        "quiz:user:local",
        auth_context(),
        None,
    )

    done = event_data(await collect_events(response), "done")

    assert done["schema_version"] == 2
    assert done["outcome"] == "cancelled_by_user"
    assert done["result"] is None
    assert done["error"] == {
        "code": "QUIZ_CANCELLED_BY_USER",
        "message": "已取消本次出题",
        "stage": "confirm",
        "retryable": False,
        "attempts": 0,
    }


@pytest.mark.asyncio
async def test_quiz_selection_trace_is_qualitative_and_bounded():
    response = await threads.stream_graph_run(
        EndRuntime({
            "terminal_outcome": "generation_failed",
            "retry_count": 3,
        }),
        "quiz",
        "quiz:user:local",
        auth_context(),
        None,
        trace_prelude=[
            {
                "node": "weak_points",
                "event": "weak_points_selected",
                "mode": "assessment",
                "count": 3,
                "kc_names": ["极限", "导数"],
                "difficulty_mix": {"easy": 1, "mid": 2, "hard": 0},
                "rationale": "先做一次快速验证摸清起点",
            }
        ],
    )

    events = await collect_events(response)
    trace = event_data(events, "trace_event")
    assert set(trace) == {
        "node",
        "event",
        "mode",
        "count",
        "kc_names",
        "difficulty_mix",
        "rationale",
    }
    assert "p_mastery" not in trace
    assert "confidence" not in trace
    assert "mastery_band" not in trace


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "code", "stage"),
    [
        ("generation_failed", "QUIZ_GENERATION_EMPTY", "generate"),
        ("validation_failed", "QUIZ_VALIDATION_FAILED", "validate"),
    ],
)
async def test_quiz_failure_done_v2_has_non_empty_classification(outcome, code, stage):
    state = {
        "terminal_outcome": outcome,
        "retry_count": 3,
        "ever_generated_nonempty": outcome == "validation_failed",
    }
    response = await threads.stream_graph_run(
        EndRuntime(state),
        "quiz",
        "quiz:user:local",
        auth_context(),
        None,
    )

    done = event_data(await collect_events(response), "done")

    assert done["schema_version"] == 2
    assert done["outcome"] == outcome
    assert done["result"] is None
    assert done["error"]["code"] == code
    assert done["error"]["stage"] == stage
    assert done["error"]["retryable"] is True
    assert done["error"]["attempts"] == 3


@pytest.mark.asyncio
async def test_quiz_success_done_v2_contains_persisted_result(monkeypatch):
    async def persist_quiz(ctx, local_id, state):
        return {"quiz_id": local_id, "questions": [{"id": "q1"}]}

    monkeypatch.setattr(threads, "finalize_quiz", persist_quiz)
    state = {
        "blueprint_confirmed": True,
        "terminal_outcome": "succeeded",
        "questions": [{"safe": True}],
    }
    response = await threads.stream_graph_run(
        EndRuntime(state),
        "quiz",
        "quiz:user:local",
        auth_context(),
        None,
    )

    done = event_data(await collect_events(response), "done")

    assert done == {
        "thread_id": "quiz:user:local",
        "schema_version": 2,
        "outcome": "succeeded",
        "result": {"quiz_id": "local", "questions": [{"id": "q1"}]},
        "error": None,
    }


@pytest.mark.asyncio
async def test_quiz_success_never_emits_done_when_finalize_returns_empty_result(monkeypatch):
    async def empty_finalize(ctx, local_id, state):
        return {}

    monkeypatch.setattr(threads, "finalize_quiz", empty_finalize)
    state = {
        "blueprint_confirmed": True,
        "terminal_outcome": "succeeded",
        "questions": [{"stem": "safe test payload"}],
    }
    response = await threads.stream_graph_run(
        EndRuntime(state),
        "quiz",
        "quiz:user:local",
        auth_context(),
        None,
    )

    events = await collect_events(response)
    assert not [event for event in events if event["event"] == "done"]
    error = event_data(events, "error")
    assert error["code"] == "QUIZ_FINALIZE_INVALID"


@pytest.mark.asyncio
async def test_quiz_kc_business_error_keeps_its_stable_sse_code(monkeypatch):
    from eduagents_api.services import QuizKCError

    async def reject_finalize(ctx, local_id, state):
        raise QuizKCError("QUIZ_KC_NOT_READY")

    monkeypatch.setattr(threads, "finalize_quiz", reject_finalize)
    response = await threads.stream_graph_run(
        EndRuntime(
            {
                "blueprint_confirmed": True,
                "terminal_outcome": "succeeded",
                "questions": [{"safe": True}],
            }
        ),
        "quiz",
        "quiz:user:local",
        auth_context(),
        None,
    )

    events = await collect_events(response)
    assert not [event for event in events if event["event"] == "done"]
    assert event_data(events, "error") == {
        "code": "QUIZ_KC_NOT_READY",
        "message": "练习知识点尚未准备好，请稍后重试",
    }
