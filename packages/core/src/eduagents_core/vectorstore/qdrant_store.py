"""Qdrant VectorStore 实现 (M2)。

- 单 collection + named vectors {dense, sparse} + tenant_id/kb_id/doc_id payload 索引
- settings.qdrant_local_path 非空 -> qdrant-client 本地嵌入模式 (dev/CI, 零服务依赖);
  为空 -> 连 qdrant_url 服务端
- 同步 client + asyncio.to_thread 包装: 本地模式只有同步 API 稳定,
  两种模式统一走同一条同步代码路径
- 所有查询/删除强制携带 tenant_id 过滤 (§14 禁止无租户查询)
"""

from __future__ import annotations

import asyncio
import warnings

from qdrant_client import QdrantClient, models

from ..config import Settings
from .base import SearchHit, VectorPoint, VectorPointRef

# 本地嵌入模式同一路径只允许一个客户端实例 (文件锁): API 进程内
# RetrievalService 与摄取任务必须共享同一 client, 按目标 key 做进程级单例。
_client_cache: dict[str, QdrantClient] = {}


def _get_client(settings: Settings) -> QdrantClient:
    key = settings.qdrant_local_path or settings.qdrant_url
    if key not in _client_cache:
        if settings.qdrant_local_path:
            _client_cache[key] = QdrantClient(path=settings.qdrant_local_path)
        else:
            _client_cache[key] = QdrantClient(url=settings.qdrant_url)
    return _client_cache[key]


class QdrantStore:
    def __init__(self, settings: Settings) -> None:
        self._collection = settings.qdrant_collection
        self._retired = settings.retired_chunk_collection
        self._dim = settings.embedding_dim
        self._local = bool(settings.qdrant_local_path)
        self._client = _get_client(settings)

    def close(self) -> None:
        key = None
        for k, v in list(_client_cache.items()):
            if v is self._client:
                key = k
        self._client.close()
        if key is not None:
            _client_cache.pop(key, None)

    # --- schema ---

    async def ensure_ready(self) -> None:
        await asyncio.to_thread(self._ensure_ready_sync)

    def _ensure_ready_sync(self) -> None:
        """幂等: collection 已存在则跳过创建, payload 索引重复创建无害。"""
        if not self._client.collection_exists(self._collection):
            self._client.create_collection(
                self._collection,
                vectors_config={
                    "dense": models.VectorParams(
                        size=self._dim, distance=models.Distance.COSINE
                    )
                },
                sparse_vectors_config={"sparse": models.SparseVectorParams()},
            )
        self._create_payload_indexes()

    def _create_payload_indexes(self) -> None:
        tenant_schema: models.PayloadSchemaType | models.KeywordIndexParams
        if self._local:
            # 本地嵌入模式不支持 is_tenant 参数且 payload 索引本身是 no-op
            # (qdrant-client 只发 UserWarning): 用普通 keyword schema 并压掉警告,
            # 保持与服务端同一条调用路径。
            tenant_schema = models.PayloadSchemaType.KEYWORD
        else:
            tenant_schema = models.KeywordIndexParams(
                type=models.KeywordIndexType.KEYWORD, is_tenant=True
            )
        with warnings.catch_warnings():
            if self._local:
                warnings.simplefilter("ignore", UserWarning)
            for field_name, schema in (
                ("tenant_id", tenant_schema),
                ("kb_id", models.PayloadSchemaType.KEYWORD),
                ("doc_id", models.PayloadSchemaType.KEYWORD),
            ):
                self._client.create_payload_index(
                    self._collection, field_name, field_schema=schema
                )

    # --- write path ---

    async def upsert(self, points: list[VectorPoint]) -> None:
        if not points:
            return
        structs = [
            models.PointStruct(
                id=p.id,
                vector={
                    "dense": p.dense,
                    "sparse": models.SparseVector(
                        indices=list(p.sparse.keys()), values=list(p.sparse.values())
                    ),
                },
                payload=p.payload,
            )
            for p in points
        ]
        await asyncio.to_thread(
            self._client.upsert, self._collection, points=structs, wait=True
        )

    async def delete_by_doc(self, tenant_id: str, doc_id: str) -> None:
        await self._delete(self._filter(tenant_id=tenant_id, doc_id=doc_id))

    async def delete_points(self, point_ids: list[str]) -> None:
        """Delete an exact point set after a stale worker generation is detected.

        This is deliberately narrower than ``delete_by_doc``: a reindex may have
        already written a newer set of points for the same document, so stale
        enrichment cleanup must never remove the replacement generation.
        """
        if not point_ids:
            return
        await asyncio.to_thread(
            self._client.delete,
            self._collection,
            points_selector=models.PointIdsList(points=list(point_ids)),
            wait=True,
        )

    async def delete_by_kb(self, tenant_id: str, kb_id: str) -> None:
        await self._delete(self._filter(tenant_id=tenant_id, kb_id=kb_id))

    async def _delete(self, flt: models.Filter) -> None:
        selector = models.FilterSelector(filter=flt)
        await asyncio.to_thread(self._client.delete, self._collection, points_selector=selector, wait=True)
        if self._retired and await asyncio.to_thread(self._client.collection_exists, self._retired):
            await asyncio.to_thread(self._client.delete, self._retired, points_selector=selector, wait=True)

    # --- read path ---

    async def count_by_kb(self, tenant_id: str, kb_id: str) -> int:
        """该 kb 已落库的点数 (对账用, 精确计数)。

        collection 尚未创建时返回 0 而不是抛错: 全新环境对账应报"两侧都空",
        不该被当成基础设施故障。
        """
        return await asyncio.to_thread(self._count_sync, tenant_id, kb_id)

    def _count_sync(self, tenant_id: str, kb_id: str) -> int:
        if not self._client.collection_exists(self._collection):
            return 0
        result = self._client.count(
            self._collection,
            count_filter=self._filter(tenant_id=tenant_id, kb_id=kb_id),
            exact=True,
        )
        return int(result.count)

    async def scan_point_refs(
        self,
        *,
        collection: str | None = None,
        page_size: int = 1000,
        max_pages: int = 1000,
        timeout: int | None = 10,
        max_points: int | None = None,
    ) -> list[VectorPointRef]:
        """Scan point identity metadata for the administrator audit path.

        The scan deliberately requests only the three ownership/document
        payload keys and disables vectors.  It is the only global read used by
        reconciliation; normal retrieval continues to require tenant and KB
        filters.  A bounded page count/point count turns an unexpectedly large
        collection into an explicit audit error instead of returning a
        misleading partial result.
        """
        return await asyncio.to_thread(
            self._scan_point_refs_sync,
            collection,
            page_size,
            max_pages,
            timeout,
            max_points,
        )

    def _scan_point_refs_sync(
        self,
        collection: str | None,
        page_size: int,
        max_pages: int,
        timeout: int | None,
        max_points: int | None,
    ) -> list[VectorPointRef]:
        if page_size <= 0:
            raise ValueError("page_size must be positive")
        if max_pages <= 0:
            raise ValueError("max_pages must be positive")
        if timeout is not None and timeout <= 0:
            raise ValueError("timeout must be positive or None")
        if max_points is not None and max_points <= 0:
            raise ValueError("max_points must be positive or None")
        target_collection = collection or self._collection
        if not self._client.collection_exists(target_collection):
            return []

        refs: list[VectorPointRef] = []
        offset = None
        for _page_number in range(max_pages):
            points, next_offset = self._client.scroll(
                target_collection,
                limit=page_size,
                offset=offset,
                with_payload=["tenant_id", "kb_id", "doc_id"],
                with_vectors=False,
                timeout=timeout,
            )
            for point in points:
                payload = point.payload if isinstance(point.payload, dict) else {}
                refs.append(
                    VectorPointRef(
                        point_id=str(point.id) if point.id is not None else None,
                        tenant_id=payload.get("tenant_id"),
                        kb_id=payload.get("kb_id"),
                        doc_id=payload.get("doc_id"),
                    )
                )
                if max_points is not None and len(refs) > max_points:
                    raise RuntimeError("qdrant point scan point limit exceeded")

            if next_offset is None:
                return refs
            if next_offset == offset:
                raise RuntimeError("qdrant point scan returned a repeated offset")
            offset = next_offset

        raise RuntimeError("qdrant point scan page limit exceeded")

    async def scan_kb_ids(self) -> dict[tuple[str, str], int]:
        """全库 (tenant_id, kb_id) -> 点数。**仅供管理员对账**, 是本类唯一不带租户过滤
        的读路径 (§14 的显式例外)。

        只取 tenant_id/kb_id 两个 payload 键, 不取 text/向量 —— 对账要回答的是
        "有没有、有多少", 不需要看内容。用途是发现 PG 侧已删除但 Qdrant 仍留存的
        孤儿向量 (知识库删除后未清理会同时造成存储浪费与数据删除不完整)。
        """
        return await asyncio.to_thread(self._scan_kb_ids_sync)

    def _scan_kb_ids_sync(self) -> dict[tuple[str, str], int]:
        if not self._client.collection_exists(self._collection):
            return {}
        counts: dict[tuple[str, str], int] = {}
        offset = None
        while True:
            points, offset = self._client.scroll(
                self._collection,
                limit=1000,
                offset=offset,
                with_payload=["tenant_id", "kb_id"],
                with_vectors=False,
            )
            for point in points:
                payload = point.payload or {}
                key = (str(payload.get("tenant_id")), str(payload.get("kb_id")))
                counts[key] = counts.get(key, 0) + 1
            if offset is None:
                return counts

    async def search_dense(
        self,
        tenant_id: str | list[str],
        kb_id: str | list[str],
        vector: list[float],
        limit: int,
    ) -> list[SearchHit]:
        return await self._query(
            query=vector,
            using="dense",
            flt=self._filter(tenant_id=tenant_id, kb_id=kb_id),
            limit=limit,
        )

    async def search_sparse(
        self,
        tenant_id: str | list[str],
        kb_id: str | list[str],
        sparse: dict[int, float],
        limit: int,
    ) -> list[SearchHit]:
        return await self._query(
            query=models.SparseVector(
                indices=list(sparse.keys()), values=list(sparse.values())
            ),
            using="sparse",
            flt=self._filter(tenant_id=tenant_id, kb_id=kb_id),
            limit=limit,
        )

    async def _query(
        self,
        query: list[float] | models.SparseVector,
        using: str,
        flt: models.Filter,
        limit: int,
    ) -> list[SearchHit]:
        if not await asyncio.to_thread(self._client.collection_exists, self._collection):
            return []
        response = await asyncio.to_thread(
            self._client.query_points,
            self._collection,
            query=query,
            using=using,
            query_filter=flt,
            limit=limit,
            with_payload=True,
        )
        return [
            SearchHit(id=str(p.id), score=p.score, payload=p.payload or {})
            for p in response.points
        ]

    @staticmethod
    def _filter(**conditions: str | list[str]) -> models.Filter:
        """单值 -> MatchValue; 列表 -> MatchAny (Wave3 联合检索: 多 kb_id/多 owner 单次查询)。"""
        must: list[models.FieldCondition] = []
        for key, value in conditions.items():
            if isinstance(value, list | tuple):
                must.append(
                    models.FieldCondition(
                        key=key, match=models.MatchAny(any=[str(v) for v in value])
                    )
                )
            else:
                must.append(
                    models.FieldCondition(key=key, match=models.MatchValue(value=value))
                )
        return models.Filter(must=must)
