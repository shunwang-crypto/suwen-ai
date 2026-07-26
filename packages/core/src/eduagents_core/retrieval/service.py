from __future__ import annotations

from eduagents_core.config import Settings
from eduagents_core.embedding.base import EmbeddingProvider, Reranker, SparseEncoder
from eduagents_core.retrieval.base import RetrievalResult, RetrievedChunk, rrf_fuse
from eduagents_core.vectorstore.base import SearchHit, VectorStore


class RetrievalService:
    """混合检索: dense + (可选) sparse -> RRF 融合 -> (可选) rerank -> top final_k。

    每个策略开关 (settings.retrieval_enable_sparse / retrieval_enable_rerank)
    独立、真实生效, 供 M2-D5 消融实验对比。过程字段全部回填到
    RetrievalResult (dense_hits/sparse_hits/used_sparse/used_rerank/confident)。
    """

    def __init__(
        self,
        settings: Settings,
        embedder: EmbeddingProvider,
        sparse: SparseEncoder,
        store: VectorStore,
        reranker: Reranker,
    ) -> None:
        self._settings = settings
        self._embedder = embedder
        self._sparse = sparse
        self._store = store
        self._reranker = reranker

    async def retrieve(self, tenant_id: str, kb_id: str, query: str) -> RetrievalResult:
        s = self._settings
        candidate_k = s.retrieval_candidate_k
        final_k = s.retrieval_final_k

        # a) dense 通道
        dense_vec = (await self._embedder.embed([query]))[0]
        dense_hits = await self._store.search_dense(tenant_id, kb_id, dense_vec, candidate_k)

        # b) sparse 通道 (开关生效; 关闭时完全不触发 sparse 编码与检索)
        used_sparse = s.retrieval_enable_sparse
        sparse_hits: list[SearchHit] = []
        if used_sparse:
            sparse_query = self._sparse.encode_query(query)
            sparse_hits = await self._store.search_sparse(
                tenant_id, kb_id, sparse_query, candidate_k
            )

        hit_by_id: dict[str, SearchHit] = {}
        in_dense: set[str] = set()
        in_sparse: set[str] = set()
        for hit in dense_hits:
            hit_by_id.setdefault(hit.id, hit)
            in_dense.add(hit.id)
        for hit in sparse_hits:
            hit_by_id.setdefault(hit.id, hit)
            in_sparse.add(hit.id)

        # c) RRF 融合两路有序 id; 只有一路时直接沿用该路排序与原始分
        if used_sparse:
            fused = rrf_fuse(
                [[h.id for h in dense_hits], [h.id for h in sparse_hits]],
                k=s.rrf_k,
            )
            # sorted 稳定: 同分时保持 dense 优先的插入序, 结果确定性
            ordered = sorted(fused.items(), key=lambda kv: kv[1], reverse=True)
        else:
            ordered = [(h.id, h.score) for h in dense_hits]
        ordered = ordered[:candidate_k]

        chunks: list[RetrievedChunk] = []
        for chunk_id, score in ordered:
            hit = hit_by_id[chunk_id]
            payload = hit.payload or {}
            if chunk_id in in_dense and chunk_id in in_sparse:
                source = "fused"
            elif chunk_id in in_sparse:
                source = "sparse"
            else:
                source = "dense"
            chunks.append(
                RetrievedChunk(
                    chunk_id=chunk_id,
                    text=payload.get("text", ""),
                    heading_path=payload.get("heading_path", ""),
                    doc_id=payload.get("doc_id", ""),
                    page_start=payload.get("page_start"),
                    page_end=payload.get("page_end"),
                    score=score,
                    source=source,
                    payload=payload,
                )
            )

        # d) rerank (开) 重排取 final_k; (关) 按融合分截断 final_k
        used_rerank = s.retrieval_enable_rerank
        confident = False
        if used_rerank and chunks:
            scores = await self._reranker.rerank(query, [c.text for c in chunks])
            for chunk, rerank_score in zip(chunks, scores, strict=True):
                chunk.rerank_score = rerank_score
            chunks.sort(key=lambda c: c.rerank_score or 0.0, reverse=True)
            top = chunks[:final_k]
            # e) confident 门控: top final_k 的 rerank_score 均 >= 阈值 (§5.3 快路径)
            confident = bool(top) and all(
                c.rerank_score is not None
                and c.rerank_score >= s.retrieval_confidence_threshold
                for c in top
            )
        else:
            top = chunks[:final_k]
            # rerank 关闭时无可信度依据, 恒 False

        return RetrievalResult(
            chunks=top,
            dense_hits=len(dense_hits),
            sparse_hits=len(sparse_hits),
            used_sparse=used_sparse,
            used_rerank=used_rerank,
            confident=confident,
        )
