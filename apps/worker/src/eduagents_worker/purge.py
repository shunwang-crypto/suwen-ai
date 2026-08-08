"""租户物理清理 (P0-4 注销收尾): 由 outbox (pending_cleanup kind=tenant) 消费。

PG 业务行已在注销事务内删除 (FK 级联 + 显式删个人库), 这里清四类残留:
1. Qdrant 两 collection (eduagents_chunks + answer_cache) 该租户全部点位
2. 对象存储前缀 {uid}/ (原始文档) 与 exports/{uid}/ (导出 ZIP)
3. Redis 前缀: profile_summary:{uid}* / budget:{uid}:* / reflect:{sid} (会话 id
   随注销事务收进 payload, 会话行删后无法反查) / orgs:{uid} / export:{uid}
4. LangGraph checkpoint 三表 (不在 ORM/Alembic, 明文问答): thread_id 约定
   "{kind}:{user_id}:{local_id}" (graph_runtime.py), 按 LIKE '%:{uid}:%' 删,
   直连 SQLAlchemy text (checkpoint 表由迁移账户建, edu_app 走默认授权 DML)。

全部操作可重放 (幂等), 供 outbox 失败重试。
"""

from __future__ import annotations

import asyncio

from eduagents_core.config import get_settings
from eduagents_core.logging import get_logger
from qdrant_client import models as qmodels

log = get_logger("purge")

_CHECKPOINT_TABLES = ("checkpoints", "checkpoint_blobs", "checkpoint_writes")


async def purge_tenant(
    ctx: dict,
    tenant_id: str,
    session_ids: list[str] | None = None,
    *,
    progress=None,
) -> str:
    from eduagents_api.db import plain_session
    from eduagents_core.storage import create_storage
    from eduagents_core.vectorstore.qdrant_store import _get_client
    from sqlalchemy import text

    from .cleanup_progress import run_cleanup_step

    settings = ctx.get("settings") or get_settings()
    tenant_id = str(tenant_id)

    # 1) Qdrant: 两 collection 按 tenant_id filter 删点位
    client = _get_client(settings)
    tenant_filter = qmodels.Filter(
        must=[
            qmodels.FieldCondition(
                key="tenant_id", match=qmodels.MatchValue(value=tenant_id)
            )
        ]
    )

    def _purge_vectors() -> None:
        for collection in (settings.qdrant_collection, "answer_cache"):
            if client.collection_exists(collection):
                client.delete(
                    collection,
                    points_selector=qmodels.FilterSelector(filter=tenant_filter),
                    wait=True,
                )

    await run_cleanup_step(progress, "qdrant", lambda: asyncio.to_thread(_purge_vectors))

    # 2) 对象存储前缀
    storage = create_storage(settings)

    async def _purge_storage() -> None:
        await storage.delete_prefix(tenant_id)
        await storage.delete_prefix(f"exports/{tenant_id}")

    await run_cleanup_step(progress, "storage", _purge_storage)

    # 3) Redis 前缀 (ctx 无 redis 时临时建连接, 供测试/独立调用)
    redis = ctx.get("redis")
    owns_redis = False
    if redis is None:
        from redis.asyncio import Redis

        redis = Redis.from_url(settings.redis_url)
        owns_redis = True
    async def _purge_redis() -> None:
        try:
            keys: list = [
                f"orgs:{tenant_id}",
                f"export:{tenant_id}",
                f"export:pending:{tenant_id}",
            ]
            keys += [f"reflect:{sid}" for sid in (session_ids or [])]
            for pattern in (f"profile_summary:*:{tenant_id}*", f"budget:{tenant_id}:*"):
                async for key in redis.scan_iter(match=pattern, count=500):
                    keys.append(key)
            if keys:
                await redis.delete(*keys)
        finally:
            if owns_redis:
                await redis.aclose()

    await run_cleanup_step(progress, "redis", _purge_redis)

    # 4) checkpoint 三表 (明文问答, P0-4)
    async def _purge_checkpoints() -> None:
        async with plain_session() as session:
            for table in _CHECKPOINT_TABLES:
                exists = await session.scalar(
                    text("SELECT to_regclass(:name)"), {"name": f"public.{table}"}
                )
                if exists is None:
                    continue
                await session.execute(
                    text(f"DELETE FROM {table} WHERE thread_id LIKE :pat"),  # noqa: S608 — 表名白名单
                    {"pat": f"%:{tenant_id}:%"},
                )

    await run_cleanup_step(progress, "checkpoint", _purge_checkpoints)

    log.info("purge_tenant_done", tenant_id=tenant_id, sessions=len(session_ids or []))
    return "ok"
