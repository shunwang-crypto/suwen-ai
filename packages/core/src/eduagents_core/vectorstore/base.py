from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class VectorPoint:
    id: str  # chunk_id (uuid str)
    dense: list[float]
    sparse: dict[int, float]
    payload: dict = field(default_factory=dict)
    # payload 必含: tenant_id, kb_id, doc_id, parent_id, text, heading_path,
    #              page_start, page_end, is_table


@dataclass
class SearchHit:
    id: str
    score: float
    payload: dict


@dataclass(frozen=True, slots=True)
class VectorPointRef:
    """The metadata-only identity of a point used by the audit path.

    This intentionally does not contain a vector or document text.  Qdrant
    point IDs are normally UUID strings, but the type stays permissive here so
    the reconcile layer can report malformed records instead of failing while
    parsing them.
    """

    point_id: object
    tenant_id: object | None
    kb_id: object | None
    doc_id: object | None


# A short alias is useful to callers that refer to Qdrant's terminology.
PointRef = VectorPointRef


class VectorStore(Protocol):
    """Qdrant 实现见 vectorstore/qdrant_store.py。
    - 单 collection + named vectors {dense, sparse} + tenant_id/kb_id payload 索引
    - settings.qdrant_local_path 非空 -> qdrant-client 本地模式 (dev, 零服务依赖)
    - 所有查询强制携带 tenant_id 过滤 (§14 禁止无租户查询)
    - scan_point_refs 是仅供管理员对账的 metadata-only 全量扫描例外
    """

    async def ensure_ready(self) -> None: ...

    async def upsert(self, points: list[VectorPoint]) -> None: ...

    async def delete_by_doc(self, tenant_id: str, doc_id: str) -> None: ...

    async def delete_by_kb(self, tenant_id: str, kb_id: str) -> None: ...

    async def count_by_kb(self, tenant_id: str, kb_id: str) -> int: ...

    async def scan_kb_ids(self) -> dict[tuple[str, str], int]: ...

    async def scan_point_refs(
        self,
        *,
        page_size: int = 1000,
        max_pages: int = 1000,
        timeout: int | None = 10,
        max_points: int | None = None,
    ) -> list[VectorPointRef]: ...

    async def search_dense(
        self,
        tenant_id: str | list[str],
        kb_id: str | list[str],
        vector: list[float],
        limit: int,
    ) -> list[SearchHit]: ...

    async def search_sparse(
        self,
        tenant_id: str | list[str],
        kb_id: str | list[str],
        sparse: dict[int, float],
        limit: int,
    ) -> list[SearchHit]: ...
