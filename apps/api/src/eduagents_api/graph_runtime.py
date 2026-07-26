from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from eduagents_core.config import Settings
from eduagents_core.graphs.state import GraphDeps
from eduagents_core.logging import get_logger
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.types import Command
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

log = get_logger("graph_runtime")

_KINDS = ("qa", "quiz", "plan", "tutor")


def _pg_conninfo(url: str) -> str:
    # sqlalchemy url -> psycopg conninfo
    return url.replace("postgresql+asyncpg://", "postgresql://")


class GraphRuntime:
    """五子图的编译/执行中枢: PG Checkpointer + thread_id 会话恢复 + interrupt/resume。

    thread_id 约定: "{kind}:{user_id}:{local_id}" — resume 端点据前缀路由到对应图,
    并校验 user_id 归属 (跨用户 resume 直接拒绝)。
    """

    def __init__(self, settings: Settings, deps: GraphDeps):
        self._settings = settings
        self._deps = deps
        self._pool: AsyncConnectionPool | None = None
        self._checkpointer: AsyncPostgresSaver | None = None
        self._compiled: dict[str, Any] = {}

    async def start(self) -> None:
        # checkpointer 表由 migration 账户建 (owner=edu), edu_app 走默认授权 DML
        conninfo = _pg_conninfo(
            self._settings.migration_database_url or self._settings.database_url
        )
        self._pool = AsyncConnectionPool(
            conninfo,
            open=False,
            max_size=8,
            kwargs={"autocommit": True, "row_factory": dict_row},
        )
        await self._pool.open()
        self._checkpointer = AsyncPostgresSaver(self._pool)
        await self._checkpointer.setup()
        log.info("graph_runtime_started")

    async def stop(self) -> None:
        if self._pool is not None:
            await self._pool.close()

    def graph(self, kind: str):
        if kind not in _KINDS:
            raise ValueError(f"unknown graph kind: {kind}")
        if kind not in self._compiled:
            if kind == "qa":
                from eduagents_core.graphs.qa_graph import build_qa_graph

                builder = build_qa_graph(self._deps)
            elif kind == "quiz":
                from eduagents_core.graphs.quiz_graph import build_quiz_graph

                builder = build_quiz_graph(self._deps)
            elif kind == "plan":
                from eduagents_core.graphs.plan_graph import build_plan_graph

                builder = build_plan_graph(self._deps)
            else:
                from eduagents_core.graphs.tutor_graph import build_tutor_graph

                builder = build_tutor_graph(self._deps)
            self._compiled[kind] = builder.compile(checkpointer=self._checkpointer)
        return self._compiled[kind]

    @staticmethod
    def thread_id(kind: str, user_id: str, local_id: str) -> str:
        return f"{kind}:{user_id}:{local_id}"

    @staticmethod
    def parse_thread_id(thread_id: str) -> tuple[str, str, str]:
        parts = thread_id.split(":", 2)
        if len(parts) != 3 or parts[0] not in _KINDS:
            raise ValueError("bad thread_id")
        return parts[0], parts[1], parts[2]

    async def astream_updates(
        self, kind: str, thread_id: str, payload: dict | None, resume: Any = None
    ) -> AsyncIterator[dict]:
        """逐节点产出规范化事件流:
        {"kind":"node","node":name,"delta":partial_state} |
        {"kind":"interrupt","payload":...} | {"kind":"end","state":final}
        """
        graph = self.graph(kind)
        config = {"configurable": {"thread_id": thread_id}}
        inp: Any = Command(resume=resume) if resume is not None else payload
        async for chunk in graph.astream(inp, config, stream_mode="updates"):
            if "__interrupt__" in chunk:
                intr = chunk["__interrupt__"]
                value = intr[0].value if isinstance(intr, (list, tuple)) else intr.value
                yield {"kind": "interrupt", "payload": value}
                return
            for node, delta in chunk.items():
                yield {"kind": "node", "node": node, "delta": delta or {}}
        state = await graph.aget_state(config)
        yield {"kind": "end", "state": dict(state.values)}
