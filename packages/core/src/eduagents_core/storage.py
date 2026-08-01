from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import Protocol

import aiofiles

from .config import Settings


class ObjectStorage(Protocol):
    """原始文档存储。key 约定: {tenant_id}/{kb_id}/{doc_id}/{filename}
    (前缀天然按租户隔离, MinIO 版预签名 URL 也基于此前缀)。"""

    async def save(self, key: str, data: bytes) -> None: ...

    async def load(self, key: str) -> bytes: ...

    async def delete_prefix(self, prefix: str) -> None: ...


def _safe_rel(key: str) -> Path:
    p = Path(key)
    if p.is_absolute() or ".." in p.parts:
        raise ValueError(f"非法存储 key: {key}")
    return p


class LocalStorage:
    """开发默认 (decisions.md D5): 本地文件系统, 与 MinIO 同 key 约定零迁移成本。"""

    def __init__(self, root: str | Path):
        self._root = Path(root)

    async def save(self, key: str, data: bytes) -> None:
        path = self._root / _safe_rel(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        async with aiofiles.open(path, "wb") as f:
            await f.write(data)

    async def load(self, key: str) -> bytes:
        async with aiofiles.open(self._root / _safe_rel(key), "rb") as f:
            return await f.read()

    async def delete_prefix(self, prefix: str) -> None:
        target = self._root / _safe_rel(prefix)
        if target.exists():
            # 删除失败必须向上抛出，调用方才能保留数据库中的 deleting 状态并重试，
            # 避免把对象存储失败误报成已经删除。
            await asyncio.to_thread(shutil.rmtree, target)


def create_storage(settings: Settings) -> ObjectStorage:
    if settings.storage_backend == "minio":
        from .storage_minio import MinioStorage

        return MinioStorage(settings)
    root = settings.local_storage_dir or str(
        Path.home() / ".local/share/eduagents/objects"
    )
    return LocalStorage(root)
