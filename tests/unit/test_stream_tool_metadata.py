"""Tool streaming must preserve fragments, reasoning, source and usage."""

import asyncio
import json
from types import SimpleNamespace

import pytest
from eduagents_core.config import Settings
from eduagents_core.education.toolkit import schema
from eduagents_core.gateway import ChatMessage, ChatRequest, ChatResult, OpenAIGateway, StreamDelta
from eduagents_core.gateway.base import CredentialSource, ResolvedCredential
from eduagents_core.harness.gateway import HarnessGateway


def chunk(*, content=None, name=None, arguments=None, reasoning=None, finish=None, usage=None):
    calls = [SimpleNamespace(index=0, id="memory-call", function=SimpleNamespace(name=name, arguments=arguments))] if name or arguments else []
    delta = SimpleNamespace(content=content, tool_calls=calls, reasoning_content=reasoning)
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta, finish_reason=finish)], usage=usage)


class StreamingProvider:
    def __init__(self, **kwargs):
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    async def create(self, **kwargs):
        assert kwargs["stream"] and kwargs["tools"]

        async def response():
            yield chunk(content="先看记录。")
            yield chunk(name="search_learning_", arguments='{"query":"', reasoning="需要历史记录")
            yield chunk(name="memories", arguments='导数"}', finish="tool_calls")
            yield chunk(usage=SimpleNamespace(prompt_tokens=20, completion_tokens=30))

        return response()

    async def close(self):
        pass


@pytest.mark.parametrize("route", ["legacy", "routed", "byok"])
async def test_each_provider_stream_preserves_tool_metadata(monkeypatch, tmp_path, route):
    import eduagents_core.gateway.openai_gateway as module

    monkeypatch.setattr(module, "AsyncOpenAI", StreamingProvider)
    settings = Settings(_env_file=None, gateway_mode="live", gateway_max_retries=1,
                        gateway_cassette_dir=str(tmp_path), standard_api_key="fake",
                        model_routing_enabled=route == "routed", trenddao_api_key="fake")
    usage = []

    async def sink(event):
        usage.append(event)

    async def resolver(_request):
        return ResolvedCredential(source=CredentialSource.user, provider="deepseek",
                                  api_key="fake", base_url="https://example.invalid/v1", model="deepseek-chat")

    gateway = OpenAIGateway(settings, usage_sink=sink, credential_resolver=resolver if route == "byok" else None)
    request = ChatRequest(messages=(ChatMessage("user", "导数"),), request_id="tool-stream",
                          tools=({"type": "function", "function": {"name": "search_learning_memories", "parameters": {"type": "object"}}},))
    events = [event async for event in gateway.stream_chat(request)]
    result = events[-1]
    assert isinstance(result, ChatResult)
    assert result.text == "先看记录。" and result.reasoning_content == "需要历史记录"
    assert result.finish_reason == "tool_calls"
    assert result.tool_calls[0]["function"]["name"] == "search_learning_memories"
    assert json.loads(result.tool_calls[0]["function"]["arguments"]) == {"query": "导数"}
    assert result.prompt_tokens == 20 and result.completion_tokens == 30
    assert len(usage) == 1
    assert result.credential_source == ("user" if route == "byok" else "platform")
    assert "需要历史记录" not in "".join(e.text for e in events if isinstance(e, StreamDelta))


async def test_native_chat_can_stream_call_memory_tool_and_continue_answer():
    class Gateway:
        calls = 0

        async def chat(self, req):
            raise AssertionError("Streaming chat must use the stream provider")

        async def stream_chat(self, req):
            self.calls += 1
            if self.calls == 1:
                yield ChatResult("", "fake", "standard", 1, 1, 0, 0, "stub", tool_calls=({
                    "id": "memory-call", "type": "function", "function": {
                        "name": "search_learning_memories", "arguments": '{"query":"导数"}',
                    },
                },))
            else:
                assert any(m.role == "tool" and "之前的困惑" in m.content for m in req.messages)
                yield StreamDelta("可以从极限定义继续。")
                yield ChatResult("可以从极限定义继续。", "fake", "standard", 1, 1, 0, 0, "stub")

    async def remember(args):
        assert args == {"query": "导数"}
        return {"memories": [{"text": "之前的困惑"}]}

    async def checkpoint(archive):
        pass

    request = ChatRequest(messages=(ChatMessage("user", "导数"),))
    async with HarnessGateway(Gateway()) as runtime:
        runtime.configure_chat(request, session_id="a" * 32, archive={}, checkpoint=checkpoint,
                               context_window=32768,
                               schemas=[schema("search_learning_memories", "读取学习记录", {"query": {"type": "string"}}, ("query",))],
                               handlers={"search_learning_memories": remember})
        events = [event async for event in runtime.stream_chat(request)]
    assert any(isinstance(event, StreamDelta) and "极限" in event.text for event in events)


async def test_cancelled_byok_tool_stream_accounts_received_arguments(monkeypatch, tmp_path):
    import eduagents_core.gateway.openai_gateway as module

    started = asyncio.Event()

    class PartialProvider(StreamingProvider):
        async def create(self, **kwargs):
            async def response():
                yield chunk(name="search_learning_memories", arguments='{"query":"导数')
                started.set()
                await asyncio.Event().wait()
            return response()

    monkeypatch.setattr(module, "AsyncOpenAI", PartialProvider)
    usage = []

    async def sink(event):
        usage.append(event)

    async def resolver(req):
        return ResolvedCredential(source=CredentialSource.user, provider="deepseek", api_key="fake",
                                  base_url="https://example.invalid/v1", model="deepseek-chat")

    gateway = OpenAIGateway(Settings(_env_file=None, gateway_mode="live", gateway_max_retries=1,
                                    gateway_cassette_dir=str(tmp_path)), usage_sink=sink, credential_resolver=resolver)
    req = ChatRequest(messages=(ChatMessage("user", "导数"),), tools=({"type": "function", "function": {"name": "search_learning_memories"}},))

    async def consume():
        return [event async for event in gateway.stream_chat(req)]

    task = asyncio.create_task(consume())
    await asyncio.wait_for(started.wait(), 3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(usage) == 1 and usage[0].completion_tokens > 0
    assert usage[0].credential_source == "user" and usage[0].cost_cny == 0
