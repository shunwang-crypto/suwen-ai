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
from .base import SearchHit, VectorPoint

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

    async def delete_by_kb(self, tenant_id: str, kb_id: str) -> None:
        await self._delete(self._filter(tenant_id=tenant_id, kb_id=kb_id))

    async def _delete(self, flt: models.Filter) -> None:
        await asyncio.to_thread(
            self._client.delete,
            self._collection,
            points_selector=models.FilterSelector(filter=flt),
            wait=True,
        )

    # --- read path ---

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
