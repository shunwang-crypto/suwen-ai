from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import replace

from eduagents_core.config import Settings
from eduagents_core.harness.gateway import ScopedHarnessGateway, validate_installation
from eduagents_core.harness.workflow import Resume, Snapshot
from eduagents_core.workflows.state import WorkflowDeps
from psycopg_pool import AsyncConnectionPool
from sqlalchemy import text

from .db import tenant_session

_KINDS = ("qa", "quiz", "plan", "tutor")


class PostgresStore:
    def __init__(self, pool):
        self.pool = pool

    async def load(self, thread_id):
        _kind, user_id, _local = HarnessRuntime.parse_thread_id(thread_id)
        async with tenant_session(user_id) as session:
            row = (
                (
                    await session.execute(
                        text(
                            "SELECT state, next_phase, pending FROM harness_sessions WHERE thread_id = :id AND user_id = :uid"
                        ),
                        {"id": thread_id, "uid": user_id},
                    )
                )
                .mappings()
                .first()
            )
        return (
            Snapshot(
                dict(row["state"]),
                (row["next_phase"],) if row["next_phase"] else (),
                row["pending"],
            )
            if row
            else Snapshot()
        )

    async def save(self, thread_id, snapshot):
        kind, user_id, _local = HarnessRuntime.parse_thread_id(thread_id)
        # Request identities are bound by the authenticated router, never by model output.
        if snapshot.values.get("user_id", user_id) != user_id:
            raise ValueError("harness owner mismatch")
        async with tenant_session(user_id) as session:
            await session.execute(
                text("""
                INSERT INTO harness_sessions (thread_id, tenant_id, user_id, kind, state, next_phase, pending)
                VALUES (:id, :uid, :uid, :kind, CAST(:state AS jsonb), :next, CAST(:pending AS jsonb))
                ON CONFLICT (thread_id) DO UPDATE SET state = EXCLUDED.state,
                    next_phase = EXCLUDED.next_phase, pending = EXCLUDED.pending, updated_at = now()
                WHERE harness_sessions.user_id = EXCLUDED.user_id
            """),
                {
                    "id": thread_id,
                    "uid": user_id,
                    "kind": kind,
                    "state": json.dumps(snapshot.values, ensure_ascii=False),
                    "next": snapshot.next[0] if snapshot.next else None,
                    "pending": json.dumps(snapshot.pending, ensure_ascii=False)
                    if snapshot.pending is not None
                    else None,
                },
            )

    @asynccontextmanager
    async def lock(self, thread_id):
        # Session advisory lock serializes a thread across API processes. State writes
        # use short RLS transactions, not a transaction spanning model generation.
        async with self.pool.connection() as connection:
            try:
                await connection.execute(
                    "SELECT pg_advisory_lock(hashtextextended(%s, 0))", (thread_id,)
                )
                yield
            except asyncio.CancelledError:
                # Cancellation during acquisition must not return a connection
                # with a session lock to the shared pool.
                await connection.close()
                raise
            finally:
                if not connection.closed:
                    await connection.execute(
                        "SELECT pg_advisory_unlock(hashtextextended(%s, 0))", (thread_id,)
                    )


class HarnessRuntime:
    """Four educational DSH workflows with durable host confirmations and state."""

    def __init__(self, settings: Settings, deps: WorkflowDeps):
        self.settings = settings
        self.deps = replace(
            deps,
            gateway=ScopedHarnessGateway(
                deps.gateway, settings.harness_timeout_seconds, settings.harness_max_concurrency
            ),
        )
        self._pool = None
        self._workflows = {}

    async def start(self):
        validate_installation()
        self._pool = AsyncConnectionPool(
            self.settings.database_url.replace("postgresql+asyncpg://", "postgresql://"),
            open=False,
            min_size=1,
            max_size=self.settings.harness_max_concurrency,
            kwargs={"autocommit": True},
        )
        await self._pool.open()

    async def stop(self):
        if self._pool:
            await self._pool.close()

    def workflow(self, kind):
        if kind not in _KINDS:
            raise ValueError(f"unknown workflow kind: {kind}")
        if kind not in self._workflows:
            from eduagents_core.workflows.plan import build_plan_workflow
            from eduagents_core.workflows.qa import build_qa_workflow
            from eduagents_core.workflows.quiz import build_quiz_workflow
            from eduagents_core.workflows.tutor import build_tutor_workflow

            factory = {
                "qa": build_qa_workflow,
                "quiz": build_quiz_workflow,
                "plan": build_plan_workflow,
                "tutor": build_tutor_workflow,
            }[kind]
            self._workflows[kind] = factory(self.deps).with_store(PostgresStore(self._pool))
        return self._workflows[kind]

    async def stream_chat(self, req, gateway=None):
        async with self.deps.gateway.activity(gateway):
            async for item in self.deps.gateway.stream_chat(req):
                yield item

    @staticmethod
    def thread_id(kind, user_id, local_id):
        return f"{kind}:{user_id}:{local_id}"

    @staticmethod
    def parse_thread_id(thread_id):
        parts = thread_id.split(":", 2)
        if len(parts) != 3 or parts[0] not in _KINDS:
            raise ValueError("bad thread_id")
        return tuple(parts)

    async def astream_updates(self, kind, thread_id, payload, resume=None):
        workflow = self.workflow(kind)
        config = {"configurable": {"thread_id": thread_id}}
        pending = None
        async for mode, data in workflow.astream(
            Resume(resume) if resume is not None else payload, config
        ):
            if mode == "custom":
                if isinstance(data, dict) and data.get("type") == "token":
                    yield {
                        "kind": "token",
                        "text": str(data.get("text", "")),
                        "attempt": int(data.get("attempt") or 0),
                    }
                else:
                    yield {"kind": "custom", "payload": data}
            elif "__interrupt__" in data:
                pending = data["__interrupt__"][0].value
            else:
                for phase, delta in data.items():
                    yield {"kind": "node", "node": phase, "delta": delta or {}}
        if pending is not None:
            yield {"kind": "interrupt", "payload": pending}
        else:
            snapshot = await workflow.aget_state(config)
            yield {"kind": "end", "state": snapshot.values}
