from __future__ import annotations

import asyncio
import io

from .config import Settings


class MinioStorage:
    """部署态存储 (compose 内 MinIO)。同步 SDK 用 to_thread 包装。"""

    def __init__(self, settings: Settings):
        from minio import Minio  # 延迟导入: 仅 minio 后端需要该依赖

        self._client = Minio(
            settings.minio_endpoint,
            access_key=settings.minio_app_access_key,
            secret_key=settings.minio_app_secret_key,
            secure=settings.minio_secure,
        )
        self._bucket = settings.minio_bucket

    def _ensure_bucket(self) -> None:
        if not self._client.bucket_exists(self._bucket):
            self._client.make_bucket(self._bucket)

    async def save(self, key: str, data: bytes) -> None:
        def _put() -> None:
            self._ensure_bucket()
            self._client.put_object(
                self._bucket, key, io.BytesIO(data), length=len(data)
            )

        await asyncio.to_thread(_put)

    async def load(self, key: str) -> bytes:
        def _get() -> bytes:
            resp = self._client.get_object(self._bucket, key)
            try:
                return resp.read()
            finally:
                resp.close()
                resp.release_conn()

        return await asyncio.to_thread(_get)

    async def delete_prefix(self, prefix: str) -> None:
        def _delete() -> None:
            objects = self._client.list_objects(
                self._bucket, prefix=prefix, recursive=True
            )
            for obj in objects:
                self._client.remove_object(self._bucket, obj.object_name)

        await asyncio.to_thread(_delete)
