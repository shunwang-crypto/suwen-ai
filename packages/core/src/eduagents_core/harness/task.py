"""Task lifecycle around native DSH sessions, without a host step interpreter."""

from __future__ import annotations

import asyncio
import copy
import json
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace

from ..gateway.base import ChatRequest, GatewayError, Tier
from .gateway import HarnessGateway


@dataclass
class TaskRecord:
    state: dict = field(default_factory=dict)
    status: str = "idle"
    pending: dict | None = None
    native_session_id: str = ""
    native_archive: dict[str, str] = field(default_factory=dict)


class MemoryTaskStore:
    def __init__(self):
        self.records = {}
        self.locks = {}

    async def load(self, thread_id):
        return copy.deepcopy(self.records.get(thread_id, TaskRecord()))

    async def save(self, thread_id, record):
        self.records[thread_id] = copy.deepcopy(record)

    @asynccontextmanager
    async def lock(self, thread_id):
        async with self.locks.setdefault(thread_id, asyncio.Lock()):
            yield


async def refresh_memory_context(deps, record):
    if deps.memory is None:
        return False
    current = await deps.memory.snapshot(record.state)
    previous = record.state.get("_memory_context")
    changed = current != previous
    if changed and record.native_archive:
        # A native archive can contain recalled tool results or compaction
        # summaries. Start a new archive after a user memory control changes.
        record.native_archive = {}
        record.native_session_id = uuid.uuid4().hex
    record.state["_memory_context"] = current
    return changed


class AgentTask:
    """Submit one task turn to DSH; domain tools decide which results are valid."""

    def __init__(self, kind, deps, store=None, *, parent_thread_id="", agent_name=""):
        self.kind, self.deps = kind, deps
        self.store = store or MemoryTaskStore()
        self.parent_thread_id, self.agent_name = parent_thread_id, agent_name

    async def run(self, thread_id, payload=None, *, resume=None, emit=None, retry=False):
        from ..education.tools import create_tools

        emit = emit or (lambda _event: None)
        async with self.store.lock(thread_id):
            record = await self.store.load(thread_id)
            if retry:
                if record.status not in {"failed", "cancelled"}:
                    raise GatewayError("TASK_NOT_FAILED", "任务当前不需要重试")
                record.status = "failed"
                for child in (record.state.get("team_tasks") or {}).values():
                    if child["status"] in {"failed", "cancelled"}:
                        child["status"] = "queued"
            if record.status == "failed":
                record.state.pop("failure_code", None)
                record.state.pop("failure_message", None)
            incoming = dict(payload or {})
            if any(key in incoming for key in ("_prompt_bundle", "_parent_thread_id", "_agent_name", "_architecture")):
                raise GatewayError("HARNESS_PROMPT_OVERRIDE_DENIED", "请求不能修改教学提示词版本")
            old_owner = record.state.get("user_id")
            if old_owner and incoming.get("user_id", old_owner) != old_owner:
                raise GatewayError("HARNESS_OWNER_MISMATCH", "任务归属不匹配")
            if resume is not None:
                if record.status in {"completed", "cancelled"}:
                    return record
                if record.status != "awaiting_confirmation" or record.pending is None:
                    raise GatewayError("HARNESS_NOT_PAUSED", "该任务没有等待确认的内容")
            elif record.pending is not None:
                raise GatewayError("HARNESS_CONFIRMATION_REQUIRED", "请先确认当前任务")
            elif self.kind not in {"qa", "tutor", "chat"} and record.status == "completed":
                return record

            request_id = incoming.get("request_id")
            duplicate = bool(request_id and request_id == record.state.get("_active_request"))
            messages = incoming.pop("messages", [])
            record.state.update(incoming)
            if await refresh_memory_context(self.deps, record):
                duplicate = False
            if duplicate and record.status in {"completed", "cancelled"} and resume is None:
                return record
            if messages and not duplicate:
                record.state["messages"] = [*(record.state.get("messages") or []), *messages]
            record.state["_active_request"] = (
                request_id or uuid.uuid4().hex if not duplicate else record.state["_active_request"]
            )
            record.state["trace"] = []
            record.native_session_id = (
                record.native_session_id
                or uuid.uuid5(uuid.NAMESPACE_URL, f"eduagents:{thread_id}").hex
            )
            if self.parent_thread_id:
                if record.state.get("_parent_thread_id", self.parent_thread_id) != self.parent_thread_id:
                    raise GatewayError("HARNESS_OWNER_MISMATCH", "子任务归属不匹配")
                record.state.update(_parent_thread_id=self.parent_thread_id, _agent_name=self.agent_name)
            from ..education.prompts import pin_prompt_bundle
            bundle = pin_prompt_bundle(record, self.deps.settings, self.deps.prompt_bundle)
            if self.kind == "plan" and "_architecture" not in record.state:
                architecture = self.deps.settings.plan_generation_mode
                if record.state.get("team_tasks"):
                    architecture = "team"
                elif record.native_archive or record.pending is not None:
                    architecture = "single"
                elif architecture == "team" and (
                    not self.deps.settings.agent_collaboration_enabled or bundle.team_policies is None
                ):
                    raise GatewayError("AGENT_COLLABORATION_UNAVAILABLE", "团队计划需要启用协作并使用包含角色策略的提示词")
                record.state["_architecture"] = architecture
            collaboration = None
            run_deps = self.deps
            if self.kind == "plan" and record.state.get("_architecture") == "team":
                from .collaboration import AgentCollaboration
                collaboration = AgentCollaboration(replace(self.deps, prompt_bundle=bundle), self.store, record, thread_id, emit)
                run_deps = collaboration.deps
            tools = create_tools(self.kind, run_deps, record, emit, collaboration)
            if resume is not None:
                tools.confirm(resume)
                if record.status in {"cancelled", "completed"}:
                    await self.store.save(thread_id, record)
                    return record
                tools = create_tools(self.kind, run_deps, record, emit, collaboration)
            if not duplicate:
                tools.prepare_turn()
                record.state.pop("_accepted_tool", None)
                record.state.pop("result", None)
            record.status = "running"
            await self.store.save(thread_id, record)

            async def checkpoint(archive):
                record.native_archive = archive
                await self.store.save(thread_id, record)

            role = tools.feature
            request = ChatRequest(
                messages=(),
                tier=tools.tier,
                feature=role,
                tenant_id=record.state.get("tenant_id", ""),
                user_id=record.state.get("user_id", ""),
                request_id=record.state.get("request_id", ""),
                max_tokens=tools.max_tokens,
                json_mode=False,
                allow_tier_fallback=tools.tier != Tier.strong,
            )
            runtime = HarnessGateway(
                run_deps.gateway,
                timeout=(self.deps.settings.agent_collaboration_timeout_seconds if collaboration is not None
                         else self.deps.settings.harness_timeout_seconds),
                max_calls=self.deps.settings.harness_max_model_calls,
            )
            try:
                if self.kind == "plan" and record.state.get("_architecture") == "guided":
                    await tools.prepare_context()
                prompt = json.dumps(
                    {"task": self.kind, "input": tools.input(messages), "confirmation": resume},
                    ensure_ascii=False,
                )
                async with runtime:
                    await runtime.run_task(
                        request,
                        session_id=record.native_session_id,
                        archive=record.native_archive,
                        prompt=prompt,
                        schemas=tools.schemas,
                        handlers=tools.handlers,
                        policy=tools.policy,
                        checkpoint=checkpoint,
                        context_window=self.deps.settings.harness_context_window_tokens,
                        is_complete=lambda: record.status != "running",
                        repair_tool_call=(
                            getattr(tools, "repair_tool_call", None)
                            if self.deps.settings.ollama_chat_model else None
                        ),
                    )
                if record.status == "running":
                    raise GatewayError("HARNESS_RESULT_REQUIRED", "AI 未提交经过校验的任务结果")
            except BaseException as exc:
                terminal = record.status in {"completed", "awaiting_confirmation"} or (
                    record.status == "failed" and bool(record.state.get("failure_code"))
                )
                if record.status == "running":
                    record.status = "cancelled" if isinstance(exc, asyncio.CancelledError) else "failed"
                    if isinstance(exc, GatewayError):
                        record.state.update(failure_code=exc.code, failure_message=exc.message)
                # A checked artifact is durable even if the final model
                # acknowledgment fails. Cancellation still stops the caller.
                if isinstance(exc, asyncio.CancelledError) or not terminal:
                    raise
            finally:
                if collaboration is not None:
                    await collaboration.close()
                record.native_archive = runtime.native_archive or record.native_archive
                await self.store.save(thread_id, record)
            return record

    async def stream(self, thread_id, payload=None, *, resume=None, retry=False):
        queue = asyncio.Queue()
        task = asyncio.create_task(
            self.run(thread_id, payload, resume=resume, emit=queue.put_nowait, retry=retry)
        )
        take = None
        try:
            while not task.done():
                take = asyncio.create_task(queue.get())
                done, _ = await asyncio.wait([task, take], return_when=asyncio.FIRST_COMPLETED)
                if take in done:
                    yield take.result()
                else:
                    take.cancel()
                    await asyncio.gather(take, return_exceptions=True)
            while not queue.empty():
                yield queue.get_nowait()
            record = await task
            if record.pending is not None:
                yield {"kind": "interrupt", "payload": record.pending}
            else:
                yield {"kind": "end", "state": record.state}
        finally:
            for pending in (take, task):
                if pending is not None and not pending.done():
                    pending.cancel()
            await asyncio.gather(
                *(p for p in (take, task) if p is not None), return_exceptions=True
            )
