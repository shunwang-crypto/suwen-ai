"""Distributed locks for destructive per-document vector operations.

Qdrant has no transaction spanning a document's delete/upsert sequence.  All
worker paths that replace or remove one document's points therefore share the
same Redis lock.  A small process-local fallback keeps direct unit/integration
invocations deterministic when a fake Redis client is not supplied.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

_LOCAL_LOCKS: dict[str, asyncio.Lock] = {}


def _local_lock(key: str) -> asyncio.Lock:
    lock = _LOCAL_LOCKS.get(key)
    if lock is None:
        lock = _LOCAL_LOCKS[key] = asyncio.Lock()
    return lock


@asynccontextmanager
async def document_vector_lock(
    redis: object | None,
    tenant_id: str,
    doc_id: str,
    *,
    timeout: int = 900,
) -> AsyncIterator[None]:
    """Serialize vector replacement/deletion for one tenant document."""

    key = f"vector:document:{tenant_id}:{doc_id}"
    lock_factory = getattr(redis, "lock", None) if redis is not None else None
    if lock_factory is None:
        lock = _local_lock(key)
        async with lock:
            yield
        return

    lock = lock_factory(key, timeout=timeout, blocking_timeout=timeout)
    acquired = await lock.acquire()
    if not acquired:
        raise TimeoutError(f"timed out acquiring vector lock for {doc_id}")
    try:
        yield
    finally:
        try:
            await lock.release()
        except Exception:  # noqa: BLE001 - a lost lease must not mask the worker result
            pass


__all__ = ["document_vector_lock"]
