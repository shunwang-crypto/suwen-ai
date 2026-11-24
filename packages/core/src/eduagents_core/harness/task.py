"""Task lifecycle around native DSH sessions, without a host step interpreter."""

from __future__ import annotations

import asyncio
import copy
import json
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

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


class AgentTask:
    """Submit one task turn to DSH; domain tools decide which results are valid."""

    def __init__(self, kind, deps, store=None):
        self.kind, self.deps = kind, deps
        self.store = store or MemoryTaskStore()

    async def run(self, thread_id, payload=None, *, resume=None, emit=None):
        from ..education.tools import create_tools

        emit = emit or (lambda _event: None)
        async with self.store.lock(thread_id):
            record = await self.store.load(thread_id)
            incoming = dict(payload or {})
            if "_prompt_bundle" in incoming:
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
            if duplicate and record.status in {"completed", "cancelled"} and resume is None:
                return record
            messages = incoming.pop("messages", [])
            record.state.update(incoming)
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
            tools = create_tools(self.kind, self.deps, record, emit)
            if resume is not None:
                tools.confirm(resume)
                if record.status == "cancelled":
                    await self.store.save(thread_id, record)
                    return record
                tools = create_tools(self.kind, self.deps, record, emit)
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
            prompt = json.dumps(
                {"task": self.kind, "input": tools.input(messages), "confirmation": resume},
                ensure_ascii=False,
            )
            runtime = HarnessGateway(
                self.deps.gateway,
                timeout=self.deps.settings.harness_timeout_seconds,
                max_calls=self.deps.settings.harness_max_model_calls,
            )
            try:
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
                    )
                if record.status == "running":
                    raise GatewayError("HARNESS_RESULT_REQUIRED", "AI 未提交经过校验的任务结果")
            except BaseException as exc:
                terminal = record.status in {"completed", "awaiting_confirmation"} or (
                    record.status == "failed" and bool(record.state.get("failure_code"))
                )
                if record.status == "running":
                    record.status = "failed"
                # A checked artifact is durable even if the final model
                # acknowledgment fails. Cancellation still stops the caller.
                if isinstance(exc, asyncio.CancelledError) or not terminal:
                    raise
            finally:
                record.native_archive = runtime.native_archive or record.native_archive
                await self.store.save(thread_id, record)
            return record

    async def stream(self, thread_id, payload=None, *, resume=None):
        queue = asyncio.Queue()
        task = asyncio.create_task(
            self.run(thread_id, payload, resume=resume, emit=queue.put_nowait)
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
