"""QdrantStore 单测 — 本地嵌入模式 (qdrant_local_path=tmp_path), 零服务依赖。

覆盖 U-A1 向量库层多租户隔离: dense/sparse 检索均强制 tenant_id+kb_id 过滤。
"""

from __future__ import annotations

import uuid

import pytest
from eduagents_core.config import Settings
from eduagents_core.vectorstore.base import VectorPoint
from eduagents_core.vectorstore.qdrant_store import QdrantStore

DIM = 8


@pytest.fixture
def store(tmp_path):
    settings = Settings(
        _env_file=None,
        qdrant_local_path=str(tmp_path / "qdrant"),
        qdrant_collection="test_chunks",
        embedding_dim=DIM,
    )
    s = QdrantStore(settings)
    yield s
    s.close()


def make_point(
    tenant_id: str, kb_id: str, doc_id: str, dense: list[float], sparse: dict[int, float]
) -> VectorPoint:
    return VectorPoint(
        id=str(uuid.uuid4()),
        dense=dense,
        sparse=sparse,
        payload={
            "tenant_id": tenant_id,
            "kb_id": kb_id,
            "doc_id": doc_id,
            "parent_id": str(uuid.uuid4()),
            "text": f"chunk of {doc_id}",
            "heading_path": [],
            "page_start": 1,
            "page_end": 1,
            "is_table": False,
        },
    )


async def test_ensure_ready_idempotent(store):
    await store.ensure_ready()
    await store.ensure_ready()  # 已存在则跳过, 不炸


async def test_tenant_isolation_and_delete(store):
    await store.ensure_ready()

    a1 = make_point("tenant-a", "kb1", "doc1", [1.0] + [0.0] * (DIM - 1), {11: 1.0})
    a2 = make_point("tenant-a", "kb1", "doc2", [0.9, 0.1] + [0.0] * (DIM - 2), {11: 0.5, 22: 1.0})
    b1 = make_point("tenant-b", "kb1", "doc9", [1.0] + [0.0] * (DIM - 1), {11: 1.0})
    await store.upsert([a1, a2, b1])

    query_dense = [1.0] + [0.0] * (DIM - 1)
    query_sparse = {11: 1.0}

    # dense 检索: 租户A只返回A的点 (b1 向量与查询完全相同, 仍不可见)
    hits_a = await store.search_dense("tenant-a", "kb1", query_dense, limit=10)
    assert {h.id for h in hits_a} == {a1.id, a2.id}
    assert all(h.payload["tenant_id"] == "tenant-a" for h in hits_a)

    # sparse 检索同理
    sparse_a = await store.search_sparse("tenant-a", "kb1", query_sparse, limit=10)
    assert {h.id for h in sparse_a} == {a1.id, a2.id}

    # 租户B查不到A的点 (U-A1 向量库层多租户隔离)
    hits_b = await store.search_dense("tenant-b", "kb1", query_dense, limit=10)
    assert {h.id for h in hits_b} == {b1.id}
    sparse_b = await store.search_sparse("tenant-b", "kb1", query_sparse, limit=10)
    assert {h.id for h in sparse_b} == {b1.id}

    # delete_by_doc: 该 doc 的点消失, 其余不受影响
    await store.delete_by_doc("tenant-a", "doc1")
    hits_after = await store.search_dense("tenant-a", "kb1", query_dense, limit=10)
    assert {h.id for h in hits_after} == {a2.id}
    assert {h.id for h in await store.search_dense("tenant-b", "kb1", query_dense, 10)} == {b1.id}

    # delete_by_kb: 租户B整库删除后查询为空
    await store.delete_by_kb("tenant-b", "kb1")
    assert await store.search_dense("tenant-b", "kb1", query_dense, limit=10) == []


async def test_search_hit_shape(store):
    await store.ensure_ready()
    p = make_point("tenant-a", "kb1", "doc1", [0.5] * DIM, {7: 1.2})
    await store.upsert([p])
    (hit,) = await store.search_dense("tenant-a", "kb1", [0.5] * DIM, limit=1)
    assert hit.id == p.id
    assert hit.score == pytest.approx(1.0)  # cosine 同向
    assert hit.payload["doc_id"] == "doc1"
    assert hit.payload["text"] == "chunk of doc1"


async def test_scan_point_refs_is_metadata_only_and_handles_missing_collection(store):
    # A missing collection is an empty audit namespace, matching count_by_kb's
    # existing compatibility behavior.
    assert await store.scan_point_refs() == []

    await store.ensure_ready()
    point = make_point("tenant-a", "kb1", "doc1", [0.5] * DIM, {7: 1.2})
    await store.upsert([point])

    refs = await store.scan_point_refs(page_size=1, max_pages=2)
    assert len(refs) == 1
    assert refs[0].point_id == point.id
    assert refs[0].tenant_id == "tenant-a"
    assert refs[0].kb_id == "kb1"
    assert refs[0].doc_id == "doc1"
    assert not hasattr(refs[0], "dense")
    assert not hasattr(refs[0], "text")

    second = make_point("tenant-a", "kb1", "doc2", [0.4] * DIM, {8: 0.7})
    await store.upsert([second])
    with pytest.raises(RuntimeError, match="page limit"):
        await store.scan_point_refs(page_size=1, max_pages=1)
