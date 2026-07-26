from __future__ import annotations

from typing import Protocol


class EmbeddingProvider(Protocol):
    """dense 向量提供者。live=SiliconFlow BGE-M3; stub=确定性哈希向量 (dev/CI)。
    实现见 embedding/providers.py, 按 settings.embedding_mode 由工厂选择。"""

    dim: int

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


class SparseEncoder(Protocol):
    """BM25 风格稀疏编码 (本地, 零 API): jieba 分词 + 词项哈希 -> {index: weight}。
    文档端与查询端权重公式不同, 分开两个方法。实现见 embedding/sparse.py。"""

    def encode_document(self, text: str) -> dict[int, float]: ...

    def encode_query(self, text: str) -> dict[int, float]: ...


class Reranker(Protocol):
    """精排。live=SiliconFlow bge-reranker-v2-m3; stub=确定性词面重合分。
    返回与 documents 等长的分数列表 (越大越相关)。"""

    async def rerank(self, query: str, documents: list[str]) -> list[float]: ...
