"""Native DSH inference and tool execution through the mandatory model gateway.

The native runtime owns the agent loop and conversation history. Its session
files are checkpointed into the tenant store and restored before a later turn.
Credentials remain exclusively in ModelGateway.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import secrets
import tempfile
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
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


def _history(messages: list[dict], *, native: bool = False) -> tuple[ChatMessage, ...]:
    """Project the native model context without rebuilding conversation history."""
    result = []
    for message in messages:
        role = message.get("role")
        if role not in (
            {"system", "user", "assistant", "tool"} if native else {"assistant", "tool"}
        ):
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
        self.native_session_id: str | None = None
        self.native_archive: dict[str, str] = {}
        self._native_prompt = ""
        self._checkpoint = None
        self._policy = None
        self._task_complete: Callable[[], bool] | None = None
        self._repair_tool_call = None
        self._repairing = False
        self.context_window = 32768
        self._workspace = None
        self._chat_overlay = None
        self._chat_seed = ()

    def archive(self) -> dict[str, str]:
        """Copy only runtime-owned session files; never credentials or attachments."""
        if self._home is None:
            return dict(self.native_archive)
        root = Path(self._home.name)
        archive = {}
        size = 0
        for path in (root / "sessions").rglob("*"):
            if not path.is_file() or path.is_symlink() or ".lock" in path.name:
                continue
            data = path.read_bytes()
            size += len(data)
            if size > 16 * 1024 * 1024:
                raise GatewayError("HARNESS_SESSION_LIMIT", "任务会话存储已到上限")
            archive[str(path.relative_to(root))] = base64.b64encode(data).decode("ascii")
        return archive

    async def checkpoint(self):
        if self.native_session_id and self._checkpoint:
            self.native_archive = self.archive()
            await self._checkpoint(self.native_archive)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        await self.close()

    async def close(self):
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        try:
            if self._harness is not None:
                await asyncio.to_thread(self._harness.close)
                self._harness = None
                self.native_archive = self.archive()
        finally:
            if self._runner is not None:
                await self._runner.cleanup()
                self._runner = None
            if self._home is not None:
                self._home.cleanup()
                self._home = None
            if self._workspace is not None:
                self._workspace.rmdir()
                self._workspace = None

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
        restored_size = 0
        for name, value in self.native_archive.items():
            relative = Path(name)
            if (
                relative.is_absolute()
                or ".." in relative.parts
                or not relative.parts
                or relative.parts[0] != "sessions"
            ):
                raise GatewayError("HARNESS_SESSION_INVALID", "任务会话存储无效")
            if (
                len(relative.parts) < 4
                or relative.parts[-2] != self.native_session_id
                or ".lock" in relative.name
            ):
                raise GatewayError("HARNESS_SESSION_INVALID", "任务会话归属无效")
            try:
                if len(value) > 24 * 1024 * 1024:
                    raise ValueError("oversized archive")
                data = base64.b64decode(value, validate=True)
            except (ValueError, TypeError) as exc:
                raise GatewayError("HARNESS_SESSION_INVALID", "任务会话存储无效") from exc
            restored_size += len(data)
            if restored_size > 16 * 1024 * 1024:
                raise GatewayError("HARNESS_SESSION_LIMIT", "任务会话存储已到上限")
            target = Path(self._home.name) / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        assets = Path(__file__).parent
        patch = Path(self._home.name) / "education.patch.yml"
        patch.write_text(
            (assets / "education.patch.yml")
            .read_text()
            .replace("EDU_DSH_PLUGIN_PATH", json.dumps(str(assets / "bridge.mjs")))
        )
        cwd = self._home.name
        if self.native_session_id:
            if not re.fullmatch(r"[a-f0-9]{32}", self.native_session_id):
                raise GatewayError("HARNESS_SESSION_INVALID", "任务会话标识无效")
            self._workspace = (
                Path(tempfile.gettempdir()) / "eduagents-native-workspaces" / self.native_session_id
            )
            if self._workspace.is_symlink():
                self._workspace = None
                raise GatewayError("HARNESS_SESSION_INVALID", "任务工作目录无效")
            self._workspace.mkdir(parents=True, exist_ok=True, mode=0o700)
            cwd = str(self._workspace)
        self._harness = DeepSeekHarness(
            provider="eduagents",
            model="education",
            profile="sdk-minimal",
            cwd=cwd,
            runtime_cwd=self._home.name,
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
                "EDU_DSH_CONTEXT_WINDOW": str(self.context_window),
            },
        )
        await asyncio.to_thread(self._harness.start)

    async def _tool(self, request):
        body = await request.json()
        name, args = body.get("name"), body.get("args")
        if name not in self._tools or not isinstance(args, dict):
            raise web.HTTPForbidden()
        try:
            result = await self._tools[name](args)
            await self.checkpoint()
            return web.json_response(result)
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
            await self.checkpoint()
            native = self.native_session_id is not None
            compaction = body.get("purpose") == "compaction"
            messages = _history(body.get("messages") or [], native=native)
            if native:
                # The host contributes current authorization/pedagogical policy;
                # all user, assistant and tool history comes from the native log.
                policy = self._policy() if self._policy and not compaction else ""
                messages = (ChatMessage("system", policy), *messages) if policy else messages
                if self._chat_overlay and not compaction:
                    # Images are a request-scoped overlay. Only text enters the
                    # native transcript, so base64 data never enters checkpoints.
                    projected = list(messages)
                    for index in range(len(projected) - 1, -1, -1):
                        if projected[index].role == "user":
                            projected[index] = self._chat_overlay
                            projected[index:index] = self._chat_seed
                            break
                    messages = tuple(projected)
            else:
                messages = self._req.messages + messages
            tools = () if compaction or (
                self._task_complete is not None and self._task_complete()
            ) else tuple({"type": "function", "function": s} for s in self._schemas)
            req = replace(
                self._req,
                messages=messages,
                tools=tools,
                tool_choice="required" if tools and self._task_complete is not None else None,
                billing_reference=f"{self._req.request_id}:dsh:{self._activity_id}:{self._calls}",
                feature="harness_compaction" if compaction else self._req.feature,
                json_mode=False if compaction else self._req.json_mode,
                max_tokens=2048 if compaction else self._req.max_tokens,
            )
            submission = self._repair_tool_call() if self._repairing and tools and self._repair_tool_call else None
            if submission:
                name, evidence, argument_schema = submission
                spec = next(s for s in self._schemas if s["name"] == name)
                feedback = messages[-1].content if messages and messages[-1].role == "tool" else ""
                req = replace(
                    req, tools=(), tool_choice=None, json_mode=True, temperature=0.0,
                    response_schema=argument_schema, max_tokens=min(req.max_tokens or 1000, 1000),
                    messages=(
                        ChatMessage("system", policy + "\n输出工具参数 JSON，只填参数本身。简明回答。"),
                        ChatMessage("user", json.dumps(
                            {"instructions": spec["description"], "input": evidence, "last_tool_feedback": feedback[:1200]},
                            ensure_ascii=False,
                        )),
                    ),
                )
            text = ""
            await emit({"type": "block-start", "index": 0, "blockType": "text"})
            if (
                compaction
                or not self._streaming
                or not hasattr(self.gateway, "stream_chat")
            ):
                result = await self.gateway.chat(req)
                if submission:
                    # Ollama may ignore required tool choice. Its JSON-mode output
                    # goes back through DSH's normal dispatch and host validation.
                    result = replace(result, text="", tool_calls=({
                        "id": uuid.uuid4().hex, "type": "function",
                        "function": {"name": name, "arguments": result.text},
                    },))
                text = result.text
                if text:
                    if self._streaming and not compaction:
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
                    name = fn["name"] if re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_]{0,63}", fn["name"]) else "unknown"
                    raise GatewayError("HARNESS_TOOL_DENIED", "模型请求了未授权的工具：" + name)
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

    def _prompt(self, session_id: str, *, repair: bool = False):
        # session/prompt deliberately bypasses coding-agent slash commands.
        from deepseek_harness.api import final_response

        if self.native_session_id and self.native_archive and not repair:
            self._harness.client.resume_session(session_id)
        prompt = self._native_prompt if self.native_session_id else "Complete the host educational task."
        if repair:
            prompt = (
                "任务仍未提交有效结果。检查上一次工具反馈，继续在本会话调用可用的领域工具，"
                "直到交付物通过核验、提交给用户确认或按工具协议报告失败。"
            )
        with self._harness.client.subscribe_session_notifications(session_id) as subscription:
            self._harness.client.session_prompt(
                session_id,
                [{"type": "text", "text": prompt}],
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
                session_id = self.native_session_id or uuid.uuid4().hex
                failed = asyncio.create_task(self._failed.wait())
                # Some providers end the turn with prose despite required tool choice.
                # Continue once in the same native transcript, under the same limits.
                for repair in (False, True):
                    self._repairing = repair
                    prompt = asyncio.create_task(asyncio.to_thread(self._prompt, session_id, repair=repair))
                    await asyncio.wait([prompt, failed], return_when=asyncio.FIRST_COMPLETED)
                    if self._error:
                        raise self._error
                    text = await prompt
                    if self._task_complete is None or self._task_complete() or self._calls >= self.max_calls:
                        break
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

    async def run_task(
        self,
        req,
        *,
        session_id,
        archive,
        prompt,
        schemas,
        handlers,
        policy=None,
        checkpoint=None,
        context_window=32768,
        is_complete: Callable[[], bool] | None = None,
        repair_tool_call=None,
    ):
        """Run one turn of a durable native agent; DSH chooses and dispatches tools."""
        self.native_session_id = session_id
        self.native_archive = dict(archive)
        self._native_prompt = prompt
        self._schemas = schemas
        self._tools = handlers
        self._policy = policy
        self._checkpoint = checkpoint
        self._task_complete = is_complete
        self._repair_tool_call = repair_tool_call
        self.context_window = context_window
        return await self._run(req)

    def configure_chat(self, req, *, session_id, archive, checkpoint, context_window,
                       schemas=(), handlers=None, extra_policy=""):
        self.native_session_id = session_id
        self.native_archive = dict(archive)
        self._checkpoint = checkpoint
        self.context_window = context_window
        self._schemas = list(schemas)
        self._tools = handlers or {}
        self._policy = lambda: "\n".join(m.content for m in req.messages if m.role == "system") + extra_policy
        self._chat_overlay = req.messages[-1]
        self._chat_seed = (
            tuple(m for m in req.messages[:-1] if m.role != "system") if not archive else ()
        )

        def text_content(message):
            if isinstance(message.content, str):
                return message.content
            return "\n".join(b.get("text", "") for b in message.content if b.get("type") == "text")

        prior = (
            [
                {"role": m.role, "content": text_content(m)}
                for m in req.messages[:-1]
                if m.role != "system"
            ]
            if not archive
            else []
        )
        self._native_prompt = json.dumps(
            {"prior_conversation": prior, "message": text_content(req.messages[-1])},
            ensure_ascii=False,
        )

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
