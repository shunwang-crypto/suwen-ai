"""Exercise the real bundled DSH executable with deterministic gateway/tool fakes."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path

import pytest
from eduagents_core.gateway.base import (
    BudgetExceeded,
    ChatMessage,
    ChatRequest,
    ChatResult,
    GatewayError,
    StreamDelta,
)
from eduagents_core.harness.gateway import HarnessGateway


class Gateway:
    def __init__(self, replies=None):
        self.requests = []
        self.replies = list(replies or [])
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def chat(self, req):
        self.requests.append(req)
        reply = self.replies.pop(0) if self.replies else "answer"
        if isinstance(reply, BaseException):
            raise reply
        if isinstance(reply, ChatResult):
            return reply
        return ChatResult(reply, "fake", req.tier.value, 3, 5, 0, 0, "stub", finish_reason="stop")

    async def stream_chat(self, req):
        self.started.set()
        if self.replies == ["block"]:
            try:
                await asyncio.Event().wait()
            finally:
                self.cancelled.set()
        result = await self.chat(req)
        yield StreamDelta(result.text[:2])
        yield StreamDelta(result.text[2:])
        yield result


def request(text="hello"):
    return ChatRequest(
        messages=(ChatMessage("system", "host policy"), ChatMessage("user", text)),
        user_id="user-a",
        tenant_id="tenant-a",
        request_id="request-a",
        json_mode=True,
        max_tokens=123,
        temperature=0.2,
        feature="quiz_generate",
    )


def call(name="web_search", args=None):
    return ChatResult(
        "",
        "fake",
        "cheap",
        3,
        5,
        0,
        0,
        "stub",
        reasoning_content="private reasoning",
        tool_calls=(
            {
                "id": "call-1",
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": json.dumps(args or {"queries": ["source"]}),
                },
            },
        ),
    )


SCHEMA = [
    {
        "name": "web_search",
        "description": "Read educational evidence.",
        "parameters": {
            "type": "object",
            "properties": {"queries": {"type": "array", "items": {"type": "string"}}},
            "required": ["queries"],
            "additionalProperties": False,
        },
    }
]


async def test_real_sdk_preserves_gateway_contract_and_isolates_stages():
    gateway = Gateway(["one", "two"])
    async with HarnessGateway(gateway) as harness:
        first = await harness.chat(request("/goal do not execute a command"))
        second = await harness.chat(request("second task"))
        home = Path(harness._home.name)
        process = harness._harness.client._proc
    assert (first.text, second.text) == ("one", "two")
    assert [replace(req, billing_reference="") for req in gateway.requests] == [
        request("/goal do not execute a command"),
        request("second task"),
    ]
    assert gateway.requests[0].billing_reference != gateway.requests[1].billing_reference
    assert not home.exists()
    assert process.poll() is not None


async def test_real_sdk_streams_before_finishing_and_preserves_truncation():
    gateway = Gateway(
        [
            "stream",
            ChatResult("cut", "fake", "standard", 1, 2, 0, 0, "stub", finish_reason="length"),
        ]
    )
    async with HarnessGateway(gateway) as harness:
        events = [item async for item in harness.stream_chat(request())]
        result = await harness.chat(request())
    assert "".join(item.text for item in events if isinstance(item, StreamDelta)) == "stream"
    assert isinstance(events[-1], ChatResult)
    assert result.finish_reason == "length"


async def test_native_tool_loop_preserves_calls_results_and_reasoning_for_byok():
    gateway = Gateway([call(), "finished"])
    executed = []

    async def search(args):
        executed.append(args)
        return {"evidence": "sanitized source"}

    async with HarnessGateway(gateway) as harness:
        result = await harness.run_tools(request(), SCHEMA, {"web_search": search})
    assert result.text == "finished"
    assert executed == [{"queries": ["source"]}]
    followup = gateway.requests[1]
    assert followup.messages[-1].role == "tool"
    assert followup.messages[-1].tool_call_id == "call-1"
    assert "sanitized source" in followup.messages[-1].content
    assert followup.messages[-2].reasoning_content == "private reasoning"
    assert followup.tenant_id == "tenant-a" and followup.user_id == "user-a"


async def test_native_tool_permission_does_not_expose_default_coding_tools():
    gateway = Gateway([call("bash")])

    async def unused(_args):
        pytest.fail("unauthorized tool must not execute")

    async with HarnessGateway(gateway) as harness:
        with pytest.raises(GatewayError, match="HARNESS_TOOL_DENIED"):
            await harness.run_tools(request(), SCHEMA, {"web_search": unused})
    assert [tool["function"]["name"] for tool in gateway.requests[0].tools] == ["web_search"]


async def test_budget_failure_survives_sdk_transport():
    gateway = Gateway([BudgetExceeded(retry_after_seconds=123)])
    async with HarnessGateway(gateway) as harness:
        with pytest.raises(BudgetExceeded) as error:
            await harness.chat(request())
    assert error.value.retry_after_seconds == 123


async def test_cancellation_reaps_runtime_and_cancels_provider_work():
    gateway = Gateway(["block"])
    async with HarnessGateway(gateway) as harness:

        async def run():
            return [item async for item in harness.stream_chat(request())]

        task = asyncio.create_task(run())
        await asyncio.wait_for(gateway.started.wait(), 10)
        process = harness._harness.client._proc
        home = Path(harness._home.name)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert gateway.cancelled.is_set()
    assert process.poll() is not None
    assert not home.exists()


async def test_activity_timeout_cancels_provider_and_reaps_runtime():
    gateway = Gateway(["block"])
    async with HarnessGateway(gateway, timeout=2) as harness:
        with pytest.raises(GatewayError, match="HARNESS_TIMEOUT"):
            _events = [item async for item in harness.stream_chat(request())]
    assert gateway.cancelled.is_set()


async def test_native_loop_records_replays_and_meters_each_model_call(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from eduagents_core.config import Settings
    from eduagents_core.gateway import OpenAIGateway
    from openai.types.chat import ChatCompletion

    provider_calls = []
    events = []

    async def create(**kwargs):
        provider_calls.append(kwargs)
        has_result = any(m["role"] == "tool" for m in kwargs["messages"])
        message = {
            "role": "assistant",
            "content": "finished" if has_result else "",
            "reasoning_content": "private reasoning",
        }
        if not has_result:
            message["tool_calls"] = list(call().tool_calls)
        return ChatCompletion.model_validate(
            {
                "id": "response",
                "object": "chat.completion",
                "created": 0,
                "model": "fake",
                "choices": [
                    {
                        "index": 0,
                        "message": message,
                        "finish_reason": "stop" if has_result else "tool_calls",
                    }
                ],
                "usage": {"prompt_tokens": 3, "completion_tokens": 5, "total_tokens": 8},
            }
        )

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

    async def sink(event):
        events.append(event)

    settings = Settings(
        _env_file=None,
        gateway_mode="record",
        standard_api_key="test-key",
        model_routing_enabled=False,
        gateway_cassette_dir=str(tmp_path),
    )
    gateway = OpenAIGateway(settings, usage_sink=sink)
    monkeypatch.setattr(gateway, "_client", lambda _tier: client)

    async def search(_args):
        return {"evidence": "source"}

    async with HarnessGateway(gateway) as harness:
        recorded = await harness.run_tools(request(), SCHEMA, {"web_search": search})
    assert len(provider_calls) == 2 and len(events) == 2
    assert len({event.billing_reference for event in events}) == 2
    assert all(event.request_id == "request-a" and event.prompt_tokens == 3 for event in events)
    replay_gateway = OpenAIGateway(settings.model_copy(update={"gateway_mode": "replay"}))
    async with HarnessGateway(replay_gateway) as harness:
        replayed = await harness.run_tools(request(), SCHEMA, {"web_search": search})
    assert replayed.text == recorded.text == "finished"
    assert replayed.mode == "replay" and replayed.cost_cny == 0
    assert len(provider_calls) == 2


async def test_native_byok_tool_rounds_do_not_use_platform_key_or_shared_cassettes(
    tmp_path, monkeypatch
):
    from types import SimpleNamespace

    from eduagents_core.config import Settings
    from eduagents_core.gateway import CredentialSource, OpenAIGateway, ResolvedCredential
    from openai.types.chat import ChatCompletion

    keys = []
    events = []

    class Client:
        def __init__(self, **kwargs):
            keys.append(kwargs["api_key"])
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

        async def create(self, **kwargs):
            has_result = any(m["role"] == "tool" for m in kwargs["messages"])
            message = {
                "role": "assistant",
                "content": "finished" if has_result else "",
                "reasoning_content": "private reasoning",
            }
            if not has_result:
                message["tool_calls"] = list(call().tool_calls)
            return ChatCompletion.model_validate(
                {
                    "id": "response",
                    "object": "chat.completion",
                    "created": 0,
                    "model": "fake",
                    "choices": [
                        {
                            "index": 0,
                            "message": message,
                            "finish_reason": "stop" if has_result else "tool_calls",
                        }
                    ],
                    "usage": {"prompt_tokens": 3, "completion_tokens": 5, "total_tokens": 8},
                }
            )

        async def close(self):
            pass

    async def resolve(_req):
        return ResolvedCredential(
            source=CredentialSource.user,
            provider="deepseek",
            api_key="user-key",
            base_url="https://test.invalid/v1",
            model="fake",
        )

    async def sink(event):
        events.append(event)

    monkeypatch.setattr("eduagents_core.gateway.openai_gateway.AsyncOpenAI", Client)
    gateway = OpenAIGateway(
        Settings(
            _env_file=None,
            gateway_mode="record",
            standard_api_key="platform-key",
            model_routing_enabled=False,
            gateway_cassette_dir=str(tmp_path),
        ),
        credential_resolver=resolve,
        usage_sink=sink,
    )

    async def search(_args):
        return {"evidence": "source"}

    async with HarnessGateway(gateway) as harness:
        await harness.run_tools(request(), SCHEMA, {"web_search": search})
    assert keys == ["user-key", "user-key"]
    assert len(events) == 2 and all(event.credential_source == "user" for event in events)
    assert all(event.platform_billed_cost_cny == 0 for event in events)
    assert not list(tmp_path.rglob("*.json"))


@pytest.mark.parametrize("repairs", [True, False])
async def test_required_tool_allows_one_native_continuation(repairs):
    import uuid

    gateway = Gateway(["finished in prose", call(), "submitted"] if repairs else ["prose", "more prose"])
    executed = []

    async def search(args):
        executed.append(args)
        return {"accepted": True}

    async with HarnessGateway(gateway, max_calls=3) as harness:
        await harness.run_task(
            request(), session_id=uuid.uuid4().hex, archive={}, prompt="use the education tool",
            schemas=SCHEMA, handlers={"web_search": search}, is_complete=lambda: bool(executed),
        )
    assert len(executed) == int(repairs)
    assert len(gateway.requests) == (3 if repairs else 2)
    assert any(message.role == "assistant" and "prose" in message.content
               for message in gateway.requests[1].messages)
    assert len({req.billing_reference for req in gateway.requests}) == len(gateway.requests)


async def test_local_repair_dispatches_json_through_the_native_tool():
    import uuid

    gateway = Gateway(["prose only", '{"queries":["source"]}', "finished"])
    executed = []

    async def search(args):
        executed.append(args)
        return {"accepted": True}

    async with HarnessGateway(gateway, max_calls=3) as harness:
        await harness.run_task(
            request(), session_id=uuid.uuid4().hex, archive={}, prompt="ground the answer",
            schemas=SCHEMA, handlers={"web_search": search}, is_complete=lambda: bool(executed),
            repair_tool_call=lambda: ("web_search", {"question": "bounded question"}, SCHEMA[0]["parameters"]),
        )
    assert executed == [{"queries": ["source"]}]
    repair = gateway.requests[1]
    assert repair.json_mode and not repair.tools and "bounded question" in repair.messages[-1].content
    assert repair.response_schema == SCHEMA[0]["parameters"]
