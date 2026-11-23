"""Durable host contracts around native DSH activities.

These phases enforce schema checks, evidence policy and explicit confirmation.
Model reasoning/tool dispatch is owned by DSH, not a second agent executor.
"""

from __future__ import annotations

import asyncio
import contextvars
import copy
import inspect
import uuid
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from ..gateway.base import GatewayError

_writer = contextvars.ContextVar("educational_events", default=lambda _event: None)
_decision = contextvars.ContextVar("educational_confirmation", default=None)
_NO_DECISION = object()


def get_stream_writer():
    return _writer.get()


@dataclass
class Resume:
    resume: object


@dataclass
class Pending:
    value: object


class Pause(Exception):
    def __init__(self, value):
        self.value = value


def interrupt(value):
    decision = _decision.get()
    if decision is _NO_DECISION or decision is None:
        raise Pause(value)
    return decision


@dataclass
class Snapshot:
    values: dict = field(default_factory=dict)
    next: tuple[str, ...] = ()
    pending: object = None


class MemoryStore:
    def __init__(self):
        self.states = {}
        self.locks = {}

    async def load(self, thread_id):
        return copy.deepcopy(self.states.get(thread_id, Snapshot()))

    async def save(self, thread_id, snapshot):
        self.states[thread_id] = copy.deepcopy(snapshot)

    @asynccontextmanager
    async def lock(self, thread_id):
        async with self.locks.setdefault(thread_id, asyncio.Lock()):
            yield


def _merge(state, delta):
    for key, value in delta.items():
        state[key] = [*(state.get(key) or []), *value] if key in {"messages", "trace"} else value


class Workflow:
    def __init__(
        self,
        *,
        entry: str,
        phases: dict[str, Callable],
        transitions: dict[str, str | Callable],
        gateway=None,
        store=None,
    ):
        self.entry = entry
        self.phases = phases
        self.transitions = transitions
        self.gateway = gateway
        self.store = store or MemoryStore()

    def with_store(self, store):
        return Workflow(
            entry=self.entry,
            phases=self.phases,
            transitions=self.transitions,
            gateway=self.gateway,
            store=store,
        )

    async def aget_state(self, config):
        return await self.store.load(config["configurable"]["thread_id"])

    async def _execute(self, payload, config, queue):
        thread_id = (config or {}).get("configurable", {}).get("thread_id") or uuid.uuid4().hex
        async with self.store.lock(thread_id):
            snapshot = await self.store.load(thread_id)
            state = snapshot.values
            if isinstance(payload, Resume):
                if state and not snapshot.next:
                    # A lost HTTP response or failed finalization may replay the
                    # terminal state; no model work or confirmation is repeated.
                    return state
                if snapshot.next and snapshot.pending is None and state.get("blueprint_confirmed"):
                    phase = snapshot.next[0]
                    decision = _NO_DECISION
                elif not snapshot.next or snapshot.pending is None:
                    raise GatewayError("HARNESS_NOT_PAUSED", "该会话没有等待确认的任务")
                else:
                    phase = snapshot.next[0]
                    decision = payload.resume
            else:
                if snapshot.next and snapshot.pending is not None:
                    raise GatewayError("HARNESS_CONFIRMATION_REQUIRED", "请先确认当前任务")
                _merge(state, payload or {})
                phase = snapshot.next[0] if snapshot.next else self.entry
                decision = _NO_DECISION
                await self.store.save(thread_id, Snapshot(state, (phase,)))
            writer_token = _writer.set(lambda event: queue.put_nowait(("custom", event)))
            decision_token = _decision.set(decision)
            try:
                async with self._activity():
                    for _step in range(40):
                        if not phase:
                            await self.store.save(thread_id, Snapshot(state))
                            return state
                        try:
                            delta = self.phases[phase](state)
                            if inspect.isawaitable(delta):
                                delta = await delta
                        except Pause as pause:
                            await self.store.save(thread_id, Snapshot(state, (phase,), pause.value))
                            queue.put_nowait(("updates", {"__interrupt__": [Pending(pause.value)]}))
                            return {**state, "__interrupt__": [Pending(pause.value)]}
                        _merge(state, delta or {})
                        queue.put_nowait(("updates", {phase: delta or {}}))
                        next_phase = self.transitions.get(phase, "")
                        phase = next_phase(state) if callable(next_phase) else next_phase
                        await self.store.save(thread_id, Snapshot(state, (phase,) if phase else ()))
                    raise GatewayError("HARNESS_PHASE_LIMIT", "教育任务重试已到上限")
            finally:
                _writer.reset(writer_token)
                _decision.reset(decision_token)

    @asynccontextmanager
    async def _activity(self):
        if hasattr(self.gateway, "activity"):
            async with self.gateway.activity():
                yield
        else:
            yield

    async def ainvoke(self, payload, config=None):
        return await self._execute(payload, config, asyncio.Queue())

    async def astream(self, payload, config=None, *, stream_mode=None):
        queue = asyncio.Queue()
        task = asyncio.create_task(self._execute(payload, config, queue))
        take = None
        try:
            while not task.done():
                take = asyncio.create_task(queue.get())
                done, _pending = await asyncio.wait(
                    [task, take], return_when=asyncio.FIRST_COMPLETED
                )
                if take in done:
                    yield take.result()
                else:
                    take.cancel()
                    await asyncio.gather(take, return_exceptions=True)
            while not queue.empty():
                yield queue.get_nowait()
            await task
        finally:
            if take is not None and not take.done():
                take.cancel()
                await asyncio.gather(take, return_exceptions=True)
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
