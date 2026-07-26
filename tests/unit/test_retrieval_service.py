"""RetrievalService 单元测试: 三协议 + VectorStore 全部用内存 fake。"""

from __future__ import annotations

from eduagents_core.config import Settings
from eduagents_core.retrieval.service import RetrievalService
from eduagents_core.vectorstore.base import SearchHit


class FakeEmbedder:
    dim = 4

    def __init__(self) -> None:
        self.calls = 0

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        return [[0.1, 0.2, 0.3, 0.4] for _ in texts]


class FakeSparseEncoder:
    def __init__(self) -> None:
        self.query_calls = 0

    def encode_document(self, text: str) -> dict[int, float]:
        return {1: 1.0}

    def encode_query(self, text: str) -> dict[int, float]:
        self.query_calls += 1
        return {1: 1.0, 2: 0.5}


class FakeStore:
    """内存 fake: 按预置顺序返回 SearchHit, 记录收到的 limit 与调用次数。"""

    def __init__(self, dense: list[SearchHit], sparse: list[SearchHit]) -> None:
        self.dense = dense
        self.sparse = sparse
        self.dense_limit: int | None = None
        self.sparse_limit: int | None = None
        self.sparse_calls = 0

    async def ensure_ready(self) -> None:
        pass

    async def upsert(self, points: list) -> None:
        pass

    async def delete_by_doc(self, tenant_id: str, doc_id: str) -> None:
        pass

    async def delete_by_kb(self, tenant_id: str, kb_id: str) -> None:
        pass

    async def search_dense(
        self, tenant_id: str, kb_id: str, vector: list[float], limit: int
    ) -> list[SearchHit]:
        self.dense_limit = limit
        return self.dense[:limit]

    async def search_sparse(
        self, tenant_id: str, kb_id: str, sparse: dict[int, float], limit: int
    ) -> list[SearchHit]:
        self.sparse_calls += 1
        self.sparse_limit = limit
        return self.sparse[:limit]


class FakeReranker:
    def __init__(self, scores_by_text: dict[str, float]) -> None:
        self.scores_by_text = scores_by_text
        self.calls = 0

    async def rerank(self, query: str, documents: list[str]) -> list[float]:
        self.calls += 1
        return [self.scores_by_text.get(doc, 0.0) for doc in documents]


def hit(chunk_id: str, score: float) -> SearchHit:
    return SearchHit(
        id=chunk_id,
        score=score,
        payload={
            "text": f"text-{chunk_id}",
            "heading_path": f"讲义 > 第一章 > {chunk_id}",
            "doc_id": "doc-1",
            "page_start": 3,
            "page_end": 4,
        },
    )


def make_settings(**overrides) -> Settings:
    base: dict = {
        "retrieval_candidate_k": 10,
        "retrieval_final_k": 3,
        "rrf_k": 60,
        "retrieval_enable_sparse": True,
        "retrieval_enable_rerank": True,
        "retrieval_confidence_threshold": 0.35,
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)


def make_service(settings: Settings, store: FakeStore, reranker: FakeReranker | None = None):
    reranker = reranker or FakeReranker({})
    sparse = FakeSparseEncoder()
    service = RetrievalService(settings, FakeEmbedder(), sparse, store, reranker)
    return service, sparse, reranker


async def test_sparse_off_uses_dense_order_only():
    store = FakeStore(
        dense=[hit("A", 0.9), hit("B", 0.8), hit("C", 0.7)],
        sparse=[hit("C", 5.0), hit("D", 4.0)],
    )
    settings = make_settings(retrieval_enable_sparse=False, retrieval_enable_rerank=False)
    service, sparse, _ = make_service(settings, store)

    result = await service.retrieve("t1", "kb1", "问题")

    assert [c.chunk_id for c in result.chunks] == ["A", "B", "C"]
    assert result.used_sparse is False
    assert result.sparse_hits == 0
    assert result.dense_hits == 3
    assert store.sparse_calls == 0  # 开关关 -> sparse 通道完全不触发
    assert sparse.query_calls == 0
    assert all(c.source == "dense" for c in result.chunks)


async def test_sparse_on_changes_result_and_flags():
    store = FakeStore(
        dense=[hit("A", 0.9), hit("B", 0.8), hit("C", 0.7)],
        sparse=[hit("C", 5.0), hit("D", 4.0)],
    )
    settings = make_settings(retrieval_enable_sparse=True, retrieval_enable_rerank=False)
    service, sparse, _ = make_service(settings, store)

    result = await service.retrieve("t1", "kb1", "问题")

    # RRF: C 同时命中两路 -> 排第一; 与 sparse-off 的 [A,B,C] 不同
    assert [c.chunk_id for c in result.chunks] == ["C", "A", "B"]
    assert result.used_sparse is True
    assert result.sparse_hits == 2
    assert store.sparse_calls == 1
    assert sparse.query_calls == 1
    assert result.chunks[0].source == "fused"
    assert result.chunks[1].source == "dense"


async def test_rrf_ranks_dual_route_id_first():
    store = FakeStore(
        dense=[hit("X", 0.5), hit("A", 0.4), hit("B", 0.3)],
        sparse=[hit("X", 9.0), hit("C", 8.0)],
    )
    settings = make_settings(retrieval_enable_rerank=False)
    service, _, _ = make_service(settings, store)

    result = await service.retrieve("t1", "kb1", "问题")

    assert result.chunks[0].chunk_id == "X"
    assert result.chunks[0].source == "fused"
    # 只出现在单路的 id 排在 X 之后
    assert {c.chunk_id for c in result.chunks[1:]} <= {"A", "B", "C"}


async def test_rerank_toggle_changes_order():
    store = FakeStore(
        dense=[hit("A", 0.9), hit("B", 0.8), hit("C", 0.7)],
        sparse=[],
    )
    reranker = FakeReranker({"text-B": 0.9, "text-A": 0.5, "text-C": 0.1})

    # rerank 开: B 被精排提到第一
    settings_on = make_settings(
        retrieval_enable_sparse=False, retrieval_enable_rerank=True, retrieval_final_k=2
    )
    service, _, _ = make_service(settings_on, store, reranker)
    result_on = await service.retrieve("t1", "kb1", "问题")
    assert [c.chunk_id for c in result_on.chunks] == ["B", "A"]
    assert result_on.used_rerank is True
    assert [c.rerank_score for c in result_on.chunks] == [0.9, 0.5]
    assert reranker.calls == 1

    # rerank 关: 按融合分截断, reranker 不被调用
    reranker_off = FakeReranker({"text-B": 0.9})
    settings_off = make_settings(
        retrieval_enable_sparse=False, retrieval_enable_rerank=False, retrieval_final_k=2
    )
    service_off, _, _ = make_service(settings_off, store, reranker_off)
    result_off = await service_off.retrieve("t1", "kb1", "问题")
    assert [c.chunk_id for c in result_off.chunks] == ["A", "B"]
    assert result_off.used_rerank is False
    assert all(c.rerank_score is None for c in result_off.chunks)
    assert reranker_off.calls == 0


async def test_final_k_truncation_and_candidate_k_passed():
    dense = [hit(f"D{i}", 1.0 - i * 0.1) for i in range(6)]
    store = FakeStore(dense=dense, sparse=[])
    settings = make_settings(
        retrieval_enable_sparse=False,
        retrieval_enable_rerank=False,
        retrieval_candidate_k=4,
        retrieval_final_k=2,
    )
    service, _, _ = make_service(settings, store)

    result = await service.retrieve("t1", "kb1", "问题")

    assert store.dense_limit == 4  # candidate_k 传给召回层
    assert len(result.chunks) == 2  # final_k 截断
    assert [c.chunk_id for c in result.chunks] == ["D0", "D1"]


async def test_confident_true_when_all_top_above_threshold():
    store = FakeStore(dense=[hit("A", 0.9), hit("B", 0.8)], sparse=[])
    reranker = FakeReranker({"text-A": 0.9, "text-B": 0.4})
    settings = make_settings(
        retrieval_enable_sparse=False,
        retrieval_confidence_threshold=0.35,
        retrieval_final_k=2,
    )
    service, _, _ = make_service(settings, store, reranker)

    result = await service.retrieve("t1", "kb1", "问题")
    assert result.confident is True


async def test_confident_false_when_any_top_below_threshold():
    store = FakeStore(dense=[hit("A", 0.9), hit("B", 0.8)], sparse=[])
    reranker = FakeReranker({"text-A": 0.9, "text-B": 0.2})
    settings = make_settings(
        retrieval_enable_sparse=False,
        retrieval_confidence_threshold=0.35,
        retrieval_final_k=2,
    )
    service, _, _ = make_service(settings, store, reranker)

    result = await service.retrieve("t1", "kb1", "问题")
    assert result.confident is False


async def test_confident_always_false_when_rerank_off():
    store = FakeStore(dense=[hit("A", 0.99)], sparse=[])
    settings = make_settings(retrieval_enable_sparse=False, retrieval_enable_rerank=False)
    service, _, _ = make_service(settings, store)

    result = await service.retrieve("t1", "kb1", "问题")
    assert result.confident is False


async def test_confident_false_when_no_chunks():
    store = FakeStore(dense=[], sparse=[])
    settings = make_settings(retrieval_enable_sparse=False, retrieval_enable_rerank=True)
    service, _, reranker = make_service(settings, store)

    result = await service.retrieve("t1", "kb1", "问题")
    assert result.chunks == []
    assert result.confident is False
    assert reranker.calls == 0


async def test_debug_off_by_default_no_stages():
    store = FakeStore(dense=[hit("A", 0.9)], sparse=[])
    settings = make_settings(retrieval_enable_sparse=False, retrieval_enable_rerank=False)
    service, _, _ = make_service(settings, store)

    result = await service.retrieve("t1", "kb1", "问题")
    assert result.stages is None  # 常规路径零额外开销


async def test_debug_stages_capture_four_phases():
    """debug=True: dense/sparse 原始召回序、RRF 融合序、rerank 终排序全部可核对。"""
    store = FakeStore(
        dense=[hit("A", 0.9), hit("B", 0.8), hit("C", 0.7)],
        sparse=[hit("C", 5.0), hit("D", 4.0)],
    )
    reranker = FakeReranker({"text-A": 0.95, "text-C": 0.5, "text-B": 0.2, "text-D": 0.1})
    settings = make_settings(retrieval_final_k=3)
    service, _, _ = make_service(settings, store, reranker)

    result = await service.retrieve("t1", "kb1", "问题", debug=True)

    st = result.stages
    assert st is not None
    assert [e["chunk_id"] for e in st["dense"]] == ["A", "B", "C"]
    assert [e["chunk_id"] for e in st["sparse"]] == ["C", "D"]
    # RRF: C 双路命中排第一; B/D 同分时 dense 插入序稳定在前
    assert [e["chunk_id"] for e in st["fused"]] == ["C", "A", "B", "D"]
    # rerank 把 A 从融合第 2 提到终排第 1 (排名变化箭头的数据来源)
    assert [e["chunk_id"] for e in st["reranked"]] == ["A", "C", "B"]
    assert [e["score"] for e in st["reranked"]] == [0.95, 0.5, 0.2]
    assert [c.chunk_id for c in result.chunks] == ["A", "C", "B"]  # 终排与 chunks 一致
    assert st["chunk_meta"]["A"]["text"] == "text-A"
    assert st["chunk_meta"]["D"]["heading_path"] == "讲义 > 第一章 > D"


async def test_debug_stages_rerank_off_reranked_empty():
    store = FakeStore(dense=[hit("A", 0.9), hit("B", 0.8)], sparse=[])
    settings = make_settings(retrieval_enable_sparse=False, retrieval_enable_rerank=False)
    service, _, _ = make_service(settings, store)

    result = await service.retrieve("t1", "kb1", "问题", debug=True)
    st = result.stages
    assert st is not None
    assert st["sparse"] == []
    assert st["reranked"] == []
    assert [e["chunk_id"] for e in st["fused"]] == ["A", "B"]


async def test_payload_mapped_to_chunk_fields():
    store = FakeStore(dense=[hit("A", 0.9)], sparse=[])
    settings = make_settings(retrieval_enable_sparse=False, retrieval_enable_rerank=False)
    service, _, _ = make_service(settings, store)

    result = await service.retrieve("t1", "kb1", "问题")
    chunk = result.chunks[0]
    assert chunk.text == "text-A"
    assert chunk.heading_path == "讲义 > 第一章 > A"
    assert chunk.doc_id == "doc-1"
    assert chunk.page_start == 3
    assert chunk.page_end == 4
    assert chunk.payload["text"] == "text-A"
