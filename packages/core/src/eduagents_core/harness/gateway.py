"""Native DSH inference and tool execution through the mandatory model gateway.

Each workflow activity owns a subprocess and a private temporary home. Durable
educational state lives in PostgreSQL; reconstructed prompts resume interrupted
contracts without retaining provider keys or attachment bytes in a DSH home.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import secrets
import tempfile
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any

from aiohttp import web

from ..gateway.base import (
    ChatMessage,
    ChatRequest,
    ChatResult,
    GatewayError,
    ModelGateway,
    StreamDelta,
)


def validate_installation():
    from importlib.metadata import version

    from deepseek_harness_runtime import bundled_runtime_path

    expected = "0.2.2rc2"
    if (
        version("deepseek-harness-sdk") != expected
        or version("deepseek-harness-runtime-bin") != expected
    ):
        raise RuntimeError("DSH SDK and runtime must both match the pinned version")
    return bundled_runtime_path()


ToolHandler = Callable[[dict], Awaitable[Any]]
_active: contextvars.ContextVar[HarnessGateway | None] = contextvars.ContextVar(
    "dsh_activity", default=None
)


def _history(messages: list[dict]) -> tuple[ChatMessage, ...]:
    """Only DSH-generated assistant/tool history follows the host's original prompt."""
    result = []
    for message in messages:
        role = message.get("role")
        if role not in {"assistant", "tool"}:
            continue
        content = message.get("content") or []
        text = "\n".join(
            str(block.get("text", "")) for block in content if block.get("type") == "text"
        )
        calls = tuple(
            {
                "id": block["id"],
                "type": "function",
                "function": {"name": block["name"], "arguments": block["arguments"]},
            }
            for block in content
            if block.get("type") == "tool-call"
        )
        reasoning = "".join(
            str(block.get("text", "")) for block in content if block.get("type") == "reasoning"
        )
        result.append(
            ChatMessage(
                role=role,
                content=text,
                tool_calls=calls,
                tool_call_id=message.get("toolCallId") if role == "tool" else None,
                reasoning_content=reasoning if role == "assistant" and reasoning else None,
            )
        )
    return tuple(result)


class HarnessGateway:
    def __init__(self, gateway: ModelGateway, *, timeout: float = 180, max_calls: int = 8):
        self.gateway = gateway
        self.timeout = timeout
        self.max_calls = max_calls
        self._harness = None
        self._home = None
        self._runner = None
        self._tasks: set[asyncio.Task] = set()
        self._req: ChatRequest | None = None
        self._tools: dict[str, ToolHandler] = {}
        self._schemas: list[dict] = []
        self._calls = 0
        self._streaming = False
        self._error: BaseException | None = None
        self._results: list[ChatResult] = []
        self._tokens: asyncio.Queue = asyncio.Queue()
        self._failed = asyncio.Event()
        self._token = secrets.token_urlsafe(32)
        self._activity_id = ""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        await self.close()

    async def close(self):
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._harness is not None:
            await asyncio.to_thread(self._harness.close)
            self._harness = None
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
        if self._home is not None:
            self._home.cleanup()
            self._home = None

    async def _start(self):
        if self._harness is not None:
            return
        from deepseek_harness import DeepSeekHarness

        @web.middleware
        async def authenticate(request, handler):
            if request.headers.get("Authorization") != f"Bearer {self._token}":
                raise web.HTTPUnauthorized()
            task = asyncio.current_task()
            self._tasks.add(task)
            try:
                return await handler(request)
            finally:
                self._tasks.discard(task)

        app = web.Application(middlewares=[authenticate], client_max_size=8 * 1024 * 1024)
        app.router.add_post("/model", self._model)

        async def schemas(_request):
            return web.json_response(self._schemas)

        app.router.add_post("/tools", schemas)
        app.router.add_post("/tool", self._tool)
        self._runner = web.AppRunner(app, handler_cancellation=True)
        await self._runner.setup()
        server = await asyncio.get_running_loop().create_server(self._runner.server, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        # AppRunner owns the registered site and closes its socket on cleanup.
        site = web.SockSite(self._runner, server.sockets[0].dup())
        server.close()
        await server.wait_closed()
        await site.start()
        self._home = tempfile.TemporaryDirectory(prefix="eduagents-dsh-")
        assets = Path(__file__).parent
        patch = Path(self._home.name) / "education.patch.yml"
        patch.write_text(
            (assets / "education.patch.yml")
            .read_text()
            .replace("EDU_DSH_PLUGIN_PATH", json.dumps(str(assets / "bridge.mjs")))
        )
        self._harness = DeepSeekHarness(
            provider="eduagents",
            model="education",
            profile="sdk-minimal",
            cwd=self._home.name,
            dsh_home=self._home.name,
            patches=(str(patch),),
            env={
                "EDU_DSH_PLUGIN": str(assets / "bridge.mjs"),
                "EDU_DSH_ENDPOINT": f"http://127.0.0.1:{port}",
                "EDU_DSH_TOKEN": self._token,
                "DSH_PRIMARY_RUNTIME": "",
                "DEEPSEEK_API_KEY": "",
                "DEEPSEEK_BASE_URL": "",
                "DSH_SYSTEM_PROMPT": "Educational assistant",
            },
        )
        await asyncio.to_thread(self._harness.start)

    async def _tool(self, request):
        body = await request.json()
        name, args = body.get("name"), body.get("args")
        if name not in self._tools or not isinstance(args, dict):
            raise web.HTTPForbidden()
        try:
            return web.json_response(await self._tools[name](args))
        except BaseException as exc:
            self._error = exc
            self._failed.set()
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise web.HTTPServiceUnavailable() from exc

    async def _model(self, request):
        response = web.StreamResponse(headers={"Content-Type": "application/x-ndjson"})
        await response.prepare(request)

        async def emit(value):
            await response.write((json.dumps(value, ensure_ascii=False) + "\n").encode())

        try:
            self._calls += 1
            if self._calls > self.max_calls:
                raise GatewayError("HARNESS_STEP_LIMIT", "AI 工具调用已到上限")
            body = await request.json()
            assert self._req is not None
            req = replace(
                self._req,
                messages=self._req.messages + _history(body.get("messages") or []),
                tools=tuple({"type": "function", "function": s} for s in self._schemas),
                billing_reference=f"{self._req.request_id}:dsh:{self._activity_id}:{self._calls}",
            )
            text = ""
            await emit({"type": "block-start", "index": 0, "blockType": "text"})
            if req.tools or not self._streaming or not hasattr(self.gateway, "stream_chat"):
                result = await self.gateway.chat(req)
                text = result.text
                if text:
                    if self._streaming:
                        await self._tokens.put(StreamDelta(text))
                    await emit({"type": "text-delta", "index": 0, "text": text})
            else:
                result = None
                async for item in self.gateway.stream_chat(req):
                    if isinstance(item, StreamDelta):
                        text += item.text
                        await self._tokens.put(item)
                        await emit({"type": "text-delta", "index": 0, "text": item.text})
                    else:
                        result = item
                if result is None:
                    raise GatewayError("HARNESS_PROTOCOL_ERROR", "模型缺少最终响应")
            self._results.append(result)
            await emit({"type": "block-end", "index": 0, "block": {"type": "text", "text": text}})
            if req.tools and result.reasoning_content:
                await emit({"type": "block-start", "index": 1, "blockType": "reasoning"})
                await emit(
                    {"type": "reasoning-delta", "index": 1, "text": result.reasoning_content}
                )
                await emit(
                    {
                        "type": "block-end",
                        "index": 1,
                        "block": {"type": "reasoning", "text": result.reasoning_content},
                    }
                )
            for index, call in enumerate(result.tool_calls, 2 if result.reasoning_content else 1):
                fn = call["function"]
                if fn["name"] not in self._tools:
                    raise GatewayError("HARNESS_TOOL_DENIED", "模型请求了未授权的工具")
                await emit({"type": "block-start", "index": index, "blockType": "tool-call"})
                await emit(
                    {
                        "type": "tool-call-delta",
                        "index": index,
                        "id": call["id"],
                        "name": fn["name"],
                        "argumentsDelta": fn["arguments"],
                    }
                )
                await emit(
                    {
                        "type": "block-end",
                        "index": index,
                        "block": {
                            "type": "tool-call",
                            "id": call["id"],
                            "name": fn["name"],
                            "arguments": fn["arguments"],
                        },
                    }
                )
            await emit(
                {
                    "type": "usage",
                    "usage": {
                        "inputTokens": result.prompt_tokens,
                        "outputTokens": result.completion_tokens,
                    },
                }
            )
            reason = (
                "tool-calls"
                if result.tool_calls
                else "max-tokens"
                if result.finish_reason == "length"
                else "stop"
            )
            await emit({"type": "finish", "reason": {"kind": reason}})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._error = exc
            self._failed.set()
            await emit(
                {
                    "type": "finish",
                    "reason": {
                        "kind": "error",
                        "failure": {
                            "code": "EDUCATION_MODEL_FAILED",
                            "message": "教育模型调用失败",
                        },
                    },
                }
            )
        await response.write_eof()
        return response

    def _prompt(self, session_id: str):
        # session/prompt deliberately bypasses coding-agent slash commands.
        from deepseek_harness.api import final_response

        with self._harness.client.subscribe_session_notifications(session_id) as subscription:
            self._harness.client.session_prompt(
                session_id, [{"type": "text", "text": "Complete the host educational task."}]
            )
            events = []
            while True:
                notification = subscription.next()
                if notification.method == "session.event":
                    events.append(notification.payload["event"])
                if (
                    notification.method == "session.status"
                    and notification.payload.get("status") == "idle"
                ):
                    break
            return final_response(events)

    async def _run(self, req: ChatRequest, *, streaming=False):
        self._req = req
        self._activity_id = uuid.uuid4().hex
        self._streaming = streaming
        self._calls = 0
        self._results = []
        self._error = None
        self._failed.clear()
        prompt = failed = None
        try:
            async with asyncio.timeout(self.timeout):
                await self._start()
                prompt = asyncio.create_task(asyncio.to_thread(self._prompt, uuid.uuid4().hex))
                failed = asyncio.create_task(self._failed.wait())
                await asyncio.wait([prompt, failed], return_when=asyncio.FIRST_COMPLETED)
                if self._error:
                    raise self._error
                text = await prompt
            if self._error:
                raise self._error
            if not self._results:
                raise GatewayError("HARNESS_PROTOCOL_ERROR", "DSH 未返回模型响应")
            result = replace(self._results[-1], text=text)
            return result
        except TimeoutError as exc:
            await self.close()
            raise GatewayError("HARNESS_TIMEOUT", "AI 运行超时，请重试") from exc
        except BaseException:
            await self.close()
            raise
        finally:
            for task in (prompt, failed):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(
                *(task for task in (prompt, failed) if task is not None), return_exceptions=True
            )

    async def chat(self, req: ChatRequest) -> ChatResult:
        return await self._run(req)

    async def stream_chat(self, req: ChatRequest) -> AsyncIterator[StreamDelta | ChatResult]:
        task = asyncio.create_task(self._run(req, streaming=True))
        take = None
        try:
            while not task.done():
                take = asyncio.create_task(self._tokens.get())
                done, _pending = await asyncio.wait(
                    [take, task], return_when=asyncio.FIRST_COMPLETED
                )
                if take in done:
                    yield take.result()
                else:
                    take.cancel()
                    await asyncio.gather(take, return_exceptions=True)
            while not self._tokens.empty():
                yield self._tokens.get_nowait()
            yield await task
        finally:
            if take is not None and not take.done():
                take.cancel()
                await asyncio.gather(take, return_exceptions=True)
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def run_tools(
        self, req: ChatRequest, schemas: list[dict], handlers: dict[str, ToolHandler]
    ) -> ChatResult:
        # Separate registry lifetime prevents tool permissions crossing activities.
        async with HarnessGateway(
            self.gateway, timeout=self.timeout, max_calls=self.max_calls
        ) as activity:
            activity._schemas = schemas
            activity._tools = handlers
            return await activity.chat(req)


class ScopedHarnessGateway:
    """One SDK activity per workflow invocation, isolated across concurrent users."""

    def __init__(self, gateway: ModelGateway, timeout: float = 180, max_concurrency: int = 8):
        self.gateway = gateway
        self.timeout = timeout
        self._slots = asyncio.Semaphore(max_concurrency)

    @asynccontextmanager
    async def activity(self, gateway=None):
        async with (
            self._slots,
            HarnessGateway(gateway or self.gateway, timeout=self.timeout) as runtime,
        ):
            token = _active.set(runtime)
            try:
                yield
            finally:
                _active.reset(token)

    def _current(self):
        runtime = _active.get()
        if runtime is None:
            raise RuntimeError("DSH requires an owned workflow activity")
        return runtime

    async def chat(self, req):
        return await self._current().chat(req)

    def stream_chat(self, req):
        return self._current().stream_chat(req)

    async def run_tools(self, req, schemas, handlers):
        return await self._current().run_tools(req, schemas, handlers)
